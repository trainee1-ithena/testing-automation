import re
from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context(storage_state="C:\\Users\\riyag\\OneDrive\\Documents\\ithena\\iSERV New\\iSERV New\\testGen\\storage_states\\session_customer.json")
    page = context.new_page()
    page.goto("http://localhost:3000/dashboard")
    page.get_by_role("button", name="New Case").click()
    page.get_by_role("combobox", name="Select Unit").click()
    page.get_by_role("option", name="Acme HQ - Main Server: Serial").click()
    page.get_by_role("combobox", name="Remote Support").click()
    page.get_by_role("option", name="Breakdown Repair").click()
    page.get_by_role("combobox", name="Normal").click()
    page.get_by_role("option", name="High").click()
    page.locator("[id=\"_r_20_\"]").click()
    page.locator("[id=\"_r_20_\"]").fill("Server is not working")
    page.locator(".jodit-wysiwyg").click()
    page.locator(".jodit-wysiwyg").fill("Im not sure")
    page.get_by_text("Drag and drop files here, or").click()
    page.locator("div").filter(has_text=re.compile(r"^Drag and drop files here, or click to select$")).nth(1).set_input_files("Create Ticket Workflow.docx")
    page.get_by_role("button", name="Submit").click()

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
