"""
Stage 4 — Execute Scenarios

Reads a generated scenario matrix and runs each scenario against the live app.

Usage:
    python testGen/execute.py --flow agent_create_ticket
    python testGen/execute.py --flow customer_create_ticket
    python testGen/execute.py --flow post_creation_visibility
    python testGen/execute.py --flow agent_create_ticket --dry-run
    python testGen/execute.py --flow agent_create_ticket --id TC003  # run one scenario

Execution branches (in priority order):
  1. test_mode == "multi_session"
         → run_multi_session_scenario()
           Session A creates ticket via API (primary role).
           Session B verifies visibility via Playwright (second_session role).
  2. category == "role_field_visibility" AND inputs == {}
         → run_visibility_test()
           Opens the form, asserts the named field is absent from the DOM.
  3. category == "cascade_dependency"
         → run_cascade_test()
           Fills only the cascade trigger field(s), waits for the network
           cascade response, asserts no error.
  4. Default (test_mode == "ui")
         → run_ui_test()
           Runs the full base_test_{flow}.py script with scenario inputs.

Outputs: testGen/raw_results_{flow}.json
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import requests
from dotenv import load_dotenv

TESTGEN_DIR = Path(__file__).parent
load_dotenv(TESTGEN_DIR / ".env")

FRONTEND_URL       = os.getenv("FRONTEND_URL", "http://localhost:3000")
API_BASE_URL       = os.getenv("API_BASE_URL", "http://localhost:5000/api")
PLAYWRIGHT_BROWSER = os.getenv("PLAYWRIGHT_BROWSER", "chromium")
PLAYWRIGHT_HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() == "true"
PLAYWRIGHT_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", "10000"))

ROLE_CREDENTIALS = {
    "agent": {
        "email":    os.getenv("TEST_AGENT_EMAIL", ""),
        "password": os.getenv("TEST_AGENT_PASSWORD", ""),
        "mode":     "agent",
        "login_path": "/se-login",
    },
    "customer": {
        "email":    os.getenv("TEST_CUSTOMER_EMAIL", ""),
        "password": os.getenv("TEST_CUSTOMER_PASSWORD", ""),
        "mode":     "user",
        "login_path": "/login",
    },
    "second_agent": {
        "email":    os.getenv("TEST_SECOND_AGENT_EMAIL", ""),
        "password": os.getenv("TEST_SECOND_AGENT_PASSWORD", ""),
        "mode":     "agent",
        "login_path": "/se-login",
    },
    "second_customer": {
        "email":    os.getenv("TEST_SECOND_CUSTOMER_EMAIL", ""),
        "password": os.getenv("TEST_SECOND_CUSTOMER_PASSWORD", ""),
        "mode":     "user",
        "login_path": "/login",
    },
}

STORAGE_STATE_DIR = TESTGEN_DIR / "storage_states"

# Success messages per flow — used to extract actual_message on pass.
FLOW_SUCCESS_MESSAGES = {
    "agent_create_ticket":    "has been created successfully",
    "customer_create_ticket": "has been created successfully",
    "post_creation_visibility": "has been created successfully",
}

# URL patterns that the app redirects to after a successful form submission.
# Used in the injected assertion block in run_ui_test — survives re-recording.
FLOW_SUCCESS_URLS: dict[str, str] = {
    "agent_create_ticket":      "**/cases/**",
    "customer_create_ticket":   "**/cases/**",
    "post_creation_visibility": "**/cases/**",
}

# Maps flow name to base script path.
FLOW_BASE_SCRIPTS: dict[str, Path] = {
    "agent_create_ticket":    TESTGEN_DIR / "base_test_agent_create_ticket.py",
    "customer_create_ticket": TESTGEN_DIR / "base_test_customer_create_ticket.py",
    "post_creation_visibility": TESTGEN_DIR / "base_test_agent_create_ticket.py",
}

# Start path per flow (namespace-translated — must match record.py FLOW_CONFIG).
# Used in cascade, visibility, and multi-session tests that build their own Playwright code.
FLOW_START_PATHS: dict[str, str] = {
    "agent_create_ticket":    "/cases",
    "customer_create_ticket": "/dashboard",
    "post_creation_visibility": "/cases",
}

# Namespace-translated base path for individual ticket detail pages.
TICKET_DETAIL_BASE = "/cases"


# ── Authentication helpers ────────────────────────────────────────────────────

def _get_api_token(role: str) -> str | None:
    """
    Obtain a JWT by calling POST /auth/login directly.
    Used by multi_session Session A (ticket creation) and for API-only checks.
    """
    creds = ROLE_CREDENTIALS.get(role, {})
    if not creds.get("email") or not creds.get("password"):
        print(f"  WARN: missing credentials for role '{role}' — check .env", file=sys.stderr)
        return None

    try:
        resp = requests.post(
            f"{API_BASE_URL}/auth/login",
            json={
                "email":         creds["email"],
                "user_password": creds["password"],
                "mode":          creds["mode"],
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("token")
        print(
            f"  WARN: login failed for role '{role}' — "
            f"HTTP {resp.status_code}: {resp.text[:200]}",
            file=sys.stderr,
        )
    except Exception as exc:
        print(f"  WARN: login request failed for role '{role}' — {exc}", file=sys.stderr)
    return None


def _get_or_create_storage_state(role: str) -> Path:
    """
    Return path to a saved browser storage state for the given role.
    Creates the file by logging in via Playwright if it doesn't exist.
    The file is reused across scenarios to avoid repeated logins.
    Role aliases: "non-agent" → "customer" (visibility tests checking fields hidden from customers).
    """
    ROLE_ALIASES = {"non-agent": "customer"}
    role = ROLE_ALIASES.get(role, role)

    STORAGE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path = STORAGE_STATE_DIR / f"session_{role}.json"
    if state_path.exists():
        return state_path

    creds = ROLE_CREDENTIALS.get(role, {})
    if not creds.get("email") or not creds.get("password"):
        raise ValueError(
            f"Cannot create storage state for role '{role}' — "
            f"credentials not set in testGen/.env"
        )

    login_url      = f"{FRONTEND_URL}{creds['login_path']}"
    state_path_str = str(state_path).replace("\\", "/")

    print(f"  Creating session for role '{role}' — logging in at {login_url} ...")
    setup_script = f"""\
import sys
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context()
    page = ctx.new_page()
    try:
        page.goto("{login_url}", timeout=20000)
    except Exception as exc:
        print(f"ERROR: could not reach {login_url} — {{exc}}\\nIs the app running?", file=sys.stderr)
        sys.exit(1)
    try:
        page.wait_for_selector("[name='email']", timeout=15000)
    except Exception:
        print(
            f"ERROR: login form not found at {{page.url}} — page title: {{page.title()}}",
            file=sys.stderr,
        )
        sys.exit(1)
    page.fill("[name='email']", "{creds['email']}")
    page.fill("[name='user_password']", "{creds['password']}")
    page.locator("[name='user_password']").press("Enter")
    try:
        page.wait_for_function(
            "() => !window.location.pathname.includes('login')",
            timeout=15000,
        )
    except Exception:
        print(
            f"ERROR: still on login page after submit — check TEST_{'{role}'.upper()}_EMAIL / PASSWORD in testGen/.env",
            file=sys.stderr,
        )
        sys.exit(1)
    ctx.storage_state(path="{state_path_str}")
    browser.close()
    print("  Storage state saved.")
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(setup_script)
        tmp = f.name

    try:
        result = subprocess.run([sys.executable, tmp], capture_output=True, text=True)
        if result.returncode != 0:
            err = (result.stdout + result.stderr).strip()
            raise ValueError(
                f"Could not create session for role '{role}':\n{err}"
            )
        print(f"  ✓ Storage state created for role: {role}")
    finally:
        Path(tmp).unlink(missing_ok=True)

    return state_path


def _invalidate_storage_state(role: str):
    """Remove cached storage state so it is re-created on next use."""
    p = STORAGE_STATE_DIR / f"session_{role}.json"
    p.unlink(missing_ok=True)


# ── Ticket creation helpers (for multi_session Session A) ─────────────────────

def _pick_entity(context_json: dict) -> dict:
    """
    Pick the first available real IDs from known_entities so Session A can
    create a ticket via the API with valid FK references.
    Returns a partial ticket payload dict.
    """
    entities = context_json.get("test_data", {}).get("known_entities", {})
    customers = entities.get("customers", [])
    departments = entities.get("departments", [])

    payload: dict = {}

    if customers:
        c = customers[0]
        payload["customer"] = c["id"]
        users = c.get("users", [])
        if users:
            payload["user"] = users[0]["id"]
        equipment = c.get("equipment", [])
        if equipment:
            payload["equipment"] = equipment[0]["id"]

    if departments:
        d = departments[0]
        payload["department"] = d["id"]
        service_types = d.get("service_types", [])
        if service_types:
            payload["service_type"] = service_types[0]["id"]

    return payload


_PRIORITY_NAME_TO_ID: dict[str, int] = {
    "low": 1, "normal": 2, "high": 3, "emergency": 4,
}


def _build_name_id_map(context_json: dict) -> dict[str, int]:
    """
    Build a flat {display_name_lower: id} lookup across all known_entities so
    scenario inputs that use human names ("John Doe", "Technical Support") can
    be resolved to integer IDs before posting to the API.
    """
    entities = (context_json or {}).get("test_data", {}).get("known_entities", {})
    m: dict[str, int] = {}
    for c in entities.get("customers", []):
        if c.get("name") and c.get("id"):
            m[c["name"].lower()] = c["id"]
        for u in c.get("users", []):
            if u.get("name") and u.get("id"):
                m[u["name"].lower()] = u["id"]
        for eq in c.get("equipment", []):
            label = eq.get("provision_name") or eq.get("name")
            if label and eq.get("id"):
                m[label.lower()] = eq["id"]
    for d in entities.get("departments", []):
        if d.get("name") and d.get("id"):
            m[d["name"].lower()] = d["id"]
        for st in d.get("service_types", []):
            if st.get("name") and st.get("id"):
                m[st["name"].lower()] = st["id"]
        for s in d.get("staff", []):
            if s.get("name") and s.get("staff_id"):
                m[s["name"].lower()] = s["staff_id"]
    return m


def _resolve_to_id(value: str, name_id_map: dict[str, int]) -> int | str:
    """Return the integer ID for a display name, or the original value if not found."""
    if isinstance(value, int):
        return value
    # Priority names are static — check those first
    prio = _PRIORITY_NAME_TO_ID.get(value.lower())
    if prio is not None:
        return prio
    return name_id_map.get(value.lower(), value)


def _create_ticket_via_api(
    scenario_inputs: dict,
    primary_role: str,
    context_json: dict | None,
) -> int | None:
    """
    Create a ticket via POST /tickets/create.
    Returns ticket_id on success, None on failure.
    """
    token = _get_api_token(primary_role)
    if not token:
        return None

    # Base payload: real IDs from context known_entities
    payload: dict = {}
    if context_json:
        payload.update(_pick_entity(context_json))

    name_id_map = _build_name_id_map(context_json)

    # Scenario inputs use display names and may use different field names than the API.
    # API numeric fields: customer, user, equipment, priority, department, service_type,
    #                     staff_accountable, staff_assigned
    field_aliases = {
        "ticket_subject":       "subject",
        "ticket_description":   "description",
        "priority_id":          "priority",
        "dept_id":              "department",
        "accountable_staff_id": "staff_accountable",
        "assigned_staff_id":    "staff_assigned",
        # status_id is not a required create field — skip it
        "status_id":            None,
    }
    api_numeric = {
        "customer", "user", "equipment", "priority",
        "department", "service_type", "staff_accountable", "staff_assigned",
    }
    for k, v in scenario_inputs.items():
        api_key = field_aliases.get(k, k)
        if api_key is None:
            continue  # explicitly skipped
        if api_key in api_numeric and isinstance(v, str):
            v = _resolve_to_id(v, name_id_map)
        payload[api_key] = v

    if "subject" not in payload:
        payload["subject"] = "Test ticket (auto-created by execute.py)"

    subject = payload["subject"]

    try:
        resp = requests.post(
            f"{API_BASE_URL}/tickets/create",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if resp.status_code == 200:
            body = resp.json()
            # Response is {"status":true,"data":{"ticket_id":...}} — not flat
            data = body.get("data") or {}
            ticket_id = data.get("ticket_id") or data.get("id") or body.get("ticket_id") or body.get("id")
            return ticket_id, subject
        print(
            f"  WARN: ticket creation failed — HTTP {resp.status_code}: {resp.text[:200]}",
            file=sys.stderr,
        )
    except Exception as exc:
        print(f"  WARN: ticket creation request failed — {exc}", file=sys.stderr)
    return None, subject


# ── Playwright runner (shared) ────────────────────────────────────────────────

def _run_playwright(
    test_code: str,
    scenario_id: str,
    *,
    storage_state_path: Path | None = None,
    timeout: int = 120,
) -> dict:
    """
    Write test_code to a temp file and run it with pytest-playwright.
    If storage_state_path is given, pytest uses it to start the browser
    already authenticated.
    Returns a result dict.
    """
    artifacts_dir = TESTGEN_DIR / "test_artifacts" / scenario_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Inject storage state into the test via a conftest fixture if needed.
    # Embed the JSON content inline rather than a file path so the pytest
    # subprocess (which runs from a temp dir) can always find it.
    conftest_content = ""
    if storage_state_path and storage_state_path.exists():
        try:
            storage_data = json.loads(storage_state_path.read_text(encoding="utf-8"))
            conftest_content = f"""\
import pytest

@pytest.fixture
def browser_context_args(browser_context_args):
    return {{
        **browser_context_args,
        "storage_state": {storage_data!r},
    }}
"""
        except Exception:
            # Fallback: use the file path (original behaviour)
            storage_str = str(storage_state_path).replace("\\", "/")
            conftest_content = f"""\
import pytest

@pytest.fixture
def browser_context_args(browser_context_args):
    return {{
        **browser_context_args,
        "storage_state": "{storage_str}",
    }}
"""

    tmp_dir = Path(tempfile.mkdtemp())
    test_path = tmp_dir / f"test_{scenario_id}.py"
    test_path.write_text(test_code, encoding="utf-8")

    if conftest_content:
        (tmp_dir / "conftest.py").write_text(conftest_content, encoding="utf-8")

    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "pytest",
                str(test_path), "-v", "--tb=short", "--no-header",
                "--browser", PLAYWRIGHT_BROWSER,
                "--screenshot", "only-on-failure",
                "--video", "retain-on-failure",
                "--output", str(artifacts_dir),
                *(["--headed"] if not PLAYWRIGHT_HEADLESS else []),
            ],
            capture_output=True, text=True, timeout=timeout,
        )
        passed = result.returncode == 0
        output = (result.stdout + result.stderr)[:3000]
        return {
            "passed": passed,
            "raw_output": output,
            "artifacts": [str(p) for p in artifacts_dir.rglob("*") if p.is_file()],
        }
    except subprocess.TimeoutExpired:
        return {"passed": False, "raw_output": "Test timed out", "artifacts": []}
    except Exception as exc:
        return {"passed": False, "raw_output": str(exc), "artifacts": []}
    finally:
        # Clean up temp dir
        try:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


# ── Branch 1: Standard UI test ────────────────────────────────────────────────

def run_ui_test(scenario: dict, scenario_id: str) -> dict:
    """
    Run the base_test_{flow}.py script with the scenario's inputs.
    The base script opens the form, fills it, submits, and asserts.
    """
    flow = scenario.get("flow", "")
    role = scenario.get("role", "agent")
    inputs = dict(scenario.get("inputs") or {})
    expected_outcome = scenario.get("expected_outcome")
    expected_message = scenario.get("expected_message") or ""

    # Resolve file paths: if the scenario has a bare filename (e.g. "test.pdf"),
    # create a dummy file with that name on the fly so set_input_files never crashes.
    # The extension is what the app validates — content doesn't matter.
    if "files" in inputs:
        f_val = inputs["files"]
        if f_val and not Path(str(f_val)).is_absolute() and not Path(str(f_val)).exists():
            fixtures_dir = TESTGEN_DIR / "test_fixtures"
            fixtures_dir.mkdir(parents=True, exist_ok=True)
            dummy_path = fixtures_dir / Path(str(f_val)).name
            if not dummy_path.exists():
                dummy_path.write_bytes(b"dummy test file for playwright set_input_files")
            inputs["files"] = str(dummy_path)

    base_path = FLOW_BASE_SCRIPTS.get(flow)
    if not base_path or not base_path.exists():
        return {
            "passed": False,
            "actual_outcome": "error",
            "actual_message": f"Base script not found: {base_path} — run record.py first.",
            "raw_output": "",
            "artifacts": [],
        }

    try:
        storage_state = _get_or_create_storage_state(role)
    except ValueError as exc:
        return {
            "passed": False,
            "actual_outcome": "error",
            "actual_message": str(exc),
            "raw_output": "",
            "artifacts": [],
        }

    base_script = base_path.read_text(encoding="utf-8")

    # Strip any assertion block baked into base_script by older --build runs.
    # Assertions live in execute.py now so they survive re-recording.
    # Handle both formats: "# ----\n    if expected_outcome" and bare "if expected_outcome".
    _sep    = "\n    # ---"
    _marker = "\n    if expected_outcome"
    if _sep in base_script:
        base_script = base_script[:base_script.index(_sep)]
    elif _marker in base_script:
        base_script = base_script[:base_script.index(_marker)]

    # ── File rejection short-circuit ──────────────────────────────────────────
    # react-dropzone fires onDropRejected instantly for invalid file types,
    # showing a SweetAlert2 modal that blocks the Submit button.
    # Fix: replace the Submit click with Swal detection so we don't time out.
    _field = scenario.get("field", "")
    if _field == "files" and expected_outcome == "fail":
        _swal_lines = (
            "    # Invalid file type — react-dropzone fires onDropRejected → SweetAlert2\n"
            "    page.wait_for_selector('.swal2-popup', timeout=10000)\n"
            "    _swal_body = page.locator('.swal2-html-container').text_content() or ''\n"
            "    assert _swal_body, 'Expected file-rejection dialog but Swal was empty'\n"
            "    page.locator('.swal2-confirm').click()  # dismiss\n"
        )
        # Match a try/except-wrapped Submit click first (customer base scripts wrap it
        # in try/except so a disabled button doesn't crash the recording).  Replacing
        # only the inner .click() line leaves the try/except shell with wrong indentation
        # → SyntaxError.  Replace the whole try block instead.
        _submit_try_pat = re.compile(
            r"[ \t]*try:[ \t]*\n"
            r"[ \t]*page\.get_by_role\(['\"]button['\"],\s*name=['\"]Submit['\"]\)"
            r"\.click\([^)]*\)[ \t]*\n"
            r"[ \t]*except\b[^\n]*\n"
            r"[ \t]*pass[^\n]*\n?",
            re.MULTILINE,
        )
        _submit_pat = re.compile(
            r"[ \t]*page\.get_by_role\(['\"]button['\"],\s*name=['\"]Submit['\"]\)"
            r"\.click\([^)]*\)[ \t]*\n?"
        )
        if _submit_try_pat.search(base_script):
            base_script = _submit_try_pat.sub(_swal_lines, base_script)
        elif _submit_pat.search(base_script):
            base_script = _submit_pat.sub(_swal_lines, base_script)
        else:
            base_script = base_script.rstrip() + "\n" + _swal_lines + "\n"

    success_url = FLOW_SUCCESS_URLS.get(flow, "**/cases/**")

    # For file-rejection tests the swal detection inside run_test already validates
    # the rejection.  Appending a wait_for_url check would false-positive when the
    # flow's success URL matches the starting URL (e.g. customer modal on /dashboard).
    if _field == "files" and expected_outcome == "fail":
        assertion_block = "    pass  # rejection validated inside run_test via swal detection"
    else:
        assertion_block = "\n".join([
            f"    if expected_outcome == 'pass':",
            f"        page.wait_for_url('{success_url}', timeout={PLAYWRIGHT_TIMEOUT_MS})",
            f"    else:",
            f"        _submitted = False",
            f"        try:",
            f"            page.wait_for_url('{success_url}', timeout=2000)",
            f"            _submitted = True",
            f"        except Exception:",
            f"            pass",
            f"        if _submitted:",
            f'            raise AssertionError("Form accepted invalid input and navigated to success URL")',
            f"        if expected_message:",
            f"            try:",
            f'                expect(page.get_by_text(re.compile(re.escape(expected_message), re.IGNORECASE))).to_be_visible(timeout=4000)',
            f"            except Exception:",
            f"                pass  # form correctly rejected, message wording differs — still a pass",
        ])

    test_code = (
        base_script.rstrip()
        + "\n\n\n"
        + "def test_run(page):\n"
        + f"    inputs = {inputs!r}\n"
        + f"    expected_outcome = {expected_outcome!r}\n"
        + f"    expected_message = {expected_message!r}\n"
        + "    run_test(page, inputs, expected_outcome, expected_message)\n"
        + assertion_block + "\n"
    )

    result = _run_playwright(test_code, scenario_id, storage_state_path=storage_state)
    passed = result["passed"]

    success_sig = FLOW_SUCCESS_MESSAGES.get(flow, "")
    if passed:
        if expected_outcome == "pass":
            actual_outcome = "submitted"           # form accepted valid input ✓
            actual_message = success_sig
        else:
            actual_outcome = "correctly rejected"  # form rejected invalid input ✓
            actual_message = expected_message
    else:
        # Pytest failed — determine why
        raw = result.get("raw_output", "")
        if success_sig and success_sig in raw:
            # Form submitted when it should have rejected
            actual_outcome = "incorrectly accepted"
            actual_message = "Form submitted successfully when it should have shown an error"
        elif "accepted invalid input" in raw:
            actual_outcome = "incorrectly accepted"
            actual_message = _extract_failure_message(raw)
        elif "TimeoutError" in raw or "timed out" in raw.lower():
            actual_outcome = "timeout"
            actual_message = _extract_failure_message(raw) or "Test timed out"
        else:
            actual_outcome = "error"
            actual_message = _extract_failure_message(raw)

    return {
        "passed": passed,
        "actual_outcome": actual_outcome,
        "actual_message": actual_message,
        **result,
    }


# ── Branch 2: Cascade dependency test ─────────────────────────────────────────
#
# Design: fully data-driven — no hardcoded field names or API endpoints.
# Everything is derived at runtime from three sources:
#   1. scenario_matrix_{flow}_inventory.txt  → A/B/E item structure
#   2. base_test_{flow}.py                  → exact Playwright selectors (recorded script)
#   3. context_{flow}.json cascade_entities → expected cascade values (full dataset, never in LLM prompt)
#
# The only manual config is CASCADE_API_MAP below, which covers cascades whose
# expected values can't be determined from known_entities (e.g. routing-tag lookups).
# Add one entry here per such cascade type — you never touch this for entity-resolvable ones.

CASCADE_API_MAP: dict[tuple[str, str], dict] = {
    # (trigger_field, target_field): API call description
    ("equipment", "service_type"): {
        "endpoint":            "/routing-tags/ticket-options",
        "id_collection":       "customers",       # top-level entities key
        "id_child":            "equipment",       # child array within each parent
        "id_match_key":        "provision_name",  # match trigger_value against this key
        "id_key":              "id",              # extract this as the API param value
        "param_name":          "provision_id",    # query-string param name
        "response_path":       ["data", "service_types"],  # walk JSON to get array
        "response_display_key": "service_type",  # extract label from each array item
    },
    # Add new entries here as you encounter API-based cascades in other forms
}

# ── Inventory parsing (derive everything from *_inventory.txt) ────────────────

def _load_inventory_items(flow: str) -> dict:
    """
    Parse scenario_matrix_{flow}_inventory.txt.
    Returns {
      "a_items": [{"name", "ui_label", "type", "auto_filled"}, ...],
      "b_items": [{"id", "trigger", "target", "effect"}, ...],
      "e_items": { field_name: {"source": path, "display_key": key} }
    }
    """
    inv_path = TESTGEN_DIR / f"scenario_matrix_{flow}_inventory.txt"
    if not inv_path.exists():
        return {"a_items": [], "b_items": [], "e_items": {}}

    text = inv_path.read_text(encoding="utf-8")
    a_items: list[dict] = []
    b_items: list[dict] = []
    e_items: dict[str, dict] = {}

    for line in text.splitlines():
        # A-items: field definitions
        m = re.match(
            r"A\d+\.\s+(\S+)\s*\|.*ui_label:\s*([^|]+?)\s*\|.*type:\s*([^|]+?)\s*\|"
            r".*auto_filled:\s*(\S+)",
            line, re.IGNORECASE,
        )
        if m:
            a_items.append({
                "name":       m.group(1).strip(),
                "ui_label":   m.group(2).strip(),
                "type":       m.group(3).lower().strip().rstrip(","),
                "auto_filled": m.group(4).lower().strip().rstrip(","),
            })

        # B-items: cascade relationships
        m = re.match(
            r"(B\d+)\.\s+(\S+)\s*->\s*(\S+)\s*\|\s*EFFECT:\s*([^|]+)",
            line, re.IGNORECASE,
        )
        if m:
            b_items.append({
                "id":      m.group(1).strip(),
                "trigger": m.group(2).strip(),
                "target":  m.group(3).strip().rstrip("|").strip(),
                "effect":  m.group(4).lower().strip().rstrip("|, "),
            })

        # E-items: entity source paths
        m = re.match(
            r"E\d+\.\s+FIELD:\s*(\S+)\s*\|.*SOURCE:\s*([^|]+?)\s*\|.*DISPLAY LABEL:\s*(\S+)",
            line, re.IGNORECASE,
        )
        if m:
            e_items[m.group(1).strip()] = {
                "source":      m.group(2).strip(),
                "display_key": m.group(3).strip(),
            }

    return {"a_items": a_items, "b_items": b_items, "e_items": e_items}


# ── Selector parsing (derive from recorded base_test script) ──────────────────

def _parse_base_script_selectors(flow: str) -> dict[str, dict]:
    """
    Parse base_test_{flow}.py for `if inputs.get("field"):` blocks.
    Returns { field_name: { "selector_type", "open_line", "action_line" } }

    selector_type: "react_select" | "textbox" | "file" | "unknown"
    open_line:     the page.* call that opens/focuses the widget
    action_line:   the page.* call that sets the value (click option / fill / set_input_files)
    """
    base = TESTGEN_DIR / f"base_test_{flow}.py"
    if not base.exists():
        return {}

    src = base.read_text(encoding="utf-8")
    selectors: dict[str, dict] = {}

    block_re = re.compile(
        r'if inputs\.get\("(\w+)"\):\s*\n((?:[ \t]+(?:page\.|#)[^\n]*\n)+)',
        re.MULTILINE,
    )

    for m in block_re.finditer(src):
        field = m.group(1)
        lines = [l.strip() for l in m.group(2).splitlines() if l.strip().startswith("page.")]
        if not lines:
            continue

        if any("set_input_files" in l for l in lines):
            selectors[field] = {"selector_type": "file", "open_line": lines[0], "action_line": None}
        elif any('get_by_role("combobox"' in l for l in lines):
            open_line = next(l for l in lines if 'get_by_role("combobox"' in l)
            idx = lines.index(open_line)
            action_line = lines[idx + 1] if idx + 1 < len(lines) else None
            selectors[field] = {
                "selector_type": "react_select",
                "open_line":   open_line,
                "action_line": action_line,
            }
        elif any("fill(" in l for l in lines):
            selectors[field] = {
                "selector_type": "textbox",
                "open_line":   lines[0],
                "action_line": next(l for l in lines if "fill(" in l),
            }
        else:
            selectors[field] = {
                "selector_type": "unknown",
                "open_line":   lines[0],
                "action_line": lines[-1] if len(lines) > 1 else None,
            }

    return selectors


# ── Cascade strategy classification ──────────────────────────────────────────

def _classify_cascade_strategy(
    trigger: str, target: str, effect: str,
    e_items: dict[str, dict], entities: dict,
) -> str:
    """
    Determine how to validate this cascade entirely from the E-item source paths.

    Strategies:
      parent_to_child   — trigger is a parent; target options are its children
      child_to_parent   — trigger is a child item; valid targets are parents that contain it
      autofill_manager  — auto-fill: parent has a manager/owner reference → find that child
      autofill_first    — auto-fill: no manager ref; return any non-empty child value
      api               — entities can't resolve this; see CASCADE_API_MAP
      assert_nonempty   — effect=calculate or target has no E-item; just verify non-empty
    """
    if (trigger, target) in CASCADE_API_MAP:
        return "api"

    t_info = e_items.get(trigger)
    r_info = e_items.get(target)

    if not t_info or not r_info:
        return "assert_nonempty"

    def _parts(path: str) -> list[str]:
        return [p for p in path.replace("known_entities.", "").replace("[]", "").split(".") if p]

    t = _parts(t_info["source"])  # e.g. ["departments", "service_types"]
    r = _parts(r_info["source"])  # e.g. ["departments"]

    # Trigger is a child element of target's collection (child_to_parent)
    if t[:len(r)] == r and len(t) > len(r):
        return "child_to_parent"

    # Target is a child element of trigger's collection (parent_to_child / autofill)
    if r[:len(t)] == t and len(r) > len(t):
        if effect in ("auto-fill", "autofill", "auto_fill"):
            parent_coll = entities.get(t[0], [])
            if parent_coll and isinstance(parent_coll, list) and isinstance(parent_coll[0], dict):
                if any("manager" in k.lower() or "owner" in k.lower() for k in parent_coll[0]):
                    return "autofill_manager"
            return "autofill_first"
        return "parent_to_child"

    return "assert_nonempty"


# ── Generic cascade expected-value resolver ───────────────────────────────────

def _resolve_cascade_expected(
    trigger: str, trigger_value: str, target: str, effect: str,
    e_items: dict[str, dict], entities: dict, role: str,
) -> dict:
    """
    Compute what the target field SHOULD show after the cascade fires.
    Returns { should_contain, should_not_contain, should_be_filled, filled_value, error }
    """
    strategy = _classify_cascade_strategy(trigger, target, effect, e_items, entities)

    result: dict = {
        "should_contain":     [],
        "should_not_contain": [],
        "should_be_filled":   False,
        "filled_value":       None,
        "error":              None,
    }

    def _parts(path: str) -> list[str]:
        return [p for p in path.replace("known_entities.", "").replace("[]", "").split(".") if p]

    t_info = e_items.get(trigger, {})
    r_info = e_items.get(target, {})

    # ── assert_nonempty ───────────────────────────────────────────────────────
    if strategy == "assert_nonempty":
        result["should_be_filled"] = True
        return result

    # ── API ───────────────────────────────────────────────────────────────────
    if strategy == "api":
        cfg = CASCADE_API_MAP[(trigger, target)]
        token = _get_api_token(role)
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        # Resolve entity ID for the trigger value
        trigger_id = None
        for parent in entities.get(cfg["id_collection"], []):
            for child in parent.get(cfg["id_child"], []):
                if child.get(cfg["id_match_key"]) == trigger_value:
                    trigger_id = child.get(cfg["id_key"])
                    break
            if trigger_id is not None:
                break

        if trigger_id is None:
            result["error"] = f"ID for {trigger}='{trigger_value}' not found in entities"
            return result

        try:
            resp = requests.get(
                f"{API_BASE_URL}{cfg['endpoint']}",
                params={cfg["param_name"]: trigger_id},
                headers=headers, timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                for key in cfg["response_path"]:
                    data = data.get(key, []) if isinstance(data, dict) else []
                if isinstance(data, list):
                    result["should_contain"] = [
                        item.get(cfg["response_display_key"], "")
                        for item in data if isinstance(item, dict)
                    ]
                    result["should_contain"] = [s for s in result["should_contain"] if s]
            else:
                result["error"] = f"API {cfg['endpoint']} returned {resp.status_code}"
        except Exception as exc:
            result["error"] = str(exc)
        return result

    # ── child_to_parent ───────────────────────────────────────────────────────
    if strategy == "child_to_parent":
        t = _parts(t_info["source"])  # ["departments", "service_types"]
        r = _parts(r_info["source"])  # ["departments"]
        parent_coll   = r[0]                          # "departments"
        child_arr_key = t[-1]                         # "service_types"
        t_disp = t_info.get("display_key", "name")    # child display key
        r_disp = r_info.get("display_key", "name")    # parent display key

        all_parents = entities.get(parent_coll, [])
        result["should_contain"] = [
            p[r_disp] for p in all_parents
            if any(c.get(t_disp) == trigger_value for c in p.get(child_arr_key, []))
        ]
        result["should_not_contain"] = [
            p[r_disp] for p in all_parents
            if not any(c.get(t_disp) == trigger_value for c in p.get(child_arr_key, []))
        ]
        return result

    # ── parent_to_child ───────────────────────────────────────────────────────
    if strategy == "parent_to_child":
        t = _parts(t_info["source"])  # ["customers"]
        r = _parts(r_info["source"])  # ["customers", "users"]
        parent_coll   = t[0]
        child_arr_key = r[-1]
        t_disp = t_info.get("display_key", "name")
        r_disp = r_info.get("display_key", "name")

        all_parents = entities.get(parent_coll, [])
        match = next((p for p in all_parents if p.get(t_disp) == trigger_value), None)
        if match:
            result["should_contain"] = [
                c.get(r_disp) for c in match.get(child_arr_key, []) if c.get(r_disp)
            ]
            valid_set = set(result["should_contain"])
            result["should_not_contain"] = [
                c.get(r_disp)
                for p in all_parents if p.get(t_disp) != trigger_value
                for c in p.get(child_arr_key, [])
                # exclude items that also belong to the selected parent (shared members)
                if c.get(r_disp) and c.get(r_disp) not in valid_set
            ]
        else:
            result["error"] = f"'{trigger_value}' not found in {parent_coll}"
        return result

    # ── autofill_manager ──────────────────────────────────────────────────────
    if strategy == "autofill_manager":
        result["should_be_filled"] = True
        t = _parts(t_info["source"])  # ["departments"]
        r = _parts(r_info["source"])  # ["departments", "staff"]
        parent_coll   = t[0]
        child_arr_key = r[-1]
        t_disp = t_info.get("display_key", "name")
        r_disp = r_info.get("display_key", "name")

        all_parents = entities.get(parent_coll, [])
        match = next((p for p in all_parents if p.get(t_disp) == trigger_value), None)
        if match:
            manager_key = next(
                (k for k in match if "manager" in k.lower() or "owner" in k.lower()), None
            )
            if manager_key:
                manager_id = match[manager_key]
                children = match.get(child_arr_key, [])
                for id_key in ("staff_id", "id", "user_id", "agent_id"):
                    mgr = next((c for c in children if c.get(id_key) == manager_id), None)
                    if mgr:
                        result["filled_value"] = mgr.get(r_disp)
                        break
        return result

    # ── autofill_first ────────────────────────────────────────────────────────
    if strategy == "autofill_first":
        result["should_be_filled"] = True
        t = _parts(t_info["source"])
        r = _parts(r_info["source"])
        parent_coll   = t[0]
        child_arr_key = r[-1]
        t_disp = t_info.get("display_key", "name")
        r_disp = r_info.get("display_key", "name")

        all_parents = entities.get(parent_coll, [])
        match = next((p for p in all_parents if p.get(t_disp) == trigger_value), None)
        if match:
            children = match.get(child_arr_key, [])
            if children:
                result["filled_value"] = children[0].get(r_disp)
        return result

    result["error"] = f"Unknown strategy: {strategy}"
    return result


# ── Playwright code builders (derived from parsed base_test selectors) ─────────

def _selector_open_code(field: str, selectors: dict[str, dict], a_items: list, timeout: int) -> str:
    """
    Return the Playwright line(s) that click-open the widget for `field`
    without selecting any option (used for filter assertion — opens target dropdown).
    Lines joined by \\n.
    """
    sel = selectors.get(field, {})
    a   = next((a for a in a_items if a["name"] == field), {})
    ui_label   = a.get("ui_label", field.replace("_", " ").title())
    auto_filled = a.get("auto_filled", "no") == "yes"

    if sel.get("selector_type") == "react_select":
        open_line = sel.get("open_line", "")
        # iSERV's FormField.js generates placeholder = "Select {label}" for every
        # dropdown with no explicit placeholder prop.  MUI Autocomplete reflects the
        # selected value in the input's accessible name, so a recorded open_line that
        # does NOT start with `name="Select ` captured a stale selected value rather
        # than the stable placeholder.  Always use the generated placeholder so the
        # selector works on a fresh/reset form (as in cascade tests).
        if 'name="Select ' in open_line:
            # Recorded during empty state — already the stable placeholder.
            # Covers Autocomplete/userDropdown which emit a real <label> element.
            open_code = open_line.replace(".click()", f".click(timeout={timeout})")
        else:
            # Stale value — recorded while a value was already selected.
            # MUI Select (type="select") has no ARIA label on its combobox div:
            # FormField uses <Typography> not <label>, so no aria-labelledby.
            # MUI v5+ renders <input aria-hidden name="{field}"> (no type="hidden").
            # Navigate up one level to MuiInputBase-root, then get sibling combobox.
            open_code = (
                f'page.locator(\'input[name="{field}"]\').locator(\'xpath=..\')'
                f'.get_by_role(\'combobox\').click(timeout={timeout})'
            )
        # MUI Select options: <li role="option">; React-Select: <div role="option">.
        # [role='option'] matches both — more reliable than [class*='option'].
        return f'{open_code}\npage.wait_for_selector("[role=\'option\']", timeout={timeout})'

    # Fallback: label proximity (p and label)
    return (
        f'page.locator("'
        f'label:has-text(\'{ui_label}\') ~ div, '
        f'label:has-text(\'{ui_label}\') + div, '
        f'p:has-text(\'{ui_label}\') ~ div, '
        f'p:has-text(\'{ui_label}\') + div'
        f'").last.locator("[class*=\'control\']").click(timeout={timeout})\n'
        f'page.wait_for_selector("[class*=\'option\']", timeout={timeout})'
    )


def _selector_set_value_code(
    field: str, value: str, selectors: dict[str, dict], a_items: list, timeout: int,
) -> str:
    """
    Return Playwright line(s) that open `field`'s widget and select/fill `value`.
    Lines joined by \\n.
    """
    sel = selectors.get(field, {})
    a   = next((a for a in a_items if a["name"] == field), {})
    ui_label   = a.get("ui_label", field.replace("_", " ").title())
    auto_filled = a.get("auto_filled", "no") == "yes"

    if sel.get("selector_type") == "react_select":
        # Open the dropdown before selecting a value.
        # The field is always in its empty/placeholder state at this point (cascade
        # tests start with a fresh form).  Use the stable placeholder ARIA name
        # "Select {ui_label}" — derived from FormField.js's default placeholder logic
        # — unless the recorded open_line already uses that pattern.
        open_line = sel.get("open_line", "")
        is_autocomplete = 'name="Select ' in open_line
        if is_autocomplete:
            # MUI Autocomplete / userDropdown — recorded during empty state, so the
            # combobox has a stable ARIA label ("Select Organization" etc.).
            open_code = open_line.replace(".click()", f".click(timeout={timeout})")
        else:
            # Stale value — recorded while a value was already selected.
            # MUI Select (type="select") has no ARIA label on its combobox div;
            # FormField uses <Typography> not <label>, so aria-labelledby is absent.
            # MUI v5+ renders <input aria-hidden name="{field}"> (no type="hidden").
            # Navigate up one level to MuiInputBase-root, then get sibling combobox.
            #
            # For auto-filled fields (e.g. department, service_type): options load
            # async, and the form auto-fills after the API returns.  The MUI Select
            # starts in placeholder state ("Select {label}") until auto-fill fires.
            # Pre-wait: confirm the combobox has left the placeholder state so we
            # don't open an empty menu that closes the moment real options arrive.
            # Use 30 s — same budget as the data-value option wait — because this
            # depends on an API round-trip which can be slow on a cold server.
            pre_open = ""
            if auto_filled:
                pre_open = (
                    f"page.locator('input[name=\"{field}\"]').locator('xpath=..')"
                    f".get_by_role('combobox')"
                    f".filter(has_not_text='Select')"
                    f".wait_for(timeout=30000)\n"
                )
            open_code = (
                pre_open
                + f'page.locator(\'input[name="{field}"]\').locator(\'xpath=..\')'
                + f'.get_by_role(\'combobox\').click(timeout={timeout})'
            )

        # Select option — reuse recorded action_line pattern, sub in value.
        # Handle both raw-recording format (name="literal") and --build format
        # (name=str(inputs["field"])), since cascade tests have no `inputs` variable.
        if sel.get("action_line"):
            action = sel["action_line"]
            # name="literal"  →  name="{value}"
            action = re.sub(r'name="[^"]*"', f'name="{value}"', action)
            # name=str(inputs["field"])  →  name="{value}"
            action = re.sub(r'name=str\(inputs\["[^"]*"\]\)', f'name="{value}"', action)
            # get_by_text("literal"  →  get_by_text("{value}"
            action = re.sub(r'get_by_text\("[^"]*"', f'get_by_text("{value}"', action)
            # get_by_text(str(inputs["field"])  →  get_by_text("{value}"
            action = re.sub(r'get_by_text\(str\(inputs\["[^"]*"\]\)', f'get_by_text("{value}"', action)
            action_code = action.replace(".click()", f".click(timeout={timeout})")
        else:
            action_code = f'page.get_by_role("option", name="{value}", exact=True).click(timeout={timeout})'

        # Wait for real options before clicking.
        # MUI Select: options are <li role="option" data-value="...">; the placeholder
        # MenuItem renders as data-value="" — filter it out with :not([data-value=""]).
        # MUI Autocomplete: options are <li role="option" data-option-index="..."> with
        # NO data-value attribute — [data-value] selector never matches, causing a 30 s
        # timeout.  For Autocomplete just wait for any [role='option'] to appear.
        if is_autocomplete:
            wait_code = f'page.wait_for_selector("[role=\'option\']", timeout=30000)'
        else:
            wait_code = f'page.wait_for_selector("[role=\'option\'][data-value]:not([data-value=\'\'])", timeout=30000)'
        return f"{open_code}\n{wait_code}\n{action_code}"

    if sel.get("selector_type") == "textbox":
        action = sel.get("action_line") or sel["open_line"]
        # Replace the entire fill(...) argument regardless of its format.
        # Slice from .fill( to end of string — the fill call is always the last
        # thing on the line in both raw-recording and --build formats.
        if ".fill(" in action:
            prefix = action[:action.index(".fill(")]
            return f'{prefix}.fill("{value}")'
        return action

    # Fallback: label proximity
    return (
        f'page.locator("label:has-text(\'{ui_label}\') ~ div, '
        f'label:has-text(\'{ui_label}\') + div").last'
        f'.locator("[class*=\'control\']").click(timeout={timeout})\n'
        f'page.get_by_role("option", name="{value}", exact=True).click(timeout={timeout})'
    )


def _selector_single_value_locator(field: str, a_items: list) -> str:
    """
    Return the Playwright locator string (no .click()) for checking the current
    value displayed in a React-Select field (single-value div).
    """
    a = next((a for a in a_items if a["name"] == field), {})
    ui_label = a.get("ui_label", field.replace("_", " ").title())
    return (
        f'page.locator("label:has-text(\'{ui_label}\') ~ div, '
        f'label:has-text(\'{ui_label}\') + div").last'
        f'.locator("[class*=\'single-value\']")'
    )


def _build_prereq_snippet(
    trigger_field: str,
    b_items: list[dict],
    selectors: dict[str, dict],
    a_items: list[dict],
    inputs: dict,
    e_items: dict[str, dict],
    entities: dict,
    timeout: int,
) -> str:
    """
    If trigger_field is itself the TARGET of a filter B-item, that upstream field
    must be set first so the trigger's dropdown actually has options to click.

    Example: equipment is the target of (customer → equipment | filter).
    Without a customer being selected, the equipment dropdown returns no options.
    This function detects that dependency from B-items, resolves the first valid
    entity value for the upstream field via E-items, and returns Playwright code
    to set it before the main trigger step.

    Returns an empty string when no prerequisite is needed.
    """
    def _first_entity_value(field: str) -> str:
        e = e_items.get(field, {})
        if not e:
            return ""
        parts = [p for p in e["source"].replace("known_entities.", "").replace("[]", "").split(".") if p]
        coll = entities.get(parts[0], [])
        if not coll or not isinstance(coll[0], dict):
            return ""
        return str(coll[0].get(e.get("display_key", "name"), ""))

    # Auto-filled trigger fields load their options unconditionally on form mount
    # (e.g. department fetches all departments on open, regardless of service_type).
    # The filter B-item only narrows options *after* a value is chosen — it doesn't
    # gate the dropdown from loading.  No prereq needed for these fields.
    trigger_a = next((a for a in a_items if a["name"] == trigger_field), {})
    if trigger_a.get("auto_filled", "no") == "yes":
        return ""

    lines: list[str] = []
    for b in b_items:
        if b.get("target") != trigger_field or b.get("effect") != "filter":
            continue
        source = b.get("trigger", "")
        if not source or source in inputs:
            continue  # already provided in scenario inputs — skip
        val = _first_entity_value(source)
        if not val:
            continue
        code = _selector_set_value_code(source, val, selectors, a_items, timeout)
        if code:
            lines.append(code)

    return "\n".join(lines)


# ── run_cascade_test ──────────────────────────────────────────────────────────

def run_cascade_test(scenario: dict, scenario_id: str, context_json: dict | None = None) -> dict:
    """
    Data-driven cascade dependency test.

    All field selectors come from parsing base_test_{flow}.py (the recorded script).
    All expected values come from known_entities (via E-item path traversal) or
    CASCADE_API_MAP for the handful of cascades entities can't resolve.
    No field names, selectors, or API paths are hardcoded here.
    """
    flow             = scenario.get("flow", "")
    role             = scenario.get("role", "agent")
    inputs           = dict(scenario.get("inputs") or {})
    cascade_trigger  = scenario.get("cascade_trigger")
    target_field     = scenario.get("field") or ""
    b_item_id        = scenario.get("business_rule", "")
    description      = scenario.get("description", "")

    # Normalise cascade_trigger to list
    if isinstance(cascade_trigger, list):
        trigger_fields = [f.strip() for f in cascade_trigger]
    elif cascade_trigger:
        trigger_fields = [f.strip() for f in str(cascade_trigger).replace("+", ",").split(",")]
    else:
        trigger_fields = []

    primary_trigger = trigger_fields[0] if trigger_fields else ""
    trigger_value   = str(inputs.get(primary_trigger, ""))

    try:
        storage_state = _get_or_create_storage_state(role)
    except ValueError as exc:
        return {"passed": False, "actual_outcome": "error", "actual_message": str(exc),
                "raw_output": "", "artifacts": []}

    # ── 1. Load inventory items and field selectors ───────────────────────────
    inv      = _load_inventory_items(flow)
    a_items  = inv["a_items"]
    e_items  = inv["e_items"]
    b_items  = inv["b_items"]
    selectors = _parse_base_script_selectors(flow)

    # Find the B-item for this scenario
    b_item = next((b for b in b_items if b["id"] == b_item_id), None)
    effect = (b_item or {}).get("effect", "filter")

    # If the scenario omits `field` (LLM left it null), derive from the B-item
    if not target_field and b_item:
        target_field = b_item.get("target", "")

    # cascade_entities = full dataset (all orgs, all depts) — accurate for assertions.
    # Falls back to known_entities if cascade_entities not present (old context files).
    td = (context_json or {}).get("test_data", {})
    entities = td.get("cascade_entities") or td.get("known_entities", {})

    # ── 2. Resolve expected values ────────────────────────────────────────────
    expected = _resolve_cascade_expected(
        primary_trigger, trigger_value, target_field, effect,
        e_items, entities, role,
    )
    if expected.get("error") and not expected["should_contain"] and not expected["should_be_filled"]:
        return {"passed": False, "actual_outcome": "error",
                "actual_message": f"Could not resolve expected values: {expected['error']}",
                "raw_output": "", "artifacts": []}

    should_contain     = expected["should_contain"]
    should_not_contain = expected["should_not_contain"]
    should_be_filled   = expected["should_be_filled"]
    filled_value       = expected.get("filled_value")

    # ── 3. Classify test sub-type ─────────────────────────────────────────────
    desc_lower    = description.lower()
    is_reset_test  = "reset" in desc_lower or "clear" in desc_lower or "wrong-order" in desc_lower
    is_fill_test   = should_be_filled and not is_reset_test
    is_filter_test = bool(should_contain) and not is_reset_test

    # ── 4. Build Playwright snippets from parsed selectors ────────────────────
    _I = "\n    "  # continuation indent inside test function body

    # Auto-detect upstream prerequisites: if the trigger field is itself filtered
    # by another field (e.g. equipment requires customer), set that field first so
    # the trigger's dropdown is populated.
    prereq_raw = _build_prereq_snippet(
        primary_trigger, b_items, selectors, a_items,
        inputs, e_items, entities, PLAYWRIGHT_TIMEOUT_MS,
    )
    prereq_snippet = prereq_raw.replace("\n", _I) if prereq_raw else ""

    trigger_snippet = _selector_set_value_code(
        primary_trigger, trigger_value, selectors, a_items, PLAYWRIGHT_TIMEOUT_MS
    ).replace("\n", _I)

    # Full setup: prerequisites (if any) then the trigger itself
    setup_snippet = (prereq_snippet + _I + trigger_snippet) if prereq_snippet else trigger_snippet

    start_url = f"{FRONTEND_URL}{FLOW_START_PATHS.get(flow, '/cases')}"

    if is_fill_test:
        _tgt_sel  = selectors.get(target_field, {})
        _open_line = _tgt_sel.get("open_line", "")
        _a_tgt     = next((a for a in a_items if a["name"] == target_field), {})
        _ui_label  = _a_tgt.get("ui_label", target_field.replace("_", " ").title())

        if not _tgt_sel:
            # No recorded selector for this field (e.g. DatePicker, file).
            # Can't read its DOM value generically — just assert no error toast,
            # which confirms the cascade fired without crashing the form.
            assertion_code = (
                f'page.wait_for_timeout(1000)\n'
                f'error_toasts = page.locator("[class*=\'swal2-error\'], [class*=\'error-toast\']")\n'
                f'assert error_toasts.count() == 0, '
                f'"Error after cascade suggests {target_field} was not calculated"'
            )
        elif 'name="Select ' in _open_line:
            # MUI Autocomplete / userDropdown — selected value lives in the <input>
            # element, not a React-Select single-value div.  Read with input_value().
            _aria_m = re.search(r'name="([^"]+)"', _open_line)
            _aria   = _aria_m.group(1) if _aria_m else f"Select {_ui_label}"
            _loc    = f'page.get_by_role("combobox", name="{_aria}")'
            if filled_value:
                assertion_code = (
                    f'filled = {_loc}.input_value(timeout={PLAYWRIGHT_TIMEOUT_MS})\n'
                    f'assert filled and filled.strip(), "Expected {target_field} to be auto-filled, got empty"\n'
                    f'assert {filled_value!r} in filled, '
                    f'"Expected {target_field}={filled_value!r}, got: " + str(filled)'
                )
            else:
                assertion_code = (
                    f'filled = {_loc}.input_value(timeout={PLAYWRIGHT_TIMEOUT_MS})\n'
                    f'assert filled and filled.strip(), "Expected {target_field} to be auto-filled, got empty"'
                )
        else:
            # MUI Select (type="select") — selected value is the text_content of the
            # combobox div, reached via the hidden input[name] anchor.
            _loc = (
                f'page.locator(\'input[name="{target_field}"]\').locator(\'xpath=..\')'
                f'.get_by_role(\'combobox\')'
            )
            if filled_value:
                assertion_code = (
                    f'filled = {_loc}.text_content(timeout={PLAYWRIGHT_TIMEOUT_MS})\n'
                    f'assert filled and filled.strip(), "Expected {target_field} to be auto-filled, got empty"\n'
                    f'assert {filled_value!r} in filled, '
                    f'"Expected {target_field}={filled_value!r}, got: " + str(filled)'
                )
            else:
                assertion_code = (
                    f'filled = {_loc}.text_content(timeout={PLAYWRIGHT_TIMEOUT_MS})\n'
                    f'assert filled and filled.strip(), "Expected {target_field} to be auto-filled, got empty"'
                )

    elif is_reset_test:
        sv_loc = _selector_single_value_locator(target_field, a_items)
        assertion_code = (
            f'sv = {sv_loc}\n'
            f'assert sv.count() == 0, '
            f'"Expected {target_field} to be cleared after trigger changed, but still has a value"'
        )

    elif is_filter_test:
        open_target = _selector_open_code(
            target_field, selectors, a_items, PLAYWRIGHT_TIMEOUT_MS
        )
        must_have_py    = repr(should_contain[:10])
        must_not_have_py = repr(should_not_contain[:5])
        assertion_code = (
            f"{open_target}\n"
            f'page.wait_for_timeout(500)\n'
            # [role='option'] matches both MUI Select <li role="option"> and
            # React-Select <div role="option"> — more reliable than [class*='option'].
            f'option_els = page.locator("[role=\'option\']").all()\n'
            f'option_texts = [t.strip() for t in (el.text_content() or "" for el in option_els)]\n'
            f'must_have = {must_have_py}\n'
            f'must_not_have = {must_not_have_py}\n'
            f'missing = [v for v in must_have if not any(v in t for t in option_texts)]\n'
            f'assert not missing, f"Options missing from {target_field} dropdown: {{missing!r}}. Saw: {{option_texts}}"\n'
            f'unwanted = [v for v in must_not_have if any(v in t for t in option_texts)]\n'
            f'assert not unwanted, f"Options that should NOT appear in {target_field}: {{unwanted!r}}"\n'
            f'page.keyboard.press("Escape")'
        )

    else:
        assertion_code = (
            f'error_toasts = page.locator("[class*=\'swal2-error\'], [class*=\'error-toast\']")\n'
            f'assert error_toasts.count() == 0, "Unexpected error toast after cascade"'
        )

    assertion_indented = assertion_code.replace("\n", _I)

    cascade_code = f"""\
import re
from playwright.sync_api import Page, expect


def test_cascade_{scenario_id}(page: Page):
    page.goto("{start_url}")
    page.wait_for_load_state("domcontentloaded")
    page.get_by_role("button", name="New Case").click(timeout=30000)
    page.wait_for_timeout(1000)

    # Setup: prerequisites + trigger ({primary_trigger} = {trigger_value!r})
    {setup_snippet}

    # Wait for cascade to settle
    try:
        page.wait_for_load_state("networkidle", timeout=4000)
    except Exception:
        page.wait_for_timeout(1500)

    # Assert ({b_item_id} {effect}): {description}
    {assertion_indented}
"""

    result = _run_playwright(cascade_code, scenario_id, storage_state_path=storage_state)
    passed = result["passed"]

    if passed:
        if is_fill_test:
            actual_msg = f"{target_field} auto-filled" + (f" → '{filled_value}'" if filled_value else "")
        elif is_reset_test:
            actual_msg = f"{target_field} correctly cleared after {primary_trigger} changed"
        else:
            actual_msg = f"{target_field} shows correct filtered options after {primary_trigger} set"
    else:
        actual_msg = _extract_failure_message(result["raw_output"])

    return {
        "passed": passed,
        "actual_outcome": "correctly cascaded" if passed else "cascade failed",
        "actual_message": actual_msg,
        **result,
    }


# ── Branch 3: Role field visibility test ──────────────────────────────────────

def run_visibility_test(scenario: dict, scenario_id: str) -> dict:
    """
    Opens the ticket form and asserts that a given field is absent from the DOM
    (role_exclusion scenario with empty inputs).

    Uses the `field` key from the scenario to know which HTML name attribute
    to look for.  expected_outcome is always "pass" for absence checks.
    """
    flow          = scenario.get("flow", "")
    role          = scenario.get("role", "agent")
    target_field  = scenario.get("field") or ""
    expected_outcome = scenario.get("expected_outcome", "pass")
    assertion_text   = scenario.get("assertion") or f"{target_field} field is not present in the DOM"

    try:
        storage_state = _get_or_create_storage_state(role)
    except ValueError as exc:
        return {
            "passed": False,
            "actual_outcome": "error",
            "actual_message": str(exc),
            "raw_output": "",
            "artifacts": [],
        }

    vis_start_url = f"{FRONTEND_URL}{FLOW_START_PATHS.get(flow, '/cases')}"
    visibility_code = f"""\
from playwright.sync_api import Page, expect


def test_visibility(page: Page):
    page.goto("{vis_start_url}")
    page.wait_for_load_state("domcontentloaded")
    # Open the New Ticket modal
    page.get_by_role("button", name="New Case").click(timeout=30000)
    # Wait for the modal to render — avoid networkidle which never fires on polling apps
    page.wait_for_timeout(1000)

    field_name      = {target_field!r}
    expected_outcome = {expected_outcome!r}

    locator = page.locator(f"[name='{{field_name}}']")

    if expected_outcome == "pass":
        # Field should NOT be present
        assert locator.count() == 0, (
            f"Expected field '{{field_name}}' to be absent, but it is present in the DOM"
        )
    else:
        # Field SHOULD be present
        expect(locator.first).to_be_visible(timeout={PLAYWRIGHT_TIMEOUT_MS})
"""

    result = _run_playwright(visibility_code, scenario_id, storage_state_path=storage_state)
    passed = result["passed"]

    return {
        "passed": passed,
        "actual_outcome": "pass" if passed else "fail",
        "actual_message": assertion_text if passed else _extract_failure_message(result["raw_output"]),
        **result,
    }


# ── Branch 4: Multi-session test ──────────────────────────────────────────────

def run_multi_session_scenario(scenario: dict, scenario_id: str, context_json: dict | None) -> dict:
    """
    Session A: Create a ticket via the API (primary role).
    Session B: Log in as the second_session role and navigate to the ticket
               URL to verify visibility.

    Expected outcomes:
      "pass" → Session B can see the ticket page (no redirect, no 404)
      "fail" → Session B cannot access it (redirect or error)
    """
    primary_role  = scenario.get("role", "agent")
    second_sess   = scenario.get("second_session") or {}
    second_role   = second_sess.get("role", "agent")

    # Support per-scenario named credentials via second_session.key.
    # If the scenario has {"key": "carloss"}, look for TEST_SECOND_AGENT_CARLOSS_EMAIL / PASSWORD
    # and register it as a dynamic role entry so storage-state caching still works.
    second_key = (second_sess.get("key") or "").strip().upper()
    if second_key:
        named_email    = os.getenv(f"TEST_SECOND_AGENT_{second_key}_EMAIL", "")
        named_password = os.getenv(f"TEST_SECOND_AGENT_{second_key}_PASSWORD", "")
        if named_email and named_password:
            second_role_key = f"second_agent_{second_key.lower()}"
            if second_role_key not in ROLE_CREDENTIALS:
                ROLE_CREDENTIALS[second_role_key] = {
                    "email":      named_email,
                    "password":   named_password,
                    "mode":       "agent" if second_role == "agent" else "user",
                    "login_path": "/se-login" if second_role == "agent" else "/login",
                }
        else:
            second_role_key = f"second_{second_role}"
    else:
        second_role_key = f"second_{second_role}"  # e.g. "second_agent" (default)

    expected_outcome = scenario.get("expected_outcome", "pass")
    expected_message = scenario.get("expected_message") or ""
    inputs = dict(scenario.get("inputs") or {})

    # ── Session A: create ticket ──────────────────────────────────────────────
    ticket_id, ticket_subject = _create_ticket_via_api(inputs, primary_role, context_json)
    if ticket_id is None:
        return {
            "passed": False,
            "actual_outcome": "error",
            "actual_message": (
                "Session A: failed to create ticket via API. "
                "Check credentials and known_entities in context JSON."
            ),
            "raw_output": "",
            "artifacts": [],
        }

    ticket_url = f"{FRONTEND_URL}{TICKET_DETAIL_BASE}/{ticket_id}"

    # ── Session B: verify visibility ──────────────────────────────────────────
    try:
        storage_b = _get_or_create_storage_state(second_role_key)
    except ValueError:
        # Fall back to same role's credentials if dedicated second-session creds not set
        try:
            storage_b = _get_or_create_storage_state(second_role)
        except ValueError as exc:
            return {
                "passed": False,
                "actual_outcome": "error",
                "actual_message": str(exc),
                "raw_output": "",
                "artifacts": [],
            }

    session_b_code = f"""\
from playwright.sync_api import Page, expect


def test_session_b(page: Page):
    page.goto("{ticket_url}")
    # Wait for the SPA to fully load — networkidle is more reliable than a fixed sleep
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        page.wait_for_timeout(3000)

    expected_outcome = {expected_outcome!r}
    ticket_subject = {ticket_subject!r}
    current_url = page.url

    if expected_outcome == "pass":
        # Must still be on the ticket detail page (not redirected to login/dashboard)
        assert "{TICKET_DETAIL_BASE}/{ticket_id}" in current_url, (
            f"Expected to view ticket {ticket_id}, but was redirected to: {{current_url}}"
        )
        # The ticket subject must actually be rendered — rules out blank/spinner pages
        try:
            expect(page.get_by_text(ticket_subject, exact=False)).to_be_visible(timeout={PLAYWRIGHT_TIMEOUT_MS})
        except Exception:
            raise AssertionError(
                f"Ticket page loaded at {{current_url}} but subject {{ticket_subject!r}} "
                f"was not visible — page may have loaded empty or access was silently denied"
            )
    else:
        # Should be redirected away or show access denied.
        # Wait for the content to actually render before drawing conclusions.
        is_redirected = "{TICKET_DETAIL_BASE}/{ticket_id}" not in current_url
        has_error = any(
            msg in page.inner_text("body").lower()
            for msg in ["not authorized", "not found", "access denied", "forbidden"]
        )
        # Wait up to 5s to see if the subject appears (if it never appears, that's a denial)
        subject_visible = False
        try:
            expect(page.get_by_text(ticket_subject, exact=False)).to_be_visible(timeout=5000)
            subject_visible = True
        except Exception:
            pass
        assert is_redirected or has_error or not subject_visible, (
            f"Expected Session B to be denied, but ticket page loaded with content at: {{current_url}}"
        )
"""

    result = _run_playwright(session_b_code, scenario_id, storage_state_path=storage_b)
    passed = result["passed"]
    second_name = second_sess.get("name", second_role)

    return {
        "passed": passed,
        "actual_outcome": "pass" if passed else "fail",
        "actual_message": (
            f"Ticket {ticket_id} {'visible' if passed else 'NOT visible'} to Session B ({second_name})"
        ),
        **result,
    }


# ── Message extraction ────────────────────────────────────────────────────────

def _extract_failure_message(raw_output: str) -> str:
    for pattern in [
        r'AssertionError[:\s]+(.+)',
        r'locator\.to_be_visible.*?AssertionError[^\n]*',
        r'E\s+assert.*',
    ]:
        m = re.search(pattern, raw_output, re.DOTALL)
        if m:
            text = m.group(1) if m.lastindex else m.group(0)
            return text.strip()[:300]
    return ""


# ── Result printer ───────────────────────────────────────────────────────────

def _print_result(status: str, exec_result: dict, inputs: dict):
    """Print a structured, readable summary of one scenario result."""
    actual_outcome = exec_result.get("actual_outcome", "") or ""
    actual_msg     = exec_result.get("actual_message", "") or ""
    passed         = exec_result.get("passed")

    # Derive "Action" (what the app did) vs "Test" (did it match expectations)
    APP_PASS_OUTCOMES = {"submitted", "correctly cascaded", "pass",
                         "correctly rejected"}  # correctly rejected = form rejected, which IS the app behaving
    if actual_outcome in ("error", "timeout"):
        action_label = f"Error ({actual_outcome})"
    elif actual_outcome in ("submitted", "correctly cascaded", "pass"):
        action_label = "Pass"
    elif actual_outcome in ("correctly rejected",):
        action_label = "Fail (correctly rejected)"
    elif actual_outcome in ("cascade failed", "incorrectly accepted", "fail"):
        action_label = "Fail"
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

    # On failure: extract the most useful lines from the raw pytest output
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
            print("           ── pytest output ──")
            for ln in useful:
                print(f"           {ln[:180]}")


# ── Main ──────────────────────────────────────────────────────────────────────

def _load_context(flow: str) -> dict | None:
    ctx_path = TESTGEN_DIR / f"context_{flow}.json"
    if ctx_path.exists():
        try:
            return json.loads(ctx_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    # For post_creation_visibility, fall back to agent context for entity data
    fallback = TESTGEN_DIR / "context_agent_create_ticket.json"
    if fallback.exists():
        try:
            return json.loads(fallback.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Execute scenario matrix against the live iSERV app"
    )
    parser.add_argument("--flow", required=True, help="Flow name, e.g. agent_create_ticket")
    parser.add_argument("--dry-run", action="store_true", help="Print scenarios without running them")
    parser.add_argument("--id", dest="scenario_id", default=None, help="Run only this scenario ID")
    args = parser.parse_args()

    matrix_path = TESTGEN_DIR / f"scenario_matrix_{args.flow}.json"
    if not matrix_path.exists():
        print(f"ERROR: {matrix_path} not found — run generate.py first.", file=sys.stderr)
        sys.exit(1)

    raw = json.loads(matrix_path.read_text(encoding="utf-8"))
    # Support both {"scenarios": [...]} and plain list
    scenarios: list[dict] = raw.get("scenarios", raw) if isinstance(raw, dict) else raw

    if args.scenario_id:
        scenarios = [s for s in scenarios if s.get("id") == args.scenario_id]
        if not scenarios:
            print(f"ERROR: scenario '{args.scenario_id}' not found in {matrix_path}", file=sys.stderr)
            sys.exit(1)

    # Always start with a fresh login — wipe any cached session files so stale
    # JWTs never silently redirect tests to the login page.
    if STORAGE_STATE_DIR.exists():
        for f in STORAGE_STATE_DIR.glob("session_*.json"):
            f.unlink(missing_ok=True)

    context_json = _load_context(args.flow)
    output_path  = TESTGEN_DIR / f"raw_results_{args.flow}.json"

    print(f"── Stage 4: Execute [{args.flow}] ──────────────────────────────────")
    print(f"  {len(scenarios)} scenario(s) to run")

    results: list[dict] = []
    total = len(scenarios)

    for i, scenario in enumerate(scenarios, 1):
        sid         = scenario.get("id", f"TC{i:03d}")
        category    = scenario.get("category", "")
        test_mode   = scenario.get("test_mode", "ui")
        flow        = scenario.get("flow", args.flow)
        inputs      = scenario.get("inputs") or {}
        description = scenario.get("description", "")[:70]

        print(f"\n  [{i:>3}/{total}] {sid} [{category}] {description}")

        if args.dry_run:
            result = {
                "id":               sid,
                "category":         category,
                "flow":             flow,
                "test_mode":        test_mode,
                "description":      scenario.get("description"),
                "inputs":           inputs,
                "expected_outcome": scenario.get("expected_outcome"),
                "expected_message": scenario.get("expected_message"),
                "actual_outcome":   "skipped",
                "actual_message":   "",
                "passed":           None,
                "raw_output":       "dry_run",
                "artifacts":        [],
            }
            print(f"         [DRY RUN] skipped")
            results.append(result)
            continue

        try:
            # ── Route to the correct executor ─────────────────────────────────
            if test_mode == "multi_session":
                exec_result = run_multi_session_scenario(scenario, sid, context_json)

            elif category == "role_field_visibility" and not inputs:
                exec_result = run_visibility_test(scenario, sid)

            elif category == "cascade_dependency":
                exec_result = run_cascade_test(scenario, sid, context_json)

            else:
                exec_result = run_ui_test(scenario, sid)

            passed = exec_result.get("passed")
            status = "PASS" if passed else ("SKIP" if passed is None else "FAIL")
            _print_result(status, exec_result, inputs)

            result = {
                "id":               sid,
                "category":         category,
                "flow":             flow,
                "test_mode":        test_mode,
                "description":      scenario.get("description"),
                "inputs":           inputs,
                "expected_outcome": scenario.get("expected_outcome"),
                "expected_message": scenario.get("expected_message"),
                "actual_outcome":   exec_result.get("actual_outcome", ""),
                "actual_message":   exec_result.get("actual_message", ""),
                "passed":           passed,
                "raw_output":       exec_result.get("raw_output", ""),
                "artifacts":        exec_result.get("artifacts", []),
            }

        except Exception as exc:
            _print_result("ERROR", {"actual_message": str(exc), "raw_output": str(exc), "passed": False}, inputs)
            result = {
                "id":               sid,
                "category":         category,
                "flow":             flow,
                "test_mode":        test_mode,
                "description":      scenario.get("description"),
                "inputs":           inputs,
                "expected_outcome": scenario.get("expected_outcome"),
                "expected_message": scenario.get("expected_message"),
                "actual_outcome":   "error",
                "actual_message":   str(exc),
                "passed":           False,
                "raw_output":       str(exc),
                "artifacts":        [],
            }

        results.append(result)

    output_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n  Results saved → {output_path}")

    passed_n  = sum(1 for r in results if r.get("passed") is True)
    failed_n  = sum(1 for r in results if r.get("passed") is False)
    skipped_n = sum(1 for r in results if r.get("passed") is None)
    print(f"  Summary: {passed_n} passed / {failed_n} failed / {skipped_n} skipped  (total {total})")

    return results


if __name__ == "__main__":
    main()
