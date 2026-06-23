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


# ── CALL 1: Inventory prompt ──────────────────────────────────────────────────
INVENTORY_SYSTEM = """
You are a senior QA automation engineer. Your job is to read application source code and produce a complete, accurate inventory that will drive test scenario generation. Be exhaustive — missing an item here means a missing test later.

You will receive source code for a form (frontend + backend + models) and a flow_name that tells you the primary role for this flow.

Flow name format: the part before the first underscore is the primary role (e.g. a flow named "agent_…" has primary role "agent"; "customer_…" has primary role "customer").

════════════════════════════════════════════════════════════
INVENTORY A — FIELDS VISIBLE TO THE PRIMARY ROLE
════════════════════════════════════════════════════════════
Read the form source carefully. The form renders different fields for different roles using explicit JSX conditional rendering (e.g. `{isAdmin && (...)}`, `{!isCustomer && (...)}`), permission guards (e.g. `hasPerms`, `hasPermsV2`), or derived booleans from auth/context. When deciding visibility, always prefer explicit JSX conditionals and permission checks found next to the input elements over any default-initialized state values.
List ONLY the fields the primary role can see and interact with. Do not list fields rendered for other roles.

For each field write one line:
  A<n>. <field_name> | ui_label: <label text shown in the UI next to this field> | type: <text|select|multiselect|textarea|date|file|checkbox> | required: <yes|no|role-dependent> | constraints: <e.g. maxLength N, minValue X, email format, or "none"> | auto_filled: <yes|no>

ui_label: read the JSX that renders this field — find the <Typography>, <label>, <FormLabel>, or similar element next to the input and copy its text content exactly. 

Set auto_filled: yes if this field is populated automatically by a cascade (e.g., another field's selection triggers a state update, INCLUDING reverse cascades where selecting a child entity auto-fills the parent entity). The user does not type into auto-filled fields.
════════════════════════════════════════════════════════════
INVENTORY B — CASCADE CHAINS
════════════════════════════════════════════════════════════
A cascade is any relationship where changing Field X causes Field Y to update, filter, refetch, auto-fill, clear, or recalculate. 

CRITICAL ROLE CHECK: Before writing a B item, check your A items. If the target field (e.g., 'department') is NOT in Inventory A, YOU MUST NOT LIST the cascade. Cascades for hidden fields are strictly forbidden.
# Add this right after the "A cascade is any relationship..." line:
CRITICAL REVERSE CASCADES: Pay special attention to upward/reverse cascades. If selecting a child entity automatically populates its parent entity, you MUST list this as a B item with EFFECT: auto-fill.
For each cascade write one line:
  B<n>. <trigger_field> -> <target_field> | EFFECT: <filter|reset|auto-fill|calculate> | GUARD: <condition or none> | REVERSAL: <what happens when trigger is cleared/changed>

EFFECT definitions — choose the most accurate one:
  filter   : changing the trigger narrows the OPTIONS in the target dropdown, but the target field keeps its current value if still valid
  reset    : changing the trigger CLEARS the target field's current value entirely
  auto-fill: changing the trigger automatically populates the target field with a value
  calculate: changing the trigger recomputes a derived value 

MULTI-TRIGGER CASCADES — a single useEffect or onChange handler may depend on multiple fields (e.g. useEffect([fieldA, fieldB, fieldC])). When this happens, list it as ONE B item with all triggers separated by " + " in the trigger position.

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
════════════════════════════════════════════════════════════
INVENTORY D — POST-CREATION ACCESS RULES & SIDE-EFFECTS
════════════════════════════════════════════════════════════
Read the backend access-control and creation logic. Look for explicit visibility rules AND implicit access side-effects (e.g., users being automatically added to access lists, roles, or relationships during the creation transaction).
One rule per line:
  D<n>. <who> CAN/CANNOT see the record | WHEN: <condition from source>

════════════════════════════════════════════════════════════
INVENTORY E — ENTITY MAPPING FOR LOOKUP FIELDS
════════════════════════════════════════════════════════════
For each select/multiselect field in Inventory A, write:
  E<n>. FIELD: <name> | SOURCE: <known_entities path> | DISPLAY LABEL: <property the user sees in the dropdown>

Then list any cross-field constraints:
  CONSTRAINT: <field X> and <field Y> must share the same <parent entity>

CRITICAL: Only list mappings for fields that are present in Inventory A.

════════════════════════════════════════════════════════════
OUTPUT FORMAT
════════════════════════════════════════════════════════════
Write the five inventories as plain text, one item per line. No JSON, no markdown headers. Just the labelled lines. Be exhaustive.
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

role_field_visibility
  Per C<n>:
  - TYPE permission_gate: generate SHOWN TO (with permission, pass) and HIDDEN FROM (without permission, fail).
  - TYPE role_exclusion: generate only a HIDDEN FROM scenario. inputs must be empty ({}). expected_outcome: "pass". assertion: "<ui_label> field is not present in the DOM".

ui_capability
  One scenario per distinct field-type in Inventory A. Tests that the UI widget is physically usable.
  expected_outcome: "pass"

post_creation_visibility
  Per D<n>, generate BOTH:
    • Positive: session B user CAN see the record -> expected_outcome: "pass", assertion: "Session B user can view the record in the UI."
    • Negative: session B user CANNOT see the record -> expected_outcome: "pass", assertion: "Session B user cannot see the record in the UI."
  Session A inputs must configure the record so the D<n> condition is met for session B. Deduce the required fields from the D<n> rule.
  test_mode: "multi_session"
  second_session: { "role": "...", "name": "..." } using real names from known_entities.

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
  second_session   : { "role": "...", "name": "..." } or null
  business_rule    : inventory item reference (e.g. "A3")
  test_mode        : "ui" | "api" | "multi_session"

════════════════════════════════════════════════════════════
OUTPUT FORMAT
════════════════════════════════════════════════════════════
Return ONLY valid JSON:
{ "scenarios": [ { ...scenario... }, ... ] }
""".strip()


def build_inventory(flow_id: str, context: dict, client: OpenAI) -> str:
    user_msg = f"""Flow: {flow_id}
The flow_name above identifies the primary role for this flow — read it from the conditional rendering in the source and list ONLY the fields that role sees.

Here is the extracted source context:
{json.dumps(context, indent=2)}

Build the complete INVENTORY A through E for this flow. Be exhaustive. Use the exact line format from the instructions.
""".strip()

    print(f"  [Call 1] Building inventory...")
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": INVENTORY_SYSTEM},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0,
        max_tokens=MAX_TOKENS_INVENTORY,
    )
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

        # 4. Filter out CONSTRAINT lines targeting hidden fields
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

def _parse_inventory(inventory_text: str, allowed_categories: list[str]) -> str:
    lines = inventory_text.splitlines()
    a_items, b_items, c_items = [], [], []

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

    cat_set = set(allowed_categories)
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


def _generate_category(flow_id: str, category: str, inventory_text: str, entities: dict, checklist: str, client: OpenAI) -> list[dict]:
    user_msg = f"""Flow: {flow_id}\nGenerate ONLY this category: {category}\n\nINVENTORY:\n{inventory_text}\n\nKNOWN ENTITIES:\n{json.dumps(entities, indent=2)}\n\n{checklist}"""
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "system", "content": SCENARIO_SYSTEM}, {"role": "user", "content": user_msg}],
        temperature=0, max_tokens=MAX_TOKENS_SCENARIOS, response_format={"type": "json_object"},
    )
    return _parse_response(response.choices[0].message.content)


def generate_scenarios(flow_id: str, context: dict, inventory_text: str, client: OpenAI) -> list[dict]:
    entities = context.get("test_data", {}).get("known_entities", {})
    categories = context.get("scenario_categories", ["happy_path", "missing_field", "boundary", "invalid_value", "cascade_dependency", "role_field_visibility", "ui_capability", "post_creation_visibility"])
    all_scenarios = []

    for cat in categories:
        print(f"  [Call 2] Generating: {cat}...")
        batch = _generate_category(flow_id, cat, inventory_text, entities, _parse_inventory(inventory_text, [cat]), client)
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


def sanitize_multi_session_inputs(
    scenarios: list[dict],
    entities: dict,
) -> list[dict]:
    """
    For multi_session scenarios: enforce that inputs are consistent with the
    scenario's business_rule + expected_outcome + second_session.name.

    D1 pass  → second_session person must appear as accountable_staff_id or
                assigned_staff_id; inject accountable_staff_id if missing.
    D2 pass  → dept_id must be a department the second_session person belongs to;
                auto-correct dept_id if not.
    any fail → second_session person must NOT be accountable or assigned;
                remove if wrongly set. Warn if they're also in the ticket dept
                (the scenario would pass for the wrong reason).
    """
    def _staff_depts(name: str) -> set[str]:
        needle = name.strip().lower()
        depts: set[str] = set()
        for dept in entities.get("departments", []):
            for s in dept.get("staff", []):
                if s.get("name", "").strip().lower() == needle:
                    depts.add(dept.get("name", ""))
        return depts

    for scenario in scenarios:
        if scenario.get("test_mode") != "multi_session":
            continue
        second_sess = scenario.get("second_session")
        if not second_sess or not second_sess.get("name"):
            continue

        second_name = second_sess["name"]
        rule        = (scenario.get("business_rule") or "").upper()
        expected    = scenario.get("expected_outcome", "pass")
        inputs      = dict(scenario.get("inputs") or {})
        sid         = scenario.get("id", "?")
        changed     = False
        their_depts = _staff_depts(second_name)
        their_depts_lower = {d.lower() for d in their_depts}

        if expected == "pass":
            if rule.startswith("D1"):
                accountable = inputs.get("accountable_staff_id", "")
                assigned    = inputs.get("assigned_staff_id", "")
                if accountable != second_name and assigned != second_name:
                    dept = inputs.get("dept_id", "")
                    if dept and dept.lower() not in their_depts_lower:
                        print(f"  WARN {sid}: {second_name!r} not in dept {dept!r} — "
                              f"cannot inject accountable_staff_id (dept constraint)")
                    else:
                        inputs["accountable_staff_id"] = second_name
                        changed = True
                        print(f"  SANITIZE {sid}: set accountable_staff_id={second_name!r} "
                              f"to match second_session (D1 pass)")

            elif rule.startswith("D2"):
                dept = inputs.get("dept_id", "")
                if dept and dept.lower() not in their_depts_lower:
                    if their_depts:
                        inputs["dept_id"] = next(iter(their_depts))
                        changed = True
                        print(f"  SANITIZE {sid}: fixed dept_id to {inputs['dept_id']!r} "
                              f"({second_name!r} not in {dept!r}, D2 pass)")
                    else:
                        print(f"  WARN {sid}: {second_name!r} found in no dept — "
                              f"cannot auto-fix D2")

        elif expected == "fail":
            if inputs.get("accountable_staff_id") == second_name:
                inputs.pop("accountable_staff_id")
                changed = True
                print(f"  SANITIZE {sid}: removed accountable_staff_id={second_name!r} "
                      f"(fail scenario must not link second_session as accountable)")
            if inputs.get("assigned_staff_id") == second_name:
                inputs.pop("assigned_staff_id")
                changed = True
                print(f"  SANITIZE {sid}: removed assigned_staff_id={second_name!r} "
                      f"(fail scenario must not link second_session as assigned)")
            dept = inputs.get("dept_id", "")
            if dept and dept.lower() in their_depts_lower:
                print(f"  WARN {sid}: {second_name!r} IS in dept {dept!r} — "
                      f"fail scenario may incorrectly pass via D2 access")

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
    NEEDS_FULL_INPUTS = {"boundary", "invalid_value", "ui_capability", "happy_path"}

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

    # Required fields the test must supply.
    # We include ALL required fields regardless of auto_filled — auto_filled=yes means the
    # field CAN be populated by a cascade, not that it's always pre-populated.  Without a
    # cascade trigger in scope, ui_capability/boundary/happy_path scenarios still need a
    # real value or the form will reject on submit for the wrong reason.
    required_manual = [
        a["name"] for a in a_items
        if a["required"] in ("yes",)
    ]

    # Build a lookup for a_items by name
    a_lookup = {a["name"]: a for a in a_items}

    # Drop missing_field scenarios for auto_filled select fields — you can't test
    # "missing" a field the form populates automatically.
    def _is_invalid_missing_field(s: dict) -> bool:
        if s.get("category") != "missing_field":
            return False
        field = s.get("field")
        a = a_lookup.get(field)
        return bool(a and a["auto_filled"] == "yes" and a["type"] not in ("text", "textarea"))

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

        # 1. Inject missing required fields for submit categories
        if cat in NEEDS_FULL_INPUTS:
            changed = False
            for field in required_manual:
                if field not in inputs and field in field_defaults:
                    inputs[field] = field_defaults[field]
                    changed = True
            if changed:
                scenario["inputs"] = inputs

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
    parser.add_argument("--out",  default=None)
    args = parser.parse_args()

    context_path = TESTGEN_DIR / f"context_{args.flow}.json"
    out_path = Path(args.out) if args.out else TESTGEN_DIR / f"scenario_matrix_{args.flow}.json"

    print(f"-- Scenario Generation -----------------------------------------")
    print(f"  flow:    {args.flow}")
    
    context = json.loads(context_path.read_text(encoding="utf-8"))
    client = OpenAI(api_key=OPENAI_API_KEY)

    # 1. Build initial raw inventory
    inventory_text = build_inventory(args.flow, context, client)
    
    # 2. Extract explicitly hidden fields defined by the LLM
    primary_role = args.flow.split("_")[0]
    inventory_text = post_process_inventory_remove_role_excluded(inventory_text, primary_role, context)

    # 3. BULLETPROOF LAYER: Strip any cascades that point to hidden fields
    inventory_text = sanitize_inventory_dependencies(inventory_text)

    # Save cleaned inventory text
    inv_path = out_path.with_name(out_path.stem + "_inventory.txt")
    inv_path.write_text(inventory_text, encoding="utf-8")
    print(f"  Inventory saved -> {inv_path}")

    # Generate matrix using the cleaned inventory
    scenarios = generate_scenarios(args.flow, context, inventory_text, client)
    scenarios, fixes = sanitise(scenarios, context.get("scenario_categories"))

    # Post-LLM sanitization: inject missing required fields, fix boundary string lengths
    entities = context.get("test_data", {}).get("known_entities", {})
    scenarios = sanitize_scenario_inputs(scenarios, inventory_text, entities)

    # Resolve second_session.key from staff emails + .env named credentials
    scenarios = resolve_second_session_keys(scenarios, entities, TESTGEN_DIR / ".env")

    # Enforce that inputs are consistent with each scenario's business_rule + expected_outcome
    scenarios = sanitize_multi_session_inputs(scenarios, entities)

    output = {"flow": args.flow, "inventory_text": inventory_text, "scenarios": scenarios}
    out_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"  Saved -> {out_path}")

if __name__ == "__main__":
    main()