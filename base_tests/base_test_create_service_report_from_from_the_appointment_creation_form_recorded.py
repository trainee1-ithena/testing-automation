import re
from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context(storage_state="C:\\Users\\riyag\\OneDrive\\Documents\\ithena\\iSERV New\\iSERV New\\approach3\\storage_states\\session_service_manager.json")
    page = context.new_page()
    page.goto("http://localhost:3000/appointments")
    page.get_by_role("button", name="Appointment").click()
    page.get_by_role("textbox", name="Please specify the title for").click()
    page.get_by_role("textbox", name="Please specify the title for").fill("Test")
    page.locator(".jodit-wysiwyg").click()
    page.locator(".jodit-wysiwyg").fill("Test")
    page.get_by_role("combobox", name="Select Case").click()
    page.get_by_role("option", name="0626400010 - Server not").click()
    page.get_by_role("combobox", name="Select Service Report", exact=True).click()
    page.get_by_role("option", name="Create New Service Report").click()
    page.get_by_role("button", name="Close", description="Close").click()
    page.get_by_role("combobox", name="Time & Material").click()
    page.get_by_role("option", name="Fixed Fee & Material").click()
    page.get_by_role("combobox", name="External (Customer Visible)").click()
    page.get_by_role("option", name="Internal Only").click()
    page.get_by_role("combobox", name="Select Assignee").click()
    page.get_by_text("John Doe").click()
    page.get_by_role("button", name="Submit").click()

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
