"""
generate.py

Pipeline:
  Call 1  — free-text reasoning: builds INVENTORY A-E from source
  Call 2+ — one json_object call per category: expands inventory items into scenarios
  Merge   — renumber TC001..N, sanitise, structural validation

Usage:
  python testGen/generate.py --flow agent_create_ticket
  python testGen/generate.py --flow customer_create_ticket
  python testGen/generate.py --flow post_creation_visibility
"""

import argparse
import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

TESTGEN_DIR  = Path(__file__).parent
ENV_PATH     = TESTGEN_DIR / ".env"
load_dotenv(ENV_PATH)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o")

MAX_TOKENS_INVENTORY = 4000   # Call 1 — inventory text only
MAX_TOKENS_SCENARIOS = 16000  # Call 2 — full scenario matrix

TOKEN_USAGE = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "dollars": 0.0,
    "calls": 0,
}
LOG_FILE = TESTGEN_DIR / "token_usage.log"
USAGE_LOG_ENTRIES: list[str] = []

MODEL_COST_RATES = {
    # dollars per 1K tokens (divided by 1000 in accumulate_token_usage)
    "gpt-4o": {"prompt": 0.0025, "completion": 0.01},
    "gpt-4o-mini": {"prompt": 0.00015, "completion": 0.0006},
    "gpt-4o-mini-2024": {"prompt": 0.00015, "completion": 0.0006},
    "gpt-4.1": {"prompt": 0.002, "completion": 0.008},
    "gpt-4.1-mini": {"prompt": 0.0004, "completion": 0.0016},
}


def get_model_cost_rates(model_name: str) -> dict[str, float]:
    prompt_rate = os.getenv("OPENAI_MODEL_PROMPT_COST")
    completion_rate = os.getenv("OPENAI_MODEL_COMPLETION_COST")
    if prompt_rate is not None and completion_rate is not None:
        try:
            return {"prompt": float(prompt_rate), "completion": float(completion_rate)}
        except ValueError:
            pass

    key = model_name.split(":")[0].split("/")[0]
    return MODEL_COST_RATES.get(key, {"prompt": 0.0, "completion": 0.0})


def accumulate_token_usage(response, label: str = "") -> None:
    usage = getattr(response, "usage", None)
    if not usage:
        return

    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    total_tokens = getattr(usage, "total_tokens", None)

    if prompt_tokens is not None:
        TOKEN_USAGE["prompt_tokens"] += int(prompt_tokens)
    if completion_tokens is not None:
        TOKEN_USAGE["completion_tokens"] += int(completion_tokens)
    if total_tokens is not None:
        TOKEN_USAGE["total_tokens"] += int(total_tokens)

    rates = get_model_cost_rates(OPENAI_MODEL)
    TOKEN_USAGE["dollars"] += (
        (int(prompt_tokens) if prompt_tokens is not None else 0) * rates["prompt"] / 1000.0
        + (int(completion_tokens) if completion_tokens is not None else 0) * rates["completion"] / 1000.0
    )
    TOKEN_USAGE["calls"] += 1

    if label:
        log_line = (
            f"  [Usage] {label}: prompt={prompt_tokens}, completion={completion_tokens}, "
            f"total={total_tokens}, cost=${TOKEN_USAGE['dollars']:.6f}"
        )
        print(log_line)
        USAGE_LOG_ENTRIES.append(log_line)


def write_usage_log(args, out_path: Path) -> None:
    if not TOKEN_USAGE["calls"]:
        return

    summary_line = (
        f"\n  Token usage ({TOKEN_USAGE['calls']} LLM call(s)): "
        f"prompt={TOKEN_USAGE['prompt_tokens']:,}  "
        f"completion={TOKEN_USAGE['completion_tokens']:,}  "
        f"total={TOKEN_USAGE['total_tokens']:,}  "
        f"dollars=${TOKEN_USAGE['dollars']:.6f}"
    )
    print(summary_line)
    USAGE_LOG_ENTRIES.append(summary_line)

    log_header = f"[{datetime.utcnow().isoformat()}Z] Flow: {args.flow} subdir: {args.subdir or 'none'}"
    command_line = f"Command: python generate.py --flow {args.flow}"
    if args.subdir:
        command_line += f" --subdir {args.subdir}"
    if args.out:
        command_line += f" --out {args.out}"

    with LOG_FILE.open("a", encoding="utf-8") as lf:
        lf.write(log_header + "\n")
        lf.write(command_line + "\n")
        lf.write(f"Saved -> {out_path}\n")
        for line in USAGE_LOG_ENTRIES:
            lf.write(line + "\n")
        lf.write("\n")


# ── CALL 1: Inventory prompt ──────────────────────────────────────────────────
INVENTORY_SYSTEM = """
You are a senior QA automation engineer. Your job is to read application source code and produce a complete, accurate inventory that will drive test scenario generation. Be exhaustive — missing an item here means a missing test later.

You will receive source code for a form (frontend + backend + models) and an explicit primary_role that tells you which role Session A operates as in this flow. Use this primary_role exactly as given — do NOT try to derive it from the flow name.

════════════════════════════════════════════════════════════
INVENTORY A — FIELDS VISIBLE TO THE PRIMARY ROLE
════════════════════════════════════════════════════════════
Read the form source carefully. The form renders different fields for different roles using explicit JSX conditional rendering (e.g. `{isAdmin && (...)}`, `{!isCustomer && (...)}`), permission guards (e.g. `hasPerms`, `hasPermsV2`), or derived booleans from auth/context. When deciding visibility, always prefer explicit JSX conditionals and permission checks found next to the input elements over any default-initialized state values.
List ONLY the fields the primary role can see and interact with. Do not list fields rendered for other roles.

For each field write one line:
  A<n>. <field_name> | ui_label: <label text shown in the UI next to this field> | type: <text|select|multiselect|textarea|date|file|checkbox> | required: <yes|no|role-dependent> | constraints: <e.g. maxLength N, minValue X, email format, or "none"> | auto_filled: <yes|no>

ui_label: read the JSX that renders this field — find the <Typography>, <label>, <FormLabel>, or similar element next to the input and copy its text content exactly. 

Set auto_filled: yes if ANY of these apply:
(a) This field is populated automatically by a cascade (another field's selection triggers a state update, including reverse cascades and conditional effects that fire when another required field is set).
(b) This field already has a value when the form first renders — meaning the user can submit the form without ever touching this field. Look for: useState initialised with a non-empty string, number, or object (e.g. useState("someValue") or useState({label:...,value:...})); a value set from the first element of a static array on mount; or a date/timestamp computed at render time (e.g. new Date(), moment()). The user being ABLE to change the value does not matter — if the field starts with a value, it is auto_filled: yes.
(c) This field always derives its value from another required field at runtime, even conditionally.
Only use auto_filled: no if the field is completely empty (null, undefined, or empty string) when the form first renders AND is never populated by another field's selection. Do not use any other value — only "yes" or "no".
════════════════════════════════════════════════════════════
INVENTORY B — CASCADE CHAINS
════════════════════════════════════════════════════════════
A cascade is any relationship where changing Field X causes Field Y to update, filter, refetch, auto-fill, clear, or recalculate.

CRITICAL ROLE CHECK: Before writing a B item, check your A items. If the target field (e.g., 'department') is NOT in Inventory A, YOU MUST NOT LIST the cascade. Cascades for hidden fields are strictly forbidden.
CRITICAL REVERSE CASCADES: Pay special attention to upward/reverse cascades. If selecting a child entity automatically populates its parent entity, you MUST list this as a B item with EFFECT: auto-fill.
CRITICAL TRACEABILITY: Only list a cascade if you can point to the EXACT useEffect, onChange, API call, or JSX disabled/readOnly prop in source code that implements it. Do not infer cascades from field proximity, related database columns, or backend processing logic. If you cannot quote the specific code that triggers the dependency, do NOT list it. For each cascade write one line:
  B<n>. <trigger_field> -> <target_field> | EFFECT: <filter|reset|auto-fill|calculate|disable|lock> | GUARD: <condition or none> | REVERSAL: <what happens when trigger is cleared/changed>

EFFECT definitions — choose the most accurate one:
  filter   : changing the trigger narrows the OPTIONS in the target dropdown, but the target field keeps its current value if still valid
  reset    : changing the trigger CLEARS the target field's current value entirely
  auto-fill: changing the trigger automatically populates the target field with a value
  calculate: changing the trigger recomputes a derived value
  disable  : when the trigger field is empty or unset the target field is rendered disabled/non-interactive; setting the trigger enables it. ALSO covers the pattern where the target's `disabled` prop is tied to its own options array being empty (e.g. `disabled={optionsArray.length <= 0}` or `disabled={!options.length}`): if those options are populated by another field's cascade, that field is the trigger and the EFFECT is disable — empty options means disabled, options loaded means enabled.
  lock     : setting the trigger auto-fills the target with data derived from the trigger entity AND hard-disables the target field. Look for JSX props like `disabled={someRef ? true : false}` or `disabled={!!someState}` where the disabled condition is truthy when the trigger has a value. This is the REVERSE of disable — the field starts interactive and becomes non-editable once the trigger is set. When the trigger is cleared, the target value resets and the field becomes interactive again. Only use lock when the source shows BOTH: (1) a conditional `disabled` prop that is true when the trigger field/ref holds a value, AND (2) a useEffect or onChange that writes the target's value from the trigger entity's data.

MULTI-TRIGGER CASCADES — a single useEffect or onChange handler may depend on multiple fields (e.g. useEffect([fieldA, fieldB, fieldC])). When this happens, list it as ONE B item with all triggers separated by " + " in the trigger position.

CRITICAL REVERSE FILTER CASCADES: Autocomplete and select fields that load from a master data array may be FILTERED by other form fields that are not obviously "parent" fields. Look for useEffect hooks whose dependency array includes both a master data list AND one or more other form field values (e.g. `useEffect([..., formData.fieldA, formData.fieldB, masterList])`), where the body filters that master list based on the currently selected fieldA/fieldB values. Each such dependency that causes re-filtering is its own B-item pointing TO the lookup field with EFFECT: filter.

CRITICAL AUTO-SELECT-WHEN-ONE: If a filter cascade (or any cascade that rebuilds an options array) contains logic that auto-selects the field when exactly one option remains after filtering (e.g. `if (filtered.length === 1 && !formData.fieldName) { setFormData(...) }`), list this as an ADDITIONAL B-item for the same trigger(s) with EFFECT: auto-fill, noting the single-result condition in the GUARD.

════════════════════════════════════════════════════════════
INVENTORY C — VISIBILITY GATES (role-exclusion AND permission)
════════════════════════════════════════════════════════════
A visibility gate is any condition that shows or hides a field based on the logged-in user's role or permissions. There are TWO types — you MUST list BOTH.

TYPE 1 — PERMISSION GATE: the primary role CAN see this field, but only when the user holds a specific permission flag. The field IS in Inventory A.

TYPE 2 — ROLE EXCLUSION: the field exists in the form source but is wrapped in a role check (e.g. `{isAgent && (...)}`, `{!isAgent && (...)}`) that COMPLETELY excludes the primary role. The field is NOT in Inventory A.

CRITICAL JSX TRACING RULE: Look extremely closely at (eg. `{isAgent && (...)})` blocks. Often, an entire section containing the (eg. 'customer', 'user', 'department', 'staff_assigned', and 'due_date') dropdowns is wrapped in one of these blocks. If the primary role is excluded by the block wrapper, EVERY field inside that wrapper MUST be listed here as TYPE: role_exclusion, and they MUST NOT APPEAR in Inventory A.

For each gate write one line:
  C<n>. FIELD: <field_name> | CONDITION: <exact expression from source> | SHOWN TO: <role or condition> | HIDDEN FROM: <role or condition> | TYPE: permission_gate|role_exclusion

════════════════════════════════════════════════════════════
INVENTORY D — POST-CREATION ACCESS RULES & SIDE-EFFECTS
════════════════════════════════════════════════════════════
Read the backend access-control and creation logic. Look for explicit visibility rules AND implicit access side-effects (e.g., users being automatically added to access lists, roles, or relationships during the creation transaction).
One rule per line:
  D<n>. <who> CAN/CANNOT see the record | WHEN: <condition from source>

CRITICAL — ONE D-ITEM PER CONDITION: If the source has multiple independent conditions (connected by OR) that each grant access, write ONE D-item per condition — do NOT merge them into a single D-item. Example: if the access checker has:
  if (assigned_staff_id === agent) return true;
  if (staff_accountable === agent) return true;
  if (isManagerOrAdmin && dept has view permission) return true;
Write THREE separate D-items, not one combined item.

CRITICAL — USE FORM FIELD NAMES IN WHEN: For identity conditions (a ticket field stores the agent's id), use the EXACT form field name from Inventory A in the WHEN clause, not model field names or human descriptions. Example: write "WHEN: staff_assigned=agent" not "WHEN: they are the assigned staff".

CRITICAL — COMPOUND CONDITIONS (AND): If a single condition uses `&&` (AND), ALL parts MUST appear in WHEN for that one D-item. Example: `isManagerOrAdmin && allowedDeptIdSet.has(dept_id)` → "WHEN: staff is manager/admin (staff_type=admin/manager/lead or account isadmin/isservicemanager) AND dept has ticket.view permission"

CRITICAL — TRACE HELPER FUNCTIONS: When a condition calls a helper function (e.g. `isStaffManagerOrAdmin()`), read that function's source and inline its logic in the D-rule WHEN clause. Do NOT write the helper function name — expand it to its actual logic.

════════════════════════════════════════════════════════════
INVENTORY E — ENTITY MAPPING FOR LOOKUP FIELDS
════════════════════════════════════════════════════════════
For each select/multiselect field in Inventory A, write:
  E<n>. FIELD: <name> | SOURCE: <known_entities path> | DISPLAY LABEL: <property the user sees in the dropdown>

If the options list is pre-filtered by the authenticated user's session context —
e.g. filtered by the user's own org_id, site_id, or any session attribute that comes
from the token rather than from a field the user sets in the form — add:
  | SCOPE: current_user_<attribute>   (e.g. SCOPE: current_user_org)

Only add SCOPE when the filter is session-driven. Form-driven filters (triggered by
the user setting another field) belong in Inventory B, not here.

Then list any cross-field constraints:
  CONSTRAINT: <field X> and <field Y> must share the same <parent entity>

CRITICAL: Only list mappings for fields that are present in Inventory A.

════════════════════════════════════════════════════════════
INVENTORY F — FILTERED DROPDOWN FIELDS
════════════════════════════════════════════════════════════
For every select/multiselect field in Inventory A, read the controller or
utility function that serves its options list. Look for TWO types of filters:

  1. Active/inactive or soft-delete guard — records excluded based on a status
     column, paranoid timestamp, or delete marker.

CRITICAL EVIDENCE REQUIREMENT: Only list a field if you can quote the EXACT
filter expression or conditional branch from the source code. Do not infer a
filter from model field names, database column names, or backend processing
logic. If you cannot point to the specific code in the controller or utility,
do NOT list the field.

For each filter found, write one line:
  F<n>. FIELD: <field_name> | FILTER: <exact condition from source code>

Do not list fields where ALL roles receive identical unfiltered results.
Only list fields present in Inventory A.

════════════════════════════════════════════════════════════
OUTPUT FORMAT
════════════════════════════════════════════════════════════
Write the six inventories as plain text, one item per line. No JSON, no markdown headers. Just the labelled lines. Be exhaustive.
""".strip()


# ── CALL 2: Scenario generation prompt ───────────────────────────────────────
SCENARIO_SYSTEM = """
You are a senior QA automation engineer. You have been given a pre-built INVENTORY (A through E) derived from application source code. Your job is to expand every inventory item into test scenarios exhaustively. The inventory is complete and accurate — trust it.

════════════════════════════════════════════════════════════
EXPANSION RULES
════════════════════════════════════════════════════════════
Expand ONLY the categories listed in scenario_categories. For each category, apply these rules to EVERY relevant inventory item.

VISIBILITY GATE: Only generate scenarios for fields that exist in Inventory A.
- If a cascade field (B item) is not in A, skip it entirely.
- The only category that may reference hidden fields is role_field_visibility.

happy_path
  Exactly ONE scenario for the primary role. All required non-auto-filled fields filled with valid values. Cross-entity constraints satisfied (e.g. customer and user must share the same org).

missing_field
  One scenario per A<n> where required=yes AND auto_filled=no.
  Omit exactly that one field from inputs. Include all other required non-auto-filled fields.
  expected_outcome: "fail"
  expected_message: the exact error/toast string the form shows on submit.

boundary
  Per A<n> with a length or range constraint, generate:
    1. AT the limit (pass)
    2. ONE UNIT above the limit (fail)
  Use a short realistic string as the value — the test harness will enforce the exact character count.
  expected_message: the exact error string the form shows when the limit is exceeded.

invalid_value
  Only for A<n> with explicit format constraints (email format, file-type allowlist, regex pattern).
  Do NOT generate for select fields or plain text fields with only a maxLength constraint.
  expected_outcome: "fail"

cascade_dependency
  Per B<n>, generate ALL of:
    1. Happy-order: set trigger only -> assert target updates
    2. Wrong-order: interact with target first, then set trigger -> assert target self-corrects
    3. Clear-trigger: set trigger, then clear it -> assert target resets
  inputs for happy-order: include ONLY the trigger field. Do not include the target field.
  If B<n> has a GUARD condition, include the guard field in inputs.
  cascade_trigger = the trigger field(s) from B<n>.
  Also, if the source code applies a status or membership filter to a dropdown (e.g. an
  isActive-style guard that filters the options list), find at least one item in the entity
  data that the filter would exclude, and generate a scenario that sets the trigger and asserts
  that item does not appear among the dropdown options.

  FILTER EFFECT — MANDATORY ASSERTION FORMAT:
  For B<n> where EFFECT is "filter", ALL assertions MUST follow this rule — no exceptions.

  Step 1: Find the collection in known_entities that populates the target field
          (look at the E-item source path for the target, or inspect known_entities directly).
  Step 2: Pick ONE specific named item (e.g. a name/title string) from that collection.
          — If the collection is empty or absent in known_entities: generate ONLY the
            clear-trigger scenario for this B-item, with assertion:
            "<target ui_label> resets after <trigger ui_label> is cleared"
            Do NOT generate happy-order or wrong-order scenarios.
          — If items exist: use that one named item in every assertion below.

  Required assertion text (copy the format exactly, fill in the bracketed parts):
    happy-order:   "'<item name>' option is visible in the <target field> dropdown"
    wrong-order:   "'<item name>' option is visible in the <target field> dropdown"
    clear-trigger: "'<item name>' option is absent from the <target field> dropdown"

  FORBIDDEN — never write assertions like these, they will fail at runtime:
    ✗ "<field> dropdown is filtered based on <trigger>"
    ✗ "options update after <trigger> is set"
    ✗ "<field> dropdown self-corrects after <trigger> is set"

  For B<n> where EFFECT is "disable": generate a 4th scenario in addition to the three above —
    4. Empty-trigger (disable only): leave the trigger unset → assert the target field is
       disabled and not interactive.
       inputs: {}
       expected_outcome: "pass"
       assertion: "<target field ui_label> is disabled and not interactive when <trigger field> is not set"
       test_mode: "cascade"

  For B<n> where EFFECT is "lock": generate 3 scenarios (replaces the standard 3):
    1. Happy-order (lock): set trigger → assert target auto-fills from trigger entity data AND the target field element is disabled/non-interactive.
       inputs: { <trigger_field>: <value from known_entities> }
       assertion: "<target field ui_label> is auto-filled and disabled after <trigger field ui_label> is set"
       expected_outcome: "pass"
       test_mode: "cascade"
    2. Overwrite attempt: confirm target is interactive before trigger is set, then set trigger → assert target value is overwritten with trigger entity data AND field becomes disabled.
       inputs: { <trigger_field>: <value from known_entities> }
       assertion: "<target field ui_label> is overwritten and disabled when <trigger field ui_label> is selected, regardless of prior user input"
       expected_outcome: "pass"
       test_mode: "cascade"
    3. Clear-trigger (unlock): set trigger, then clear it → assert target value resets to empty AND field becomes interactive again.
       inputs: {}
       assertion: "<target field ui_label> clears and becomes interactive again after <trigger field ui_label> is removed"
       expected_outcome: "pass"
       test_mode: "cascade"

  Also, for any E<n> that carries a SCOPE annotation: generate ONE scenario.
  Do NOT pick specific item names — execute.py resolves what should appear at runtime.
  cascade_trigger: null
  field: the E<n> field name
  scope: the value from the SCOPE annotation verbatim (e.g. "current_user_org")
  test_mode: "cascade"
  inputs: {}
  description: "Scope check: <field> dropdown shows only items from the user's <scope>"

role_field_visibility
  Per C<n>:
  - TYPE permission_gate: generate SHOWN TO (with permission, pass) and HIDDEN FROM (without permission, fail).
  - TYPE role_exclusion: generate only a HIDDEN FROM scenario. inputs must be empty ({}). expected_outcome: "pass". assertion: "<ui_label> field is not present in the DOM".

ui_capability
  One scenario per distinct field-type in Inventory A. Tests that the UI widget is physically usable.
  expected_outcome: "pass"
  CRITICAL — CASCADE TRIGGER FIELDS: If the field under test appears as a TRIGGER in a B-item
  (i.e. it causes another field to filter, reset, or auto-fill), do NOT include the cascade
  TARGET field(s) in inputs — even when those targets are required and auto_filled=yes.
  After the trigger field is set the cascade populates the target; the harness relies on this.
  Including the target would break the base script's selector which expects the post-cascade state.

════════════════════════════════════════════════════════════
INPUTS
════════════════════════════════════════════════════════════
inputs = flat object: field_name -> value. Only include fields from Inventory A.
- Never include fields hidden from the primary role (role_exclusion in C).
- For select/multiselect fields: value must be the DISPLAY LABEL from Inventory E, not an ID.
- For text/textarea fields: write a realistic value (e.g. "Server not responding", not "test string").

════════════════════════════════════════════════════════════
OUTPUT SCHEMA — every scenario has exactly these keys
════════════════════════════════════════════════════════════
  id               : "TC001" sequential, no gaps
  category         : category name
  flow             : flow name
  flow_sequence    : [flow name]
  role             : primary role for session A
  description      : one sentence
  inputs           : flat object, field -> value
  expected_outcome : "pass" or "fail"
  expected_message : exact UI error string or null
  assertion        : specific observable browser state
  field            : primary field under test, or null
  cascade_trigger  : trigger field(s) from B<n>, or null
  scope            : SCOPE annotation value from E<n> (scope-check cascade scenarios only, else null)
  second_session   : { "role": "...", "name": "..." } or null
  business_rule    : inventory item reference (e.g. "A3")
  test_mode        : "ui" | "api" | "multi_session" | "permission"
  permission_agent : staff name (permission_access scenarios only, else null)
  permission_element: UI text of the source-derived gated element (permission_access only, else null)

════════════════════════════════════════════════════════════
OUTPUT FORMAT
════════════════════════════════════════════════════════════
Return ONLY valid JSON:
{ "scenarios": [ { ...scenario... }, ... ] }
""".strip()


def _manifest_path(flow_id: str, subdir: str | None = None) -> Path:
    """Locate the manifest JSON, checking a named subdir first, then searching recursively."""
    if subdir:
        return TESTGEN_DIR / "manifests" / subdir / f"{flow_id}.json"
    flat = TESTGEN_DIR / "manifests" / f"{flow_id}.json"
    if flat.exists():
        return flat
    matches = list((TESTGEN_DIR / "manifests").glob(f"**/{flow_id}.json"))
    return matches[0] if matches else flat


def build_inventory(flow_id: str, primary_role: str, context: dict, client: OpenAI, subdir: str | None = None) -> str:
    manifest_path = _manifest_path(flow_id, subdir)
    form_name = json.loads(manifest_path.read_text(encoding="utf-8")).get("form", "") if manifest_path.exists() else ""
    inv_prompt_path = TESTGEN_DIR / "prompts" / f"{form_name}_inventory.txt" if form_name else None
    extra_inventory = inv_prompt_path.read_text(encoding="utf-8") if inv_prompt_path and inv_prompt_path.exists() else ""
    if extra_inventory:
        print(f"  [Custom inventory prompt] Loaded {inv_prompt_path.name}")
    inventory_system = INVENTORY_SYSTEM if not extra_inventory else INVENTORY_SYSTEM + "\n\n" + extra_inventory

    user_msg = f"""Flow: {flow_id}
Primary role (Session A): {primary_role}
List ONLY the fields the primary role ({primary_role}) can see and interact with. Ignore fields hidden from this role.

Here is the extracted source context:
{json.dumps(context, indent=2)}

Build the complete INVENTORY A through E for this flow. Be exhaustive. Use the exact line format from the instructions.
""".strip()

    print(f"  [Call 1] Building inventory...")
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": inventory_system},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0,
        max_tokens=MAX_TOKENS_INVENTORY,
    )
    accumulate_token_usage(response, "build_inventory")
    return response.choices[0].message.content


def post_process_inventory_remove_role_excluded(inventory_text: str, primary_role: str, context: dict = None) -> str:
    import re

    lines = inventory_text.splitlines()
    hidden_fields = set()
    c_fields_existing = set()
    
    # 1. Trust the LLM's C-items (Role Exclusions)
    c_re = re.compile(r"C\d+\.\s+FIELD:\s*(\S+)\s*\|.*TYPE:\s*role_exclusion", re.IGNORECASE)
    for line in lines:
        m = c_re.match(line)
        if m:
            field_name = m.group(1).strip()
            c_fields_existing.add(field_name)
            # Check if it's hidden from the primary role
            hidden_from_match = re.search(r"HIDDEN FROM:\s*([\w\s,]+)", line, re.IGNORECASE)
            if hidden_from_match and primary_role.lower() in hidden_from_match.group(1).lower():
                hidden_fields.add(field_name)

    # 2. BULLETPROOF HEURISTIC WITH BRACKET MATCHING
    heuristic_hidden_fields = set()
    if context and primary_role:
        try:
            possible_sources = []
            def _collect_strings(obj):
                if isinstance(obj, str): possible_sources.append(obj)
                elif isinstance(obj, dict):
                    for v in obj.values(): _collect_strings(v)
                elif isinstance(obj, list):
                    for v in obj: _collect_strings(v)

            _collect_strings(context)
            filtered_sources = [s for s in possible_sources if isinstance(s, str) and '<' in s and (('&&' in s) or ('name=' in s) or ('{is' in s))]
            if filtered_sources: possible_sources = filtered_sources

            primary = primary_role.lower()
            pattern = re.compile(r"\{\s*(!?is[A-Za-z0-9_]+)\s*&&\s*\(")
            name_attr_re = re.compile(r"name\s*=\s*\"([^\"]+)\"|name\s*=\s*'([^']+)'")

            for src in possible_sources:
                if not isinstance(src, str): continue
                for m in pattern.finditer(src):
                    flag = m.group(1)
                    inverted = flag.startswith("!")
                    flag_clean = flag.lstrip("!").lower()
                    
                    start = m.end()
                    open_count = 1
                    end = start
                    
                    while end < len(src) and open_count > 0:
                        if src[end] == '(': open_count += 1
                        elif src[end] == ')': open_count -= 1
                        end += 1
                        
                    window = src[start:end] 
                    
                    found_names = set()
                    for nm in name_attr_re.finditer(window):
                        name_val = nm.group(1) or nm.group(2)
                        if name_val:
                            found_names.add(name_val)
                            
                    if not found_names: continue
                    
                    flag_matches_primary = primary in flag_clean
                    shown_to_primary = bool(flag_matches_primary) ^ inverted
                    
                    if not shown_to_primary:
                        hidden_fields.update(found_names)
                        heuristic_hidden_fields.update(found_names)
        except Exception as e:
            print(f"  [Warn] Heuristic scan failed: {e}")

    if not hidden_fields:
        return inventory_text

    # 3. Filter out A-lines and INJECT missing C-lines
    out_lines = []
    a_re = re.compile(r"(A\d+)\.\s+(\S+)\s+\|.*")
    
    # Find the max C index so we know where to start appending (e.g., C5, C6...)
    max_c_index = 0
    c_index_re = re.compile(r"C(\d+)\.")
    for line in lines:
        m = c_index_re.match(line)
        if m:
            max_c_index = max(max_c_index, int(m.group(1)))

    for line in lines:
        m = a_re.match(line)
        if m and m.group(2) in hidden_fields:
            continue # Skip this hidden field from Inventory A
        out_lines.append(line)

    # Inject missing C items that the heuristic found but the LLM missed
    missing_c_fields = heuristic_hidden_fields - c_fields_existing
    if missing_c_fields:
        out_lines.append("\n# --- HEURISTIC INJECTED ROLE EXCLUSIONS ---")
        for missing_field in missing_c_fields:
            max_c_index += 1
            out_lines.append(f"C{max_c_index}. FIELD: {missing_field} | CONDITION: Heuristic JSX Match | SHOWN TO: other | HIDDEN FROM: {primary_role} | TYPE: role_exclusion")

    return "\n".join(out_lines)

def sanitize_inventory_dependencies(inventory_text: str) -> str:
    """Bulletproof layer: Physically removes Cascades (B), Mappings (E), and Constraints that reference fields not visible in A."""
    import re
    lines = inventory_text.splitlines()
    valid_a_fields = set()
    
    # 1. Collect whitelist of visible fields from Inventory A
    for line in lines:
        if line.strip().startswith("A"):
            match = re.match(r"A\d+\.\s+([a-zA-Z0-9_-]+)\s*\|", line)
            if match:
                valid_a_fields.add(match.group(1).strip())
                
    cleaned_lines = []
    for line in lines:
        line_str = line.strip()
        
        # 2. Filter out B items (cascades) targeting hidden fields
        if line_str.startswith("B"):
            try:
                trigger_target = line_str.split("|")[0]
                trigger_part = trigger_target.split("->")[0].split(".")[1]
                target = trigger_target.split("->")[1].strip()
                triggers = [t.strip() for t in trigger_part.split("+")]
                
                # Drop cascade if target or any trigger is hidden
                if target not in valid_a_fields or not all(t in valid_a_fields for t in triggers):
                    continue 
            except Exception:
                pass 
                
        # 3. Filter out E items (mappings) targeting hidden fields
        elif line_str.startswith("E") and "FIELD:" in line_str:
            try:
                field = line_str.split("FIELD:")[1].split("|")[0].strip()
                if field not in valid_a_fields:
                    continue 
            except Exception:
                pass

        # 4. Filter out F items (active/inactive/public/private filters) targeting hidden fields
        elif line_str.startswith("F") and "FIELD:" in line_str:
            try:
                field = line_str.split("FIELD:")[1].split("|")[0].strip()
                if field not in valid_a_fields:
                    continue
            except Exception:
                pass

        # 5. Filter out CONSTRAINT lines targeting hidden fields
        elif line_str.startswith("CONSTRAINT:"):
            # Matches format: "CONSTRAINT: fieldX and fieldY must..."
            match = re.match(r"CONSTRAINT:\s*([a-zA-Z0-9_-]+)\s+and\s+([a-zA-Z0-9_-]+)", line_str)
            if match:
                f1, f2 = match.groups()
                # If either field in the constraint is hidden, delete the constraint
                if f1 not in valid_a_fields or f2 not in valid_a_fields:
                    continue

        cleaned_lines.append(line)

    return "\n".join(cleaned_lines)


def strip_permission_gate_c_items(inventory_text: str) -> str:
    """Remove C-items of TYPE permission_gate from the inventory.

    Permission-gate tests are driven from source code by the role_permission_access
    scenario category, not from C-items. Leaving permission_gate C-items in causes
    the scenario LLM to test field-level permission guards (e.g. attachment upload)
    instead of the form-entry permission (e.g. the New Case button) that
    role_permission_access is actually meant to cover.
    """
    return "\n".join(
        line for line in inventory_text.splitlines()
        if not (re.match(r"^C\d+\.", line.strip()) and "TYPE: permission_gate" in line)
    )


def filter_role_visibility_non_primary(scenarios: list[dict], inventory_text: str) -> list[dict]:
    """Remove role_field_visibility scenarios that don't belong in this flow.

    Two cases are removed:
    1. Absence-assertion (expected_outcome == "pass") for a field in Inventory A — the primary
       role sees that field, so "field is absent" is testing the other role's perspective and
       belongs in that role's flow instead.
    2. Any scenario whose field is not in Inventory A at all — invented/hallucinated field names
       can't map to a real DOM element and produce vacuous tests.
    """
    a_fields: set[str] = set()
    for line in inventory_text.splitlines():
        line_str = line.strip()
        if re.match(r"^A\d+\.", line_str):
            parts = line_str.split("|")
            field = parts[0].split(".", 1)[1].strip()
            a_fields.add(field)

    kept = []
    for sc in scenarios:
        if sc.get("category") == "role_field_visibility":
            field = sc.get("field") or ""
            # Field not in Inventory A — hallucinated name, can't test it
            if field not in a_fields:
                continue
            # Absence assertion for a field the primary role sees — wrong flow
            if sc.get("expected_outcome") == "pass":
                continue
        kept.append(sc)
    return kept


def _parse_inventory(inventory_text: str, allowed_categories: list[str], entities: dict | None = None, suppress_categories: set[str] | None = None) -> str:
    lines = inventory_text.splitlines()
    a_items, b_items, c_items, f_items = [], [], [], []

    for line in lines:
        m = re.match(r"(A\d+)\.\s+(\S+)\s+\|.*type:\s*(\S+).*\|.*required:\s*(\S+).*\|.*constraints:\s*([^|]+).*\|.*auto_filled:\s*(\S+)", line, re.IGNORECASE)
        if m:
            a_items.append({
                "id": m.group(1), "name": m.group(2), "type": m.group(3).lower().rstrip(","),
                "required": m.group(4).lower().rstrip(","), "constraints": m.group(5).strip().rstrip(","),
                "auto_filled": m.group(6).lower().rstrip(",")
            })

        m_b = re.match(r"(B\d+)\.\s+(.*)", line)
        if m_b: b_items.append(f"{m_b.group(1)}. {m_b.group(2).strip()}")

        m_c = re.match(r"(C\d+)\.\s+(.*)", line)
        if m_c: c_items.append(f"{m_c.group(1)}. {m_c.group(2).strip()}")

        m_f = re.match(r"(F\d+)\.\s+(.*)", line)
        if m_f: f_items.append(f"{m_f.group(1)}. {m_f.group(2).strip()}")

    cat_set = set(allowed_categories) - (suppress_categories or set())
    parts = ["EXPLICIT EXPANSION REQUIREMENTS:"]

    if "missing_field" in cat_set:
        candidates = [a for a in a_items if a["required"] in ("yes", "role-dependent") and a["auto_filled"] == "no"]
        if candidates:
            parts.append(f"\nmissing_field — generate EXACTLY {len(candidates)} scenarios:")
            for a in candidates: parts.append(f"  • {a['id']} ({a['name']}): omit this field")

    if "boundary" in cat_set:
        candidates = [a for a in a_items if a["constraints"].lower() not in ("none", "", "n/a")]
        if candidates:
            parts.append(f"\nboundary — generate 2 scenarios per item (AT limit, ONE ABOVE):")
            for a in candidates: parts.append(f"  • {a['id']} ({a['name']}): constraint = {a['constraints']}")

    if "invalid_value" in cat_set:
        candidates = [a for a in a_items if a["type"] not in ("select", "multiselect") and a["auto_filled"] == "no" and not re.fullmatch(r"(max|min)?length\s*\d+.*", a["constraints"].strip().lower()) and a["constraints"].lower() not in ("none", "n/a", "")]
        if candidates:
            parts.append(f"\ninvalid_value — generate scenarios for:")
            for a in candidates: parts.append(f"  • {a['id']} ({a['name']})")

    if "cascade_dependency" in cat_set and b_items:
        parts.append(f"\ncascade_dependency — generate scenarios for ALL {len(b_items)} chains:")
        for b in b_items: parts.append(f"  • {b}")

    if "role_field_visibility" in cat_set and c_items:
        parts.append(f"\nrole_field_visibility:")
        for c in c_items: parts.append(f"  • {c} (absence checks MUST use empty inputs {{}})")

    if "ui_capability" in cat_set:
        type_set = {a["type"] for a in a_items}
        parts.append(f"\nui_capability — generate one scenario per type ({', '.join(sorted(type_set))}):")
        for ft in sorted(type_set):
            examples = [a["name"] for a in a_items if a["type"] == ft]
            parts.append(f"  • {ft}: use field '{examples[0]}'")

    if "permission_access" in cat_set:
        parts.append(
            "\npermission_access — generate exactly TWO scenarios using "
            "known_entities.permission_representatives (one per access tier). "
            "Derive the observable UI element from the source code."
        )

    if "role_permission_access" in cat_set:

        _ents = entities or {}

        if "staff_access_summary" in _ents:
            parts.append(
                "\nrole_permission_access — Use 'known_entities.staff_access_summary' to map agents to their access tiers. "
                "Read the source code and namespace_map to resolve the correct UI element for the primary action. "
                "Follow your system instructions exactly for generating ALL, LIMITED, and MIXED access scenarios."
                "Expected outcome (pass/fail) must be derived from the 'depts_with_create' list. "
                "Output strictly: permission_agent, permission_element (resolved from namespace), and inputs."
            )   
        else:
            role_defs = _ents.get("role_definitions", {})
            sample_role_ids = {
                str(s["role_id"])
                for dept in _ents.get("departments", [])
                for s in dept.get("staff", [])
                if s.get("role_id")
            }
            testable = {rid: rd for rid, rd in role_defs.items() if rid in sample_role_ids}

            if testable:
                parts.append(
                    f"\nrole_permission_access — generate ONE scenario per testable dept role "
                    f"({len(testable)} roles). Read source to find the permission key path and "
                    f"UI element — do NOT hardcode either. Match staff by role_id from "
                    f"known_entities.departments[*].staff:"
                )
                for rid, rdef in testable.items():
                    parts.append(
                        f"  • role_id={rid}: {rdef.get('name', 'unknown')} "
                        f"(staff_type={rdef.get('staff_type', '?')}) "
                        f"permissions={json.dumps(rdef.get('permissions', {}))}"
                    )
            else:
                parts.append(
                    "\nrole_permission_access — no testable roles found in sample departments; skip."
                )

    if "active_inactive_filter" in cat_set and f_items:
        std_f = [f for f in f_items if "role_unrestricted" not in f]
        unr_f = [f for f in f_items if "role_unrestricted" in f]
        if std_f:
            parts.append(f"\nactive_inactive_filter (standard) — generate 2 scenarios each (filtered-in present, filtered-out absent):")
            for f in std_f:
                parts.append(f"  • {f}")
        if unr_f:
            parts.append(f"\nactive_inactive_filter (role_unrestricted) — generate 1 scenario each: a restricted-for-others item IS visible to this role:")
            for f in unr_f:
                parts.append(f"  • {f}")

    return "\n".join(parts)


def _parse_response(raw: str) -> list[dict]:
    parsed = json.loads(raw)
    if isinstance(parsed, list): return parsed
    if isinstance(parsed, dict):
        if "scenarios" in parsed and isinstance(parsed["scenarios"], list): return parsed["scenarios"]
        for v in parsed.values():
            if isinstance(v, list): return v
        if {"id", "category", "description", "inputs", "expected_outcome"}.issubset(parsed.keys()): return [parsed]
    raise ValueError("Unexpected JSON structure")


def _generate_category(flow_id: str, primary_role: str, category: str, inventory_text: str, entities: dict, checklist: str, client: OpenAI, extra_system: str = "") -> list[dict]:
    system_content = SCENARIO_SYSTEM if not extra_system else SCENARIO_SYSTEM + "\n\n" + extra_system
    user_msg = f"""Flow: {flow_id}\nPrimary role (Session A): {primary_role}\nGenerate ONLY this category: {category}\n\nINVENTORY:\n{inventory_text}\n\nKNOWN ENTITIES:\n{json.dumps(entities, indent=2)}\n\n{checklist}"""
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "system", "content": system_content}, {"role": "user", "content": user_msg}],
        temperature=0, max_tokens=MAX_TOKENS_SCENARIOS, response_format={"type": "json_object"},
    )
    accumulate_token_usage(response, f"generate_category:{category}")
    return _parse_response(response.choices[0].message.content)


def generate_scenarios(flow_id: str, primary_role: str, context: dict, inventory_text: str, client: OpenAI, subdir: str | None = None) -> list[dict]:
    entities = context.get("test_data", {}).get("known_entities", {})
    entities["namespace_map"] = context.get("test_data", {}).get("namespace_map", {})
    categories = context.get("scenario_categories", ["happy_path", "missing_field", "boundary", "invalid_value", "cascade_dependency", "role_field_visibility", "ui_capability", "post_creation_visibility"])

    manifest_path = _manifest_path(flow_id, subdir)
    form_name = json.loads(manifest_path.read_text(encoding="utf-8")).get("form", "") if manifest_path.exists() else ""
    custom_prompt_path = TESTGEN_DIR / "prompts" / f"{form_name}_scenarios.txt" if form_name else None
    extra_system = custom_prompt_path.read_text(encoding="utf-8") if custom_prompt_path and custom_prompt_path.exists() else ""
    if extra_system:
        print(f"  [Custom prompt] Loaded {custom_prompt_path.name} (form: {form_name})")

    # If the custom prompt fully overrides a category's expansion rules, suppress the
    # default _parse_inventory checklist for that category to avoid contradictory instructions.
    custom_override_cats: set[str] = set()
    if extra_system:
        for _cat in ["role_permission_access", "appointment_field_flows", "complex_state_dependencies"]:
            if re.search(rf"^\s*{_cat}\s*$", extra_system, re.MULTILINE | re.IGNORECASE):
                custom_override_cats.add(_cat)

    all_scenarios = []

    for cat in categories:
        print(f"  [Call 2] Generating: {cat}...")
        batch = _generate_category(
            flow_id, primary_role, cat, inventory_text, entities,
            _parse_inventory(inventory_text, [cat], entities, suppress_categories=custom_override_cats),
            client, extra_system,
        )
        print(f"    -> {len(batch)} scenario(s)")
        all_scenarios.extend(batch)

    for i, s in enumerate(all_scenarios, 1):
        s["id"] = f"TC{i:03d}"
    return all_scenarios


def sanitise(scenarios: list[dict], allowed_categories: list[str] | None = None) -> tuple[list[dict], list[str]]:
    fixes = []
    if allowed_categories:
        before = len(scenarios)
        scenarios = [s for s in scenarios if s.get("category") in allowed_categories]
        if before - len(scenarios): fixes.append(f"  DROPPED {before - len(scenarios)} out-of-scope scenario(s)")

    for s in scenarios:
        sid, cat = s.get("id", "?"), s.get("category", "")
        if not isinstance(s.get("flow_sequence"), list):
            s["flow_sequence"] = [s.get("flow", "")]
            fixes.append(f"  FIX {sid}: flow_sequence coerced to list")
    return scenarios, fixes


def _collect_entity_names(entities: dict) -> set[str]:
    label_keys, names = {"name", "provision_name", "service_type", "label", "email"}, set()
    if isinstance(entities, dict):
        for k, v in entities.items():
            if k in label_keys and isinstance(v, str): names.add(v)
            else: names |= _collect_entity_names(v)
    elif isinstance(entities, list):
        for item in entities: names |= _collect_entity_names(item)
    return names


def validate(scenarios: list[dict], inventory_text: str, entities: dict, primary_role: str) -> list[str]:
    warnings = []
    return warnings


def _resolve_entity_default(source_path: str, display_label: str, entities: dict) -> str | None:
    """
    Walk a known_entities source path (e.g. 'known_entities.departments[].service_types')
    and return the first display_label value found.
    """
    path = source_path.strip().replace("known_entities.", "").replace("[]", "")
    parts = [p for p in path.split(".") if p]
    obj = entities
    for part in parts:
        if isinstance(obj, list):
            obj = obj[0] if obj else None
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    if isinstance(obj, list):
        obj = obj[0] if obj else None
    if isinstance(obj, dict):
        return obj.get(display_label) or obj.get("name")
    if obj is not None:
        return str(obj)
    return None


def _parse_inventory_rules(inventory_text: str) -> tuple[dict, dict]:
    """
    Parse D-rules and B-rules from the inventory_text of any flow.

    D-rule lines look like:
        D1. agent CAN see the record | WHEN: they are the accountable_staff_id or assigned_staff_id
        D2. agent CAN see the record | WHEN: they are in the same department as the ticket's dept_id

    B-rule lines look like:
        B2. dept_id -> accountable_staff_id | EFFECT: filter | GUARD: none | REVERSAL: none

    Returns:
        d_rules  — { "D1": {"when": str, "fields": [str,...], "match_type": "identity"|"membership"} }
        b_filter — { child_field: parent_field }
                   e.g. {"accountable_staff_id": "dept_id", "assigned_staff_id": "dept_id"}
    """
    d_rules: dict[str, dict] = {}
    b_filter: dict[str, str] = {}

    # D-rules: capture rule code and the WHEN clause
    d_pat = re.compile(
        r'^(D\d+)\.\s[^|]*\|\s*WHEN:\s*(.+)$',
        re.MULTILINE | re.IGNORECASE,
    )
    for m in d_pat.finditer(inventory_text):
        code = m.group(1).upper()
        when = m.group(2).strip()
        # Field names are snake_case identifiers (at least one underscore)
        fields = re.findall(r'\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b', when)
        # "in the same …" / "belongs to" / "member of" → membership
        # "they are the <field>" → identity (person's name stored directly in the field)
        is_membership = bool(re.search(
            r'\bin the same\b|\bbelongs? to\b|\bmember of\b',
            when, re.IGNORECASE,
        ))
        d_rules[code] = {
            "when": when,
            "fields": fields,
            "match_type": "membership" if is_membership else "identity",
        }

    # B-rules: parent -> child | EFFECT: filter  (child is constrained by parent)
    b_pat = re.compile(
        r'^B\d+\.\s+(\w+)\s*->\s*(\w+)\s*\|[^|]*EFFECT:\s*filter',
        re.MULTILINE | re.IGNORECASE,
    )
    for m in b_pat.finditer(inventory_text):
        parent, child = m.group(1), m.group(2)
        b_filter[child] = parent

    return d_rules, b_filter


def sanitize_multi_session_inputs(
    scenarios: list[dict],
    entities: dict,
    inventory_text: str = "",
) -> list[dict]:
    # Build set of valid form field names from A-items — only inject fields that
    # actually exist as form inputs (not model-only fields like created_by_staff_id).
    _a_fields: set[str] = set()
    for line in inventory_text.splitlines():
        m = re.match(r"A\d+\.\s+(\S+)\s+\|", line)
        if m:
            _a_fields.add(m.group(1))
    """
    For multi_session scenarios: enforce that inputs are consistent with the
    scenario's business_rule + expected_outcome + second_session.name.

    Rule semantics are derived from D/B-rule lines in inventory_text — nothing
    about field names or rule meanings is hardcoded here.

    identity rule  (e.g. "they are the accountable_staff_id or assigned_staff_id"):
        pass → second_session person must appear in one of those fields
               → inject into the first field if missing (respecting B-rule filter constraints)
        fail → second_session person must NOT appear in those fields → remove if wrongly set

    membership rule (e.g. "they are in the same department as the ticket's dept_id"):
        pass → the group field's value must be a dept the second_session person belongs to
               → auto-correct the field value if not
        fail → the group field's value must NOT be a dept the second_session person belongs to
               → warn if it is (scenario design issue, can't auto-fix dept membership)
    """
    d_rules, b_filter = _parse_inventory_rules(inventory_text)

    def _staff_depts(name: str) -> set[str]:
        needle = name.strip().lower()
        depts: set[str] = set()
        for dept in entities.get("departments", []):
            for s in dept.get("staff", []):
                if s.get("name", "").strip().lower() == needle:
                    depts.add(dept.get("name", ""))
        return depts

    def _in_group(name: str, group_val: str) -> bool:
        return group_val.lower() in {d.lower() for d in _staff_depts(name)}

    for scenario in scenarios:
        if scenario.get("test_mode") != "multi_session":
            continue
        second_sess = scenario.get("second_session")
        if not second_sess or not second_sess.get("name"):
            continue

        second_name = second_sess["name"]
        rule_raw    = scenario.get("business_rule") or ""
        inputs      = dict(scenario.get("inputs") or {})
        sid         = scenario.get("id", "?")
        changed     = False

        # expected_outcome describes app behaviour: "pass" = session B can see,
        # "fail" = session B cannot see. This is consistent with all other categories.
        should_see = scenario.get("expected_outcome", "pass") == "pass"

        # Find the first D-rule code in the business_rule string (e.g. "D1" from "D1, A2")
        dm = re.search(r'D(\d+)', rule_raw, re.IGNORECASE)
        if not dm:
            continue
        rule_code = f"D{dm.group(1)}"
        rule = d_rules.get(rule_code)
        if not rule:
            continue

        fields     = rule["fields"]
        match_type = rule["match_type"]

        if match_type == "identity":
            if should_see:
                already_set = any(inputs.get(f) == second_name for f in fields)
                # Only inject a field that is a real form input (in A-items).
                # D-rule WHEN clauses may reference model fields like created_by_staff_id
                # which cannot be set via the form — skip those.
                injectable = [f for f in fields if not _a_fields or f in _a_fields]
                if not already_set and injectable:
                    inject_field = injectable[0]
                    # Respect any B-rule filter: e.g. dept_id → accountable_staff_id
                    # means the person must be in the dept before we can inject them.
                    filter_field = b_filter.get(inject_field)
                    group_val    = inputs.get(filter_field, "") if filter_field else ""
                    if group_val and not _in_group(second_name, group_val):
                        print(f"  WARN {sid}: {second_name!r} not in "
                              f"{filter_field}={group_val!r} — cannot inject "
                              f"{inject_field!r} ({rule_code} pass, B-rule constraint)")
                    else:
                        inputs[inject_field] = second_name
                        changed = True
                        print(f"  SANITIZE {sid}: set {inject_field}={second_name!r} "
                              f"to match second_session ({rule_code} pass)")

            else:  # should NOT see — remove second_session from any staff field in inputs
                for f in fields:
                    if inputs.get(f) == second_name:
                        inputs.pop(f)
                        changed = True
                        print(f"  SANITIZE {sid}: removed {f}={second_name!r} "
                              f"(cannot-see scenario: second_session must not hold {f}, {rule_code})")
                # Also strip second_session from ANY staff identity field, not just D-rule fields,
                # because the LLM may have set them in inputs without matching the rule.
                identity_fields = [a_field for a_field in inputs if "staff" in a_field.lower()]
                for f in identity_fields:
                    if inputs.get(f) == second_name and f not in fields:
                        inputs.pop(f)
                        changed = True
                        print(f"  SANITIZE {sid}: removed {f}={second_name!r} "
                              f"(cannot-see scenario: second_session must not appear in any staff field)")

        elif match_type == "membership":
            group_field = fields[0] if fields else None
            if not group_field:
                continue
            group_val = inputs.get(group_field, "")

            if should_see:
                if group_val and not _in_group(second_name, group_val):
                    their_depts = _staff_depts(second_name)
                    if their_depts:
                        inputs[group_field] = next(iter(their_depts))
                        changed = True
                        print(f"  SANITIZE {sid}: fixed {group_field}={inputs[group_field]!r} "
                              f"({second_name!r} not in {group_val!r}, {rule_code} pass)")
                    else:
                        print(f"  WARN {sid}: {second_name!r} found in no dept — "
                              f"cannot auto-fix {rule_code}")

            else:  # should NOT see
                if group_val and _in_group(second_name, group_val):
                    print(f"  WARN {sid}: {second_name!r} IS in "
                          f"{group_field}={group_val!r} — cannot-see scenario may "
                          f"incorrectly grant access via {rule_code}")

        if changed:
            scenario["inputs"] = inputs

    return scenarios


def resolve_second_session_keys(
    scenarios: list[dict],
    entities: dict,
    env_path: Path | None = None,
) -> list[dict]:
    """
    For every multi_session scenario that has a second_session.name, look up
    the agent's email in known_entities, derive their credential key (username
    part of email), and write it back as second_session.key so execute.py can
    pick the right TEST_SECOND_AGENT_{KEY}_EMAIL / PASSWORD pair from .env.

    Rules:
      - If TEST_SECOND_AGENT_{USERNAME}_EMAIL exists in .env → set key = username
      - If username matches the default TEST_SECOND_AGENT_EMAIL → remove key (use default)
      - If neither matches → leave key unchanged (manual intervention needed)
    """
    from dotenv import dotenv_values

    env: dict[str, str | None] = {}
    if env_path and env_path.exists():
        env = dotenv_values(env_path)

    default_email = (env.get("TEST_SECOND_AGENT_EMAIL") or "").strip().lower()

    def _find_email(name: str) -> str:
        needle = name.strip().lower()
        for dept in entities.get("departments", []):
            for s in dept.get("staff", []):
                if s.get("name", "").strip().lower() == needle:
                    return s.get("email", "")
        return ""

    for scenario in scenarios:
        if scenario.get("test_mode") != "multi_session":
            continue
        second_sess = scenario.get("second_session")
        if not second_sess or not second_sess.get("name"):
            continue

        email = _find_email(second_sess["name"])
        if not email:
            continue

        username = email.split("@")[0].lower()
        named_env_key = f"TEST_SECOND_AGENT_{username.upper()}_EMAIL"

        if env.get(named_env_key, "").strip():
            second_sess["key"] = username
        elif username == default_email or email.lower() == default_email:
            second_sess.pop("key", None)

        scenario["second_session"] = second_sess

    return scenarios


def sanitize_scenario_inputs(
    scenarios: list[dict],
    inventory_text: str,
    entities: dict,
) -> list[dict]:
    """
    Post-LLM sanitization pass over generated scenarios:

    1. For boundary/invalid_value/ui_capability/happy_path categories:
       inject any missing required, non-auto-filled fields using the first
       available value from known_entities (via E-item source paths).
       missing_field, cascade_dependency, role_field_visibility are skipped —
       their inputs are intentionally partial.

    2. For boundary scenarios on string-length constrained fields:
       validate that the string value has the correct length (N chars for AT-limit,
       N+1 for ABOVE-limit). If not, fix it — LLM often truncates due to token limits.
    """
    NEEDS_FULL_INPUTS = {"boundary", "invalid_value", "ui_capability", "happy_path", "post_creation_visibility"}

    # Parse A-items
    a_items = []
    for line in inventory_text.splitlines():
        m = re.match(
            r"(A\d+)\.\s+(\S+)\s+\|.*type:\s*(\S+).*\|.*required:\s*(\S+).*\|.*constraints:\s*([^|]+).*\|.*auto_filled:\s*(\S+)",
            line, re.IGNORECASE,
        )
        if m:
            a_items.append({
                "name":        m.group(2),
                "type":        m.group(3).lower().rstrip(","),
                "required":    m.group(4).lower().rstrip(","),
                "constraints": m.group(5).strip().rstrip(","),
                "auto_filled": m.group(6).lower().rstrip(","),
            })

    # Parse E-items: field → (source_path, display_label)
    e_items: dict[str, tuple[str, str]] = {}
    for line in inventory_text.splitlines():
        m = re.match(
            r"E\d+\.\s+FIELD:\s*(\S+)\s*\|.*SOURCE:\s*([^|]+)\|.*DISPLAY LABEL:\s*(\S+)",
            line, re.IGNORECASE,
        )
        if m:
            e_items[m.group(1).strip()] = (m.group(2).strip(), m.group(3).strip())

    # Build field → first available default value (entity-derived fields)
    field_defaults: dict[str, str] = {}
    for field, (source, label) in e_items.items():
        val = _resolve_entity_default(source, label, entities)
        if val:
            field_defaults[field] = val

    # For required text/textarea fields with no E-item, use a generic placeholder
    for a in a_items:
        if a["name"] not in field_defaults and a["required"] == "yes":
            if a["type"] in ("text", "textarea"):
                field_defaults[a["name"]] = f"Test {a['name'].replace('_', ' ').title()}"

    # For static-source select fields (SOURCE: static, e.g. priority) that still have no
    # default, scan existing scenarios for the first non-null value that was used.
    # This avoids hardcoding values like "High" while still providing a safe fallback.
    for a in a_items:
        if a["name"] not in field_defaults and a["required"] == "yes":
            for s in scenarios:
                v = (s.get("inputs") or {}).get(a["name"])
                if v is not None:
                    field_defaults[a["name"]] = v
                    break

    # Build cascade trigger map: target_field → set of trigger_fields (from B-items)
    # Used below to skip injecting auto_filled required fields when their trigger is in inputs.
    b_trigger_map: dict[str, set[str]] = {}
    for line in inventory_text.splitlines():
        mb = re.match(r"B\d+\.\s+(.+?)\s*->\s*(\w+)\s*\|", line)
        if mb:
            triggers = {t.strip() for t in mb.group(1).replace("+", ",").split(",")}
            b_trigger_map.setdefault(mb.group(2).strip(), set()).update(triggers)

    # Required fields the test must supply.
    # We include ALL required fields regardless of auto_filled — auto_filled=yes means the
    # field CAN be populated by a cascade, not that it's always pre-populated.  Without a
    # cascade trigger in scope, ui_capability/boundary/happy_path scenarios still need a
    # real value or the form will reject on submit for the wrong reason.
    # Exception: if a required auto_filled field's B-item cascade trigger is already in the
    # scenario's inputs, the cascade will populate it — do NOT inject a static value.
    required_manual = [
        a["name"] for a in a_items
        if a["required"] in ("yes",)
    ]

    # Build a lookup for a_items by name
    a_lookup = {a["name"]: a for a in a_items}

    # Drop missing_field scenarios for ALL auto_filled fields — you can't test
    # "missing" a field the form populates automatically, regardless of input type.
    def _is_invalid_missing_field(s: dict) -> bool:
        if s.get("category") != "missing_field":
            return False
        field = s.get("field")
        a = a_lookup.get(field)
        return bool(a and a.get("auto_filled", "no") == "yes")

    before_count = len(scenarios)
    scenarios = [s for s in scenarios if not _is_invalid_missing_field(s)]
    dropped = before_count - len(scenarios)
    if dropped:
        print(f"  SANITIZE: dropped {dropped} missing_field scenario(s) for auto_filled fields")

    # Renumber IDs sequentially after any drops
    for i, s in enumerate(scenarios, 1):
        s["id"] = f"TC{i:03d}"

    for scenario in scenarios:
        cat = scenario.get("category", "")
        inputs = dict(scenario.get("inputs") or {})

        # 1. Inject missing required fields for submit categories.
        # missing_field also needs all required fields except the one intentionally omitted —
        # otherwise the form rejects for the wrong reason and the test is invalid.
        omit_field = scenario.get("field") if cat == "missing_field" else None
        if cat in NEEDS_FULL_INPUTS or cat == "missing_field":
            changed = False
            for field in required_manual:
                if field in inputs:
                    continue
                # Never inject the field that missing_field is deliberately omitting.
                if omit_field and field == omit_field:
                    continue
                # Skip auto_filled required fields whose cascade trigger is already in inputs —
                # the cascade will populate the value; injecting a static one would conflict
                # with the base script's post-cascade selector state.
                a_info = a_lookup.get(field, {})
                if a_info.get("auto_filled") == "yes" and any(
                    t in inputs for t in b_trigger_map.get(field, set())
                ):
                    continue
                if field in field_defaults:
                    inputs[field] = field_defaults[field]
                    changed = True
            if changed:
                scenario["inputs"] = inputs

        # 1b. For multi_session scenarios: strip any inputs key that is not a valid A-item
        #     form field. Model-only fields (e.g. created_by_staff_id) are not form inputs
        #     and cannot be set via the create-ticket form or API payload the same way.
        if scenario.get("test_mode") == "multi_session" and a_items:
            valid_fields = {a["name"] for a in a_items}
            bad_keys = [k for k in inputs if k not in valid_fields]
            if bad_keys:
                for k in bad_keys:
                    inputs.pop(k)
                scenario["inputs"] = inputs
                print(f"  SANITIZE {scenario.get('id','?')}: removed non-A-item input(s) {bad_keys} from multi_session scenario")

        # 2. Fix boundary string lengths — only for the field under test, not all fields
        if cat == "boundary":
            field_under_test = scenario.get("field")
            for a in a_items:
                length_m = re.search(r"maxlength\s*(\d+)", a["constraints"], re.IGNORECASE)
                if not length_m or a["name"] not in inputs:
                    continue
                n = int(length_m.group(1))
                val = str(inputs[a["name"]])
                # Reset fields that were incorrectly set to the boundary length by a previous bug
                # (single repeated character at exactly N or N+1 chars, not the field under test)
                if a["name"] != field_under_test and len(val) in (n, n + 1) and len(set(val)) == 1:
                    inputs[a["name"]] = field_defaults.get(a["name"], "Test input")
                    scenario["inputs"] = inputs
                if a["name"] != field_under_test:
                    continue
                if a["name"] not in inputs:
                    continue
                length_m = re.search(r"maxlength\s*(\d+)", a["constraints"], re.IGNORECASE)
                if not length_m:
                    continue
                n = int(length_m.group(1))
                val = str(inputs[a["name"]])
                desc = (scenario.get("description", "") + " " + scenario.get("assertion", "")).lower()
                target_len = n + 1 if ("exceed" in desc or "above" in desc) else n
                if len(val) != target_len:
                    inputs[a["name"]] = "A" * target_len
                    scenario["inputs"] = inputs

    return scenarios


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate scenario matrix from extracted context")
    parser.add_argument("--flow", required=True)
    parser.add_argument("--subdir", default=None, help="Manifest subdirectory (e.g. createReport)")
    parser.add_argument("--out",  default=None)
    parser.add_argument("--rebuild-inventory", action="store_true",
                        help="Force LLM rebuild of the inventory even if a cached version exists")
    args = parser.parse_args()

    context_path = TESTGEN_DIR / f"context_{args.flow}.json"
    out_path = Path(args.out) if args.out else TESTGEN_DIR / f"scenario_matrix_{args.flow}.json"
    inv_path = out_path.with_name(out_path.stem + "_inventory.txt")

    print(f"-- Scenario Generation -----------------------------------------")
    print(f"  flow:    {args.flow}")
    if args.subdir:
        print(f"  subdir:  {args.subdir}")

    context = json.loads(context_path.read_text(encoding="utf-8"))
    client = OpenAI(api_key=OPENAI_API_KEY)
    primary_role = context.get("primary_role") or args.flow.split("_")[0]

    # 1. Inventory — load from cache if available, otherwise call the LLM.
    # The cached file holds the fully post-processed inventory (hidden fields removed,
    # bad cascades stripped, permission-gate C-items removed) so it is safe to reuse
    # directly.  Pass --rebuild-inventory to force a fresh LLM call, e.g. after editing
    # source files or changing the inventory prompt.
    if inv_path.exists() and not args.rebuild_inventory:
        print(f"  [Cache] Inventory loaded from {inv_path.name}")
        print(f"          Pass --rebuild-inventory to regenerate from source.")
        inventory_text = inv_path.read_text(encoding="utf-8")
    else:
        inventory_text = build_inventory(args.flow, primary_role, context, client, subdir=args.subdir)
        inventory_text = post_process_inventory_remove_role_excluded(inventory_text, primary_role, context)
        inventory_text = sanitize_inventory_dependencies(inventory_text)
        inventory_text = strip_permission_gate_c_items(inventory_text)
        inv_path.write_text(inventory_text, encoding="utf-8")
        print(f"  Inventory saved -> {inv_path}")

    # Generate matrix using the cleaned inventory
    scenarios = generate_scenarios(args.flow, primary_role, context, inventory_text, client, subdir=args.subdir)
    scenarios, fixes = sanitise(scenarios, context.get("scenario_categories"))

    # Post-LLM sanitization: inject missing required fields, fix boundary string lengths
    entities = context.get("test_data", {}).get("known_entities", {})
    scenarios = sanitize_scenario_inputs(scenarios, inventory_text, entities)

    # Remove role_field_visibility absence tests for fields the primary role can see
    scenarios = filter_role_visibility_non_primary(scenarios, inventory_text)

    # Resolve second_session.key from staff emails + .env named credentials
    scenarios = resolve_second_session_keys(scenarios, entities, TESTGEN_DIR / ".env")

    # Enforce that inputs are consistent with each scenario's business_rule + expected_outcome
    scenarios = sanitize_multi_session_inputs(scenarios, entities, inventory_text)

    output = {"flow": args.flow, "inventory_text": inventory_text, "scenarios": scenarios}
    out_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"  Saved -> {out_path}")
    write_usage_log(args, out_path)

if __name__ == "__main__":
    main()