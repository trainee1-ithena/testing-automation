import re
from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context(storage_state="C:\\Users\\riyag\\OneDrive\\Documents\\ithena\\iSERV New\\iSERV New\\approach3\\storage_states\\session_agent.json")
    page = context.new_page()
    page.goto("http://localhost:3000/service_reports")
    page.get_by_role("button", name="Service Report", exact=True).click()
    page.get_by_role("combobox", name="Time & Material Service Report").click()
    page.get_by_role("option", name="Fixed Fee & Materials Service").click()
    page.get_by_role("checkbox", name="Internal Only When enabled,").check()
    page.get_by_role("textbox", name="Please enter name for the").click()
    page.get_by_role("textbox", name="Please enter name for the").fill("Test")
    page.get_by_role("button", name="Choose date, selected date is").click()
    page.get_by_role("gridcell", name="4", exact=True).click()
    page.get_by_role("combobox", name="Select Case").click()
    page.get_by_role("option", name="0626400279 - Server not responding").click()
    page.get_by_role("combobox", name="Select Assignee(s)").click()
    page.get_by_role("option", name="CS Carlos Silva Technical Support (Primary)").click()
    page.get_by_role("button", name="Save").click()
    # NOTE: reconstructed by hand in codegen format on 2026-07-05 — the original
    # recording was overwritten by an edited base script. Every selector above was
    # verified against the live app. Re-record with
    #   python approach3/record.py --record create_service_report create_service_report_from_service_reports
    # to replace this with a true recording.

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
