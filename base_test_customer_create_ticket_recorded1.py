import re
from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context(storage_state="C:\\Users\\riyag\\OneDrive\\Documents\\ithena\\iSERV New\\iSERV New\\testGen\\storage_states\\session_customer.json")
    page = context.new_page()
    page.goto("http://localhost:3000/dashboard")
    page.close()

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
