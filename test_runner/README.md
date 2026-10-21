# Test Runner

Runs Jira test-case steps in the system-default Chrome or Edge browser, takes screenshots,
and writes the actual results.

## Workflow

1. Put the Jira JSON in `test_cases\`.
2. Create the tester file:
   ```
   python -m test_runner.template test_cases\
   ```
   This creates `<name>.steps.txt` next to each JSON, with the Jira step and expected result as notes.
3. The tester writes commands under each `STEP` line (see below).
4. Check for mistakes (no browser opens):
   ```
   python -m test_runner test_cases\ --check
   ```
5. Run:
   ```
   python -m test_runner test_cases\
   ```

## Steps written in Jira

Instead of a `.steps.txt`, write the commands straight into each Test Script step's
**Step** field in Jira (Type: Step-by-Step), one command per line. Test Data and
Expected Result stay as normal text and go into the evidence document.

```
python -m test_runner IAODTM-T106608            # one test case, read from Jira
python -m test_runner IAODTM-C22426            # every test case in a test cycle
python -m test_runner IAODTM-T106608 --check    # read and check only, no run
```

Several test cases at once:

```
python -m test_runner TWC5958-T1196 TWC5958-T1197                        # several keys
python -m test_runner regression.txt                                     # a list file
python -m test_runner --jira-project TWC5958 --jira-folder "/Regression/ZGM1"   # a Zephyr folder
python -m test_runner --jira-label automation                             # a label
python -m test_runner --jira-query "projectKey = \"TWC5958\" AND status = \"Approved\""
```
For direct Jira API login, set both `JIRA_USERNAME` and `JIRA_API_TOKEN` as Windows
environment variables for the account running the command. With both set, Jira is fetched
through REST Basic authentication without opening a browser for Jira sign-in. If neither is
set, the runner falls back to its saved browser profile. Do not put either credential in source code.

For the application at `https://deviaoprice.intel.com/`, set the separate Windows user
environment variables `DEVIAOPRICE_USERNAME` and `DEVIAOPRICE_PASSWORD`. These are sent as
HTTP Basic credentials only to that app origin. Set both or neither; when neither is set,
the browser continues using its saved profile. Do not put the password in source code.

The runner selects Chrome or Edge from the Windows default HTTPS browser on startup and
uses that browser's saved profile. Set `TEST_BROWSER_CHANNEL` to `chrome` or `msedge` to
override the Windows default on a particular server.

A list file is any `.txt` (not `.steps.txt`) with one Jira key, Jira link, JSON file,
`.steps.txt` or folder per line; `#` lines are notes. Files are relative to the list file.
Folder and label can be combined (both must match). The folder is matched exactly
(sub-folders are not included). A test case found twice is run once.

When Jira returns plain-English steps that need AI actions, a fresh plan is generated from
the Jira fields on every run. Previous generated JSON files are not used as the AI input;
they are retained only as timestamped backups.

- Jira is read through the selected browser with the saved profile in `config.JIRA_BROWSER_PROFILE_DIR`
  (the same one `ccr.py` uses). If it isn't signed in, sign in in the browser window that
  opens; the runner waits. Close other windows using that profile first.
- A line starting with `#` is a note. A step with only notes is `NOT_AUTOMATED` (manual).
- A step that calls another test case is shown as a note (manual).
- The same works for a saved Jira JSON: with no `.steps.txt` and no `"actions"`, the
  commands are read from each step's `"step"` text.
- Which site is tested: `DEFAULT_APP_URL` in `config.py`, unless the test starts with
  `open https://...` (then that site is used for the rest of the test), or the run is
  given `--url https://...` (that site for every test in the run).

Report regression suites (the validate.py flow, without the email), one per report:
```
python -m test_runner suites\                                  # all reports
python -m test_runner suites\ZGM1_Report_regression.steps.txt   # one report
```
A `.steps.txt` with no matching JSON (like the suites) runs on its own.

**Logins:** tests run in the saved system-default browser profile (`BROWSER_PROFILE_DIR` in `config.py`,
the same one used to read Jira). It stays signed in to Microsoft, so any site behind
Microsoft sign-in logs in by itself, whatever URL a test opens. Sign in once in that
window for any other kind of login; it is remembered. Only one run (or `ccr.py`) can use
the profile at a time. `--fresh-browser` (or `USE_SAVED_PROFILE = False`) uses a clean
browser with no saved logins instead.

**Run summary:** when a group is run (a Jira folder, label, search, test cycle, list file,
local folder, or several test cases), `test_runs\Run_Summary_<date_time>.docx` is also
created: run details, result tiles, a results table with links to each evidence document,
failures with the failing command, message and screenshot, and test cases that could not
be run. A `.json` copy is written next to it. A single test case gets no summary.

Each run creates `test_runs\<cycle>_<testcase>_<timestamp>\` with `screenshots\`,
`downloads\` (Excel exports), `result_<id>.json` (actual results filled in) and `Evidence_<id>.docx`.

If a report finishes with `No data found for the selected criteria`, the case is marked
`NO_DATA` instead of `FAIL`; its remaining checks are skipped and summaries list it separately.
This means the report could not be tested with the current data, not that its assertions passed.

## Tester file (`.steps.txt`)

```
STEP 1
open
click Price Dictionary
click Data Definition
verify column Business Name and Description = ZCNP Price
verify column Business Name and Description != YCP1 Price
screenshot zcnp_price
```

| Command | Meaning |
|---|---|
| `open` | open the application home page |
| `open https://...` | open a specific URL; later `open` / `open report` in the same test use that site too |
| `click <text>` | click a button / link / tab with that text |
| `hover <text>` | move the mouse over it (e.g. open a menu) |
| `menu <menu> > <item>` | open a menu and click an item in it, retrying if the menu doesn't open - e.g. `menu On Demand Reports > ZGM1 Report` |
| `type <field> = <value>` | type into the box labelled `<field>` |
| `select <field> = <value>` | choose `<value>` in the dropdown labelled `<field>` |
| `show options <field>` | list the choices in a dropdown (selects nothing) - handy when writing `select` lines |
| `tick <text>` / `untick <text>` | tick or untick the checkbox for `<text>` - e.g. `untick ItemMarketName` in the Report Columns panel |
| `choose <option>` | pick a radio button - e.g. `choose Item Market Name Level` |
| `press <key>` | press Enter, Escape, Tab ... |
| `upload <file>` | put a file in the page's upload box (like Browse... and picking it). Files are in `test_data\` or give a full path; `--check` warns if the file is missing |
| `upload <button> = <file>` | same, using the upload box of that button - e.g. `upload Browse = PVP.xlsx` |
| `wait <seconds>` | pause |
| `wait for <text>` | wait until `<text>` appears |
| `verify <text>` | PASS if `<text>` is shown |
| `verify not <text>` | PASS if `<text>` is NOT shown |
| `verify column <column>` | PASS if a table on the page has that column heading (spaces/case ignored: `LineOfBusinessName` = `Line Of Business Name`) |
| `verify no column <column>` | PASS if no table on the page has that heading (`verify not column` works too) |
| `verify column <column> = <text>` | PASS if a table column contains `<text>` |
| `verify column <column> != <text>` | PASS if a table column does NOT contain `<text>` |
| `verify url <text>` | PASS if the page address contains `<text>` |
| `switch tab <number>` | go to another tab (first tab = 1) |
| `close tab` | close the current tab |
| `screenshot [name]` | take a screenshot |
| **Reports** | |
| `open report <name>` | home page -> finds the report in the top menus (`REPORT_MENUS` in config.py) -> its filter page |
| `open report <menu> > <name>` | same, naming the menu - e.g. `open report WW Price Ops > MM DQ` |
| `run report` | click Run Report (3 tries, 45s each), wait for data, switch to the report tab. `click Run Report` and `click Run Report again` do the same |
| `download excel` | export the open report to Excel |
| `verify excel column <column>` | PASS if the Excel has that column with data |
| `verify no excel column <column>` | PASS if the Excel does NOT have that column (e.g. after unticking it in Report Columns) |
| `verify excel column <column> = <value>` | PASS if every row in that column is `<value>` (use after a filtered run) |
| `test filter <filter>` | random value from the Excel column of the same name -> apply on a clean filter page -> run report -> screenshot selection + result |
| `test filter <filter> = <excel column>` | same, when the Excel column name differs |
| `test all filters` | every filter on the report page - found automatically and matched to Excel columns by name; mappings in `reports.py` take priority; filters with no Excel column are SKIPPED |
| **Database** (read-only, one SELECT per line) | |
| `use database <name>` | run the following db lines on this database (default `DB_DEFAULT_DATABASE` in config.py) |
| `show db <select>` | log the first rows of a query, checks nothing - handy when writing `verify db` lines |
| `verify db <select>` | PASS if the query returns at least one row |
| `verify db <select> expect <value>` | PASS if the first value returned is `<value>` (numbers compared as numbers) |
| `verify no db <select>` | PASS if the query returns no rows |
| `verify excel column <column> in db <select>` | PASS if every value in the Excel column is in the query's first column |
| `verify excel rows = <number>` | PASS if the downloaded Excel has that many data rows |
| `verify excel rows = db <select>` | PASS if the Excel row count equals the number the query returns (e.g. a `COUNT(*)`) |
| `check file <file>` | check a local Excel (e.g. the file just uploaded) instead of a report download - the excel commands then use that file |
| `verify excel rows in db <select> match on <key>[, <key>]` | PASS if every Excel row is in the query result: matched on the key columns, all other same-named columns equal. Name the query's columns like the Excel's (`... AS [Purchase Price]`) |
| `try <any command>` | if it fails, record a warning and carry on |

Rules:
- Lines starting with `#` are notes. Blank lines are ignored. Commands are not case-sensitive.
- Leave a STEP empty if it must be tested manually - it is reported as `NOT_AUTOMATED`.
- A click that opens a new tab switches to it automatically.
- A screenshot is taken automatically at the end of every step and on every failure.
- If a step fails, later steps are marked `BLOCKED`.

## Advanced: actions in the JSON

Instead of a `.steps.txt`, a step in the JSON can have an `"actions"` list - either the same
one-line commands as strings, or full action objects for cases the simple commands can't express
(CSS/XPath targets, `nth` match, `within` a container, `optional`, `timeout_ms`).
A `.steps.txt` file, if present, takes priority. See `python -m test_runner --list-actions`.

```json
"actions": [
  "open",
  { "action": "click", "css": "#btnViewReport", "opens_new_tab": true },
  { "action": "verify_text", "text": "Profit Center Code", "within": "#reportTable", "timeout_ms": 60000 }
]
```

## Code layout

| File | Responsibility |
|---|---|
| `config.py` | URLs, timeouts, output folder, screenshot settings, report viewer selectors |
| `reports.py` | per-report data markers and filter lists (was `REPORTS` in validate.py) |
| `report_viewer.py` | run report with retry, wait for data, Excel export, reset filter page |
| `excel.py` | read report exports: header row detection, column checks, random values |
| `db.py` | read-only SQL Server queries (Windows login): SELECT-only guard, always rolled back |
| `widgets.py` | PrimeNG multiselect dropdowns used on report filter pages |
| `simple_steps.py` | tester commands -> actions; `.steps.txt` reading and template |
| `template.py` | `python -m test_runner.template` |
| `models.py` | loads the JSON (+ `.steps.txt`) into `TestCase` / `Step` |
| `jira.py` | reads test cases / test cycles from Jira (Zephyr Scale REST API) through the selected browser |
| `browser.py` | launches the system-default browser, tracks the current tab |
| `waits.py` | waits for spinners / page load |
| `locators.py` | turns a target into an element |
| `actions.py` | every action; add new ones with `@action("name")` |
| `executor.py` | runs steps, records results and screenshots |
| `report.py` | results JSON + Word evidence document |
| `prompt.py` | optional: builds a prompt to paste into a chat LLM |
| `__main__.py` | command line |

To add a new tester command: add the action in `actions.py`, then one `if` in
`simple_steps._parse` and one line in `CHEAT_SHEET`.
