import re
from playwright.sync_api import Page, expect

# Keys match the scenario matrix's inputs dict exactly.
FIELD_ORDER = ["report_type", "internal_only", "name", "reportDate", "ticket_id"]
FIELD_TYPES = {
    "report_type":   "react_select",
    "internal_only": "checkbox",
    "name":          "textbox",
    "reportDate":    "date",
    "ticket_id":     "react_select",
}


def open_form(page: Page):
    """Navigate to /service_reports and open the SR creation drawer."""
    page.goto("http://localhost:3000/service_reports")
    page.get_by_role("button", name="Service Report", exact=True).click()


def set_field(page: Page, field: str, value):
    if field == "report_type":
        page.get_by_role("combobox", name="Time & Material Service Report").click()
        page.wait_for_selector("[role='option']", timeout=10000)
        page.get_by_role("option", name=str(value), exact=False).click()
    elif field == "internal_only":
        if value:
            page.get_by_role("checkbox", name="Internal Only When enabled,").check()
        else:
            page.get_by_role("checkbox", name="Internal Only When enabled,").uncheck()
    elif field == "name":
        page.get_by_role("textbox", name="Please enter name for the").fill(str(value))
    elif field == "reportDate":
        page.get_by_role("button", name=re.compile(r"Choose date", re.I)).click()
        page.locator(".MuiPickersCalendarHeader-root + div").get_by_role("gridcell", name=str(value.day), exact=True).first.click()
        #page.get_by_role("gridcell", name=str(value)).click()
    elif field == "ticket_id":
        page.locator("div").filter(has_text=re.compile(r"^Select Case$")).click()
        page.wait_for_selector("[role='option']", timeout=10000)
        page.get_by_role("option", name=str(value), exact=False).click()
    else:
        raise ValueError(f"Unknown field: {field}")


def read_field(page: Page, field: str):
    if field == "report_type":
        return page.get_by_role("combobox", name="Time & Material Service Report").text_content() or ""
    elif field == "internal_only":
        return page.get_by_role("checkbox", name="Internal Only When enabled,").is_checked()
    elif field == "name":
        return page.get_by_role("textbox", name="Please enter name for the").input_value()
    elif field == "reportDate":
        return page.get_by_role("button", name=re.compile(r"hoose date", re.I)).text_content()
    elif field == "ticket_id":
        return page.locator("div").filter(has_text=re.compile(r"^Select Case$")).text_content() or ""
    else:
        raise ValueError(f"Unknown field: {field}")


def open_field(page: Page, field: str):
    if field == "report_type":
        page.get_by_role("combobox", name="Time & Material Service Report").click()
        page.wait_for_selector("[role='option']", timeout=10000)
    elif field == "internal_only":
        raise ValueError("Field 'internal_only' has no dropdown to open (type=checkbox)")
    elif field == "name":
        raise ValueError("Field 'name' has no dropdown to open (type=textbox)")
    elif field == "reportDate":
        page.get_by_role("button", name=re.compile(r"hoose date", re.I)).click()
    elif field == "ticket_id":
        page.locator("div").filter(has_text=re.compile(r"^Select Case$")).click()
        page.wait_for_selector("[role='option']", timeout=10000)
    else:
        raise ValueError(f"Unknown field: {field}")


def is_field_present(page: Page, field: str) -> bool:
    if field == "report_type":
        return page.get_by_role("combobox", name="Time & Material Service Report").count() > 0
    elif field == "internal_only":
        return page.get_by_role("checkbox", name="Internal Only When enabled,").count() > 0
    elif field == "name":
        return page.get_by_role("textbox", name="Please enter name for the").count() > 0
    elif field == "reportDate":
        return page.get_by_role("button", name=re.compile(r"hoose date", re.I)).count() > 0
    elif field == "ticket_id":
        return page.locator("div").filter(has_text=re.compile(r"^Select Case$")).count() > 0
    else:
        raise ValueError(f"Unknown field: {field}")


def run_test(page: Page, inputs: dict, expected_outcome: str, expected_message: str):
    """
    inputs keys : report_type, internal_only, name, reportDate, ticket_id
    expected_outcome : "pass" | "fail"
    """
    open_form(page)
    for field in FIELD_ORDER:
        value = inputs.get(field)
        if value is not None:
            set_field(page, field, value)

    try:
        page.get_by_role("button", name="Save").click(timeout=5000)
    except Exception:
        pass
