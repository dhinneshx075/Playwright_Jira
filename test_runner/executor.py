"""Runs a TestCase step by step, recording results and screenshots."""

from __future__ import annotations

import re
import traceback
from datetime import datetime
from pathlib import Path

from . import config
from .actions import ACTIONS, ActionError
from .browser import BrowserSession
from . import generated, repair
from .models import ActionResult, Step, TestCase
from .waits import wait_for_no_loading_indicator


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(text)).strip("_")[:60] or "shot"


class ExecutionContext:
    """What an action can reach: the browser session, the test case, the current step."""

    def __init__(self, session: BrowserSession, test_case: TestCase, run_dir: Path):
        self.session = session
        self.test_case = test_case
        self.run_dir = run_dir
        self.screenshot_dir = run_dir / "screenshots"
        self.step: Step | None = None
        self._shot_counter = 0

        # Report state shared between actions (open_report, run_report, download_excel ...)
        self.report_name: str | None = None
        self.filter_page = None
        self.filter_url: str | None = None
        self.report_columns_expanded_pages = []
        self.no_data_message: str | None = None
        self.excel = None
        # "select" choices made on the current report's filter page; filter tests
        # re-apply them after reloading a clean page (e.g. a required "Run Report For").
        self.base_selections: list[tuple[dict, str]] = []
        # Database for the "db" commands; None = config.DB_DEFAULT_DATABASE.
        self.database: str | None = None

    @property
    def page(self):
        return self.session.page

    def take_screenshot(self, name: str) -> Path | None:
        self._shot_counter += 1
        sequence = self.step.sequence if self.step else 0
        path = self.screenshot_dir / (
            f"step{sequence:02d}_{self._shot_counter:03d}_{safe_name(name)}.png"
        )
        try:
            wait_for_no_loading_indicator(self.page, 10000)
            self.page.screenshot(path=str(path), full_page=config.SCREENSHOT_FULL_PAGE)
        except Exception as e:
            print(f"    WARNING: screenshot failed: {str(e).splitlines()[0]}")
            return None
        if self.step is not None:
            self.step.screenshots.append(path)
        return path


def _log(text: str = ""):
    print(text, flush=True)


class BrowserClosed(Exception):
    """The browser window was closed (e.g. by hand) while the test was running."""


def _browser_closed(ctx) -> bool:
    try:
        return all(page.is_closed() for page in ctx.session.context.pages)
    except Exception:
        return True


def _try_repair(ctx: ExecutionContext, step: Step, params: dict, error: str) -> list[dict] | None:
    """Let the LLM look at the real page and fix a failed interaction. Returns the actions
    that worked (they replace the failed one), or None if it could not be fixed."""
    tried = [str({k: v for k, v in params.items() if k != "description"})]
    first_error = error
    for attempt in range(1, config.LLM_REPAIR_ATTEMPTS + 1):
        _log(f"        AI repair attempt {attempt}: looking at the page...")
        proposal = repair.propose(step, params, error, ctx.page, tried)
        if proposal is None:
            tried.append("Previous response was unusable. Return a non-empty JSON array; every click must include a valid target from the visible page.")
            continue
        precondition = params.get("action") in repair.PRECONDITION_CHECKS
        try:
            for fixed in proposal:
                ACTIONS[fixed["action"]].fn(ctx, fixed)
            if precondition:
                ACTIONS[params["action"]].fn(ctx, params)   # the check, unchanged
        except Exception as e:
            error = str(e) if isinstance(e, ActionError) else f"{type(e).__name__}: {e}"
            tried.append(str(proposal))
            continue
        repair.log_repair(ctx, step, params, proposal, first_error)
        _log(f"        AI repair worked: {proposal}")
        return proposal + [params] if precondition else proposal
    return None


def _run_step(ctx: ExecutionContext, step: Step) -> None:
    ctx.step = step
    messages = []
    failure = None
    original = list(step.actions)
    effective: list[dict] = []     # the actions as they actually ran (a repair replaces the failed one)
    consumed = 0
    repaired = False
    no_data = False

    for index, params in enumerate(original, start=1):
        consumed = index
        effective.append(params)
        name = params["action"]
        description = params.get("description", "")
        _log(f"  [{index}/{len(step.actions)}] {name}"
             + (f" - {description}" if description else ""))

        result = ActionResult(index, name, description, "PASS", "")

        try:
            result.message = ACTIONS[name].fn(ctx, params)
            if params.get("screenshot"):
                result.screenshot = ctx.take_screenshot(f"{name}_{index}")
        except Exception as e:
            if not isinstance(e, ActionError) and _browser_closed(ctx):
                result.status = "FAIL"
                result.message = "The browser window was closed during the test."
                _log(f"        FAIL: {result.message}")
                step.action_results.append(result)
                step.status = "FAIL"
                step.actual_result = f"Failed at action {index} ({name}): {result.message}"
                raise BrowserClosed() from None
            result.message = str(e) if isinstance(e, ActionError) else f"{type(e).__name__}: {e}"
            fixed = (_try_repair(ctx, step, params, result.message)
                     if repair.is_repairable(ctx, params) else None)
            if fixed is not None:
                effective[-1:] = fixed
                repaired = True
                result.message = f"Repaired by AI (original failed: {result.message})"
                step.action_results.append(result)
                messages.append(f"{index}. PASS - {result.message}")
                _log(f"        PASS: {result.message}")
                continue
            result.status = "WARN" if params.get("optional") else "FAIL"
            if config.SCREENSHOT_ON_FAILURE:
                result.screenshot = ctx.take_screenshot(f"{result.status}_{name}_{index}")
            if not isinstance(e, ActionError):
                traceback.print_exc()

        _log(f"        {result.status}: {result.message}")
        step.action_results.append(result)
        messages.append(f"{index}. {result.status} - {result.message}")

        if result.status == "FAIL":
            if failure is None:
                failure = result
            # With --keep-going a failed check does not stop the step, so one run lists every failure.
            if ctx.test_case.stop_on_failure or not name.startswith("verify"):
                break

        if name == "run_report" and ctx.no_data_message:
            no_data = True
            break

    if failure is None and no_data:
        if repaired:
            step.actions = effective + original[consumed:]
            step.repaired = True
        if step.screenshot_after_step:
            ctx.take_screenshot(f"step{step.sequence}_end")
        step.status = "NO_DATA"
        step.actual_result = f"Report returned no data: {ctx.no_data_message}"
        return

    if failure is None and repaired:
        step.actions = effective + original[consumed:]
        step.repaired = True

    if failure is None and step.screenshot_after_step:
        ctx.take_screenshot(f"step{step.sequence}_end")

    if failure is None:
        step.status = "PASS"
        step.actual_result = (
            f"As expected. All {len(step.actions)} automated checks passed.\n"
            + "\n".join(messages)
        )
    else:
        step.status = "FAIL"
        step.actual_result = (
            f"Failed at action {failure.index} ({failure.action}): {failure.message}\n"
            + "\n".join(messages)
        )


def _overall_status(steps: list[Step]) -> str:
    statuses = {step.status for step in steps}
    if "FAIL" in statuses or "BLOCKED" in statuses:
        return "FAIL"   # BLOCKED always follows a failure or a runner error
    if "NO_DATA" in statuses:
        return "NO_DATA"
    if statuses == {"PASS"}:
        return "PASS"
    if "PASS" in statuses:
        return "PARTIAL"   # some steps not automated
    return "NOT_AUTOMATED"


def run_test_case(test_case: TestCase, headless: bool, output_root: Path,
                  use_saved_profile: bool | None = None) -> Path:
    """Execute every step and return the run's output folder."""
    run_dir = output_root / f"{test_case.id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    screenshot_dir = run_dir / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    test_case.started_at = datetime.now().isoformat(timespec="seconds")
    _log("=" * 80)
    _log(f"TEST CASE {test_case.id}  ({len(test_case.steps)} steps)")
    _log("=" * 80)

    try:
        with BrowserSession(headless=headless, use_saved_profile=use_saved_profile) as session:
            ctx = ExecutionContext(session, test_case, run_dir)
            blocked = False
            no_data_message = None

            for step in test_case.steps:
                _log()
                _log(f"STEP {step.sequence}: {step.step[:150]}")

                if blocked:
                    step.status = "BLOCKED"
                    step.actual_result = "Not executed because a previous step failed."
                elif no_data_message:
                    step.status = "SKIPPED"
                    step.actual_result = f"Not executed because the report returned no data: {no_data_message}"
                elif not step.actions:
                    step.status = "NOT_AUTOMATED"
                    step.actual_result = "No actions defined for this step - execute manually."
                else:
                    _run_step(ctx, step)
                    if step.status == "FAIL" and test_case.stop_on_failure:
                        blocked = True
                    elif step.status == "NO_DATA":
                        no_data_message = test_case.no_data_message

                _log(f"  => {step.status}")

    except BrowserClosed:
        _log("  => FAIL (browser window closed - remaining steps not run)")
        for step in test_case.steps:
            if step.status == "NOT_RUN":
                step.status = "BLOCKED"
                step.actual_result = "Not executed because the browser window was closed."

    except Exception as e:
        # Browser could not start, or crashed mid-run.
        if isinstance(e, RuntimeError):
            _log(f"  ERROR: {e}")
        else:
            traceback.print_exc()
        for step in test_case.steps:
            if step.status == "NOT_RUN":
                step.status = "BLOCKED"
                step.actual_result = f"Runner error: {e}"

    try:
        saved = generated.save_repairs(test_case)
        if saved:
            _log(f"  Saved {saved} AI-repaired step(s) to {test_case.generated_file} "
                 "(previous version: .bak) - the next run uses them.")
    except Exception as e:
        _log(f"  WARNING: could not save AI repairs: {e}")

    test_case.finished_at = datetime.now().isoformat(timespec="seconds")
    test_case.status = _overall_status(test_case.steps)
    return run_dir
