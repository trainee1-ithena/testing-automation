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
import sys
import time
import traceback
from datetime import datetime, timezone
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

FRONTEND_URL          = os.getenv("FRONTEND_URL", "http://localhost:3000")
BACKEND_URL           = os.getenv("BACKEND_URL",  "http://localhost:5000")
PLAYWRIGHT_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", "10000"))
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
        rk = _persona_to_role_key(s.get("persona", "any_authorized_role"))
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


def _get_storage_state(persona: str) -> dict | None:
    """Return the cached storage_state dict for a persona, or None."""
    role_key = _persona_to_role_key(persona)
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
        if isinstance(val, str) and val.strip() and len(val) <= _PREFIX_MAX_LEN:
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


def _compute_passed(actual_outcome: str, expected_outcome: str) -> bool | None:
    if expected_outcome == "pass":
        return actual_outcome == "submitted"
    if expected_outcome == "fail":
        return actual_outcome == "correctly rejected"
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


# ── Playwright runner ──────────────────────────────────────────────────────────

def _run_playwright(
    script_path: Path,
    inputs: dict,
    expected_outcome: str,
    storage_state: dict | None,
    pw,
) -> tuple[str, str, str]:
    """
    Open a fresh browser context (pre-authenticated via storage_state dict),
    run the base script's run_test(), detect outcome.

    Each scenario gets its own browser instance so tests are fully isolated.

    Returns: (actual_outcome, actual_message, raw_output)
    """
    mod     = _load_base_module(script_path)
    browser = pw.chromium.launch(headless=False)
    #browser = pw.chromium.launch(headless=not _HEADED)
    ctx     = (
        browser.new_context(storage_state=storage_state)
        if storage_state is not None
        else browser.new_context()
    )
    ctx.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    page = ctx.new_page()
    page.set_default_timeout(PLAYWRIGHT_TIMEOUT_MS)

    actual_outcome = "error"
    actual_message = ""
    raw_output     = ""

    try:
        mod.run_test(page, inputs, expected_outcome, "")
        actual_outcome, actual_message = _detect_outcome(page, expected_outcome)
    except Exception as exc:
        raw_output = traceback.format_exc()
        exc_str    = str(exc)
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

    return actual_outcome, actual_message, raw_output


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
    role_key      = _persona_to_role_key(persona)
    storage_state = _get_storage_state(persona)
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
    try:
        actual_outcome, actual_message, raw_output = _run_playwright(
            script_path, inputs, expected_outcome, storage_state, pw
        )
        passed = _compute_passed(actual_outcome, expected_outcome)
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
        "artifacts":        [],
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
                const r = await fetch(url + "/reports/list", {
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
                    const r = await fetch(url + "/reports/delete", {
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
    out     = RESULTS_DIR / f"raw_results_{flow}.json"
    total   = len(results)
    passed  = sum(1 for r in results if r.get("passed") is True)
    failed  = sum(1 for r in results if r.get("passed") is False)
    skipped = sum(1 for r in results if r.get("passed") is None)
    payload = {
        "flow":    flow,
        "run_at":  datetime.now(timezone.utc).isoformat(),
        "summary": {"total": total, "pass": passed, "fail": failed, "skip": skipped},
        "results": results,
    }
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    global _HEADED
    parser = argparse.ArgumentParser(description="Run scenario matrix against live app")
    parser.add_argument("--flow",       required=True,       help="Flow id, e.g. create_service_report")
    parser.add_argument("--dry-run",    action="store_true", help="Print plan without running")
    parser.add_argument("--case",       default=None,        help="Run only this case, e.g. case025")
    parser.add_argument("--no-cleanup", action="store_true", help="Skip post-run SR cleanup")
    parser.add_argument("--headed",     action="store_true", help="Run browsers visibly (for debugging)")
    args = parser.parse_args()
    _HEADED = args.headed

    matrix         = load_matrix(args.flow)
    cleanup_prefix = os.getenv("TEST_CLEANUP_PREFIX", "[TEST]")

    if args.case:
        matrix = [s for s in matrix if s.get("case") == args.case]
        if not matrix:
            print(f"ERROR: case '{args.case}' not found in matrix.", file=sys.stderr)
            sys.exit(1)

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
                role   = _persona_to_role_key(persona) or "unauthenticated"
                print(f"  {tag}  {case}  [{role}]  -> {_base_name(args.flow, ft)}.py")
        return

    # ── live run ─────────────────────────────────────────────────────────────
    results: list[dict] = []

    with sync_playwright() as pw:
        # Log in once per role before touching any scenario.
        prebuild_sessions(matrix, pw)

        for scenario in matrix:
            case    = scenario.get("case", "?")
            persona = scenario.get("persona", "?")
            cat     = scenario.get("category", "")
            print(f"  {case}  [{persona}]  {cat} ...", end=" ", flush=True)

            result  = run_scenario(scenario, args.flow, cleanup_prefix, pw)
            results.append(result)

            ao     = result.get("actual_outcome", "?")
            p      = result.get("passed")
            msg    = result.get("actual_message", "").strip()
            status = "PASS" if p is True else "FAIL" if p is False else "SKIP"
            print(f"{status}  ({ao})")

            if ao == "timeout" and msg:
                # Extract the "waiting for <selector>" line — most actionable part
                waiting = next(
                    (l.strip() for l in msg.splitlines() if "waiting for" in l.lower()),
                    msg.splitlines()[0] if msg else "",
                )
                print(f"           ! Timeout — {waiting[:200]}")
            elif ao == "error" and msg:
                first = next((l.strip() for l in msg.splitlines() if l.strip()), msg)
                print(f"           ! Error: {first[:200]}")
            elif ao in ("incorrectly rejected", "incorrectly accepted") and msg:
                print(f"           ! UI: {msg[:200]}")
            elif ao == "skip" and msg:
                print(f"           (skip: {msg[:150]})")

        if not args.no_cleanup and cleanup_prefix:
            print(f"\nCleaning up '{cleanup_prefix}' records ...")
            n = cleanup_test_records(cleanup_prefix, pw)
            print(f"  [cleanup] Deleted {n} record(s).")

    out   = write_results(results, args.flow)
    total = len(results)
    p     = sum(1 for r in results if r.get("passed") is True)
    f     = sum(1 for r in results if r.get("passed") is False)
    sk    = sum(1 for r in results if r.get("passed") is None)

    print(f"\nResults: {p} pass / {f} fail / {sk} skip  (total {total})")
    print(f"Written -> {out}")

    if f:
        sys.exit(1)


if __name__ == "__main__":
    main()
