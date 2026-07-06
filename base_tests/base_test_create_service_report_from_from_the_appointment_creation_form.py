import re
import time
from playwright.sync_api import Page, expect

FIELD_ORDER = ["title", "description", "case", "service_report", "job_type", "report_flow", "assignee"]
FIELD_TYPES = {
    "title": "textbox",
    "description": "textbox",
    "case": "react_select",
    "service_report": "react_select",
    "job_type": "react_select",
    "report_flow": "react_select",
    "assignee": "react_select"
}


def _dismiss_tour(page: Page, wait_ms: int = 1800):
    """
    iSERV's product-tour overlay (Shepherd.js — see TourContext.js) auto-starts
    on a route's first visit per logged-in user, but the start is ASYNC (up to
    ~1.5s after page load: skeleton-loader polling + a 300ms delay before
    tour.start()). Its useModalOverlay covers the page and can intercept the
    click on the very button this test is about to click — but only when the
    tour's start lands inside the test's click window, which is why the
    failure is intermittent rather than every run. Poll for the tour's cancel
    icon for a bit instead of checking once, and dismiss it if it shows up.
    Test sessions are fresh logins each run (see record.py/_setup_auth_storage
    and execute.py/_login), so every route's tour is still "not completed"
    every time — this can fire on every navigation, not just the first.
    """
    deadline = time.time() + (wait_ms / 1000)
    while time.time() < deadline:
        try:
            cancel_icon = page.locator(".shepherd-cancel-icon")
            if cancel_icon.count() > 0:
                cancel_icon.first.click(timeout=1000)
                page.wait_for_timeout(300)
                return
        except Exception:
            pass
        page.wait_for_timeout(150)


def _clear_stale_aria_hidden(page: Page):
    """
    MUI occasionally loses the modal mount/unmount race at page boot and leaves
    aria-hidden="true" on #root with no modal actually on screen. The page then
    LOOKS perfectly normal, but every role-based locator on it resolves to
    nothing (the ARIA tree excludes aria-hidden subtrees) — observed live as a
    visible, clickable button that get_by_role() could not find for 90s.
    Repair only when clearly stale: a real open drawer/menu is portaled to
    <body> outside #root, so if any such overlay is visible we leave the
    attribute alone. Visibility must be rect-based — MUI overlays are
    position:fixed, where offsetParent is ALWAYS null.
    """
    try:
        page.evaluate(
            "() => {"
            "  const root = document.getElementById('root');"
            "  if (!root || root.getAttribute('aria-hidden') !== 'true') return;"
            "  const vis = e => { const r = e.getBoundingClientRect();"
            "    return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; };"
            "  const overlayVisible = [...document.querySelectorAll('.MuiModal-root, .MuiDrawer-root, .MuiPopover-root')]"
            "    .some(m => !root.contains(m) && vis(m));"
            "  if (!overlayVisible) root.removeAttribute('aria-hidden');"
            "}"
        )
    except Exception:
        pass


def _debug_aria_state(page: Page, label: str, locator=None):
    """One-line page-state dump printed when a locator has been failing for a
    while — shows whether the target exists in DOM vs the accessibility tree,
    and what modal/aria state is active. Read it from execute.py's output."""
    try:
        state = page.evaluate(
            "() => {"
            "  const root = document.getElementById('root');"
            "  const vis = e => { const r = e.getBoundingClientRect();"
            "    return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; };"
            "  return {"
            "    root_ah: root ? root.getAttribute('aria-hidden') : null,"
            "    drawers: [...document.querySelectorAll('.MuiDrawer-root')].map(d =>"
            "      (vis(d) ? 'vis' : 'hid') + '/ah=' + d.getAttribute('aria-hidden')),"
            "    menus_open: [...document.querySelectorAll(\"[role='listbox'],.MuiMenu-root\")].filter(vis).length,"
            "    cb_dom: document.querySelectorAll(\"[role='combobox']\").length,"
            "    cb_in_ah: [...document.querySelectorAll(\"[role='combobox']\")]"
            "      .filter(c => c.closest(\"[aria-hidden='true']\")).length,"
            "  };"
            "}"
        )
        role_count = None
        if locator is not None:
            try:
                role_count = locator.count()
            except Exception:
                role_count = "?"
        print(f"    [debug {label}] locator_count={role_count} state={state}")
    except Exception as exc:
        print(f"    [debug {label}] state dump failed: {str(exc)[:80]}")


def _click_menu_item(page: Page, label: str, timeout_ms: int = 30000):
    """
    Click a label+live-count menu/tab item (e.g. a ticket's "Service Reports65").
    The recording captures label+count concatenated, which goes stale when the
    count changes; matching the bare label instead hits the app's left sidebar
    nav item with the same name (observed live: the click navigated to the main
    module page, silently testing the wrong entry point). So: match
    ^label<digits?>$, prefer a digit-suffixed candidate (only the in-page menu
    item carries a count), and fall back to the LAST bare match (the sidebar
    renders before page content in DOM order).
    """
    pattern = re.compile(r"^" + re.escape(label) + r"\d*$")
    deadline = time.time() + (timeout_ms / 1000)
    last_exc = None
    while time.time() < deadline:
        try:
            candidates = page.get_by_text(pattern)
            best = None
            for i in range(candidates.count()):
                txt = (candidates.nth(i).text_content() or "").strip()
                if txt != label:   # has a count suffix -> the in-page menu item
                    best = candidates.nth(i)
                    break
            if best is None and candidates.count():
                best = candidates.last
            if best is None:
                raise RuntimeError("menu item '" + label + "' not found on page")
            best.click(timeout=2000)
            return
        except Exception as exc:
            last_exc = exc
            _dismiss_tour(page, wait_ms=400)
            _clear_stale_aria_hidden(page)
            page.wait_for_timeout(300)
    raise last_exc


def _force_click(locator):
    """
    Last-resort DOM-level click for a locator that RESOLVES but whose normal
    click keeps failing actionability — observed live as a visible, enabled,
    uncovered button failing 'stability' for 90s on a page whose content
    live-updates over the websocket. Dispatches mousedown/mouseup/click so
    MUI controls that react on mousedown (Select, Autocomplete) respond too.
    Bypasses Playwright's covered/stable checks — only call after normal
    clicking has already failed for a long time.
    """
    locator.first.evaluate(
        "el => ['mousedown', 'mouseup', 'click'].forEach(t =>"
        " el.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true})))"
    )


def _click_through_tour(page: Page, locator, timeout_ms: int = 90000):
    # Default sized for this dev stack's worst observed page-data latency
    # (30-45s+ under load), not for a responsive app — navigation clicks in
    # open_form() wait on whole-page data fetches, unlike field actions.
    """
    Click `locator`, dismissing the Shepherd tour if it appears mid-wait.
    TourContext.js's auto-start chain (an async getCompletedTours() call, a
    skeleton-loader poll, a waitForElement poll, then a 300ms setTimeout) can
    start the tour well past any fixed pre-wait, and the tour's first step
    has no `attachTo` target — so its modal overlay blocks the ENTIRE page,
    not just its own target, until dismissed. A single dismiss-then-click can
    still lose this race; retry both across the click's own timeout instead
    of gambling on one fixed wait length. Each retry also repairs the stale
    aria-hidden state that blinds role-based locators (see
    _clear_stale_aria_hidden).

    Success detection: a click attempt can time out AFTER its click actually
    dispatched (observed live: the drawer opened, then every retry failed with
    "MuiDrawer subtree intercepts pointer events" — the retries were blocked
    by the very drawer the first click opened). So if a drawer/modal overlay
    appears that was not on screen before the first attempt, treat the click
    as landed instead of retrying against a covered button.
    """
    def _overlay_open() -> bool:
        try:
            return bool(page.evaluate(
                "() => {"
                "  const root = document.getElementById('root');"
                "  const vis = e => { const r = e.getBoundingClientRect();"
                "    return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; };"
                "  return [...document.querySelectorAll('.MuiDrawer-root, .MuiModal-root')]"
                "    .some(m => (!root || !root.contains(m)) && vis(m));"
                "}"
            ))
        except Exception:
            return False

    had_overlay = _overlay_open()
    deadline = time.time() + (timeout_ms / 1000)
    start = time.time()
    last_exc = None
    debugged = False
    forced = False
    while time.time() < deadline:
        try:
            locator.click(timeout=10000)
            return
        except Exception as exc:
            last_exc = exc
            if not had_overlay and _overlay_open():
                return
            if not debugged and time.time() - start > 15:
                _debug_aria_state(page, "nav-click failing", locator)
                debugged = True
            if not forced and time.time() - start > (timeout_ms / 2000):
                try:
                    _force_click(locator)
                    page.wait_for_timeout(700)
                    if not had_overlay and _overlay_open():
                        return
                except Exception:
                    pass
                forced = True
            _dismiss_tour(page, wait_ms=500)
            _clear_stale_aria_hidden(page)
    raise last_exc


def _wait_form_ready(page: Page, timeout_ms: int = 90000):
    """
    The form's fields render only after its options fetch resolves — measured
    anywhere from 2s to 40s+ on the dev stack, run to run, for the same page.
    Gate on the first recorded field actually existing before any set_field
    call, instead of sizing every per-action timeout for the worst case.
    """
    if not FIELD_ORDER:
        return
    deadline = time.time() + (timeout_ms / 1000)
    while True:
        try:
            if is_field_present(page, FIELD_ORDER[0]):
                return
        except Exception:
            pass
        if time.time() > deadline:
            raise AssertionError(
                "form did not render field '" + FIELD_ORDER[0] + "' within "
                + str(timeout_ms) + "ms"
            )
        _clear_stale_aria_hidden(page)
        page.wait_for_timeout(500)


def _open_dropdown(page: Page, opener, timeout_ms: int = 60000):
    """
    Open a dropdown (without selecting) with the same hardening _select_option uses —
    dismiss the tour, clear a stale aria-hidden, retry the click until the option list is
    actually visible. Used by open_field()/is-interactable checks, which a plain
    click+wait made time out on the same MOUSEDOWN/refetch race set_field survives.
    """
    options = page.locator("[role='option']")
    deadline = time.time() + (timeout_ms / 1000)
    while True:
        try:
            if not options.first.is_visible():
                opener.click(timeout=5000)
                options.first.wait_for(state="visible", timeout=5000)
            return
        except Exception:
            if options.first.is_visible():
                return
            if time.time() > deadline:
                raise
            _dismiss_tour(page, wait_ms=500)
            _clear_stale_aria_hidden(page)


def _close_open_menu(page: Page):
    """
    Close an open dropdown menu WITHOUT stranding the form. A bare Escape can aria-hide the
    very drawer/dialog we're working inside (observed live on a host-form drawer: after Escape
    the whole MuiDrawer got aria-hidden, so every later field became unreachable). After the
    Escape, strip aria-hidden back off any VISIBLE drawer/dialog that still holds interactable
    controls — never a real background overlay, which has no such controls on screen.
    """
    page.keyboard.press("Escape")
    try:
        page.evaluate(
            "() => {"
            "  const vis = e => { const r = e.getBoundingClientRect();"
            "    return r.width > 0 && r.height > 0; };"
            "  document.querySelectorAll('.MuiDrawer-root, .MuiDialog-root').forEach(d => {"
            "    if (d.getAttribute('aria-hidden') === 'true' && vis(d)"
            "        && d.querySelector('input, textarea, button'))"
            "      d.removeAttribute('aria-hidden');"
            "  });"
            "}"
        )
    except Exception:
        pass


def _select_option(page: Page, opener, value, timeout_ms: int = 60000):
    """
    Open a dropdown and pick an option, robust to two races observed live:
      1. the dropdown ignoring a click that landed mid-render — re-open and
         retry until the option list is actually visible;
      2. the option click getting swallowed — the drawer refetches its options
         while the menu is open, React re-creates the option nodes, and a click
         dispatched to the stale node reaches no handler. The menu then stays
         open; since MUI renders it as a modal that marks the page behind it
         aria-hidden, every later role-based locator resolves to nothing and
         its backdrop intercepts every later click.
    Distinguish "click swallowed" (option not aria-selected — click it again)
    from "selected but menu stays open by design" (multi-select — close it via
    _close_open_menu, which repairs the drawer Escape can wrongly aria-hide).
    """
    options = page.locator("[role='option']")
    deadline = time.time() + (timeout_ms / 1000)
    start = time.time()
    debugged = False
    while True:
        try:
            if not options.first.is_visible():
                opener.click(timeout=5000)
                options.first.wait_for(state="visible", timeout=5000)
            break
        except Exception:
            # The click can dispatch and still raise: MUI opens on MOUSEDOWN,
            # the option fetch re-renders the control, the node detaches
            # mid-click, and Playwright's internal retry then can't re-resolve
            # the opener (the now-open menu aria-hides the drawer behind it).
            # If the option list is on screen, the click did its job — anything
            # else the click call reported is a false negative. Without this
            # check the loop re-clicks an unresolvable opener until deadline.
            if options.first.is_visible():
                break
            if time.time() > deadline:
                raise
            if not debugged and time.time() - start > 15:
                _debug_aria_state(page, "dropdown-open failing", opener)
                debugged = True
                try:
                    _force_click(opener)   # see _force_click — stability flake fallback
                    options.first.wait_for(state="visible", timeout=5000)
                    break
                except Exception:
                    pass
    option = page.get_by_role("option", name=str(value), exact=False).first
    clicked = False
    typed = False
    while True:
        if not options.first.is_visible():
            if clicked:
                return
            # menu vanished before we picked anything — reopen and retry
        try:
            if not options.first.is_visible():
                opener.click(timeout=5000)
                options.first.wait_for(state="visible", timeout=5000)
            if not clicked and not typed and option.count() == 0:
                # The list is open but the target option isn't rendered —
                # role-scoped pickers (e.g. an engineer's Case list) can order/
                # filter differently, and long lists may need the user to type
                # before the wanted row exists. Autocomplete openers are real
                # <input>s, so type the value's leading token to filter.
                typed = True
                try:
                    if (opener.first.evaluate("el => el.tagName") or "") == "INPUT":
                        opener.first.fill(str(value).split(" ")[0][:20])
                        option.wait_for(state="visible", timeout=5000)
                except Exception:
                    pass
            if clicked and option.get_attribute("aria-selected", timeout=1000) == "true":
                _close_open_menu(page)
            else:
                option.click(timeout=5000)
                clicked = True
            options.first.wait_for(state="hidden", timeout=3000)
            return
        except Exception:
            if time.time() > deadline:
                raise
        page.wait_for_timeout(200)


def open_form(page: Page):
    """Navigate to the flow's start URL and open the form. Recorded from
    base_test_create_service_report_from_from_the_appointment_creation_form_recorded.py — edit selectors here if the
    navigation path changes."""
    page.goto("http://localhost:3000/appointments")
    page.wait_for_load_state("domcontentloaded")
    _dismiss_tour(page)
    _click_through_tour(page, page.get_by_role("button", name="Appointment"))
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    _click_through_tour(page, page.get_by_role("textbox", name="Please specify the title for"))
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass


def set_field(page: Page, field: str, value):
    """Generic field setter — dispatches on FIELD_TYPES."""
    if field == "title":
        page.get_by_role("textbox", name="Please specify the title for").fill(str(value))
    elif field == "description":
        page.locator(".jodit-wysiwyg").fill(str(value))
    elif field == "case":
        _select_option(page, page.get_by_role("combobox", name="Select Case"), value)
    elif field == "service_report":
        _select_option(page, page.get_by_role("combobox", name="Select Service Report", exact=True), value)
    elif field == "job_type":
        _select_option(page, page.get_by_role("combobox", name="Time & Material"), value)
    elif field == "report_flow":
        _select_option(page, page.get_by_role("combobox", name="External (Customer Visible)"), value)
    elif field == "assignee":
        _open_dropdown(page, page.get_by_role("combobox", name="Select Assignee"))
        page.get_by_role("listbox").get_by_text(str(value), exact=False).first.click()
    else:
        raise ValueError(f"Unknown field: {field}")


def read_field(page: Page, field: str):
    """Generic field getter — reads the field's current displayed value."""
    if field == "title":
        return page.get_by_role("textbox", name="Please specify the title for").input_value()
    elif field == "description":
        return page.locator(".jodit-wysiwyg").input_value()
    elif field == "case":
        return (page.get_by_role("combobox", name="Select Case")).text_content() or ""
    elif field == "service_report":
        return (page.get_by_role("combobox", name="Select Service Report", exact=True)).text_content() or ""
    elif field == "job_type":
        return (page.get_by_role("combobox", name="Time & Material")).text_content() or ""
    elif field == "report_flow":
        return (page.get_by_role("combobox", name="External (Customer Visible)")).text_content() or ""
    elif field == "assignee":
        return (page.get_by_role("combobox", name="Select Assignee")).text_content() or ""
    else:
        raise ValueError(f"Unknown field: {field}")


def open_field(page: Page, field: str):
    """Open a dropdown without selecting a value (for option-list inspection)."""
    if field == "title":
        raise ValueError("Field 'title' has no dropdown to open (type=textbox)")
    elif field == "description":
        raise ValueError("Field 'description' has no dropdown to open (type=textbox)")
    elif field == "case":
        _open_dropdown(page, page.get_by_role("combobox", name="Select Case"))
    elif field == "service_report":
        _open_dropdown(page, page.get_by_role("combobox", name="Select Service Report", exact=True))
    elif field == "job_type":
        _open_dropdown(page, page.get_by_role("combobox", name="Time & Material"))
    elif field == "report_flow":
        _open_dropdown(page, page.get_by_role("combobox", name="External (Customer Visible)"))
    elif field == "assignee":
        _open_dropdown(page, page.get_by_role("combobox", name="Select Assignee"))
    else:
        raise ValueError(f"Unknown field: {field}")


def is_field_present(page: Page, field: str) -> bool:
    """DOM existence check — used for role-based field visibility tests."""
    if field == "title":
        return page.get_by_role("textbox", name="Please specify the title for").count() > 0
    elif field == "description":
        return page.locator(".jodit-wysiwyg").count() > 0
    elif field == "case":
        return (page.get_by_role("combobox", name="Select Case")).count() > 0
    elif field == "service_report":
        return (page.get_by_role("combobox", name="Select Service Report", exact=True)).count() > 0
    elif field == "job_type":
        return (page.get_by_role("combobox", name="Time & Material")).count() > 0
    elif field == "report_flow":
        return (page.get_by_role("combobox", name="External (Customer Visible)")).count() > 0
    elif field == "assignee":
        return (page.get_by_role("combobox", name="Select Assignee")).count() > 0
    else:
        raise ValueError(f"Unknown field: {field}")


def run_test(page: Page, inputs: dict, expected_outcome: str, expected_message: str):
    """
    Execute one create_service_report/create_service_report_from_from_the_appointment_creation_form scenario.

    inputs keys : title, description, case, service_report, job_type, report_flow, assignee
    expected_outcome : "pass" | "fail"
    expected_message : exact visible text expected on failure, or None

    Fields are set in FIELD_ORDER (the order they were recorded) so cascade
    prerequisites (e.g. customer before equipment) are respected. Assertions
    are injected by execute.py — do not add them here.
    """
    open_form(page)
    _wait_form_ready(page)
    for field in FIELD_ORDER:
        value = inputs.get(field)
        if value is not None:
            set_field(page, field, value)

    _dismiss_tour(page)
    try:
        _click_through_tour(page, page.get_by_role("button", name="Submit"), timeout_ms=5000)
    except Exception:
        pass  # submit may be disabled (e.g. invalid input rejected by the form)
