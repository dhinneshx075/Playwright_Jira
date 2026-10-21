"""
Plain-English commands for testers, one per line. Each line is turned into an
action dict that actions.py understands.

Testers write them in a "<test case>.steps.txt" file next to the Jira JSON:

    STEP 1
    open
    click Price Dictionary
    verify column Business Name and Description = ZCNP Price
    screenshot

Lines starting with # are notes and are ignored.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

CHEAT_SHEET = """\
open                                   open the application home page
open https://...                       open a specific URL - later "open" / "open report"
                                       then use that site too
click <text>                           click a button / link / tab with that text
hover <text>                           move the mouse over it (e.g. open a menu)
menu <menu> > <item>                   open a menu and click an item in it (retries if the menu
                                       doesn't open), e.g. menu On Demand Reports > ZGM1 Report
type <field> = <value>                 type into the box labelled <field>
select <field> = <value>               choose <value> in the dropdown labelled <field>
show options <field>                   list the choices in a dropdown (selects nothing)
tick <text> / untick <text>            tick or untick the checkbox for <text>, e.g. in the
                                       Report Columns panel: untick ItemMarketName
choose <option>                        pick a radio button, e.g. choose Item Market Name Level
press <key>                            press a key: Enter, Escape, Tab
upload <file>                          put a file in the page's upload box (like Browse...);
                                       files are in the test_data folder, or give the full path
upload <button> = <file>               same, using that button, e.g. upload Browse = PVP.xlsx
wait <seconds>                         pause, e.g. wait 5
wait for <text>                        wait until <text> appears
verify <text>                          PASS if <text> is shown on the page
verify not <text>                      PASS if <text> is NOT shown
verify column <column>                 PASS if a table on the page has that column heading
                                       (spaces and case ignored: LineOfBusinessName = Line Of Business Name)
verify no column <column>              PASS if no table on the page has that column heading
verify column <column> = <text>        PASS if a table column contains <text>
verify column <column> != <text>       PASS if a table column does NOT contain <text>
verify url <text>                      PASS if the page address contains <text>
switch tab <number>                    go to another browser tab (first tab = 1)
close tab                              close the current tab
screenshot                             take a screenshot (optional name: screenshot home_page)

REPORTS
open report <report name>              home page -> finds the report in the top menus -> its filter page
open report <menu> > <report name>    same, naming the menu, e.g. open report WW Price Ops > MM DQ
run report                             click Run Report (3 tries), wait for data, go to the report tab
                                       ("click Run Report" / "click Run Report again" do the same)
download excel                         export the open report to Excel
verify excel column <column>           PASS if the Excel has that column with data in it
verify excel column <column> = <value> PASS if every row in that column is <value>
verify no excel column <column>        PASS if the Excel does NOT have that column
test filter <filter>                   pick a random value from the Excel column of the same name,
                                       apply it, run the report, screenshot selection + result
test filter <filter> = <excel column>  same, when the Excel column name differs from the filter
test all filters                       test every filter on the report page (found automatically)

DATABASE (read-only: one SELECT per line)
use database <name>                    run the next db lines on this database, e.g. use database pricebook
show db <select>                       log the first rows of a query (checks nothing)
verify db <select>                     PASS if the query returns at least one row
verify db <select> expect <value>      PASS if the first value returned is <value>, e.g.
                                       verify db SELECT COUNT(*) FROM dbo.ZGM1Prices expect 139763
verify no db <select>                  PASS if the query returns no rows
verify excel column <column> in db <select>
                                       PASS if every value in the Excel column is in the query result
verify excel rows = <number>           PASS if the downloaded Excel has that many data rows
verify excel rows = db <select>        PASS if the Excel row count equals the number the query returns
check file <file>                      check a local Excel (e.g. the file just uploaded) instead of a
                                       report download - the excel commands then use that file
verify excel rows in db <select> match on <key column>[, <key column>]
                                       PASS if every Excel row is in the query result: rows matched on
                                       the key columns, all other same-named columns must be equal

try <any command>                      if it fails, record a warning and carry on"""


def _split_pair(text: str, separator: str = "="):
    if separator not in text:
        return None, None
    left, right = text.split(separator, 1)
    return left.strip(), right.strip()


def parse_line(line: str) -> dict:
    """Turn one tester command into an action dict. Returns {"error": ...} if not understood."""
    text = line.strip()
    optional = text.lower().startswith("try ")
    command = text[4:].strip() if optional else text
    action = _parse(command, command.lower())
    if optional:
        action["optional"] = True
    action.setdefault("description", text)
    return action


def _parse(text: str, lower: str) -> dict:
    def after(prefix: str) -> str:
        return text[len(prefix):].strip()

    # Database - checked first because the SQL itself may contain "=" or "verify" words.
    if lower.startswith("use database "):
        return {"action": "use_database", "database": after("use database ")}
    if lower.startswith("show db "):
        return {"action": "show_db", "sql": after("show db ")}
    if lower.startswith("verify no db "):
        return {"action": "verify_db", "sql": after("verify no db "), "absent": True}
    if lower.startswith("verify db "):
        rest = after("verify db ")
        expects = list(re.finditer(r"\s+expect\s+", rest, re.IGNORECASE))
        if not expects:
            return {"action": "verify_db", "sql": rest}
        last = expects[-1]
        sql, value = rest[:last.start()].strip(), rest[last.end():].strip()
        if not sql or not value:
            return {"error": "use: verify db <select>  or  verify db <select> expect <value>"}
        return {"action": "verify_db", "sql": sql, "value": value}
    if lower.startswith("check file "):
        return {"action": "check_file", "file": after("check file ")}
    if lower.startswith("verify excel rows in db "):
        rest = after("verify excel rows in db ")
        matches = list(re.finditer(r"\s+match\s+on\s+", rest, re.IGNORECASE))
        if not matches:
            return {"error": "use: verify excel rows in db <select> match on <key column>[, <key column>]"}
        sql, keys = rest[:matches[-1].start()].strip(), rest[matches[-1].end():]
        keys = [k.strip() for k in keys.split(",") if k.strip()]
        if not sql or not keys:
            return {"error": "use: verify excel rows in db <select> match on <key column>[, <key column>]"}
        return {"action": "verify_excel_rows_in_db", "sql": sql, "keys": keys}
    if lower.startswith("verify excel rows"):
        rest = after("verify excel rows").lstrip("= ").strip()
        if rest.lower().startswith("db "):
            return {"action": "verify_excel_rows", "sql": rest[3:].strip()}
        if not rest.isdigit():
            return {"error": "use: verify excel rows = <number>  or  verify excel rows = db <select>"}
        return {"action": "verify_excel_rows", "value": rest}
    if lower.startswith("verify excel column ") and " in db " in lower:
        split_at = lower.index(" in db ")
        column = text[len("verify excel column "):split_at].strip()
        sql = text[split_at + len(" in db "):].strip()
        if not column or not sql:
            return {"error": "use: verify excel column <column> in db <select>"}
        return {"action": "verify_excel_column_in_db", "column": column, "sql": sql}

    # Reports - checked first because "open report ..." also starts with "open ".
    if lower.startswith("open report "):
        return {"action": "open_report", "name": after("open report ")}
    # "click Run Report (again)" is what testers naturally write - treat it as "run report",
    # which also waits for the data and switches to the report tab.
    if lower == "run report" or re.fullmatch(
            r"click\s+(on\s+)?(the\s+)?run\s+report(\s+button)?(\s+again)?", lower):
        return {"action": "run_report"}
    if lower == "download excel":
        return {"action": "download_excel"}
    if lower.startswith("verify no excel column "):
        return {"action": "verify_excel_column", "column": after("verify no excel column "),
                "absent": True}
    if lower.startswith("verify excel column "):
        column, value = _split_pair(after("verify excel column "))
        if column is None:
            return {"action": "verify_excel_column", "column": after("verify excel column ")}
        if not column or not value:
            return {"error": "use: verify excel column <column>  or  <column> = <value>"}
        return {"action": "verify_excel_column", "column": column, "value": value}
    if lower == "test all filters":
        return {"action": "test_all_filters"}
    if lower.startswith("test filter "):
        label, column = _split_pair(after("test filter "))
        if label is None:
            return {"action": "filter_test", "filter": after("test filter ")}
        if not label or not column:
            return {"error": "use: test filter <filter>  or  test filter <filter> = <excel column>"}
        return {"action": "filter_test", "filter": label, "column": column}

    if lower == "open":
        return {"action": "goto"}
    if lower.startswith("open "):
        return {"action": "goto", "url": after("open ")}

    if lower.startswith("menu "):
        path = [part.strip() for part in after("menu ").split(">")]
        if len(path) < 2 or not all(path):
            return {"error": "use: menu <menu> > <item>, e.g. menu On Demand Reports > ZGM1 Report"}
        return {"action": "menu", "path": path}

    if lower.startswith("click "):
        return {"action": "click", "text": after("click ")}
    if lower.startswith("hover "):
        return {"action": "hover", "text": after("hover ")}

    if lower.startswith("show options "):
        return {"action": "show_options", "field": after("show options ")}

    for prefix, name in (("type ", "fill"), ("select ", "select")):
        if lower.startswith(prefix):
            field, value = _split_pair(after(prefix))
            if not field or value is None:
                return {"error": f"use: {prefix}<field> = <value>"}
            return {"action": name, "field": field, "value": value}

    if lower.startswith("untick "):
        return {"action": "uncheck", "option": after("untick ")}
    if lower.startswith("choose "):
        return {"action": "check", "option": after("choose ")}
    if lower.startswith("tick "):
        return {"action": "check", "option": after("tick ")}

    if lower.startswith("upload "):
        target, file = _split_pair(after("upload "))
        if target is None:
            return {"action": "upload", "file": after("upload ")}
        if not target or not file:
            return {"error": "use: upload <file>  or  upload <button> = <file>"}
        return {"action": "upload", "text": target, "file": file}

    if lower.startswith("press "):
        return {"action": "press", "key": after("press ")}

    if lower.startswith("wait for "):
        return {"action": "wait_for", "text": after("wait for ")}
    if lower.startswith("wait "):
        seconds = after("wait ").split()[0]
        try:
            float(seconds)
        except ValueError:
            return {"error": "use: wait <seconds>, e.g. wait 5"}
        return {"action": "wait", "seconds": seconds}

    for prefix in ("verify no column ", "verify not column "):
        if lower.startswith(prefix):
            return {"action": "verify_column_header", "column": after(prefix), "absent": True}
    if lower.startswith("verify column "):
        rest = after("verify column ")
        if "=" not in rest:
            return {"action": "verify_column_header", "column": rest}
        column, value = _split_pair(rest, "!=")
        if column and value:
            return {"action": "verify_column_not_contains", "column": column, "text": value}
        column, value = _split_pair(rest, "=")
        if column and value:
            return {"action": "verify_column_contains", "column": column, "text": value}
        return {"error": "use: verify column <column>,  verify column <column> = <text>  or  != <text>"}
    if lower.startswith("verify url "):
        return {"action": "verify_url_contains", "text": after("verify url ")}
    if lower.startswith("verify not "):
        return {"action": "verify_not_text", "text": after("verify not ")}
    if lower.startswith("verify "):
        return {"action": "verify_text", "text": after("verify ")}

    if lower.startswith("switch tab "):
        number = after("switch tab ")
        if not number.isdigit() or int(number) < 1:
            return {"error": "use: switch tab <number>, first tab = 1"}
        return {"action": "switch_tab", "index": int(number) - 1}
    if lower == "close tab":
        return {"action": "close_tab"}

    if lower == "screenshot":
        return {"action": "screenshot"}
    if lower.startswith("screenshot "):
        return {"action": "screenshot", "name": after("screenshot ")}

    return {"error": "command not recognised (command list: python -m test_runner --commands)"}


_HTML_TAG = re.compile(r"</?(br|p|div|span|li|ul|ol|b|strong|i|em|u|a|font|h\d|table|tr|td)\b[^>]*>",
                       re.IGNORECASE)
_LINE_BREAK_TAG = re.compile(r"<\s*(br|/p|/div|/li|/h\d|/tr)\b[^>]*>", re.IGNORECASE)


def jira_text(text) -> str:
    """Jira rich text (<p>, <br>, &gt; ...) as plain text, one line per paragraph / line break."""
    text = str(text or "")
    if _HTML_TAG.search(text):
        text = _LINE_BREAK_TAG.sub("\n", text)
        text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    return "\n".join(line.strip() for line in text.splitlines()).strip()


_STEP_HEADING = re.compile(r"step\s*(\d+)\s*(?:[:.)-]\s*(.*))?", re.IGNORECASE)


def step_heading(line: str):
    """
    (step number, command on the same line or "") for a "STEP 2" / "Step 2:" line -
    also "Step 2: verify Sales Org." with the first command after the colon. Else None.
    """
    match = _STEP_HEADING.fullmatch(line.strip())
    if not match:
        return None
    return int(match.group(1)), (match.group(2) or "").strip()


def split_step_headings(raw_steps: list[dict]) -> list[dict]:
    """
    A Jira "Step" field may hold several "STEP n" blocks (e.g. a whole .steps.txt pasted
    into one step). Each block becomes its own step; steps are then numbered in order.
    """
    result, split = [], False
    for raw_step in raw_steps:
        lines = jira_text(raw_step.get("step", "")).splitlines()
        if "actions" in raw_step or not any(step_heading(l) for l in lines):
            result.append(raw_step)
            continue

        blocks = [[]]
        for line in lines:
            heading = step_heading(line)
            if heading:
                if any(l and not l.startswith("#") for l in blocks[-1]):
                    blocks.append([])
                if heading[1]:
                    blocks[-1].append(heading[1])
            else:
                blocks[-1].append(line)   # lines before the first heading join its block

        jira_sequence = raw_step.get("sequence", len(result) + 1)
        for part, block in enumerate(blocks, start=1):
            new_step = dict(raw_step, step="\n".join(block).strip(), jira_step=jira_sequence)
            if part > 1:
                new_step["test_data"] = ""
                new_step["expected_result"] = f"(part {part} of Jira step {jira_sequence})"
            result.append(new_step)
        split = split or len(blocks) > 1

    if split:
        result = [dict(s, sequence=n) for n, s in enumerate(result, start=1)]
    return result


def parse_command_block(text: str) -> list[dict]:
    """Commands written straight into a Jira step's "Step" field, one per line (# = note)."""
    actions = []
    for number, line in enumerate(jira_text(text).splitlines(), start=1):
        if not line or line.startswith("#"):
            continue
        action = parse_line(line)
        action["step_line"] = number
        actions.append(action)
    return actions


STEPS_SUFFIX = ".steps.txt"


def steps_file_for(json_path: Path) -> Path:
    return json_path.with_name(json_path.stem + STEPS_SUFFIX)


def json_file_for(steps_path: Path) -> Path:
    return steps_path.with_name(steps_path.name[: -len(STEPS_SUFFIX)] + ".json")


def raw_from_steps_file(steps_path: Path) -> dict:
    """A minimal test case for a .steps.txt that has no Jira JSON (e.g. a regression suite)."""
    parsed = parse_steps_file(steps_path)
    steps = []
    for sequence in sorted(n for n in parsed if n):
        commands = [a.get("description", "") for a in parsed[sequence]]
        steps.append({
            "sequence": sequence,
            "step": "; ".join(commands),
            "test_data": "",
            "expected_result": "All commands pass.",
            "actual_result": "",
        })
    return {
        "cycle_key": steps_path.name[: -len(STEPS_SUFFIX)],
        "assigned_test_case_id": "suite",
        "steps": steps,
    }


def parse_steps_file(path: Path) -> dict[int, list[dict]]:
    """Read a .steps.txt file into {step number: [actions]}."""
    steps: dict[int, list[dict]] = {}
    current = None

    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        heading = step_heading(line)
        if heading:
            current = heading[0]
            steps.setdefault(current, [])
            if not heading[1]:
                continue
            line = heading[1]   # "STEP 2: verify ..." - the command after the colon

        action = parse_line(line)
        action["line"] = line_number
        if current is None:
            action = {"error": "command is above the first STEP line",
                      "description": line, "line": line_number}
            current_key = 0
        else:
            current_key = current
        steps.setdefault(current_key, []).append(action)

    return steps


def build_template(test_case_raw: dict) -> str:
    """A .steps.txt skeleton with each Jira step's text as notes."""
    lines = [
        f"# Test case: {test_case_raw.get('cycle_key', '')} / "
        f"{test_case_raw.get('assigned_test_case_id', '')}",
        "#",
        "# Under each STEP line, write one command per line. Lines starting with # are notes.",
        "# Leave a step empty if it has to be tested manually.",
        "#",
        "# COMMANDS",
    ]
    lines += [f"#   {row}" for row in CHEAT_SHEET.splitlines()]
    lines.append("")

    for index, step in enumerate(test_case_raw.get("steps", []), start=1):
        sequence = step.get("sequence", index)
        lines.append("#" + "-" * 79)
        lines.append(f"# Step:     {step.get('step', '')}")
        if step.get("test_data") and step.get("test_data") != "N/A":
            lines.append(f"# Data:     {step['test_data']}")
        lines.append(f"# Expected: {step.get('expected_result', '')}")
        lines.append(f"STEP {sequence}")
        lines.append("")
        lines.append("")

    return "\n".join(lines)
