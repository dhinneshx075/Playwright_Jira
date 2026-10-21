"""
Reads test cases straight from Jira (Zephyr Scale). The tester writes the commands in
each Test Script step's "Step" field, one per line - the same commands as a .steps.txt.

    python -m test_runner IAODTM-T106608           one test case
    python -m test_runner IAODTM-C22426           every test case in a test cycle
    python -m test_runner IAODTM-T106608 --check  read from Jira and check, no run
    python -m test_runner --jira-project TWC5958 --jira-folder "/Regression/ZGM1"
    python -m test_runner --jira-label automation

The Jira REST API uses HTTP Basic authentication when JIRA_USERNAME and JIRA_API_TOKEN
are set. Otherwise it uses the selected browser with its saved profile for sign-in.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

from . import config, debug
from .simple_steps import jira_text

_KEY = re.compile(r"^[A-Z][A-Z0-9_]*-([TC])\d+$", re.IGNORECASE)


class JiraError(Exception):
    pass


def is_jira_key(text: str) -> bool:
    return bool(_KEY.match(str(text).strip()))


def key_from_url(text: str) -> str | None:
    """The test case / cycle key in a pasted Jira link, e.g. .../testCase/IAODTM-T106608."""
    if not str(text).lower().startswith("http"):
        return None
    keys = re.findall(r"\b([A-Z][A-Z0-9_]*-[TC]\d+)\b", str(text), re.IGNORECASE)
    return keys[-1].upper() if keys else None


def _is_cycle(key: str) -> bool:
    return _KEY.match(key).group(1).upper() == "C"


def _parse_json(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


# Runs inside the signed-in Jira tab, so the request carries exactly what Jira's own pages
# send (the SSO plugin refuses Playwright's separate API channel with 401).
_FETCH_JS = """
async (url) => {
    try {
        const r = await fetch(url, {credentials: 'same-origin',
                                    headers: {'Accept': 'application/json'}});
        return {status: r.status, text: await r.text()};
    } catch (e) {
        return {status: 0, text: String(e)};
    }
}
"""


def _error_text(data) -> str:
    if isinstance(data, dict):
        messages = data.get("errorMessages") or data.get("message") or data.get("errors")
        if messages:
            return str(messages)
    return ""


class JiraReader:
    """Read Jira through API-token auth or the selected browser's saved profile."""

    def __init__(self, headless: bool = False):
        self.headless = headless
        self.base = config.JIRA_BASE_URL.rstrip("/")
        self.username = os.environ.get(config.JIRA_USERNAME_ENV, "").strip()
        self.api_token = os.environ.get(config.JIRA_API_TOKEN_ENV, "")
        self._basic_auth = None

    def __enter__(self):
        if bool(self.username) != bool(self.api_token):
            raise JiraError(
                f"Set both {config.JIRA_USERNAME_ENV} and {config.JIRA_API_TOKEN_ENV}, "
                "or unset both to use browser sign-in."
            )
        if self.username and self.api_token:
            credentials = base64.b64encode(
                f"{self.username}:{self.api_token}".encode("utf-8")
            ).decode("ascii")
            self._basic_auth = f"Basic {credentials}"
            return self

        self._playwright = sync_playwright().start()
        try:
            self.context = self._playwright.chromium.launch_persistent_context(
                config.JIRA_BROWSER_PROFILE_DIR,
                channel=config.BROWSER_CHANNEL,
                headless=self.headless,
            )
        except Exception as e:
            self._playwright.stop()
            raise JiraError(
                f"Could not open {config.BROWSER_NAME} with the Jira profile "
                f"({config.JIRA_BROWSER_PROFILE_DIR}). Close other {config.BROWSER_NAME} "
                "windows using it (e.g. ccr.py) and try again. "
                f"Details: {str(e).splitlines()[0]}"
            )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self._signed_in = False
        self._login_opened = False
        return self

    def __exit__(self, *exc):
        if self._basic_auth:
            return False
        for closer in (self.context.close, self._playwright.stop):
            try:
                closer()
            except Exception:
                pass
        return False

    def _goto(self, url: str):
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=config.NAVIGATION_TIMEOUT_MS)
        except Exception as e:   # sign-in redirects interrupt the navigation - the caller polls
            debug.swallowed("Jira goto", e)

    def _sign_in(self):
        """Open Jira in the window so the company gateway's SSO runs."""
        self._goto(self.base)

    def _on_jira(self) -> bool:
        url = self.page.url
        return url.startswith(self.base) and "/my.policy" not in url

    def _fetch(self, url: str) -> dict:
        try:
            return self.page.evaluate(_FETCH_JS, url)
        except Exception as e:  # the tab was navigating (sign-in redirects)
            return {"status": 0, "text": str(e).splitlines()[0]}

    def _signed_in_as(self) -> str | None:
        """The Jira user's name if this tab's Jira session works, else None."""
        result = self._fetch(self.base + "/rest/api/2/myself")
        data = _parse_json(result["text"])
        if result["status"] == 200 and isinstance(data, dict):
            return data.get("displayName") or data.get("name") or "?"
        return None

    def _get_with_api_token(self, path: str):
        url = self.base + path
        request = Request(url, headers={
            "Accept": "application/json",
            "Authorization": self._basic_auth,
        })
        try:
            with urlopen(request, timeout=config.JIRA_API_TIMEOUT_S) as response:
                body = response.read().decode("utf-8")
        except HTTPError as e:
            if e.code in (401, 403):
                raise JiraError(
                    f"Jira API authentication/permission failed for {path} (HTTP {e.code}). "
                    "Check the username, token type, and Jira permissions."
                ) from None
            raise JiraError(f"Jira API request failed for {path} (HTTP {e.code}).") from None
        except (URLError, TimeoutError) as e:
            reason = getattr(e, "reason", e)
            raise JiraError(f"Could not reach the Jira API for {path}: {reason}") from None

        data = _parse_json(body)
        if data is None:
            raise JiraError(f"Jira returned non-JSON data for {path}.")
        return data

    def get(self, path: str):
        """GET a Jira REST path as JSON from the Jira tab, signing in first if needed."""
        if self._basic_auth:
            return self._get_with_api_token(path)

        url = self.base + path
        wait_s = 30 if self.headless else config.JIRA_SIGN_IN_WAIT_S
        deadline = time.time() + wait_s
        told_user = False
        last = {"status": 0, "text": "Jira page not open yet"}

        if not self._signed_in:
            self._sign_in()

        while True:
            if self._on_jira():
                last = self._fetch(url)
                status, data = last["status"], _parse_json(last["text"])

                if data is not None and 200 <= status < 300:
                    self._signed_in = True
                    return data
                if status == 404:
                    raise JiraError(f"Not found in Jira: {path} {_error_text(data)}".strip())
                if status == 403:
                    raise JiraError(f"No permission to read {path} in Jira. {_error_text(data)}".strip())
                if status == 401:
                    user = self._signed_in_as()
                    if user:
                        raise JiraError(
                            f"Signed in to Jira as {user}, but {path} answered HTTP 401: "
                            f"{last['text'][:300]}"
                        )
                    if not self._login_opened:
                        # Jira shows the anonymous dashboard ("Log In" top right): its login
                        # page hands over to Microsoft sign-in, which usually completes by itself.
                        self._login_opened = True
                        print("    Jira is not logged in - opening the Jira login page...", flush=True)
                        self._goto(self.base + "/login.jsp")
                        continue
                elif status >= 400:
                    raise JiraError(f"Jira answered HTTP {status} for {path}: "
                                    f"{_error_text(data) or last['text'][:300]}")

            # Still signing in: wait for it to finish in the window.
            if time.time() >= deadline:
                raise JiraError(
                    f"Not signed in to Jira after {wait_s}s (tab: {self.page.url[:100]}, "
                    f"last response: HTTP {last['status']} {last['text'][:150]!r}). "
                    + ("Run once without --headless and sign in in the browser window."
                       if self.headless else "Sign in in the browser window, then run again.")
                )
            if not told_user:
                print("    Waiting for Jira sign-in - if the browser shows a sign-in page or a "
                      f"'Log In' button, sign in there (up to {wait_s}s)...", flush=True)
                told_user = True
            self.page.wait_for_timeout(3000)

    # ------------------------------------------------------------------ Zephyr Scale

    def test_case(self, key: str, cycle_key: str = "") -> dict:
        data = self.get(f"/rest/atm/1.0/testcase/{key}")
        return _to_test_case_json(data, key, cycle_key, self.base)

    def search_test_case_keys(self, query: str) -> list[str]:
        """Keys of the test cases matching a Zephyr Scale query (TQL), all pages."""
        keys, start, page_size = [], 0, 100
        while True:
            data = self.get("/rest/atm/1.0/testcase/search?"
                            + urlencode({"query": query, "fields": "key",
                                         "startAt": start, "maxResults": page_size}))
            items = data if isinstance(data, list) else data.get("values") or []
            keys.extend(item.get("key") for item in items if item.get("key"))
            if len(items) < page_size:
                break
            start += page_size
        keys = list(dict.fromkeys(keys))
        if not keys:
            raise JiraError(f"No test cases match: {query}")
        return keys

    def cycle_test_case_keys(self, cycle_key: str) -> list[str]:
        data = self.get(f"/rest/atm/1.0/testrun/{cycle_key}")
        keys = [item.get("testCaseKey") for item in data.get("items") or []]
        keys = list(dict.fromkeys(k for k in keys if k))
        if not keys:
            raise JiraError(f"Test cycle {cycle_key} has no test cases.")
        return keys


def _to_test_case_json(data: dict, key: str, cycle_key: str, base: str) -> dict:
    """Zephyr Scale test case -> the same shape as the Jira JSON files in test_cases\\."""
    script = data.get("testScript") or {}
    raw_steps = script.get("steps") or []
    if not raw_steps:
        kind = script.get("type") or "none"
        raise JiraError(f"{key}: the Test Script has no steps (type: {kind}). "
                        "Use Type: Step-by-Step and write the commands in each Step field.")

    raw_steps = sorted(raw_steps, key=lambda s: s.get("index", 0))
    steps = []
    for sequence, raw_step in enumerate(raw_steps, start=1):
        text = jira_text(raw_step.get("description"))
        if not text and raw_step.get("testCaseKey"):
            # A step that calls another test case: shown as a note, so it counts as manual.
            text = f"# Calls test case {raw_step['testCaseKey']}"
        steps.append({
            "sequence": sequence,
            "step": text,
            "test_data": jira_text(raw_step.get("testData")),
            "expected_result": jira_text(raw_step.get("expectedResult")),
            "actual_result": "",
        })

    test_case_key = data.get("key") or key
    return {
        "cycle_key": cycle_key,
        "assigned_test_case_id": test_case_key,
        "name": data.get("name", ""),
        "project_id": data.get("projectKey", ""),
        "url": f"{base}/secure/Tests.jspa#/testCase/{test_case_key}",
        "source": "jira",
        "steps": steps,
    }


def _quoted(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_query(project: str | None = None, folder: str | None = None,
                labels: list[str] | None = None) -> str:
    """A Zephyr Scale query (TQL) from --jira-project / --jira-folder / --jira-label."""
    parts = []
    if project:
        parts.append(f"projectKey = {_quoted(project.strip().upper())}")
    if folder:
        folder = "/" + folder.strip().strip("/")
        parts.append(f"folder = {_quoted(folder)}")
    if labels:
        parts.append(f"labels IN ({', '.join(_quoted(l.strip()) for l in labels)})")
    return " AND ".join(parts)


def _key_list(keys: list[str], shown: int = 10) -> str:
    more = f" ... (+{len(keys) - shown} more)" if len(keys) > shown else ""
    return ", ".join(keys[:shown]) + more


def fetch(keys: list[str], queries: list[str] = (), headless: bool = False
          ) -> list[tuple[str, dict | None, str]]:
    """
    Read test cases from Jira: test case T-keys, test cycle C-keys, and Zephyr Scale
    searches (TQL, e.g. from --jira-folder / --jira-label).
    Returns (label, test case JSON or None, error) per test case.
    """
    # (cycle key or "", [test case keys]) per request
    requests = []
    results = []
    with JiraReader(headless=headless) as jira:
        for key in keys:
            key = key.strip().upper()
            print(f"Reading {key} from Jira...", flush=True)
            try:
                if _is_cycle(key):
                    case_keys = jira.cycle_test_case_keys(key)
                    print(f"    {key}: {len(case_keys)} test case(s): {_key_list(case_keys)}", flush=True)
                    requests.append((key, case_keys))
                else:
                    requests.append(("", [key]))
            except JiraError as e:
                results.append((key, None, str(e)))

        for query in queries:
            print(f"Searching Jira: {query}", flush=True)
            try:
                case_keys = jira.search_test_case_keys(query)
                print(f"    {len(case_keys)} test case(s): {_key_list(case_keys)}", flush=True)
                requests.append(("", case_keys))
            except JiraError as e:
                results.append((query, None, str(e)))

        done = set()
        for cycle_key, case_keys in requests:
            for case_key in case_keys:
                if (cycle_key, case_key) in done:
                    continue  # e.g. in the folder and also given by key
                done.add((cycle_key, case_key))
                label = f"{cycle_key} {case_key}".strip()
                try:
                    results.append((label, jira.test_case(case_key, cycle_key=cycle_key), ""))
                except JiraError as e:
                    results.append((label, None, str(e)))
    return results
