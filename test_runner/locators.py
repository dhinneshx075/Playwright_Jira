"""
Finding elements from a "target" in the JSON.

A target is any one of these keys on an action (first match wins, in this order):

    "css":         "#btnViewReport"
    "xpath":       "//button[@id='x']"
    "role":        "button", with optional "name": "Run Report"
    "label":       "Profit Center Code"          (form field by its label)
    "placeholder": "Example: 939840"
    "text":        "Price Dictionary"            (visible text, exact first, then partial,
                                                  then a button/link with that name)
    "field":       "Profit Center Code"          (input/dropdown by label, placeholder or name)
    "option":      "ItemMarketName"              (the checkbox / radio button for that text,
                                                  e.g. in the Report Columns panel)

Optional modifiers:
    "exact":  true/false   force exact or partial text/name matching
    "nth":    0            pick the Nth visible match (default first)
    "within": "css"        only search inside this container

All frames on the page are searched, so iframe content works too.
"""

from __future__ import annotations

import time

from . import config, debug
from .widgets import NEAR_LABEL_XPATHS

TARGET_KEYS = ("css", "xpath", "role", "label", "placeholder", "text", "field", "option")


def has_target(params: dict) -> bool:
    return any(key in params for key in TARGET_KEYS)


def describe_target(params: dict) -> str:
    for key in TARGET_KEYS:
        if key in params:
            text = f"{key}='{params[key]}'"
            if key == "role" and "name" in params:
                text += f" name='{params['name']}'"
            if "within" in params:
                text += f" within '{params['within']}'"
            return text
    return "<no target>"


def _candidate_locators(root, params: dict):
    """Yield locators to try, most specific first."""
    exact = params.get("exact")

    if "css" in params:
        yield root.locator(params["css"])
    elif "xpath" in params:
        yield root.locator(f"xpath={params['xpath']}")
    elif "role" in params:
        yield root.get_by_role(
            params["role"], name=params.get("name"), exact=bool(exact)
        )
    elif "label" in params:
        for is_exact in ([exact] if exact is not None else [True, False]):
            yield root.get_by_label(params["label"], exact=is_exact)
    elif "placeholder" in params:
        for is_exact in ([exact] if exact is not None else [True, False]):
            yield root.get_by_placeholder(params["placeholder"], exact=is_exact)
    elif "text" in params:
        for is_exact in ([exact] if exact is not None else [True, False]):
            yield root.get_by_text(params["text"], exact=is_exact)
        # Buttons like <input type="submit" value="Run Report"> have no text node.
        for role in ("button", "link", "tab", "menuitem"):
            yield root.get_by_role(role, name=params["text"])
    elif "option" in params:
        yield from _option_candidates(root, params["option"])
    elif "field" in params:
        name = params["field"]
        yield root.get_by_label(name, exact=True)
        yield root.get_by_placeholder(name, exact=True)
        for role in ("textbox", "combobox", "listbox", "checkbox", "radio", "spinbutton"):
            yield root.get_by_role(role, name=name)
        # Report filter pages: plain text label with the control next to it.
        label_text = root.get_by_text(name, exact=True)
        for xpath in NEAR_LABEL_XPATHS:
            yield label_text.locator(xpath)
        yield root.get_by_label(name)
        yield root.get_by_placeholder(name)
        label_text = root.get_by_text(name)
        for xpath in NEAR_LABEL_XPATHS:
            yield label_text.locator(xpath)


_BOX = "input[@type='checkbox' or @type='radio']"

# Checkbox/radio for a piece of text. The box usually comes BEFORE its text
# (e.g. "[x] ItemMarketName"), so "the next control after the label" would pick the
# NEXT row's box - these look inside, beside and just before the text instead.
_OPTION_XPATHS = [
    f"xpath=.//{_BOX}",                          # <label><input> text</label>
    f"xpath=preceding-sibling::{_BOX}[1]",       # <input> <span>text</span>
    f"xpath=../{_BOX}",                          # <div><input><span>text</span></div>
    f"xpath=preceding-sibling::*[1]//{_BOX}",    # <span><input></span><span>text</span>
    f"xpath=preceding::{_BOX}[1]",               # anything else: the nearest box before the text
]


def _option_candidates(root, text: str):
    yield root.get_by_role("checkbox", name=text, exact=True)
    yield root.get_by_role("radio", name=text, exact=True)
    yield root.get_by_label(text, exact=True)
    for exact in (True, False):
        label_text = root.get_by_text(text, exact=exact)
        for xpath in _OPTION_XPATHS:
            yield label_text.locator(xpath)


def _control_next_to_label(root, name: str):
    """
    The control shown right after the visible label text `name`, when that text appears
    exactly once - else None. Used to catch forms whose <label for="..."> points at the
    wrong box (e.g. "Country Found" linked to the Geography dropdown).
    """
    try:
        labels = root.get_by_text(name, exact=True)
        shown = [labels.nth(i) for i in range(min(labels.count(), 5)) if labels.nth(i).is_visible()]
        if len(shown) != 1:
            return None
        control = shown[0].locator(NEAR_LABEL_XPATHS[0])
        if control.count() > 0 and control.first.is_visible():
            return control.first
    except Exception:
        pass
    return None


_IS_CONTROL_JS = """
(el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'select' || tag === 'textarea' || tag === 'p-multiselect') return true;
    return tag === 'input' && !el.readOnly && el.type !== 'hidden';
}
"""


def _is_control(element) -> bool:
    """A real, editable box (the kind a label is expected to sit next to)."""
    try:
        return element.evaluate(_IS_CONTROL_JS)
    except Exception:
        return False


def _same_control(a, b) -> bool:
    """Same element, or one inside the other (a dropdown widget and its inner input)."""
    try:
        return a.evaluate("(x, y) => x === y || x.contains(y) || y.contains(x)",
                          b.element_handle(timeout=2000))
    except Exception:
        return True


def _search_roots(page, params: dict):
    for frame in page.frames:
        if "within" in params:
            yield frame.locator(params["within"])
        else:
            yield frame


def find_element(page, params: dict, timeout_ms: int | None = None):
    """Return the first visible element matching the target, or None after timeout."""
    timeout_ms = params.get("timeout_ms", timeout_ms or config.DEFAULT_TIMEOUT_MS)
    wanted_index = int(params.get("nth", 0))
    deadline = time.time() + timeout_ms / 1000

    while True:
        for root in _search_roots(page, params):
            for locator in _candidate_locators(root, params):
                try:
                    visible = []
                    for index in range(min(locator.count(), 50)):
                        item = locator.nth(index)
                        if item.is_visible():
                            visible.append(item)
                    if wanted_index < len(visible):
                        found = visible[wanted_index]
                        if "field" in params and wanted_index == 0:
                            beside = _control_next_to_label(root, params["field"])
                            if (beside is not None and _is_control(found)
                                    and not _same_control(found, beside)):
                                print(f"        Note: the label '{params['field']}' is linked to "
                                      "another box in the page code - using the box shown "
                                      "next to the label.", flush=True)
                                return beside
                        return found
                except Exception as e:   # a locator that cannot be evaluated just does not match
                    debug.swallowed(f"find_element {describe_target(params)}", e)
                    continue

        if time.time() >= deadline:
            return None

        page.wait_for_timeout(500)
