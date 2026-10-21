"""
Report filter page and report viewer handling (moved from validate.py):
run a report with retry, wait for its data, export to Excel, reset the filter page.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from . import config, debug
from .waits import wait_for_no_loading_indicator, wait_for_page_settled


def _log(text: str):
    print(f"        {text}", flush=True)


def wait_for_filter_page_ready(page, timeout_ms: int = 30000) -> bool:
    try:
        page.locator(config.RUN_REPORT_SELECTOR).first.wait_for(state="visible", timeout=timeout_ms)
    except Exception as e:
        debug.swallowed("wait for filter page", e)
        return False
    return wait_for_no_loading_indicator(page, timeout_ms)


def _page_text(page) -> str:
    chunks = []
    for frame in page.frames:
        try:
            chunks.append(frame.locator("body").inner_text(timeout=5000))
        except Exception:
            continue
    return "\n".join(chunks)


def no_data_message(page) -> str | None:
    text = _page_text(page).lower()
    return next((message for message in config.NO_DATA_TEXTS if message.lower() in text), None)


def _export_button_visible(page) -> bool:
    for frame in page.frames:
        try:
            button = frame.locator(config.EXPORT_DROPDOWN_SELECTOR)
            if button.count() > 0 and button.first.is_visible():
                return True
        except Exception:
            continue
    return False


def wait_for_report_data(page, markers: list[str] | None) -> bool:
    """True once the viewer has finished loading and shows report data."""
    deadline = time.time() + config.REPORT_LOAD_TIMEOUT_S

    while time.time() < deadline:
        if wait_for_no_loading_indicator(page, 1000):
            if no_data_message(page):
                return True
            if markers:
                text = _page_text(page).lower()
                if any(marker.lower() in text for marker in markers):
                    return True
            elif _export_button_visible(page):
                return True
        page.wait_for_timeout(1000)

    return False


def _open_report_tab(context, filter_page):
    """Click Run Report and return the tab the report opens in, or None."""
    button = filter_page.locator(config.RUN_REPORT_SELECTOR).first
    try:
        button.wait_for(state="visible", timeout=30000)
    except Exception as e:
        debug.swallowed("Run Report button not visible", e)
        return None

    pages_before = list(context.pages)

    try:
        with context.expect_page(timeout=15000) as page_info:
            button.click()
        report_page = page_info.value
    except Exception as e:
        debug.swallowed("Run Report click / new tab", e)
        # Fall back to any new tab, or any report viewer tab.
        filter_page.wait_for_timeout(3000)
        report_page = None
        for page in context.pages:
            if page not in pages_before:
                report_page = page
        if report_page is None:
            for page in context.pages:
                if config.REPORT_VIEWER_URL_PART in page.url:
                    report_page = page

    if report_page is not None:
        try:
            report_page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass
    return report_page


def run_report_with_retry(context, filter_page, markers: list[str] | None):
    """Returns (report page or None, message)."""
    for attempt in range(1, config.MAX_REPORT_ATTEMPTS + 1):
        _log(f"Run Report attempt {attempt}/{config.MAX_REPORT_ATTEMPTS}")
        report_page = _open_report_tab(context, filter_page)

        if report_page is not None:
            if wait_for_report_data(report_page, markers):
                return report_page, f"Report loaded (attempt {attempt})"
            _log(f"Report did not load within {config.REPORT_LOAD_TIMEOUT_S}s")
            try:
                report_page.close()
            except Exception:
                pass
        else:
            _log("No report tab opened")

        if attempt < config.MAX_REPORT_ATTEMPTS:
            filter_page.wait_for_timeout(config.RETRY_WAIT_S * 1000)

    return None, (f"Report did not load after {config.MAX_REPORT_ATTEMPTS} attempts "
                  f"({config.REPORT_LOAD_TIMEOUT_S}s each).")


def download_excel(report_page, download_dir: Path, name: str) -> Path:
    """Export the report shown in the viewer to Excel. Raises on failure."""
    export = None
    for frame in report_page.frames:
        candidate = frame.locator(config.EXPORT_DROPDOWN_SELECTOR)
        if candidate.count() > 0:
            export = candidate.first
            root = frame
            break
    if export is None:
        raise RuntimeError("Export button not found on this page - is the report viewer open?")

    export.wait_for(state="visible", timeout=30000)
    export.click()

    excel_option = root.get_by_title("Excel", exact=True)
    excel_option.wait_for(state="visible", timeout=15000)

    with report_page.expect_download(timeout=config.DOWNLOAD_TIMEOUT_MS) as download_info:
        excel_option.click()

    download_dir.mkdir(parents=True, exist_ok=True)
    path = download_dir / f"{name}_{datetime.now().strftime('%H%M%S')}.xlsx"
    download_info.value.save_as(str(path))
    return path


def close_report_tabs(context, keep_page) -> None:
    """Close every tab except the filter page (and browser-internal pages)."""
    for page in list(context.pages):
        if page is keep_page:
            continue
        try:
            if not page.url.startswith(("edge://", "chrome://")):
                page.close(run_before_unload=False)
        except Exception:
            pass


def reload_filter_page(page, url: str) -> bool:
    try:
        page.bring_to_front()
        page.goto(url, wait_until="domcontentloaded", timeout=config.NAVIGATION_TIMEOUT_MS)
    except Exception as e:
        debug.swallowed("reload filter page", e)
    wait_for_page_settled(page)
    return wait_for_filter_page_ready(page)
