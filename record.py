"""
record.py — Record + Build UI flow base scripts (approach3)

Two-step process, run once per flow_type — flow_types come from generate2.py's
RECORDINGS_PROMPT output (matrices/recordings_{flow}.json /
matrices/recordings_needed_{flow}.md), NOT from a hardcoded list in this file.
One business `flow` (e.g. create_service_report) can fan out into several
flow_types (different entry points/roles), each needing its own recording.

STEP 1 — record (interactive):
    python approach3/record.py --record create_service_report create_service_report_from_list
    python approach3/record.py --record-all create_service_report   # walks every flow_type needing one

  Pre-authenticates as the flow_type's role, launches `playwright codegen` at
  its start_url, and prints the exact steps (from the matrix) to click
  through. Fill every visible field with any valid value, submit, close the
  browser.
  Output: base_tests/base_test_{flow}_{short flow_type}_recorded.py
  (flow_type's redundant `{flow}_` prefix, if any, is stripped for the
  filename only — see base_name() — the full flow_type is still what the
  matrix uses to key scenarios)

STEP 2 — build:
    python approach3/record.py --build create_service_report create_service_report_from_list
    python approach3/record.py --build-all create_service_report

  Reads the recorded file and auto-detects every field's name straight from
  its own selector (HTML name=, label text, or the "Select {label}" pattern
  iSERV's FormField.js uses for dropdowns) — no hand-maintained field list.
  Writes base_tests/base_test_{flow}_{short flow_type}.py exposing:

      FIELD_ORDER, FIELD_TYPES     — field metadata
      open_form(page)              — navigate + open the form
      set_field(page, field, val)  — generic setter, dispatches on FIELD_TYPES
      read_field(page, field)      — generic getter (current value/label)
      open_field(page, field)      — opens a dropdown without selecting
                                      (used to inspect its option list)
      is_field_present(page, field) — DOM existence check
      run_test(page, inputs, expected_outcome, expected_message)
                                    — loops FIELD_ORDER calling set_field, then submits

  A category-agnostic execute.py calls into these functions directly —
  cascade/visibility/permission checks reuse the same field table instead of
  execute.py hand-writing Playwright per test category.

If a field can't be auto-named (no label/name anywhere near its recorded
selector) it's assigned field_unknown_N with a printed warning — open the
generated file and rename that one key; no re-recording needed.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

# Windows consoles default to cp1252, which can't encode em-dashes/arrows used
# below — reconfigure so prints never crash instead of avoiding unicode entirely.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

MATRICES_DIR      = BASE_DIR / "matrices"
BASE_TESTS_DIR    = BASE_DIR / "base_tests"
STORAGE_STATE_DIR = BASE_DIR / "storage_states"

FRONTEND_URL          = os.getenv("FRONTEND_URL", "http://localhost:3000")
PLAYWRIGHT_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", "10000"))


# ── Recordings plan (generate2.py's RECORDINGS_PROMPT output) ─────────────────

def load_recordings(flow: str) -> list[dict]:
    path = MATRICES_DIR / f"recordings_{flow}.json"
    if not path.exists():
        print(f"ERROR: {path} not found — run generate2.py --flow {flow} first.")
        sys.exit(1)
    return json.loads(path.read_text(encoding="utf-8"))


def find_recording(flow: str, flow_type: str) -> dict:
    for rec in load_recordings(flow):
        if rec["flow_type"] == flow_type:
            return rec
    print(f"ERROR: flow_type '{flow_type}' not found for flow '{flow}'. "
          f"Check matrices/recordings_needed_{flow}.md for valid flow_types.")
    sys.exit(1)


def base_name(flow: str, flow_type: str) -> str:
    """
    Filename stem for this flow_type's base script.

    flow_type is LLM-generated and almost always repeats `flow` as a prefix
    (e.g. flow="create_service_report", flow_type="create_service_report_from_list")
    — stripping that redundant prefix turns
    base_test_create_service_report_create_service_report_from_list.py into
    base_test_create_service_report_from_list.py. The full flow_type is still
    what matrices key scenarios by; only the filename gets shorter.

    execute.py MUST import this same function to look files up — it can't
    reconstruct the filename with its own `base_test_{flow}_{flow_type}.py`
    formula, or lookups will miss.
    """
    if flow_type == flow:
        return f"base_test_{flow}"
    prefix = flow + "_"
    short = flow_type[len(prefix):] if flow_type.startswith(prefix) else flow_type
    return f"base_test_{flow}_{short}"


# ── Role credentials — dynamic, any role name generate2.py assigns ───────────

def _role_env_prefix(role: str) -> str:
    # "any_authorized_role" means literally any account works — default to agent.
    if role.strip().lower().replace(" ", "_") in ("", "any_authorized_role"):
        return "AGENT"
    return re.sub(r"[^A-Z0-9]+", "_", role.upper()).strip("_")


def _role_login_path(role: str) -> str:
    return "/login" if "customer" in role.lower() else "/se-login"


def _get_credentials(role: str) -> tuple[str, str]:
    prefix = _role_env_prefix(role)
    email = os.getenv(f"TEST_{prefix}_EMAIL", "")
    password = os.getenv(f"TEST_{prefix}_PASSWORD", "")
    if not email or not password:
        print(
            f"ERROR: missing credentials for role '{role}'. "
            f"Add TEST_{prefix}_EMAIL and TEST_{prefix}_PASSWORD to approach3/.env"
        )
        sys.exit(1)
    return email, password


def _setup_auth_storage(role: str) -> Path:
    """
    Log in as `role` via a headless Playwright script and save storage state
    (cookies + localStorage) so codegen/tests can start already authenticated.
    Always re-logs in to avoid stale JWT issues.
    """
    STORAGE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe_role = _role_env_prefix(role).lower()
    storage_path = STORAGE_STATE_DIR / f"session_{safe_role}.json"

    email, password = _get_credentials(role)
    login_url = f"{FRONTEND_URL}{_role_login_path(role)}"
    storage_path_str = str(storage_path).replace("\\", "/")

    print(f"  Logging in as '{role}' at {login_url} ...")
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
        print(f"ERROR: could not reach {login_url} — {{exc}}. Is the app running?", file=sys.stderr)
        sys.exit(1)
    try:
        page.wait_for_selector("[name='email']", timeout=15000)
    except Exception:
        print(f"ERROR: login form not found at {{page.url}}", file=sys.stderr)
        sys.exit(1)
    page.fill("[name='email']", "{email}")
    page.fill("[name='user_password']", "{password}")
    page.locator("[name='user_password']").press("Enter")
    try:
        page.wait_for_function(
            "() => !window.location.pathname.includes('login')", timeout=15000,
        )
    except Exception:
        print("ERROR: still on login page after submit — check credentials in approach3/.env", file=sys.stderr)
        sys.exit(1)
    ctx.storage_state(path="{storage_path_str}")
    browser.close()
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(setup_script)
        tmp = f.name
    try:
        result = subprocess.run([sys.executable, tmp], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"ERROR: could not create session for role '{role}':\n{(result.stdout + result.stderr).strip()}")
            sys.exit(1)
        print(f"  Session created for role: {role}")
    finally:
        Path(tmp).unlink(missing_ok=True)

    return storage_path


# ── Step 1: record ─────────────────────────────────────────────────────────────

def _resolve_placeholders(url_template: str) -> str:
    placeholders = re.findall(r"\{([^{}]+)\}", url_template)
    if not placeholders:
        return url_template
    values = {p: input(f"  Enter a value for {{{p}}} (used in start URL): ").strip() for p in placeholders}
    return url_template.format(**values)


def record_one(flow: str, flow_type: str, force: bool = False):
    rec = find_recording(flow, flow_type)
    BASE_TESTS_DIR.mkdir(parents=True, exist_ok=True)
    recorded_path = BASE_TESTS_DIR / f"{base_name(flow, flow_type)}_recorded.py"

    role = rec.get("role", "agent")
    print(f"\n{'=' * 70}\nRecording: {flow_type}  (role: {role})")
    print(f"{rec.get('description', '')}")

    storage_path = _setup_auth_storage(role)
    start_url = f"{FRONTEND_URL}{_resolve_placeholders(rec['start_url'])}"

    print(f"\n  URL: {start_url}")
    print(f"  Steps to click through (fill every visible field with any valid value):")
    for i, step in enumerate(rec.get("steps", []), 1):
        print(f"    {i}. {step}")
    print(f"\n  Close the browser when done. Output → {recorded_path}\n")

    cmd = [
        sys.executable, "-m", "playwright", "codegen",
        start_url,
        "--target", "python",
        "-o", str(recorded_path),
        "--load-storage", str(storage_path),
    ]
    subprocess.run(cmd, check=True)

    if recorded_path.exists():
        print(f"  Recorded → {recorded_path}")
        print(f"  Next: python approach3/record.py --build {flow} {flow_type}")
    else:
        print(f"  WARN: {recorded_path} was not created — browser may have closed before saving.")


def record_all(flow: str, force: bool = False):
    recs = load_recordings(flow)
    print(f"{len(recs)} flow_type(s) to record for '{flow}'")
    for rec in recs:
        record_one(flow, rec["flow_type"], force=force)
    print(f"\nNext: python approach3/record.py --build-all {flow}")


# ── Step 2: build — parse the recording, auto-detect field names ─────────────

_ACTION_RE = re.compile(
    r'^(?P<indent>\s*)(?P<target>page\.[^\n]*?)'
    r'\.(?P<method>fill|select_option)'
    r'\((?P<quote>["\'])(?P<value>.*?)(?P=quote)\)\s*$'
)
_OPTION_CLICK_RE = re.compile(
    r'^\s*page\.get_by_role\("option",\s*name=(?P<q>["\'])(?P<value>.*?)(?P=q)\)\.click\(\)\s*$'
)
_TEXT_CLICK_RE = re.compile(
    r'^\s*page\.get_by_text\((?P<q>["\'])(?P<value>.*?)(?P=q)(?:,\s*exact=(?:True|False))?\)\.click\(\)\s*$'
)
_NAV_TEXT_CLICK_RE = re.compile(
    r'^page\.get_by_text\((?P<q>["\'])(?P<text>.+?)(?P=q)\)\.click\(\)$'
)
_SET_INPUT_FILES_RE = re.compile(
    r'^\s*(?P<target>page\.[^\n]*?)\.set_input_files\((?P<q>["\'])(?P<value>.*?)(?P=q)\)\s*$'
)
_CHECKBOX_RE = re.compile(r'^\s*(?P<target>page\..*?)\.check\(\)\s*$')
_GRIDCELL_CLICK_RE = re.compile(r'^\s*(?P<target>page\..*get_by_role\(["\']gridcell["\'].*\))\.click\(\)\s*$')
_DATE_BUTTON_HINT = re.compile(r'hoose date', re.IGNORECASE)

_SKIP_PREFIXES = (
    "from playwright", "import re", "with sync_playwright",
    "browser =", "context =", "page =", "context.close", "browser.close",
    "def run(",
)


def _strip_stale_count_badge(line: str) -> str:
    """
    Ticket-page menu items render a label and a live count badge concatenated
    (TicketMenu.js: Typography "Service Reports" + count Box "65" with no
    separator), so codegen captures "Service Reports65" — which goes stale as
    soon as the count changes. The earlier fix (match the bare label with
    exact=True) was WRONG: the app's left sidebar has a nav item with the
    same bare label, so the click landed on the sidebar and navigated to the
    main module page — silently testing the wrong entry point (observed live
    on the from-ticket flow). Emit a _click_menu_item() call instead, which
    prefers the digit-suffixed candidate at runtime (only the in-page menu
    item carries a count) and tolerates any current count value.
    """
    m = _NAV_TEXT_CLICK_RE.match(line)
    if not m:
        return line
    text = m.group("text")
    label = re.sub(r'\d+$', '', text)
    if label == text or not label.strip():
        return line
    return f'_click_menu_item(page, {label!r})'


def _is_potential_opener(stripped: str) -> bool:
    """Lines that open a dropdown/file-picker and may precede a value-selection action.

    Deliberately does NOT include a bare `get_by_text(...).click()` — that's
    indistinguishable from a plain navigation click (e.g. clicking a card),
    and treating it as an opener causes the next unrelated click to get
    swallowed as if it were a dropdown's selected option. Real react-select/
    MUI dropdown openers in this codebase always carry either a combobox
    role, a `.filter(has_text=...)`, or a MUI input wrapper class — a text
    click alone is not enough signal.
    """
    return (
        ('get_by_role("combobox"' in stripped and stripped.endswith('.click()'))
        or ('.filter(' in stripped and stripped.endswith('.click()'))
        or ('page.locator(' in stripped and 'MuiInputBase' in stripped and stripped.endswith('.click()'))
        or ('get_by_role("button"' in stripped and 'name="Open"' in stripped and stripped.endswith('.click()'))
        or ('get_by_role("button"' in stripped and _DATE_BUTTON_HINT.search(stripped) and stripped.endswith('.click()'))
    )


# ── Field-key auto-detection ───────────────────────────────────────────────────
#
# Codegen's own selectors already carry the field's identity — an HTML name=
# attribute, a <label> association, or (for iSERV's MUI dropdowns) the
# "Select {label}" accessible name FormField.js always sets. We just read it
# back out of the line instead of asking a human to declare field order.

_LABEL_PATTERNS = (
    r'get_by_label\(["\']([^"\']+)["\']',
    r'get_by_placeholder\(["\']([^"\']+)["\']',
    r'get_by_role\(["\']combobox["\'],\s*name=["\']([^"\']+)["\']',
    r'get_by_role\(["\']textbox["\'],\s*name=["\']([^"\']+)["\']',
    r'get_by_role\(["\']checkbox["\'],\s*name=["\']([^"\']+)["\']',
    r'get_by_role\(["\']button["\'],\s*name=["\']([^"\']+)["\']',  # date pickers
    r'\.filter\(has_text=["\']([^"\']+)["\']',
    # codegen wraps exact-text filters in re.compile(r"^...$") — pull the label
    # out of the anchored pattern (e.g. has_text=re.compile(r"^Select Case$"))
    r'\.filter\(has_text=re\.compile\(r?["\']\^?([^"\'^$\\]+?)\$?["\']',
    r'\[name=["\']([^"\']+)["\']\]',
)
# Deliberately no bare `name=["']...["']` catch-all: it would also match
# get_by_role("option", name="...") / get_by_role("gridcell", name="...") lines,
# whose name= is the SELECTED VALUE, not the field's identity. Callers must
# only pass opener/target text here — never the value-selection line itself
# for option/text/gridcell clicks (see key_for call sites below).
_SELECT_PREFIX_RE = re.compile(r'^select\s+', re.IGNORECASE)

# Rich-text editors (jodit, quill, prosemirror, generic wysiwyg) render no
# label/name/placeholder codegen can capture — the selector is a bare CSS
# class — so label detection can't name them. Every rich-text editor in this
# app is a Description field; key it that way instead of field_unknown_N.
_RICH_TEXT_HINT_RE = re.compile(r'jodit|wysiwyg|rich-?text|ql-editor|prosemirror', re.IGNORECASE)

# Accessible names in codegen often include verbose descriptions appended to the
# actual label, or long placeholder-style prefixes.  These regexes strip the noise
# before slugifying so field keys stay short and meaningful.
_PAREN_CONTENT_RE  = re.compile(r'\s*\([^)]*\)')          # "(s)", "(optional)", …
_VERBOSE_SUFFIX_RE = re.compile(                           # " When enabled, …" etc.
    r'[\s,]+\b(when|if|note|selected)\b.+$', re.IGNORECASE
)
_VERBOSE_PREFIX_RE = re.compile(                           # "Please enter the …"
    r'^(please\s+)?(choose|enter|specify|type|add)\s+(a\s+|an\s+|the\s+)?',
    re.IGNORECASE,
)
_FILLER_WORDS = frozenset({'for', 'of', 'in', 'to', 'by', 'the', 'a', 'an', 'this', 'that', 'with'})


def _normalize_label(label: str) -> str:
    """Strip verbose role-name noise so keys stay short and meaningful."""
    label = _PAREN_CONTENT_RE.sub('', label)                       # "Assignee(s)" → "Assignee"
    label = _VERBOSE_SUFFIX_RE.sub('', label).strip().strip(',')   # "…, When enabled," → trimmed
    label = _VERBOSE_PREFIX_RE.sub('', label).strip()              # "Please enter the " → ""
    # After prefix stripping a phrase like "name for the" keep only the first word.
    words = label.split()
    if len(words) >= 2 and words[1].lower() in _FILLER_WORDS:
        label = words[0]
    return label


def _slugify(text: str) -> str:
    text = _SELECT_PREFIX_RE.sub("", text.strip())
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return text or "field"


def _label_from_text(text: str) -> str | None:
    for pat in _LABEL_PATTERNS:
        m = re.search(pat, text)
        if m and m.group(1).strip().lower() not in ("open", ""):
            label = _normalize_label(m.group(1).strip())
            return label if label else None
    return None


class FieldKeyer:
    """Turns recorded selector text into a stable field key, tracking fallbacks."""

    def __init__(self):
        self._unknown = 0
        self._files_seen = 0

    def key_for(self, sources: list[str], *, is_file: bool = False) -> str:
        """
        sources: candidate lines to search for a label/name, in priority order.
        Pass ONLY lines whose own selector text can legitimately carry the
        field's identity (a target locator, or a dropdown opener) — never a
        value-selection line (option/gridcell click), whose name= is the
        value the user picked, not the field.
        """
        label = None
        for src in sources:
            if src:
                label = _label_from_text(src)
                if label:
                    break
        if label:
            return _slugify(label)
        if any(src and _RICH_TEXT_HINT_RE.search(src) for src in sources):
            return "description"
        if is_file:
            self._files_seen += 1
            return "files" if self._files_seen == 1 else f"files_{self._files_seen}"
        self._unknown += 1
        key = f"field_unknown_{self._unknown}"
        shown = next((s for s in sources if s), "")
        print(f"  WARN: could not auto-detect a field name for line {shown[:70]!r} "
              f"— assigned '{key}'. Rename it in the generated base script if needed.")
        return key


class FieldEntry:
    __slots__ = ("type", "set_lines", "read_expr", "open_lines", "present_expr", "recorded_value")

    def __init__(self, type_, set_lines, read_expr, open_lines, present_expr, recorded_value=None):
        self.type = type_
        self.set_lines = set_lines            # list[str], template with {value}
        self.read_expr = read_expr            # str or None
        self.open_lines = open_lines           # list[str] or None
        self.present_expr = present_expr       # str
        self.recorded_value = recorded_value   # option text the human picked (react_select)


def _analyze_recording(recorded_source: str) -> tuple[list[str], dict, list[str], str]:
    """
    Walk the recorded script once. Returns:
      open_form_lines — nav/click lines before the first field is touched
      fields          — {field_key: FieldEntry}
      field_order     — field keys in the order they were filled
      submit_line     — the last recorded button click (Submit / Save / etc.)
    """
    keyer = FieldKeyer()
    open_form_lines: list[str] = []
    fields: dict[str, FieldEntry] = {}
    field_order: list[str] = []
    pending_opener: str | None = None
    seen_first_field = False
    last_button_click: str | None = None
    # An opener line (combobox/filter click) is only known to belong to open_form()
    # once we've seen the NEXT line and confirmed it did NOT consume the opener as
    # part of a value-selection. Buffer it instead of appending immediately, or it
    # ends up duplicated: once here, once again inside set_field/open_field.
    buffered_opener: str | None = None

    lines = recorded_source.splitlines()
    for line in lines:
        stripped = line.strip()

        if re.match(r'^\s*run\(playwright\)\s*$', line) or stripped.startswith(_SKIP_PREFIXES) \
                or stripped.startswith("page.goto(") or stripped.startswith("#") or not stripped:
            pending_opener = None
            continue

        m_fill = _ACTION_RE.match(line)
        m_option = _OPTION_CLICK_RE.match(line)
        m_files = _SET_INPUT_FILES_RE.match(line)
        m_checkbox = _CHECKBOX_RE.match(line)
        m_gridcell = _GRIDCELL_CLICK_RE.match(line)
        m_text = _TEXT_CLICK_RE.match(line) if pending_opener else None

        is_value_sel = bool(m_fill or m_option or m_files or m_text or m_checkbox or m_gridcell)

        if not is_value_sel:
            if re.search(r'get_by_role\(["\']button["\'].*\.click\(\)', stripped):
                last_button_click = stripped
            if not seen_first_field:
                # Whatever was buffered wasn't consumed by a value-selection (this
                # line proves that, since it isn't one) — it was just a plain nav
                # click, so it belongs in open_form() after all.
                if buffered_opener:
                    open_form_lines.append(_strip_stale_count_badge(buffered_opener))
                    buffered_opener = None
                if _is_potential_opener(stripped):
                    buffered_opener = stripped  # hold — may still get consumed next line
                else:
                    open_form_lines.append(_strip_stale_count_badge(stripped))
            pending_opener = stripped if _is_potential_opener(stripped) else None
            continue

        seen_first_field = True
        buffered_opener = None  # consumed by this value-selection, not part of open_form()

        if m_fill:
            target = m_fill.group("target").strip()
            method = m_fill.group("method")
            key = keyer.key_for([target])
            if key not in fields:
                if method == "fill":
                    set_lines = ['{target}.fill(str(value))'.format(target=target)]
                else:
                    set_lines = ['{target}.select_option(str(value))'.format(target=target)]
                fields[key] = FieldEntry(
                    type_="textbox" if method == "fill" else "select",
                    set_lines=set_lines,
                    read_expr=f'{target}.input_value()',
                    open_lines=None,
                    present_expr=f'{target}.count() > 0',
                )
                field_order.append(key)
            pending_opener = None

        elif m_checkbox:
            target = m_checkbox.group("target").strip()
            key = keyer.key_for([target, pending_opener])
            if key not in fields:
                fields[key] = FieldEntry(
                    type_="checkbox",
                    set_lines=[f'{target}.check()', "", f'{target}.uncheck()'],  # rendered specially below
                    read_expr=f'{target}.is_checked()',
                    open_lines=None,
                    present_expr=f'{target}.count() > 0',
                )
                field_order.append(key)
            pending_opener = None

        elif m_gridcell:
            opener_line = pending_opener
            stable_opener = None
            if opener_line:
                stable_opener = re.sub(
                    r'get_by_role\("button",\s*name=["\'][^"\']*["\']',
                    r'get_by_role("button", name=re.compile(r"hoose date", re.I)',
                    opener_line,
                )
            # Only the opener can carry the field's identity — the gridcell's own
            # name= is the selected day (a value), not the field name.
            key = keyer.key_for([opener_line])
            if key not in fields:
                opener_target = re.sub(r'\.click\(\)\s*$', '', stable_opener) if stable_opener else None
                # An unscoped `get_by_role("gridcell", name=str(value))` is a strict-mode
                # trap: MUI's calendar grid renders every visible month, so name-matching
                # a bare day number ("1") also matches "10-19", "21", "31" etc. Scope to
                # the actual calendar body (the sibling right after the header) and take
                # the first match, same fix already applied by hand elsewhere.
                set_lines = ([stable_opener] if stable_opener else []) + [
                    'day = str(value.day) if hasattr(value, "day") else str(value)',
                    'page.locator(".MuiPickersCalendarHeader-root + div").get_by_role("gridcell", name=day, exact=True).first.click()',
                ]
                fields[key] = FieldEntry(
                    type_="date",
                    set_lines=set_lines,
                    read_expr=f'({opener_target}).text_content()' if opener_target else None,
                    open_lines=[stable_opener] if stable_opener else None,
                    present_expr=f'({opener_target}).count() > 0' if opener_target else "False",
                )
                field_order.append(key)
            pending_opener = None

        elif m_files:
            target_expr = 'page.locator("input[type=\'file\']")'
            key = keyer.key_for([stripped, pending_opener], is_file=True)
            if key not in fields:
                fields[key] = FieldEntry(
                    type_="file",
                    set_lines=[f'{target_expr}.set_input_files(str(value))'],
                    read_expr=None,
                    open_lines=None,
                    present_expr=f'{target_expr}.count() > 0',
                )
                field_order.append(key)
            pending_opener = None

        else:
            # option-click / text-click — react-select / MUI combobox value selection.
            # Only the opener can carry the field's identity — this line's own name=/
            # text is the value the human picked, never the field name.
            opener_line = pending_opener
            if not opener_line:
                # Orphan option/text click with no preceding opener — recording
                # artifact (e.g. a stale listbox still open from a prior field).
                # Skip rather than emit a broken field_unknown_N with no opener.
                pending_opener = None
                continue
            key = keyer.key_for([opener_line])
            if key not in fields:
                opener_target = None
                open_lines = None
                if opener_line:
                    opener_target = re.sub(r'\.click\(\)\s*$', '', opener_line)
                    # _open_dropdown (generated template) opens with the same tour /
                    # stale-aria-hidden / retry hardening _select_option uses — a plain
                    # click+wait here made ui-visibility checks (which call open_field)
                    # time out on the exact race set_field already survives.
                    open_lines = [f'_open_dropdown(page, {opener_target})']
                if m_option:
                    # _select_option (see generated template) owns the whole
                    # open→pick→confirm-closed cycle — both the "dropdown ignored
                    # a mid-render click" and the "menu never closed, blocking
                    # everything after it" races were observed live.
                    set_lines = [f'_select_option(page, {opener_target}, value)']
                else:
                    select_line = 'page.get_by_role("listbox").get_by_text(str(value), exact=False).first.click()'
                    set_lines = (open_lines or []) + [select_line]
                fields[key] = FieldEntry(
                    type_="react_select",
                    set_lines=set_lines,
                    read_expr=f'({opener_target}).text_content() or ""' if opener_target else None,
                    open_lines=open_lines,
                    present_expr=f'({opener_target}).count() > 0' if opener_target else "False",
                    recorded_value=(m_option.group("value") if m_option else m_text.group("value")),
                )
                field_order.append(key)
            pending_opener = None

    submit_line = last_button_click or 'page.get_by_role("button", name="Submit", exact=True).click()'
    return open_form_lines, fields, field_order, submit_line


def _load_matrix_keys(flow: str, flow_type: str) -> tuple[set[str], dict[str, set[str]]]:
    """
    Every `inputs` key the scenario matrix uses for this flow_type, plus each
    key's string values across all cases — value text is what lets us bind a
    recorded MUI select whose accessible name is its CURRENT VALUE (useless
    for name matching) to the right matrix key (see _remap_to_matrix_keys).
    """
    path = MATRICES_DIR / f"scenario_matrix_{flow}.json"
    if not path.exists():
        return set(), {}
    try:
        cases = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set(), {}
    keys: set[str] = set()
    key_values: dict[str, set[str]] = {}
    for case in cases:
        if case.get("flow_type") != flow_type:
            continue
        for k, v in (case.get("inputs") or {}).items():
            keys.add(k)
            vals = v if isinstance(v, list) else [v]
            for item in vals:
                if isinstance(item, str) and item.strip():
                    key_values.setdefault(k, set()).add(item.strip())
    return keys, key_values


def _tokenize(key: str) -> set[str]:
    # Split snake_case AND camelCase ("reportDate" → {report, date}), and
    # compare plural-insensitively so a UI label like "Assignee" still matches
    # the matrix key "assignees". The plural strip is applied to both sides,
    # so words that mangle (e.g. "status" → "statu") still match themselves.
    key = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', key)
    toks = {t for t in key.lower().split("_") if t}
    return {t[:-1] if t.endswith("s") and len(t) > 3 else t for t in toks}


def _load_namespace_map(flow: str) -> dict:
    """
    test_data.namespace_map from extract.py's context JSON — maps internal/model
    names to the UI's display vocabulary (e.g. "ticket" → "case"). The matrix's
    input keys come from source/model names while a recording's selectors carry
    UI labels, so this is the generic bridge between the two vocabularies —
    without it, a field labelled "Case" in the UI can never token-match the
    matrix key "ticket_id".
    """
    path = BASE_DIR / "context" / f"context_{flow}.json"
    if not path.exists():
        return {}
    try:
        ctx = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return (ctx.get("test_data") or {}).get("namespace_map") or {}


# Generic English synonyms for field-name matching — bridges naming gaps the
# namespace_map (domain vocabulary) doesn't cover, e.g. a rich-text editor we
# key "description" vs a matrix key "notes", or "assignee" vs "staff_assigned".
_TOKEN_SYNONYMS: dict[str, set[str]] = {
    "description": {"note", "comment"},
    "note":        {"description", "comment"},
    "comment":     {"description", "note"},
    "assignee":    {"assigned", "staff"},
    "assigned":    {"assignee"},
    "staff":       {"assignee"},
    # A form's primary text field is labelled "Name" on the SR-list/ticket forms
    # but "Please specify the title…" on the appointment host-form, so the same
    # logical field records as `name` in one flow and `title` in another.
    "title":       {"name"},
    "name":        {"title"},
}


def _value_tokens(text: str) -> set[str]:
    toks = {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 1}
    return {t[:-1] if t.endswith("s") and len(t) > 3 else t for t in toks}


def _remap_to_matrix_keys(
    fields: dict, field_order: list[str], matrix_keys: set[str],
    namespace_map: dict | None = None,
    key_values: dict[str, set[str]] | None = None,
) -> tuple[dict, list[str]]:
    """
    Snap auto-detected field keys onto the matrix's real input keys, so
    execute.py's `inputs.get(key)` actually hits. Auto-detection reads a
    field's name straight off its recorded selector, which works when the
    control exposes a stable label — but some MUI dropdowns expose their
    CURRENTLY SELECTED VALUE as the accessible name instead (e.g. a Select
    that defaults to a real option rather than a "Select X" placeholder),
    so the detected key silently drifts from what the matrix calls the same
    field. Renaming here is metadata-only — field keys only ever appear as
    dict keys, never inside the generated Playwright selectors — so it can't
    corrupt any locator.
    """
    if not matrix_keys:
        return fields, field_order

    # UI-label token → internal-name tokens (from namespace_map), so a detected
    # key of "case" also carries the token "ticket" when scoring against matrix keys.
    label_aliases: dict[str, set[str]] = {}
    for internal, label in (namespace_map or {}).items():
        if not isinstance(label, str):
            continue
        for lt in _tokenize(label):
            label_aliases.setdefault(lt, set()).update(_tokenize(internal))

    claimed = {k for k in field_order if k in matrix_keys}
    remaining_matrix = matrix_keys - claimed
    renames: dict[str, str] = {}

    # Pass 1: token-overlap match for fields that got a real (non-fallback) name.
    for key in field_order:
        if key in matrix_keys or key.startswith("field_unknown_"):
            continue
        rec_val = getattr(fields.get(key), "recorded_value", None)
        if rec_val and (_tokenize(key) & _value_tokens(rec_val)):
            # The detected name shares vocabulary with the option the human
            # picked — i.e. the accessible name is the field's CURRENT VALUE
            # (a MUI Select with a default), not a label. Name tokens would
            # bind to a random key sharing one word (observed live:
            # "Time & Material" -> timeZone via "time"); let the value-based
            # pass 1.5 decide instead.
            continue
        key_tokens = _tokenize(key)
        for t in set(key_tokens):
            key_tokens |= label_aliases.get(t, set())
            key_tokens |= _TOKEN_SYNONYMS.get(t, set())
        if not key_tokens or not remaining_matrix:
            continue
        # Tie-break equal token overlaps by whether the key carries any real
        # (non-empty) value in the matrix — sanitize.py injects required-field
        # placeholders like ticket_id="" that would otherwise tie with the key
        # the cases actually use (e.g. 'ticket' vs 'ticket_id').
        scored = sorted(
            ((len(key_tokens & _tokenize(mk)),
              1 if (key_values or {}).get(mk) else 0,
              mk) for mk in remaining_matrix),
            reverse=True,
        )
        if scored[0][0] > 0 and (len(scored) == 1 or scored[0][:2] > scored[1][:2]):
            match = scored[0][2]
            renames[key] = match
            remaining_matrix.discard(match)

    # Pass 1.5: value-based match. A MUI Select with a default exposes its
    # CURRENT VALUE as its accessible name ("Time & Material"), which can never
    # token-match a matrix key like job_type. But the OPTION the human picked
    # while recording is that field's vocabulary — score it against each
    # remaining matrix key's input values across the matrix and take a unique
    # best overlap ("Fixed Fee & Material" ⇒ job_type, "Internal Only" ⇒ visibility).
    if key_values:
        for key in field_order:
            if key in renames or key in matrix_keys or not remaining_matrix:
                continue
            rec_val = getattr(fields.get(key), "recorded_value", None)
            if not rec_val:
                continue
            rv_tokens = _value_tokens(rec_val)
            scored = sorted(
                ((len(rv_tokens & set().union(*(map(_value_tokens, key_values.get(mk, {""}))))), mk)
                 for mk in remaining_matrix),
                reverse=True,
            )
            if scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
                match = scored[0][1]
                renames[key] = match
                remaining_matrix.discard(match)

    # Pass 2: one nameless field, one unclaimed matrix key — unambiguous even
    # with zero label information (e.g. a control with no name= at all).
    remaining_unknown = [k for k in field_order if k.startswith("field_unknown_") and k not in renames]
    if len(remaining_unknown) == 1 and len(remaining_matrix) == 1:
        renames[remaining_unknown[0]] = next(iter(remaining_matrix))

    if not renames:
        return fields, field_order

    for old, new in renames.items():
        print(f"  Matched field '{old}' -> matrix key '{new}'")

    new_fields, new_order = {}, []
    for key in field_order:
        new_key = renames.get(key, key)
        new_fields[new_key] = fields[key]
        new_order.append(new_key)
    return new_fields, new_order


def _indent(lines: list[str], n: int = 4) -> str:
    """
    Pad every physical line by n spaces — not just the first line of each
    item. Items may themselves be multi-line blocks (e.g. a try/except
    wrapper) written with 0-based *relative* indentation internally; padding
    every line uniformly means such blocks compose correctly no matter how
    deeply this gets nested (e.g. _indent(_indent(inner, 4), 8) shifts
    `inner` by a flat +8, as expected) instead of only the block's opening
    line receiving the requested indentation.
    """
    pad = " " * n
    physical = []
    for item in lines:
        if not item.strip():
            continue
        for sub in item.split("\n"):
            physical.append(f"{pad}{sub}" if sub.strip() else sub)
    return "\n".join(physical) or f"{pad}pass"


def _render_field_function(name: str, fields: dict, body_for: callable, default: str) -> str:
    branches = []
    for i, (key, entry) in enumerate(fields.items()):
        kw = "if" if i == 0 else "elif"
        body = body_for(key, entry)
        if body is None:
            continue
        branches.append(f'    {kw} field == "{key}":\n{_indent(body, 8)}')
    if not branches:
        return f'def {name}(page: Page, field: str, *args):\n    raise ValueError(f"No fields recorded for {{field}}")\n'
    branches_src = "\n".join(branches)
    return (
        f'{branches_src}\n'
        f'    else:\n'
        f'        raise ValueError(f"Unknown field: {{field}}")\n'
    )


# Matches a recorded ticket-subject click in BOTH forms it can reach
# _genericize_first_ticket_click in: raw codegen (`page.get_by_text(...).click()`)
# and after _settle_after_navigation_clicks has rewrapped it
# (`_click_through_tour(page, page.get_by_text(...))`). The genericize pass runs
# after the settle pass, so matching only the raw form silently disabled the
# retry-over-tickets block in every build.
_CASES_LIST_TICKET_CLICK_RE = re.compile(
    r'^(?:page\.get_by_text\(("|\').*?\1\)\.click\(\)'
    r'|_click_through_tour\(page, page\.get_by_text\(("|\').*?\2\)\))$'
)


def _genericize_first_ticket_click(open_form_lines: list[str], start_url_path: str, full_start_url: str) -> list[str]:
    """
    A /cases recording clicks one SPECIFIC ticket's subject text. Played back
    naively that breaks two ways (both observed live): the board renders as
    skeleton loaders for many seconds so the subject isn't on screen yet, and
    the ticket may sit on a later page/column so it never appears at all.

    Earlier revisions replaced the click with "whichever ticket card renders
    first" via a get_by_role("heading", name="View .* Details") retry loop —
    verified live to match NOTHING on this app's /cases (0 headings), so that
    approach is gone. Instead, drive the page's own search box: fill it with
    the recorded subject, then click the (now unique, top-of-list) match.
    Verified live: search narrows to exactly one card and the click lands on
    the ticket detail page. The recorded ticket is also what the matrix's
    expected results reference, so pinning to it is correct, not a limitation
    — if the ticket ever disappears from the DB, re-record.
    """
    if "/cases" not in start_url_path:
        return open_form_lines
    idx = next((i for i, l in enumerate(open_form_lines) if _CASES_LIST_TICKET_CLICK_RE.match(l)), None)
    if idx is None:
        return open_form_lines
    line = open_form_lines[idx]
    m = re.search(r'get_by_text\(("|\')(?P<subject>.*?)\1\)', line)
    if not m:
        return open_form_lines
    subject = m.group("subject")
    search_block = [
        f'page.get_by_placeholder("Search...", exact=True).first.fill({subject!r})',
        f'_click_through_tour(page, page.get_by_text({subject!r}).first)',
        '_dismiss_tour(page)  # navigating into the ticket can re-trigger the tour',
    ]
    return open_form_lines[:idx] + search_block + open_form_lines[idx + 1:]


def _settle_after_navigation_clicks(open_form_lines: list[str]) -> list[str]:
    """
    Every click in open_form() is, by construction, a navigation step (tab
    switch, card click, drawer-opening button) rather than a field value
    selection — those are handled separately in set_field(). Each one can
    trigger an async data fetch for whatever renders next (e.g. clicking a
    ticket's "Service Reports" tab fetches that tab's list before the
    "Service Report" create button exists), so the next locator in line can
    still be racing the fetch even though the click itself succeeded. A
    the initial `wait_for_load_state("domcontentloaded")` after goto() only
    covers the initial page load, not subsequent in-app navigation. Insert a
    bounded `networkidle` wait after every click here too, best-effort (some
    navigations have no follow-up fetch, or the app keeps a persistent
    connection open that never goes idle — the 5s timeout + try/except keeps
    that from hanging the test; see open_form_body above for why the very
    first wait uses domcontentloaded instead of networkidle).

    Every click is also routed through `_click_through_tour()` instead of a
    bare `.click()`. iSERV's Shepherd product tour (TourContext.js) auto-
    starts with highly variable timing — an async getCompletedTours() call,
    a skeleton-loader poll, a waitForElement poll, then a 300ms setTimeout —
    and its first step ("...-welcome") has no `attachTo` target, so Shepherd's
    modal overlay blocks clicks across the ENTIRE page (not just around its
    target) until dismissed. A one-shot `_dismiss_tour()` pre-wait loses this
    race often enough to be the actual cause of "Locator.click: Timeout
    15000ms exceeded" failures seen in practice on the very first click of a
    fresh session — retrying the click and the dismissal together across the
    click's own timeout is the only way that doesn't depend on guessing the
    tour's startup latency.
    """
    out: list[str] = []
    settle = (
        'try:\n'
        '    page.wait_for_load_state("networkidle", timeout=5000)\n'
        'except Exception:\n'
        '    pass'
    )
    for line in open_form_lines:
        stripped = line.rstrip()
        if stripped.endswith(".click()"):
            locator_expr = stripped[: -len(".click()")]
            out.append(f'_click_through_tour(page, {locator_expr})')
            out.append(settle)
        elif stripped.startswith("_click_menu_item("):
            # already retry/tour-aware internally — just add the settle wait
            out.append(stripped)
            out.append(settle)
        else:
            out.append(line)
    return out


def _build_base_script(flow: str, flow_type: str, rec: dict, recorded_source: str) -> str:
    open_form_lines, fields, field_order, submit_line = _analyze_recording(recorded_source)
    matrix_keys, key_values = _load_matrix_keys(flow, flow_type)
    fields, field_order = _remap_to_matrix_keys(
        fields, field_order, matrix_keys, _load_namespace_map(flow), key_values
    )
    start_url = f"{FRONTEND_URL}{rec.get('start_url', '')}"
    open_form_lines = _settle_after_navigation_clicks(open_form_lines)
    open_form_lines = _genericize_first_ticket_click(open_form_lines, rec.get("start_url", ""), start_url)
    recorded_filename = f"{base_name(flow, flow_type)}_recorded.py"

    open_form_body = _indent(
        [
            'page.goto("' + start_url + '")',
            'page.wait_for_load_state("domcontentloaded")',
            '_dismiss_tour(page)',
        ]
        + [l for l in open_form_lines]
    )

    def set_body(key, entry):
        if entry.type == "checkbox":
            target = entry.present_expr.rsplit(".count()", 1)[0]
            return [f'if value:', f'    {target}.check()', 'else:', f'    {target}.uncheck()']
        return entry.set_lines

    def read_body(key, entry):
        if entry.read_expr is None:
            return [f'raise ValueError("Field \'{key}\' has no readable value (type={entry.type})")']
        return [f'return {entry.read_expr}']

    def open_body(key, entry):
        if not entry.open_lines:
            return [f'raise ValueError("Field \'{key}\' has no dropdown to open (type={entry.type})")']
        return entry.open_lines

    def present_body(key, entry):
        return [f'return {entry.present_expr}']

    set_field_src = _render_field_function("set_field", fields, set_body, "raise")
    read_field_src = _render_field_function("read_field", fields, read_body, "raise")
    open_field_src = _render_field_function("open_field", fields, open_body, "raise")
    present_field_src = _render_field_function("is_field_present", fields, present_body, "raise")

    field_order_py = json.dumps(field_order)
    field_types_py = json.dumps({k: v.type for k, v in fields.items()}, indent=4)

    submit_locator_expr = (
        submit_line[: -len(".click()")] if submit_line.endswith(".click()") else None
    )
    submit_click_call = (
        f"_click_through_tour(page, {submit_locator_expr}, timeout_ms=5000)"
        if submit_locator_expr is not None
        else submit_line.replace(".click()", ".click(timeout=5000)")
    )
    submit_try = (
        "    _dismiss_tour(page)\n"
        "    try:\n"
        f"        {submit_click_call}\n"
        "    except Exception:\n"
        "        pass  # submit may be disabled (e.g. invalid input rejected by the form)\n"
    )

    return f'''\
import re
import time
from playwright.sync_api import Page, expect

FIELD_ORDER = {field_order_py}
FIELD_TYPES = {field_types_py}


def _dismiss_tour(page: Page, wait_ms: int = 1800):
    """
    iSERV's product-tour overlay (Shepherd.js — see TourContext.js) auto-starts
    on a route's first visit per logged-in user, but the start is ASYNC (up to
    ~1.5s after page load: skeleton-loader polling + a 300ms delay before
    tour.start()). Its useModalOverlay covers the page and can intercept the
    click on the very button this test is about to click — but only when the
    tour's start lands inside the test's click window, which is why the
    failure is intermittent rather than every run. Poll for the tour's cancel
    icon for a bit instead of checking once, and dismiss it if it shows up.
    Test sessions are fresh logins each run (see record.py/_setup_auth_storage
    and execute.py/_login), so every route's tour is still "not completed"
    every time — this can fire on every navigation, not just the first.
    """
    deadline = time.time() + (wait_ms / 1000)
    while time.time() < deadline:
        try:
            cancel_icon = page.locator(".shepherd-cancel-icon")
            if cancel_icon.count() > 0:
                cancel_icon.first.click(timeout=1000)
                page.wait_for_timeout(300)
                return
        except Exception:
            pass
        page.wait_for_timeout(150)


def _clear_stale_aria_hidden(page: Page):
    """
    MUI occasionally loses the modal mount/unmount race at page boot and leaves
    aria-hidden="true" on #root with no modal actually on screen. The page then
    LOOKS perfectly normal, but every role-based locator on it resolves to
    nothing (the ARIA tree excludes aria-hidden subtrees) — observed live as a
    visible, clickable button that get_by_role() could not find for 90s.
    Repair only when clearly stale: a real open drawer/menu is portaled to
    <body> outside #root, so if any such overlay is visible we leave the
    attribute alone. Visibility must be rect-based — MUI overlays are
    position:fixed, where offsetParent is ALWAYS null.
    """
    try:
        page.evaluate(
            "() => {{"
            "  const root = document.getElementById('root');"
            "  if (!root || root.getAttribute('aria-hidden') !== 'true') return;"
            "  const vis = e => {{ const r = e.getBoundingClientRect();"
            "    return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; }};"
            "  const overlayVisible = [...document.querySelectorAll('.MuiModal-root, .MuiDrawer-root, .MuiPopover-root')]"
            "    .some(m => !root.contains(m) && vis(m));"
            "  if (!overlayVisible) root.removeAttribute('aria-hidden');"
            "}}"
        )
    except Exception:
        pass


def _debug_aria_state(page: Page, label: str, locator=None):
    """One-line page-state dump printed when a locator has been failing for a
    while — shows whether the target exists in DOM vs the accessibility tree,
    and what modal/aria state is active. Read it from execute.py's output."""
    try:
        state = page.evaluate(
            "() => {{"
            "  const root = document.getElementById('root');"
            "  const vis = e => {{ const r = e.getBoundingClientRect();"
            "    return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; }};"
            "  return {{"
            "    root_ah: root ? root.getAttribute('aria-hidden') : null,"
            "    drawers: [...document.querySelectorAll('.MuiDrawer-root')].map(d =>"
            "      (vis(d) ? 'vis' : 'hid') + '/ah=' + d.getAttribute('aria-hidden')),"
            "    menus_open: [...document.querySelectorAll(\\"[role='listbox'],.MuiMenu-root\\")].filter(vis).length,"
            "    cb_dom: document.querySelectorAll(\\"[role='combobox']\\").length,"
            "    cb_in_ah: [...document.querySelectorAll(\\"[role='combobox']\\")]"
            "      .filter(c => c.closest(\\"[aria-hidden='true']\\")).length,"
            "  }};"
            "}}"
        )
        role_count = None
        if locator is not None:
            try:
                role_count = locator.count()
            except Exception:
                role_count = "?"
        print(f"    [debug {{label}}] locator_count={{role_count}} state={{state}}")
    except Exception as exc:
        print(f"    [debug {{label}}] state dump failed: {{str(exc)[:80]}}")


def _click_menu_item(page: Page, label: str, timeout_ms: int = 30000):
    """
    Click a label+live-count menu/tab item (e.g. a ticket's "Service Reports65").
    The recording captures label+count concatenated, which goes stale when the
    count changes; matching the bare label instead hits the app's left sidebar
    nav item with the same name (observed live: the click navigated to the main
    module page, silently testing the wrong entry point). So: match
    ^label<digits?>$, prefer a digit-suffixed candidate (only the in-page menu
    item carries a count), and fall back to the LAST bare match (the sidebar
    renders before page content in DOM order).
    """
    pattern = re.compile(r"^" + re.escape(label) + r"\d*$")
    deadline = time.time() + (timeout_ms / 1000)
    last_exc = None
    while time.time() < deadline:
        try:
            candidates = page.get_by_text(pattern)
            best = None
            for i in range(candidates.count()):
                txt = (candidates.nth(i).text_content() or "").strip()
                if txt != label:   # has a count suffix -> the in-page menu item
                    best = candidates.nth(i)
                    break
            if best is None and candidates.count():
                best = candidates.last
            if best is None:
                raise RuntimeError("menu item '" + label + "' not found on page")
            best.click(timeout=2000)
            return
        except Exception as exc:
            last_exc = exc
            _dismiss_tour(page, wait_ms=400)
            _clear_stale_aria_hidden(page)
            page.wait_for_timeout(300)
    raise last_exc


def _force_click(locator):
    """
    Last-resort DOM-level click for a locator that RESOLVES but whose normal
    click keeps failing actionability — observed live as a visible, enabled,
    uncovered button failing 'stability' for 90s on a page whose content
    live-updates over the websocket. Dispatches mousedown/mouseup/click so
    MUI controls that react on mousedown (Select, Autocomplete) respond too.
    Bypasses Playwright's covered/stable checks — only call after normal
    clicking has already failed for a long time.
    """
    locator.first.evaluate(
        "el => ['mousedown', 'mouseup', 'click'].forEach(t =>"
        " el.dispatchEvent(new MouseEvent(t, {{bubbles: true, cancelable: true}})))"
    )


def _click_through_tour(page: Page, locator, timeout_ms: int = 90000):
    # Default sized for this dev stack's worst observed page-data latency
    # (30-45s+ under load), not for a responsive app — navigation clicks in
    # open_form() wait on whole-page data fetches, unlike field actions.
    """
    Click `locator`, dismissing the Shepherd tour if it appears mid-wait.
    TourContext.js's auto-start chain (an async getCompletedTours() call, a
    skeleton-loader poll, a waitForElement poll, then a 300ms setTimeout) can
    start the tour well past any fixed pre-wait, and the tour's first step
    has no `attachTo` target — so its modal overlay blocks the ENTIRE page,
    not just its own target, until dismissed. A single dismiss-then-click can
    still lose this race; retry both across the click's own timeout instead
    of gambling on one fixed wait length. Each retry also repairs the stale
    aria-hidden state that blinds role-based locators (see
    _clear_stale_aria_hidden).

    Success detection: a click attempt can time out AFTER its click actually
    dispatched (observed live: the drawer opened, then every retry failed with
    "MuiDrawer subtree intercepts pointer events" — the retries were blocked
    by the very drawer the first click opened). So if a drawer/modal overlay
    appears that was not on screen before the first attempt, treat the click
    as landed instead of retrying against a covered button.
    """
    def _overlay_open() -> bool:
        try:
            return bool(page.evaluate(
                "() => {{"
                "  const root = document.getElementById('root');"
                "  const vis = e => {{ const r = e.getBoundingClientRect();"
                "    return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; }};"
                "  return [...document.querySelectorAll('.MuiDrawer-root, .MuiModal-root')]"
                "    .some(m => (!root || !root.contains(m)) && vis(m));"
                "}}"
            ))
        except Exception:
            return False

    had_overlay = _overlay_open()
    deadline = time.time() + (timeout_ms / 1000)
    start = time.time()
    last_exc = None
    debugged = False
    forced = False
    while time.time() < deadline:
        try:
            locator.click(timeout=10000)
            return
        except Exception as exc:
            last_exc = exc
            if not had_overlay and _overlay_open():
                return
            if not debugged and time.time() - start > 15:
                _debug_aria_state(page, "nav-click failing", locator)
                debugged = True
            if not forced and time.time() - start > (timeout_ms / 2000):
                try:
                    _force_click(locator)
                    page.wait_for_timeout(700)
                    if not had_overlay and _overlay_open():
                        return
                except Exception:
                    pass
                forced = True
            _dismiss_tour(page, wait_ms=500)
            _clear_stale_aria_hidden(page)
    raise last_exc


def _wait_form_ready(page: Page, timeout_ms: int = 90000):
    """
    The form's fields render only after its options fetch resolves — measured
    anywhere from 2s to 40s+ on the dev stack, run to run, for the same page.
    Gate on the first recorded field actually existing before any set_field
    call, instead of sizing every per-action timeout for the worst case.
    """
    if not FIELD_ORDER:
        return
    deadline = time.time() + (timeout_ms / 1000)
    while True:
        try:
            if is_field_present(page, FIELD_ORDER[0]):
                return
        except Exception:
            pass
        if time.time() > deadline:
            raise AssertionError(
                "form did not render field '" + FIELD_ORDER[0] + "' within "
                + str(timeout_ms) + "ms"
            )
        _clear_stale_aria_hidden(page)
        page.wait_for_timeout(500)


def _open_dropdown(page: Page, opener, timeout_ms: int = 60000):
    """
    Open a dropdown (without selecting) with the same hardening _select_option uses —
    dismiss the tour, clear a stale aria-hidden, retry the click until the option list is
    actually visible. Used by open_field()/is-interactable checks, which a plain
    click+wait made time out on the same MOUSEDOWN/refetch race set_field survives.
    """
    options = page.locator("[role='option']")
    deadline = time.time() + (timeout_ms / 1000)
    while True:
        try:
            if not options.first.is_visible():
                opener.click(timeout=5000)
                options.first.wait_for(state="visible", timeout=5000)
            return
        except Exception:
            if options.first.is_visible():
                return
            if time.time() > deadline:
                raise
            _dismiss_tour(page, wait_ms=500)
            _clear_stale_aria_hidden(page)


def _close_open_menu(page: Page):
    """
    Close an open dropdown menu WITHOUT stranding the form. A bare Escape can aria-hide the
    very drawer/dialog we're working inside (observed live on a host-form drawer: after Escape
    the whole MuiDrawer got aria-hidden, so every later field became unreachable). After the
    Escape, strip aria-hidden back off any VISIBLE drawer/dialog that still holds interactable
    controls — never a real background overlay, which has no such controls on screen.
    """
    page.keyboard.press("Escape")
    try:
        page.evaluate(
            "() => {{"
            "  const vis = e => {{ const r = e.getBoundingClientRect();"
            "    return r.width > 0 && r.height > 0; }};"
            "  document.querySelectorAll('.MuiDrawer-root, .MuiDialog-root').forEach(d => {{"
            "    if (d.getAttribute('aria-hidden') === 'true' && vis(d)"
            "        && d.querySelector('input, textarea, button'))"
            "      d.removeAttribute('aria-hidden');"
            "  }});"
            "}}"
        )
    except Exception:
        pass


def _select_option(page: Page, opener, value, timeout_ms: int = 60000):
    """
    Open a dropdown and pick an option, robust to two races observed live:
      1. the dropdown ignoring a click that landed mid-render — re-open and
         retry until the option list is actually visible;
      2. the option click getting swallowed — the drawer refetches its options
         while the menu is open, React re-creates the option nodes, and a click
         dispatched to the stale node reaches no handler. The menu then stays
         open; since MUI renders it as a modal that marks the page behind it
         aria-hidden, every later role-based locator resolves to nothing and
         its backdrop intercepts every later click.
    Distinguish "click swallowed" (option not aria-selected — click it again)
    from "selected but menu stays open by design" (multi-select — close it via
    _close_open_menu, which repairs the drawer Escape can wrongly aria-hide).
    """
    options = page.locator("[role='option']")
    deadline = time.time() + (timeout_ms / 1000)
    start = time.time()
    debugged = False
    while True:
        try:
            if not options.first.is_visible():
                opener.click(timeout=5000)
                options.first.wait_for(state="visible", timeout=5000)
            break
        except Exception:
            # The click can dispatch and still raise: MUI opens on MOUSEDOWN,
            # the option fetch re-renders the control, the node detaches
            # mid-click, and Playwright's internal retry then can't re-resolve
            # the opener (the now-open menu aria-hides the drawer behind it).
            # If the option list is on screen, the click did its job — anything
            # else the click call reported is a false negative. Without this
            # check the loop re-clicks an unresolvable opener until deadline.
            if options.first.is_visible():
                break
            if time.time() > deadline:
                raise
            if not debugged and time.time() - start > 15:
                _debug_aria_state(page, "dropdown-open failing", opener)
                debugged = True
                try:
                    _force_click(opener)   # see _force_click — stability flake fallback
                    options.first.wait_for(state="visible", timeout=5000)
                    break
                except Exception:
                    pass
    option = page.get_by_role("option", name=str(value), exact=False).first
    clicked = False
    typed = False
    while True:
        if not options.first.is_visible():
            if clicked:
                return
            # menu vanished before we picked anything — reopen and retry
        try:
            if not options.first.is_visible():
                opener.click(timeout=5000)
                options.first.wait_for(state="visible", timeout=5000)
            if not clicked and not typed and option.count() == 0:
                # The list is open but the target option isn't rendered —
                # role-scoped pickers (e.g. an engineer's Case list) can order/
                # filter differently, and long lists may need the user to type
                # before the wanted row exists. Autocomplete openers are real
                # <input>s, so type the value's leading token to filter.
                typed = True
                try:
                    if (opener.first.evaluate("el => el.tagName") or "") == "INPUT":
                        opener.first.fill(str(value).split(" ")[0][:20])
                        option.wait_for(state="visible", timeout=5000)
                except Exception:
                    pass
            if clicked and option.get_attribute("aria-selected", timeout=1000) == "true":
                _close_open_menu(page)
            else:
                option.click(timeout=5000)
                clicked = True
            options.first.wait_for(state="hidden", timeout=3000)
            return
        except Exception:
            if time.time() > deadline:
                raise
        page.wait_for_timeout(200)


def open_form(page: Page):
    """Navigate to the flow's start URL and open the form. Recorded from
    {recorded_filename} — edit selectors here if the
    navigation path changes."""
{open_form_body}


def set_field(page: Page, field: str, value):
    """Generic field setter — dispatches on FIELD_TYPES."""
{set_field_src}

def read_field(page: Page, field: str):
    """Generic field getter — reads the field's current displayed value."""
{read_field_src}

def open_field(page: Page, field: str):
    """Open a dropdown without selecting a value (for option-list inspection)."""
{open_field_src}

def is_field_present(page: Page, field: str) -> bool:
    """DOM existence check — used for role-based field visibility tests."""
{present_field_src}

def run_test(page: Page, inputs: dict, expected_outcome: str, expected_message: str):
    """
    Execute one {flow}/{flow_type} scenario.

    inputs keys : {", ".join(field_order) or "(none detected)"}
    expected_outcome : "pass" | "fail"
    expected_message : exact visible text expected on failure, or None

    Fields are set in FIELD_ORDER (the order they were recorded) so cascade
    prerequisites (e.g. customer before equipment) are respected. Assertions
    are injected by execute.py — do not add them here.
    """
    open_form(page)
    _wait_form_ready(page)
    for field in FIELD_ORDER:
        value = inputs.get(field)
        if value is not None:
            set_field(page, field, value)

{submit_try}'''


def build_one(flow: str, flow_type: str):
    rec = find_recording(flow, flow_type)
    stem = base_name(flow, flow_type)
    recorded_path = BASE_TESTS_DIR / f"{stem}_recorded.py"
    output_path = BASE_TESTS_DIR / f"{stem}.py"

    if not recorded_path.exists():
        print(f"ERROR: {recorded_path} not found — run "
              f"`python approach3/record.py --record {flow} {flow_type}` first.")
        sys.exit(1)

    recorded_source = recorded_path.read_text(encoding="utf-8")
    script = _build_base_script(flow, flow_type, rec, recorded_source)
    output_path.write_text(script, encoding="utf-8")

    print(f"  Base script → {output_path}")
    print(f"  Review FIELD_ORDER / FIELD_TYPES and any field_unknown_N warnings above.")


def build_all(flow: str):
    recs = load_recordings(flow)
    built = 0
    for rec in recs:
        flow_type = rec["flow_type"]
        recorded_path = BASE_TESTS_DIR / f"{base_name(flow, flow_type)}_recorded.py"
        if recorded_path.exists():
            build_one(flow, flow_type)
            built += 1
        else:
            print(f"  SKIP {flow_type} — not recorded yet "
                  f"(run --record {flow} {flow_type} first)")
    print(f"\n  Built {built} base script(s).")


# ── CLI ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("-- Record + Build UI Flow Base Scripts --------------------------")
    args = sys.argv[1:]
    force = "--force" in args
    args = [a for a in args if a != "--force"]

    if "--record" in args:
        idx = args.index("--record")
        if idx + 2 >= len(args):
            print("ERROR: --record requires <flow> <flow_type>")
            sys.exit(1)
        record_one(args[idx + 1], args[idx + 2], force=force)

    elif "--record-all" in args:
        idx = args.index("--record-all")
        if idx + 1 >= len(args):
            print("ERROR: --record-all requires <flow>")
            sys.exit(1)
        record_all(args[idx + 1], force=force)

    elif "--build-all" in args:
        idx = args.index("--build-all")
        if idx + 1 >= len(args):
            print("ERROR: --build-all requires <flow>")
            sys.exit(1)
        build_all(args[idx + 1])

    elif "--build" in args:
        idx = args.index("--build")
        if idx + 2 >= len(args):
            print("ERROR: --build requires <flow> <flow_type>")
            sys.exit(1)
        build_one(args[idx + 1], args[idx + 2])

    else:
        print("Usage:")
        print("  python approach3/record.py --record      <flow> <flow_type>   # Step 1: interactive recording")
        print("  python approach3/record.py --record-all  <flow>               # Step 1: record every flow_type needed")
        print("  python approach3/record.py --build        <flow> <flow_type>  # Step 2: build base script")
        print("  python approach3/record.py --build-all    <flow>               # Step 2: build all recorded flow_types")
        print("  Re-running --record or --record-all overwrites the existing recorded file.")
        print(f"\nSee matrices/recordings_needed_{{flow}}.md for available flow_types.")
