"""
Standalone script: re-apply sanitize_scenario_inputs to an existing scenario matrix
without re-running the LLM.

Usage:
  python testGen/apply_sanitize.py --flow agent_create_ticket
  python testGen/apply_sanitize.py --flow customer_create_ticket
"""
import argparse
import json
import sys
from pathlib import Path

TESTGEN_DIR = Path(__file__).parent
sys.path.insert(0, str(TESTGEN_DIR.parent))

from testGen.generate import sanitize_scenario_inputs, resolve_second_session_keys, sanitize_multi_session_inputs

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--flow", required=True)
    args = parser.parse_args()

    matrix_path = TESTGEN_DIR / f"scenario_matrix_{args.flow}.json"
    ctx_path    = TESTGEN_DIR / f"context_{args.flow}.json"

    matrix   = json.loads(matrix_path.read_text(encoding="utf-8-sig"))
    ctx      = json.loads(ctx_path.read_text(encoding="utf-8-sig"))
    entities = ctx.get("test_data", {}).get("known_entities", {})

    before = {s["id"]: dict(s.get("inputs") or {}) for s in matrix["scenarios"]}
    matrix["scenarios"] = sanitize_scenario_inputs(matrix["scenarios"], matrix["inventory_text"], entities)

    # Resolve second_session.key from staff emails + .env named credentials
    matrix["scenarios"] = resolve_second_session_keys(
        matrix["scenarios"], entities, TESTGEN_DIR / ".env"
    )

    # Enforce that inputs are consistent with each scenario's business_rule + expected_outcome
    matrix["scenarios"] = sanitize_multi_session_inputs(matrix["scenarios"], entities)

    for s in matrix["scenarios"]:
        sid  = s["id"]
        now  = s.get("inputs") or {}
        was  = before.get(sid, {})
        added = {k: v for k, v in now.items() if k not in was}
        if added:
            print(f"  {sid} ({s['category']}): injected {list(added.keys())}")
        ss = s.get("second_session") or {}
        if ss.get("key"):
            print(f"  {sid}: second_session.key = {ss['key']!r} ({ss.get('name', '')})")

    matrix_path.write_text(json.dumps(matrix, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved -> {matrix_path}")

if __name__ == "__main__":
    main()
