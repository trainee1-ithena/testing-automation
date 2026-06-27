import re
from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context(storage_state="C:\\Users\\riyag\\OneDrive\\Documents\\ithena\\iSERV New\\iSERV New\\testGen - Copy\\storage_states\\session_agent.json")
    page = context.new_page()
    page.goto("http://localhost:3000/service_reports")
    page.get_by_role("button", name="Service Report").click()
    page.get_by_role("combobox", name="Time & Material Service Report").click()
    page.get_by_role("option", name="Fixed Fee & Materials Service").click()
    page.get_by_role("checkbox", name="Internal Only When enabled,").check()
    page.get_by_role("textbox", name="Please enter name for the").click()
    page.get_by_role("textbox", name="Please enter name for the").fill("Test Name")
    page.get_by_role("button", name="Choose date, selected date is").click()
    page.get_by_role("gridcell", name="27").click()
    page.get_by_role("combobox", name="Select Organization").click()
    page.get_by_role("option", name="Acme Corp").click()
    page.get_by_role("combobox", name="Select Customer User").click()
    page.get_by_text("ASAlice Smithalice.smith@abc.").click()
    page.get_by_role("combobox", name="Select Case").click()
    page.get_by_role("option", name="0626400279 - Server not").click()
    page.get_by_role("button", name="Save").click()

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
