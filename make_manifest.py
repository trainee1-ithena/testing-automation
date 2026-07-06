"""
make_manifest.py — Convert a plain template into a manifest JSON for extract.py

Usage:
    python make_manifest.py path/to/template.txt
    python make_manifest.py path/to/template.txt --out path/to/output.json
    python make_manifest.py path/to/template.txt --dry-run

Template format:
    flow: create_service_report
    subdir: createReport

    [frontend]
    iserv_portal/src/Components/CreateReportForm/CreateReportForm.js | full
    iserv_portal/src/Components/TicketServiceReports/TicketServiceReports.js | slice | customButtons
    iserv_portal/src/Context/AuthContext.js | auto

    [backend]
    iserv_server/src/controllers/service_report.controller.js | slice | getServiceReportFormOptions
    iserv_server/src/routes/service_report.route.js | full

    [models]
    iserv_server/src/models/service_report_header.model.js | full

Each line: path | read_mode [| slice_target]
  full  — read entire file
  slice — read only around the named function (slice_target required)
  auto  — read full if ≤300 lines, else header + first 150 lines of body
"""

import argparse
import json
import sys
from pathlib import Path

TESTGEN_DIR   = Path(__file__).parent
MANIFESTS_DIR = TESTGEN_DIR / "manifests"

VALID_READ_MODES = {"full", "slice", "auto"}
VALID_SECTIONS   = {"frontend", "backend", "models"}


def parse_template(text: str) -> dict:
    flow    = ""
    subdir  = ""
    section = None
    entries: dict[str, list] = {"frontend": [], "backend": [], "models": []}

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        if line.lower().startswith("flow:"):
            flow = line.split(":", 1)[1].strip()
            continue

        if line.lower().startswith("subdir:"):
            subdir = line.split(":", 1)[1].strip()
            continue

        if line.startswith("[") and line.endswith("]"):
            sec = line[1:-1].strip().lower()
            if sec not in VALID_SECTIONS:
                print(f"WARNING: unknown section [{sec}] — skipping", file=sys.stderr)
                section = None
            else:
                section = sec
            continue

        if section is None:
            continue

        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            print(f"WARNING: missing '|' — skipping line: {line!r}", file=sys.stderr)
            continue

        path      = parts[0]
        read_mode = parts[1].lower()

        if read_mode not in VALID_READ_MODES:
            print(f"WARNING: invalid read_mode {read_mode!r} — skipping line: {line!r}", file=sys.stderr)
            continue

        entry: dict = {"path": path, "read_mode": read_mode}

        if read_mode == "slice":
            if len(parts) < 3 or not parts[2]:
                print(f"WARNING: slice requires a function name — skipping line: {line!r}", file=sys.stderr)
                continue
            entry["slice_target"] = parts[2]

        entries[section].append(entry)

    return {"flow": flow, "subdir": subdir, "entries": entries}


def build_manifest(parsed: dict) -> dict:
    entries  = parsed["entries"]
    manifest: dict = {"files": {}}

    if entries["frontend"]:
        manifest["files"]["frontend"] = {"files": entries["frontend"]}

    if entries["backend"]:
        manifest["files"]["backend"] = {"files": entries["backend"]}

    if entries["models"]:
        manifest["files"]["models"] = entries["models"]

    return manifest


def main():
    parser = argparse.ArgumentParser(
        description="Convert a manifest template into extract.py manifest JSON"
    )
    parser.add_argument("template",   help="Path to the template .txt file")
    parser.add_argument("--out",      default=None, help="Override output path")
    parser.add_argument("--dry-run",  action="store_true", help="Print JSON without writing")
    args = parser.parse_args()

    template_path = Path(args.template)
    if not template_path.exists():
        print(f"ERROR: template not found: {template_path}", file=sys.stderr)
        sys.exit(1)

    parsed = parse_template(template_path.read_text(encoding="utf-8"))
    flow   = parsed["flow"]
    subdir = parsed["subdir"]

    if not flow:
        print("ERROR: template must include 'flow: <name>'", file=sys.stderr)
        sys.exit(1)

    manifest = build_manifest(parsed)
    json_str = json.dumps(manifest, indent=2)

    if args.dry_run:
        print(json_str)
        return

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
    elif subdir:
        out_dir  = MANIFESTS_DIR / subdir
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{flow}.json"
    else:
        MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = MANIFESTS_DIR / f"{flow}.json"

    out_path.write_text(json_str, encoding="utf-8")
    print(f"Manifest written → {out_path}")
    print(f"  frontend : {len(parsed['entries']['frontend'])} file(s)")
    print(f"  backend  : {len(parsed['entries']['backend'])} file(s)")
    print(f"  models   : {len(parsed['entries']['models'])} file(s)")


if __name__ == "__main__":
    main()
