import re
from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context(storage_state="C:\\Users\\riyag\\OneDrive\\Documents\\ithena\\iSERV New\\iSERV New\\approach3\\storage_states\\session_service_manager.json")
    page = context.new_page()
    page.goto("http://localhost:3000/cases")
    page.get_by_text("skddjafklsjklfjslajsaf").click()
    page.get_by_text("Service Reports73").click()
    page.get_by_role("button", name="Service Report", exact=True).click()
    page.get_by_role("combobox", name="Time & Material Service Report").click()
    page.get_by_role("option", name="Fixed Fee & Materials Service").click()
    page.get_by_role("checkbox", name="Internal Only When enabled,").check()
    page.get_by_role("textbox", name="Please enter name for the").click()
    page.get_by_role("textbox", name="Please enter name for the").press("ControlOrMeta+a")
    page.get_by_role("textbox", name="Please enter name for the").fill("New Test")
    page.get_by_role("button", name="Choose date, selected date is").click()
    page.get_by_role("gridcell", name="7", exact=True).click()
    page.locator(".MuiInputBase-root.MuiOutlinedInput-root.MuiInputBase-colorPrimary.MuiInputBase-fullWidth.MuiInputBase-formControl.MuiInputBase-sizeSmall.MuiInputBase-adornedStart").click()
    page.get_by_role("option", name="JD John Doe Access:").click()
    page.get_by_role("button", name="Save").click()

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
