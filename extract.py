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
    Return 1-based line number where func_name is defined.
    Matches:  function createTicket(  |  const createTicket =  |  async createTicket(
    Falls back to line 1 if not found.
    """
    pattern = re.compile(
        rf"(?:^|\s)(?:async\s+)?(?:function\s+{re.escape(func_name)}\s*\(|"
        rf"(?:const|let|var)\s+{re.escape(func_name)}\s*=)"
    )
    for i, line in enumerate(lines, start=1):
        if pattern.search(line):
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


def _query_db(env: dict) -> dict:
    """
    Connect to iSERV MySQL and fetch ALL active entity data (no row limit).
    Returns a dict with two keys:
      "all"     — complete dataset used by execute.py for cascade validation
      "sample"  — first 2 customers + 2 departments, used as known_entities for the LLM prompt
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
            # ── all active customers ────────────────────────────────────────
            cur.execute(
                f"SELECT id, name FROM {prefix}organization "
                f"WHERE delete_string = 'active' ORDER BY id"
            )
            customers = cur.fetchall()
            org_ids   = [c["id"] for c in customers]

            if org_ids:
                # ── Users per customer ──────────────────────────────────────
                placeholders = ",".join(["%s"] * len(org_ids))
                cur.execute(
                    f"SELECT id, name, org_id "
                    f"FROM {prefix}user "
                    f"WHERE org_id IN ({placeholders}) "
                    f"AND status = 1",
                    org_ids,
                )
                all_users = cur.fetchall()

                # ── Equipment per customer ──────────────────────────────────
                cur.execute(
                    f"SELECT id, provision_name, org_id, site_id "
                    f"FROM {prefix}organization_equipments "
                    f"WHERE org_id IN ({placeholders}) AND delete_string = 'active' AND status_id != 0",
                    org_ids,
                )
                all_equipment = cur.fetchall()
            else:
                all_users     = []
                all_equipment = []

            # ── all active departments ──────────────────────────────────────
            cur.execute(
                f"SELECT id, name, manager_id FROM {prefix}department "
                f"WHERE delete_string = 'active' ORDER BY id"
            )
            departments = cur.fetchall()
            dept_ids    = [d["id"] for d in departments]

            if dept_ids:
                placeholders = ",".join(["%s"] * len(dept_ids))

                # ── Staff per department (email via staff_account join) ─────
                cur.execute(
                    f"SELECT s.staff_id, s.name, sa.email, sda.dept_id, sda.role_id "
                    f"FROM {prefix}staff s "
                    f"JOIN {prefix}staff_dept_access sda ON s.staff_id = sda.staff_id "
                    f"LEFT JOIN {prefix}staff_account sa "
                    f"  ON sa.staff_id = s.staff_id AND sa.delete_string = 'active' "
                    f"WHERE sda.dept_id IN ({placeholders})",
                    dept_ids,
                )
                all_staff = cur.fetchall()

                # ── Service types per department (paranoid=true → deleted_at) ─
                cur.execute(
                    f"SELECT st.id, st.service_type AS name, std.department_id "
                    f"FROM {prefix}service_type st "
                    f"JOIN {prefix}service_type_departments std ON st.id = std.service_type_id "
                    f"WHERE std.department_id IN ({placeholders}) AND st.deletedAt IS NULL",
                    dept_ids,
                )
                all_service_types = cur.fetchall()
            else:
                all_staff         = []
                all_service_types = []

    finally:
        conn.close()

    # ── Shape into entity collections ─────────────────────────────────────────
    customers_out = [
        {
            "id":        c["id"],
            "name":      c["name"],
            "users":     [u for u in all_users     if u["org_id"] == c["id"]],
            "equipment": [e for e in all_equipment if e["org_id"] == c["id"]],
        }
        for c in customers
    ]

    departments_out = [
        {
            "id":            d["id"],
            "name":          d["name"],
            "manager_id":    d["manager_id"],
            "staff":         [s for s in all_staff         if s["dept_id"] == d["id"]],
            "service_types": [s for s in all_service_types if s["department_id"] == d["id"]],
        }
        for d in departments
    ]

    print(
        f"  [extract] DB: {len(customers_out)} customers, {len(departments_out)} departments",
        file=sys.stderr,
    )

    full = {"customers": customers_out, "departments": departments_out}

    # Small sample for the LLM prompt — just enough to generate realistic inputs.
    # The full dataset is stored separately and never sent to the LLM.
    sample = {
        "customers":   customers_out[:2],
        "departments": departments_out[:2],
    }

    return {"all": full, "sample": sample}


# ── Main extract function ──────────────────────────────────────────────────────

def extract(flow_id: str) -> dict:
    manifest_path = MANIFESTS_DIR / f"{flow_id}.json"
    if not manifest_path.exists():
        print(f"ERROR: manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    files_spec          = manifest.get("files", {})
    scenario_categories = manifest.get("scenario_categories", [])

    frontend, backend, models, manifest_names = _build_sections(files_spec)

    server_env = _load_server_env()
    db_result  = _query_db(server_env)   # {"all": {...}, "sample": {...}}

    # known_entities  — small sample, included in the LLM prompt by generate.py
    # cascade_entities — full dataset, read only by execute.py for cascade validation
    known_entities   = db_result.get("sample", {})
    cascade_entities = db_result.get("all",    {})

    context = {
        "flow_name":           flow_id,
        "scenario_categories": scenario_categories,
        "files_manifest":      manifest_names,
        "frontend":            frontend,
        "backend":             backend,
        "models":              models,
        "test_data": {
            "known_entities":   known_entities,    # 2 customers, 2 depts — LLM sees this
            "cascade_entities": cascade_entities,  # all records — execute.py only, never in LLM prompt
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
        "--out", default=None,
        help="Output path (default: testGen/context_{flow}.json)"
    )
    args = parser.parse_args()

    out_path = Path(args.out) if args.out else TESTGEN_DIR / f"context_{args.flow}.json"

    print(f"[extract] flow:     {args.flow}")
    print(f"[extract] manifest: {MANIFESTS_DIR / args.flow}.json")
    print(f"[extract] output:   {out_path}")

    ctx = extract(args.flow)

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
