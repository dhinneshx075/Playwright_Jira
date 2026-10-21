"""Central settings for the test runner. Change values here, not in the modules."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Application opened by the "goto" action when no url is given.
# A test case JSON can override this with a top-level "app_url".
DEFAULT_APP_URL = "https://deviaoprice.intel.com/"
DEVIAOPRICE_AUTH_ORIGIN = "https://deviaoprice.intel.com"
DEVIAOPRICE_USERNAME_ENV = "DEVIAOPRICE_USERNAME"
DEVIAOPRICE_PASSWORD_ENV = "DEVIAOPRICE_PASSWORD"

# Every run gets its own folder here: screenshots, results JSON, Word evidence.
OUTPUT_DIR = BASE_DIR / "test_runs"

# Files for the "upload" command: "upload PVP.xlsx" looks here first (or give a full path).
TEST_DATA_DIR = BASE_DIR / "test_data"

# Browser
def _browser_channel_from_prog_id(prog_id: str) -> str:
    prog_id = prog_id.casefold()
    if "chrome" in prog_id:
        return "chrome"
    if "msedge" in prog_id or "edge" in prog_id:
        return "msedge"
    raise RuntimeError(
        f"Unsupported Windows default browser ({prog_id}). "
        "Set TEST_BROWSER_CHANNEL to 'chrome' or 'msedge'."
    )


def _detect_browser_channel() -> str:
    override = os.environ.get("TEST_BROWSER_CHANNEL", "").strip().casefold()
    if override:
        if override not in {"chrome", "msedge"}:
            raise RuntimeError("TEST_BROWSER_CHANNEL must be 'chrome' or 'msedge'.")
        return override

    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
        ) as key:
            prog_id, _ = winreg.QueryValueEx(key, "ProgId")
    except (ImportError, OSError):
        return "msedge"
    return _browser_channel_from_prog_id(prog_id)


def _browser_profile_dir(channel: str, local_app_data: str | Path | None = None) -> str:
    local_app_data = Path(local_app_data or os.environ.get(
        "LOCALAPPDATA", Path.home() / "AppData" / "Local"
    ))
    if channel == "chrome":
        user_data_dir = local_app_data / "Google" / "Chrome" / "User Data"
    else:
        user_data_dir = local_app_data / "Microsoft" / "Edge" / "User Data"
    return str(user_data_dir / "Default")


BROWSER_CHANNEL = _detect_browser_channel()
BROWSER_NAME = "Google Chrome" if BROWSER_CHANNEL == "chrome" else "Microsoft Edge"
HEADLESS = False
VIEWPORT = {"width": 1600, "height": 900}

# Run tests in the selected browser's saved profile. False = a clean browser with no cookies
# every run (same as --fresh-browser). Sign in once in this profile; it is remembered.
USE_SAVED_PROFILE = True
BROWSER_PROFILE_DIR = _browser_profile_dir(BROWSER_CHANNEL)

# Timeouts (milliseconds)
DEFAULT_TIMEOUT_MS = 30000        # finding elements / verifying text
NAVIGATION_TIMEOUT_MS = 90000     # page.goto and new tabs
NAVIGATION_ATTEMPTS = 3           # retries when the site can't be reached (VPN/network blips)
NEW_TAB_DETECT_MS = 2500          # how long a click waits to see if it opened a tab
LOADING_WAIT_MS = 15000           # waiting for spinners / overlays to disappear

# Screenshots
SCREENSHOT_FULL_PAGE = True
SCREENSHOT_AFTER_EACH_STEP = True  # a step can override with "screenshot_after_step"
SCREENSHOT_ON_FAILURE = True

# If a step fails, the remaining steps are marked BLOCKED instead of run.
# A test case JSON can override this with a top-level "stop_on_failure".
STOP_ON_FAILURE = True

# Top menus searched by "open report <name>" when no menu is given (in this order).
REPORT_MENUS = [
    "On Demand Reports",
    "WW Price Ops",
    "Price Publications",
    "System Administration",
]

# Jira (Zephyr Scale): "python -m test_runner IAODTM-T106608" reads the test case's
# Test Script steps from Jira - the commands are written in each step's "Step" field.
JIRA_BASE_URL = "https://jira.devtools.intel.com"
JIRA_USERNAME_ENV = "JIRA_USERNAME"
JIRA_API_TOKEN_ENV = "JIRA_API_TOKEN"
JIRA_API_TIMEOUT_S = 60
# Jira is behind Microsoft sign-in, so the browser fallback uses the selected browser profile.
# Close other windows using this profile (e.g. ccr.py) while fetching.
JIRA_BROWSER_PROFILE_DIR = BROWSER_PROFILE_DIR
JIRA_EDGE_PROFILE_DIR = JIRA_BROWSER_PROFILE_DIR  # Backward-compatible alias.
JIRA_SIGN_IN_WAIT_S = 180         # how long to wait for you to sign in, when needed

# Azure AI Foundry (OpenAI-compatible) used by "python -m test_runner.prompt <json> --auto"
# to generate the "actions" for a test case. The key is NOT stored here: set the
# FOUNDRY_API_KEY environment variable. The endpoint is only reachable on the corporate network.
LLM_ENDPOINT = "https://foundry-lhariprx-48589c24.services.ai.azure.com/openai/v1"
LLM_DEPLOYMENT = "gpt-5.4-mini"
LLM_API_KEY_ENV = "FOUNDRY_API_KEY"
LLM_TIMEOUT_S = 180
# How hard the model thinks before answering ("low" / "medium" / "high"; "" = deployment default).
# Higher is slower and costs more, but plans multi-step tests better.
LLM_REASONING_EFFORT = "high"
# When an AI-generated click/type/select fails, the LLM sees the real page and proposes a fix.
# Verification actions are never repaired. 0 = off.
LLM_REPAIR_ATTEMPTS = 2
LLM_API_KEY = ""


# Summary mail sent through Outlook after a run (see mail.py). --no-email skips it for one run,
# --email-to <address> sends it elsewhere, --email-preview writes the mail as .html without sending.
EMAIL_AFTER_RUN = True
EMAIL_RECIPIENT = "dhinneshx.purushothaman@intel.com"

# Report viewer (moved from validate.py)
RUN_REPORT_SELECTOR = "#btnViewReport"
EXPORT_DROPDOWN_SELECTOR = "#ReportViewer1_ctl05_ctl04_ctl00_ButtonLink"
REPORT_VIEWER_URL_PART = "ReportViewer.aspx"
NO_DATA_TEXTS = ["No data found for the selected criteria"]  # shown by empty reports
REPORT_LOAD_TIMEOUT_S = 45
MAX_REPORT_ATTEMPTS = 3
RETRY_WAIT_S = 5
DOWNLOAD_TIMEOUT_MS = 60000

# Database (SQL Server, Windows authentication) used by the "db" commands.
# Queries are read-only: only one SELECT per command, always rolled back (see db.py).
DB_SERVER = "sql4045-fm1-in.amr.corp.intel.com,3181"
DB_DEFAULT_DATABASE = "pricebookstaging"   # "use database <name>" switches per test
DB_DRIVER = "ODBC Driver 17 for SQL Server"
DB_LOGIN_TIMEOUT_S = 20
DB_QUERY_TIMEOUT_S = 120
DB_MAX_ROWS = 200000              # a query returning more fails - add WHERE / DISTINCT

# Other databases, by a short name for "use database <name>" - e.g. where an upload page
# saves its data. A name not listed here is a database on DB_SERVER.
#   "pvp": {"server": "<server>,<port>", "database": "<database>"},
DB_CONNECTIONS = {
}

# Spinners / overlays used by the site; the runner waits for these to vanish.
LOADING_INDICATOR_SELECTORS = [
    ".block-ui-wrapper.active",
    ".block-ui-wrapper.block-ui-main.active",
    "block-ui-content .block-ui-wrapper.active",
    ".loading",
    ".loading-overlay",
    ".loading-indicator",
    ".spinner",
    ".spinner-border",
    ".p-progress-spinner",
    "[aria-busy='true']",
    "[class*='AsyncWait']",
    "[id*='AsyncWait']",
    "img[src*='loading' i]",
    "img[src*='spinner' i]",
    "img[src*='ajax-loader' i]",
]
