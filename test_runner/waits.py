"""Waiting for the page to finish loading before acting on it or taking screenshots."""

import time

from . import config


def wait_for_no_loading_indicator(page, timeout_ms: int = config.LOADING_WAIT_MS) -> bool:
    """Wait until no known spinner/overlay is visible. Returns False on timeout."""
    deadline = time.time() + timeout_ms / 1000

    while time.time() < deadline:
        blocking = False

        for selector in config.LOADING_INDICATOR_SELECTORS:
            try:
                locator = page.locator(selector)
                for index in range(min(locator.count(), 5)):
                    if locator.nth(index).is_visible():
                        blocking = True
                        break
            except Exception:
                pass

            if blocking:
                break

        if not blocking:
            return True

        page.wait_for_timeout(200)

    return False


def wait_for_page_settled(page, timeout_ms: int = config.LOADING_WAIT_MS) -> None:
    """Best-effort wait for network quiet and spinners gone. Never raises."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass

    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass

    wait_for_no_loading_indicator(page, timeout_ms)
