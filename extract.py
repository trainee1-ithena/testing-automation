"""
extract.py

Generic context extractor for the Playwright testing pipeline.
Reads a flow manifest + test_fixtures.json → writes context_{flow}.json.

Usage:
  python testGen/extract.py --flow agent_create_ticket
  python testGen/extract.py --flow customer_create_ticket
  python testGen/extract.py --flow post_creation_visibility
  python testGen/extract.py --flow agent_create_ticket --out testGen/my_context.json

Manifest format: testGen/manifests/{flow_id}.json
Fixtures:        testGen/test_fixtures.json
Output:          testGen/context_{flow_id}.json  (or --out path)
"""

import argparse
import csv
import json
import os
import re
import sys
import datetime as _dt
from datetime import datetime
from pathlib import Path

PROJECT_ROOT   = Path(__file__).parent.parent
TESTGEN_DIR    = Path(__file__).parent
MANIFESTS_DIR  = TESTGEN_DIR / "manifests"
SERVER_ENV     = PROJECT_ROOT / "iserv_server" / ".env"
TESTGEN_ENV    = TESTGEN_DIR / ".env"

# ── Read-mode constants ────────────────────────────────────────────────────────
LARGE_FILE_THRESHOLD   = 300   # lines — "auto" mode threshold
CONTROLLER_HEADER_LINES = 30   # import block prepended before the function slice
SLICE_LINE_LIMIT       = 150   # lines read from slice_target onward


# ── Low-level file helpers (ported from stage1_extract.py) ────────────────────

def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [f"# ERROR: could not read {path}"]


def _parse_loc(loc: str) -> int:
    """'L359' → 359"""
    try:
        return int(re.sub(r"[^0-9]", "", loc.split(":")[0])) or 1
    except (ValueError, AttributeError):
        return 1


def _slice_source(lines: list[str], start_line: int, limit: int) -> str:
    idx = max(0, start_line - 1)
    return "\n".join(lines[idx : idx + limit])


def _find_function_line(lines: list[str], func_name: str) -> int:
    """
    Return 1-based line number for the slice target.
    Primary: function/const declaration pattern (function foo( | const foo =).
    Fallback: first line containing func_name as a substring.
    Falls back to line 1 with a warning if neither matches.
    """
    pattern = re.compile(
        rf"(?:^|\s)(?:async\s+)?(?:function\s+{re.escape(func_name)}\s*\(|"
        rf"(?:const|let|var)\s+{re.escape(func_name)}\s*=)"
    )
    for i, line in enumerate(lines, start=1):
        if pattern.search(line):
            return i
    # Fallback: any line containing the target as a literal substring
    for i, line in enumerate(lines, start=1):
        if func_name in line:
            return i
    print(f"  [extract] WARNING: slice_target '{func_name}' not found — slicing from line 1",
          file=sys.stderr)
    return 1


# ── Read a single file entry according to its read_mode ───────────────────────

def _read_entry(entry: dict | str, section_label: str) -> tuple[str, str]:
    """
    entry is either:
      - a plain string path  → read_mode defaults to "auto"
      - a dict with keys: path, read_mode, [slice_target]

    Returns (filename, content).
    """
    if isinstance(entry, str):
        path_str  = entry
        read_mode = "auto"
        slice_target = None
    else:
        path_str     = entry["path"]
        read_mode    = entry.get("read_mode", "auto")
        slice_target = entry.get("slice_target")

    path  = PROJECT_ROOT / path_str
    fname = Path(path_str).name

    if not path.exists():
        print(f"  [extract] WARNING: file not found: {path_str}", file=sys.stderr)
        return fname, f"# ERROR: file not found: {path_str}"

    lines = _read_lines(path)

    if read_mode == "full":
        content = "\n".join(lines)

    elif read_mode == "slice":
        if not slice_target:
            print(f"  [extract] WARNING: read_mode=slice but no slice_target for {fname} "
                  f"— falling back to auto", file=sys.stderr)
            content = _auto_read(lines, fname)
        else:
            start   = _find_function_line(lines, slice_target)
            header  = "\n".join(lines[:CONTROLLER_HEADER_LINES])
            func    = _slice_source(lines, max(1, start - 2), SLICE_LINE_LIMIT)
            content = header + f"\n// [...] (sliced at '{slice_target}', line {start})\n" + func

    else:  # "auto"
        content = _auto_read(lines, fname)

    return fname, content


def _auto_read(lines: list[str], fname: str) -> str:
    if len(lines) <= LARGE_FILE_THRESHOLD:
        return "\n".join(lines)
    # Large file with no specific target: header + beginning slice
    header = "\n".join(lines[:CONTROLLER_HEADER_LINES])
    body   = _slice_source(lines, CONTROLLER_HEADER_LINES + 1, SLICE_LINE_LIMIT)
    return header + f"\n// [...] ({fname} truncated — {len(lines)} lines total)\n" + body


# ── Build the frontend / backend / models sections from manifest["files"] ──────

def _build_sections(files_spec: dict) -> tuple[dict, dict, dict, list[str]]:
    """
    Returns (frontend, backend, models, files_manifest).
    files_spec mirrors the manifest "files" key.
    """
    frontend: dict = {}
    backend:  dict = {}
    models:   dict = {}
    manifest_names: list[str] = []

    # ── Frontend ──────────────────────────────────────────────────────────────
    fe_spec = files_spec.get("frontend", {})
    for section, entries in fe_spec.items():
        if not isinstance(entries, list):
            entries = [entries]
        frontend[section] = {}
        for entry in entries:
            fname, content = _read_entry(entry, f"frontend.{section}")
            frontend[section][fname] = content
            manifest_names.append(fname)

    # ── Backend ───────────────────────────────────────────────────────────────
    be_spec = files_spec.get("backend", {})
    for section, entries in be_spec.items():
        if not isinstance(entries, list):
            entries = [entries]
        backend[section] = {}
        for entry in entries:
            fname, content = _read_entry(entry, f"backend.{section}")
            backend[section][fname] = content
            manifest_names.append(fname)

    # ── Models ────────────────────────────────────────────────────────────────
    for entry in files_spec.get("models", []):
        fname, content = _read_entry(entry, "models")
        models[fname] = content
        manifest_names.append(fname)

    return frontend, backend, models, manifest_names


# ── DB ────────────────────────────────────────────────────────────────────────

def _load_server_env() -> dict:
    """Parse iserv_server/.env into a plain dict (no dotenv dependency needed here)."""
    env: dict = {}
    if not SERVER_ENV.exists():
        return env
    for line in SERVER_ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def _load_testgen_env() -> dict:
    """Parse approach3/.env into a plain dict."""
    env: dict = {}
    if not TESTGEN_ENV.exists():
        return env
    for line in TESTGEN_ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def _load_env_example_keys() -> list[str]:
    """Return just the key names from approach3/.env.example — no values."""
    keys: list[str] = []
    example_path = TESTGEN_DIR / ".env.example"
    if not example_path.exists():
        return keys
    for line in example_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.partition("=")[0].strip()
        if key:
            keys.append(key)
    return keys


def _decrypt_db_password(env: dict) -> str:
    """Decrypt AES-256-CBC password using the same algorithm as iserv_server config.js."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend

        key = bytes.fromhex(env["ENCRYPTION_KEY"])
        iv  = bytes.fromhex(env["DB_PASS_IV"])
        ct  = bytes.fromhex(env["DB_PASS_ENCRYPTED"])  # hex-encoded, matching config.js

        cipher    = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        padded    = decryptor.update(ct) + decryptor.finalize()

        # PKCS7 unpadding
        pad_len = padded[-1]
        return padded[:-pad_len].decode("utf-8")
    except Exception as exc:
        print(f"  [extract] WARNING: password decryption failed ({exc}) — DB query skipped",
              file=sys.stderr)
        return ""


def _json_default(obj):
    """Handle MySQL types that json.dumps can't serialise by default."""
    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.isoformat()
    try:
        import decimal
        if isinstance(obj, decimal.Decimal):
            return float(obj)
    except ImportError:
        pass
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


QUERY_PLANS_DIR = TESTGEN_DIR / "query_plans"
TOKEN_LOG        = TESTGEN_DIR / "token_usage.csv"

MODEL_COST_RATES = {
    "gpt-4o":       {"prompt": 0.0025,  "completion": 0.01},
    "gpt-4o-mini":  {"prompt": 0.00015, "completion": 0.0006},
    "gpt-4.1":      {"prompt": 0.002,   "completion": 0.008},
    "gpt-4.1-mini": {"prompt": 0.0004,  "completion": 0.0016},
    "gpt-4-turbo":  {"prompt": 0.01,    "completion": 0.03},
    "gpt-4":        {"prompt": 0.03,    "completion": 0.06},
}


def get_cost_rates(model: str) -> dict:
    prompt_rate     = os.getenv("OPENAI_MODEL_PROMPT_COST")
    completion_rate = os.getenv("OPENAI_MODEL_COMPLETION_COST")
    if prompt_rate and completion_rate:
        try:
            return {"prompt": float(prompt_rate), "completion": float(completion_rate)}
        except ValueError:
            pass
    key = model.split(":")[0].split("/")[0]
    return MODEL_COST_RATES.get(key, {"prompt": 0.0, "completion": 0.0})


def log_usage(flow: str, model: str, input_tokens: int, output_tokens: int) -> float:
    rates       = get_cost_rates(model)
    input_cost  = (input_tokens  / 1000) * rates["prompt"]
    output_cost = (output_tokens / 1000) * rates["completion"]
    total_cost  = input_cost + output_cost

    write_header = not TOKEN_LOG.exists()
    with TOKEN_LOG.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow([
                "timestamp", "script", "flow", "model",
                "input_tokens", "output_tokens", "total_tokens",
                "input_cost_usd", "output_cost_usd", "total_cost_usd",
            ])
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "extract.py", flow, model,
            input_tokens, output_tokens, input_tokens + output_tokens,
            f"{input_cost:.6f}", f"{output_cost:.6f}", f"{total_cost:.6f}",
        ])
    return total_cost


QUERY_PLAN_PROMPT = """You are a database query writer for a QA test automation pipeline.

You will receive:
1. Source code for a web application feature (frontend services, backend controllers, ORM models)
2. The database table prefix in use
3. A list of available runtime param token names from the project's .env.example

Your job: analyse the source code and produce a query plan — a structured set of SQL queries
that fetch everything needed to both drive test inputs AND validate test outcomes.

────────────────────────────────────────────────────────────────────────────────
## PURPOSE — TWO TYPES OF DATA

The query plan serves two consumers:

### 1. Test inputs (known_entities)
A small sample of real database records that the LLM uses to write realistic test
scenarios — e.g. actual customer names, ticket numbers, staff names. These come from
entities the form lets the user select or interact with (dropdowns, search fields, etc.).

### 2. Outcome validation (cascade_entities)
The complete set of records — with all relevant relationships embedded — that the test
runner uses AFTER execution to assert the UI showed the correct data. For example: if
a test selects ticket #123, the runner needs to know upfront that ticket #123 belongs
to org X and has appointments [A, B], so it can assert those values appeared.

Both datasets come from the same queries — the full result is cascade_entities, a
known_limit-sized slice is known_entities.

────────────────────────────────────────────────────────────────────────────────
## STEP 1 — IDENTIFY ALL ENTITIES

Read the source code with all three sources in mind:

**A. Frontend service calls (page load)**
Read the frontend service files to find every API call made when the page loads or when
a selection triggers a follow-up fetch. These reveal the full set of entities the UI
needs — not just what one backend endpoint returns. A form options controller that
requires an input (e.g. ticket_id) only covers one slice; the frontend services show
what is fetched unconditionally on load.

**B. Backend form options controller**
Find the controller function that loads selectable options (look for names like
getFormOptions, getSelectData, getReportFormOptions, etc.). Every entity that populates
a dropdown, search, or selector needs queries.

**C. Access control and permission utilities**
Read any access control utilities or middleware referenced by the controller. Every
branch in the access check is a distinct test scenario that requires its own DB data.
For each check, identify and fetch the relevant data:
- Access depends on department/team/group → fetch those groupings and the agent's membership mapping
- Access depends on role (manager, admin, etc.) → fetch staff with their roles. Also look
  at the roles model file — if there is a permissions or configuration JSON column, include
  it in the SELECT. The test generator needs to know what each role actually permits (which
  actions it can and cannot take), not just the role's name or ID.
- Access depends on shared-access status → fetch the access mapping table
- Access depends on assignment or creation → ensure those columns are in the primary record query

Do not skip access control data because it looks like "middleware." It defines the
permission matrix — the scenarios the tests must cover are derived from it.

**Query types:**
- For selectable entities (dropdown options): write an ACTIVE query (records the app
  considers valid/selectable, matching the app's own filters) and an INACTIVE query
  (reversed filter, for negative test cases — e.g. submitting with a deleted record).
- For permission / access-control entities (staff, departments, roles, collaborators):
  fetch all records needed to construct the test scenarios — no active/inactive split,
  but include enough columns to identify role, department, and access level.
- For outcome validation data (things nested under a selected entity for post-execution
  assertion): write a single query — no active/inactive split needed.

────────────────────────────────────────────────────────────────────────────────
## STEP 2 — WRITE QUERIES

Rules:
- Use {prefix} as the table name placeholder in every table reference. Never hardcode it.
- Do NOT write a LIMIT clause in "sql". Limits are controlled by cascade_limit separately.
- Use column names exactly as they appear in the ORM models or existing SQL in the code.
- Prefer LEFT JOIN unless the source code explicitly shows INNER JOIN.
- When an entity's display label or category comes from a related table, JOIN it directly
  so each row is self-contained — avoid requiring Python to look up related records afterward.
- Only reference tables you can see in the provided source code.

- **Soft-delete pattern**: Before writing a WHERE clause for any table, look at the model
  file for that table. Identify how the application implements soft-deletion — it could be a
  `deletedAt` timestamp, a `delete_string` sentinel value, a boolean `is_deleted` flag, a
  `status` column, or a combination of multiple columns. Apply whatever guard the model uses
  in every active/inactive filter. If the model shows that two or more conditions are
  required together (e.g. a soft-delete column AND a status column), include all of them.
  Do not rely on a single column if the model clearly uses multiple.
  Read the model's column definitions AND its ORM options to determine the exact column name
  — do not assume any convention. Column names vary: `deletedAt` (Sequelize without underscored),
  `deleted_at` (Sequelize with underscored:true), `delete_string` (sentinel value), `is_deleted`
  (boolean), etc. If the model uses an ORM with auto-managed soft-delete (e.g. Sequelize
  `paranoid: true`), check the ORM's naming options in that model definition to find the
  exact column name. If you cannot determine the soft-delete column with confidence, omit
  the soft-delete filter rather than guess.

- **params — cross-query values must use JOIN/subquery, not %s**:
  The `params` mechanism is only for values known at runtime from the environment (e.g. the
  test agent's email address). It is NOT for values that come from another query's result
  set (e.g. "filter reports by ticket_id where ticket_id came from the tickets query").
  For inter-query dependencies, write a JOIN or subquery directly in the SQL so the database
  resolves the relationship in one shot. Only use %s and list a token in `params` if the
  value genuinely comes from the available_runtime_params environment tokens. Do NOT invent
  token names that are not in available_runtime_params.

────────────────────────────────────────────────────────────────────────────────
## STEP 3 — DESCRIBE RELATIONSHIPS AND LABEL TEMPLATES

nesting: for every entity that a test will SELECT, embed its related validation data as
nested children. Python embeds child rows inside matching parent rows and removes them
from the top-level result. Nesting specs are processed in order — you can nest a child
that is itself already nested.

Think: "given a selected ticket, what does execute.py need to look up?" → nest
appointments, linked org, linked user, etc. into the ticket object so the runner can do
a single lookup by ID without extra queries at test time.

Also nest permission data where it aids test construction: e.g. nest a staff member's
dept access rows into their staff record so it is easy to find "a staff member who has
access to dept X" or "a staff member who has no access to dept Y."

deduplicate_by: when a JOIN produces multiple rows per logical entity (e.g. staff ×
departments produces one row per staff-dept pair), use this to collapse them into one
object per entity with the repeated fields collected into a list.

## OUTPUT FORMAT

Example below uses a hypothetical bookstore domain — your keys and queries must reflect
the actual application's entities, not this example.

{
  "queries": [
    {
      "key":            "active_books",
      "sql":            "SELECT id, title, category_id FROM {prefix}book WHERE in_stock = 1 AND published = 1 ORDER BY id",
      "known_limit":    3,
      "cascade_limit":  null,
      "inactive_of":    null,
      "deduplicate_by": null,
      "params":         []
    },
    {
      "key":            "archived_books",
      "sql":            "SELECT id, title, category_id FROM {prefix}book WHERE in_stock = 0 OR published = 0 ORDER BY id",
      "known_limit":    1,
      "cascade_limit":  null,
      "inactive_of":    "active_books",
      "deduplicate_by": null,
      "params":         []
    },
    {
      "key":            "open_orders",
      "sql":            "SELECT o.id, o.book_id, o.quantity, o.status FROM {prefix}order o JOIN {prefix}staff_account sa ON sa.staff_id = o.assigned_staff_id WHERE sa.email = %s AND o.status != 'closed'",
      "known_limit":    5,
      "cascade_limit":  50,
      "inactive_of":    null,
      "deduplicate_by": null,
      "params":         ["TEST_AGENT_EMAIL"]
    },
    {
      "key":            "staff_with_roles",
      "sql":            "SELECT s.id, s.name, sa.email, sda.dept_id, r.name AS role_name, r.permissions FROM {prefix}staff s JOIN {prefix}staff_account sa ON sa.staff_id = s.id JOIN {prefix}staff_dept_access sda ON sda.staff_id = s.id JOIN {prefix}role r ON r.id = sda.role_id",
      "known_limit":    5,
      "cascade_limit":  null,
      "inactive_of":    null,
      "deduplicate_by": {
        "unique_key":     "id",
        "collect_as":     "dept_roles",
        "collect_fields": ["dept_id", "role_name", "permissions"]
      },
      "params":         []
    }
  ],
  "nesting": [
    {
      "child_key":  "open_orders",
      "into":       "active_books",
      "join_field": "book_id",
      "nest_as":    "orders",
      "parent_pk":  "id"
    }
  ],
  "namespace_map_query": "SELECT `key`, `value` FROM {prefix}options WHERE namespace = 'naming_convention'"
}

Field definitions:

key            — unique snake_case name for this result set
sql            — SQL with {prefix} placeholders and %s for any runtime params. NO LIMIT clause.
known_limit    — rows shown to the LLM (generate2) for writing test inputs; typically 1–5
cascade_limit  — null for most tables; integer cap only for high-volume tables (e.g. 50)
inactive_of    — key of the corresponding active query if this fetches excluded records; else null
deduplicate_by — null, or an object:
                   { "unique_key": "staff_id",
                     "collect_as": "dept_roles",
                     "collect_fields": ["dept_id", "dept_name", "role_id"] }
                 unique_key:     column that identifies one logical entity per output row
                 collect_as:     key under which per-join-row fields are collected into a list
                 collect_fields: the fields that differ per join row and should be collected
params         — [] for most queries. If this query's SQL uses %s placeholders, list the
                 corresponding token names here in order (e.g. ["TEST_AGENT_EMAIL"]).
                 Only use token names from the "available_runtime_params" list in the input.
                 Python substitutes each token with its runtime value before executing.

                 Use params when a query must be filtered by the test agent's identity at
                 runtime — e.g. fetch only records the test agent can access. Embed the
                 filter directly in the SQL (subquery or JOIN) using %s. Do NOT use a
                 separate lookup query and post-filter in Python; put the filter in the SQL.

nesting[].child_key  — key of the child query whose rows get embedded
nesting[].into       — key of the parent query to embed them into
nesting[].join_field — column on the child row whose value matches the parent's primary key
nesting[].nest_as    — key name to store the embedded list under inside each parent object
nesting[].parent_pk  — the parent's primary key column name

namespace_map_query — SQL to read UI label overrides; must return rows with 'key' and
                      'value' columns. Use {prefix} for table names.

label_templates — for any dropdown/select field whose options are built from DB records
                  via a .map() call that constructs a composite label (e.g.
                  `${t.ticket_number} - ${t.ticket_subject}`), add an entry here.
                  Omit entries for static option lists hardcoded in JSX — those don't need this.
                  If no composite labels exist, output label_templates: {}
                  Python extracts the exact template string from the source code directly —
                  you only need to identify which entity and which field is the value.

  Example:
  "label_templates": {
    "ticket_id": {
      "value_field": "ticket_id",
      "source_key":  "active_tickets"
    }
  }

  value_field: field in the record used as the option's submitted value
  source_key:  key in known_entities whose records this template applies to

Return only the raw JSON object. No markdown, no explanation outside the JSON."""


FIX_PROMPT = """Some SQL queries you generated for a QA test pipeline failed when executed against MySQL.

Fix only the queries listed in "failing_queries". Rules:
- Use {prefix} as the table name placeholder — never substitute the actual prefix value
- Fix only what the MySQL error message indicates — do not change JOINs, add tables, or remove columns
- Common causes: wrong column case (e.g. `deleted_at` vs `deletedAt`), wrong column name, wrong alias
- The MySQL error message names the exact column or table that is wrong — use that to guide the fix

Return ONLY this JSON — no markdown, no explanation:
{"fixes": [{"key": "query_key", "sql": "corrected SQL"}]}"""


def _sanity_check_plan(plan: dict) -> dict:
    """Fix trivial Python-detectable issues before hitting the DB."""
    for q in plan.get("queries", []):
        sql = q.get("sql", "").strip()
        # Strip trailing semicolons — pymysql doesn't need them and some drivers reject them
        sql = sql.rstrip(";").rstrip()
        q["sql"] = sql
    return plan


def _fix_failing_queries(
    plan: dict,
    failures: list[dict],
    prefix: str,
    api_key: str,
    model: str,
    flow_id: str,
) -> dict:
    """Send only the failing queries + their errors back to the LLM. Patch plan in place."""
    from openai import OpenAI

    user_msg = json.dumps({
        "table_prefix":   prefix,
        "failing_queries": failures,   # [{"key", "sql", "error"}, ...]
    }, indent=2)

    client   = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": FIX_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0.1,
    )
    usage = response.usage
    cost  = log_usage(flow_id, model, usage.prompt_tokens, usage.completion_tokens)
    print(
        f"  [extract] fix call — in: {usage.prompt_tokens:,}  "
        f"out: {usage.completion_tokens:,}  cost: ${cost:.4f}",
        file=sys.stderr,
    )

    result = json.loads(response.choices[0].message.content)
    fixes  = {f["key"]: f["sql"] for f in result.get("fixes", [])}

    for q in plan["queries"]:
        if q["key"] in fixes:
            old = q["sql"]
            q["sql"] = fixes[q["key"]].rstrip(";").rstrip()
            print(f"  [extract] fixed '{q['key']}': {old!r} → {q['sql']!r}", file=sys.stderr)

    return plan


def _validate_and_correct_plan(
    plan: dict,
    conn,
    prefix: str,
    param_values: dict,
    api_key: str,
    model: str,
    flow_id: str,
    cache_path: Path,
    max_retries: int = 3,
) -> dict:
    """
    Run each query with LIMIT 1. If any fail, send only the failures to the LLM for a fix.
    Repeat up to max_retries times. Update the cache if any fixes were applied.
    """
    fixed = False

    for attempt in range(max_retries):
        failures: list[dict] = []

        with conn.cursor() as cur:
            for q in plan.get("queries", []):
                key    = q["key"]
                sql    = q["sql"].replace("{prefix}", prefix)
                params = q.get("params") or []
                values = [param_values.get(t, "") for t in params] if params else None
                test   = sql + " LIMIT 1"

                try:
                    cur.execute(test, values) if values else cur.execute(test)
                    cur.fetchall()
                except Exception as exc:
                    failures.append({"key": key, "sql": q["sql"], "error": str(exc)})

        if not failures:
            break

        print(
            f"  [extract] {len(failures)} query/queries failed "
            f"(attempt {attempt + 1}/{max_retries}) — asking LLM to fix...",
            file=sys.stderr,
        )
        plan  = _fix_failing_queries(plan, failures, prefix, api_key, model, flow_id)
        fixed = True
    else:
        remaining = [f["key"] for f in failures]
        print(
            f"  [extract] WARNING: {len(remaining)} query/queries still failing after "
            f"{max_retries} retries: {remaining}",
            file=sys.stderr,
        )

    if fixed:
        cache_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        print(f"  [extract] corrected plan re-cached → {cache_path.name}", file=sys.stderr)

    return plan


def _generate_query_plan(
    source_code: dict,
    prefix: str,
    model: str,
    api_key: str,
    flow_id: str,
    env_example_keys: list | None = None,
    force: bool = False,
) -> dict:
    """One LLM call that reads source code and writes the SQL query plan. Result is cached."""
    QUERY_PLANS_DIR.mkdir(exist_ok=True)
    cache_path = QUERY_PLANS_DIR / f"{flow_id}.json"

    if cache_path.exists() and not force:
        print(f"  [extract] query plan: cache hit ({cache_path.name})", file=sys.stderr)
        return json.loads(cache_path.read_text(encoding="utf-8"))

    if not api_key:
        print("  [extract] WARNING: OPENAI_API_KEY not set — DB query skipped", file=sys.stderr)
        return {}

    try:
        from openai import OpenAI
    except ImportError:
        print("  [extract] WARNING: openai not installed — DB query skipped", file=sys.stderr)
        return {}

    user_msg = json.dumps({
        "table_prefix":            prefix,
        "available_runtime_params": env_example_keys or [],
        "source_code":             source_code,
    }, indent=2)

    print(f"  [extract] generating query plan via LLM ({model})...", file=sys.stderr)
    client   = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": QUERY_PLAN_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0.1,
    )
    usage = response.usage
    cost  = log_usage(flow_id, model, usage.prompt_tokens, usage.completion_tokens)
    print(
        f"  [extract] query plan — in: {usage.prompt_tokens:,}  "
        f"out: {usage.completion_tokens:,}  cost: ${cost:.4f}",
        file=sys.stderr,
    )
    plan = json.loads(response.choices[0].message.content)
    cache_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    print(f"  [extract] query plan cached → {cache_path.name}", file=sys.stderr)
    return plan


def _run_query_plan(
    plan: dict,
    conn,
    prefix: str,
    param_values: dict | None = None,
) -> dict:
    """Execute the LLM-generated query plan generically. Returns {all, sample, namespace_map}."""
    if param_values is None:
        param_values = {}
    results: dict[str, list] = {}

    with conn.cursor() as cur:

        # ── 1. Run each query ────────────────────────────────────────────────
        for q in plan.get("queries", []):
            key           = q["key"]
            sql           = q["sql"].replace("{prefix}", prefix)
            cascade_limit = q.get("cascade_limit")
            params        = q.get("params") or []

            if cascade_limit:
                sql = sql.rstrip().rstrip(";") + f" LIMIT {int(cascade_limit)}"

            values = [param_values.get(t, "") for t in params] if params else None

            try:
                cur.execute(sql, values) if values else cur.execute(sql)
                rows = list(cur.fetchall())
            except Exception as exc:
                print(f"  [extract] WARNING: query '{key}' failed ({exc})", file=sys.stderr)
                results[key] = []
                continue

            # ── 2. Deduplicate if specified ──────────────────────────────────
            dedup = q.get("deduplicate_by")
            if dedup:
                unique_key     = dedup["unique_key"]
                collect_as     = dedup["collect_as"]
                collect_fields = dedup.get("collect_fields", [])
                collect_set    = set(collect_fields)
                seen: dict     = {}
                for row in rows:
                    uid = row[unique_key]
                    if uid not in seen:
                        entry = {k: v for k, v in row.items() if k not in collect_set}
                        entry[collect_as] = []
                        seen[uid] = entry
                    seen[uid][collect_as].append({f: row.get(f) for f in collect_fields})
                rows = list(seen.values())

            results[key] = rows

        # ── 3. Namespace map ─────────────────────────────────────────────────
        namespace_map: dict[str, str] = {}
        ns_sql = plan.get("namespace_map_query", "")
        if ns_sql:
            try:
                cur.execute(ns_sql.replace("{prefix}", prefix))
                for row in cur.fetchall():
                    if row.get("key") and row.get("value") is not None:
                        namespace_map[row["key"]] = str(row["value"])
            except Exception as exc:
                print(f"  [extract] WARNING: namespace_map query failed ({exc})", file=sys.stderr)

    # ── 6. Apply nesting (processed in order — supports multi-level) ──────────
    for spec in plan.get("nesting", []):
        child_key  = spec["child_key"]
        parent_key = spec["into"]
        join_field = spec["join_field"]
        nest_as    = spec["nest_as"]
        parent_pk  = spec.get("parent_pk")

        children = results.get(child_key, [])
        parents  = results.get(parent_key, [])
        if not parents:
            continue
        if not parent_pk:
            parent_pk = next(iter(parents[0]))  # first column as fallback

        child_map: dict = {}
        for child in children:
            child_map.setdefault(child.get(join_field), []).append(child)

        for parent in parents:
            parent[nest_as] = child_map.get(parent.get(parent_pk), [])

        results.pop(child_key, None)

    # ── 7. Build full (cascade_entities) and sample (known_entities) ──────────
    known_limits = {q["key"]: q.get("known_limit", 3) for q in plan.get("queries", [])}
    full:   dict = {}
    sample: dict = {}
    for q in plan.get("queries", []):
        key = q["key"]
        if key not in results:      # was nested into a parent and removed from top-level
            continue
        rows        = results[key]
        full[key]   = rows
        sample[key] = rows[: known_limits.get(key, 3)]

    return {"all": full, "sample": sample, "namespace_map": namespace_map}


def _extract_template_from_source(
    frontend: dict,
    source_key: str,
    known_entities: dict,
) -> str | None:
    """
    Scan frontend source files for a .map(var => `...`) template literal whose
    interpolated fields match the columns of known_entities[source_key].
    Returns a normalized template string (${field_name} without variable prefix),
    or None if no matching pattern is found.
    """
    sample_records = known_entities.get(source_key, [])
    if not sample_records:
        return None
    entity_fields = set(sample_records[0].keys())

    # Pattern 1: .map(var => `template`)  — direct template literal return
    # Pattern 2: .map(var => ({ ..., label: `template`, ... }))  — object literal with label property
    # Both forms appear in iSERV frontend source.
    map_patterns = [
        re.compile(r'\.map\s*\(\s*\(?(\w+)\)?\s*=>\s*`([^`]+)`'),
        re.compile(r'\.map\s*\(\s*\(?(\w+)\)?\s*=>\s*\(\s*\{[^`]*?label\s*:\s*`([^`]+)`', re.DOTALL),
    ]

    def _normalize(var_name: str, template_body: str) -> str | None:
        field_refs = re.findall(
            r'\$\{' + re.escape(var_name) + r'\.(\w+)\}',
            template_body,
        )
        if not field_refs:
            return None
        if not any(f in entity_fields for f in field_refs):
            return None
        return re.sub(
            r'\$\{' + re.escape(var_name) + r'\.(\w+)\}',
            r'${\1}',
            template_body,
        )

    for section_dict in frontend.values():
        if not isinstance(section_dict, dict):
            continue
        for content in section_dict.values():
            if not isinstance(content, str):
                continue
            for pat in map_patterns:
                for m in pat.finditer(content):
                    result = _normalize(m.group(1), m.group(2))
                    if result:
                        return result
    return None


def _build_field_options(
    plan: dict,
    known_entities: dict,
    frontend: dict | None = None,
) -> dict:
    """
    Apply label_templates from the query plan against known_entities records.
    Template strings are extracted from frontend source files via regex first;
    falls back to the LLM-provided 'template' field if source extraction fails.
    Returns field_options: {field_name: [{label, value}, ...]}
    """
    field_options: dict = {}
    templates = plan.get("label_templates", {})
    if not templates:
        return field_options

    for field_name, spec in templates.items():
        value_field = spec.get("value_field", "")
        source_key  = spec.get("source_key", "")
        records     = known_entities.get(source_key, [])
        if not records or not value_field:
            continue

        # Optional: filter records to only those in departments where some role
        # has the required permission.  Spec example:
        #   "dept_permission_filter": {
        #     "dept_id_field": "dept_id",
        #     "staff_key": "staff_with_roles",
        #     "permission": "service_report_management.service_report.create"
        #   }
        dpf = spec.get("dept_permission_filter")
        if dpf:
            dept_id_field = dpf.get("dept_id_field", "dept_id")
            staff_key     = dpf.get("staff_key", "staff_with_roles")
            perm_path     = dpf.get("permission", "")

            def _has_perm(perm_json: dict, path: str) -> bool:
                parts = path.split(".")
                node = perm_json
                for p in parts:
                    if not isinstance(node, dict):
                        return False
                    node = node.get(p)
                return node is True or node == "limited"

            allowed_dept_ids: set = set()
            for staff in known_entities.get(staff_key, []):
                for dr in staff.get("dept_roles", []):
                    perm_json = dr.get("permissions", {})
                    if isinstance(perm_json, str):
                        try:
                            import json as _json
                            perm_json = _json.loads(perm_json)
                        except Exception:
                            continue
                    if _has_perm(perm_json, perm_path):
                        allowed_dept_ids.add(str(dr.get("dept_id", "")))

            if allowed_dept_ids:
                records = [r for r in records if str(r.get(dept_id_field, "")) in allowed_dept_ids]

        # Prefer template extracted directly from source; fall back to LLM output
        template = None
        if frontend:
            template = _extract_template_from_source(frontend, source_key, known_entities)
        if not template:
            template = spec.get("template", "")
        if not template:
            continue

        options = []
        for record in records:
            label = re.sub(
                r"\$\{(\w+)\}",
                lambda m, r=record: str(r.get(m.group(1), "")),
                template,
            )
            value = record.get(value_field)
            if value is not None:
                options.append({"label": label, "value": value})

        if options:
            field_options[field_name] = options

    return field_options


# ── Main extract function ──────────────────────────────────────────────────────

def extract(flow_id: str, subdir: str | None = None, force_regenerate: bool = False) -> dict:
    if subdir:
        manifest_path = MANIFESTS_DIR / subdir / f"{flow_id}.json"
    else:
        manifest_path = MANIFESTS_DIR / f"{flow_id}.json"

    if not manifest_path.exists():
        print(f"ERROR: manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    manifest     = json.loads(manifest_path.read_text(encoding="utf-8"))
    files_spec   = manifest.get("files", {})
    primary_role = manifest.get("primary_role", "")

    frontend, backend, models, manifest_names = _build_sections(files_spec)

    server_env       = _load_server_env()
    testgen_env      = _load_testgen_env()
    env_example_keys = _load_env_example_keys()
    prefix           = server_env.get("DB_TABLE_PREFIX", "ith_")
    model            = testgen_env.get("OPENAI_MODEL", "gpt-4o")
    api_key          = testgen_env.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")

    source_code = {"frontend": frontend, "backend": backend, "models": models}
    plan        = _generate_query_plan(source_code, prefix, model, api_key, flow_id,
                                       env_example_keys=env_example_keys,
                                       force=force_regenerate)

    db_result: dict = {}
    if plan:
        password = _decrypt_db_password(server_env)
        if password:
            try:
                import pymysql
                import pymysql.cursors
                conn = pymysql.connect(
                    host=server_env.get("DB_HOST", "localhost"),
                    port=int(server_env.get("DB_PORT", 3306)),
                    user=server_env.get("DB_USER", ""),
                    password=password,
                    database=server_env.get("DB_NAME", ""),
                    cursorclass=pymysql.cursors.DictCursor,
                    connect_timeout=5,
                )
                try:
                    plan = _sanity_check_plan(plan)
                    plan = _validate_and_correct_plan(
                        plan, conn, prefix, dict(testgen_env),
                        api_key, model, flow_id,
                        cache_path=QUERY_PLANS_DIR / f"{flow_id}.json",
                    )
                    db_result = _run_query_plan(plan, conn, prefix, param_values=dict(testgen_env))
                finally:
                    conn.close()
            except ImportError:
                print("  [extract] WARNING: pymysql not installed — DB query skipped. "
                      "Run: pip install pymysql cryptography", file=sys.stderr)
            except Exception as exc:
                print(f"  [extract] WARNING: DB connection failed ({exc}) — DB query skipped",
                      file=sys.stderr)

    known_entities   = db_result.get("sample",        {})
    cascade_entities = db_result.get("all",            {})
    namespace_map    = db_result.get("namespace_map",  {})
    field_options    = _build_field_options(plan, known_entities, frontend=frontend)

    context = {
        "flow_name":      flow_id,
        "primary_role":   primary_role,
        "files_manifest": manifest_names,
        "frontend":       frontend,
        "backend":        backend,
        "models":         models,
        "test_data": {
            "known_entities":   known_entities,
            "cascade_entities": cascade_entities,
            "namespace_map":    namespace_map,
            "field_options":    field_options,
        },
        "meta": {
            "files_read":   len(manifest_names),
            "db_queried":   bool(cascade_entities),
        },
    }

    return context


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract source context for a test flow into context_{flow}.json"
    )
    parser.add_argument(
        "--flow", required=True,
        help="Flow ID matching a manifest in testGen/manifests/, "
             "e.g. agent_create_ticket"
    )
    parser.add_argument(
        "--subdir", default=None,
        help="Subdirectory inside testGen/manifests/, e.g. createTicket"
    )
    parser.add_argument(
        "--out", default=None,
        help="Output path (default: testGen/context_{flow}.json)"
    )
    parser.add_argument(
        "--regenerate-query-plan", action="store_true",
        help="Force regeneration of the LLM query plan even if a cached plan exists"
    )
    args = parser.parse_args()

    context_dir = TESTGEN_DIR / "context"
    context_dir.mkdir(exist_ok=True)
    out_path = Path(args.out) if args.out else context_dir / f"context_{args.flow}.json"

    target_manifest = (
        MANIFESTS_DIR / args.subdir / f"{args.flow}.json"
        if args.subdir else
        MANIFESTS_DIR / f"{args.flow}.json"
    )

    print(f"[extract] flow:     {args.flow}")
    if args.subdir:
        print(f"[extract] subdir:   {args.subdir}")
    print(f"[extract] manifest: {target_manifest}")
    print(f"[extract] output:   {out_path}")
    if args.regenerate_query_plan:
        print(f"[extract] --regenerate-query-plan: ignoring cache")

    ctx = extract(args.flow, subdir=args.subdir, force_regenerate=args.regenerate_query_plan)

    out_path.write_text(json.dumps(ctx, indent=2, default=_json_default), encoding="utf-8")

    m = ctx["meta"]
    print(f"[extract] done — {m['files_read']} files, "
          f"DB {'queried' if m['db_queried'] else 'SKIPPED (check warnings)'}")

    # Print section summary
    for section, files in ctx["frontend"].items():
        if files:
            print(f"  frontend.{section}: {list(files.keys())}")
    for section, files in ctx["backend"].items():
        if files:
            print(f"  backend.{section}: {list(files.keys())}")
    if ctx["models"]:
        print(f"  models: {list(ctx['models'].keys())}")


if __name__ == "__main__":
    main()
