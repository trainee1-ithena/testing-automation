# iSERV Agentic Test Generation Pipeline

Automatically generates and runs Playwright UI tests for iSERV forms by reading source code, building a scenario matrix with an LLM, recording interactions with Playwright Codegen, and executing scenarios against the live application.

---

## Table of Contents

1. [How It Works](#how-it-works)
2. [Prerequisites](#prerequisites)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Running the Pipeline](#running-the-pipeline)
6. [Recording a New Flow](#recording-a-new-flow)
7. [Running Individual Steps](#running-individual-steps)
8. [Pipeline Flags Reference](#pipeline-flags-reference)
9. [Output Files](#output-files)
10. [Adding a New Flow](#adding-a-new-flow)
11. [Methodology](#methodology)

---

## How It Works

```
Source Code                                   Live App
(JS/JSX + models)                             (React + Express)
      │                                              │
      ▼                                              │
 extract.py           ── builds context JSON         │
      │                                              │
      ▼                                              │
 generate.py          ── LLM builds scenario matrix  │
      │                                              │
      ▼                                              │
 apply_sanitize.py    ── fills missing required inputs│
      │                                              │
      ▼                                              │
 record.py --build    ── parameterises base test      │
      │                                              │
      ▼                                              ▼
 execute.py           ── runs each scenario ──────────
      │
      ▼
 report.py            ── Excel report
```

Five scenario categories are handled automatically:

| Category | How it runs |
|---|---|
| `happy_path`, `boundary`, `invalid_value`, `ui_capability` | Full form interaction via `base_test_{flow}.py` |
| `missing_field` | Same form flow; form validation catches the missing field |
| `cascade_dependency` | Opens form, selects trigger field, waits for API response |
| `role_field_visibility` | Opens form, asserts the field is absent from the DOM |
| `multi_session` | Session A creates ticket via API; Session B verifies visibility via Playwright |

---

## Prerequisites

- **Python 3.11+**
- **Node.js** — iSERV frontend and backend must be running locally
- **iSERV running locally:**
  - Frontend: `http://localhost:3000`
  - Backend API: `http://localhost:5000`
- **OpenAI API key** — used by `generate.py` to build the scenario matrix
- **Test accounts** — one agent login and one customer login in your local iSERV instance

---

## Installation

```powershell
# 1. Install Python dependencies
pip install -r testGen/requirements.txt

# 2. Install the Playwright browser (Chromium is the default)
playwright install chromium
```

---

## Configuration

Copy the example env file and fill in your values:

```powershell
copy testGen\.env.example testGen\.env
```

Open `testGen/.env` and set:

```env
# OpenAI — used for scenario matrix generation
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o

# iSERV app URLs
FRONTEND_URL=http://localhost:3000
API_BASE_URL=http://localhost:5000

# Agent login (for agent_create_ticket flow)
TEST_AGENT_EMAIL=agent@example.com
TEST_AGENT_PASSWORD=yourpassword

# Customer login (for customer_create_ticket flow)
TEST_CUSTOMER_EMAIL=customer@example.com
TEST_CUSTOMER_PASSWORD=yourpassword

# Optional — second accounts for multi-session visibility tests
TEST_SECOND_AGENT_EMAIL=
TEST_SECOND_AGENT_PASSWORD=
TEST_SECOND_CUSTOMER_EMAIL=
TEST_SECOND_CUSTOMER_PASSWORD=

# Playwright
PLAYWRIGHT_BROWSER=chromium
PLAYWRIGHT_HEADLESS=true
PLAYWRIGHT_TIMEOUT_MS=10000
```

> `testGen/.env` is gitignored. Never commit it.

---

## Running the Pipeline

> **Windows note:** Set `$env:PYTHONUTF8 = "1"` before running any pipeline command to avoid encoding errors with Unicode output.

```powershell
$env:PYTHONUTF8 = "1"
```

### Full pipeline (first time)

The first time you run a flow, you need to record the happy-path interaction manually (Step 3 below). After that, the whole pipeline can run unattended.

```powershell

# Step 1: record (interactive — see Recording section below)
python testGen/record.py --record customer_create_ticket

# Step 2: run the entire pipeline
python testGen/pipeline.py --flow customer_create_ticket 
```

Available flows: `customer_create_ticket`, `agent_create_ticket`, `post_creation_visibility`

---

## Recording a New Flow

Recording captures a single happy-path interaction using Playwright Codegen. It only needs to be done once per flow (or when the form changes significantly).


### Step 1 — Run the recorder

```powershell
python testGen/record.py --record customer_create_ticket
```

A browser opens at the form start page. You are already logged in.

**Do this in the browser:**
1. Open the "New Case" modal
2. Fill **every field** in order (equipment → service type → priority → subject → description → file)
3. Submit the form
4. Close the browser window

The raw recording is saved to `testGen/base_test_customer_create_ticket_recorded.py`.

### Step 2 — Build the parameterised base script

```powershell
python testGen/record.py --build customer_create_ticket
```

This reads the recorded file, replaces hardcoded values with `inputs.get(field)` calls, and writes the clean `testGen/base_test_customer_create_ticket.py`. You don't need to edit that file.

> **Important:** Assertions are NOT put into the base script. They are injected by `execute.py` at run time, so re-recording never overwrites them.

---

## Running Individual Steps

Each stage can be run independently:

```powershell
# Stage 1: extract source context into context_{flow}.json
python testGen/extract.py --flow customer_create_ticket

# Stage 2: generate scenario matrix (calls OpenAI)
python testGen/generate.py --flow customer_create_ticket

# Stage 2b: re-apply sanitization to existing matrix without re-calling OpenAI
python testGen/apply_sanitize.py --flow customer_create_ticket

# Stage 3a: record (interactive)
python testGen/record.py --record customer_create_ticket

# Stage 3b: build parameterised base script from recorded file
python testGen/record.py --build customer_create_ticket

# Stage 4: execute all scenarios
python testGen/execute.py --flow customer_create_ticket

# Stage 4 options:
python testGen/execute.py --flow customer_create_ticket --dry-run      # print plan, no browser
python testGen/execute.py --flow customer_create_ticket --id TC003     # run one scenario

# Stage 5: generate Excel report
python testGen/report.py --flow customer_create_ticket
```

---

## Pipeline Flags Reference

```
python testGen/pipeline.py --flow <flow_name> [options]

--flow        Flow name: customer_create_ticket | agent_create_ticket | post_creation_visibility
--skip        Space-separated list of steps to skip
--only        Space-separated list of steps to run (ignores --skip)
--dry-run     Pass --dry-run to the execute step (no browser launched)
--id TC###    Run a single scenario in the execute step
--no-build    Skip the build step (use when no recorded file exists yet)
```

Step names for `--skip` / `--only`: `extract` `generate` `sanitize` `build` `execute` `report`

**Examples:**

```powershell
# Resume after recording
python testGen/pipeline.py --flow customer_create_ticket --skip extract generate sanitize

# Re-run tests only, no regeneration
python testGen/pipeline.py --flow customer_create_ticket --only execute report

# Debug one failing test
python testGen/pipeline.py --flow customer_create_ticket --only execute --id TC007

# Check scenario routing before running
python testGen/pipeline.py --flow customer_create_ticket --only execute --dry-run
```

---

## Output Files

| File | Description |
|---|---|
| `testGen/context_{flow}.json` | Extracted source code context (Stage 1 output) |
| `testGen/scenario_matrix_{flow}.json` | Generated scenario matrix (Stage 2 output) |
| `testGen/base_test_{flow}_recorded.py` | Raw Playwright codegen output — do not edit |
| `testGen/base_test_{flow}.py` | Parameterised base script — do not edit |
| `testGen/storage_states/session_{role}.json` | Browser session state — gitignored |
| `testGen/raw_results_{flow}.json` | Per-scenario test results (Stage 4 output) |
| `testGen/report_{flow}.xlsx` | Excel report (Stage 5 output) |

---

## Adding a New Flow

1. **Create a manifest** at `testGen/manifests/{flow_id}.json` listing the source files to read (see existing manifests for the format).

2. **Register the flow in `record.py`** — add an entry to `FLOW_CONFIG` with `role`, `start_path`, and `field_map`.

3. **Register the flow in `execute.py`** — add entries to `FLOW_BASE_SCRIPTS`, `FLOW_START_PATHS`, `FLOW_SUCCESS_URLS`, and `FLOW_SUCCESS_MESSAGES`.

4. **Run the full pipeline** for the new flow.

---

## Methodology

### Why this architecture

Traditional test generation approaches either require engineers to write tests by hand (slow, brittle) or record-and-replay scripts that break when values change (no coverage logic). This pipeline bridges both by using the application source code as the authoritative input for what to test, and keeping generated interaction scripts separate from assertion logic.

### Stage 1 — Context Extraction (`extract.py`)

The extractor reads a flow manifest (JSON file listing which source files are relevant) and assembles a single `context_{flow}.json` containing:

- Frontend form component source (JSX) — field definitions, conditional rendering, cascade logic
- Backend controller and model source — validation rules, DB constraints
- Test fixtures — known entities (customers, equipment, service types) that can be used as valid inputs

Large files are read in "slice" mode (a window around a named function), avoiding sending entire 2000-line controllers to the LLM.

### Stage 2 — Scenario Generation (`generate.py`)

The scenario matrix is generated in two LLM calls:

**Call 1 — Inventory:** The LLM reads the assembled source code and produces a structured inventory:
- **A items** — fields visible to the primary role, with type, required flag, constraints, and whether the field is auto-filled by a cascade
- **B items** — cascade chains (which field triggers which downstream effect)
- **C items** — role-visibility rules (which fields are hidden from which roles)
- **D items** — data-visibility rules (who can see created records)
- **E items** — entity sources for select fields (which API/fixture provides the options)

**Call 2 — Scenario expansion:** For each scenario category, a separate structured JSON call expands inventory items into concrete test scenarios with inputs and expected outcomes. Categories:

| Category | What it tests |
|---|---|
| `happy_path` | All required fields valid — should submit |
| `missing_field` | One required field omitted — form should reject |
| `boundary` | Field at max/min constraint edge — exact/exceed |
| `invalid_value` | Semantically invalid input (wrong file type, etc.) |
| `cascade_dependency` | Trigger field changes downstream field |
| `role_field_visibility` | Agent-only fields are absent in customer view |
| `ui_capability` | Each widget is functional |
| `multi_session` | Ticket created by role A is visible/hidden to role B |

**Sanitization (`apply_sanitize.py`):** After generation, a deterministic post-processor scans each scenario. For categories that submit the form (`boundary`, `invalid_value`, `ui_capability`, `happy_path`, `missing_field`), it injects any missing required fields using the first available value from the known entities in the context. This prevents scenarios from failing for the wrong reason (e.g. a boundary test on `subject` failing because `service_type` was also missing).

### Stage 3 — Base Script Builder (`record.py`)

Playwright Codegen records a human's happy-path interaction as a Python script with hardcoded values. The builder (`--build`) transforms that recording into a parameterised function:

```python
# Recording captures:
page.get_by_role("option", name="Breakdown Repair").click()

# Builder produces:
if inputs.get("service_type"):
    page.get_by_role("combobox", name="Remote Support").click()
    page.get_by_role("option", name=str(inputs["service_type"])).click()
```

Key design decisions:
- **No assertions in the base script.** `run_test()` is purely interaction code. Assertions are injected by `execute.py` at runtime so they survive re-recording without any manual edits.
- **File upload fix is baked in.** MUI dropzones record a `div` click as the target for `set_input_files`, which fails. The builder always rewrites this to `page.locator("input[type='file']")`.
- **Conditional blocks for optional fields.** Fields that may be absent from scenario inputs are wrapped in `if inputs.get(field):` so missing fields don't cause selector timeouts.

### Stage 4 — Execution (`execute.py`)

Each scenario is dispatched to one of four execution strategies based on its metadata:

**`run_ui_test`** (most scenarios):
Assembles a temporary pytest file by concatenating `base_test_{flow}.py` (the interaction function) with a `test_run` function that:
1. Sets `inputs`, `expected_outcome`, `expected_message`
2. Calls `run_test(page, inputs, expected_outcome, expected_message)`
3. Asserts the outcome:
   - **pass:** `page.wait_for_url(success_url)` — form must redirect
   - **fail:** form must NOT redirect (hard check), then soft-checks for the expected message text (try/except pass — message wording differences don't fail the test)

The soft assertion for fail cases prevents false failures when the app correctly rejects a form but uses slightly different wording than what the LLM generated.

**`run_cascade_test`** (cascade_dependency):
Opens the form, selects only the trigger field, and waits for the cascade API call to complete (using `page.expect_response` with the known endpoint). Asserts the response is not an error status.

**`run_visibility_test`** (role_field_visibility):
Opens the form and asserts `page.locator(field_selector).count() == 0` within the DOM.

**`run_multi_session_scenario`** (multi_session):
Session A (agent) calls the REST API directly to create a ticket. Session B (customer or second agent) opens the ticket detail page and asserts visibility or lack thereof.

Session state (browser cookies + localStorage JWT) is saved per role to `storage_states/session_{role}.json` on first use and reused across all scenarios in the run.

### Stage 5 — Reporting (`report.py`)

Reads `raw_results_{flow}.json` and `scenario_matrix_{flow}.json` and writes a three-sheet Excel workbook:

- **Scenario Matrix** — all scenarios, inputs, expected outcomes, business rules
- **Test Results** — actual outcome per scenario with failure details
- **Summary** — pass/fail counts by category and overall totals
