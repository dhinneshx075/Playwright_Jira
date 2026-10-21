"""
Per-report settings (moved from validate.py).

- markers:      text that appears once the report viewer has loaded data
- filter_tests: filter -> Excel column mappings used by "test all filters" (taking priority
                over automatic matching). Filters found on the page are tested either way.
"""

REPORTS = {
    "ZGM1 Report": {
        "markers": [
            "Price Product Hierarchy",
            "Material Id",
            "Price Distribution Type",
            "Profit Center Code",
            "Sales Region",
        ],
        "filter_tests": [
            {"label": "Profit Center Code", "excel_column": "Profit Center Code"},
            {"label": "Level 5 Code", "excel_column": "Level 5 Code"},
            {"label": "Sales Region", "excel_column": "Sales Region"},
            {"label": "Sales Org.", "excel_column": "Sales Org."},
        ],
    },

    "ZCEP Report": {
        "markers": [
            "Profit Center Code",
            "Level 5 Code",
            "Line Of Business",
            "Sales Region",
            "Customer Name",
        ],
        "filter_tests": [
            {"label": "Line Of Business", "excel_column": "Line Of Business Name"},
            {"label": "Profit Center Code", "excel_column": "Profit Center Code"},
            {"label": "MM#", "excel_column": "MM #"},
            {"label": "Item Market Name", "excel_column": "Item Market Name"},
            {"label": "Level 5 Code", "excel_column": "Level 5 Code"},
            {"label": "Sales Region", "excel_column": "Sales Region"},
            {"label": "Sales Org.", "excel_column": "Sales Org."},
            {"label": "End Customer", "excel_column": "End Customer Number"},
            {"label": "Customer Sold To", "excel_column": "Customer Sold To"},
            {"label": "Customer Name", "excel_column": "End Customer Name"},
            {"label": "Price Distribution Type", "excel_column": "Price Distribution Type"},
        ],
    },

    "ZCEP Report (MM Level)": {
        "markers": [
            "Profit Center Code",
            "Level 5 Code",
            "Line Of Business",
            "Sales Region",
            "Customer Name",
        ],
        "filter_tests": [
            {"label": "Line Of Business", "excel_column": "Line Of Business Name"},
            {"label": "Profit Center Code", "excel_column": "Profit Center Code"},
            {"label": "MM#", "excel_column": "MM #"},
            {"label": "Item Market Name", "excel_column": "Item Market Name"},
            {"label": "Level 5 Code", "excel_column": "Level 5 Code"},
            {"label": "Sales Region", "excel_column": "Sales Region"},
            {"label": "Sales Org.", "excel_column": "Sales Org."},
            {"label": "End Customer", "excel_column": "End Customer Number"},
            {"label": "Customer Sold To", "excel_column": "Customer Sold To"},
            {"label": "Customer Name", "excel_column": "End Customer Name"},
            {"label": "Price Distribution Type", "excel_column": "Price Distribution Type"},
        ],
    },

    "ZMPR - ZSDP Report": {
        "markers": [
            "Profit Center Code",
            "Level 5 Code",
            "Sales Region",
            "Customer Name",
        ],
        "filter_tests": [
            {"label": "Line Of Business", "excel_column": "Line Of Business Name"},
            {"label": "Profit Center Code", "excel_column": "Profit Center Code"},
            {"label": "MM#", "excel_column": "MM#"},
            {"label": "Item Market Name", "excel_column": "Item Market Name"},
            {"label": "Level5Code", "excel_column": "Level 5 Code"},
            {"label": "Sales Region", "excel_column": "Sales Region"},
            {"label": "Sales Org.", "excel_column": "Sales Org."},
            {"label": "Price Condition Types", "excel_column": "Price Condition Types"},
            {"label": "Price Distribution Type", "excel_column": "Price Distribution Type"},
        ],
    },

    "Transfer Price Report": {
        "markers": [
            "Profit Center Code",
            "Level 5 Code",
            "Sales Region",
            "Customer Name",
        ],
        "filter_tests": [
            {"label": "Line Of Business", "excel_column": "Line Of Business"},
            {"label": "Profit Center Code", "excel_column": "Profit Center Code"},
            {"label": "Material Id", "excel_column": "Material Id"},
            {"label": "Sales Org", "excel_column": "Sales Org"},
        ],
    },
}


# A report does NOT need to be listed here: "test all filters" finds the filters on the page
# and matches them to Excel columns by name, and "run report" waits for the Export button.
# Add a report only to set data markers, or to map a filter whose Excel column name is
# different, e.g. {"label": "End Customer", "excel_column": "End Customer Number"}.


def find_report(name: str | None) -> dict | None:
    if not name:
        return None
    wanted = name.strip().lower()
    for report_name, settings in REPORTS.items():
        if report_name.lower() == wanted:
            return settings
    return None
