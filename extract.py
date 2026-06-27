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
import json
import os
import re
import sys
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


# ── Permission helpers ─────────────────────────────────────────────────────────

def _role_can_create_ticket(permissions_raw) -> bool:
    """Returns True if the role permits ticket creation (True or 'limited')."""
    if isinstance(permissions_raw, str):
        try:
            permissions_raw = json.loads(permissions_raw)
        except Exception:
            return False
    if not isinstance(permissions_raw, dict):
        return False
    val = (permissions_raw
           .get("ticket_management", {})
           .get("ticket", {})
           .get("create", False))
    return bool(val)


# ── DB query step ──────────────────────────────────────────────────────────────

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
    """Parse testGen/.env into a plain dict."""
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


def _query_db(env: dict, form: str = "", test_agent_email: str = "") -> dict:
    """
    Connect to iSERV MySQL and fetch ALL active entity data (no row limit).

    form=""           → create_ticket shape (customers + equipment + departments)
    form="create_report" → create_report shape (customers + tickets/appointments +
                           report forms + flat staff; no equipment)

    Returns a dict with keys:
      "all"     — complete dataset used by execute.py for cascade validation
      "sample"  — minimal sample used as known_entities for the LLM prompt
      "namespace_map" — resolved UI terminology
    Returns empty dict on connection failure.
    """
    try:
        import pymysql
        import pymysql.cursors
    except ImportError:
        print("  [extract] WARNING: pymysql not installed — DB query skipped. "
              "Run: pip install pymysql cryptography", file=sys.stderr)
        return {}

    password = _decrypt_db_password(env)
    if not password:
        return {}

    prefix = env.get("DB_TABLE_PREFIX", "ith_")

    try:
        conn = pymysql.connect(
            host=env.get("DB_HOST", "localhost"),
            port=int(env.get("DB_PORT", 3306)),
            user=env.get("DB_USER", ""),
            password=password,
            database=env.get("DB_NAME", ""),
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5,
        )
    except Exception as exc:
        print(f"  [extract] WARNING: DB connection failed ({exc}) — DB query skipped",
              file=sys.stderr)
        return {}

    try:
        with conn.cursor() as cur:
            # ── Shared: Active customers ────────────────────────────────────
            cur.execute(
                f"SELECT id, name, status FROM {prefix}organization "
                f"WHERE delete_string = 'active' AND status = 1 ORDER BY id"
            )
            customers = cur.fetchall()
            org_ids   = [c["id"] for c in customers]

            # ── Shared: One inactive customer (deactivated, not deleted) ────
            cur.execute(
                f"SELECT id, name FROM {prefix}organization "
                f"WHERE delete_string = 'active' AND (status IS NULL OR status != 1) "
                f"ORDER BY id LIMIT 1"
            )
            inactive_customers_raw = cur.fetchall()

            all_users              = []
            inactive_users_raw     = []
            all_equipment          = []
            inactive_equipment_raw = []

            if org_ids:
                placeholders = ",".join(["%s"] * len(org_ids))

                # ── Shared: Active users per customer ───────────────────────
                cur.execute(
                    f"SELECT id, name, org_id "
                    f"FROM {prefix}user "
                    f"WHERE org_id IN ({placeholders}) AND status = 1",
                    org_ids,
                )
                all_users = cur.fetchall()

                # ── Shared: Inactive users per customer ─────────────────────
                cur.execute(
                    f"SELECT id, name, org_id "
                    f"FROM {prefix}user "
                    f"WHERE org_id IN ({placeholders}) AND status != 1",
                    org_ids,
                )
                inactive_users_raw = cur.fetchall()

                if form != "create_report":
                    # ── create_ticket only: Active equipment per customer ───
                    cur.execute(
                        f"SELECT id, provision_name, org_id, site_id "
                        f"FROM {prefix}organization_equipments "
                        f"WHERE org_id IN ({placeholders}) "
                        f"AND delete_string = 'active' AND status_id != 0",
                        org_ids,
                    )
                    all_equipment = cur.fetchall()

                    # ── create_ticket only: Inactive equipment ──────────────
                    cur.execute(
                        f"SELECT id, provision_name, org_id, site_id "
                        f"FROM {prefix}organization_equipments "
                        f"WHERE org_id IN ({placeholders}) "
                        f"AND delete_string = 'active' AND status_id = 0",
                        org_ids,
                    )
                    inactive_equipment_raw = cur.fetchall()

            # ── Shared: Active departments ──────────────────────────────────
            cur.execute(
                f"SELECT id, name, manager_id, ispublic FROM {prefix}department "
                f"WHERE delete_string = 'active' ORDER BY id"
            )
            departments = cur.fetchall()
            dept_ids    = [d["id"] for d in departments]

            # ── Shared: One inactive department (soft-deleted) ──────────────
            cur.execute(
                f"SELECT id, name, ispublic FROM {prefix}department "
                f"WHERE delete_string != 'active' ORDER BY id LIMIT 1"
            )
            inactive_departments_raw = cur.fetchall()

            all_staff                  = []
            inactive_staff_raw         = []
            all_service_types          = []
            inactive_service_types_raw = []
            all_role_perms             = []
            all_role_defs              = []

            if dept_ids:
                placeholders = ",".join(["%s"] * len(dept_ids))

                # ── Shared: Active staff per department ─────────────────────
                # s.role_id  = global staff role (on the staff row itself)
                # sda.role_id = dept-specific role assignment
                cur.execute(
                    f"SELECT s.staff_id, s.name, sa.email, "
                    f"sda.dept_id, sda.role_id, s.role_id AS staff_role_id "
                    f"FROM {prefix}staff s "
                    f"JOIN {prefix}staff_dept_access sda ON s.staff_id = sda.staff_id "
                    f"LEFT JOIN {prefix}staff_account sa "
                    f"  ON sa.staff_id = s.staff_id AND sa.delete_string = 'active' "
                    f"WHERE sda.dept_id IN ({placeholders}) AND sda.delete_string = 'active'",
                    dept_ids,
                )
                all_staff = cur.fetchall()

                # ── Shared: Inactive staff-dept entries (revoked access) ────
                cur.execute(
                    f"SELECT s.staff_id, s.name, sa.email, "
                    f"sda.dept_id, sda.role_id, s.role_id AS staff_role_id "
                    f"FROM {prefix}staff s "
                    f"JOIN {prefix}staff_dept_access sda ON s.staff_id = sda.staff_id "
                    f"LEFT JOIN {prefix}staff_account sa "
                    f"  ON sa.staff_id = s.staff_id AND sa.delete_string = 'active' "
                    f"WHERE sda.dept_id IN ({placeholders}) AND sda.delete_string != 'active'",
                    dept_ids,
                )
                inactive_staff_raw = cur.fetchall()

                # ── Shared: Active service types per department ─────────────
                cur.execute(
                    f"SELECT st.id, st.service_type AS name, st.ispublic, std.department_id "
                    f"FROM {prefix}service_type st "
                    f"JOIN {prefix}service_type_departments std ON st.id = std.service_type_id "
                    f"WHERE std.department_id IN ({placeholders}) AND st.deletedAt IS NULL",
                    dept_ids,
                )
                all_service_types = cur.fetchall()

                # ── Shared: Soft-deleted service types per department ───────
                cur.execute(
                    f"SELECT st.id, st.service_type AS name, st.ispublic, std.department_id "
                    f"FROM {prefix}service_type st "
                    f"JOIN {prefix}service_type_departments std ON st.id = std.service_type_id "
                    f"WHERE std.department_id IN ({placeholders}) AND st.deletedAt IS NOT NULL",
                    dept_ids,
                )
                inactive_service_types_raw = cur.fetchall()

                # ── Shared: role_name annotation per (staff_id, dept_id) ────
                all_role_perms = []
                for roles_table in (f"{prefix}roles", "roles"):
                    try:
                        cur.execute(
                            f"SELECT sda.staff_id, sda.dept_id, r.name AS role_name "
                            f"FROM {prefix}staff_dept_access sda "
                            f"JOIN {roles_table} r ON sda.role_id = r.id "
                            f"WHERE sda.dept_id IN ({placeholders}) "
                            f"AND sda.delete_string = 'active'",
                            dept_ids,
                        )
                        all_role_perms = cur.fetchall()
                        break
                    except Exception:
                        all_role_perms = []

                # ── Shared: Role definitions — both dept roles (sda.role_id)
                #    and global staff roles (s.role_id), deduplicated ────────
                all_role_ids = list({
                    rid
                    for row in all_staff
                    for rid in (row.get("role_id"), row.get("staff_role_id"))
                    if rid
                })
                all_role_defs = []
                if all_role_ids:
                    placeholders_r = ",".join(["%s"] * len(all_role_ids))
                    for roles_table in (f"{prefix}roles", "roles"):
                        try:
                            cur.execute(
                                f"SELECT id, name, type, staff_type, permissions "
                                f"FROM {roles_table} WHERE id IN ({placeholders_r})",
                                all_role_ids,
                            )
                            all_role_defs = cur.fetchall()
                            break
                        except Exception:
                            all_role_defs = []

            # ── Shared: Naming conventions (namespace_map) ──────────────────
            namespace_map: dict[str, str] = {}
            for opts_table in (f"{prefix}options", "options"):
                try:
                    cur.execute(
                        f"SELECT `key`, `value` FROM {opts_table} "
                        f"WHERE namespace = 'naming_convention'",
                    )
                    for row in cur.fetchall():
                        if row.get("key") and row.get("value") is not None:
                            namespace_map[row["key"]] = str(row["value"])
                    break
                except Exception:
                    namespace_map = {}

            # ── create_report specific queries ──────────────────────────────
            open_tickets           = []
            closed_tickets_raw     = []
            scheduled_appointments = []
            all_appt_staff         = []
            active_forms           = []
            inactive_forms_raw     = []
            form_mappings          = []

            if form == "create_report":
                # ── Resolve test-agent department access (permission filter) ─
                agent_dept_ids: list[int] = []
                if test_agent_email:
                    cur.execute(
                        f"SELECT sda.dept_id "
                        f"FROM {prefix}staff_dept_access sda "
                        f"JOIN {prefix}staff s ON s.staff_id = sda.staff_id "
                        f"JOIN {prefix}staff_account sa ON sa.staff_id = s.staff_id "
                        f"WHERE sa.email = %s "
                        f"AND sda.delete_string = 'active' "
                        f"AND sa.delete_string = 'active'",
                        [test_agent_email],
                    )
                    agent_dept_ids = [row["dept_id"] for row in cur.fetchall()]
                    if agent_dept_ids:
                        print(
                            f"  [extract] TEST_AGENT_EMAIL dept access: "
                            f"{len(agent_dept_ids)} department(s) — "
                            f"tickets filtered to those departments",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            f"  [extract] WARNING: TEST_AGENT_EMAIL '{test_agent_email}' "
                            f"not found in staff_account or has no active dept access — "
                            f"fetching all tickets (permission errors may occur at runtime)",
                            file=sys.stderr,
                        )

                # ── Open (non-closed) tickets, filtered to agent's depts ────
                ticket_select = (
                    f"SELECT t.ticket_id, t.ticket_number, t.ticket_subject, "
                    f"t.dept_id, u.org_id, t.user_id, t.status_id, t.service_type, "
                    f"o.name AS org_name, u.name AS user_name "
                    f"FROM {prefix}ticket t "
                    f"LEFT JOIN {prefix}user u ON u.id = t.user_id "
                    f"LEFT JOIN {prefix}organization o "
                    f"  ON o.id = u.org_id AND o.delete_string = 'active' "
                    f"WHERE t.deletedAt IS NULL AND t.status_id != 3"
                )
                if agent_dept_ids:
                    dept_ph = ",".join(["%s"] * len(agent_dept_ids))
                    cur.execute(
                        ticket_select + f" AND t.dept_id IN ({dept_ph}) "
                        f"ORDER BY t.ticket_id DESC LIMIT 50",
                        agent_dept_ids,
                    )
                else:
                    cur.execute(ticket_select + " ORDER BY t.ticket_id DESC LIMIT 50")
                open_tickets = cur.fetchall()
                ticket_ids   = [t["ticket_id"] for t in open_tickets]

                # ── One closed ticket (active_inactive_filter test) ─────────
                cur.execute(
                    f"SELECT ticket_id, ticket_number, ticket_subject "
                    f"FROM {prefix}ticket "
                    f"WHERE deletedAt IS NULL AND status_id = 3 "
                    f"ORDER BY ticket_id DESC LIMIT 1"
                )
                closed_tickets_raw = cur.fetchall()

                if ticket_ids:
                    placeholders_t = ",".join(["%s"] * len(ticket_ids))

                    # ── Scheduled appointments without existing reports ──────
                    cur.execute(
                        f"SELECT a.id, a.ticket_id, a.title, "
                        f"a.start_datetime, a.end_datetime "
                        f"FROM {prefix}appointment a "
                        f"JOIN {prefix}appointment_status ast ON ast.id = a.status "
                        f"LEFT JOIN {prefix}service_report_header srh "
                        f"  ON srh.appointment_id = a.id AND srh.deletedAt IS NULL "
                        f"WHERE a.deletedAt IS NULL "
                        f"AND ast.state = 'scheduled' "
                        f"AND srh.appointment_id IS NULL "
                        f"AND a.ticket_id IN ({placeholders_t})",
                        ticket_ids,
                    )
                    scheduled_appointments = cur.fetchall()
                    appt_ids = [a["id"] for a in scheduled_appointments]

                    if appt_ids:
                        placeholders_a = ",".join(["%s"] * len(appt_ids))
                        cur.execute(
                            f"SELECT apst.appointment_id, apst.staff_id, "
                            f"s.name AS staff_name "
                            f"FROM {prefix}appointment_staff apst "
                            f"JOIN {prefix}staff s ON s.staff_id = apst.staff_id "
                            f"WHERE apst.deletedAt IS NULL "
                            f"AND apst.appointment_id IN ({placeholders_a})",
                            appt_ids,
                        )
                        all_appt_staff = cur.fetchall()

                # ── Active SERVICE_REPORT form schemas ──────────────────────
                cur.execute(
                    f"SELECT id, name, title FROM {prefix}form_schema "
                    f"WHERE formFor = 'SERVICE_REPORT' AND isActive = 1 "
                    f"AND deletedAt IS NULL ORDER BY id"
                )
                active_forms = cur.fetchall()

                # ── One inactive form schema ────────────────────────────────
                cur.execute(
                    f"SELECT id, name, title FROM {prefix}form_schema "
                    f"WHERE formFor = 'SERVICE_REPORT' "
                    f"AND (isActive = 0 OR deletedAt IS NOT NULL) LIMIT 1"
                )
                inactive_forms_raw = cur.fetchall()

                # ── Form → service_type + applies_to mappings ───────────────
                cur.execute(
                    f"SELECT form_id, service_type_id, applies_to, is_default "
                    f"FROM {prefix}service_report_form_service_types "
                    f"WHERE deletedAt IS NULL"
                )
                form_mappings = cur.fetchall()

    finally:
        conn.close()

    # ── Common post-processing ────────────────────────────────────────────────

    # {(staff_id, dept_id): role_name} for annotation
    role_name_map: dict[tuple, str] = {
        (row["staff_id"], row["dept_id"]): row.get("role_name", "")
        for row in all_role_perms
    }

    # role_definitions: covers both dept roles (sda.role_id) and global staff
    # roles (s.role_id), keyed by role id string
    role_definitions: dict[str, dict] = {
        str(row["id"]): {
            "name":        row.get("name"),
            "type":        row.get("type"),
            "staff_type":  row.get("staff_type"),
            "permissions": row.get("permissions") or {},
        }
        for row in all_role_defs
    }

    role_can_create_map: dict[str, bool] = {
        rid: _role_can_create_ticket(info.get("permissions", {}))
        for rid, info in role_definitions.items()
    }

    # dept-nested staff: includes both role_id (dept role) and staff_role_id (global)
    customers_out = [
        {
            "id":                 c["id"],
            "name":               c["name"],
            "users":              [u for u in all_users              if u["org_id"] == c["id"]],
            "inactive_users":     [u for u in inactive_users_raw     if u["org_id"] == c["id"]],
            "equipment":          [e for e in all_equipment          if e["org_id"] == c["id"]],
            "inactive_equipment": [e for e in inactive_equipment_raw if e["org_id"] == c["id"]],
        }
        for c in customers
    ]

    def _annotated_staff(staff_row: dict, dept_id: int) -> dict:
        return {
            **staff_row,
            "role_name": role_name_map.get((staff_row["staff_id"], dept_id), ""),
        }

    departments_out = [
        {
            "id":                     d["id"],
            "name":                   d["name"],
            "manager_id":             d["manager_id"],
            "staff":                  [_annotated_staff(s, d["id"]) for s in all_staff              if s["dept_id"] == d["id"]],
            "inactive_staff":         [s                             for s in inactive_staff_raw     if s["dept_id"] == d["id"]],
            "service_types":          [s for s in all_service_types          if s["department_id"] == d["id"]],
            "inactive_service_types": [s for s in inactive_service_types_raw if s["department_id"] == d["id"]],
        }
        for d in departments
    ]

    # staff_access_summary — cross-dept ticket-create classification
    _staff_dept_access: dict[int, list[dict]] = {}
    _staff_names:       dict[int, str]        = {}
    for _row in all_staff:
        _sid = _row["staff_id"]
        _staff_names[_sid] = _row["name"]
        _dept_name = next(
            (d["name"] for d in departments if d["id"] == _row["dept_id"]),
            str(_row["dept_id"]),
        )
        _rid_str = str(_row.get("role_id") or "")
        _staff_dept_access.setdefault(_sid, []).append({
            "dept_name":  _dept_name,
            "can_create": role_can_create_map.get(_rid_str, False),
        })

    staff_access_summary: list[dict] = []
    for _sid in sorted(_staff_dept_access):
        _entries  = _staff_dept_access[_sid]
        _with     = [e["dept_name"] for e in _entries if e["can_create"]]
        _without  = [e["dept_name"] for e in _entries if not e["can_create"]]
        _atype    = "MIXED" if _with and _without else ("ALL" if _with else "LIMITED")
        staff_access_summary.append({
            "name":                  _staff_names[_sid],
            "access_type":           _atype,
            "depts_with_create":     _with,
            "depts_without_create":  _without,
        })

    sample_depts = [d for d in departments_out[:2] if d is not None]

    print(
        f"  [extract] DB: {len(customers_out)} customers, {len(departments_out)} departments",
        file=sys.stderr,
    )

    # ── create_report specific output structures ──────────────────────────────
    if form == "create_report":
        tickets_out = [
            {
                "ticket_id":      t["ticket_id"],
                "ticket_number":  t["ticket_number"],
                "ticket_subject": t["ticket_subject"],
                "dept_id":        t["dept_id"],
                "org_id":         t["org_id"],
                "org_name":       t["org_name"] or "",
                "user_id":        t["user_id"],
                "user_name":      t["user_name"] or "",
                "service_type":   t["service_type"],
                "appointments": [
                    {
                        "id":             a["id"],
                        "title":          a["title"] or "",
                        "start_datetime": str(a["start_datetime"]) if a["start_datetime"] else "",
                        "end_datetime":   str(a["end_datetime"]) if a["end_datetime"] else "",
                        "staff": [
                            {"staff_id": s["staff_id"], "name": s["staff_name"]}
                            for s in all_appt_staff if s["appointment_id"] == a["id"]
                        ],
                    }
                    for a in scheduled_appointments if a["ticket_id"] == t["ticket_id"]
                ],
            }
            for t in open_tickets
        ]

        report_forms_out = [
            {
                "id":   f["id"],
                "name": f["name"] or f["title"] or f"Form #{f['id']}",
                "mappings": [
                    {
                        "service_type_id": m["service_type_id"],
                        "applies_to":      m["applies_to"],
                        "is_default":      bool(m["is_default"]),
                    }
                    for m in form_mappings if m["form_id"] == f["id"]
                ],
            }
            for f in active_forms
        ]

        # Flat deduplicated staff list — each person once, with global role +
        # all dept memberships. Used for assignees multiselect (cross-dept).
        seen_staff: dict[int, dict] = {}
        for s in all_staff:
            sid = s["staff_id"]
            entry = seen_staff.setdefault(sid, {
                "staff_id":      sid,
                "name":          s["name"],
                "email":         s.get("email") or "",
                "staff_role_id": s.get("staff_role_id"),
                "dept_roles":    [],
            })
            entry["dept_roles"].append({
                "dept_id":      s["dept_id"],
                "dept_name":    next(
                    (d["name"] for d in departments if d["id"] == s["dept_id"]),
                    str(s["dept_id"]),
                ),
                "dept_role_id": s.get("role_id"),
            })
        all_staff_flat = list(seen_staff.values())

        print(
            f"  [extract] DB (create_report): {len(tickets_out)} open tickets, "
            f"{sum(len(t['appointments']) for t in tickets_out)} scheduled appointments, "
            f"{len(report_forms_out)} active forms, {len(all_staff_flat)} staff",
            file=sys.stderr,
        )

        full = {
            "customers":             customers_out,
            "inactive_customers":    inactive_customers_raw,
            "departments":           departments_out,
            "inactive_departments":  inactive_departments_raw,
            "role_definitions":      role_definitions,
            "tickets":               tickets_out,
            "inactive_tickets":      closed_tickets_raw,
            "report_forms":          report_forms_out,
            "inactive_report_forms": inactive_forms_raw,
            "staff":                 all_staff_flat,
        }
        sample = {
            "customers":             customers_out[:1],
            "inactive_customers":    inactive_customers_raw[:1],
            "departments":           sample_depts,
            "inactive_departments":  inactive_departments_raw[:1],
            "role_definitions":      role_definitions,
            "staff_access_summary":  staff_access_summary,
            "tickets":               tickets_out[:2],
            "inactive_tickets":      closed_tickets_raw[:1],
            "report_forms":          report_forms_out,
            "inactive_report_forms": inactive_forms_raw[:1],
            "staff":                 all_staff_flat,
        }
    else:
        full = {
            "customers":            customers_out,
            "inactive_customers":   inactive_customers_raw,
            "departments":          departments_out,
            "inactive_departments": inactive_departments_raw,
            "role_definitions":     role_definitions,
        }
        sample = {
            "customers":            customers_out[:1],
            "inactive_customers":   inactive_customers_raw[:1],
            "departments":          sample_depts,
            "inactive_departments": inactive_departments_raw[:1],
            "role_definitions":     role_definitions,
            "staff_access_summary": staff_access_summary,
        }

    return {
        "all":           full,
        "sample":        sample,
        "namespace_map": namespace_map,
    }


# ── Main extract function ──────────────────────────────────────────────────────

def extract(flow_id: str, subdir: str | None = None) -> dict:
    if subdir:
        manifest_path = MANIFESTS_DIR / subdir / f"{flow_id}.json"
    else:
        manifest_path = MANIFESTS_DIR / f"{flow_id}.json"

    if not manifest_path.exists():
        print(f"ERROR: manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    
    files_spec          = manifest.get("files", {})
    scenario_categories = manifest.get("scenario_categories", [])
    primary_role        = manifest.get("primary_role", "")

    frontend, backend, models, manifest_names = _build_sections(files_spec)

    server_env       = _load_server_env()
    testgen_env      = _load_testgen_env()
    test_agent_email = testgen_env.get("TEST_AGENT_EMAIL", "")
    db_result        = _query_db(server_env, form=manifest.get("form", ""),
                                 test_agent_email=test_agent_email)

    known_entities   = db_result.get("sample",        {})
    cascade_entities = db_result.get("all",            {})
    namespace_map    = db_result.get("namespace_map",  {})

    context = {
        "flow_name":           flow_id,
        "scenario_categories": scenario_categories,
        "primary_role":        primary_role,
        "files_manifest":      manifest_names,
        "frontend":            frontend,
        "backend":             backend,
        "models":              models,
        "test_data": {
            "known_entities":   known_entities,    # LLM sees this — sample per entity type
            "cascade_entities": cascade_entities,  # execute.py only — full dataset for dropdown validation
            "namespace_map":    namespace_map,     # resolved UI terminology (e.g. ticket → case)
        },
        "meta": {
            "files_read": len(manifest_names),
            "db_queried": bool(cascade_entities),
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
    args = parser.parse_args()

    out_path = Path(args.out) if args.out else TESTGEN_DIR / f"context_{args.flow}.json"

    # Compute target path for printing visibility
    target_manifest = MANIFESTS_DIR / args.subdir / f"{args.flow}.json" if args.subdir else MANIFESTS_DIR / f"{args.flow}.json"

    print(f"[extract] flow:     {args.flow}")
    if args.subdir:
        print(f"[extract] subdir:   {args.subdir}")
    print(f"[extract] manifest: {target_manifest}")
    print(f"[extract] output:   {out_path}")

    ctx = extract(args.flow, subdir=args.subdir)

    out_path.write_text(json.dumps(ctx, indent=2), encoding="utf-8")

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
