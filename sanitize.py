"""
sanitize.py — audit + auto-correct the populated scenario matrix.

Locked-field check (unchanged, report-only): every case's locked_fields (set by
generate1.py from the PRD) must be absent from that case's inputs. A violation means
generate2's LLM included a field the PRD says the user cannot set for that entry point.
Not auto-fixed here — re-run generate2.py.

New checks (auto-corrected in place, written back to the matrix):
  - field key correction: an inputs key that doesn't match any real `name="..."`
    attribute in the flow's frontend source is renamed to the closest real field
    name (e.g. generate2's code-analysis pass wrote "report_name" — the backend API
    param — instead of "name", the actual form field).
  - boundary inputs: a boundary-category (or boundary-worded) case's tested value must
    actually BE a string of the right length, not prose describing one (generate2 has
    written literal text like "A string exceeding 256 characters" instead of a real
    257-character string). Direction (at-limit vs over-limit) is read from
    expected_outcome, not from title wording, since code-analysis cases don't follow
    generate1's phrasing convention.
  - missing required fields: a case must carry every OTHER required field's real value,
    not just the one field intentionally left blank (or none, outside field_validation),
    or the test fails for the wrong reason. "Required" is read from the frontend source
    itself — a FormField's `showRequired={true}` prop, or a field named in the form's
    own `errors.<field> = true` submit-validation — never guessed. Optional fields
    (description, appointment_id, ...) are never injected even if a sibling case
    happens to use them. Fill values are majority-voted from sibling cases sharing the
    same entry_point — no PRD parsing needed.
  - name/value reconciliation: an inputs value for a field backed by a real record must
    match the frontend source's literal option labels (e.g. report_type's two option
    strings, reconstructed exactly including their getNamespace() splice) or
    known_entities/namespace_map — never an LLM paraphrase. Free-text fields (report
    name, description, ...) are left alone since they never match either source.

Usage:
    python approach3/sanitize.py --flow create_service_report

Exit codes:
    0 — no locked-field violations and no unresolved manual-review flags
    1 — locked-field violations and/or manual-review flags remain
"""

import argparse
import difflib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR     = Path(__file__).parent
MATRICES_DIR = BASE_DIR / "matrices"
CONTEXT_DIR  = BASE_DIR / "context"

# Cross-department ticket selection needs the TEST_<ROLE>_EMAIL logins.
load_dotenv(BASE_DIR / ".env")


# ── Locked-field audit (report-only) ─────────────────────────────────────────

def strip_locked_fields(matrix: list[dict]) -> tuple[list[str], list[str]]:
    """Actively removes any fields from inputs that the PRD designated as locked."""
    changes, flags = [], []
    for case in matrix:
        locked = set(case.get("locked_fields") or [])
        if not locked:
            continue
        
        for label, ins in _case_containers(case):
            bad = locked & set(ins.keys())
            for lock_key in bad:
                del ins[lock_key]
                changes.append(f"{case.get('case','?')}: Auto-removed locked field '{lock_key}' from {label}")
    
    return changes, flags

def check_case(case: dict) -> list[str]:
    """Return violation descriptions for this case (empty list if clean)."""
    locked = set(case.get("locked_fields") or [])
    if not locked:
        return []

    violations = []

    if "inputs" in case:
        bad = locked & set(case["inputs"].keys())
        if bad:
            violations.append(f"inputs contains locked keys: {sorted(bad)}")

    for i, session in enumerate(case.get("sessions") or []):
        bad = locked & set((session.get("inputs") or {}).keys())
        if bad:
            violations.append(f"sessions[{i}].inputs contains locked keys: {sorted(bad)}")

    return violations


# ── Shared helpers ────────────────────────────────────────────────────────────

def _case_containers(case: dict) -> list[tuple[str, dict]]:
    """Every (label, inputs_dict) pair on a case: top-level inputs + each session's."""
    containers = []
    if isinstance(case.get("inputs"), dict):
        containers.append(("inputs", case["inputs"]))
    for i, session in enumerate(case.get("sessions") or []):
        if isinstance(session, dict) and isinstance(session.get("inputs"), dict):
            containers.append((f"sessions[{i}].inputs", session["inputs"]))
    return containers


def _find_balanced(text: str, start: int, open_ch: str, close_ch: str) -> int:
    """Index just past the char matching text[start] (which must be open_ch), or -1."""
    depth = 0
    i = start
    while i < len(text):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _end_of_jsx_tag(content: str, start: int) -> int:
    """Index just past the first bare '>' at brace-depth 0, starting from a '<Tag' match."""
    depth = 0
    i = start
    while i < len(content):
        ch = content[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif depth == 0 and ch == ">":
            return i + 1
        i += 1
    return -1


def _iter_frontend_files(frontend: dict):
    for category in frontend.values():
        if not isinstance(category, dict):
            continue
        for content in category.values():
            if isinstance(content, str):
                yield content


# ── Field key correction ─────────────────────────────────────────────────────

_NAME_ATTR_RE = re.compile(r'name=\{?["\']([A-Za-z_][A-Za-z0-9_]*)["\']\}?')


def extract_field_names(frontend: dict) -> set[str]:
    """Every real `name="..."` attribute found in the flow's frontend source."""
    names = set()
    for content in _iter_frontend_files(frontend):
        names.update(_NAME_ATTR_RE.findall(content))
    return names


def _closest_field_key(bad_key: str, canonical_keys: set[str]) -> str | None:
    """Word-containment only — e.g. "report_name" -> "name". No fuzzy/edit-distance
    fallback: shared-prefix strings like "reportDate" vs "report_type" score high on
    plain character similarity despite being unrelated fields, and a wrong silent
    rename corrupts good data far worse than leaving an unmatched key flagged."""
    bad_words = set(bad_key.lower().replace("-", "_").split("_"))
    candidates = [
        key for key in canonical_keys
        if set(key.lower().split("_")) <= bad_words
    ]
    return max(candidates, key=len) if candidates else None  # prefer the most specific match


def correct_field_keys(matrix: list[dict], canonical_keys: set[str]) -> tuple[list[str], list[str]]:
    changes, flags = [], []
    if not canonical_keys:
        return changes, flags

    for case in matrix:
        for label, ins in _case_containers(case):
            for bad_key in list(ins.keys()):
                if bad_key in canonical_keys:
                    continue
                match = _closest_field_key(bad_key, canonical_keys)
                if match:
                    ins[match] = ins.pop(bad_key)
                    changes.append(f"{case.get('case','?')}: {label} key '{bad_key}' -> '{match}'")
                else:
                    flags.append(
                        f"{case.get('case','?')}: {label} key '{bad_key}' has no matching "
                        f"name=\"...\" attribute anywhere in the frontend source — verify manually"
                    )
    return changes, flags


# ── Boundary input correction ────────────────────────────────────────────────

_LENGTH_RE    = re.compile(r'(\d+)\s*characters?', re.IGNORECASE)
_MAXLENGTH_RE = re.compile(r'[Mm]ax[Ll]ength=\{(\d+)\}')


def _fill_to_length(n: int) -> str:
    filler = "Lorem ipsum dolor sit amet consectetur adipiscing elit "
    return (filler * (n // len(filler) + 1))[:n]


def extract_field_max_lengths(frontend: dict) -> dict[str, int]:
    """field_name -> maxLength from FormField JSX (e.g. maxLength={256})."""
    result: dict[str, int] = {}
    for content in _iter_frontend_files(frontend):
        for tag_match in _FORMFIELD_RE.finditer(content):
            end = _end_of_jsx_tag(content, tag_match.start())
            if end == -1:
                continue
            tag_text = content[tag_match.start():end]
            name_m = _NAME_IN_TAG_RE.search(tag_text)
            max_m  = _MAXLENGTH_RE.search(tag_text)
            if name_m and max_m:
                result[name_m.group(1)] = int(max_m.group(1))
    return result


def fix_boundary_inputs(matrix: list[dict], max_lengths: dict[str, int] | None = None) -> tuple[list[str], list[str]]:
    changes, flags = [], []

    for case in matrix:
        if case.get("category") not in ("boundary", "industry_best_practice"):
            continue

        ins = case.get("inputs")
        if not isinstance(ins, dict):
            continue
        string_fields = [k for k, v in ins.items() if isinstance(v, str)]

        text = f"{case.get('title', '')} {case.get('description', '')}"
        m = _LENGTH_RE.search(text)

        if m:
            limit = int(m.group(1))
        elif max_lengths and len(string_fields) == 1:
            # No explicit number in description — fall back to the field's maxLength in source
            field = string_fields[0]
            if field not in max_lengths:
                continue
            limit = max_lengths[field]
        else:
            continue

        # expected_outcome, not title wording, decides direction: code-analysis cases
        # describe the business rule ("does not exceed 256 characters") even when the
        # case itself is testing the over-limit failure path.
        outcome = (case.get("expected_outcome") or "").strip().lower()
        over_limit = outcome != "pass"
        target_len = limit + 1 if over_limit else limit

        if len(string_fields) != 1:
            if string_fields:
                flags.append(
                    f"{case.get('case','?')}: boundary limit {limit} found in title/description "
                    f"but {len(string_fields)} candidate text fields in inputs "
                    f"({string_fields}) — cannot tell which one to size, skipped"
                )
            else:
                flags.append(
                    f"{case.get('case','?')}: boundary limit {limit} found in title/description "
                    f"but inputs has no text field to size (likely a non-form business rule, "
                    f"e.g. a server-generated value) — skipped"
                )
            continue

        field = string_fields[0]
        current = ins[field]
        if len(current) == target_len:
            continue

        ins[field] = _fill_to_length(target_len)
        changes.append(
            f"{case.get('case','?')}: inputs['{field}'] was {len(current)} chars ({current!r}) "
            f"-> regenerated to exactly {target_len} chars "
            f"({'over' if over_limit else 'at'} the {limit}-character limit)"
        )

    return changes, flags


# ── Missing required-field completion ────────────────────────────────────────

# Categories that intentionally carry no (or only trigger) inputs — never force-fill them.
# ui: verifies a control is present/interactable in the DOM, submits nothing — execute routes
#   these to a DOM-visibility check ONLY when inputs is empty, so injecting fields silently
#   reroutes them to the fill-and-submit path.
# field_interaction: only the trigger field. permission: stops at the point access is denied.
_SPARSE_BY_DESIGN_CATEGORIES = {"field_interaction", "permission", "ui"}

_SHOW_REQUIRED_TRUE_RE = re.compile(r'showRequired=\{\s*true\s*\}')
_ERRORS_REQUIRED_RE    = re.compile(r'errors\.([A-Za-z_][A-Za-z0-9_]*)\s*=\s*true')
# A form that validates via a `requiredFields = ["a","b",...]` list (+ conditional
# `requiredFields.push("x")`) then `errors[field] = true` — the appointment/host forms use
# this bracket idiom, which the dot-notation _ERRORS_REQUIRED_RE above can't see.
_REQUIRED_ARRAY_RE = re.compile(r'requiredFields\s*=\s*\[([^\]]*)\]')
_REQUIRED_PUSH_RE  = re.compile(r'requiredFields\.push\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']')
_QUOTED_IDENT_RE   = re.compile(r'["\']([A-Za-z_][A-Za-z0-9_]*)["\']')


def extract_required_fields(frontend: dict) -> set[str]:
    """Field names the frontend source itself marks as required — never guessed.

    Unioned signals across every component file (idiom-agnostic so it works on any form):
      - <FormField ... name="x" ... showRequired={true} .../> (the UI's required-asterisk)
      - submit-time validation naming the field: `errors.x = true` (dot) OR a
        `requiredFields = ["x", ...]` / `requiredFields.push("x")` list feeding
        `errors[field] = true` (bracket) — the host/appointment-form idiom.

    Fields with no signal (description, appointment_id, ...) are optional and never
    force-filled. Over-inclusion is harmless: complete_missing_fields only fills a field
    some sibling case at the same entry_point actually uses.
    """
    required: set[str] = set()

    for content in _iter_frontend_files(frontend):
        for tag_match in _FORMFIELD_RE.finditer(content):
            end = _end_of_jsx_tag(content, tag_match.start())
            if end == -1:
                continue
            tag_text = content[tag_match.start():end]
            name_m = _NAME_IN_TAG_RE.search(tag_text)
            if name_m and _SHOW_REQUIRED_TRUE_RE.search(tag_text):
                required.add(name_m.group(1))

        required.update(_ERRORS_REQUIRED_RE.findall(content))
        for arr in _REQUIRED_ARRAY_RE.findall(content):
            required.update(_QUOTED_IDENT_RE.findall(arr))
        required.update(_REQUIRED_PUSH_RE.findall(content))

    return required


def _field_named_in_title(case: dict, candidate_fields) -> str | None:
    """The field a field_validation case names in its title (e.g. 'Validate Service Report
    Type is required' -> report_type). Generic: matches a field key's spaced form against the
    title/description. Returns None if zero or many match (ambiguous)."""
    text = f"{case.get('title','')} {case.get('description','')}".lower()
    matches = [f for f in candidate_fields if f.replace("_", " ").lower() in text]
    return matches[0] if len(matches) == 1 else None


def complete_missing_fields(
    matrix: list[dict],
    required_fields: set[str],
    known_entities: dict | None = None,
    field_options: dict | None = None,
) -> tuple[list[str], list[str]]:
    """Every case must carry a real value for every REQUIRED field this entry_point
    uses elsewhere in the matrix, unless: the field is locked for that case (never
    fill a locked field), the category is intentionally sparse by generate2.py's own
    POPULATE_PROMPT rules (field_interaction includes only the trigger field;
    permission stops at the point access is denied — filling either would corrupt a
    deliberately partial scenario, not fix a bug), or this is the one field a
    field_validation case deliberately leaves blank to test it. Optional fields are
    never injected — see extract_required_fields().

    Each field's fill value is derived from sibling cases sharing the same
    entry_point — no PRD parsing, the matrix itself is the ground truth for values.
    The field_validation deliberate-blank detection is scoped to field_validation
    siblings only (a field present in every OTHER field_validation case at this
    entry_point is this one's own tested field), so it isn't diluted by other
    categories that may legitimately omit different fields for other reasons.
    """
    changes, flags = [], []

    # Build a pool of verified entity values — used to guard against spreading hallucinated values.
    # field_options labels (pre-built composites) take priority; then all string values in known_entities.
    entity_pool: set[str] = set()
    fo = field_options or {}
    ke = known_entities or {}
    for opts in fo.values():
        for opt in opts:
            if isinstance(opt.get("label"), str):
                entity_pool.add(opt["label"])
    for collection in ke.values():
        if not isinstance(collection, list):
            continue
        for record in collection:
            if isinstance(record, dict):
                entity_pool.update(v for v in record.values() if isinstance(v, str) and v.strip())

    # Global fallback pool: real values used for a required field ANYWHERE in the matrix.
    # Used when a field has no value within its own entry_point group — a free-text required
    # field (e.g. name) may only be filled on happy-path cases in a different group, but any
    # valid value works, so borrow one rather than leaving the field blank (which would make
    # the case fail for the wrong reason).
    global_votes: dict[str, Counter] = defaultdict(Counter)
    for c in matrix:
        if c.get("category") in _SPARSE_BY_DESIGN_CATEGORIES:
            continue
        for k, v in (c.get("inputs") or {}).items():
            if k in required_fields and isinstance(v, str) and v.strip():
                global_votes[k][v] += 1

    groups: dict[str, list[dict]] = defaultdict(list)
    for case in matrix:
        groups[case.get("entry_point", "")].append(case)

    # all_entry_points cases apply universally — use them as a value baseline for every group
    universal_fillable = [
        c for c in groups.get("all_entry_points", [])
        if c.get("category") not in _SPARSE_BY_DESIGN_CATEGORIES
    ]

    for entry_point, cases in groups.items():
        fillable = [c for c in cases if c.get("category") not in _SPARSE_BY_DESIGN_CATEGORIES]
        if not fillable:
            continue

        # Vote pool: this group's cases plus universal baseline (avoids missing required
        # fields that only appear in all_entry_points cases like reportDate)
        vote_pool = fillable if entry_point == "all_entry_points" else fillable + universal_fillable

        all_keys: set[str] = set()
        value_votes: dict[str, Counter] = defaultdict(Counter)
        for c in vote_pool:
            for k, v in (c.get("inputs") or {}).items():
                if k not in required_fields:
                    continue  # optional field — never force-filled onto other cases
                all_keys.add(k)
                # Only a REAL value is a vote. A blank ("" / []) is a field left empty on
                # purpose (the case testing it) — counting it poisons the pool so every
                # sibling gets back-filled with "" instead of a real value.
                if isinstance(v, str) and v.strip():
                    value_votes[k][v] += 1

        for c in fillable:
            ins = c.setdefault("inputs", {})
            locked = set(c.get("locked_fields") or [])
            missing = all_keys - set(ins.keys()) - locked

            # A field_validation case leaves exactly ONE required field empty to test it.
            # That tested field is the one present as "" (a deliberate blank) — which is NOT
            # in `missing`, so every genuinely-absent field here is a plain omission we should
            # fill. Only if the LLM left NO blank (it omitted the tested field entirely) do we
            # need to protect one: identify it from the case title so we don't fill it back in.
            tested_field = None
            if c.get("category") == "field_validation":
                has_blank = any(
                    (isinstance(v, str) and not v.strip()) or v == []
                    for v in ins.values()
                )
                if not has_blank:
                    tested_field = _field_named_in_title(c, all_keys)

            for field in sorted(missing):
                if field == tested_field:
                    continue  # the tested field the LLM omitted — must stay blank

                votes = value_votes.get(field) or global_votes.get(field)
                if votes:
                    fill_value, _ = votes.most_common(1)[0]
                    # If this field is entity-backed (any of its votes appear in the
                    # verified entity pool) the fill value must also be in that pool.
                    # This prevents spreading hallucinated composite labels (e.g. a
                    # ticket_id label like "0626400001 - 12345" that doesn't exist in DB).
                    field_is_entity_backed = entity_pool and any(
                        v in entity_pool for v in votes.keys()
                    )
                    if field_is_entity_backed and fill_value not in entity_pool:
                        flags.append(
                            f"{c.get('case','?')}: missing field '{field}', most-voted fill "
                            f"value {fill_value!r} is not in known_entities — skipped to avoid "
                            f"spreading a hallucinated value"
                        )
                        continue
                    ins[field] = fill_value
                    changes.append(
                        f"{c.get('case','?')}: filled missing field '{field}' = {fill_value!r} "
                        f"(value used elsewhere for entry_point '{entry_point}')"
                    )
                else:
                    flags.append(
                        f"{c.get('case','?')}: missing field '{field}', no other case at "
                        f"entry_point '{entry_point}' has a value for it — needs manual value"
                    )

    return changes, flags


# ── Setup-only field injection (locked but structurally required) ───────────
#
# "Locked" means the PRD treats the field as an already-established precondition
# that the test must not manipulate as part of the behavior under test. ticket_id
# is locked for both the ticket-tab and appointment-creation entry points, but
# neither base script needs the matrix to supply a specific value for it — both
# fall back to picking whatever ticket is first available in the live UI when
# none is given (see base_test_create_service_report_from_from_within_a_ticket_
# service_reports_tab.py and base_test_create_service_report_from_from_the_
# appointment_creation_form.py). So no matrix-side injection is needed here.
#
# The appointment-creation form only renders its SR-type ("Time & Material") field
# after the service_reports multiselect includes the synthetic "new" option — see
# CreateAppointmentForm.js: `formData.service_reports.includes("new")` gates
# `jobSelectShow`. That field itself is optional in general (showRequired={false},
# you can create an appointment with no SR attached), so extract_required_fields()
# correctly never flags it as required — but every scenario at this entry_point is
# specifically about creating an SR *alongside* the appointment, so it must always
# be set here regardless of the form's general optionality.

def inject_appointment_service_report_selection(
    matrix: list[dict], namespace_map: dict | None
) -> tuple[list[str], list[str]]:
    changes, flags = [], []
    label = f"Create New {_resolve_get_namespace(namespace_map or {}, 'service_report', 'capitalize: true, plural: false')}"

    for case in matrix:
        if case.get("entry_point") != "From the Appointment Creation Form":
            continue
        ins = case.setdefault("inputs", {})
        if ins.get("service_report"):
            continue
        ins["service_report"] = label
        changes.append(
            f"{case.get('case','?')}: injected 'service_report' = {label!r} — this entry "
            f"point's scenarios require creating a new SR alongside the appointment, which "
            f"needs the 'Create New Service Report' option checked even though the field "
            f"is optional on the form in general"
        )

    return changes, flags


# ── Source-literal option reconstruction (getNamespace-aware) ────────────────

_FORMFIELD_RE     = re.compile(r'<FormField\b')
_NAME_IN_TAG_RE   = re.compile(r'name=["\']([A-Za-z_][A-Za-z0-9_]*)["\']')
_OPTIONS_START_RE = re.compile(r'options=\{\[')
_OPTION_LABEL_RE  = re.compile(r'label:\s*(`[^`]*`|"[^"]*"|\'[^\']*\')')
_OPTION_VALUE_RE  = re.compile(r'value:\s*("[^"]*"|\'[^\']*\')')
_GET_NS_RE        = re.compile(r'getNamespace\(\s*["\']([\w]+)["\']\s*(?:,\s*\{([^}]*)\})?\s*\)')


def _to_capital_case(s: str) -> str:
    s = s.replace("_", " ").replace("-", " ")
    return re.sub(r"\b\w", lambda m: m.group(0).upper(), s)


def _to_plural(s: str) -> str:
    if s.endswith(("s", "x", "z", "ch", "sh")):
        return s + "es"
    if s.endswith("y") and len(s) > 1 and s[-2].lower() not in "aeiou":
        return s[:-1] + "ies"
    return s + "s"


def _to_camel_case(s: str) -> str:
    parts = [p for p in re.split(r"[\s_-]+", s) if p]
    if not parts:
        return s
    return parts[0].lower() + "".join(p[:1].upper() + p[1:].lower() for p in parts[1:])


def _resolve_get_namespace(namespace_map: dict, key: str, opts_text: str) -> str:
    """Mirrors NamespaceContext.js's getNamespace(): lookup then plural/camel/capitalize,
    in that exact order — matching the JS implementation line for line."""
    value = namespace_map.get(key, key)
    opts_text = opts_text or ""
    if re.search(r'plural:\s*true', opts_text):
        value = _to_plural(value)
    if re.search(r'camel:\s*true', opts_text):
        value = _to_camel_case(value)
    if re.search(r'capitalize:\s*true', opts_text):
        value = _to_capital_case(value)
    return value


def _resolve_template_literal(body: str, namespace_map: dict) -> str:
    out = []
    i = 0
    while i < len(body):
        if body[i:i + 2] == "${":
            end = _find_balanced(body, i + 1, "{", "}")
            if end == -1:
                out.append(body[i:])
                break
            expr = body[i + 2:end - 1]
            m = _GET_NS_RE.search(expr)
            out.append(_resolve_get_namespace(namespace_map, m.group(1), m.group(2)) if m else expr)
            i = end
        else:
            out.append(body[i])
            i += 1
    return "".join(out)


def _resolve_label_literal(raw: str, namespace_map: dict) -> str:
    quote, body = raw[0], raw[1:-1]
    return _resolve_template_literal(body, namespace_map) if quote == "`" else body


def extract_source_options(frontend: dict, namespace_map: dict) -> dict[str, dict[str, str]]:
    """field_name -> {submitted_value: resolved_display_label}, for every FormField whose
    options are literal in the source (not fetched from an API at runtime)."""
    result: dict[str, dict[str, str]] = {}

    for content in _iter_frontend_files(frontend):
        for tag_match in _FORMFIELD_RE.finditer(content):
            end = _end_of_jsx_tag(content, tag_match.start())
            if end == -1:
                continue
            tag_text = content[tag_match.start():end]

            name_m = _NAME_IN_TAG_RE.search(tag_text)
            opts_m = _OPTIONS_START_RE.search(tag_text)
            if not name_m or not opts_m:
                continue

            arr_start = opts_m.end() - 1  # index of the "["
            arr_end = _find_balanced(tag_text, arr_start, "[", "]")
            if arr_end == -1:
                continue
            options_text = tag_text[arr_start:arr_end]

            field_options = result.setdefault(name_m.group(1), {})
            pos = 0
            while True:
                brace = options_text.find("{", pos)
                if brace == -1:
                    break
                obj_end = _find_balanced(options_text, brace, "{", "}")
                if obj_end == -1:
                    break
                obj_text = options_text[brace:obj_end]
                label_m = _OPTION_LABEL_RE.search(obj_text)
                value_m = _OPTION_VALUE_RE.search(obj_text)
                if label_m and value_m:
                    field_options[value_m.group(1)[1:-1]] = _resolve_label_literal(
                        label_m.group(1), namespace_map
                    )
                pos = obj_end

    return result


# ── Name/value reconciliation ────────────────────────────────────────────────

def reconcile_values(
    matrix: list[dict],
    source_options: dict[str, dict[str, str]],
    known_entities: dict,
    namespace_map: dict,
    field_options: dict | None = None,
) -> tuple[list[str], list[str]]:
    changes, flags = [], []

    entity_pool: set[str] = set()
    for collection in known_entities.values():
        if not isinstance(collection, list):
            continue
        for entity in collection:
            if not isinstance(entity, dict):
                continue
            entity_pool.update(v for v in entity.values() if isinstance(v, str) and v.strip())
    entity_pool.update(v for v in namespace_map.values() if isinstance(v, str))

    occurrences: dict[str, list[tuple[str, str, dict, str]]] = defaultdict(list)
    for case in matrix:
        case_id = case.get("case", "?")
        for label, ins in _case_containers(case):
            for field, value in ins.items():
                if value == "" or value is None:
                    continue
                if isinstance(value, str):
                    occurrences[field].append((case_id, label, ins, value))

    for field, occs in occurrences.items():
        valid_labels = source_options.get(field)
        if valid_labels:
            labels = set(valid_labels.values())
            for case_id, label, ins, value in occs:
                if value in labels:
                    continue
                # substring containment first: safe here because the option set is small
                # and closed (sourced directly from code), so an unambiguous single match
                # (e.g. "Fixed Fee" inside only one of two real labels) is trustworthy in
                # a way a short fuzzy-ratio match against dozens of field names would not be
                containing = [l for l in labels if value.lower() in l.lower()]
                corrected = containing[0] if len(containing) == 1 else None
                if not corrected and value.isupper() and 2 <= len(value) <= 6:
                    # Abbreviation matching: the LLM sometimes emits domain
                    # shorthand ("TM", "FFM") instead of the rendered label.
                    # Compare against each label's word initials
                    # ("Time & Material" -> "TM", "Fixed Fee & Material" -> "FFM").
                    by_initials = [
                        l for l in labels
                        if "".join(w[0] for w in re.findall(r"[A-Za-z]+", l)).upper() == value
                    ]
                    corrected = by_initials[0] if len(by_initials) == 1 else None
                if not corrected:
                    close = difflib.get_close_matches(value, list(labels), n=1, cutoff=0.6)
                    corrected = close[0] if close else None
                if corrected:
                    ins[field] = corrected
                    changes.append(
                        f"{case_id}: {label}['{field}'] {value!r} -> {corrected!r} "
                        f"(matched the field's real frontend option label)"
                    )
                else:
                    flags.append(
                        f"{case_id}: {label}['{field}'] = {value!r} matches none of the real "
                        f"option labels for '{field}': {sorted(labels)}"
                    )
            continue  # source options are authoritative for this field — done

        # composite labels pre-built by extract.py from .map() templates
        if field_options and field in field_options:
            labels = {str(opt["label"]) for opt in field_options[field] if "label" in opt}
            # Map raw values (IDs) back to their correct labels in case generate2 used the ID
            id_to_label = {str(opt["value"]): str(opt["label"]) for opt in field_options[field] if "value" in opt and "label" in opt}
            
            for case_id, label, ins, value in occs:
                val_str = str(value)
                if val_str in labels:
                    continue
                
                # Check if the LLM output the raw ID (e.g., 3) instead of the label
                if val_str in id_to_label:
                    corrected = id_to_label[val_str]
                    ins[field] = corrected
                    changes.append(
                        f"{case_id}: {label}['{field}'] {value!r} -> {corrected!r} "
                        f"(corrected raw ID back to pre-built composite label)"
                    )
                    continue
                
                containing = [l for l in labels if val_str.lower() in l.lower()]
                if containing:
                    corrected = containing[0]
                    ins[field] = corrected
                    changes.append(
                        f"{case_id}: {label}['{field}'] {value!r} -> {corrected!r} "
                        f"(corrected substring match to full pre-built composite label)"
                    )
                    continue

                close = difflib.get_close_matches(val_str, list(labels), n=1, cutoff=0.5)
                if close:
                    ins[field] = close[0]
                    changes.append(
                        f"{case_id}: {label}['{field}'] {value!r} -> {close[0]!r} "
                        f"(corrected to match pre-built composite label)"
                    )
                else:
                    flags.append(
                        f"{case_id}: {label}['{field}'] = {value!r} matches none of the "
                        f"pre-built labels for '{field}' — possible hallucinated format"
                    )
            continue

        # no literal options in source for this field; only validate it against
        # known_entities/namespace_map if it's actually entity-backed somewhere in the
        # matrix — otherwise it's free text (report name, description, ...) and any
        # "non-match" would be a false positive, not a bug
        if not any(v in entity_pool for *_, v in occs):
            continue

        for case_id, label, ins, value in occs:
            if value in entity_pool:
                continue
            close = difflib.get_close_matches(value, list(entity_pool), n=1, cutoff=0.75)
            if close:
                ins[field] = close[0]
                changes.append(
                    f"{case_id}: {label}['{field}'] {value!r} -> {close[0]!r} "
                    f"(corrected to match known_entities/namespace_map)"
                )
            else:
                flags.append(
                    f"{case_id}: {label}['{field}'] = {value!r} does not match any "
                    f"known_entities/namespace_map value for a field that is entity-backed "
                    f"elsewhere in the matrix — possible hallucinated name"
                )

    return changes, flags


# expected_outcome is taken verbatim from the LLM (generate2's POPULATE_PROMPT defines it from
# the runner's POV: "pass" = the create action succeeds, "fail" = it's correctly prevented).
# There is deliberately NO sanitize override here: an earlier prose-keyword heuristic wrongly
# flipped correct "pass" labels to "fail" whenever the expected_result mentioned an internal
# SR's customer-invisibility ("cannot see" / "not visible") or a toggle's "disabled" state.
# The LLM labels these correctly on its own; keyword-matching prose does not.


# ── Skip cases needing an entity state that doesn't exist ────────────────────────

_TERMINAL_STATE_RE = re.compile(r"\b(closed|resolved|archived|deleted|terminal|inactive|cancell?ed|expired)\b", re.I)


def skip_absent_state_cases(matrix: list[dict], known_entities: dict) -> tuple[list[str], list[str]]:
    """If a case's prose says it needs an entity in a terminal/inactive state but
    known_entities holds no inactive record of any kind, it can't be tested with real
    data — mark it skip instead of letting it run blank and fail for the wrong reason.
    Generic: extract.py already splits active_* / inactive_* for every entity type."""
    has_inactive = any(
        isinstance(v, list) and v
        for k, v in (known_entities or {}).items()
        if k.startswith("inactive_")
    )
    if has_inactive:
        return [], []   # real terminal data exists — let these cases run
    changes: list[str] = []
    for c in matrix:
        if c.get("skip"):
            continue
        prose = " ".join([
            c.get("title", ""), c.get("expected_result", ""),
            " ".join(c.get("preconditions") or []),
        ])
        # "non-terminal" / "non closed" describe a NORMAL record the case needs (which exists) —
        # strip the negation so it can't trip the terminal-state match below (the PRD says
        # appointments must be against a non-terminal ticket, which was false-skipping them).
        prose = re.sub(r"\bnon[-\s]?(closed|resolved|archived|deleted|terminal|inactive|expired)\b",
                       " ", prose, flags=re.I)
        if _TERMINAL_STATE_RE.search(prose):
            c["skip"] = True
            c["skip_reason"] = "requires a terminal/inactive record; none exist in known_entities"
            c["inputs"] = {}
            changes.append(f"{c.get('case','?')}: skipped — needs a terminal/inactive record, none in known_entities")
    return changes, []


# ── Cross-department ticket selection (uses .env login → staff → dept) ───────────

_CROSS_DEPT_RE = re.compile(r"cross[-\s]?depart|outside\s+(?:their|the)\s+(?:primary\s+)?depart|different\s+depart", re.I)


def _persona_login_email(persona: str) -> str | None:
    """Mirror execute.py's persona → TEST_<ROLE>_EMAIL resolution, without importing it
    (execute pulls in playwright). Generic: derives the env var name from the persona."""
    key = (persona or "").strip().upper().replace(" ", "_").replace("-", "_")
    if key in ("", "ANY_AUTHORIZED_ROLE"):
        key = "AGENT"
    return os.getenv(f"TEST_{key}_EMAIL") or None


def _staff_departments(email: str, staff_rows: list) -> tuple[set[int], str | None]:
    """dept_ids the given login belongs to, matched by email local-part against staff records."""
    local = email.split("@")[0].lower()
    for s in staff_rows or []:
        if not isinstance(s, dict):
            continue
        s_email = str(s.get("email", "")).split("@")[0].lower()
        if s_email and s_email == local:
            depts: set[int] = set()
            dr = s.get("dept_roles")
            if isinstance(dr, str):
                for m in re.finditer(r"'dept_id'\s*:\s*(\d+)", dr):
                    depts.add(int(m.group(1)))
            elif isinstance(dr, list):
                for r in dr:
                    if isinstance(r, dict) and "dept_id" in r:
                        depts.add(int(r["dept_id"]))
            return depts, str(s.get("staff_id")) if s.get("staff_id") is not None else None
    return set(), None


def apply_cross_dept_tickets(matrix: list[dict], known_entities: dict, cascade_entities: dict) -> tuple[list[str], list[str]]:
    """For cross-department permission scenarios, set ticket_id to a ticket in a department
    the acting login is NOT a member of but still participates in (assigned/accountable/
    creator — so they're allowed to create an SR there per the cross-dept collaboration rule).
    Fully data-driven off dept_id + the persona→login→staff mapping; nothing hardcoded."""
    changes, flags = [], []
    staff_rows = known_entities.get("staff_with_roles") or []
    tickets = (cascade_entities.get("active_tickets") or known_entities.get("active_tickets") or [])
    for c in matrix:
        if c.get("skip"):
            continue
        prose = f"{c.get('title','')} {c.get('expected_result','')} {c.get('description','')}"
        if not _CROSS_DEPT_RE.search(prose):
            continue
        if "ticket_id" not in (c.get("inputs") or {}):
            continue  # ticket-tab / appointment flows lock the ticket — not our concern
        email = _persona_login_email(c.get("persona", ""))
        if not email:
            continue
        depts, staff_id = _staff_departments(email, staff_rows)
        if not depts or staff_id is None:
            flags.append(f"{c.get('case','?')}: cross-dept — could not resolve login '{email}' to a staff dept; left as-is")
            continue
        pick = None
        for t in tickets:
            if not isinstance(t, dict) or t.get("dept_id") in depts:
                continue
            participants = {str(t.get(k)) for k in ("assigned_staff_id", "accountable_staff_id", "created_by_staff_id")}
            if staff_id in participants:
                pick = t
                break
        if pick is None:
            c["skip"] = True
            c["skip_reason"] = f"no cross-department ticket the acting login participates in exists in known data"
            changes.append(f"{c.get('case','?')}: cross-dept — no eligible ticket, skipped")
            continue
        label = f"{pick.get('ticket_number','')} - {pick.get('ticket_subject','')}".strip()
        old = c["inputs"].get("ticket_id")
        if old != label:
            c["inputs"]["ticket_id"] = label
            changes.append(f"{c.get('case','?')}: ticket_id -> {label!r} (cross-dept: dept {pick.get('dept_id')} not in login's {sorted(depts)})")
    return changes, flags


# ── Report ────────────────────────────────────────────────────────────────────

def write_report_md(path: Path, violations: dict[str, list[str]], changes: list[str], flags: list[str]) -> None:
    lines = ["# Sanitize Report\n", "## Locked-field violations (not auto-fixed — re-run generate2.py)\n"]
    if violations:
        for case_id in sorted(violations):
            lines.append(f"- **{case_id}**")
            lines.extend(f"  - {msg}" for msg in violations[case_id])
    else:
        lines.append("None.")

    lines.append("\n## Auto-corrected\n")
    lines.extend(f"- {c}" for c in changes) if changes else lines.append("None.")

    lines.append("\n## Needs manual review\n")
    lines.extend(f"- {f}" for f in flags) if flags else lines.append("None.")

    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Audit + auto-correct the populated scenario matrix.")
    parser.add_argument("--flow", required=True, help="Flow name (e.g. create_service_report)")
    args = parser.parse_args()

    matrix_path  = MATRICES_DIR / f"scenario_matrix_{args.flow}.json"
    context_path = CONTEXT_DIR / f"context_{args.flow}.json"
    if not matrix_path.exists():
        print(f"ERROR: matrix not found: {matrix_path}", file=sys.stderr)
        sys.exit(1)
    if not context_path.exists():
        print(f"ERROR: context not found: {context_path}", file=sys.stderr)
        sys.exit(1)

    matrix  = json.loads(matrix_path.read_text(encoding="utf-8"))
    context = json.loads(context_path.read_text(encoding="utf-8"))
    test_data         = context.get("test_data", {})
    known_entities    = test_data.get("known_entities", {})
    cascade_entities  = test_data.get("cascade_entities", {})
    namespace_map     = test_data.get("namespace_map", {})
    field_options     = test_data.get("field_options", {})
    frontend          = context.get("frontend", {})

    violations: dict[str, list[str]] = {}
    for case in matrix:
        v = check_case(case)
        if v:
            violations[case.get("case", "?")] = v

    canonical_keys = extract_field_names(frontend)
    key_changes, key_flags = correct_field_keys(matrix, canonical_keys)

    lock_changes, lock_flags = strip_locked_fields(matrix)
    violations: dict[str, list[str]] = {}
    for case in matrix:
        v = check_case(case)
        if v:
            violations[case.get("case", "?")] = v
    # Reconcile values BEFORE complete_missing_fields so corrected values are
    # what get voted on and spread — prevents propagating hallucinated labels.
    source_options = extract_source_options(frontend, namespace_map)
    name_changes, name_flags = reconcile_values(matrix, source_options, known_entities, namespace_map, field_options)

    required_fields = extract_required_fields(frontend)
    missing_changes, missing_flags = complete_missing_fields(matrix, required_fields, known_entities, field_options)

    sr_sel_changes, sr_sel_flags = inject_appointment_service_report_selection(matrix, namespace_map)

    max_lengths = extract_field_max_lengths(frontend)
    boundary_changes, boundary_flags = fix_boundary_inputs(matrix, max_lengths)

    # Data-availability skips + cross-dept ticket selection. (expected_outcome is left as the
    # LLM produced it — see note above; no sanitize override.)
    skip_changes, skip_flags         = skip_absent_state_cases(matrix, known_entities)
    xdept_changes, xdept_flags       = apply_cross_dept_tickets(matrix, known_entities, cascade_entities)

    all_changes = (lock_changes + key_changes + name_changes + missing_changes
                   + sr_sel_changes + boundary_changes
                   + skip_changes + xdept_changes)
    all_flags   = (lock_flags + key_flags + name_flags + missing_flags
                   + sr_sel_flags + boundary_flags
                   + skip_flags + xdept_flags)

    if all_changes:
        matrix_path.write_text(json.dumps(matrix, indent=2), encoding="utf-8")

    print(f"Checked {len(matrix)} cases in matrices/{matrix_path.name}\n")

    if violations:
        print(f"LOCKED-FIELD VIOLATIONS  {len(violations)} case(s) — not auto-fixed, re-run generate2.py:")
        for case_id in sorted(violations):
            for msg in violations[case_id]:
                print(f"  {case_id}: {msg}")
        print()

    if all_changes:
        print(f"AUTO-CORRECTED  {len(all_changes)} change(s):")
        for c in all_changes:
            print(f"  {c}")
        print()
    else:
        print("No auto-correctable issues found.\n")

    if all_flags:
        print(f"NEEDS MANUAL REVIEW  {len(all_flags)} item(s):")
        for f in all_flags:
            print(f"  {f}")
        print()

    report_path = MATRICES_DIR / f"sanitize_report_{args.flow}.md"
    write_report_md(report_path, violations, all_changes, all_flags)
    print(f"Full report -> matrices/{report_path.name}")

    sys.exit(1 if (violations or all_flags) else 0)


if __name__ == "__main__":
    main()
