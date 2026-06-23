"""
pipeline.py — Run the full iSERV test generation pipeline for one flow.

Steps (in order):
  1. extract   — build context_{flow}.json from manifests + fixtures
  2. generate  — LLM scenario matrix → scenario_matrix_{flow}.json
  3. sanitize  — inject missing required inputs (no LLM)
  4. build     — record.py --build <flow>  (parameterise base_test from recorded file)
  5. execute   — run all scenarios against the live app
  6. report    — write Excel report

Usage:
  python testGen/pipeline.py --flow customer_create_ticket
  python testGen/pipeline.py --flow agent_create_ticket
  python testGen/pipeline.py --flow customer_create_ticket --skip extract generate
  python testGen/pipeline.py --flow customer_create_ticket --only execute report
  python testGen/pipeline.py --flow customer_create_ticket --dry-run
  python testGen/pipeline.py --flow customer_create_ticket --id TC003

Flags:
  --skip <step> [<step> ...]   Skip listed steps (useful to resume mid-pipeline)
  --only <step> [<step> ...]   Run ONLY listed steps (mutually exclusive with --skip)
  --dry-run                    Pass --dry-run to execute (print plan, no browser)
  --id <TC_ID>                 Pass --id to execute (run a single scenario)
  --no-build                   Skip the build step even if not listed in --skip
                               (use when you haven't recorded yet)

Step names: extract  generate  sanitize  build  execute  report
"""

import argparse
import subprocess
import sys
from pathlib import Path

TESTGEN_DIR  = Path(__file__).parent
PYTHON       = sys.executable

ALL_STEPS = ["extract", "generate", "sanitize", "build", "execute", "report"]


def run(cmd: list[str], step: str) -> bool:
    """Run a subprocess command. Return True on success, False on failure."""
    print(f"\n{'─' * 60}")
    print(f"  [{step.upper()}]  {' '.join(cmd)}")
    print(f"{'─' * 60}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\n  ERROR: [{step}] exited with code {result.returncode}")
        return False
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Run the full test-generation pipeline for a flow."
    )
    parser.add_argument("--flow", required=True, help="Flow name, e.g. customer_create_ticket")
    parser.add_argument("--skip", nargs="+", metavar="STEP", default=[],
                        help="Steps to skip")
    parser.add_argument("--only", nargs="+", metavar="STEP", default=[],
                        help="Run only these steps (ignores --skip)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Pass --dry-run to execute step")
    parser.add_argument("--id", metavar="TC_ID", default=None,
                        help="Pass --id to execute step (single scenario)")
    parser.add_argument("--no-build", action="store_true",
                        help="Skip build step (no recorded file yet)")
    args = parser.parse_args()

    flow = args.flow.replace("-", "_")

    # Resolve which steps to run
    if args.only:
        steps = [s for s in ALL_STEPS if s in args.only]
    else:
        skip = set(args.skip)
        if args.no_build:
            skip.add("build")
        steps = [s for s in ALL_STEPS if s not in skip]

    if not steps:
        print("  No steps selected. Exiting.")
        sys.exit(0)

    print(f"\n{'═' * 60}")
    print(f"  iSERV Test Pipeline — flow: {flow}")
    print(f"  Steps: {' → '.join(steps)}")
    print(f"{'═' * 60}")

    results: dict[str, str] = {}

    for step in steps:
        if step == "extract":
            ok = run([PYTHON, str(TESTGEN_DIR / "extract.py"), "--flow", flow], step)

        elif step == "generate":
            ok = run([PYTHON, str(TESTGEN_DIR / "generate.py"), "--flow", flow], step)

        elif step == "sanitize":
            ok = run([PYTHON, str(TESTGEN_DIR / "apply_sanitize.py"), "--flow", flow], step)

        elif step == "build":
            recorded = TESTGEN_DIR / f"base_test_{flow}_recorded.py"
            if not recorded.exists():
                print(f"\n  WARN: [{step}] recorded file not found: {recorded}")
                print(f"  Run:  python testGen/record.py --record {flow}")
                print(f"  Then re-run pipeline with --skip extract generate sanitize")
                print(f"  Skipping build and continuing...")
                results[step] = "SKIPPED (no recorded file)"
                continue
            ok = run([PYTHON, str(TESTGEN_DIR / "record.py"), "--build", flow], step)

        elif step == "execute":
            cmd = [PYTHON, str(TESTGEN_DIR / "execute.py"), "--flow", flow]
            if args.dry_run:
                cmd.append("--dry-run")
            if args.id:
                cmd += ["--id", args.id]
            ok = run(cmd, step)

        elif step == "report":
            ok = run([PYTHON, str(TESTGEN_DIR / "report.py"), "--flow", flow], step)

        else:
            print(f"  WARN: unknown step '{step}' — skipping")
            results[step] = "UNKNOWN"
            continue

        results[step] = "OK" if ok else "FAILED"

        if not ok:
            print(f"\n  Pipeline aborted at [{step}].")
            print(f"  To resume: python testGen/pipeline.py --flow {flow} --skip {' '.join(s for s in steps[:steps.index(step)])}")
            _print_summary(results, steps)
            sys.exit(1)

    _print_summary(results, steps)
    print(f"\n  Done. Report: testGen/report_{flow}.xlsx")


def _print_summary(results: dict[str, str], steps: list[str]):
    print(f"\n{'═' * 60}")
    print(f"  Pipeline Summary")
    print(f"{'─' * 60}")
    for step in steps:
        status = results.get(step, "NOT RUN")
        icon = "✓" if status == "OK" else ("⚠" if "SKIPPED" in status else "✗")
        print(f"  {icon}  {step:<12} {status}")
    print(f"{'═' * 60}")


if __name__ == "__main__":
    main()
