"""
Builds a ready-to-paste prompt that asks a chat LLM (e.g. iGPT) to add "actions"
to a Jira test case JSON. The action list is read from actions.py, so the prompt
stays up to date when new actions are added.

    python -m test_runner.prompt test_cases\\test_script_IAODTM-C22426_7905796.json

The prompt is copied to the clipboard and saved next to the JSON as *.prompt.txt.
Paste it into the chat, save the JSON reply over the test case file, then run
    python -m test_runner test_cases\\ --check
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from . import config
from .actions import ACTIONS, validate_actions
from .jira import JiraError, fetch as fetch_from_jira, is_jira_key, key_from_url
from . import generated
from .models import test_case_from_raw

EXAMPLE_ACTIONS = [
    {"action": "goto", "description": "Launch the application"},
    {"action": "verify_visible", "text": "Price Quick Search", "description": "Application launched successfully"},
    {"action": "click", "text": "Price Dictionary", "description": "Open Price Dictionary"},
    {"action": "click", "text": "Data Definition", "description": "Expand Data Definition section"},
    {"action": "verify_column_contains", "column": "Business Name and Description", "text": "ZCNP Price",
     "description": "ZCNP Price is displayed"},
    {"action": "verify_column_not_contains", "column": "Business Name and Description", "text": "YCP1 Price",
     "description": "YCP1 Price is no longer displayed"},
    {"action": "screenshot", "name": "zcnp_price_in_data_definition"},
]

_APP_CONTEXT_FILE = Path(__file__).with_name("app_context.md")


def _load_app_context() -> str:
    """What the AI is told about the application - edit app_context.md, not this file."""
    try:
        return _APP_CONTEXT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "The application is " + config.DEFAULT_APP_URL


APP_CONTEXT = _load_app_context()


def _action_reference() -> str:
    lines = []
    for name, spec in ACTIONS.items():
        keys = (["<target>"] if spec.needs_target else []) + list(spec.required)
        doc = " ".join(line.strip() for line in spec.doc.splitlines())
        lines.append(f'- "{name}"  keys: {", ".join(keys) or "none"}  - {doc}')
    return "\n".join(lines)


def build_prompt(test_case: dict) -> str:
    return f"""You convert manual Jira test steps into browser automation actions.

Return the SAME JSON I give you, unchanged, except: add an "actions" array to every step.
Reply with ONLY the JSON - no explanation, no markdown fences.

## Available actions
{_action_reference()}

## Targets (for actions that need <target>) - use exactly one:
- "text": visible text of the element (preferred, e.g. "text": "Price Dictionary")
- "role" + "name": e.g. "role": "button", "name": "Run Report"
- "label": form field label   - "placeholder": input placeholder
- "css": CSS selector         - "xpath": XPath
Optional: "exact": true/false, "nth": 0, "within": "<css of container>".

## Common keys on any action
"description" (short, taken from the step wording), "optional": true, "screenshot": true, "timeout_ms".

## Rules
1. Break each step into small actions in the order written.
2. Every "verify"/"should"/"displayed"/"expected" statement becomes a verify_* action.
   Use the expected_result as the source of what to verify. Read it BEFORE writing the actions: it
   says what state the page must reach, so add whatever actions get there even if the step does not
   spell them out (e.g. open the section, tab or panel that holds the text it names).
   Text quoted in the expected_result ('...' or "...") must be checked word for word with
   verify_text (or verify_not_text / absent if it must be gone) - copy it exactly.
3. "X instead of Y" means verify X is present AND verify Y is not present.
4. Checks about a table column use verify_column_contains / verify_column_not_contains.
5. End every step with a "screenshot" action with a short snake_case "name".
6. The first step usually starts with {{"action": "goto"}} (no url needed).
7. Use only the action names and keys listed above. Use button/link text exactly as written in the step.
8. To run a report ("click Run Report") ALWAYS use the "run_report" action, never "click". It retries,
   waits for the data and switches to the report tab by itself - do NOT add switch_tab after it.
   Use "switch_tab" only when a step explicitly needs another tab, and always give it "index",
   "url_contains" or "title_contains".
9. Every sentence of the step that says verify / confirm / ensure / should / check / is displayed must
   become its own verify_* action - do not skip or merge them. A step that asks for a check must
   have at least one verify_* action.
10. Do every action the step lists, including the later ones: if it says untick / uncheck a column, include
   an "uncheck"; if it says run the report again, include another "run_report" and the check after it.
11. Think about WHICH TAB the browser is on. "run_report" opens the report viewer in a NEW tab and
   switches to it. The filter boxes and the "Report Columns" panel only exist on the FILTER page
   (tab index 0). When a step says to "go back" / "return to the filter page / report columns", first
   add {{"action": "switch_tab", "index": 0}}. Run the report again afterwards to see the effect.
12. Tick / untick a report column with "check" / "uncheck" and {{"option": "<ColumnName>"}}, not "text".
   Never leave "text", "column" or "value" empty - to check a column is empty or full, say what you
   expect instead, or use verify_column_header with "absent": true.
13. "Sign in / log in to Price.intel.com with a valid user account" needs NO actions of its own: the
   browser is already signed in as the tester, so {{"action": "goto"}} covers it. Never leave a step
   manual because it mentions signing in.
14. Only when the step or expected result says the page loads (successfully / without UI errors /
   without missing content), add
   {{"action": "verify_visible", "text": "<that page's title or a button on it>"}} plus
   {{"action": "verify_not_text", "text": "Error", "partial": true}}.
15. Set "actions": [] ONLY when the step cannot be done by the signed-in tester in this browser: it needs
   a different user (e.g. one without access or approval), an email, another application, or a human
   opinion of how something looks. A step that only signs in, navigates, clicks, reads text or checks
   that a page loads MUST have actions.

## Application context
{APP_CONTEXT}

## Example step "actions"
{json.dumps(EXAMPLE_ACTIONS, indent=2)}

## Test case JSON to convert
{json.dumps(test_case, indent=2, ensure_ascii=False)}
"""


def _parse_reply(reply: str, original: dict) -> dict:
    """Turn the LLM reply into a test case dict, or raise ValueError with the reason."""
    text = reply.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        result = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"reply is not valid JSON ({e})")
    steps = result.get("steps") if isinstance(result, dict) else None
    if not isinstance(steps, list) or len(steps) != len(original.get("steps", [])):
        raise ValueError("reply does not have the same number of steps as the test case")
    for number, (step, source) in enumerate(zip(steps, original["steps"]), start=1):
        if not isinstance(step.get("actions"), list):
            raise ValueError(f"step {number} has no 'actions' array")
        missing = (wrongly_manual(source, step["actions"]) or missing_check(source, step["actions"])
                   or unchecked_expected_text(source, step["actions"])
                   or missing_interaction(source, step["actions"]))
        if missing:
            raise ValueError(f"step {number} {missing}")
    # The browser stays on whichever tab the plan left it on, across steps.
    on_viewer = False
    for number, step in enumerate(steps, start=1):
        wrong_tab, on_viewer = wrong_tab_action(step["actions"], on_viewer)
        if wrong_tab:
            raise ValueError(f"step {number} {wrong_tab}")
    return result


# Actions that only make sense on the report FILTER page (not in the report viewer tab).
_FILTER_PAGE_ONLY = {"check", "uncheck", "select", "fill"}


def wrong_tab_action(actions: list, on_viewer: bool) -> tuple[str, bool]:
    """Follow which tab the plan is on. run_report moves to the report viewer tab, so an action
    that belongs to the filter page (ticking a column, the Report Columns panel) must come
    after a switch_tab back to index 0. Returns (problem or '', tab state after these actions)."""
    for number, action in enumerate(a for a in actions if isinstance(a, dict)):
        name = action.get("action")
        if name == "run_report":
            on_viewer = True
        elif name in ("goto", "menu", "open_report"):
            on_viewer = False
        elif name == "switch_tab":
            on_viewer = action.get("index") not in (0, "0") if "index" in action else on_viewer
        elif on_viewer and (name in _FILTER_PAGE_ONLY
                            or (name == "click" and action.get("text") == "Report Columns")):
            return (f"action {number + 1} ({name}) is for the report filter page, but the plan is "
                    "still on the report viewer tab that run_report opened - add "
                    '{"action": "switch_tab", "index": 0} first to go back to the filter page'), on_viewer
    return "", on_viewer


_CHECK_WORDS = re.compile(r"\b(verify|verifies|confirm|ensure|should|check|displayed|visible)\b", re.IGNORECASE)


# Text in quotes ('...', "...", ‘...’, “...”) - an opening quote follows a space, colon or bracket,
# so the apostrophe in "doesn't" or "user's" does not start one.
_QUOTED = re.compile(r"""(?:^|(?<=[\s:(]))['"‘“]([^'"‘’“”\n]{3,}?)['"’”](?=[\s.,;:)]|$)""")
_QUOTE_MIN_WORDS = 5
_PLACEHOLDER = re.compile(r"\{[^}]*\}|<[^>]*>|\[[^\]]*\]")

# Wording that really needs a person or another account - such a step may stay manual.
_NEEDS_PERSON = re.compile(
    r"\b(does not have|doesn't have|without (special )?(access|approval|permission)|unauthori[sz]ed|"
    r"not authori[sz]ed|another user|different user|other user|second user|non-?admin|e-?mail|outlook|"
    r"manually|visually|look and feel|log ?out|sign ?out)\b", re.IGNORECASE)
_UNTICK = re.compile(r"\b(un-?check|un-?tick|deselect)\w*", re.IGNORECASE)
_RERUN = re.compile(r"\b(run (the )?report again|re-?run\w*|run again|run the report once more)\b", re.IGNORECASE)


def missing_interaction(source_step: dict, actions: list) -> str:
    """A reason if the step clearly asks for an untick or a second report run that the actions
    leave out (the model sometimes stops after the first run); else ''."""
    if not actions:
        return ""      # deliberately left manual
    text = f"{source_step.get('step', '')} {source_step.get('test_data', '')}"
    names = [a.get("action") for a in actions if isinstance(a, dict)]
    if _UNTICK.search(text) and "uncheck" not in names:
        return 'says to untick/uncheck a column but its actions have no "uncheck"'
    if _RERUN.search(text) and names.count("run_report") < 2:
        return 'says to run the report again but its actions run it only once - add another "run_report"'
    return ""


def wrongly_manual(source_step: dict, actions: list) -> str:
    """A reason if the step was left manual ("actions": []) although nothing in it needs a
    person - signing in as the tester, navigating and checking a page are all automatable; else ''."""
    if actions:
        return ""
    text = f"{source_step.get('step', '')} {source_step.get('expected_result', '')}"
    if _NEEDS_PERSON.search(text):
        return ""      # e.g. another user's access - really manual
    return ('has "actions": [] but nothing in it needs a person - the browser is already signed in '
            "as the tester, so automate it (goto, click, verify_*, screenshot)")


def _squash(text: str) -> str:
    """Spaces and case ignored, so "ProfitCenterCode" matches "Profit Center Code"."""
    return "".join(text.split()).casefold().rstrip(".")


def _action_texts(actions: list) -> list[str]:
    """Every text an action uses (click text, menu path, verify text, column...), squashed."""
    texts = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        for key, value in a.items():
            if key == "description":
                continue
            for v in (value if isinstance(value, list) else [value]):
                if isinstance(v, str) and v.strip():
                    texts.append(_squash(v))
    return texts


def unchecked_expected_text(source_step: dict, actions: list) -> str:
    """A reason if a quoted sentence in the expected result (e.g. a description it says is
    displayed) is used by none of the actions; else ''. Short quotes are usually buttons, menus
    or filter values the plan reaches another way (run_report, open_report), so only quotes of
    _QUOTE_MIN_WORDS words or more are enforced."""
    if not actions:
        return ""      # deliberately left manual
    used = _action_texts(actions)
    for quote in _QUOTED.findall(str(source_step.get("expected_result", ""))):
        if len(quote.split()) < _QUOTE_MIN_WORDS:
            continue
        # "Successfully Uploaded {no of items} item(s)": only the fixed text can be checked.
        wanted = _squash(max(_PLACEHOLDER.split(quote), key=len))
        if wanted and not any(wanted in u or (u in wanted and len(u) * 5 >= len(wanted) * 4)
                              for u in used):
            return (f"never uses the text quoted in its expected result: '{quote}' - add a "
                    "verify_text with exactly that text (or verify_not_text if it must be gone)")
    return ""


def missing_check(source_step: dict, actions: list) -> str:
    """A reason if the step asks for a check but the actions contain none; else ''."""
    if not actions:
        return ""      # deliberately left manual
    text = f"{source_step.get('step', '')} {source_step.get('expected_result', '')}"
    if _CHECK_WORDS.search(text) and not any(
            str(a.get("action", "")).startswith("verify") for a in actions if isinstance(a, dict)):
        return ("asks for a check (verify/confirm/should/displayed) but its actions contain no "
                "verify_* action")
    return ""


def _generate(test_case: dict, path: Path, prompt: str) -> dict | None:
    """Ask Foundry for the actions; return the test case with actions, or None on failure."""
    from .llm import LLMError, ask

    extra = ""
    for attempt in (1, 2):
        print(f"Asking {path.name} -> Foundry (attempt {attempt}) ...")
        try:
            reply = ask(prompt + extra)
        except LLMError as e:
            print(f"ERROR: {e}")
            return None
        try:
            result = _parse_reply(reply, test_case)
            problems = validate_actions(test_case_from_raw(result, path))
            if problems:
                raise ValueError("invalid actions:\n  " + "\n  ".join(problems))
        except ValueError as e:
            print(f"Attempt {attempt} rejected: {e}")
            extra = (f"\n\n## Your previous reply was rejected\n{e}\n"
                     "Fix this and reply again with ONLY the complete JSON.\n")
            continue
        return generated.stamp(result, test_case)
    return None


def _strip_generated(test_case: dict) -> dict:
    """A copy without old results or actions - the LLM should write them fresh."""
    test_case = json.loads(json.dumps(test_case))
    for step in test_case.get("steps", []):
        step.pop("actions", None)
        for key in ("status", "screenshots", "action_results"):
            step.pop(key, None)
    for key in ("automation_result", "ai_generated", "source_hash", "generated_at", "last_repair_at"):
        test_case.pop(key, None)
    return test_case


def generate_actions(raw: dict, path: Path) -> dict | None:
    """Public: turn a test case's plain-English steps into actions with the LLM.
    Returns the test case dict with an "actions" array on every step, or None on failure."""
    test_case = _strip_generated(raw)
    return _generate(test_case, path, build_prompt(test_case))


def save_generated(result: dict, path: Path) -> Path:
    out = path.with_suffix(".generated.json")
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def _copy_to_clipboard(text: str) -> bool:
    try:
        subprocess.run("clip", input=text.encode("utf-16"), check=True, shell=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m test_runner.prompt",
                                     description="Build an LLM prompt to generate actions.")
    parser.add_argument("path", help="Test case JSON file, or a Jira test case key / link "
                                     "(e.g. TWC5958-T1218) to read from Jira first")
    parser.add_argument("--auto", action="store_true",
                        help="Send the prompt to Foundry and save the validated reply as *.generated.json")
    args = parser.parse_args(argv)

    path = Path(args.path)
    key = key_from_url(args.path) or args.path
    if not path.exists() and is_jira_key(key):
        try:
            results = fetch_from_jira([key])
        except JiraError as e:
            print(f"Jira: {e}")
            return 1
        label, raw, error = results[0]
        if raw is None:
            print(f"{label}: could not read from Jira - {error}")
            return 1
        path = config.BASE_DIR / "test_cases" / f"{key.upper()}.json"
        path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Read from Jira: {path}")
    with open(path, encoding="utf-8") as f:
        test_case = json.load(f)

    test_case = _strip_generated(test_case)

    prompt = build_prompt(test_case)
    prompt_path = path.with_suffix(".prompt.txt")
    prompt_path.write_text(prompt, encoding="utf-8")

    print(f"Prompt saved: {prompt_path}")
    if args.auto:
        result = _generate(test_case, path, prompt)
        if result is None:
            return 1
        out = save_generated(result, path)
        print(f"Generated: {out}")
        print("Review it, then replace the original (keep a backup) and run:")
        print(f"  python -m test_runner {out} --check")
        return 0
    if _copy_to_clipboard(prompt):
        print("Prompt copied to clipboard - paste it into iGPT.")
    print()
    print("Next:")
    print(f"  1. Paste the prompt into iGPT and copy the JSON reply.")
    print(f"  2. Save the reply over {path}")
    print(f"  3. python -m test_runner {path.parent} --check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
