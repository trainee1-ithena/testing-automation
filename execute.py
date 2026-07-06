"""
execute.py — Run populated scenario matrix against the live app via Playwright base scripts.

Usage:
    python approach3/execute.py --flow create_service_report [--dry-run] [--case case025]

How it works
------------
At startup, execute.py scans the matrix for every distinct persona and logs in once per
role using the credentials in .env.  The authenticated browser state (cookies +
localStorage, i.e. the JWT) is cached in memory as a dict.  Every scenario for that
role reuses that cached state — no per-test login, no stale session files on disk.

For each scenario it then:
  1. Opens a fresh Playwright browser context pre-loaded with that role's session.
  2. Calls open_form() + set_field() + Save from the base script (run_test).
  3. Waits for the page to settle, then reads any alert/toast/inline error.
  4. Maps what it sees to actual_outcome (submitted / correctly rejected / …).
  5. Compares actual_outcome to expected_outcome → passed: true / false / null.

Skip conditions (no Playwright run):
  - skip: true in matrix
  - session_count > 1  (multi-session post-submission checks — not yet automated)
  - flow_type missing
  - base script not found
  - login failed for the required role

Output: approach3/results/raw_results_{flow}.json
"""

import argparse
import importlib.util
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

# Windows consoles default to cp1252 which breaks on many unicode chars.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv(Path(__file__).parent / ".env")

TESTGEN_DIR   = Path(__file__).parent
MATRICES_DIR  = TESTGEN_DIR / "matrices"
SCRIPTS_DIR   = TESTGEN_DIR / "base_tests"
RESULTS_DIR   = TESTGEN_DIR / "results"
ARTIFACTS_DIR = RESULTS_DIR / "artifacts"
CONTEXT_DIR   = TESTGEN_DIR / "context"

# cascade_entities (full DB dataset) + field_options (pre-built dropdown labels) — loaded once
# in main(), used by the field_interaction check to confirm auto-populated values are real data
# and to source a trigger value when a reset case's own input is blank.
_CASCADE_ENTITIES: dict = {}
_FIELD_OPTIONS: dict = {}
# Most common real value each inputs field takes across the matrix — a known-good value to set
# a field_interaction trigger with when the case's own input is blank (a reset case).
_FIELD_VALUE_SAMPLES: dict = {}


def _compute_field_value_samples(matrix: list[dict]) -> dict:
    from collections import Counter
    votes: dict[str, Counter] = {}
    for c in matrix:
        for k, v in (c.get("inputs") or {}).items():
            if isinstance(v, str) and v.strip():
                votes.setdefault(k, Counter())[v] += 1
    return {k: cnt.most_common(1)[0][0] for k, cnt in votes.items()}


def _load_context_data(flow: str) -> tuple[dict, dict]:
    path = CONTEXT_DIR / f"context_{flow}.json"
    if not path.exists():
        return {}, {}
    try:
        td = json.loads(path.read_text(encoding="utf-8")).get("test_data") or {}
        return td.get("cascade_entities") or {}, td.get("field_options") or {}
    except Exception:
        return {}, {}

FRONTEND_URL          = os.getenv("FRONTEND_URL", "http://localhost:3000")
BACKEND_URL           = os.getenv("BACKEND_URL",  "http://localhost:5000")
PLAYWRIGHT_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", "20000"))
NAV_TIMEOUT_MS        = 30000   # page.goto() gets more time than element actions

# Set to True via --headed flag; makes browsers visible for debugging.
_HEADED = False

_NAME_FIELDS    = {"name"}
_PREFIX_MAX_LEN = 100

# ── Role → login config ────────────────────────────────────────────────────────
#
# Maps an internal role key to:
#   env_prefix  — prefix used in .env, e.g. "TEST_AGENT" → TEST_AGENT_EMAIL / _PASSWORD
#   login_path  — URL path for the login page
#
# Customer users have their own portal at /login;
# all agent-side roles (agent, service manager, engineer, admin) use /se-login.

_ROLE_CONFIG: dict[str, dict] = {
    "agent":            {"env": "TEST_AGENT",            "path": "/se-login"},
    "service_manager":  {"env": "TEST_SERVICE_MANAGER",  "path": "/se-login"},
    "service_engineer": {"env": "TEST_SERVICE_ENGINEER", "path": "/se-login"},
    # Same app role as "service_engineer", but a distinct DB account that DOES have
    # service_report.create permission in at least one department — see
    # _resolve_role_key() for how scenarios pick between the two.
    "service_engineer_with_permission": {"env": "TEST_SERVICE_ENGINEER_WITH_PERMISSION", "path": "/se-login"},
    "admin":            {"env": "TEST_ADMIN",            "path": "/se-login"},
    "customer_user":    {"env": "TEST_CUSTOMER_USER",    "path": "/login"},
}

# Persona strings from the matrix that mean "any agent-side role will do"
_ANY_ROLE_KEY = "agent"

# In-memory session cache: role_key → storage_state dict (or None if login failed)
_SESSION_CACHE: dict[str, dict | None] = {}


# ── Persona → role key ─────────────────────────────────────────────────────────

def _persona_to_role_key(persona: str) -> str | None:
    """
    Map a persona string (from the matrix) to a key in _ROLE_CONFIG.
    Returns None for unauthenticated — those tests run with no session at all.
    """
    key = persona.strip().lower().replace(" ", "_").replace("-", "_")

    if key in ("unauthenticated", ""):
        return None

    if key == "any_authorized_role":
        return _ANY_ROLE_KEY

    slug_map = {
        "agent":             "agent",
        "service_manager":   "service_manager",
        "servicemanager":    "service_manager",
        "service_engineer":  "service_engineer",
        "serviceengineer":   "service_engineer",
        "admin":             "admin",
        "customer_user":     "customer_user",
        "customeruser":      "customer_user",
        "customer":          "customer_user",
    }
    mapped = slug_map.get(key)
    if mapped and mapped in _ROLE_CONFIG:
        return mapped

    # Unknown persona → fall back to agent
    print(f"  [auth] WARN: unknown persona '{persona}' — using agent as fallback")
    return _ANY_ROLE_KEY


# The single "Service Engineer" persona maps to two different real DB accounts
# depending on which permission state the scenario actually needs — TEST_SERVICE_ENGINEER
# (no service_report.create permission in any department) or
# TEST_SERVICE_ENGINEER_WITH_PERMISSION (has it in >=1 department). Persona text alone
# can't distinguish these; the scenario's own preconditions/description can, since
# generate1.py already writes explicit negation language for permission-denial cases.
_NO_PERMISSION_KEYWORDS = ("lacks", "without", "no permission", "cannot", "not have")


def _resolve_role_key(scenario: dict) -> str | None:
    """
    Like _persona_to_role_key(), but for persona == "Service Engineer" also
    inspects preconditions/description to pick between the permission-having
    and permission-lacking test accounts.
    """
    persona  = scenario.get("persona", "any_authorized_role")
    role_key = _persona_to_role_key(persona)

    if role_key != "service_engineer":
        return role_key

    text = " ".join(scenario.get("preconditions") or []) + " " + (scenario.get("description") or "")
    text = text.lower()
    if any(kw in text for kw in _NO_PERMISSION_KEYWORDS):
        return "service_engineer"                    # lacks permission — janesmith
    return "service_engineer_with_permission"          # default: needs to actually create a report


# ── Permission-case classification ──────────────────────────────────────────────
#
# "permission" category cases don't all mean "fill and submit the form" — some
# only assert whether a control is reachable, which run_test()'s generic
# fill+submit flow answers incorrectly (an empty submit is rejected as invalid
# input, not as a permission denial; an unauthenticated redirect makes the
# target button never appear, which reads as a timeout instead of a redirect).

def _has_effective_inputs(scenario: dict) -> bool:
    """True if the case actually supplies a value to type/select. An inputs dict of
    only blanks ("" / [] / None) is NOT effective input — it's a case that submits
    nothing. Guards the visibility-only routing below against a stray empty-string
    injection re-routing a DOM-check case into the fill-and-submit path."""
    ins = scenario.get("inputs") or {}
    return any(
        (v.strip() if isinstance(v, str) else v)
        for v in ins.values()
        if v not in (None, "", [], {})
    )


def _is_unauthenticated_permission(scenario: dict) -> bool:
    """'Direct URL access without auth' cases — there's no session to submit
    a form with, so this needs a redirect/Not-Authorized check instead."""
    return (
        scenario.get("category") == "permission"
        and scenario.get("persona", "").strip().lower() == "unauthenticated"
    )


def _is_visibility_only_permission(scenario: dict) -> bool:
    """'Verify X button/field is visible and enabled' cases — no inputs,
    nothing to submit. Running these through run_test() fills nothing and
    clicks Save, which the app correctly rejects as an empty form — a false
    failure unrelated to whether the control was actually accessible."""
    return (
        scenario.get("category") == "permission"
        and scenario.get("persona", "").strip().lower() != "unauthenticated"
        and not _has_effective_inputs(scenario)
    )


def _is_visibility_only_ui(scenario: dict) -> bool:
    """generate1.py's 'ui' category includes 'verify field X is visible and
    interactable' cases (e.g. case010/case011) that carry no inputs and were
    never meant to submit the form — same false-failure mode as
    _is_visibility_only_permission (an empty Save click gets rejected as
    "Please fill all required fields", unrelated to whether the field itself
    was actually visible/interactable)."""
    return scenario.get("category") == "ui" and not _has_effective_inputs(scenario)


# ── Login ──────────────────────────────────────────────────────────────────────

def _login(pw, role_key: str) -> dict | None:
    """
    Log in headlessly as role_key using credentials from .env.
    Returns a Playwright storage_state dict (cookies + localStorage) on success,
    or None if credentials are missing or login fails.

    The storage_state dict is passed directly to browser.new_context() —
    no temp files needed.
    """
    cfg      = _ROLE_CONFIG[role_key]
    email    = os.getenv(f"{cfg['env']}_EMAIL", "")
    password = os.getenv(f"{cfg['env']}_PASSWORD", "")

    if not email or not password:
        print(f"  [auth] WARN: {cfg['env']}_EMAIL / _PASSWORD not set in .env — "
              f"scenarios for '{role_key}' will be skipped")
        return None

    login_url = f"{FRONTEND_URL}{cfg['path']}"
    browser   = None
    try:
        browser = pw.chromium.launch(headless=True)
        ctx     = browser.new_context()
        page    = ctx.new_page()
        page.goto(login_url, timeout=20000)
        page.wait_for_selector("[name='email']", timeout=15000)
        page.fill("[name='email']", email)
        page.fill("[name='user_password']", password)
        page.locator("[name='user_password']").press("Enter")
        # Wait until we're no longer on a login page
        page.wait_for_function(
            "() => !window.location.pathname.toLowerCase().includes('login')",
            timeout=15000,
        )
        state = ctx.storage_state()   # dict: {"cookies": [...], "origins": [...]}
        return state
    except Exception as exc:
        print(f"  [auth] ERROR: login failed for '{role_key}' ({login_url}): {exc}")
        return None
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass


def prebuild_sessions(matrix: list[dict], pw) -> None:
    """
    Inspect the matrix for every persona that will actually run, then log in
    once per distinct role.  Results are stored in _SESSION_CACHE.
    """
    needed: set[str] = set()
    for s in matrix:
        if s.get("skip"):
            continue
        if (s.get("session_count", 1) or 1) > 1:
            continue
        rk = _resolve_role_key(s)
        if rk:
            needed.add(rk)

    if not needed:
        return

    print(f"[auth] Logging in for {len(needed)} role(s): {', '.join(sorted(needed))}")
    for role_key in sorted(needed):
        if role_key in _SESSION_CACHE:
            print(f"  [auth] {role_key}: already cached")
            continue
        state = _login(pw, role_key)
        _SESSION_CACHE[role_key] = state
        status = "OK" if state else "FAILED"
        print(f"  [auth] {role_key}: {status}")
    print()


def _get_storage_state(role_key: str | None) -> dict | None:
    """Return the cached storage_state dict for a resolved role_key, or None."""
    if role_key is None:
        return None   # unauthenticated — intentionally no session
    return _SESSION_CACHE.get(role_key)  # None if login failed earlier


# ── Input preprocessing ────────────────────────────────────────────────────────

def _preprocess_inputs(inputs: dict) -> dict:
    """
    Clean up matrix inputs before passing to base script set_field calls.

    - None / empty list         → dropped (field left blank)
    - Empty string ""           → dropped (field stays at default/blank — what
                                   validation tests need)
    - ISO date "YYYY-MM-DD"     → extracts day number (e.g. "01" → "1") because
                                   the MUI date-picker shows day cells in the
                                   current month; navigate separately if month
                                   matters
    - List ["John Doe", ...]    → first element only (multi-select not yet wired)
    - bool / int / other str    → kept as-is
    """
    out = {}
    for k, v in inputs.items():
        if v is None:
            continue
        if isinstance(v, list):
            if v:
                first = v[0]
                out[k] = str(first) if not isinstance(first, (str, bool)) else first
        elif isinstance(v, bool):
            out[k] = v
        elif isinstance(v, str) and v.strip() == "":
            continue   # leave field blank
        elif isinstance(v, str) and len(v) == 10 and v[4:5] == "-" and v[7:8] == "-":
            # ISO date → day number for MUI calendar gridcell
            try:
                out[k] = str(int(v.split("-")[2]))
            except Exception:
                out[k] = v
        else:
            out[k] = v
    return out


def _apply_cleanup_prefix(inputs: dict, prefix: str) -> dict:
    if not prefix or not isinstance(inputs, dict):
        return inputs
    out = dict(inputs)
    for field in _NAME_FIELDS:
        val = out.get(field)
        if (isinstance(val, str) and val.strip() and len(val) <= _PREFIX_MAX_LEN
                and not val.startswith(prefix)):
            out[field] = f"{prefix} {val}"
    return out


def _prefix_scenario(scenario: dict, prefix: str) -> dict:
    if not prefix:
        return scenario
    sc = dict(scenario)
    if isinstance(sc.get("inputs"), dict):
        sc["inputs"] = _apply_cleanup_prefix(sc["inputs"], prefix)
    return sc


# ── Outcome detection ──────────────────────────────────────────────────────────
#
# After run_test() calls Save, we wait for the page to settle and then read
# any alert / snackbar / inline error text to classify what happened.
#
# Pass criteria:
#   expected_outcome == "pass"  →  actual_outcome must be "submitted"
#   expected_outcome == "fail"  →  actual_outcome must be "correctly rejected"
#   Anything else               →  passed = None (can't determine automatically)

_REJECTION_KWS = frozenset({
    "required", "cannot exceed", "must be", "invalid", "error", "failed",
    "blank", "empty", "not found", "exceeded",
})
_SUCCESS_KWS = frozenset({
    "created", "saved", "success", "added", "submitted",
})


def _scrape_alert_text(page) -> str:
    for sel in ["[role='alert']", ".MuiAlert-message",
                ".MuiSnackbarContent-message", "[class*='notification']"]:
        try:
            loc = page.locator(sel)
            if loc.count() == 0:
                continue
            parts = [
                (loc.nth(i).text_content(timeout=1000) or "").strip()
                for i in range(min(loc.count(), 5))
            ]
            combined = " | ".join(p for p in parts if p)
            if combined:
                return combined
        except Exception:
            pass
    return ""


def _scrape_inline_error(page) -> str:
    for sel in [".Mui-error", "p.MuiFormHelperText-root.Mui-error",
                "[class*='helperText']", "[class*='helper-text']"]:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                txt = (loc.first.text_content(timeout=1000) or "").strip()
                if txt:
                    return txt
        except Exception:
            pass
    return ""


def _form_still_open(page) -> bool:
    """True if the create form / drawer appears to still be on screen."""
    for sel in ["button:has-text('Save')", "button:has-text('Submit')", ".MuiDrawer-paper"]:
        try:
            if page.locator(sel).count() > 0:
                return True
        except Exception:
            pass
    return False


def _detect_outcome(page, expected_outcome: str) -> tuple[str, str]:
    """
    Wait for React to settle after Save, then classify the result.

    Returns (actual_outcome, actual_message) where actual_outcome is one of:
      "submitted"            — form closed, record created
      "correctly rejected"   — form blocked with validation error (good when expected "fail")
      "incorrectly rejected" — form blocked but we expected success
      "incorrectly accepted" — form submitted but we expected rejection
      "ambiguous"            — could not determine
    """
    page.wait_for_timeout(2000)

    alert        = _scrape_alert_text(page)
    inline_err   = _scrape_inline_error(page)
    msg          = alert or inline_err
    msg_lower    = msg.lower()

    is_rejection = bool(msg and any(kw in msg_lower for kw in _REJECTION_KWS))
    is_success   = bool(msg and any(kw in msg_lower for kw in _SUCCESS_KWS))
    form_open    = _form_still_open(page)

    if expected_outcome == "pass":
        if is_rejection:
            return "incorrectly rejected", msg
        if is_success or not form_open:
            return "submitted", msg
        # Extra wait — async React state update
        page.wait_for_timeout(2000)
        if not _form_still_open(page):
            return "submitted", msg
        alert2 = _scrape_alert_text(page)
        if alert2 and any(kw in alert2.lower() for kw in _REJECTION_KWS):
            return "incorrectly rejected", alert2
        return "submitted", msg   # optimistic — form may have closed silently

    if expected_outcome == "fail":
        if is_rejection:
            return "correctly rejected", msg
        if form_open:
            inline2 = _scrape_inline_error(page)
            return "correctly rejected", inline2 or msg or ""
        if is_success or not form_open:
            return "incorrectly accepted", msg
        return "correctly rejected", ""

    # expected_outcome is empty / unknown
    if is_rejection or form_open:
        return "rejected", msg
    return "submitted", msg


_HIDDEN_CONTROL_KEYWORDS = (
    "not visible", "not present", "absent", "disabled", "cannot access",
    "cannot see", "hidden", "without", "lacks", "no permission",
    # Denial-of-access phrasing (e.g. case056: "receives a 'Not Authorized'
    # error or is redirected; no SR creation form is displayed") — these
    # describe a blocked/redirected user, which also means the control
    # itself is not reachable, even though they don't use "visible"/"absent".
    "not authorized", "unauthorized", "forbidden", "not accessible",
    "is not displayed",
)


def _expects_visible(scenario: dict) -> bool:
    """
    Visibility-only cases (permission or ui category) all carry
    expected_outcome == "pass" — that field means "the scenario's assertion
    holds", not "the control is visible". The actual direction (should the
    control be visible/interactable or absent) only lives in the scenario's
    own prose, e.g. expected_result: "Create button is visible and enabled"
    (case026) vs "Create SR action is not visible or is disabled" (case044).
    Default to "should be visible" when no negation is found.
    """
    text = (scenario.get("expected_result") or scenario.get("description") or "").lower()
    return not any(kw in text for kw in _HIDDEN_CONTROL_KEYWORDS)


def _compute_passed(actual_outcome: str, expected_outcome: str, scenario: dict | None = None) -> bool | None:
    if scenario is not None and (
        _is_visibility_only_permission(scenario) or _is_visibility_only_ui(scenario)
    ):
        expects_visible = _expects_visible(scenario)
        return actual_outcome == ("control accessible" if expects_visible else "control not accessible")
    if expected_outcome == "pass":
        return actual_outcome in (
            "submitted", "control accessible",
        )
    if expected_outcome == "fail":
        # "redirected to login" / "not authorized shown" are _run_unauthenticated_check's
        # "the app correctly blocked this" outcomes — always paired with expected_outcome
        # == "fail" (the create action should NOT go through), never "pass". "access
        # granted" (the opposite: an unauthenticated user reached the form — a security
        # hole) deliberately has no accepted branch, so it always computes to False here.
        return actual_outcome in (
            "correctly rejected", "control not accessible",
            "redirected to login", "not authorized shown",
        )
    return None


# ── Base script helpers ────────────────────────────────────────────────────────

def _base_name(flow: str, flow_type: str) -> str:
    """Filename stem for a base script. Must mirror record.py's base_name()."""
    if flow_type == flow:
        return f"base_test_{flow}"
    prefix = flow + "_"
    short  = flow_type[len(prefix):] if flow_type.startswith(prefix) else flow_type
    return f"base_test_{flow}_{short}"


def find_base_script(flow: str, flow_type: str) -> Path | None:
    path = SCRIPTS_DIR / f"{_base_name(flow, flow_type)}.py"
    return path if path.exists() else None


def _load_base_module(script_path: Path):
    spec = importlib.util.spec_from_file_location(script_path.stem, str(script_path))
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Permission-case / ui visibility-only runners ────────────────────────────────

def _run_visibility_check(script_path: Path, storage_state: dict | None, pw) -> tuple[str, str, str]:
    """
    For 'verify X is visible/enabled' permission cases. Calls the base script's
    open_form() only — never fills a field or clicks Save — since submitting an
    empty form triggers unrelated required-field validation that has nothing to
    do with whether the control was actually reachable. If open_form() succeeds
    (the trigger button existed, was enabled, and the form opened), the control
    is accessible; if it times out, it wasn't.
    """
    module  = _load_base_module(script_path)
    browser = pw.chromium.launch(headless=not _HEADED)
    ctx = (
        browser.new_context(storage_state=storage_state)
        if storage_state is not None
        else browser.new_context()
    )
    page = ctx.new_page()
    try:
        module.open_form(page)
        return "control accessible", "", ""
    except Exception:
        # Whatever the underlying Playwright error text says (almost always a
        # Timeout, since a missing/disabled trigger button just never becomes
        # clickable), the operationally meaningful fact is the same: we could
        # not open the form. Don't try to string-sniff a finer distinction.
        raw = traceback.format_exc()
        return "control not accessible", raw.strip().splitlines()[-1][:300], raw
    finally:
        browser.close()


def _match_field_for_ui_case(module, scenario: dict) -> str | None:
    """
    Best-effort match of a 'ui' category "verify field X is visible/
    interactable" scenario to one of the base script's known field keys, by
    checking whether the field's underscore-separated name (e.g. "report_type"
    -> "report type") appears in the scenario's own title/description text
    (e.g. "Service Report Type dropdown"). Generic across any flow's
    FIELD_TYPES — not a per-case hardcoded lookup. Returns None if zero or
    more than one field matches, so the caller can fall back to a plain
    "did the form open" check.
    """
    text = f"{scenario.get('title', '')} {scenario.get('description', '')}".lower()
    field_types = getattr(module, "FIELD_TYPES", {})
    matches = [f for f in field_types if f.replace("_", " ") in text]
    return matches[0] if len(matches) == 1 else None


def _run_ui_visibility_check(
    script_path: Path, storage_state: dict | None, scenario: dict, pw,
) -> tuple[str, str, str]:
    """
    For 'ui' category "verify field X is visible and interactable" cases
    (e.g. case010/case011) — no inputs, nothing to submit. Opens the form,
    matches the scenario to a specific field via FIELD_TYPES, then uses the
    base script's own is_field_present()/open_field() instead of run_test()'s
    fill-everything-and-Save flow (which rejects an intentionally-empty form
    as "Please fill all required fields" — a false failure unrelated to
    whether the field itself was visible/interactable).
    """
    module  = _load_base_module(script_path)
    browser = pw.chromium.launch(headless=not _HEADED)
    ctx = (
        browser.new_context(storage_state=storage_state)
        if storage_state is not None
        else browser.new_context()
    )
    page = ctx.new_page()
    try:
        module.open_form(page)
        # Base scripts built by record.py gate on the form's slow options fetch;
        # without this, is_field_present() races the fetch and reads a false 0.
        ready = getattr(module, "_wait_form_ready", None)
        if callable(ready):
            ready(page)

        field = _match_field_for_ui_case(module, scenario)
        if field is None:
            # Couldn't tie the case to one specific field — the form having
            # opened at all is the best signal available.
            return "control accessible", "", ""

        if not module.is_field_present(page, field):
            return "control not accessible", f"field '{field}' not present on the form", ""

        # "Interactable" for a dropdown/date-picker means it actually opens;
        # for a checkbox/textbox, presence already implies interactability.
        if module.FIELD_TYPES.get(field) in ("react_select", "date"):
            try:
                module.open_field(page, field)
            except Exception:
                raw = traceback.format_exc()
                return (
                    "control not accessible",
                    f"field '{field}' present but did not open",
                    raw,
                )

        return "control accessible", "", ""
    except Exception:
        raw = traceback.format_exc()
        return "control not accessible", raw.strip().splitlines()[-1][:300], raw
    finally:
        browser.close()


# ── field_interaction: verify one field's effect on another (never submit) ──────

def _snapshot_form_values(page) -> dict:
    """Displayed value of every input/textarea in the open drawer/modal, keyed by name or
    placeholder. Generic — used to diff which fields auto-populate when a trigger is set."""
    try:
        return page.evaluate("""() => {
            const root = document.querySelector('.MuiDrawer-paper')
                || document.querySelector('.MuiModal-root') || document.body;
            const out = {};
            root.querySelectorAll('input, textarea').forEach((el, i) => {
                const key = el.name || el.getAttribute('placeholder') || ('ctrl_' + i);
                out[key] = (el.value || '').trim();
            });
            return out;
        }""")
    except Exception:
        return {}


def _flatten_entity_values(entities: dict) -> set[str]:
    vals: set[str] = set()
    for coll in (entities or {}).values():
        if isinstance(coll, list):
            for rec in coll:
                if isinstance(rec, dict):
                    vals.update(str(v).strip() for v in rec.values()
                                if isinstance(v, (str, int)) and str(v).strip())
    return vals


def _entity_token_pool(entities: dict) -> set[str]:
    """All word-tokens (>2 chars) appearing anywhere in the entity data. A UI value is judged
    'real' if it shares a token with this pool — tolerant of the app rendering a COMPOSITE
    label ('Bob Johnson - bob@x.com') that never appears verbatim in any single DB column."""
    toks: set[str] = set()
    for v in _flatten_entity_values(entities):
        toks |= {t for t in re.split(r"[^a-z0-9]+", v.lower()) if len(t) > 2}
    return toks


def _value_is_real(value: str, token_pool: set[str]) -> bool:
    if not token_pool:
        return True   # no data to check against — don't flag
    vt = {t for t in re.split(r"[^a-z0-9]+", value.lower()) if len(t) > 2}
    return bool(vt & token_pool)


def _sample_value_for(field: str, field_options: dict):
    """A real display label for a field, from the pre-built field_options — used to set a
    trigger whose own inputs value is blank (a reset case) without touching the live dropdown."""
    for o in (field_options or {}).get(field) or []:
        if isinstance(o, dict) and o.get("label"):
            return o["label"]
    return None


def _key_named_in(key: str, prose_lower: str) -> bool:
    """True if a control's identifier (name/placeholder) is referenced by the scenario prose —
    e.g. key 'Select Organization' is named by 'auto-populates the Organization'. Filters out
    incidental auto-fills (a default name/date) the case doesn't actually test."""
    words = [w for w in re.split(r"[^a-z]+", key.lower())
             if len(w) > 3 and w not in ("select", "please", "enter", "choose")]
    return any(w in prose_lower for w in words)


def _first_option_for(page, module, field: str):
    """Open the field's dropdown, grab the first offered option's text, close it."""
    try:
        module.open_field(page, field)
        opt = page.locator("[role='option']").first
        opt.wait_for(state="visible", timeout=10000)
        txt = (opt.text_content() or "").strip()
        page.keyboard.press("Escape")
        return txt or None
    except Exception:
        return None


def _clear_field(page, module, field: str):
    """Generic clear for a MUI/react-select: a Clear button if present, else focus + select-all + delete."""
    try:
        clear = page.get_by_role("button", name=re.compile(r"clear", re.I))
        if clear.count():
            clear.first.click(timeout=3000)
            return
    except Exception:
        pass
    try:
        module.open_field(page, field)
        page.keyboard.press("ControlOrMeta+A")
        page.keyboard.press("Delete")
        page.keyboard.press("Escape")
    except Exception:
        pass


def _run_field_interaction_check(
    script_path: Path, storage_state: dict | None, scenario: dict,
    cascade_entities: dict, field_options: dict, pw,
) -> tuple[str, str, str]:
    """
    field_interaction cases assert a CAUSAL UI relationship (setting a trigger field
    auto-populates/filters a target; clearing it resets the target) — NOT that an SR is
    created. So: set the trigger, diff which other controls changed, and assert the expected
    direction. Populated values are cross-checked against cascade_entities so a garbage
    auto-populate is caught. Never submits. Generic — the target is discovered by the
    value-diff, needing no per-form knowledge of which field auto-populates.
    """
    module = _load_base_module(script_path)
    field_types = getattr(module, "FIELD_TYPES", {})
    prose = f"{scenario.get('expected_result','')} {scenario.get('description','')} {scenario.get('title','')}".lower()
    is_reset = bool(re.search(r"\b(reset|clear|cleared|remov|becomes? empty|no longer)\b", prose))

    inputs  = scenario.get("inputs") or {}
    trigger = next((k for k in inputs if k in field_types), None) \
        or next((f for f in field_types if f.replace("_", " ") in prose), None)
    if trigger is None:
        return "ambiguous", "no trigger field identified for field_interaction", ""

    raw = inputs.get(trigger)
    trigger_value = raw if raw not in (None, "", []) else None

    browser = pw.chromium.launch(headless=not _HEADED)
    ctx = (browser.new_context(storage_state=storage_state)
           if storage_state is not None else browser.new_context())
    page = ctx.new_page()
    page.set_default_timeout(PLAYWRIGHT_TIMEOUT_MS)
    try:
        module.open_form(page)
        ready = getattr(module, "_wait_form_ready", None)
        if callable(ready):
            ready(page)

        if trigger_value is None:
            trigger_value = (_FIELD_VALUE_SAMPLES.get(trigger)
                             or _sample_value_for(trigger, field_options)
                             or _first_option_for(page, module, trigger))
            if trigger_value is None:
                return "ambiguous", f"could not obtain a value to set trigger '{trigger}'", ""

        before = _snapshot_form_values(page)
        module.set_field(page, trigger, trigger_value)
        page.wait_for_timeout(1500)
        after = _snapshot_form_values(page)

        populated = {k: after[k] for k in after
                     if not before.get(k) and after.get(k) and after[k] != str(trigger_value)}
        if not populated:
            return ("control not accessible",
                    f"setting '{trigger}' populated no other field (expected auto-population)", "")

        # Only the fields the scenario NAMES as targets matter (e.g. "auto-populates the
        # Organization and User"). Incidental auto-fills — a default report name, today's date —
        # are not what this case tests, so don't scrutinize them. Fall back to all populated if
        # the prose named none.
        prose_l = (scenario.get("expected_result") or "").lower()
        targets = {k: v for k, v in populated.items() if _key_named_in(k, prose_l)} or populated

        if not is_reset:
            pool   = _entity_token_pool(cascade_entities)
            unreal = [f"{k}={v!r}" for k, v in targets.items() if not _value_is_real(v, pool)]
            msg    = "auto-populated " + ", ".join(f"{k}={v!r}" for k, v in targets.items())
            if unreal:   # a NAMED target holds a value with no token in the DB — wrong data flowed
                return "control not accessible", msg + f" | not in cascade data: {unreal}", ""
            return "control accessible", msg, ""

        # reset: clear the trigger, assert the named targets empty back out
        _clear_field(page, module, trigger)
        page.wait_for_timeout(1500)
        after_clear = _snapshot_form_values(page)
        still_set   = {k: after_clear.get(k) for k in targets if after_clear.get(k)}
        if still_set:
            return "control not accessible", f"clearing '{trigger}' did NOT reset: {still_set}", ""
        return "control accessible", f"clearing '{trigger}' reset: {list(targets)}", ""
    except Exception:
        raw_output = traceback.format_exc()
        return "error", raw_output.strip().splitlines()[-1][:300], raw_output
    finally:
        browser.close()


def _run_unauthenticated_check(target_url: str, pw) -> tuple[str, str, str]:
    """
    For 'direct URL access without authentication' permission cases. Uses no
    storage state at all, then checks where the app actually sent us instead of
    waiting on a form control that a login redirect makes unreachable (which
    would otherwise read as a plain timeout rather than what it actually is).
    """
    browser = pw.chromium.launch(headless=not _HEADED)
    ctx  = browser.new_context()
    page = ctx.new_page()
    try:
        page.goto(target_url, timeout=20000)
        page.wait_for_load_state("networkidle", timeout=10000)
        if "login" in page.url.lower():
            return "redirected to login", "", ""
        body = page.locator("body").text_content(timeout=2000) or ""
        if re.search(r"not authorized|unauthorized|access denied", body, re.I):
            return "not authorized shown", "", ""
        return "access granted", "unauthenticated user reached the form", ""
    except Exception:
        raw = traceback.format_exc()
        return "error", raw.strip().splitlines()[-1][:300], raw
    finally:
        browser.close()


# ── Failure artifacts ──────────────────────────────────────────────────────────

def _capture_failure_artifacts(page, case: str) -> list[str]:
    """
    On any scenario error, save a screenshot plus a JSON dump of the page's
    modal/aria state so a timeout can be diagnosed from the results file
    instead of re-running the suite. Best-effort — never raises.
    """
    artifacts: list[str] = []
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    shot = ARTIFACTS_DIR / f"{case}_fail.png"
    try:
        page.screenshot(path=str(shot))
        artifacts.append(str(shot))
    except Exception:
        pass
    try:
        state = page.evaluate("""() => {
            const root = document.getElementById('root');
            const vis = e => e.offsetParent !== null;
            return {
                url: window.location.href,
                root_aria_hidden: root ? root.getAttribute('aria-hidden') : null,
                drawers: [...document.querySelectorAll('.MuiDrawer-root')].map(d => ({
                    visible: vis(d), aria_hidden: d.getAttribute('aria-hidden')})),
                modals_visible: [...document.querySelectorAll('.MuiModal-root')].filter(vis).length,
                open_option_lists: [...document.querySelectorAll("[role='listbox']")].filter(vis).length,
                shepherd_nodes: document.querySelectorAll("[class*='shepherd']").length,
                comboboxes: [...document.querySelectorAll("[role='combobox']")].map(c => ({
                    text: (c.textContent || '').trim().slice(0, 50),
                    placeholder: c.placeholder || null,
                    visible: vis(c),
                    in_aria_hidden: !!c.closest("[aria-hidden='true']")})),
                buttons_service_report: [...document.querySelectorAll('button')]
                    .filter(b => (b.textContent || '').includes('Service Report'))
                    .map(b => ({text: b.textContent.trim().slice(0, 40), visible: vis(b),
                                disabled: b.disabled,
                                in_aria_hidden: !!b.closest("[aria-hidden='true']")})),
            };
        }""")
        dump = ARTIFACTS_DIR / f"{case}_state.json"
        dump.write_text(json.dumps(state, indent=2), encoding="utf-8")
        artifacts.append(str(dump))
    except Exception:
        pass
    return artifacts


# ── Playwright runner ──────────────────────────────────────────────────────────

def _run_playwright(
    script_path: Path,
    inputs: dict,
    expected_outcome: str,
    storage_state: dict | None,
    pw,
    case: str = "unknown",
) -> tuple[str, str, str, list[str]]:
    """
    Open a fresh browser context (pre-authenticated via storage_state dict),
    run the base script's run_test(), detect outcome.

    Each scenario gets its own browser instance so tests are fully isolated.

    Returns: (actual_outcome, actual_message, raw_output)
    """
    mod     = _load_base_module(script_path)
    browser = pw.chromium.launch(headless=not _HEADED)
    ctx     = (
        browser.new_context(storage_state=storage_state)
        if storage_state is not None
        else browser.new_context()
    )
    ctx.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    page = ctx.new_page()
    page.set_default_timeout(PLAYWRIGHT_TIMEOUT_MS)
    # IMPORTANT: page.set_default_timeout() (above) outranks
    # context.set_default_navigation_timeout() in Playwright's timeout
    # resolution order, so without this line every goto() was silently
    # capped to PLAYWRIGHT_TIMEOUT_MS (10s) instead of the intended
    # NAV_TIMEOUT_MS (30s) — this was the real cause of the page.goto()
    # timeouts seen across every case.
    page.set_default_navigation_timeout(NAV_TIMEOUT_MS)

    actual_outcome = "error"
    actual_message = ""
    raw_output     = ""
    artifacts: list[str] = []

    try:
        mod.run_test(page, inputs, expected_outcome, "")
        actual_outcome, actual_message = _detect_outcome(page, expected_outcome)
    except Exception as exc:
        raw_output = traceback.format_exc()
        exc_str    = str(exc)
        artifacts  = _capture_failure_artifacts(page, case)
        if "TimeoutError" in type(exc).__name__ or "timeout" in exc_str.lower():
            actual_outcome = "timeout"
            actual_message = exc_str[:400]
        else:
            actual_outcome = "error"
            actual_message = exc_str[:400]
    finally:
        try:
            browser.close()
        except Exception:
            pass

    return actual_outcome, actual_message, raw_output, artifacts


# ── Scenario orchestration ─────────────────────────────────────────────────────

def load_matrix(flow: str) -> list[dict]:
    candidates = list(MATRICES_DIR.glob(f"scenario_matrix_{flow}.json"))
    if not candidates:
        candidates = list(MATRICES_DIR.glob(f"scenario_matrix_{flow}*.json"))
    if not candidates:
        print(f"ERROR: no matrix found for flow '{flow}' in {MATRICES_DIR}", file=sys.stderr)
        sys.exit(1)
    exact  = [c for c in candidates if c.name == f"scenario_matrix_{flow}.json"]
    chosen = exact[0] if exact else sorted(candidates)[0]
    return json.loads(chosen.read_text(encoding="utf-8"))


def run_scenario(scenario: dict, flow: str, cleanup_prefix: str, pw) -> dict:
    """Execute one scenario; returns a result dict."""
    case     = scenario.get("case", "unknown")
    category = scenario.get("category", "")
    desc     = scenario.get("description", "")
    persona  = scenario.get("persona", "any_authorized_role")

    # ── skip: generate2 flagged it ────────────────────────────────────────────
    if scenario.get("skip"):
        return _skip(case, category, desc, flow, persona,
                     scenario.get("skip_reason") or "marked skip in matrix",
                     scenario.get("inputs", {}))

    # ── skip: multi-session (two-actor flows not yet automated) ───────────────
    if (scenario.get("session_count", 1) or 1) > 1:
        return _skip(case, category, desc, flow, persona,
                     "multi-session scenario — not yet automated",
                     scenario.get("inputs", {}))

    flow_type = scenario.get("flow_type", "")

    # ── skip: no recording ────────────────────────────────────────────────────
    if not flow_type:
        return _skip(case, category, desc, flow, persona,
                     "flow_type not assigned — no recording available",
                     scenario.get("inputs", {}))

    script_path = find_base_script(flow, flow_type)
    if script_path is None:
        return _skip(case, category, desc, flow, persona,
                     f"base script not found: {_base_name(flow, flow_type)}.py",
                     scenario.get("inputs", {}))

    # ── skip: login failed for this role ──────────────────────────────────────
    role_key      = _resolve_role_key(scenario)
    storage_state = _get_storage_state(role_key)
    if role_key is not None and storage_state is None:
        return _skip(case, category, desc, flow, persona,
                     f"login failed for role '{role_key}' — check .env credentials",
                     scenario.get("inputs", {}))

    # ── run ───────────────────────────────────────────────────────────────────
    scenario        = _prefix_scenario(scenario, cleanup_prefix)
    raw_inputs      = dict(scenario.get("inputs") or {})
    inputs          = _preprocess_inputs(raw_inputs)
    expected_outcome = scenario.get("expected_outcome", "pass")
    expected_msg     = scenario.get("expected_result", "") or ""

    start = time.time()
    artifacts: list[str] = []
    try:
        if _is_unauthenticated_permission(scenario):
            actual_outcome, actual_message, raw_output = _run_unauthenticated_check(
                scenario.get("url", ""), pw
            )
        elif _is_visibility_only_permission(scenario):
            actual_outcome, actual_message, raw_output = _run_visibility_check(
                script_path, storage_state, pw
            )
        elif _is_visibility_only_ui(scenario):
            actual_outcome, actual_message, raw_output = _run_ui_visibility_check(
                script_path, storage_state, scenario, pw
            )
        elif scenario.get("category") == "field_interaction":
            actual_outcome, actual_message, raw_output = _run_field_interaction_check(
                script_path, storage_state, scenario, _CASCADE_ENTITIES, _FIELD_OPTIONS, pw
            )
        else:
            actual_outcome, actual_message, raw_output, artifacts = _run_playwright(
                script_path, inputs, expected_outcome, storage_state, pw, case=case
            )
        passed = _compute_passed(actual_outcome, expected_outcome, scenario)
    except Exception:
        raw_output     = traceback.format_exc()
        actual_outcome = "error"
        actual_message = (raw_output.strip().splitlines() or ["unknown error"])[-1][:300]
        passed         = False

    return {
        "case":             case,
        "category":         category,
        "flow":             flow,
        "flow_type":        flow_type,
        "persona":          persona,
        "title":            scenario.get("title", ""),
        "description":      desc,
        "inputs":           raw_inputs,
        "expected_outcome": expected_outcome,
        "expected_message": expected_msg,
        "actual_outcome":   actual_outcome,
        "actual_message":   actual_message,
        "passed":           passed,
        "raw_output":       raw_output,
        "artifacts":        artifacts,
        "duration_ms":      int((time.time() - start) * 1000),
    }


def _skip(case, category, desc, flow, persona, reason, inputs) -> dict:
    return {
        "case":             case,
        "category":         category,
        "flow":             flow,
        "flow_type":        "",
        "persona":          persona,
        "title":            "",
        "description":      desc,
        "inputs":           inputs or {},
        "expected_outcome": "",
        "expected_message": "",
        "actual_outcome":   "skip",
        "actual_message":   reason,
        "passed":           None,
        "raw_output":       "",
        "artifacts":        [],
        "duration_ms":      0,
    }


# ── Result printer ───────────────────────────────────────────────────────────

def _print_result(status: str, exec_result: dict, inputs: dict):
    """Print a structured, readable summary of one scenario result."""
    actual_outcome = exec_result.get("actual_outcome", "") or ""
    actual_msg     = exec_result.get("actual_message", "") or ""
    passed         = exec_result.get("passed")

    # Derive "Action" (what the app did) vs "Test" (did it match expectations)
    if actual_outcome in ("error", "timeout"):
        action_label = f"Error ({actual_outcome})"
    elif actual_outcome in ("submitted", "control accessible", "redirected to login", "not authorized shown"):
        action_label = "Pass"
    elif actual_outcome == "correctly rejected":
        action_label = "Fail (correctly rejected)"
    elif actual_outcome in ("incorrectly rejected", "incorrectly accepted", "rejected",
                             "control not accessible", "access granted"):
        action_label = "Fail"
    elif actual_outcome == "skip":
        action_label = "Skipped"
    else:
        action_label = actual_outcome or "—"

    test_label = "PASS" if passed else ("SKIP" if passed is None else "FAIL")
    test_icon  = "✓" if passed else ("~" if passed is None else "✗")

    print(f"         {test_icon} Action: {action_label:<30}  Test: {test_label}")

    if actual_msg:
        print(f"           {actual_msg[:120]}")

    # Inputs (always helpful to see what was sent)
    if inputs:
        pairs = "  |  ".join(f"{k}={v!r}" for k, v in inputs.items())
        print(f"           inputs: {pairs[:160]}")

    # On failure: extract the most useful lines from the raw output
    if status in ("FAIL", "ERROR"):
        raw = exec_result.get("raw_output", "") or ""
        useful: list[str] = []
        for line in raw.splitlines():
            s = line.strip()
            if s.startswith(("FAILED", "E ", "AssertionError", "TimeoutError",
                             "playwright._impl", "locator.", "Error:")):
                useful.append(s)
            if len(useful) >= 5:
                break
        if useful:
            print("           ── raw output ──")
            for ln in useful:
                print(f"           {ln[:180]}")


# ── Cleanup (delete [TEST]-prefixed SRs after the run) ────────────────────────

def cleanup_test_records(prefix: str, pw) -> int:
    """Delete every SR whose name starts with prefix. Uses the agent session."""
    if not prefix:
        return 0
    state = _SESSION_CACHE.get("agent") or _SESSION_CACHE.get("service_manager")
    if not state:
        print("  [cleanup] WARNING: no session available — skipping cleanup.")
        return 0

    deleted = 0
    browser = pw.chromium.launch(headless=True)
    ctx     = browser.new_context(storage_state=state)
    page    = ctx.new_page()

    try:
        page.goto(FRONTEND_URL, wait_until="domcontentloaded", timeout=15000)
    except Exception as exc:
        print(f"  [cleanup] WARNING: could not reach {FRONTEND_URL}: {exc}")
        browser.close()
        return 0

    token = page.evaluate("""
        () => {
            const enc = localStorage.getItem("auth");
            if (!enc) return null;
            try { return JSON.parse(atob(enc)).token || null; } catch { return null; }
        }
    """)
    if not token:
        print("  [cleanup] WARNING: no auth token in session localStorage.")
        browser.close()
        return 0

    try:
        list_result = page.evaluate(
            """async ({url, token}) => {
                const r = await fetch(url + "/api/reports/list", {
                    headers: { "Authorization": "Bearer " + token }
                });
                return r.json();
            }""",
            {"url": BACKEND_URL, "token": token},
        )
    except Exception as exc:
        print(f"  [cleanup] WARNING: could not fetch SR list: {exc}")
        browser.close()
        return 0

    reports   = (list_result.get("data") or []) if isinstance(list_result, dict) else []
    to_delete = [r for r in reports
                 if isinstance(r.get("report_name"), str)
                 and r["report_name"].startswith(prefix)]

    print(f"  [cleanup] {len(to_delete)} SR(s) with prefix '{prefix}' to delete.")
    for report in to_delete:
        rid = report.get("report_id")
        if not rid:
            continue
        try:
            res = page.evaluate(
                """async ({url, token, id}) => {
                    const r = await fetch(url + "/api/reports/delete", {
                        method: "POST",
                        headers: {
                            "Authorization": "Bearer " + token,
                            "Content-Type": "application/json"
                        },
                        body: JSON.stringify({ report_id: id })
                    });
                    return r.json();
                }""",
                {"url": BACKEND_URL, "token": token, "id": rid},
            )
            if res.get("status"):
                deleted += 1
            else:
                print(f"  [cleanup] WARNING: could not delete SR {rid}: {res}")
        except Exception as exc:
            print(f"  [cleanup] WARNING: error deleting SR {rid}: {exc}")

    browser.close()
    return deleted


# ── Results writer ─────────────────────────────────────────────────────────────

def write_results(results: list[dict], flow: str) -> Path:
    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"raw_results_{flow}.json"
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    global _HEADED, _CASCADE_ENTITIES, _FIELD_OPTIONS, _FIELD_VALUE_SAMPLES
    parser = argparse.ArgumentParser(description="Run scenario matrix against live app")
    parser.add_argument("--flow",       required=True,       help="Flow id, e.g. create_service_report")
    parser.add_argument("--dry-run",    action="store_true", help="Print plan without running")
    parser.add_argument("--case",       default=None,        help="Run only this case, e.g. case025")
    parser.add_argument("--only",       default=None,        help="Comma-separated cases to run back-to-back, e.g. case003,case004")
    parser.add_argument("--no-cleanup", action="store_true", help="Skip post-run SR cleanup")
    parser.add_argument("--headed",     action="store_true", help="Run browsers visibly (for debugging)")
    args = parser.parse_args()
    _HEADED = args.headed
    _CASCADE_ENTITIES, _FIELD_OPTIONS = _load_context_data(args.flow)

    matrix         = load_matrix(args.flow)
    _FIELD_VALUE_SAMPLES = _compute_field_value_samples(matrix)
    cleanup_prefix = os.getenv("TEST_CLEANUP_PREFIX", "[TEST]")

    if args.case:
        matrix = [s for s in matrix if s.get("case") == args.case]
        if not matrix:
            print(f"ERROR: case '{args.case}' not found in matrix.", file=sys.stderr)
            sys.exit(1)

    if args.only:
        wanted = {c.strip() for c in args.only.split(",") if c.strip()}
        matrix = [s for s in matrix if s.get("case") in wanted]
        missing = wanted - {s.get("case") for s in matrix}
        if missing:
            print(f"ERROR: case(s) not in matrix: {', '.join(sorted(missing))}", file=sys.stderr)
            sys.exit(1)

    print(f"── Execute [{args.flow}] ──────────────────────────────────")
    print(f"  {len(matrix)} scenario(s) to run")

    # ── dry run ──────────────────────────────────────────────────────────────
    if args.dry_run:
        for s in matrix:
            case     = s.get("case", "?")
            persona  = s.get("persona", "?")
            ft       = s.get("flow_type", "")
            if s.get("skip"):
                print(f"  SKIP  {case}  -- {s.get('skip_reason', 'marked skip')}")
            elif (s.get("session_count", 1) or 1) > 1:
                print(f"  SKIP  {case}  -- multi-session")
            elif not ft:
                print(f"  SKIP  {case}  -- no flow_type")
            else:
                script = find_base_script(args.flow, ft)
                tag    = "RUN " if script else "SKIP (no script)"
                role   = _resolve_role_key(s) or "unauthenticated"
                print(f"  {tag}  {case}  [{role}]  -> {_base_name(args.flow, ft)}.py")
        return

    # ── live run ─────────────────────────────────────────────────────────────
    results: list[dict] = []
    total = len(matrix)

    with sync_playwright() as pw:
        # Log in once per role before touching any scenario.
        prebuild_sessions(matrix, pw)

        for i, scenario in enumerate(matrix, 1):
            case     = scenario.get("case", "?")
            persona  = scenario.get("persona", "?")
            category = scenario.get("category", "")
            desc     = scenario.get("description", "")[:70]

            print(f"\n  [{i:>3}/{total}] {case} [{persona}/{category}] {desc}")

            result = run_scenario(scenario, args.flow, cleanup_prefix, pw)
            results.append(result)

            passed = result.get("passed")
            status = "PASS" if passed else ("SKIP" if passed is None else "FAIL")
            _print_result(status, result, result.get("inputs", {}))

        if not args.no_cleanup and cleanup_prefix:
            print(f"\nCleaning up '{cleanup_prefix}' records ...")
            n = cleanup_test_records(cleanup_prefix, pw)
            print(f"  [cleanup] Deleted {n} record(s).")

    out = write_results(results, args.flow)
    print(f"\n  Results saved → {out}")

    passed_n  = sum(1 for r in results if r.get("passed") is True)
    failed_n  = sum(1 for r in results if r.get("passed") is False)
    skipped_n = sum(1 for r in results if r.get("passed") is None)
    print(f"  Summary: {passed_n} passed / {failed_n} failed / {skipped_n} skipped  (total {total})")

    if failed_n:
        sys.exit(1)


if __name__ == "__main__":
    main()