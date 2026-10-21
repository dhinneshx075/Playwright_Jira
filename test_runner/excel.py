"""Reading downloaded report Excel files (moved from validate.py)."""

from __future__ import annotations

import random
from pathlib import Path

import pandas as pd


def normalize_value(value) -> str:
    """Cell value as text; 2189.0 -> "2189" for integer codes."""
    if pd.isna(value):
        return ""
    text = str(value).strip()
    if text.endswith(".0"):
        try:
            number = float(text)
            if number.is_integer():
                return str(int(number))
        except ValueError:
            pass
    return text


def _loose(text: str) -> str:
    return str(text).strip().lower().replace(" ", "").replace(".", "")


class ExcelReport:
    """A report export. Report exports have title/parameter rows above the real header,
    so the header row is detected per column."""

    # The header is always near the top; exports can have 100k+ rows, so don't scan them all.
    HEADER_SEARCH_ROWS = 100

    def __init__(self, path: Path):
        self.path = Path(path)
        # Read once - large exports take a long time to parse.
        self.raw = pd.read_excel(self.path, header=None)
        self._frames: dict[int, pd.DataFrame] = {}
        self._columns: dict[str, tuple] = {}

        # An empty report exports only its title and a "No data found ..." line.
        from .config import NO_DATA_TEXTS
        top_text = " ".join(
            str(v) for v in self.raw.head(self.HEADER_SEARCH_ROWS).to_numpy().ravel() if not pd.isna(v)
        ).lower()
        self.no_data_message = next((t for t in NO_DATA_TEXTS if t.lower() in top_text), None)

    def _header_row_for(self, column: str) -> int | None:
        """Among rows that contain `column` as a cell, pick the one with the most filled cells."""
        wanted_exact = str(column).strip().lower()
        wanted_loose = _loose(column)
        best_row, best_filled = None, -1

        for loose in (False, True):
            for row_index in range(min(len(self.raw), self.HEADER_SEARCH_ROWS)):
                cells = [
                    str(v).strip() for v in self.raw.iloc[row_index]
                    if not pd.isna(v) and str(v).strip()
                ]
                if loose:
                    hit = any(_loose(c) == wanted_loose for c in cells)
                else:
                    hit = any(c.lower() == wanted_exact for c in cells)
                if hit and len(cells) > best_filled:
                    best_row, best_filled = row_index, len(cells)
            if best_row is not None:
                return best_row
        return None

    def _frame_for_header(self, header_row: int) -> pd.DataFrame:
        """Data below `header_row`, with that row as column names (built from the one read)."""
        if header_row not in self._frames:
            names, seen = [], {}
            for value in self.raw.iloc[header_row]:
                name = "" if pd.isna(value) else str(value).strip()
                count = seen.get(name, 0)
                seen[name] = count + 1
                names.append(name if count == 0 else f"{name}.{count}")
            frame = self.raw.iloc[header_row + 1:].copy()
            frame.columns = names
            self._frames[header_row] = frame.dropna(how="all")
        return self._frames[header_row]

    def column(self, column: str):
        """Return (data frame, actual column name), or (None, None) if the column is missing."""
        if column in self._columns:
            return self._columns[column]

        result = (None, None)
        header_row = self._header_row_for(column)
        if header_row is not None:
            frame = self._frame_for_header(header_row)
            for loose in (False, True):
                matches = [
                    actual for actual in frame.columns
                    if (_loose(actual) == _loose(column) if loose
                        else actual.lower() == str(column).strip().lower())
                ]
                if matches:
                    result = (frame, matches[0])
                    break

        self._columns[column] = result
        return result

    def _main_header_row(self) -> int | None:
        """The main table's header: the top row with the most filled cells."""
        best_row, best_filled = None, 0
        for row_index in range(min(len(self.raw), self.HEADER_SEARCH_ROWS)):
            filled = int(self.raw.iloc[row_index].notna().sum())
            if filled > best_filled:
                best_row, best_filled = row_index, filled
        return best_row

    def all_columns(self) -> list[str]:
        """Column names of the main table."""
        best_row = self._main_header_row()
        if best_row is None:
            return []
        return [c for c in self._frame_for_header(best_row).columns if c]

    def row_count(self) -> int:
        """Data rows in the main table (fully empty rows are not counted)."""
        return len(self.table())

    def table(self) -> pd.DataFrame:
        """The main table: its header row as column names, fully empty rows dropped."""
        best_row = self._main_header_row()
        if best_row is None:
            return pd.DataFrame()
        frame = self._frame_for_header(best_row)
        return frame[[c for c in frame.columns if c]]

    def match_column(self, label: str) -> str | None:
        """
        The Excel column for a filter label: same name ignoring spaces/dots/case first
        ("MM#" = "MM #"), then a column that starts with it ("Line Of Business" ->
        "Line Of Business Name"), then one that contains it ("Customer Name" -> "End Customer Name").
        """
        wanted = _loose(label)
        if not wanted:
            return None
        columns = self.all_columns()
        for rule in (
            lambda c: _loose(c) == wanted,
            lambda c: _loose(c).startswith(wanted),
            lambda c: wanted in _loose(c),
        ):
            matches = [c for c in columns if rule(c)]
            if matches:
                return min(matches, key=len)
        return None

    def column_stats(self, column: str) -> dict | None:
        frame, actual = self.column(column)
        if frame is None:
            return None
        values = frame[actual].map(normalize_value)
        with_data = int(values.ne("").sum())
        return {
            "column": actual,
            "total_rows": len(values),
            "with_data": with_data,
            "blank": len(values) - with_data,
        }

    def rows_not_matching(self, column: str, value: str) -> dict | None:
        """Check every filled cell equals `value` (also accepts "AM22 - Americas" for "AM22")."""
        frame, actual = self.column(column)
        if frame is None:
            return None
        wanted = str(value).strip().lower()
        values = frame[actual].map(normalize_value)
        filled = values[values.ne("")]
        lowered = filled.str.lower()
        matches = lowered.eq(wanted) | lowered.str.startswith(wanted + " ")
        others = filled[~matches]
        return {
            "column": actual,
            "total_rows": len(values),
            "matching": int(matches.sum()),
            "blank": len(values) - len(filled),
            "other_count": len(others),
            "other_examples": list(dict.fromkeys(others))[:5],
        }

    def random_value(self, column: str) -> str | None:
        frame, actual = self.column(column)
        if frame is None:
            return None
        values = [v for v in dict.fromkeys(frame[actual].map(normalize_value)) if v]
        return random.choice(values) if values else None
