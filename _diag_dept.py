from playwright.sync_api import sync_playwright
import pathlib

state = str(pathlib.Path("testGen/storage_states/agent.json").resolve())

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(storage_state=state)
    page = ctx.new_page()
    page.goto("http://localhost:3000/cases")
    page.get_by_role("button", name="New Case").click(timeout=15000)
    page.wait_for_timeout(2000)

    try:
        page.wait_for_function(
            'document.querySelector([name="department"])?.value?.length > 0',
            timeout=8000
        )
        dept_prop = page.eval_on_selector('input[name="department"]', 'el => el.value') or ""
        print(f"dept value prop={dept_prop!r}")
    except Exception as e:
        print(f"wait_for_function FAILED: {e}")

    try:
        page.locator('input[name="department"]').locator('xpath=..').get_by_role('combobox').click(timeout=5000)
        print("combobox clicked OK")
    except Exception as e:
        print(f"combobox click FAILED: {e}")
        browser.close()
        exit()

    page.wait_for_timeout(800)

    opts = page.locator("[role='option']").all()
    print(f"Total [role='option'] count: {len(opts)}")
    for i, opt in enumerate(opts[:20]):
        tc = (opt.text_content() or "").strip()
        dv = opt.get_attribute("data-value")
        av = opt.get_attribute("aria-selected") or ""
        print(f"  [{i}] text={tc!r}  data-value={dv!r}  aria-selected={av!r}")

    browser.close()
