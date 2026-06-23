"""
Stage 3 — Base Script Builder (Codegen-driven)

TWO-STEP PROCESS per flow:

  STEP 1 — record (interactive, run once / whenever the form changes):
      python testGen/record.py --record agent_create_ticket
      python testGen/record.py --record customer_create_ticket

      Pre-authenticates as the flow's primary role (saves browser storage state),
      then launches `playwright codegen` at /tickets. Open the "New Ticket" modal,
      fill the form with ONE valid happy-path entry, submit, then close the browser.
      Output: testGen/base_test_{flow}_recorded.py

  STEP 2 — build the template:
      python testGen/record.py --build agent_create_ticket
      python testGen/record.py --build customer_create_ticket
      python testGen/record.py --build-all      # build all that have a recorded file

      Reads the recorded file, replaces hardcoded values with inputs.get(<field>)
      calls using FIELD_MAPS, wraps everything in run_test(...), and writes
      testGen/base_test_{flow}.py.

If FIELD_MAPS doesn't match the fill order captured in the recording, edit the
field_map list below and re-run Step 2 — no re-recording needed.
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

TESTGEN_DIR = Path(__file__).parent
load_dotenv(TESTGEN_DIR / ".env")

FRONTEND_URL       = os.getenv("FRONTEND_URL", "http://localhost:3000")
PLAYWRIGHT_BROWSER = os.getenv("PLAYWRIGHT_BROWSER", "chromium")
PLAYWRIGHT_HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() == "true"
PLAYWRIGHT_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", "10000"))

ROLE_CREDENTIALS = {
    "agent": {
        "email":    os.getenv("TEST_AGENT_EMAIL", ""),
        "password": os.getenv("TEST_AGENT_PASSWORD", ""),
    },
    "customer": {
        "email":    os.getenv("TEST_CUSTOMER_EMAIL", ""),
        "password": os.getenv("TEST_CUSTOMER_PASSWORD", ""),
    },
}

# Agent portal is at /se-login; customer portal at /login.
ROLE_LOGIN_PATHS = {
    "agent":    "/se-login",
    "customer": "/login",
}

STORAGE_STATE_DIR = TESTGEN_DIR / "storage_states"


# ── Per-flow configuration ────────────────────────────────────────────────────

# field_map: order in which the codegen recording filled / selected each form
# field.  Must match the actual fill order.  Edit here and re-run --build if the
# order changes; no re-recording needed.
FLOW_CONFIG: dict[str, dict] = {
    "agent_create_ticket": {
        "role": "agent",
        "start_path": "/cases",
        # List only the fields that appear as actual value-selection actions in the
        # recording (option-click, text-click, fill, select_option, set_input_files),
        # in the order they appear.  Fields filled via non-standard interactions
        # (date-picker button clicks, etc.) are omitted — they stay verbatim.
        "field_map": [
            "customer", "user", "equipment", "service_type", "department",
            "staff_accountable", "staff_assigned", "priority",
            "subject", "description", "files",
        ],
        # pass assertion: on success the app redirects to /cases/<id>
        "pass_assertion": (
            f'page.wait_for_url("**/cases/**", timeout={PLAYWRIGHT_TIMEOUT_MS})'
        ),
    },
    "customer_create_ticket": {
        "role": "customer",
        "start_path": "/dashboard",
        "field_map": [
            "equipment", "service_type", "priority",
            "subject", "description", "files",
        ],
        "submit_action": 'page.get_by_role("button", name="Submit").click()',
        "pass_assertion": (
            f'page.wait_for_url("**/dashboard**", timeout={PLAYWRIGHT_TIMEOUT_MS})'
        ),
    },
}


# ── Step 1: authenticate and launch codegen ───────────────────────────────────

def _setup_auth_storage(role: str) -> Path:
    """
    Log in via Playwright using the role's credentials, then save browser
    storage state (cookies + localStorage containing JWT) to a file.
    That file is passed to codegen via --load-storage so recording starts
    already authenticated.
    """
    STORAGE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    storage_path = STORAGE_STATE_DIR / f"session_{role}.json"
    creds = ROLE_CREDENTIALS[role]
    login_url = f"{FRONTEND_URL}{ROLE_LOGIN_PATHS[role]}"

    if storage_path.exists():
        print(f"  ✓ Reusing existing session for role: {role}")
        return storage_path

    raise ValueError(
        f"No saved session for role '{role}'. "
        f"Create it by running Playwright codegen while logged in:\n\n"
        f"  python -m playwright codegen --save-storage \"{storage_path}\" {login_url}\n\n"
        f"Log in as the {role} user, then close the browser. "
        f"Re-run record.py after the file is saved."
    )


def record(flow: str):
    """
    Step 1: pre-authenticate then launch playwright codegen.
    The tester interacts with the app to record the happy-path form fill.
    """
    if flow not in FLOW_CONFIG:
        print(f"ERROR: unknown flow '{flow}'. Available: {', '.join(FLOW_CONFIG)}")
        sys.exit(1)

    cfg = FLOW_CONFIG[flow]
    recorded_path = TESTGEN_DIR / f"base_test_{flow}_recorded.py"
    url = f"{FRONTEND_URL}{cfg['start_path']}"

    print(f"\n  Setting up authentication for role: {cfg['role']}")
    storage_path = _setup_auth_storage(cfg["role"])

    print(f"\n  Launching playwright codegen")
    print(f"  URL: {url}")
    print(f"  Instructions:")
    print(f"    1. The browser opens at {url} (you are already logged in).")
    print(f"    2. Click 'New Ticket' to open the Create Ticket modal.")
    print(f"    3. Fill all form fields — customer → user → equipment → ... → subject → description.")
    print(f"    4. Submit the form.")
    print(f"    5. Close the browser window.")
    print(f"  Output will be saved to: {recorded_path}\n")

    cmd = [
        sys.executable, "-m", "playwright", "codegen",
        url,
        "--target", "python",
        "-o", str(recorded_path),
        "--load-storage", str(storage_path),
    ]
    subprocess.run(cmd, check=True)

    if recorded_path.exists():
        print(f"\n  ✓ Recorded script saved → {recorded_path}")
        print(f"  Next: python testGen/record.py --build {flow}")
    else:
        print(f"\n  WARN: {recorded_path} was not created.")
        print(f"  The browser may have been closed before codegen could save.")

    storage_path.unlink(missing_ok=True)


# ── Step 2: transform recorded script → parameterised template ────────────────

# fill("value") and select_option("value")
_ACTION_RE = re.compile(
    r'^(?P<indent>\s*)(?P<target>page\.[^\n]*?)'
    r'\.(?P<method>fill|select_option)'
    r'\((?P<quote>["\'])(?P<value>.*?)(?P=quote)\)\s*$'
)

# get_by_role("option", name="VALUE").click()  — React-Select item selection
_OPTION_CLICK_RE = re.compile(
    r'^\s*page\.get_by_role\("option",\s*name=(?P<q>["\'])(?P<value>.*?)(?P=q)\)\.click\(\)\s*$'
)

# get_by_text("VALUE").click() or get_by_text("VALUE", exact=True).click()
# Only treated as a value-selection when it follows a combobox opener.
_TEXT_CLICK_RE = re.compile(
    r'^\s*page\.get_by_text\((?P<q>["\'])(?P<value>.*?)(?P=q)(?:,\s*exact=(?:True|False))?\)\.click\(\)\s*$'
)

# something.set_input_files("path")
_SET_INPUT_FILES_RE = re.compile(
    r'^\s*(?P<target>page\.[^\n]*?)\.set_input_files\((?P<q>["\'])(?P<value>.*?)(?P=q)\)\s*$'
)

_SKIP_PREFIXES = (
    "from playwright", "import re", "with sync_playwright",
    "browser =", "context =", "page =", "context.close", "browser.close",
    "def run(",
)


def _is_potential_opener(stripped: str) -> bool:
    """
    Return True for lines that open a dropdown or file picker and may be immediately
    followed by a value-selection action (option click, text click, set_input_files).

    Combobox clicks: get_by_role("combobox", ...).click()
    Filter-div clicks: locator("div").filter(...).click()  — precede set_input_files
    """
    return (
        'get_by_role("combobox"' in stripped and stripped.endswith('.click()')
    ) or (
        '.filter(' in stripped and stripped.endswith('.click()')
    ) or (
        # get_by_text(...).click() can be a file-picker opener (e.g. "Drag and drop files here")
        # It's only treated as a value-selection when pending_opener is already set (text-click
        # branch); when pending_opener is None it falls here and becomes an opener for set_input_files.
        'get_by_text(' in stripped and stripped.endswith('.click()')
    )


def _build_run_test_body(recorded_source: str, flow: str) -> str:
    """
    Walk the recorded lines and produce a parameterised run_test() body.

    Value-selection actions are mapped in order to field_map entries:
      • fill / select_option  → inputs.get(field, "") inline replacement
      • option click          → wrapped with opener in `if inputs.get(field):` block
      • text click            → same (only when following a combobox opener)
      • set_input_files       → same

    Combobox / filter-div openers that immediately precede a value selection are
    pulled into the conditional block so the dropdown isn't opened for absent fields.
    """
    field_map = FLOW_CONFIG[flow]["field_map"]
    body_lines: list[str] = []
    field_idx = 0
    pending_opener: str | None = None  # stripped text of the most recent opener line

    for line in recorded_source.splitlines():
        stripped = line.strip()

        # ── strip boilerplate ─────────────────────────────────────────────────
        if re.match(r'^\s*run\(playwright\)\s*$', line):
            pending_opener = None
            continue
        if stripped.startswith(_SKIP_PREFIXES):
            pending_opener = None
            continue
        if stripped.startswith("page.goto("):
            pending_opener = None
            continue
        if not stripped:
            continue

        # ── classify the line ─────────────────────────────────────────────────
        m_fill   = _ACTION_RE.match(line)
        m_option = _OPTION_CLICK_RE.match(line)
        m_files  = _SET_INPUT_FILES_RE.match(line)
        # text-click is only a value selection when a combobox was just opened
        m_text   = _TEXT_CLICK_RE.match(line) if pending_opener else None

        is_value_sel = bool(m_fill or m_option or m_files or m_text)

        if is_value_sel:
            if field_idx >= len(field_map):
                print(
                    f"  WARN: more value-selection calls than field_map entries "
                    f"— kept unchanged: {stripped[:70]!r}"
                )
                body_lines.append(stripped)
                pending_opener = None
                continue

            field = field_map[field_idx]
            field_idx += 1

            if m_fill:
                # fill / select_option: safe to call with empty string, no conditional needed
                method = m_fill.group("method")
                target = m_fill.group("target").strip()
                if method == "fill":
                    expr = (
                        f'str(inputs.get("{field}", "")) '
                        f'if inputs.get("{field}") is not None else ""'
                    )
                    body_lines.append(f'{target}.fill({expr})')
                else:
                    body_lines.append(
                        f'{target}.select_option(str(inputs.get("{field}", "")))'
                    )
                # opener before a fill is a focus-click — leave it verbatim (already emitted)
                pending_opener = None

            else:
                # option / text / set_input_files: must be conditional
                if m_option:
                    val_line = f'page.get_by_role("option", name=str(inputs["{field}"])).click()'
                elif m_text:
                    # Scope to the open listbox so names that appear elsewhere on the
                    # page (other form fields, sidebar, header) don't cause strict-mode
                    # violations. .first handles rare duplicates inside the dropdown itself.
                    val_line = f'page.get_by_role("listbox").get_by_text(str(inputs["{field}"]), exact=True).first.click()'
                else:  # m_files — always target the hidden <input type="file">, not the MUI div wrapper
                    val_line = f'page.locator("input[type=\'file\']").set_input_files(str(inputs["{field}"]))'

                if m_files:
                    # For file upload, NEVER emit the opener click.
                    # Clicking the dropzone div triggers the native file picker which blocks
                    # Playwright even in headless mode. set_input_files bypasses it entirely.
                    if pending_opener and body_lines and body_lines[-1] == pending_opener:
                        body_lines.pop()
                    body_lines.append(f'if inputs.get("{field}"):')
                    body_lines.append(f'    {val_line}')
                # If the opener is the last emitted line, pull it into the conditional
                elif pending_opener and body_lines and body_lines[-1] == pending_opener:
                    body_lines.pop()
                    body_lines.append(f'if inputs.get("{field}"):')
                    body_lines.append(f'    {pending_opener}')
                    body_lines.append(f'    {val_line}')
                else:
                    body_lines.append(f'if inputs.get("{field}"):')
                    body_lines.append(f'    {val_line}')

                pending_opener = None

        else:
            # Non-value-selection line
            if _is_potential_opener(stripped):
                pending_opener = stripped
            else:
                pending_opener = None
            # Wrap Submit click in try/except so a disabled button (e.g. after
            # an invalid file upload) doesn't time out and fail the whole test.
            if re.search(r'get_by_role\(["\']button["\'].*Submit.*\.click\(\)', stripped):
                body_lines.append('try:')
                body_lines.append(f'    {stripped.replace(".click()", ".click(timeout=5000)")}')
                body_lines.append('except Exception:')
                body_lines.append('    pass  # Submit may be disabled (e.g. invalid file type rejected by dropzone)')
            else:
                body_lines.append(stripped)

    if field_idx < len(field_map):
        missing = field_map[field_idx:]
        print(
            f"  WARN: fewer value-selection calls recorded than expected. "
            f"Fields not parameterised: {missing}"
        )

    # Normalise to 4-space function-body indent (body_lines stores stripped content)
    return "\n".join("    " + l for l in body_lines if l.strip())


def _build_template(recorded_source: str, flow: str) -> str:
    cfg = FLOW_CONFIG[flow]
    body = _build_run_test_body(recorded_source, flow)
    field_keys = ", ".join(cfg["field_map"])
    recorded_filename = f"base_test_{flow}_recorded.py"
    # Bake the full start URL into the generated script so it works standalone.
    start_url = f"{FRONTEND_URL}{cfg['start_path']}"

    return f'''\
import re
from playwright.sync_api import Page, expect


def run_test(page: Page, inputs: dict, expected_outcome: str, expected_message: str):
    """
    Execute one {flow} scenario.

    inputs keys : {field_keys}
    expected_outcome : "pass" | "fail"
    expected_message : exact visible text expected on failure, or None

    Interaction lines below were auto-generated from a playwright codegen
    session ({recorded_filename}).  Edit selectors here if the form changes.
    Assertions are injected by execute.py — do not add them here.
    """
    page.goto("{start_url}")

{body}
'''


def save_base_script(flow: str):
    if flow not in FLOW_CONFIG:
        print(f"ERROR: unknown flow '{flow}'. Available: {', '.join(FLOW_CONFIG)}")
        sys.exit(1)

    recorded_path = TESTGEN_DIR / f"base_test_{flow}_recorded.py"
    output_path   = TESTGEN_DIR / f"base_test_{flow}.py"

    if not recorded_path.exists():
        raise FileNotFoundError(
            f"{recorded_path} not found — run "
            f"`python testGen/record.py --record {flow}` first."
        )

    recorded_source = recorded_path.read_text(encoding="utf-8")
    template = _build_template(recorded_source, flow)
    output_path.write_text(template, encoding="utf-8")

    print(f"  ✓ Base script → {output_path}")
    print(f"  → Review selectors and FIELD_MAPS['{flow}'] mapping before running Stage 4.")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("── Stage 3: Base Script Builder ──────────────────────────────────")

    if "--record" in sys.argv:
        idx = sys.argv.index("--record")
        if idx + 1 >= len(sys.argv):
            print("ERROR: --record requires a flow name")
            sys.exit(1)
        record(sys.argv[idx + 1].replace("-", "_"))

    elif "--build-all" in sys.argv:
        built = 0
        for f in FLOW_CONFIG:
            rec = TESTGEN_DIR / f"base_test_{f}_recorded.py"
            if rec.exists():
                save_base_script(f)
                built += 1
            else:
                print(f"  SKIP {f} — recorded file not found (run --record {f} first)")
        print(f"\n  ✓ Built {built} base script(s).")

    elif "--build" in sys.argv:
        idx = sys.argv.index("--build")
        if idx + 1 >= len(sys.argv):
            print("ERROR: --build requires a flow name")
            sys.exit(1)
        save_base_script(sys.argv[idx + 1].replace("-", "_"))

    else:
        print("Usage:")
        print("  python testGen/record.py --record <flow>   # Step 1: interactive recording")
        print("  python testGen/record.py --build  <flow>   # Step 2: build parameterised template")
        print("  python testGen/record.py --build-all       # Step 2: build all recorded flows")
        print(f"\nAvailable flows: {', '.join(FLOW_CONFIG)}")
