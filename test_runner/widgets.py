"""
Site-specific controls. The report filter dropdowns are PrimeNG <p-multiselect>
components (ported from validate.py), not native <select> elements.
"""

from __future__ import annotations

# The control for a filter label = the first input control after the label in the page.
# (validate.py also searched the label's grandparent, which could pick another filter's
# dropdown.) A PrimeNG multiselect's own readonly <input> is skipped - the host is used.
_CONTROL = (
    "self::p-multiselect or self::select or self::textarea"
    " or self::input[not(@type='hidden') and not(@readonly)]"
)
NEAR_LABEL_XPATHS = [
    f"xpath=following::*[{_CONTROL}][1]",
]


# Every visible filter control on the page, paired with the last visible text before it
# (its label). Radio buttons and checkboxes are options, not filters, so they are skipped.
_DISCOVER_FILTERS_JS = """
() => {
    const visible = el => {
        const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
    };
    const insideControl = el =>
        el.closest('p-multiselect, select, button, option, script, style, textarea, label.p-checkbox-label');
    const isControl = el => {
        const tag = el.tagName.toLowerCase();
        if (tag === 'p-multiselect' || tag === 'select' || tag === 'textarea') return true;
        if (tag !== 'input' || el.closest('p-multiselect') || el.readOnly) return false;
        const type = (el.type || 'text').toLowerCase();
        return !['hidden', 'radio', 'checkbox', 'submit', 'button', 'image', 'reset'].includes(type);
    };

    const labels = [];
    let lastText = null;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        if (node.nodeType === Node.TEXT_NODE) {
            const text = node.textContent.replace(/\\s+/g, ' ').trim().replace(/\\s*\\*$/, '');
            const parent = node.parentElement;
            if (text && parent && !insideControl(parent) && visible(parent)) lastText = text;
        } else if (isControl(node) && visible(node) && lastText) {
            labels.push(lastText);
            lastText = null;
        }
    }
    return [...new Set(labels)];
}
"""


def discover_filters(page) -> list[str]:
    """Labels of all filters (dropdowns / text boxes) on a report filter page."""
    labels = []
    for frame in page.frames:
        try:
            labels.extend(frame.evaluate(_DISCOVER_FILTERS_JS))
        except Exception:
            continue
    return list(dict.fromkeys(labels))


def primeng_host(element):
    """Return the <p-multiselect> the element is or belongs to, else None."""
    try:
        tag = element.evaluate("(el) => el.tagName.toLowerCase()")
        if tag == "p-multiselect":
            return element
        host = element.locator("xpath=ancestor::p-multiselect[1]")
        if host.count() > 0:
            return host.first
    except Exception:
        pass
    return None


def _close_panel(page, panel):
    try:
        close_button = panel.locator("button.p-multiselect-close")
        if close_button.count() > 0 and close_button.first.is_visible():
            close_button.first.click(timeout=5000)
            return
    except Exception:
        pass
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def _open_panel(page, host):
    """Open a multiselect and return its panel, or None."""
    trigger = host.locator("div.p-multiselect").first
    if trigger.count() == 0:
        trigger = host
    try:
        trigger.click(timeout=10000)
    except Exception:
        return None
    for panel in (host.locator(".p-multiselect-panel"), page.locator(".p-multiselect-panel").last):
        try:
            panel.wait_for(state="visible", timeout=5000)
            return panel
        except Exception:
            continue
    return None


def primeng_options(page, host) -> list[str]:
    """All option texts of a multiselect (opens and closes it)."""
    panel = _open_panel(page, host)
    if panel is None:
        return []
    items = panel.locator("li.p-multiselect-item")
    options = []
    for index in range(items.count()):
        try:
            text = (items.nth(index).inner_text() or "").strip()
        except Exception:
            continue
        if text:
            options.append(text)
    _close_panel(page, panel)
    return options


def select_in_primeng_multiselect(page, host, value: str) -> tuple[bool, str]:
    """Open this multiselect, filter to `value`, tick the matching item, close it."""
    trigger = host.locator("div.p-multiselect").first
    if trigger.count() == 0:
        trigger = host

    try:
        trigger.click(timeout=10000)
    except Exception as e:
        return False, f"Could not open the dropdown: {e}"

    panel = host.locator(".p-multiselect-panel")
    try:
        panel.wait_for(state="visible", timeout=5000)
    except Exception:
        # Some PrimeNG versions append the panel to <body> instead of the host.
        panel = page.locator(".p-multiselect-panel").last
        try:
            panel.wait_for(state="visible", timeout=5000)
        except Exception as e:
            return False, f"Dropdown panel did not open: {e}"

    filter_input = panel.locator("input.p-multiselect-filter")
    try:
        if filter_input.count() > 0 and filter_input.first.is_visible():
            filter_input.first.fill(value, timeout=5000)
            page.wait_for_timeout(500)
    except Exception:
        pass

    target = value.strip().lower()
    items = panel.locator("li.p-multiselect-item")
    exact_match = partial_match = None

    for index in range(items.count()):
        item = items.nth(index)
        try:
            if not item.is_visible():
                continue
            texts = [
                (item.get_attribute("aria-label") or "").strip().lower(),
                (item.inner_text() or "").strip().lower(),
            ]
        except Exception:
            continue
        for text in filter(None, texts):
            if text == target or text.startswith(f"{target} -") or text.startswith(f"{target} "):
                exact_match = exact_match or item
            elif target in text:
                partial_match = partial_match or item

    match = exact_match or partial_match
    if match is None:
        _close_panel(page, panel)
        return False, f"Option '{value}' is not in the dropdown."

    option_text = (match.inner_text() or "").strip()
    try:
        match.click(timeout=10000)
    except Exception as e:
        _close_panel(page, panel)
        return False, f"Could not click option '{value}': {e}"

    _close_panel(page, panel)
    return True, f"Selected '{option_text or value}'"
