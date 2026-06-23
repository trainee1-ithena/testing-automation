import re
from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context(storage_state="C:\\Users\\riyag\\OneDrive\\Documents\\ithena\\iSERV New\\iSERV New\\testGen\\storage_states\\session_agent.json")
    page = context.new_page()
    page.goto("http://localhost:3000/cases")
    page.get_by_role("button", name="New Case").click()
    page.get_by_role("combobox", name="Select Organization").click()
    page.get_by_role("option", name="Beta LLC").click()
    page.get_by_role("combobox", name="Select Customer User").click()
    page.get_by_role("option", name="BJ Bob Johnson shwetaa@").click()
    page.get_by_role("combobox", name="Select Unit").click()
    page.get_by_role("option", name="Beta - Router 1: Serial # BETA-").click()
    page.get_by_role("combobox", name="Remote Support").click()
    page.get_by_role("option", name="Breakdown Repair").click()
    page.get_by_role("combobox", name="Electrical").click()
    page.get_by_role("option", name="Electrical").click()
    page.get_by_role("combobox", name="Select Accountable").click()
    page.get_by_role("option", name="JD John Doe").click()
    page.get_by_role("combobox", name="Select Assignee").click()
    page.get_by_role("option", name="LH Liam Hughes").click()
    page.get_by_role("combobox", name="Normal").click()
    page.get_by_role("option", name="Low").click()
    page.get_by_role("button", name="Choose date, selected date is").click()
    page.get_by_role("button", name="OK").click()
    page.locator("[id=\"_r_20_\"]").fill("jkljfklajfklsjkldsjklsd")
    page.locator(".jodit-wysiwyg").fill("fsklajklsjkfldsjklajfklsjlf")
    page.locator("div").filter(has_text=re.compile(r"^Drag and drop files here, or click to select$")).nth(1).set_input_files("Create Ticket Workflow.docx")
    page.goto("http://localhost:3000/cases/35")

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
