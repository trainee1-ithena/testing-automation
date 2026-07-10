"""
generate1.py — LLM Call 1: Scenario Skeleton Generation

Usage:
    python generate1.py --prd PRD_Create_Service_Report.md --flow create_service_report

Output:
    skeletons/scenario_skeletons_{flow}.json   — full tree (common + roles)
    skeletons/coverage_summary_{flow}.md       — human-readable tree view
    token_usage.csv                            — appended on every run
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

BASE_DIR      = Path(__file__).parent
SKELETONS_DIR = BASE_DIR / "skeletons"
TOKEN_LOG     = BASE_DIR / "token_usage.csv"

# generate1 runs on gpt-4o (16,384 completion-token ceiling) and its skeleton output fits well
# under 16k. The shared OPENAI_MAX_TOKENS env var is tuned for generate2 on gpt-4.1 (32k), so
# clamp here — otherwise a 32k request is rejected by gpt-4o ("max_tokens is too large").
MAX_TOKENS = min(int(os.getenv("OPENAI_MAX_TOKENS", "16000")), 16000)

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
            "generate1.py", flow, model,
            input_tokens, output_tokens, input_tokens + output_tokens,
            f"{input_cost:.6f}", f"{output_cost:.6f}", f"{total_cost:.6f}",
        ])
    return total_cost


# ── Shared case object schema (used in both prompts) ────────────────────────

CASE_SCHEMA = """{
  "case": "case001",
  "is_smoke": false,
  "category": "",
  "entry_point": "",
  "persona": "",
  "title": "",
  "description": "",
  "preconditions": [],
  "steps_summary": [],
  "session_count": 1,
  "locked_fields": [],
  "expected_result": "",
  "prd_references": [],
  "source": ""
}"""

CASE_FIELD_RULES = """Field rules for every case object:
- case: globally unique, sequential, zero-padded integer prefixed with "case" (case001, case002, ...).
  You will be told which number to start from.
- is_smoke: true only for the minimal set that confirms the system is functional. Default false.
- category: must match the parent scenario key
- entry_point: the entry point this case uses (from the PRD); "all_entry_points" if cross-cutting
- persona: the role performing this test (from the PRD); "any_authorized_role" for common tests
  that are not specific to one role. Exception: for direct URL access without login, use "unauthenticated"
  and place those cases under the permission or industry_best_practice category.
- session_count: integer. Set AFTER writing steps_summary. Count how many distinct "Sign in as"
  actions appear in the steps you just wrote — that is the session_count. Default 1 (only one
  sign-in). Set to 2 if steps include signing in as one role, acting, then signing in as a
  different role (e.g. "Sign out and sign in as Customer User"). Count the sign-ins, not the steps.
- locked_fields: Set AFTER writing steps_summary. Look up this case's entry_point in the
  PRD's "Locked Fields by Entry Point" table (Section 3.5) and copy the exact field names
  from that row verbatim — do not paraphrase or rename them. The table is the authoritative
  source; do not infer locked fields from prose elsewhere in the PRD. If this case uses
  "all_entry_points" or a cross-cutting entry point with no matching row, leave as [].
- title: short imperative title, max 10 words
- description: 1–2 sentences — what is being tested and why it matters
- preconditions: list of strings — system state required before the test runs
- steps_summary: list of strings — the COMPLETE test from start to finish, no data values.
  Cover every step: setup, the primary action, and full verification. This includes navigating
  to another page to verify state, checking a value appeared, or switching role to confirm
  visibility/access. Use role names from the PRD — never specific usernames or data values.
  If expected_result involves another role being checked, steps_summary must include: sign in
  as that role, navigate to where the record would appear, confirm it is present or absent or inaccessible.
- expected_result: what the system should do or show — no specific data values
- prd_references: list of rule/criteria IDs from the PRD (e.g. "BR-01", "EC-03") — [] if none
- source: "prd" or "industry_best_practice"

COMPLETENESS IS MANDATORY. Generate ALL applicable cases without exception. Do not skip,
combine, or abbreviate any case to save space. Do not omit cases you generated in a previous
run — every run must produce a complete, deterministic set. If you find yourself about to
skip something, do not — write it out in full. Incomplete output is a failure."""


# ── Prompt 1: Common tests (role-agnostic, generated once) ──────────────────

COMMON_PROMPT = f"""You are a senior QA analyst specialising in web application testing.
You will receive a PRD for a web application module. Your task is to generate the
COMMON test cases — those that apply the same way regardless of which authorized role
performs them. These are form-level and browser-level tests, not role-specific flows.

---

## CATEGORIES TO GENERATE (common only)

Generate cases for ALL of the following:

1. field_validation — one case per required, user-editable field that has NO default value,
   left blank or invalid.
   Cover every such required field described in the PRD.
   Do NOT generate a field_validation case for a required field that is pre-populated with a
   default value, or that is auto-populated / read-only: such a field can never actually be
   blank at submit time, so a "leave it empty" test is impossible to stage and would fail for
   the wrong reason. Only genuinely user-supplied-from-empty required fields qualify.

2. boundary — ONLY for a field whose length, range, or format limit is stated as a CONCRETE
   value in the PRD (e.g. "max 256 characters", "between 1 and 100")
   - One case AT the limit (should pass)
   - One case ONE UNIT above the limit (should fail)
   Cover every field that HAS such an explicit concrete limit. If the PRD does not state a
   concrete numeric/length/format limit for a field, do NOT generate a boundary case for it —
   never invent or assume a limit.

3. field_interaction — for every field that triggers a downstream effect on another field:
   - Auto-population of another field
   - Filtering of another field's options
   - Showing or hiding of other fields
   - Resetting dependent fields
   - Changes to which validation rules apply
   Create ONE case per (trigger field → target field) pair — do NOT bundle multiple
   effects into one case. Also test the reverse: clearing the trigger undoes the effect
   on its target — one case per reversal pair.

4. ui — for every interactive element on the form (buttons, toggles, dropdowns, date pickers,
   multi-selects, text areas, etc.): verify it exists, is visible, and is interactable.
   One case per distinct element type.
   Additionally, for every field that is conditionally disabled or auto-populated (read-only display), excluding all fields that conditionally auto-populated from entry point:
   verify it is in the correct disabled/read-only state when its condition is met — one case
   per such field. These are not functional tests; they confirm the UI enforces the constraint.

5. industry_best_practice — gaps not covered by the PRD:
   - Whitespace-only text inputs in required fields
   - Special characters and SQL/script injection in text fields
   - Inputs exceeding maximum length
   - Browser back/forward navigation after submission
   - Re-submission of the same form without refreshing
   - Concurrent form submissions by two users simultaneously

6. post_submission_state — one case per field/setting on this form that changes what the
   record's state or visibility is after creation (e.g. a toggle governing who can see it or
   which workflow it follows). Steps: set the field to trigger the change, then confirm the
   resulting state/visibility on every surface described in the PRD (list, sub-tab, search,
   etc.) — signing in as whichever persona's view is affected, if different from the creator.

{CASE_FIELD_RULES}

---

## OUTPUT FORMAT

Return a single JSON object with three keys:

{{
  "roles": ["Role A", "Role B", ...],
  "default_url": "http://localhost:3000/...",
  "common": {{
    "field_validation": [ {CASE_SCHEMA}, ... ],
    "boundary":         [ {CASE_SCHEMA}, ... ],
    "field_interaction": [ ... ],
    "ui":               [ ... ],
    "industry_best_practice": [ ... ],
    "post_submission_state":  [ ... ]
  }}
}}

- "roles": list of all named user personas described in the PRD. Include only actual system
  roles — do not include "unauthenticated" or similar states.
- "default_url": the base URL for common tests — use the URL from the PRD routes section that
  corresponds to the default / primary entry point for this module.

- For common cases, set persona to "any_authorized_role" (they are not role-specific).
- Do not include markdown, headings, or explanation outside the JSON.
- Return only the raw JSON object, nothing else."""


# ── Prompt 2: Role-specific tests (one call per role) ───────────────────────

ROLE_PROMPT = f"""You are a senior QA analyst specialising in web application testing.
You will receive a PRD and be told which single role (persona) to generate test cases for.
Generate ONLY role-specific test cases — those where the role's permissions, access level,
or user journey differs from other roles.

Do NOT generate field_validation, boundary, field_interaction, ui, or industry_best_practice
cases — those are generated separately as common tests.

---

## CATEGORIES TO GENERATE (role-specific only)

For the specified role, generate cases for ALL of the following:

1. happy_path — multiple cases per entry point described in the PRD.
   Cover the main valid combinations this role would realistically follow, not just one.
   Different entry points must each have their own happy path cases.

2. permission — what this role IS and IS NOT allowed to do:
   - Positive: this role successfully performs an allowed action
   - Negative: this role is blocked from a restricted action (hidden, disabled, or error)
   Also include: direct URL access without authentication (persona: "unauthenticated").

3. business_rule — how PRD business rules (BR-XX) apply to this role.
   One or more cases per rule that affects this role's experience.

4. edge_case — PRD-listed edge cases and negative scenarios relevant to this role.

5. post_submission_state — what this role sees/can do after a successful submission.

---

## HOW TO THINK

Before writing JSON:
1. Identify every entry point (journey) this role can take from the PRD.
2. For each journey, exhaustively go through each category above.
3. Do not move to the next journey until all 5 categories are covered for the current one.
4. For permission: also think about what other roles can do that this role cannot,
   and generate negative cases for those restrictions.

{CASE_FIELD_RULES}

---

## OUTPUT FORMAT

Return a single JSON object with exactly this shape:

{{
  "role": "<role name exactly as in the PRD>",
  "journeys": [
    {{
      "journey": "<entry point or user flow name>",
      "url": "<base URL from PRD routes section for this entry point>",
      "scenarios": [
        {{
          "scenario": "<category name>",
          "cases": [ {CASE_SCHEMA}, ... ]
        }}
      ]
    }}
  ]
}}

- "role" must match the role name you were given exactly.
- "journey" is the human-readable entry point from the PRD (e.g. "Create from SR List").
  Use "permission_check" for permission scenarios that are not tied to a specific entry point.
  Use "all_entry_points" for cross-cutting scenarios (e.g. direct URL without auth).
- "url" is the base URL for that journey from the PRD routes section. For "permission_check"
  or "all_entry_points" journeys, use the default URL from the PRD routes section.
- "scenario" must be one of: happy_path | permission | business_rule | edge_case | post_submission_state
- Every case object must have persona set to the role name you were given (or "unauthenticated"
  for logged-out cases).
- Do not include markdown, headings, or explanation outside the JSON.
- Return only the raw JSON object, nothing else."""


def load_prd(prd_path: str) -> str:
    path = Path(prd_path)
    if not path.exists():
        print(f"Error: PRD file not found: {prd_path}")
        sys.exit(1)
    return path.read_text(encoding="utf-8")


def call_llm(system_prompt: str, user_msg: str, model: str, flow: str,
             label: str, max_tokens: int = MAX_TOKENS) -> dict:
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0.2,
        max_tokens=max_tokens,
    )
    usage = response.usage
    cost = log_usage(flow, model, usage.prompt_tokens, usage.completion_tokens)
    print(f"  [{label}] tokens — in: {usage.prompt_tokens:,}  out: {usage.completion_tokens:,}  cost: ${cost:.4f}")
    return json.loads(response.choices[0].message.content)


def generate_common(prd_content: str, model: str, flow: str, start_id: int) -> dict:
    """Generate role-agnostic cases (field_validation, boundary, field_interaction, ui, ibp)."""
    user_msg = (
        f"Here is the PRD to analyse:\n\n{prd_content}\n\n"
        f"Start case IDs at case{start_id:03d}."
    )
    print("  [common] sending to model...")
    result = call_llm(COMMON_PROMPT, user_msg, model, flow, "common")
    return result if isinstance(result, dict) else {}


def generate_for_role(prd_content: str, role: str, model: str, flow: str, start_id: int) -> dict:
    """Generate role-specific cases for one role."""
    user_msg = (
        f"Here is the PRD to analyse:\n\n{prd_content}\n\n"
        f"Generate test cases ONLY for the '{role}' persona.\n"
        f"Start case IDs at case{start_id:03d}."
    )
    print(f"  [{role}] sending to model...")
    result = call_llm(ROLE_PROMPT, user_msg, model, flow, role)
    return result if isinstance(result, dict) else {}


def flatten_tree(tree: dict) -> list[dict]:
    """Flatten the full tree (common + roles) into a plain list of case dicts."""
    cases = []

    # common section: {"common": {"category": [cases]}}
    for category_cases in tree.get("common", {}).values():
        if isinstance(category_cases, list):
            cases.extend(c for c in category_cases if isinstance(c, dict))

    # roles section: [{"role": "...", "journeys": [{"journey": "...", "scenarios": [...]}]}]
    for role_obj in tree.get("roles", []):
        if not isinstance(role_obj, dict):
            continue
        for journey_obj in role_obj.get("journeys", []):
            if not isinstance(journey_obj, dict):
                continue
            for scenario_obj in journey_obj.get("scenarios", []):
                if not isinstance(scenario_obj, dict):
                    continue
                for c in scenario_obj.get("cases", []):
                    if isinstance(c, dict):
                        cases.append(c)

    return cases


def validate_cases(cases: list[dict]) -> list[dict]:
    required = {
        "case", "category", "entry_point", "persona",
        "title", "description", "preconditions", "steps_summary",
        "expected_result", "prd_references", "source",
    }
    valid = []
    for i, c in enumerate(cases):
        missing = required - c.keys()
        if missing:
            print(f"  Warning: case {i+1} ({c.get('case', '?')}) missing {missing} — skipping")
            continue
        valid.append(c)
    return valid


def count_cases(tree_fragment: dict) -> int:
    """Count all cases in a partial tree fragment."""
    return len(flatten_tree(tree_fragment))


def write_summary_md(tree: dict, flow: str, total: int, path: Path) -> None:
    lines = [
        f"# Coverage Summary — {flow}\n",
        f"**Total cases: {total}**\n",
        "\n## Common (role-agnostic)",
    ]

    common = tree.get("common", {})
    common_total = sum(len(v) for v in common.values() if isinstance(v, list))
    default_url = tree.get("default_url", "")
    lines[2] = f"\n## Common — role-agnostic ({common_total})"
    if default_url:
        lines.append(f"**Default URL:** `{default_url}`")

    for category, case_list in common.items():
        if not isinstance(case_list, list):
            continue
        lines.append(f"\n### {category} ({len(case_list)})")
        for c in case_list:
            if not isinstance(c, dict):
                continue
            smoke = " [smoke]" if c.get("is_smoke") else ""
            lines.append(f"- [{c.get('case', '?')}] {c.get('title', '')}{smoke}")

    for role_obj in tree.get("roles", []):
        if not isinstance(role_obj, dict):
            continue
        role = role_obj.get("role", "unknown")
        role_total = count_cases({"roles": [role_obj]})
        lines.append(f"\n## {role} ({role_total})")

        for journey_obj in role_obj.get("journeys", []):
            if not isinstance(journey_obj, dict):
                continue
            journey = journey_obj.get("journey", "unknown")
            journey_url = journey_obj.get("url", "")
            journey_total = sum(
                len(s.get("cases", [])) for s in journey_obj.get("scenarios", [])
                if isinstance(s, dict)
            )
            url_label = f" — `{journey_url}`" if journey_url else ""
            lines.append(f"\n### {journey} ({journey_total}){url_label}")

            for scenario_obj in journey_obj.get("scenarios", []):
                if not isinstance(scenario_obj, dict):
                    continue
                scenario = scenario_obj.get("scenario", "unknown")
                case_list = scenario_obj.get("cases", [])
                lines.append(f"\n#### {scenario} ({len(case_list)})")
                for c in case_list:
                    if not isinstance(c, dict):
                        continue
                    smoke = " [smoke]" if c.get("is_smoke") else ""
                    lines.append(f"- [{c.get('case', '?')}] {c.get('title', '')}{smoke}")

    path.write_text("\n".join(lines), encoding="utf-8")


def generate_scenarios(prd_content: str, model: str, flow: str) -> dict:
    """Orchestrate: 1 common call (also extracts roles) + 1 call per role. Returns merged tree."""
    tree: dict = {"common": {}, "roles": []}
    id_counter = 1

    # Common tests — also returns the roles list and default_url
    common_result = generate_common(prd_content, model, flow, id_counter)
    tree["common"] = common_result.get("common", {})
    tree["default_url"] = common_result.get("default_url", "")

    # Stamp default_url onto every common case
    for cat_key, cat_cases in tree["common"].items():
        if cat_key == "field_enumeration" or not isinstance(cat_cases, list):
            continue
        for c in cat_cases:
            if isinstance(c, dict):
                c["url"] = tree["default_url"]

    id_counter += count_cases({"common": tree["common"]})

    roles = common_result.get("roles", [])
    if not roles:
        print("Error: no roles found in PRD common response.")
        sys.exit(1)
    print(f"Roles identified: {roles}")

    # Role-specific tests (per role)
    for role in roles:
        role_result = generate_for_role(prd_content, role, model, flow, id_counter)
        # accept both {role:..., journeys:[...]} and {"Role Name": {journeys:[...]}}
        if "journeys" in role_result:
            role_obj = role_result
        elif role in role_result:
            role_obj = {"role": role, **role_result[role]}
        else:
            role_obj = {"role": role, "journeys": []}

        if "role" not in role_obj:
            role_obj["role"] = role

        # Stamp url from each journey onto its cases
        for journey_obj in role_obj.get("journeys", []):
            if not isinstance(journey_obj, dict):
                continue
            journey_url = journey_obj.get("url", tree.get("default_url", ""))
            for scenario_obj in journey_obj.get("scenarios", []):
                if not isinstance(scenario_obj, dict):
                    continue
                for c in scenario_obj.get("cases", []):
                    if isinstance(c, dict):
                        c["url"] = journey_url

        tree["roles"].append(role_obj)
        id_counter += count_cases({"roles": [role_obj]})

    return tree


def main():
    parser = argparse.ArgumentParser(description="Generate scenario skeletons from a PRD.")
    parser.add_argument("--prd",   required=True, help="Path to the PRD markdown file")
    parser.add_argument("--flow",  required=True, help="Flow name (e.g. create_service_report)")
    parser.add_argument("--model", default="gpt-4o", help="OpenAI model (default: gpt-4o)")
    args = parser.parse_args()

    prd_content = load_prd(args.prd)
    print(f"Loaded PRD: {args.prd} ({len(prd_content):,} chars)")

    tree  = generate_scenarios(prd_content, args.model, args.flow)
    cases = flatten_tree(tree)
    cases = validate_cases(cases)

    SKELETONS_DIR.mkdir(exist_ok=True)

    output_path = SKELETONS_DIR / f"scenario_skeletons_{args.flow}.json"
    output_path.write_text(json.dumps(tree, indent=2), encoding="utf-8")

    summary_path = SKELETONS_DIR / f"coverage_summary_{args.flow}.md"
    write_summary_md(tree, args.flow, len(cases), summary_path)

    print(f"\nDone. {len(cases)} cases written to {output_path.name}")
    print(f"Coverage summary → {summary_path.name}")

    from collections import Counter
    counts = Counter(c.get("category", "unknown") for c in cases)
    print("\nCategory breakdown:")
    for category, count in sorted(counts.items()):
        print(f"  {category}: {count}")


if __name__ == "__main__":
    main()
