"""Browser lifecycle and tracking of the "current" tab that actions work on."""

import os

from playwright.sync_api import sync_playwright

from . import config


class BrowserSession:
    """
    The system-default Chrome or Edge browser for a test run. With
    config.USE_SAVED_PROFILE its saved browser profile
    is used. Optional HTTP Basic credentials are scoped to the configured app origin;
    otherwise the context uses the saved profile or a clean browser with no cookies.
    """

    def __init__(self, headless: bool = config.HEADLESS, use_saved_profile: bool = None):
        self.headless = headless
        self.use_saved_profile = (config.USE_SAVED_PROFILE if use_saved_profile is None
                                  else use_saved_profile)
        self.app_username = os.environ.get(config.DEVIAOPRICE_USERNAME_ENV, "").strip()
        self.app_password = os.environ.get(config.DEVIAOPRICE_PASSWORD_ENV, "")
        self._playwright = None
        self.browser = None
        self.context = None
        self._page = None

    def __enter__(self):
        if bool(self.app_username) != bool(self.app_password):
            raise RuntimeError(
                f"Set both {config.DEVIAOPRICE_USERNAME_ENV} and "
                f"{config.DEVIAOPRICE_PASSWORD_ENV}, or unset both."
            )
        context_options = {}
        if self.app_username and self.app_password:
            context_options["http_credentials"] = {
                "origin": config.DEVIAOPRICE_AUTH_ORIGIN,
                "username": self.app_username,
                "password": self.app_password,
            }

        self._playwright = sync_playwright().start()
        try:
            if self.use_saved_profile:
                try:
                    self.context = self._playwright.chromium.launch_persistent_context(
                        config.BROWSER_PROFILE_DIR,
                        channel=config.BROWSER_CHANNEL,
                        headless=self.headless,
                        accept_downloads=True,
                        viewport=config.VIEWPORT,
                        **context_options,
                    )
                except Exception as e:
                    raise RuntimeError(
                        f"Could not open {config.BROWSER_NAME} with the saved profile "
                        f"({config.BROWSER_PROFILE_DIR}). Close other {config.BROWSER_NAME} "
                        "windows using it (e.g. ccr.py, another test run) and try "
                        f"again, or run with --fresh-browser. Details: {str(e).splitlines()[0]}"
                    ) from None
                # The profile may reopen tabs from last time - start from one blank tab.
                pages = list(self.context.pages)
                self._page = pages[0] if pages else self.context.new_page()
                for extra in pages[1:]:
                    extra.close()
            else:
                self.browser = self._playwright.chromium.launch(
                    channel=config.BROWSER_CHANNEL,
                    headless=self.headless,
                )
                self.context = self.browser.new_context(
                    accept_downloads=True,
                    viewport=config.VIEWPORT,
                    **context_options,
                )
                self._page = self.context.new_page()
        except Exception:
            self._playwright.stop()
            raise

        self.context.set_default_timeout(config.DEFAULT_TIMEOUT_MS)
        self.context.set_default_navigation_timeout(config.NAVIGATION_TIMEOUT_MS)
        return self

    def __exit__(self, *exc):
        for closer in (
            lambda: self.context.close(),
            lambda: self.browser and self.browser.close(),
            lambda: self._playwright.stop(),
        ):
            try:
                closer()
            except Exception:
                pass
        return False

    @property
    def page(self):
        """The tab actions run against. Falls back to the newest open tab if it was closed."""
        if self._page is None or self._page.is_closed():
            open_pages = [p for p in self.context.pages if not p.is_closed()]
            self._page = open_pages[-1] if open_pages else self.context.new_page()
        return self._page

    def switch_to(self, page):
        self._page = page
        try:
            page.bring_to_front()
        except Exception:
            pass
