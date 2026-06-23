import re
from playwright.sync_api import Page, expect


def run_test(page: Page, inputs: dict, expected_outcome: str, expected_message: str):
    """
    Execute one agent_create_ticket scenario.

    inputs keys : customer, user, equipment, service_type, department, staff_accountable, staff_assigned, priority, subject, description, files
    expected_outcome : "pass" | "fail"
    expected_message : exact visible text expected on failure, or None

    Interaction lines below were auto-generated from a playwright codegen
    session (base_test_agent_create_ticket_recorded.py).  Edit selectors here if the form changes.
    Assertions are injected by execute.py — do not add them here.
    """
    page.goto("http://localhost:3000/cases")

    page.get_by_role("button", name="New Case").click()
    if inputs.get("customer"):
        page.get_by_role("combobox", name="Select Organization").click()
        page.get_by_role("option", name=str(inputs["customer"])).click()
    if inputs.get("user"):
        page.get_by_role("combobox", name="Select Customer User").click()
        page.get_by_role("option", name=str(inputs["user"])).click()
    if inputs.get("equipment"):
        page.get_by_role("combobox", name="Select Unit").click()
        page.get_by_role("option", name=str(inputs["equipment"])).click()
    if inputs.get("service_type"):
        page.get_by_role("combobox", name="Remote Support").click()
        page.get_by_role("option", name=str(inputs["service_type"])).click()
    if inputs.get("department"):
        page.get_by_role("combobox", name="Electrical").click()
        page.get_by_role("option", name=str(inputs["department"])).click()
    if inputs.get("staff_accountable"):
        page.get_by_role("combobox", name="Select Accountable").click()
        page.get_by_role("option", name=str(inputs["staff_accountable"])).click()
    if inputs.get("staff_assigned"):
        page.get_by_role("combobox", name="Select Assignee").click()
        page.get_by_role("option", name=str(inputs["staff_assigned"])).click()
    if inputs.get("priority"):
        page.get_by_role("combobox", name="Normal").click()
        page.get_by_role("option", name=str(inputs["priority"])).click()
    page.get_by_role("button", name="Choose date, selected date is").click()
    page.get_by_role("button", name="OK").click()
    page.locator("[id=\"_r_20_\"]").fill(str(inputs.get("subject", "")) if inputs.get("subject") is not None else "")
    page.locator(".jodit-wysiwyg").fill(str(inputs.get("description", "")) if inputs.get("description") is not None else "")
    if inputs.get("files"):
        page.locator("input[type='file']").set_input_files(str(inputs["files"]))
    page.get_by_role("button", name="Submit").click()
    # ---------------------
