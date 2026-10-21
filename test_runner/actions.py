"""
Every action a test step can use.

Each action is a function (ctx, params) -> message registered with @action.
It returns a short human-readable message on success and raises ActionError
on failure. To support a new kind of step, add one function here.

Common optional keys on every action:
    "description": "text"   shown in the report
    "optional": true        a failure is recorded as WARN and the step continues
    "screenshot": true      take a screenshot right after this action
    "timeout_ms": 30000     override the default timeout
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from . import config, debug
from .locators import describe_target, find_element, has_target
from .excel import ExcelReport
from .report_viewer import (
    close_report_tabs,
    download_excel as export_to_excel,
    no_data_message,
    reload_filter_page,
    run_report_with_retry,
    wait_for_filter_page_ready,
)
from .reports import find_report
from .waits import wait_for_page_settled
from .widgets import (
    discover_filters,
    primeng_host,
    primeng_options,
    select_in_primeng_multiselect,
)


class ActionError(Exception):
    pass


@dataclass
class ActionSpec:
    fn: Callable
    required: tuple[str, ...]
    needs_target: bool
    doc: str


ACTIONS: dict[str, ActionSpec] = {}


def action(name: str, required: tuple[str, ...] = (), needs_target: bool = False):
    def register(fn):
        ACTIONS[name] = ActionSpec(fn, required, needs_target, (fn.__doc__ or "").strip())
        return fn
    return register


# =============================================================================
# HELPERS
# =============================================================================

def _timeout(params: dict) -> int:
    return int(params.get("timeout_ms", config.DEFAULT_TIMEOUT_MS))


def _require_element(ctx, params: dict):
    element = find_element(ctx.page, params, _timeout(params))
    if element is None:
        raise ActionError(
            f"Could not find visible element {describe_target(params)} "
            f"within {_timeout(params) // 1000}s."
            + (" Is the panel it's in open? (e.g. 'click Report Columns')"
               if "option" in params else "")
        )
    return element


def _normalize(text: str, ignore_case: bool) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text.lower() if ignore_case else text


def _visible_page_text(page, within: str | None) -> str:
    """Rendered text of the page (all frames), optionally limited to a container."""
    chunks = []
    for frame in page.frames:
        try:
            if within:
                chunks.extend(frame.locator(within).all_inner_texts())
            else:
                chunks.append(frame.locator("body").inner_text(timeout=5000))
        except Exception:
            continue
    return "\n".join(chunks)


def _wait_for_new_page(ctx, pages_before, wait_ms: int):
    elapsed = 0
    while elapsed < wait_ms:
        for page in ctx.session.context.pages:
            if page not in pages_before and not page.is_closed():
                return page
        ctx.page.wait_for_timeout(250)
        elapsed += 250
    return None


_REPORT_COLUMNS_EXPANDED_JS = """
(el) => {
    const visible = node => {
        const style = getComputedStyle(node);
        const rect = node.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0
            && style.visibility !== 'hidden' && style.display !== 'none';
    };
    for (let node = el; node && node !== document.body; node = node.parentElement) {
        const expanded = node.getAttribute('aria-expanded');
        if (expanded === 'true') return true;
        if (expanded === 'false') return false;
        const controls = [...node.querySelectorAll('input[type="checkbox"], [role="checkbox"]')];
        if (controls.length) return controls.some(visible);
    }
    return null;
}
"""


def _report_columns_expanded(element) -> bool | None:
    try:
        return element.evaluate(_REPORT_COLUMNS_EXPANDED_JS)
    except Exception:
        return None


def _should_click_report_columns(ctx, page, element) -> bool:
    expanded = _report_columns_expanded(element)
    if expanded is not None:
        return not expanded
    return not any(open_page is page
                   for open_page in getattr(ctx, "report_columns_expanded_pages", []))


def _forget_report_columns_state(ctx, page) -> None:
    expanded_pages = getattr(ctx, "report_columns_expanded_pages", None)
    if expanded_pages is not None:
        ctx.report_columns_expanded_pages = [open_page for open_page in expanded_pages if open_page is not page]


_COLUMN_VALUES_JS = """
(column) => {
    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
    const wanted = norm(column).toLowerCase();
    const values = [];
    let found = false;

    for (const table of document.querySelectorAll('table')) {
        const rows = Array.from(table.rows);
        let headerRow = -1, colIndex = -1;

        for (let r = 0; r < rows.length && headerRow < 0; r++) {
            let pos = 0;
            for (const cell of rows[r].cells) {
                if (norm(cell.innerText).toLowerCase() === wanted
                        || norm(cell.innerText).toLowerCase().includes(wanted)) {
                    headerRow = r; colIndex = pos; break;
                }
                pos += cell.colSpan || 1;
            }
        }
        if (colIndex < 0) continue;
        found = true;

        for (let r = headerRow + 1; r < rows.length; r++) {
            let pos = 0;
            for (const cell of rows[r].cells) {
                if (pos === colIndex) { values.push(norm(cell.innerText)); break; }
                pos += cell.colSpan || 1;
            }
        }
    }
    return {found, values};
}
"""


def _column_values(page, column: str):
    found, values = False, []
    for frame in page.frames:
        try:
            result = frame.evaluate(_COLUMN_VALUES_JS, column)
        except Exception:
            continue
        found = found or result["found"]
        values.extend(result["values"])
    return found, values


# =============================================================================
# NAVIGATION
# =============================================================================

@action("goto")
def goto(ctx, params):
    """
    Open a URL. "url" is optional - defaults to the test case app_url.
    Opening a URL also makes its site the test's application, so a later "open" or
    "open report" goes to that site's home page.
    """
    url = params.get("url") or ctx.test_case.app_url
    _forget_report_columns_state(ctx, ctx.page)
    if params.get("url"):
        parts = urlsplit(url)
        if parts.scheme and parts.netloc:
            ctx.test_case.app_url = f"{parts.scheme}://{parts.netloc}/"

    for attempt in range(1, config.NAVIGATION_ATTEMPTS + 1):
        try:
            ctx.page.goto(url, wait_until="domcontentloaded",
                          timeout=params.get("timeout_ms", config.NAVIGATION_TIMEOUT_MS))
            break
        except PlaywrightTimeoutError:
            break  # The site is slow to fire DOMContentLoaded; later waits confirm it loaded.
        except Exception as e:
            # net::ERR_NAME_NOT_RESOLVED, ERR_CONNECTION_RESET ... - usually VPN/network.
            error = str(e).splitlines()[0]
            if "net::" not in error:
                raise
            if attempt == config.NAVIGATION_ATTEMPTS:
                raise ActionError(
                    f"Could not reach {url} after {attempt} attempts ({error.split('net::')[-1].split()[0]}). "
                    "Check VPN / network connection."
                )
            print(f"        Network error ({error.split('net::')[-1].split()[0]}), "
                  f"retrying in {config.RETRY_WAIT_S}s...", flush=True)
            ctx.page.wait_for_timeout(config.RETRY_WAIT_S * 1000)

    wait_for_page_settled(ctx.page)
    return f"Opened {url}"


@action("switch_tab")
def switch_tab(ctx, params):
    """Switch to another tab by "index", "url_contains" or "title_contains"."""
    pages = [p for p in ctx.session.context.pages if not p.is_closed()]

    if "index" in params:
        index = int(params["index"])
        if index >= len(pages):
            raise ActionError(f"No tab at index {index} ({len(pages)} open).")
        target = pages[index]
    else:
        target = None
        for page in pages:
            if "url_contains" in params and params["url_contains"] in page.url:
                target = page
            elif "title_contains" in params and params["title_contains"] in page.title():
                target = page
        if target is None:
            raise ActionError("No open tab matched the given url/title.")

    ctx.session.switch_to(target)
    wait_for_page_settled(target)
    return f"Switched to tab {target.url}"


@action("close_tab")
def close_tab(ctx, params):
    """Close the current tab and return to the most recent remaining one."""
    url = ctx.page.url
    ctx.page.close()
    ctx.session.switch_to(ctx.session.page)
    return f"Closed tab {url}"


# =============================================================================
# INTERACTION
# =============================================================================

@action("click", needs_target=True)
def click(ctx, params):
    """
    Click an element. If the click opens a new tab, the runner switches to it.
    "opens_new_tab": true (wait for it) / false (don't check) / omitted (auto-detect).
    """
    element = _require_element(ctx, params)
    is_report_columns = str(params.get("text", "")).strip().casefold() == "report columns"
    if is_report_columns and not _should_click_report_columns(ctx, ctx.page, element):
        return "Report Columns panel is already open"

    context = ctx.session.context
    opens_new_tab = params.get("opens_new_tab", "auto")
    pages_before = list(context.pages)
    new_page = None

    try:
        if opens_new_tab is True:
            with context.expect_page(timeout=config.NAVIGATION_TIMEOUT_MS) as page_info:
                element.click(timeout=_timeout(params))
            new_page = page_info.value
        else:
            element.click(timeout=_timeout(params))
            if opens_new_tab == "auto":
                new_page = _wait_for_new_page(ctx, pages_before, config.NEW_TAB_DETECT_MS)
    except PlaywrightTimeoutError:
        try:
            disabled = not element.is_enabled()
        except Exception:
            disabled = False
        if disabled:
            raise ActionError(
                f"{describe_target(params)} stayed disabled for {_timeout(params) // 1000}s - the "
                "page is not ready for it (e.g. a required field or file is missing)."
            ) from None
        raise ActionError(f"Could not click {describe_target(params)} within "
                          f"{_timeout(params) // 1000}s (it may be covered by another element).") from None

    message = f"Clicked {describe_target(params)}"

    if is_report_columns and new_page is None:
        expanded_pages = getattr(ctx, "report_columns_expanded_pages", None)
        if expanded_pages is not None and not any(open_page is ctx.page for open_page in expanded_pages):
            expanded_pages.append(ctx.page)

    # Clicking a known report's name tells run_report which data markers to wait for.
    if find_report(params.get("text")):
        ctx.report_name = params["text"]

    if new_page is not None:
        ctx.session.switch_to(new_page)
        message += f" - opened new tab {new_page.url}"

    wait_for_page_settled(ctx.page)
    return message


@action("hover", needs_target=True)
def hover(ctx, params):
    """Move the mouse over an element (e.g. to open a menu)."""
    _require_element(ctx, params).hover(timeout=_timeout(params))
    ctx.page.wait_for_timeout(500)
    return f"Hovered {describe_target(params)}"


def _open_menu_until_visible(ctx, menu_text: str, item_text: str, timeout_ms: int):
    """
    Open the menu `menu_text` until `item_text` is visible, and return that item.
    The site redraws its nav bar while it loads (e.g. when the user's workspace link
    appears), which can swallow a click - so hover, then click, and retry.
    """
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        menu = find_element(ctx.page, {"text": menu_text}, 5000)
        if menu is not None:
            for opener in ("hover", "click"):
                try:
                    getattr(menu, opener)(timeout=5000)
                except Exception as e:
                    debug.swallowed(f"menu {opener} '{menu_text}'", e)
                    continue
                item = find_element(ctx.page, {"text": item_text}, 2500)
                if item is not None:
                    return item
        wait_for_page_settled(ctx.page, 3000)
    return None


@action("menu", required=("path",))
def menu(ctx, params):
    """
    Click through a menu: "path" is a list like ["On Demand Reports", "ZGM1 Report"].
    Each level is opened (hover, then click, with retries) until the next item shows.
    """
    path = [str(p) for p in params["path"]]
    if len(path) < 2:
        raise ActionError("A menu path needs at least two items, e.g. Menu > Item.")

    for menu_text, item_text in zip(path, path[1:]):
        if _open_menu_until_visible(ctx, menu_text, item_text, _timeout(params)) is None:
            raise ActionError(f"Opened menu '{menu_text}' but '{item_text}' never appeared.")

    click(ctx, {"text": path[-1], "timeout_ms": 10000})
    return f"Menu {' > '.join(path)}"


@action("fill", required=("value",), needs_target=True)
def fill(ctx, params):
    """Type "value" into an input. Add "press_enter": true to submit."""
    element = _require_element(ctx, params)
    element.fill(str(params["value"]), timeout=_timeout(params))
    if params.get("press_enter"):
        element.press("Enter")
        wait_for_page_settled(ctx.page)
    return f"Entered '{params['value']}' into {describe_target(params)}"


@action("press", required=("key",))
def press(ctx, params):
    """Press a keyboard key ("Enter", "Escape", "Tab"...), optionally on a target."""
    if has_target(params):
        _require_element(ctx, params).press(params["key"])
    else:
        ctx.page.keyboard.press(params["key"])
    wait_for_page_settled(ctx.page)
    return f"Pressed {params['key']}"


def _apply_value(page, element, value: str, timeout_ms: int) -> str:
    """Put `value` into any kind of filter control. Returns a message, raises ActionError."""
    tag = element.evaluate("(el) => el.tagName.toLowerCase()")

    host = primeng_host(element) if tag != "select" else None
    if host is not None:
        ok, message = select_in_primeng_multiselect(page, host, value)
        if not ok:
            raise ActionError(message)
        return message

    if tag == "select":
        # Option text is often "AM22 - Americas": prefer exact, then "value - ...", then contains.
        # Dependent dropdowns (Country after Geography) fill their options a moment later,
        # so keep looking until the option appears.
        wanted = value.strip().lower()
        deadline = time.time() + timeout_ms / 1000
        while True:
            options = element.evaluate("(el) => Array.from(el.options).map(o => o.text.trim())")
            ranked = sorted(
                (0 if text.lower() == wanted
                 else 1 if text.lower().startswith(wanted + " ")
                 else 2 if wanted in text.lower()
                 else 3, index, text)
                for index, text in enumerate(options)
            )
            if ranked and ranked[0][0] < 3:
                break
            if time.time() >= deadline:
                shown = [t for t in options if t][:8]
                raise ActionError(
                    f"Option '{value}' is not in the dropdown after {timeout_ms // 1000}s "
                    f"({len(options)} options" + (f", e.g. {shown}" if shown else "") + ")."
                )
            page.wait_for_timeout(500)
        element.select_option(index=ranked[0][1], timeout=timeout_ms)
        return f"Selected '{ranked[0][2]}'"

    if tag in ("input", "textarea"):
        element.fill(value, timeout=timeout_ms)
        element.press("Enter")
        return f"Entered '{value}'"

    element.click(timeout=timeout_ms)
    option = find_element(page, {"text": value}, timeout_ms)
    if option is None:
        raise ActionError(f"Dropdown opened but option '{value}' was not found.")
    option.click()
    return f"Selected '{value}'"


@action("select", required=("value",), needs_target=True)
def select(ctx, params):
    """Choose "value" in a dropdown - PrimeNG multiselect, native <select>, or click-to-open list."""
    element = _require_element(ctx, params)
    message = _apply_value(ctx.page, element, str(params["value"]), _timeout(params))
    wait_for_page_settled(ctx.page)
    if ctx.filter_page is not ctx.page and ctx.page.locator(config.RUN_REPORT_SELECTOR).count() > 0:
        # Reached the filter page by "menu"/"click" instead of "open report".
        _remember_filter_page(ctx, ctx.page)
        ctx.base_selections = []
    if ctx.filter_page is ctx.page:
        target = {k: v for k, v in params.items() if k in ("css", "xpath", "role", "name",
                                                          "label", "placeholder", "text", "field")}
        ctx.base_selections.append((target, str(params["value"])))
    return f"{message} in {describe_target(params)}"


@action("show_options", needs_target=True)
def show_options(ctx, params):
    """List the choices in a dropdown (nothing is selected). Handy for writing 'select' lines."""
    element = _require_element(ctx, params)
    tag = element.evaluate("(el) => el.tagName.toLowerCase()")
    host = primeng_host(element) if tag != "select" else None

    if host is not None:
        options = primeng_options(ctx.page, host)
    elif tag == "select":
        options = [o.strip() for o in element.evaluate(
            "(el) => Array.from(el.options).map(o => o.text)") if o.strip()]
    else:
        raise ActionError(f"{describe_target(params)} is a text box, not a dropdown.")

    print(f"        Options: {options}", flush=True)
    if not options:
        return f"{describe_target(params)} has no options"
    return f"{len(options)} options in {describe_target(params)}: " + "; ".join(options)


@action("check", needs_target=True)
def check(ctx, params):
    """Tick a checkbox or radio button."""
    box = _require_element(ctx, params)
    box.check(timeout=_timeout(params))
    return f"Checked {describe_target(params)}"


@action("uncheck", needs_target=True)
def uncheck(ctx, params):
    """Untick a checkbox."""
    _require_element(ctx, params).uncheck(timeout=_timeout(params))
    return f"Unchecked {describe_target(params)}"


# =============================================================================
# WAITING
# =============================================================================

@action("wait", required=("seconds",))
def wait(ctx, params):
    """Pause for a fixed number of "seconds"."""
    ctx.page.wait_for_timeout(float(params["seconds"]) * 1000)
    return f"Waited {params['seconds']}s"


@action("wait_for", needs_target=True)
def wait_for(ctx, params):
    """Wait until an element is visible."""
    _require_element(ctx, params)
    return f"{describe_target(params)} is visible"


# =============================================================================
# VERIFICATION
# =============================================================================

@action("verify_visible", needs_target=True)
def verify_visible(ctx, params):
    """Pass if the element is visible."""
    _require_element(ctx, params)
    return f"Verified visible: {describe_target(params)}"


@action("verify_not_visible", needs_target=True)
def verify_not_visible(ctx, params):
    """Pass if the element is NOT visible (checked after the page settles)."""
    wait_for_page_settled(ctx.page)
    if find_element(ctx.page, params, timeout_ms=2000) is not None:
        raise ActionError(f"Element should not be visible but is: {describe_target(params)}")
    return f"Verified not visible: {describe_target(params)}"


@action("verify_text", required=("text",))
def verify_text(ctx, params):
    """Pass if "text" appears on the page (or inside "within"). "ignore_case": true optional."""
    ignore_case = params.get("ignore_case", False)
    wanted = _normalize(params["text"], ignore_case)
    within = params.get("within")
    deadline_ms = _timeout(params)
    waited = 0

    while True:
        if wanted in _normalize(_visible_page_text(ctx.page, within), ignore_case):
            return f"Found text '{params['text']}'" + (f" in '{within}'" if within else "")
        if waited >= deadline_ms:
            raise ActionError(f"Text '{params['text']}' was not found on the page.")
        ctx.page.wait_for_timeout(500)
        waited += 500


@action("verify_not_text", required=("text",))
def verify_not_text(ctx, params):
    """Pass if "text" does NOT appear on the page (or inside "within").

    Matches whole words only, so "Geo" is not tripped by "Geography";
    pass "partial": true to match inside longer words too.
    """
    ignore_case = params.get("ignore_case", False)
    wait_for_page_settled(ctx.page)
    page_text = _normalize(_visible_page_text(ctx.page, params.get("within")), ignore_case)
    wanted = re.escape(_normalize(params["text"], ignore_case))
    pattern = wanted if params.get("partial") else rf"(?<!\w){wanted}(?!\w)"
    match = re.search(pattern, page_text)
    if match:
        start, end = max(0, match.start() - 40), match.end() + 40
        raise ActionError(f"Text '{params['text']}' should not be on the page but was found: "
                          f"'...{page_text[start:end]}...'")
    return f"Confirmed text '{params['text']}' is not present"


@action("verify_column_contains", required=("column", "text"))
def verify_column_contains(ctx, params):
    """Pass if any cell under table header "column" contains "text"."""
    ignore_case = params.get("ignore_case", False)
    wanted = _normalize(params["text"], ignore_case)
    deadline_ms = _timeout(params)
    waited = 0

    while True:
        found, values = _column_values(ctx.page, params["column"])
        matches = [v for v in values if wanted in _normalize(v, ignore_case)]
        if matches:
            return (f"Column '{params['column']}' contains '{params['text']}' "
                    f"({len(matches)} matching row(s), e.g. '{matches[0][:120]}')")
        if waited >= deadline_ms:
            if not found:
                raise ActionError(f"No table column with header '{params['column']}' was found.")
            raise ActionError(
                f"Column '{params['column']}' ({len(values)} rows) does not contain '{params['text']}'."
            )
        ctx.page.wait_for_timeout(500)
        waited += 500


@action("verify_column_not_contains", required=("column", "text"))
def verify_column_not_contains(ctx, params):
    """Pass if no cell under table header "column" contains "text"."""
    ignore_case = params.get("ignore_case", False)
    wait_for_page_settled(ctx.page)
    found, values = _column_values(ctx.page, params["column"])
    if not found:
        raise ActionError(f"No table column with header '{params['column']}' was found.")
    wanted = _normalize(params["text"], ignore_case)
    matches = [v for v in values if wanted in _normalize(v, ignore_case)]
    if matches:
        raise ActionError(
            f"Column '{params['column']}' should not contain '{params['text']}' "
            f"but {len(matches)} row(s) do, e.g. '{matches[0][:120]}'."
        )
    return f"Column '{params['column']}' ({len(values)} rows) does not contain '{params['text']}'"


_COLUMN_HEADER_JS = """
(column) => {
    const squash = s => (s || '').toLowerCase().replace(/[\\s._#-]+/g, '');
    const wanted = squash(column);
    for (const cell of document.querySelectorAll('table th, table td')) {
        const text = cell.innerText || '';
        if (text.length < 100 && squash(text) === wanted) return text.replace(/\\s+/g, ' ').trim();
    }
    return null;
}
"""


def _column_header(page, column: str) -> str | None:
    """The heading text of a table column named `column` (spaces/case ignored), or None."""
    for frame in page.frames:
        try:
            found = frame.evaluate(_COLUMN_HEADER_JS, column)
        except Exception:
            continue
        if found:
            return found
    return None


@action("verify_column_header", required=("column",))
def verify_column_header(ctx, params):
    """
    Pass if a table on the page has a column headed "column" (spaces and case ignored, so
    LineOfBusinessName matches "Line Of Business Name"). With "absent": pass if none has.
    """
    column = params["column"]
    if params.get("absent"):
        wait_for_page_settled(ctx.page)
        found = _column_header(ctx.page, column)
        if found:
            raise ActionError(f"Column '{found}' is shown but should not be.")
        return f"No column '{column}' on the page, as expected"

    deadline_ms, waited = _timeout(params), 0
    while True:
        found = _column_header(ctx.page, column)
        if found:
            return f"Column '{found}' is shown"
        if waited >= deadline_ms:
            raise ActionError(f"No table column headed '{column}' on the page "
                              "(spaces and case are ignored).")
        ctx.page.wait_for_timeout(500)
        waited += 500


@action("verify_url_contains", required=("text",))
def verify_url_contains(ctx, params):
    """Pass if the current tab's URL contains "text"."""
    if params["text"] not in ctx.page.url:
        raise ActionError(f"URL '{ctx.page.url}' does not contain '{params['text']}'.")
    return f"URL contains '{params['text']}'"


# =============================================================================
# EVIDENCE
# =============================================================================

def resolve_upload_file(name: str) -> Path | None:
    """A file for "upload": a full path, or a name in the test_data folder (or the project folder)."""
    path = Path(str(name).strip().strip('"'))
    candidates = [path] if path.is_absolute() else [
        config.TEST_DATA_DIR / path, Path.cwd() / path, config.BASE_DIR / path,
    ]
    return next((p for p in candidates if p.is_file()), None)


# Upload boxes are usually hidden behind a styled "Browse..." button, so the box itself
# is never visible. It is usable when none of its parents is hidden - boxes inside a hidden
# tab (e.g. the Manual Submission grid's Image uploads) are skipped.
_FILE_INPUT_INFO_JS = """
(el, ext) => {
    let usable = true;
    for (let p = el.parentElement; p; p = p.parentElement) {
        const s = getComputedStyle(p);
        if (p.hidden || s.display === 'none' || s.visibility === 'hidden') { usable = false; break; }
    }
    const accept = (el.accept || '').toLowerCase();
    const fits = !accept || accept.split(',').some(a => {
        a = a.trim();
        return a === ext || a === '*/*' || (ext.startsWith('.xls') && a.includes('spreadsheet'))
            || (ext === '.csv' && a.includes('csv'));
    });
    return {usable, fits, accepts: accept || 'any file',
            name: el.id ? '#' + el.id : (el.name ? "'" + el.name + "'" : '(unnamed)')};
}
"""


def _pick_file_input(page, path: Path):
    """The upload box to use: in a visible part of the page and accepting this file type.
    Returns (locator, description) or (None, reason)."""
    ext = path.suffix.lower()
    shown, hidden_fits, shown_other = [], 0, set()
    for frame in page.frames:
        try:
            inputs = frame.locator("input[type=file]")
            for i in range(inputs.count()):
                info = inputs.nth(i).evaluate(_FILE_INPUT_INFO_JS, ext)
                if info["usable"] and info["fits"]:
                    shown.append((inputs.nth(i), info["name"]))
                elif info["fits"]:
                    hidden_fits += 1
                elif info["usable"]:
                    shown_other.add(info["accepts"])
        except Exception:
            continue
    if shown:
        return shown[0]
    if hidden_fits:
        return None, (f"The upload box for {ext} files is in a hidden part of the page. Open its "
                      "tab first (e.g. 'click Excel/CSV Submission').")
    if shown_other:
        return None, (f"No upload box on the page accepts {ext} files (the visible ones accept: "
                      f"{', '.join(sorted(shown_other))}). Open the right tab first.")
    return None, ("No file upload box on this page. Open the upload tab first "
                  "(e.g. 'click Excel/CSV Submission'), or name the button: upload Browse = <file>")


@action("upload", required=("file",))
def upload(ctx, params):
    """
    Choose "file" in a file-upload box (like Browse... and picking the file).
    The file is looked up in the test_data folder unless a full path is given. With a target
    (e.g. "text": "Browse"), that button / box is used; otherwise the page's upload box.
    """
    path = resolve_upload_file(params["file"])
    if path is None:
        raise ActionError(f"File '{params['file']}' not found - put it in {config.TEST_DATA_DIR} "
                          "or give the full path.")

    if has_target(params):
        element = _require_element(ctx, params)
        # The upload box that belongs to this button: itself, inside it (a styled
        # <label>Browse...<input type=file hidden></label>), or in its nearest group.
        own_input = element.locator(
            "xpath=(self::input[@type='file'] | .//input[@type='file']"
            " | ancestor::*[.//input[@type='file']][1]//input[@type='file'])[1]"
        )
        if own_input.count() > 0:
            own_input.first.set_input_files(str(path), timeout=_timeout(params))
        else:
            try:
                with ctx.page.expect_file_chooser(timeout=10000) as chooser:
                    element.click(timeout=_timeout(params))
                chooser.value.set_files(str(path))
            except PlaywrightTimeoutError:
                raise ActionError(f"Clicking {describe_target(params)} did not open a file chooser.")
    else:
        box, where = _pick_file_input(ctx.page, path)
        if box is None:
            raise ActionError(where)
        box.set_input_files(str(path), timeout=_timeout(params))
        wait_for_page_settled(ctx.page)
        return f"Chose file {path.name} in upload box {where} ({path})"

    wait_for_page_settled(ctx.page)
    return f"Chose file {path.name} for upload ({path})"


@action("screenshot")
def screenshot(ctx, params):
    """Take a screenshot. "name" is optional and used in the file name."""
    path = ctx.take_screenshot(params.get("name", "screenshot"))
    if path is None:
        raise ActionError("Screenshot could not be saved.")
    return f"Screenshot saved: {path.name}"


# =============================================================================
# REPORTS (moved from validate.py)
# =============================================================================

def _remember_filter_page(ctx, page):
    ctx.filter_page = page
    ctx.filter_url = page.url


def _require_excel(ctx) -> ExcelReport:
    if ctx.excel is None:
        raise ActionError("No Excel downloaded yet - add 'download excel' first.")
    if ctx.excel.no_data_message:
        raise ActionError(
            f"The report returned no data ('{ctx.excel.no_data_message}'), so the Excel has no "
            "columns to check. Select filter values before 'run report' "
            "(use 'show options <filter>' to see the choices)."
        )
    return ctx.excel


def _markers(ctx):
    report = find_report(ctx.report_name)
    return report["markers"] if report else None


@action("open_report", required=("name",))
def open_report(ctx, params):
    """
    Home page -> menu -> report, and wait for its filter page.
    "name" is either "Menu > Report" or just "Report" - then every menu in
    config.REPORT_MENUS is opened in turn until the report shows up.
    """
    goto(ctx, {})
    path = [part.strip() for part in params["name"].split(">")]
    report_name = path[-1]

    if len(path) > 1:
        menu(ctx, {"path": path})
        found_in = path[0]
    else:
        found_in = None
        for menu_text in config.REPORT_MENUS:
            print(f"        Looking for '{report_name}' in '{menu_text}'...", flush=True)
            if _open_menu_until_visible(ctx, menu_text, report_name, 6000) is not None:
                found_in = menu_text
                break
            ctx.page.keyboard.press("Escape")
        if found_in is None:
            raise ActionError(
                f"Report '{report_name}' was not found in any menu "
                f"({', '.join(config.REPORT_MENUS)}). Check the exact name on screen."
            )
        click(ctx, {"text": report_name, "timeout_ms": 10000})

    ctx.report_name = report_name
    ctx.base_selections = []
    if not wait_for_filter_page_ready(ctx.page):
        raise ActionError(f"'{report_name}' opened but its filter page did not finish loading "
                          "(no Run Report button).")
    _remember_filter_page(ctx, ctx.page)
    return f"Opened report '{report_name}' from '{found_in}'"


@action("run_report")
def run_report(ctx, params):
    """Click Run Report (up to 3 attempts), wait for the data, and switch to the report tab."""
    ctx.no_data_message = None
    ctx.test_case.no_data_message = ""
    page = ctx.page
    if page.locator(config.RUN_REPORT_SELECTOR).count() == 0:
        if ctx.filter_page is not None and not ctx.filter_page.is_closed():
            page = ctx.filter_page
        else:
            # Not a standard report filter page: a plain click on a "Run Report" button.
            return click(ctx, {"text": "Run Report", "timeout_ms": _timeout(params)})

    _remember_filter_page(ctx, page)
    report_page, message = run_report_with_retry(ctx.session.context, page, _markers(ctx))
    if report_page is None:
        raise ActionError(message)

    ctx.session.switch_to(report_page)
    message = f"{message} - switched to the report tab"
    no_data = no_data_message(report_page)
    if no_data:
        ctx.no_data_message = no_data
        ctx.test_case.no_data_message = no_data
        message += f". NOTE: the report returned no data ('{no_data}')"
    return message


@action("download_excel")
def download_excel(ctx, params):
    """Export the open report to Excel (kept for "verify excel column" and filter tests)."""
    name = re.sub(r"[^A-Za-z0-9]+", "_", ctx.report_name or "report").strip("_")
    try:
        path = export_to_excel(ctx.page, ctx.run_dir / "downloads", name)
        ctx.excel = ExcelReport(path)
    except Exception as e:
        raise ActionError(f"Excel download failed: {e}")

    ctx.test_case.downloads.append(path)
    if ctx.excel.no_data_message:
        return f"Downloaded {path.name} - the report is empty ('{ctx.excel.no_data_message}')"
    return f"Downloaded {path.name}"


@action("verify_excel_column", required=("column",))
def verify_excel_column(ctx, params):
    """
    Pass if the downloaded Excel has "column" and at least one row has data in it.
    With "value": pass only if every filled row in the column equals that value.
    """
    if params.get("absent"):
        frame, actual = _require_excel(ctx).column(params["column"])
        if frame is not None:
            raise ActionError(f"Excel column '{actual}' is present but should not be.")
        return f"Excel has no '{params['column']}' column, as expected"

    if "value" in params:
        return _verify_excel_column_value(ctx, params["column"], str(params["value"]))

    stats = _require_excel(ctx).column_stats(params["column"])
    if stats is None:
        raise ActionError(f"Column '{params['column']}' is not in the Excel file.")
    if stats["with_data"] == 0:
        raise ActionError(
            f"Column '{stats['column']}' is present but empty ({stats['total_rows']} rows)."
        )
    return (f"Excel column '{stats['column']}' present: {stats['with_data']} of "
            f"{stats['total_rows']} rows have data, {stats['blank']} blank")


def _verify_excel_column_value(ctx, column: str, value: str) -> str:
    result = _require_excel(ctx).rows_not_matching(column, value)
    if result is None:
        raise ActionError(f"Column '{column}' is not in the Excel file.")
    if result["blank"] == result["total_rows"]:
        raise ActionError(
            f"Excel column '{result['column']}' is empty in all {result['total_rows']} rows, "
            f"so '{value}' cannot be checked there - use 'verify {value}' on the report page instead."
        )
    if result["matching"] == 0:
        raise ActionError(
            f"No rows in column '{result['column']}' have '{value}' "
            f"({result['total_rows']} rows, e.g. {result['other_examples']})."
        )
    if result["other_count"]:
        raise ActionError(
            f"Column '{result['column']}': {result['matching']} rows are '{value}' but "
            f"{result['other_count']} rows have other values, e.g. {result['other_examples']}."
        )
    blank_note = f", {result['blank']} blank" if result["blank"] else ""
    return (f"All {result['matching']} filled rows in Excel column '{result['column']}' "
            f"are '{value}'{blank_note}")


def _run_filter_test(ctx, label: str, column: str, timeout_ms: int) -> str:
    excel = _require_excel(ctx)
    filter_page = ctx.filter_page
    if filter_page is None or filter_page.is_closed():
        raise ActionError("No report filter page yet - use 'open report' or 'run report' first.")

    frame, actual_column = excel.column(column)
    if frame is None:
        raise ActionError(f"Excel column '{column}' not found.")
    value = excel.random_value(column)
    if value is None:
        raise ActionError(f"Excel column '{actual_column}' has no values.")

    print(f"        Filter '{label}' <- random value '{value}' from column '{actual_column}'",
          flush=True)

    # Start from a clean filter page every time.
    close_report_tabs(ctx.session.context, filter_page)
    ctx.session.switch_to(filter_page)
    _forget_report_columns_state(ctx, filter_page)
    if not reload_filter_page(filter_page, ctx.filter_url):
        raise ActionError("Could not reload a clean filter page.")

    # Re-apply the step's own "select" choices (other than the filter under test).
    for target, base_value in ctx.base_selections:
        if target.get("field", "").strip().lower() == label.strip().lower():
            continue
        base_control = find_element(filter_page, target, timeout_ms)
        if base_control is None:
            raise ActionError(f"Could not re-apply {describe_target(target)} = '{base_value}'.")
        _apply_value(filter_page, base_control, base_value, timeout_ms)
        wait_for_page_settled(filter_page)

    control = find_element(filter_page, {"field": label}, timeout_ms)
    if control is None:
        raise ActionError(f"Filter '{label}' not found on the filter page.")
    applied = _apply_value(filter_page, control, value, timeout_ms)
    wait_for_page_settled(filter_page)
    ctx.take_screenshot(f"filter_{label}_selected")

    report_page, message = run_report_with_retry(ctx.session.context, filter_page, _markers(ctx))
    if report_page is None:
        raise ActionError(f"'{label}' = '{value}' applied, but: {message}")

    ctx.session.switch_to(report_page)
    no_data = no_data_message(report_page)
    if no_data:
        ctx.take_screenshot(f"filter_{label}_result")
        raise ActionError(f"'{label}' = '{value}' returned no data ('{no_data}'), "
                          "although the value came from the unfiltered report.")
    shown = value.lower() in _visible_page_text(report_page, None).lower()
    ctx.take_screenshot(f"filter_{label}_result")

    report_page.close()
    ctx.session.switch_to(filter_page)
    _forget_report_columns_state(ctx, filter_page)
    reload_filter_page(filter_page, ctx.filter_url)

    return (f"'{label}' = '{value}': {applied}, report loaded"
            + ("" if shown else " (value not found in the report text - check screenshot)"))


@action("filter_test", required=("filter",))
def filter_test(ctx, params):
    """
    Random-value filter evidence test: take a random value of "column" (default: same
    name as "filter") from the downloaded Excel, apply it to "filter" on a clean filter
    page, run the report, and screenshot the selection and the result.
    """
    return _run_filter_test(ctx, params["filter"], params.get("column") or params["filter"],
                            _timeout(params))


@action("test_all_filters")
def test_all_filters(ctx, params):
    """
    Run filter_test for every filter on the report's filter page. Filters are found on the
    page automatically and matched to Excel columns by name; entries in reports.py take
    priority. Filters with no matching Excel column are SKIPPED (no value to pick).
    """
    excel = _require_excel(ctx)
    filter_page = ctx.filter_page
    if filter_page is None or filter_page.is_closed():
        raise ActionError("No report filter page yet - use 'open report' or 'run report' first.")

    # Discover filters on a clean filter page.
    close_report_tabs(ctx.session.context, filter_page)
    ctx.session.switch_to(filter_page)
    _forget_report_columns_state(ctx, filter_page)
    reload_filter_page(filter_page, ctx.filter_url)
    found_on_page = discover_filters(filter_page)
    print(f"        Filters on page: {', '.join(found_on_page) or '(none found)'}", flush=True)

    configured = {
        item["label"]: item["excel_column"]
        for item in ((find_report(ctx.report_name) or {}).get("filter_tests") or [])
    }
    plan, lines = [], []
    for label in list(dict.fromkeys(found_on_page + list(configured))):
        column = configured.get(label) or excel.match_column(label)
        if column:
            plan.append((label, column))
        else:
            lines.append(f"SKIPPED '{label}': no matching Excel column")
            print(f"        {lines[-1]}", flush=True)

    if not plan:
        raise ActionError("No filters could be matched to Excel columns.\n" + "\n".join(lines))

    failed = 0
    for label, column in plan:
        try:
            lines.append("PASS " + _run_filter_test(ctx, label, column, _timeout(params)))
        except Exception as e:
            failed += 1
            lines.append(f"FAIL '{label}': {e}")
        print(f"        {lines[-1]}", flush=True)

    skipped = len(lines) - len(plan)
    summary = (f"{len(plan) - failed}/{len(plan)} filters passed"
               + (f", {skipped} skipped" if skipped else "") + "\n" + "\n".join(lines))
    if failed:
        raise ActionError(summary)
    return summary


# =============================================================================
# DATABASE (read-only, see db.py)
# =============================================================================

def _db_query(ctx, sql: str):
    from . import db
    try:
        return db.query(sql, ctx.database)
    except db.QueryNotAllowed as e:
        raise ActionError(f"Query not allowed: {e}")
    except Exception as e:
        error = str(e).splitlines()[0] if str(e) else type(e).__name__
        raise ActionError(f"Database error ({ctx.database or config.DB_DEFAULT_DATABASE}): {error}")


@action("use_database", required=("database",))
def use_database(ctx, params):
    """Run the following db commands against "database" (default: config.DB_DEFAULT_DATABASE)."""
    ctx.database = params["database"]
    return f"Using database '{ctx.database}'"


@action("show_db", required=("sql",))
def show_db(ctx, params):
    """Run a SELECT and log the first rows (checks nothing). Handy for writing 'verify db' lines."""
    from .db import format_rows
    columns, rows = _db_query(ctx, params["sql"])
    table = format_rows(columns, rows)
    print("        " + table.replace("\n", "\n        "), flush=True)
    return f"{len(rows)} row(s):\n{table}"


@action("verify_db", required=("sql",))
def verify_db(ctx, params):
    """
    Pass if the SELECT returns at least one row. With "value": pass if the first column of
    the first row equals it (numbers compared as numbers). With "absent": pass if no rows.
    """
    from .db import same_value, to_text
    columns, rows = _db_query(ctx, params["sql"])

    if params.get("absent"):
        if rows:
            raise ActionError(f"Query should return no rows but returned {len(rows)}, "
                              f"e.g. {[to_text(v) for v in rows[0]]}.")
        return "Query returned no rows, as expected"

    if not rows:
        raise ActionError("Query returned no rows.")
    if "value" not in params:
        return f"Query returned {len(rows)} row(s)"

    actual = to_text(rows[0][0])
    if not same_value(actual, str(params["value"])):
        raise ActionError(f"Query returned '{actual}' ({columns[0] or 'first column'}), "
                          f"expected '{params['value']}'.")
    return f"Query returned '{actual}' as expected"


@action("verify_excel_column_in_db", required=("column", "sql"))
def verify_excel_column_in_db(ctx, params):
    """
    Pass if every distinct filled value of Excel "column" is in the first column of the
    SELECT (e.g. SELECT DISTINCT ProfitcenterCode FROM dbo.ZGM1Prices).
    """
    from .db import to_text
    from .excel import normalize_value

    frame, actual = _require_excel(ctx).column(params["column"])
    if frame is None:
        raise ActionError(f"Column '{params['column']}' is not in the Excel file.")
    excel_values = [v for v in dict.fromkeys(frame[actual].map(normalize_value)) if v]
    if not excel_values:
        raise ActionError(f"Excel column '{actual}' has no values to check.")

    _, rows = _db_query(ctx, params["sql"])
    db_values = {to_text(row[0]).lower() for row in rows}
    missing = [v for v in excel_values if v.lower() not in db_values]
    if missing:
        raise ActionError(
            f"{len(missing)} of {len(excel_values)} distinct values in Excel column '{actual}' "
            f"are not in the database result ({len(db_values)} values), e.g. {missing[:5]}."
        )
    return (f"All {len(excel_values)} distinct values in Excel column '{actual}' "
            f"are in the database result ({len(db_values)} values)")


@action("check_file", required=("file",))
def check_file(ctx, params):
    """
    Load a local Excel file (e.g. the one just uploaded) as "the Excel" for the following
    excel checks - e.g. to compare an uploaded file with the database. Found like "upload".
    """
    path = resolve_upload_file(params["file"])
    if path is None:
        raise ActionError(f"File '{params['file']}' not found - put it in {config.TEST_DATA_DIR} "
                          "or give the full path.")
    try:
        ctx.excel = ExcelReport(path)
    except Exception as e:
        raise ActionError(f"Could not read {path.name} as Excel: {e}")
    return f"Checking {path.name}: {ctx.excel.row_count()} data rows, columns {ctx.excel.all_columns()}"


def _loose_name(name) -> str:
    return re.sub(r"[\s._#-]+", "", str(name)).lower()


@action("verify_excel_rows_in_db", required=("sql", "keys"))
def verify_excel_rows_in_db(ctx, params):
    """
    Pass if every row of the Excel is in the SELECT's result: rows are matched on the "keys"
    columns, then every other column in both (same name, ignoring case/spaces) must be equal.
    Name the query's columns like the Excel's, e.g. SELECT ult AS [ULT Number], price AS [Purchase Price] ...
    """
    from .db import same_value, to_text
    from .excel import normalize_value

    table = _require_excel(ctx).table()
    if table.empty:
        raise ActionError("The Excel has no data rows to check.")
    columns, rows = _db_query(ctx, params["sql"])

    excel_cols = {_loose_name(c): c for c in table.columns}
    db_cols = {_loose_name(c): i for i, c in enumerate(columns)}
    keys = [k.strip() for k in params["keys"] if k.strip()]
    for key in keys:
        if _loose_name(key) not in excel_cols:
            raise ActionError(f"Key column '{key}' is not in the Excel (columns: {list(table.columns)}).")
        if _loose_name(key) not in db_cols:
            raise ActionError(f"Key column '{key}' is not in the query result (columns: {columns}). "
                              f"Name it in the SELECT, e.g. ... AS [{key}].")
    key_ids = [_loose_name(k) for k in keys]
    compared = [c for c in excel_cols if c in db_cols and c not in key_ids]
    not_compared = [excel_cols[c] for c in excel_cols if c not in db_cols]

    # Database rows grouped by their key values.
    by_key: dict[tuple, list[tuple]] = {}
    for row in rows:
        key = tuple(to_text(row[db_cols[k]]).lower() for k in key_ids)
        by_key.setdefault(key, []).append(row)

    missing, different, matched = [], [], 0
    for _, excel_row in table.iterrows():
        key = tuple(normalize_value(excel_row[excel_cols[k]]).lower() for k in key_ids)
        label = ", ".join(f"{excel_cols[k]}={normalize_value(excel_row[excel_cols[k]])}" for k in key_ids)
        candidates = by_key.get(key)
        if not candidates:
            missing.append(label)
            continue
        best_diffs = None
        for db_row in candidates:   # several database rows with the key: any full match passes
            diffs = [
                f"{excel_cols[c]} file '{normalize_value(excel_row[excel_cols[c]])}' "
                f"db '{to_text(db_row[db_cols[c]])}'"
                for c in compared
                if not same_value(normalize_value(excel_row[excel_cols[c]]), to_text(db_row[db_cols[c]]))
            ]
            if best_diffs is None or len(diffs) < len(best_diffs):
                best_diffs = diffs
        if best_diffs:
            different.append(f"{label}: " + "; ".join(best_diffs))
        else:
            matched += 1

    checked = f"compared on {', '.join(excel_cols[c] for c in compared) or 'keys only'}"
    note = f"; not in the query, so not compared: {', '.join(not_compared)}" if not_compared else ""
    if missing or different:
        lines = [f"{matched} of {len(table)} Excel rows match the database ({checked}{note})."]
        if missing:
            lines.append(f"{len(missing)} not in the database: " + " | ".join(missing[:10])
                         + (" ..." if len(missing) > 10 else ""))
        if different:
            lines.append(f"{len(different)} with different values: " + " | ".join(different[:10])
                         + (" ..." if len(different) > 10 else ""))
        raise ActionError("\n".join(lines))
    return f"All {len(table)} Excel rows are in the database ({checked}{note})"


@action("verify_excel_rows")
def verify_excel_rows(ctx, params):
    """
    Pass if the downloaded Excel has "value" data rows, or as many rows as the number
    returned by "sql" (e.g. SELECT COUNT(*) FROM dbo.ZGM1Prices WHERE ProfitcenterCode = '2189').
    """
    from .db import to_text
    rows = _require_excel(ctx).row_count()

    if "sql" in params:
        _, result = _db_query(ctx, params["sql"])
        if not result:
            raise ActionError("Query returned no rows - it should return a count, e.g. SELECT COUNT(*) ...")
        expected, source = to_text(result[0][0]), "the database"
    elif "value" in params:
        expected, source = str(params["value"]).strip(), "expected"
    else:
        raise ActionError("Give a number or a database query: verify excel rows = <n> / = db <select>")

    try:
        expected_count = int(float(expected))
    except ValueError:
        raise ActionError(f"'{expected}' is not a number of rows.")
    if rows != expected_count:
        raise ActionError(f"Excel has {rows} data rows, {source} says {expected_count} "
                          f"(difference {rows - expected_count:+d}).")
    return f"Excel has {rows} data rows, same as {source}"


# =============================================================================
# VALIDATION OF THE JSON (no browser needed)
# =============================================================================

def validate_actions(test_case) -> list[str]:
    """Return a list of problems in the test case's actions. Empty list = valid."""
    problems = list(test_case.load_problems)
    for step in test_case.steps:
        for index, params in enumerate(step.actions, start=1):
            if "line" in params and test_case.steps_file is not None:
                where = (f"{test_case.steps_file.name} line {params['line']} "
                         f"'{params.get('description', '')}'")
            elif "step_line" in params:
                where = (f"Step {step.sequence} (Jira Step field) line {params['step_line']} "
                         f"'{params.get('description', '')}'")
            else:
                where = f"Step {step.sequence}, action {index}"
            if "error" in params:
                problems.append(f"{where}: {params['error']}")
                continue
            name = params.get("action")
            if name not in ACTIONS:
                problems.append(f"{where}: unknown action '{name}'. "
                                f"Known: {', '.join(sorted(ACTIONS))}")
                continue
            spec = ACTIONS[name]
            for key in spec.required:
                if key not in params:
                    problems.append(f"{where} ({name}): missing required key '{key}'.")
                elif key in ("text", "column", "value") and isinstance(params[key], str) \
                        and not params[key].strip() and not params.get("allow_empty"):
                    problems.append(f"{where} ({name}): '{key}' is empty - a check on an empty "
                                    "text proves nothing. Say what the cell or page should contain.")
            if name == "switch_tab" and not any(k in params for k in ("index", "url_contains", "title_contains")):
                problems.append(f"{where} (switch_tab): needs one of index / url_contains / title_contains.")
            if name == "upload" and "file" in params and resolve_upload_file(params["file"]) is None:
                problems.append(f"{where} (upload): file '{params['file']}' not found - put it in "
                                f"{config.TEST_DATA_DIR} or give the full path.")
            if spec.needs_target and not has_target(params):
                problems.append(f"{where} ({name}): needs a target "
                                "(text / field / css / xpath / role / label / placeholder).")
            if "sql" in params:
                from .db import QueryNotAllowed, check_read_only
                try:
                    check_read_only(params["sql"])
                except QueryNotAllowed as e:
                    problems.append(f"{where} ({name}): {e}.")
    return problems
