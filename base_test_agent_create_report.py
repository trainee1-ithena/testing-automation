import re
from playwright.sync_api import Page, expect


def run_test(page: Page, inputs: dict, expected_outcome: str, expected_message: str):
    """
    Execute one agent_create_report scenario.

    inputs keys : report_type, internal_only, name, reportDate, org_id, user_id, ticket_id, assignee, appointment_id, description
    expected_outcome : "pass" | "fail"
    expected_message : exact visible text expected on failure, or None

    Interaction lines below were auto-generated from a playwright codegen
    session (base_test_agent_create_report_recorded.py).  Edit selectors here if the form changes.
    Assertions are injected by execute.py — do not add them here.
    """
    page.goto("http://localhost:3000/service_reports")

    page.get_by_role("button", name="Service Report", exact=True).click()
    if inputs.get("report_type"):
        page.get_by_role("combobox", name="Time & Material Service Report").click()
        page.get_by_role("option", name=str(inputs["report_type"]), exact=False).click()
    if inputs.get("internal_only"):
        page.get_by_role("checkbox", name="Internal Only When enabled,").check()
    page.get_by_role("textbox", name="Please enter name for the").click()
    page.get_by_role("textbox", name="Please enter name for the").fill(str(inputs.get("name", "")) if inputs.get("name") is not None else "")
    if inputs.get("reportDate"):
        page.get_by_role("button", name=re.compile(r"hoose date", re.I)).click()
        page.get_by_role("gridcell", name="27").click()
    if inputs.get("org_id"):
        page.get_by_role("combobox", name="Select Organization").click()
        page.get_by_role("option", name=str(inputs["org_id"]), exact=False).click()
    if inputs.get("user_id"):
        page.get_by_role("combobox", name="Select Customer User").click()
        page.get_by_role("listbox").get_by_text(str(inputs["user_id"]), exact=False).first.click()
    if inputs.get("ticket_id"):
        page.get_by_role("combobox", name="Select Case").click()
        page.get_by_role("option", name=str(inputs["ticket_id"]), exact=False).click()
    page.get_by_role("button", name="Save", exact=True).click()
    # ---------------------
