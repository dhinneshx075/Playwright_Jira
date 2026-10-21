"""
Read-only queries against the pricing SQL Server (Windows authentication).

The test account may have write rights, so every query is guarded twice:
  1. check_read_only() accepts only a single SELECT / WITH statement with no
     data-changing keywords (also run by --check, before any browser opens);
  2. the query runs inside a transaction that is always rolled back.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

from . import config


class QueryNotAllowed(Exception):
    pass


_WRITE_WORDS = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|exec|execute|grant|revoke"
    r"|deny|backup|restore|dbcc|use|into|openrowset|opendatasource|sp_\w+|xp_\w+)\b",
    re.IGNORECASE,
)


def _without_literals(sql: str) -> str:
    """The SQL with 'string literals', [bracketed names] and comments blanked out."""
    sql = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"'(?:[^']|'')*'|\[[^\]]*\]", "''", sql)


def check_read_only(sql: str) -> None:
    """Raise QueryNotAllowed unless `sql` is one read-only SELECT."""
    code = _without_literals(sql).strip().rstrip(";").strip()
    if not re.match(r"(select|with)\b", code, re.IGNORECASE):
        raise QueryNotAllowed("only SELECT queries are allowed")
    if ";" in code:
        raise QueryNotAllowed("only one statement per command (remove the ';')")
    word = _WRITE_WORDS.search(code)
    if word:
        raise QueryNotAllowed(f"'{word.group(0)}' is not allowed - queries must be read-only")


def resolve(name: str | None) -> tuple[str, str]:
    """(server, database) for a "use database" name: a DB_CONNECTIONS entry, else a
    database on DB_SERVER. No name = DB_DEFAULT_DATABASE."""
    name = name or config.DB_DEFAULT_DATABASE
    for alias, connection in config.DB_CONNECTIONS.items():
        if alias.lower() == name.strip().lower():
            return connection["server"], connection["database"]
    return config.DB_SERVER, name


def query(sql: str, database: str | None = None) -> tuple[list[str], list[tuple]]:
    """Run a read-only query. Returns (column names, rows)."""
    check_read_only(sql)
    import pyodbc  # only needed when a test actually uses the database

    server, database = resolve(database)
    connection = pyodbc.connect(
        f"Driver={{{config.DB_DRIVER}}};Server={server};"
        f"Database={database};Trusted_Connection=yes;",
        autocommit=False,
        timeout=config.DB_LOGIN_TIMEOUT_S,
    )
    try:
        connection.timeout = config.DB_QUERY_TIMEOUT_S
        cursor = connection.cursor()
        cursor.execute(sql)
        if cursor.description is None:
            return [], []
        columns = [d[0] for d in cursor.description]
        rows = [tuple(r) for r in cursor.fetchmany(config.DB_MAX_ROWS + 1)]
        if len(rows) > config.DB_MAX_ROWS:
            raise QueryNotAllowed(
                f"query returned more than {config.DB_MAX_ROWS} rows - add a WHERE or DISTINCT"
            )
        return columns, rows
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


def to_text(value) -> str:
    """A database value as text comparable with Excel/page text: 2189.00 -> "2189"."""
    if value is None:
        return ""
    if isinstance(value, (Decimal, float)):
        if value == int(value):
            return str(int(value))
        return format(value, "f").rstrip("0") if isinstance(value, Decimal) else str(value)
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if value.time() == dt.time() else value.isoformat(sep=" ")
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value).strip()


def same_value(a: str, b: str) -> bool:
    """Equal as numbers if both are numbers, else equal ignoring case and outer spaces."""
    try:
        return float(a) == float(b)
    except ValueError:
        return a.strip().lower() == b.strip().lower()


def format_rows(columns: list[str], rows: list[tuple], limit: int = 20) -> str:
    lines = [" | ".join(columns)]
    lines += [" | ".join(to_text(v) for v in row) for row in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"... {len(rows) - limit} more row(s)")
    return "\n".join(lines)
