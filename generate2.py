"""
generate2.py — LLM Call 2: Populate Scenario Matrix

Reads scenario skeletons + code/DB context → writes fully populated
scenario matrix, recordings checklist, and flagged conflicts.

Usage:
    python generate2.py --flow create_service_report

    Paths are resolved automatically from the folder structure:
      skeletons/scenario_skeletons_{flow}.json   (input)
      context/context_{flow}.json                (input)
      matrices/scenario_matrix_{flow}.json       (output)
      matrices/recordings_needed_{flow}.md       (output)
      matrices/flagged_conflicts_{flow}.md        (output, if conflicts found)
      token_usage.csv                            (appended)

    Override any path explicitly:
      --skeletons path/to/skeletons.json
      --context   path/to/context.json
"""

import argparse
import csv
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Windows consoles default to cp1252, which can't encode the arrows/dashes in
# progress output — a print() crash after the LLM calls already succeeded once
# lost the recordings files. Same guard as record.py/execute.py.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

BASE_DIR      = Path(__file__).parent
SKELETONS_DIR = BASE_DIR / "skeletons"
CONTEXT_DIR   = BASE_DIR / "context"
MATRICES_DIR  = BASE_DIR / "matrices"
MANIFESTS_DIR = BASE_DIR / "manifests"

# 32000 = gpt-4.1's max output. 16000 truncated a 20-skeleton batch's JSON mid-string.
MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "32000"))


class TruncatedResponse(Exception):
    """Model hit max_tokens mid-JSON — almost always a repetition loop on one
    skeleton in the batch, not a legitimately oversized response. Caught by the
    populate loop, which splits the batch and retries instead of aborting."""

MODEL_COST_RATES = {
    "gpt-4o":       {"prompt": 0.0025,  "completion": 0.01},
    "gpt-4o-mini":  {"prompt": 0.00015, "completion": 0.0006},
    "gpt-4.1":      {"prompt": 0.002,   "completion": 0.008},
    "gpt-4.1-mini": {"prompt": 0.0004,  "completion": 0.0016},
    "gpt-4-turbo":  {"prompt": 0.01,    "completion": 0.03},
    "gpt-4":        {"prompt": 0.03,    "completion": 0.06},
}

TOKEN_LOG = Path(__file__).parent / "token_usage.csv"


def get_cost_rates(model: str) -> dict:
    prompt_rate = os.getenv("OPENAI_MODEL_PROMPT_COST")
    completion_rate = os.getenv("OPENAI_MODEL_COMPLETION_COST")
    if prompt_rate and completion_rate:
        try:
            return {"prompt": float(prompt_rate), "completion": float(completion_rate)}
        except ValueError:
            pass
    key = model.split(":")[0].split("/")[0]
    return MODEL_COST_RATES.get(key, {"prompt": 0.0, "completion": 0.0})


def log_usage(flow: str, model: str, input_tokens: int, output_tokens: int,
              cached_tokens: int = 0) -> float:
    rates = get_cost_rates(model)
    billable_input = input_tokens - cached_tokens
    input_cost  = (billable_input  / 1000) * rates["prompt"]
    cached_cost = (cached_tokens   / 1000) * rates["prompt"] * 0.5
    output_cost = (output_tokens   / 1000) * rates["completion"]
    total_cost  = input_cost + cached_cost + output_cost

    write_header = not TOKEN_LOG.exists()
    with TOKEN_LOG.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow([
                "timestamp", "script", "flow", "model",
                "input_tokens", "cached_tokens", "output_tokens", "total_tokens",
                "input_cost_usd", "cached_cost_usd", "output_cost_usd", "total_cost_usd",
            ])
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "generate2.py",
            flow,
            model,
            input_tokens,
            cached_tokens,
            output_tokens,
            input_tokens + output_tokens,
            f"{input_cost:.6f}",
            f"{cached_cost:.6f}",
            f"{output_cost:.6f}",
            f"{total_cost:.6f}",
        ])
    return total_cost


POPULATE_PROMPT = """You are a senior QA automation engineer. You will receive a batch of scenario skeletons and a context object containing source code, known DB entities, and a namespace map.

Your job: for each scenario in the batch, output ONLY the fields that need to be filled in — do not repeat fields that come from the skeleton.

## FIELDS TO OUTPUT PER SCENARIO

Return a JSON object with one key "populated": an array where each entry uses EXACTLY ONE of these three shapes:

Single-session (session_count == 1 or absent):
{
  "case": "case001",
  "inputs": {},
  "expected_result": "",
  "expected_outcome": "",
  "note": null
}

Multi-session (session_count > 1) — MANDATORY when session_count > 1, no exceptions:
{
  "case": "case001",
  "sessions": [
    { "persona": "", "inputs": {}, "expected_result": "" },
    { "persona": "", "inputs": {}, "expected_result": "" }
  ],
  "expected_outcome": "",
  "note": null
}

Skip (required data not found in known_entities):
{
  "case": "case001",
  "skip": true,
  "skip_reason": "no closed tickets found in known_entities",
  "expected_outcome": "",
  "note": null
}

For multi-session: one entry per distinct login in steps_summary order. Each session's inputs follows the same rules as single-session inputs. Each session's expected_result is the assertion for that specific session only.

### field eligibility — decide this BEFORE resolving any value
Each scenario skeleton carries its own `locked_fields`: field names that PRD's description
of THAT case's entry_point states are auto-populated/read-only/pre-selected-and-locked.
Treat it as authoritative and case-specific — a field locked for one case's entry point may
be freely user-set for a different case with a different entry point. These field names can
never appear as keys in that case's `inputs` (or any of its sessions' `inputs`).

For any field NOT in that case's `locked_fields`, classify it yourself from the frontend
source as either user-set (the user directly types or selects it) or auto-derived (auto-
populated, read-only, reset/overwritten when another field changes, or otherwise locked by
this case's entry point). Only user-set fields may become keys in `inputs`.

Eligibility overrides every other rule below, including "required fields need real values"
in the field_validation rule — a field being required does not make it user-set. Likewise,
known_entities having a real value for a field does not make it eligible. If the PRD and the
source code disagree about whether a field is locked, the PRD wins — `locked_fields` (PRD-
derived) always takes precedence over what the code appears to do.

If PRD sections disagree with each other about whether the user fills a field at this
case's entry point, the case's own steps_summary is authoritative: a field the steps
explicitly fill or select is user-set for this case and must appear in inputs.

### composite entry points (host forms)
Some entry points open the form under test from INSIDE another form (a "host" form). If the
steps fill/select host-form fields, those are inputs too:
- Identify the host form's OWN source component (from the entry_point name / steps) and use its
  field names exactly — its formData keys / name= attributes / required-field list. ONE key per
  field: never emit two keys for the same field (a synonym, or a singular/plural variant), and
  never a field that exists only on the record's STANDALONE form (a different component), not
  this host form. Include the mode-select that reveals the record's sub-fields.
- locked_fields / "only X and Y are user-selected" describe the record's OWN fields — never the
  host form's required fields. Keep inputs FLAT; use display labels, not the codes the form submits.

### inputs
Flat object of field_name → value, containing only user-set fields (per the eligibility
check above).

Resolve each value by checking sources in this order — stop at the first that applies:
0. field_options — pre-built dropdown options for DB-backed fields. If `field_options` in the
   context contains a key matching the field name, use the EXACT `label` string from one of
   its entries as the input value. Do NOT construct the label yourself from known_entities —
   field_options already has the correctly formatted labels exactly as the UI renders them.
   Only pick labels from entries where `value` is a valid record ID (never null).
1. Frontend source code — authoritative for option text/labels defined in the UI itself: a
   plain string literal, or a template combining fixed text with a label-lookup call (any
   function that takes a key and returns a display term). Plain literals are used as-is.
   Templated labels: resolve the lookup call via namespace_map (look up the key, apply
   whatever transform the call requests), then splice the result into the template — output
   the full reconstructed string, not the raw namespace_map value alone. Check every
   select/dropdown/radio field for this pattern; it isn't limited to one field.
2. known_entities — for fields whose options are DB records the frontend fetches (tickets,
   organizations, staff, appointments, etc.). Use the value exactly as it appears.
3. namespace_map directly — for fields whose value is itself a plain namespace_map entry.
Stop at the first source that defines the field. If none do, use the skip shape — never
invent IDs, ticket numbers, names, or any other values.

- For select/multiselect: use the exact display label as it renders in the DOM, not an ID,
  resolved per the priority order above — never from the PRD's prose description of the
  option, which may be paraphrased or incomplete. The value the form actually submits (e.g.
  a short code) may differ from the visible label — inputs must contain the label, not the
  submitted value.
- For text fields: use a realistic value matching the field's expected format
- For date/datetime/time-zone fields with no source-defined options: use valid near-today
  values in the format the UI accepts. These are not data-dependent — they are exempt from
  the no-fabrication rule, like free-text values.
- For validation failure scenarios: use the value that triggers the failure (blank, whitespace-only, too long, wrong format)
- For permission/blocked scenarios: inputs reflect the point at which access is denied — do not populate fields never reached
- For field_interaction scenarios: only include the trigger field, not the target
- If the case's expected_result or prd_references depend on a specific value of a required
  field (e.g. a toggle whose setting the cited business rule is about), inputs must set that
  field to the value that produces the described outcome — don't omit it or leave it at an
  implicit default.
- For field_validation scenarios: only the field being tested should be blank or invalid; all
  other required *user-set* fields must have real values from known_entities. Required
  auto-derived fields stay excluded per the eligibility check above.
- For boundary scenarios: fill the constrained field exactly at / one past the limit and all
  other required user-set fields with real values. If that field is auto-derived/system-generated
  (never user-typed, e.g. an auto-assigned number), skip — it can't be exercised through the form.
- For "exceeding maximum length" / over-length input scenarios: emit a real string of at most
  ~300 characters — a value modestly past the documented limit exercises the length check just
  as well as a huge one. NEVER emit an unbounded or thousands-of-characters string, and NEVER a
  placeholder token (e.g. "<over-max-length value>"); a genuine ~300-character string is both
  safe (no runaway) and sufficient.

### expected_result
Replace the abstract skeleton version with a specific, testable one:
- Success: exact status set, exact UI element or text that confirms it
- Failure: exact error message or UI indicator, which field it appears on
- Cascade: exact field that updates, what value or type it takes
- Permission blocked: exact element hidden/disabled or exact error shown

### expected_outcome
Judge the form's PRIMARY action, not whether the assertion holds:
- "pass" — the record is created/submitted successfully.
- "fail" — the action is correctly prevented (validation error, permission/rule block,
  control hidden/disabled, redirect, or "Not Authorized"). Every negative scenario is "fail".

### note
null for all scenarios except PRD-conflict ones (those are handled separately).

## RULES
- Output every case id from the batch — do not skip any
- MANDATORY: For every scenario where session_count > 1, you MUST use the multi-session shape.
  Using a flat `inputs` field (even empty `{}`) for a multi-session scenario is an error.
- MANDATORY: Every input value must be traceable to known_entities, namespace_map, or a label
  literally defined in the source code (including labels reconstructed from a template that
  combines source-code text with a namespace_map lookup, per the select/multiselect rule above).
  If required data is absent, use the skip shape — never fabricate. "Absent" includes needing an
  entity in a STATE no known_entities record has (e.g. a closed ticket when all are active): skip
  with a reason; don't substitute a normal record or leave inputs blank to let it fail.
- MANDATORY: inputs may never contain a key from locked_fields, or any other field you
  classify as auto-derived per the field eligibility check above — even when the PRD lists
  it as required and even when known_entities has a real value for it.
- Return only the raw JSON object, nothing else"""


ANALYZE_PROMPT = """You are a senior QA automation engineer. You will receive:
1. Source code (frontend, backend, models)
2. A list of case ids already covered by the skeleton matrix
3. known_entities and namespace_map for test data
4. The next available case id offset to use

Your job: read the source code carefully and find validation rules, access checks,
business logic, and field behaviours NOT already covered by the skeleton scenarios.

## FOR EACH NEW RULE FOUND

If the rule does NOT conflict with the PRD:
- Add a new scenario to new_scenarios
- Set source: "code_analysis"
- Fill all skeleton fields (category, entry_point, persona, title, description,
  preconditions, steps_summary, expected_result, prd_references, session_count)
- Leave locked_fields as [] — inputs and expected_outcome are resolved in a
  separate grounding pass and must NOT appear in your output

## category — MUST be one of the following exact strings (the same taxonomy
generate1.py uses for the skeleton matrix). Never invent a new category name —
downstream tooling (the populate pass's field-eligibility rules, and the test
runner's per-category execution logic) key off these exact strings and will
silently mishandle anything else:

  field_validation | boundary | field_interaction | ui | industry_best_practice
  | happy_path | permission | business_rule | edge_case | post_submission_state

Pick whichever is the closest match to the rule you found — e.g. a length/format
check you noticed in the code -> field_validation or boundary; an access-control
check -> permission; a business rule enforced in code but not in the PRD's
common-case list -> business_rule.

## companion fields — a new scenario must be independently runnable
Every scenario needs enough inputs to actually reach the behaviour being tested,
not just the one field the rule is about. If this flow's form requires other
fields to be filled before the field under test is even reachable (e.g. a
required dropdown that must be set before a text field renders), list those
field names in the scenario the same way generate1.py's field_validation cases
do — the populate pass fills them from known_entities as long as the scenario's
own category is one of the exact strings above (its field-eligibility rules are
keyed off category, so an unrecognized category silently skips this).

If the rule CONFLICTS with the PRD:
- Write the scenario based on what the PRD says is correct — NOT what the code does
- Set source: "code_analysis"
- Set note: "PRD conflict flagged — test expected to fail until codebase is corrected"
- Add an entry to flagged_conflicts

The PRD is always the source of truth.

## OUTPUT FORMAT

Return a JSON object with exactly two keys:

{
  "new_scenarios": [
    {
      "case": "case001",
      "is_smoke": false,
      "category": "",
      "entry_point": "",
      "locked_fields": [],
      "persona": "",
      "title": "",
      "description": "",
      "preconditions": [],
      "steps_summary": [],
      "session_count": 1,
      "expected_result": "",
      "prd_references": [],
      "source": "code_analysis",
      "note": null
    }
  ],
  "flagged_conflicts": [
    {
      "case": "",
      "description": "",
      "prd_rule": "",
      "code_behavior": "",
      "recommendation": "Fix in code — PRD takes precedence"
    }
  ]
}

If no new scenarios found, new_scenarios must be [].
If no conflicts found, flagged_conflicts must be [].
Return only the raw JSON object, nothing else."""


# 12 keeps each batch's JSON output comfortably under MAX_TOKENS (20 overran 16k). The large
# static prefix is prompt-cached across batches, so more/smaller batches cost little extra.
BATCH_SIZE = int(os.getenv("GENERATE2_BATCH_SIZE", "12"))


def load_skeletons(path: Path) -> list[dict]:
    """Flatten the skeleton tree into a plain list of case dicts.

    Handles three formats:
      - New:    {"common": {"category": [cases]}, "roles": [{"role":..., "journeys":[...]}]}
      - Legacy: {"role_name": {"journey": {"category": [cases]}}}
      - Flat:   [case, ...]
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data

    cases = []

    # New format
    if "common" in data or "roles" in data:
        for cat_key, category_cases in data.get("common", {}).items():
            if cat_key == "field_enumeration":
                continue
            if isinstance(category_cases, list):
                cases.extend(c for c in category_cases if isinstance(c, dict))
        for role_obj in data.get("roles", []):
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

    # Legacy format: role → journey → category → [cases]
    for journeys in data.values():
        if not isinstance(journeys, dict):
            continue
        for categories in journeys.values():
            if not isinstance(categories, dict):
                continue
            for case_list in categories.values():
                if isinstance(case_list, list):
                    cases.extend(c for c in case_list if isinstance(c, dict))
    return cases


def call_llm(system_prompt: str, user_message: str, model: str, flow: str, label: str) -> dict:
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_message},
        ],
        temperature=0.2,
        max_tokens=MAX_TOKENS,
    )
    usage = response.usage
    cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    cost = log_usage(flow, model, usage.prompt_tokens, usage.completion_tokens, cached)
    cache_str = f"  cached: {cached:,}" if cached else ""
    print(f"  [{label}] tokens — in: {usage.prompt_tokens:,}{cache_str}  out: {usage.completion_tokens:,}  cost: ${cost:.4f}")
    # A "length" finish means the model was cut off at max_tokens mid-JSON — the parse below
    # would fail with a cryptic "Unterminated string". Surface the real cause + the fix.
    finish = response.choices[0].finish_reason
    if finish == "length":
        raise TruncatedResponse(
            f"[{label}] response truncated at max_tokens ({MAX_TOKENS})."
        )
    return json.loads(response.choices[0].message.content)


_ENTITY_STR_LIMIT = 300


def _slim_entities(value, limit: int = _ENTITY_STR_LIMIT):
    """
    Truncate long string fields inside sample entities before sending them to
    the LLM. Entity rows carry whole HTML descriptions, permission blobs, event
    logs etc. that ballooned a 5-row ticket sample to ~54k chars after a fresh
    extract — enough to push the populate payload past the model's context
    window. Input values are short display labels (subjects, names, option
    text), so a 300-char cap never touches anything value resolution needs.
    """
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…[truncated]"
    if isinstance(value, list):
        return [_slim_entities(v, limit) for v in value]
    if isinstance(value, dict):
        return {k: _slim_entities(v, limit) for k, v in value.items()}
    return value


def populate_batch(batch: list[dict], context: dict, model: str, flow: str, batch_num: int) -> list[dict]:
    """Populate one batch of skeletons — returns list of delta dicts {scenario_id, flow_type, inputs, ...}."""
    test_data = context.get("test_data", {})

    # Static content first, varying skeleton batch last — keeps the large shared prefix
    # (source_code/known_entities/namespace_map, identical across every batch in this run)
    # eligible for OpenAI's automatic prompt caching instead of busting it every call.
    # Compact separators: indent=2 added tens of thousands of pure-whitespace
    # tokens to an already context-window-sized payload.
    user_msg = json.dumps({
        "source_code": {
            "frontend": context.get("frontend", {}),
            "backend":  context.get("backend", {}),
            "models":   context.get("models", {}),
        },
        "known_entities":     _slim_entities(test_data.get("known_entities", {})),
        "field_options":      test_data.get("field_options", {}),
        "namespace_map":      test_data.get("namespace_map", {}),
        "scenario_skeletons": batch,
    }, separators=(",", ":"))

    result = call_llm(POPULATE_PROMPT, user_msg, model, flow, f"batch {batch_num}")
    return result.get("populated", [])


def populate_batch_resilient(batch: list[dict], context: dict, model: str, flow: str, batch_num) -> list[dict]:
    """populate_batch with split-retry.

    A max_tokens truncation is a repetition loop triggered by one skeleton in the
    batch plus sampling luck — halving the batch drops that skeleton's context and
    breaks the cycle. Recurse down to a single case; a lone case that *still* runs
    away is flagged skip (with a note) so one bad scenario can never abort the run.
    """
    try:
        return populate_batch(batch, context, model, flow, batch_num)
    except TruncatedResponse:
        if len(batch) == 1:
            cid = batch[0].get("case", batch[0].get("scenario_id", "?"))
            print(f"  [batch {batch_num}] case {cid} looped past max_tokens on its own — "
                  f"flagging skip so the run finishes; re-run generate2 to retry it.")
            return [{
                "case": cid,
                "inputs": {},
                "expected_result": "",
                "expected_outcome": "",
                "skip": True,
                "skip_reason": "populate output looped past max_tokens (isolated single-case runaway)",
                "note": "auto-flagged by split-retry — re-run generate2 to retry this case",
            }]
        mid = len(batch) // 2
        print(f"  [batch {batch_num}] truncated — splitting {len(batch)} → "
              f"{mid}+{len(batch) - mid} and retrying")
        left  = populate_batch_resilient(batch[:mid], context, model, flow, f"{batch_num}a")
        right = populate_batch_resilient(batch[mid:], context, model, flow, f"{batch_num}b")
        return left + right


def analyze_code(context: dict, covered_ids: list[str], next_id: int, model: str, flow: str) -> dict:
    """Single code-analysis call — returns {new_scenarios, flagged_conflicts}."""
    test_data = context.get("test_data", {})
    user_msg = json.dumps({
        "source_code": {
            "frontend": context.get("frontend", {}),
            "backend":  context.get("backend", {}),
            "models":   context.get("models", {}),
        },
        "known_entities":      _slim_entities(test_data.get("known_entities", {})),
        "namespace_map":       test_data.get("namespace_map", {}),
        "already_covered_ids": covered_ids,
        "next_case_id":        f"case{next_id:03d}",
    }, separators=(",", ":"))

    return call_llm(ANALYZE_PROMPT, user_msg, model, flow, "code analysis")


def write_recordings_md(recordings: list[dict], path: Path) -> None:
    lines = ["# Recordings Needed\n",
             "Record each flow below once using `record.py`. "
             "Name each output file `base_test_{flow}_{flow_type}.py`.\n"]

    for i, rec in enumerate(recordings, 1):
        lines.append(f"## {i}. `{rec['flow_type']}` ({rec.get('role', '')})")
        url = rec.get("start_url") or rec.get("url", "")
        if url:
            lines.append(f"**Start URL:** `{url}`\n")
        lines.append(f"{rec['description']}\n")
        lines.append("**Steps to record:**")
        for step in rec.get("steps", []):
            lines.append(f"- {step}")
        lines.append(f"\n**Covers scenarios:** {', '.join(rec.get('scenario_ids', []))}\n")

    path.write_text("\n".join(lines), encoding="utf-8")


def write_conflicts_md(conflicts: list[dict], path: Path) -> None:
    lines = ["# Flagged Code Conflicts\n",
             "The following behaviours were found in the codebase that conflict "
             "with the PRD. The test scenarios are written to the PRD standard — "
             "they will fail until the code is corrected.\n"]

    for c in conflicts:
        lines.append(f"## {c.get('case', c.get('scenario_id', '?'))} — {c.get('description', '')}")
        lines.append(f"**PRD rule:** {c.get('prd_rule', '')}")
        lines.append(f"**Code behaviour:** {c.get('code_behavior', '')}")
        lines.append(f"**Recommendation:** {c.get('recommendation', '')}\n")

    path.write_text("\n".join(lines), encoding="utf-8")


def merge_delta(skeleton: dict, delta: dict) -> dict:
    """Merge populated fields from delta into the skeleton dict.

    session_count == 1 (or absent): flat "inputs" + "expected_result", as before.
    session_count > 1: the LLM returns "sessions" — one {persona, inputs, expected_result}
    per login — which must be written through as-is instead of dropped. "inputs" and
    "expected_result" stay absent at the top level in that case; per-session values live
    in "sessions" only, so there's one unambiguous place to look.
    """
    merged = {
        **skeleton,
        "expected_outcome": delta.get("expected_outcome", ""),
        "note":             delta.get("note", None),
    }
    if "sessions" in delta:
        merged["sessions"] = delta["sessions"]
    else:
        merged["inputs"]          = delta.get("inputs", {})
        merged["expected_result"] = delta.get("expected_result", skeleton.get("expected_result", ""))
    return merged


def _slugify(text: str) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _path_from_url(url: str) -> str:
    """'http://localhost:3000/service_reports' → '/service_reports'"""
    try:
        from urllib.parse import urlparse
        return urlparse(url).path.rstrip("/") or "/"
    except Exception:
        return url


def assign_recordings(matrix: list[dict], skeletons_path: Path, flow: str) -> list[dict]:
    """
    Derive one happy-path recording per unique journey URL from the skeleton tree.
    No LLM call, no manual config — the skeletons already have every journey + URL.

    Matching: each case carries a `url` field stamped by generate1.py from its
    journey. Cases are bucketed by that URL → one recording per distinct URL.
    flow_type is slugified from the journey name (e.g. "Create from Ticket" →
    "{flow}_from_ticket"). Common cases share the default_url journey.
    """
    data = json.loads(skeletons_path.read_text(encoding="utf-8"))
    default_url = data.get("default_url", "")

    # Collect unique URLs in encounter order: default first, then role journeys
    # url → {journey_label, role, start_path}
    url_meta: dict[str, dict] = {}

    if default_url:
        url_meta[default_url] = {
            "journey": "default",
            "role": "any_authorized_role",
            "start_path": _path_from_url(default_url),
        }

    for role_obj in data.get("roles", []):
        role = role_obj.get("role", "any_authorized_role")
        for journey_obj in role_obj.get("journeys", []):
            url = journey_obj.get("url") or default_url
            if not url or url in url_meta:
                continue
            url_meta[url] = {
                "journey": journey_obj.get("journey", ""),
                "role": role,
                "start_path": _path_from_url(url),
            }

    # Bucket cases by their url field (fall back to entry_point)
    from collections import defaultdict as _dd
    url_cases: dict[str, list[str]] = _dd(list)
    for sc in matrix:
        url = sc.get("url") or sc.get("entry_point") or default_url
        url_cases[url].append(sc.get("case", sc.get("scenario_id", "")))

    # Build one recording per URL
    recordings = []
    for url, meta in url_meta.items():
        journey = meta["journey"]
        suffix = _slugify(journey) if journey != "default" else _slugify(_path_from_url(url).strip("/"))
        flow_type = f"{flow}_from_{suffix}"
        recordings.append({
            "flow_type": flow_type,
            "role": meta["role"],
            "description": f"Happy path recording for '{journey}' entry point.",
            "start_url": meta["start_path"],
            "steps": [
                "Navigate to the form",
                "Fill all visible fields with any valid values",
                "Submit the form",
            ],
            "scenario_ids": url_cases.get(url, []),
        })

    # Any case whose url didn't match a known journey falls into the default bucket
    known_urls = set(url_meta.keys())
    unmatched = [
        sc.get("case", "") for sc in matrix
        if (sc.get("url") or sc.get("entry_point") or "") not in known_urls
    ]
    if unmatched and recordings:
        recordings[0]["scenario_ids"].extend(unmatched)

    return recordings


def main():
    parser = argparse.ArgumentParser(description="Populate scenario matrix from skeletons + context.")
    parser.add_argument("--flow",      required=True, help="Flow name (e.g. create_service_report)")
    parser.add_argument("--skeletons", default=None,  help="Override skeletons path")
    parser.add_argument("--context",   default=None,  help="Override context path")
    parser.add_argument("--model",     default="gpt-4.1", help="OpenAI model (default: gpt-4.1 — its 1M context fits this flow's payload; gpt-4o's 128k overflows)")
    args = parser.parse_args()

    skeletons_path = Path(args.skeletons) if args.skeletons else SKELETONS_DIR / f"scenario_skeletons_{args.flow}.json"
    context_path   = Path(args.context)   if args.context   else CONTEXT_DIR   / f"context_{args.flow}.json"
    MATRICES_DIR.mkdir(exist_ok=True)

    if not skeletons_path.exists():
        print(f"Error: skeletons file not found: {skeletons_path}")
        sys.exit(1)
    if not context_path.exists():
        print(f"Error: context file not found: {context_path}")
        sys.exit(1)

    skeletons = load_skeletons(skeletons_path)
    context   = json.loads(context_path.read_text(encoding="utf-8"))

    print(f"Loaded {len(skeletons)} scenario skeletons")
    print(f"Context: {context.get('meta', {}).get('files_read', '?')} files, "
          f"DB {'present' if context.get('test_data', {}).get('known_entities') else 'absent'}")

    # ── Part 1: populate skeletons in batches ─────────────────────────────────
    print(f"\nPopulating scenarios in batches of {BATCH_SIZE}...")

    # Checkpoint: persist populated deltas after every batch so a crash mid-run
    # (network, an unrecoverable runaway, Ctrl-C) doesn't discard already-paid
    # work — re-running resumes for free. Fingerprinted by the skeletons file so
    # a checkpoint from a *different* skeleton set is never silently reused.
    ckpt_path = MATRICES_DIR / f".populate_ckpt_{args.flow}.json"
    skel_sig  = hashlib.sha1(skeletons_path.read_bytes()).hexdigest()
    delta_map: dict[str, dict] = {}
    if ckpt_path.exists():
        try:
            saved = json.loads(ckpt_path.read_text(encoding="utf-8"))
            if saved.get("_skeletons_sig") == skel_sig:
                delta_map = saved.get("deltas", {})
                if delta_map:
                    print(f"  resuming from checkpoint: {len(delta_map)} case(s) already populated")
            else:
                print("  checkpoint is from a different skeletons file — ignoring it")
        except (json.JSONDecodeError, OSError):
            delta_map = {}

    def _save_ckpt():
        ckpt_path.write_text(
            json.dumps({"_skeletons_sig": skel_sig, "deltas": delta_map}, indent=2),
            encoding="utf-8",
        )

    batches = [skeletons[i:i + BATCH_SIZE] for i in range(0, len(skeletons), BATCH_SIZE)]
    for i, batch in enumerate(batches, 1):
        pending = [sc for sc in batch
                   if sc.get("case", sc.get("scenario_id", "")) not in delta_map]
        if not pending:
            print(f"  [batch {i}] already in checkpoint — skipping")
            continue
        deltas = populate_batch_resilient(pending, context, args.model, args.flow, i)
        for d in deltas:
            if isinstance(d, dict) and "case" in d:
                delta_map[d["case"]] = d
        _save_ckpt()

    matrix = [merge_delta(sc, delta_map.get(sc.get("case", sc.get("scenario_id", "")), {})) for sc in skeletons]
    print(f"Populated {len(matrix)} scenarios from {len(batches)} batch(es)")

    # ── Part 2: code analysis ─────────────────────────────────────────────────
    print("\nRunning code analysis...")
    covered_ids = [sc.get("case", sc.get("scenario_id", "")) for sc in skeletons]
    next_id = len(skeletons) + 1
    analysis = analyze_code(context, covered_ids, next_id, args.model, args.flow)

    new_scenarios = analysis.get("new_scenarios", [])
    conflicts     = analysis.get("flagged_conflicts", [])

    if new_scenarios:
        print(f"Grounding {len(new_scenarios)} code-analysis scenario(s) through populate pass...")
        ca_delta_map: dict[str, dict] = {}
        ca_batches = [new_scenarios[i:i + BATCH_SIZE] for i in range(0, len(new_scenarios), BATCH_SIZE)]
        for i, batch in enumerate(ca_batches, 1):
            deltas = populate_batch_resilient(batch, context, args.model, args.flow, f"code-analysis {i}")
            for d in deltas:
                if isinstance(d, dict) and "case" in d:
                    ca_delta_map[d["case"]] = d
        for sc in new_scenarios:
            if isinstance(sc, dict):
                delta = ca_delta_map.get(sc.get("case", ""), {})
                matrix.append(merge_delta(sc, delta))

    print(f"{len(new_scenarios)} additional scenario(s) from code analysis")

    # ── Part 3: assign recordings from skeleton journeys (no LLM call) ──────
    print("\nAssigning recordings from skeleton journeys...")
    recordings = assign_recordings(matrix, skeletons_path, args.flow)

    # stamp flow_type back onto each scenario
    flow_type_map: dict[str, str] = {}
    for rec in recordings:
        for cid in rec.get("scenario_ids", []):
            flow_type_map[cid] = rec["flow_type"]
    for sc in matrix:
        cid = sc.get("case", sc.get("scenario_id", ""))
        sc["flow_type"] = flow_type_map.get(cid, "")
    print(f"{len(recordings)} unique recording(s) for {len(matrix)} scenario(s)")

    # ── Write scenario matrix ──────────────────────────────────────────────────
    matrix_path = MATRICES_DIR / f"scenario_matrix_{args.flow}.json"
    matrix_path.write_text(json.dumps(matrix, indent=2), encoding="utf-8")
    print(f"\n{len(matrix)} total scenarios → matrices/{matrix_path.name}")

    # Populate finished and the matrix is on disk — drop the checkpoint so an
    # intentional future re-run starts clean instead of resuming stale deltas.
    ckpt_path.unlink(missing_ok=True)

    # ── Write recordings needed ────────────────────────────────────────────────
    rec_path = MATRICES_DIR / f"recordings_needed_{args.flow}.md"
    write_recordings_md(recordings, rec_path)
    print(f"{len(recordings)} recording(s) → matrices/{rec_path.name}")

    # Raw JSON alongside the markdown — record.py reads this directly instead of
    # parsing the human-readable .md back into structured data.
    rec_json_path = MATRICES_DIR / f"recordings_{args.flow}.json"
    rec_json_path.write_text(json.dumps(recordings, indent=2), encoding="utf-8")
    print(f"{len(recordings)} recording(s) → matrices/{rec_json_path.name}")

    # ── Write flagged conflicts (only if any) ──────────────────────────────────
    if conflicts:
        conf_path = MATRICES_DIR / f"flagged_conflicts_{args.flow}.md"
        write_conflicts_md(conflicts, conf_path)
        print(f"{len(conflicts)} conflict(s) flagged → matrices/{conf_path.name}")
    else:
        print("No code conflicts found.")

    # ── Summary by category ────────────────────────────────────────────────────
    from collections import Counter
    counts = Counter(sc.get("category", "unknown") for sc in matrix)
    print("\nCategory breakdown:")
    for cat, count in sorted(counts.items()):
        print(f"  {cat}: {count}")


if __name__ == "__main__":
    main()
