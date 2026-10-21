The application is https://consiaoprice.intel.com/ (Price.Intel.Com).
- Home page: title "Price Quick Search", search box, buttons "Request Access", "Training",
  "Site Feedback", "Price Dictionary".
- The "Price Dictionary" page shows collapsed sections such as "Data Definition" and
  "Report Descriptions". Their rows are NOT on the page until the section is clicked open:
  report rows and their descriptions (e.g. "APAC Disti Finance Report") are under
  "Report Descriptions"; field / price definitions are under "Data Definition".
- Top menu (click to open): "On Demand Reports", "WW Price Ops", "Price Publications",
  "System Administration", "EPM Hierarchy - Product Search".
- "On Demand Reports" menu contains report links such as "ZCEP Report", "ZCEP Report (MM Level)",
  "ZMPR - ZSDP Report", "ZGM1 Report", "Transfer Price Report", "Distributor", "Price History Report",
  and a "Misc Reports" category (e.g. "Distributor Price Move Snapshot").
- "WW Price Ops" menu contains links such as "Non IA SOX Report", "Price DQ", "MM DQ",
  "ZMPR - ZSDP_All Products", "Zero Price", "IA Price Overlap".
- A report page ("filter page") has filter boxes in the middle, a "Report Columns" panel on the left,
  and the buttons "Reset Filters" and "Run Report" at the bottom. The run button is css "#btnViewReport";
  results open in a NEW tab (the report viewer).
- The "Report Columns" panel is COLLAPSED by default. Before verifying, ticking or unticking a column
  checkbox, click "Report Columns" to expand it, then target the column with {"option": "<ColumnName>"}
  (e.g. "ProfitCenterCode"). Column names in the panel have no spaces ("ProfitCenterCode"); filter
  labels and report column headers have spaces ("Profit Center Code").

## Patterns that come up again and again

The browser is already signed in as the tester, so a "sign in" sentence is just the goto.
"Sign in with a valid user account, go to the Price Dictionary page, confirm the page loads
without UI errors" is:
  {"action": "goto"}
  {"action": "click", "text": "Price Dictionary"}
  {"action": "verify_visible", "text": "Price Dictionary"}
  {"action": "verify_not_text", "text": "Error", "partial": true}
  {"action": "screenshot", "name": "price_dictionary_loaded"}

Check a report's row and description on the Price Dictionary page (open the section first):
  {"action": "goto"}
  {"action": "click", "text": "Price Dictionary"}
  {"action": "click", "text": "Report Descriptions"}
  {"action": "verify_visible", "text": "APAC Disti Finance Report"}
  {"action": "verify_text", "text": "<the description from the expected result>"}

Open a report from the menu (a menu path may have two or three levels):
  {"action": "menu", "path": ["On Demand Reports", "Misc Reports", "Distributor Price Move Snapshot"]}
  {"action": "menu", "path": ["On Demand Reports", "ZGM1 Report"]}

Check a filter was renamed (the old name must be gone):
  {"action": "verify_visible", "text": "Profit Center Code"}
  {"action": "verify_not_text", "text": "Operation Code"}

Check a column in the Report Columns panel (expand it first):
  {"action": "click", "text": "Report Columns"}
  {"action": "verify_visible", "option": "ProfitCenterCode"}
  {"action": "verify_not_text", "text": "OperationCode"}

Untick / tick a column before running:
  {"action": "uncheck", "option": "ProfitCenterCode"}
  {"action": "check", "option": "ProfitCenterCode"}

Run the report and check its result tab (run_report switches to the new tab by itself):
  {"action": "run_report"}
  {"action": "verify_column_header", "column": "Profit Center Code"}
  {"action": "verify_not_text", "text": "Operation Code"}

Choose a filter value and type into a filter:
  {"action": "select", "field": "Sales Region", "value": "AM22"}
  {"action": "fill", "field": "MM", "value": "927500"}

Go back to the filter page after running (run_report leaves you on the report viewer tab, index 1;
the filter page is index 0), change a column, and run again:
  {"action": "run_report"}
  {"action": "verify_column_header", "column": "Profit Center Code"}
  {"action": "switch_tab", "index": 0}
  {"action": "click", "text": "Report Columns"}
  {"action": "uncheck", "option": "ProfitCenterCode"}
  {"action": "run_report"}
  {"action": "verify_column_header", "column": "Profit Center Code", "absent": true}
