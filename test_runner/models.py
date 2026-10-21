"""Test case / step data loaded from the Jira JSON, plus the results filled in by a run."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .simple_steps import (
    STEPS_SUFFIX,
    json_file_for,
    parse_command_block,
    parse_line,
    parse_steps_file,
    raw_from_steps_file,
    split_step_headings,
    steps_file_for,
)


@dataclass
class ActionResult:
    index: int
    action: str
    description: str
    status: str            # PASS / FAIL / WARN
    message: str
    screenshot: Path | None = None


@dataclass
class Step:
    sequence: int
    step: str
    test_data: str
    expected_result: str
    actions: list[dict]
    screenshot_after_step: bool
    raw: dict

    # Filled in by the executor
    status: str = "NOT_RUN"   # PASS / FAIL / BLOCKED / NOT_AUTOMATED / NO_DATA / SKIPPED
    actual_result: str = ""
    screenshots: list[Path] = field(default_factory=list)
    action_results: list[ActionResult] = field(default_factory=list)
    repaired: bool = False    # an AI repair changed this step's actions during the run


@dataclass
class TestCase:
    path: Path
    raw: dict
    cycle_key: str
    assigned_test_case_id: str
    app_url: str
    stop_on_failure: bool
    steps: list[Step]
    steps_file: Path | None = None
    load_problems: list[str] = field(default_factory=list)
    generated_file: Path | None = None   # where AI-generated actions are saved (repairs go back here)

    # Filled in by the executor
    status: str = "NOT_RUN"
    started_at: str = ""
    finished_at: str = ""
    downloads: list[Path] = field(default_factory=list)
    no_data_message: str = ""

    @property
    def id(self) -> str:
        return "_".join(part for part in (self.cycle_key, self.assigned_test_case_id) if part)


def load_test_case(path: str | Path) -> TestCase:
    path = Path(path)

    if path.name.endswith(STEPS_SUFFIX):
        if json_file_for(path).exists():
            path = json_file_for(path)
        else:
            raw = raw_from_steps_file(path)

    if path.suffix == ".json":
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)

    # A tester's .steps.txt next to the JSON takes priority over the JSON's steps.
    steps_file = path if path.name.endswith(STEPS_SUFFIX) else steps_file_for(path)
    return test_case_from_raw(raw, path, steps_file if steps_file.exists() else None)


def test_case_from_raw(raw: dict, path: Path, steps_file: Path | None = None) -> TestCase:
    """A TestCase from Jira data (a JSON file, or fetched from Jira by jira.py)."""
    file_actions = parse_steps_file(steps_file) if steps_file is not None else None
    load_problems = []
    if file_actions is None:
        # "STEP n" lines inside a Jira Step field split it into separate steps.
        raw = dict(raw, steps=split_step_headings(raw.get("steps", [])))

    steps = []
    for index, raw_step in enumerate(raw.get("steps", []), start=1):
        sequence = int(raw_step.get("sequence", index))

        if file_actions is not None:
            actions = file_actions.get(sequence, [])
        elif "actions" in raw_step:
            # "actions" in the JSON may be full dicts or the same one-line commands.
            actions = [
                parse_line(a) if isinstance(a, str) else a
                for a in raw_step.get("actions") or []
            ]
        else:
            # Commands written straight into the Jira "Step" field, one per line.
            actions = parse_command_block(raw_step.get("step", ""))

        steps.append(
            Step(
                sequence=sequence,
                step=raw_step.get("step", ""),
                test_data=raw_step.get("test_data", ""),
                expected_result=raw_step.get("expected_result", ""),
                actions=actions,
                screenshot_after_step=raw_step.get(
                    "screenshot_after_step", config.SCREENSHOT_AFTER_EACH_STEP
                ),
                raw=raw_step,
            )
        )

    if file_actions is not None:
        known = {step.sequence for step in steps}
        for sequence, actions in file_actions.items():
            if sequence == 0:
                continue  # commands above the first STEP line; reported by validation
            if sequence not in known:
                load_problems.append(
                    f"{steps_file.name}: STEP {sequence} does not exist in the Jira test case "
                    f"(steps are {sorted(known)})."
                )
        if 0 in file_actions:
            for action in file_actions[0]:
                load_problems.append(
                    f"{steps_file.name} line {action['line']}: '{action['description']}' "
                    f"- {action['error']}"
                )

    return TestCase(
        path=path,
        raw=raw,
        cycle_key=raw.get("cycle_key", "UNKNOWN"),
        assigned_test_case_id=str(raw.get("assigned_test_case_id", "UNKNOWN")),
        app_url=raw.get("app_url", config.DEFAULT_APP_URL),
        stop_on_failure=raw.get("stop_on_failure", config.STOP_ON_FAILURE),
        steps=steps,
        steps_file=steps_file if file_actions is not None else None,
        load_problems=load_problems,
    )
