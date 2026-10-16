"""sources.py - recognise a file's source, detect its period, validate and map columns.

Plain Python, no Streamlit. It never raises to the caller: problems come back
as messages in FileResult.errors / FileResult.warnings.

How it works (per uploaded workbook):
  1. Probe the first rows of EVERY sheet to find the header row (title rows are skipped).
  2. Compare each sheet's column names with the signatures in SOURCES below.
  3. Load only the matching sheets, and only the columns we need (huge unused sheets are never loaded).
  4. Rename real columns to the COMMON names that kpi_config.py expects.
  5. Detect the reporting period (the user can correct it later).

Hardcoded on purpose: the rules below (signatures, column mapping, period rule).
Never hardcoded: the data, the period, the month.
"""

from __future__ import annotations

import calendar
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

# ---- The rules (edit here when a real export changes) -------------------------
# signature : columns a sheet must have to count as this table
# columns   : real column name -> common name used by kpi_config.py
# prefer    : if several sheets match, use the one with this name (lower-case)
# dates     : common columns to convert to dates
# total_row : a "Total" row inside the sheet. It is kept as its own one-row table (into) so headline numbers match the file
SOURCES: dict[str, dict] = {
    "ServiceNow": {
        "period_rule": "month_of_latest_date",  # file spans many months -> default to the latest month
        "tables": {
            "main": {
                "signature": ("Number", "SLA definition", "Stage", "Business duration", "Created"),
                "columns": {
                    "Stage": "state",
                    "SLA definition": "sla_definition",
                    "Business duration": "business_duration",
                    "Created": "created",
                },
                "prefer": ("raw data",),
                "dates": ("created",),
            },
            "requests": {  # the "Sheet1" tab: one row per task with its request type
                "signature": ("Number", "Short Description", "Business duration", "Created"),
                "columns": {
                    "Short Description": "request_type",
                    "Business duration": "business_duration",
                    "Created": "created",
                },
                "prefer": (),
                "dates": ("created",),
            },
            "request_detail": {  # the "July" tab: priority, region, country
                "signature": ("Number", "Priority", "Region", "Country", "Opened"),
                "columns": {
                    "Priority": "priority",
                    "Region": "region",
                    "Country": "country",
                    "Opened": "opened",
                },
                "prefer": (),
                "dates": ("opened",),
            },
        },
    },   
    "Sprinklr": {
        "period_rule": "title_text",  # "TIME_INTERVAL: From:08-01-26 To:08-31-26"
        "tables": {
            "organic": {
                "signature": ("Outbound Post", "Account Type", "Total Engagements (SUM)"),
                "columns": {
                    "Account Type": "account_type",
                    "Total Engagements (SUM)": "engagements",
                    "Post Reach (SUM)": "reach",
                    "Status": "status",
                },
                "prefer": (),
                "dates": (),
            },
            "paid": {
                "signature": ("Ad Variant", "Paid Initiative Name", "Spent in USD (SUM)"),
                "columns": {
                    "Spent in USD (SUM)": "spend",
                    "Engagements (SUM)": "engagements",
                    "Impressions (SUM)": "impressions",
                },
                "prefer": (),
                "dates": (),
            },
        },
    },
    "Traackr": {
        "period_rule": "date_range",  # min/max of campaign Start Date
        "tables": {
            "brands": {
                "signature": ("Keyword Group", "Posts", "Engagements", "Potential Reach"),
                "columns": {
                    "Keyword Group": "brand",
                    "Posts": "posts",
                    "Engagements": "engagements",
                    "Est Impressions": "est_impressions",
                    "Video Views": "video_views",
                    "Potential Reach": "potential_reach",
                    "Brand Vitality Score (VIT)": "vit_score",
                    "Visibilty": "visibility",  # sic: the export spells it this way
                    "Visibility": "visibility",
                    "Impact": "impact",
                    "Trust": "trust",
                },
                "prefer": ("diageo brands",),  # "all markets" has the same columns - assumption, confirm
                "dates": (),
                "total_row": {"column": "brand", "labels": ("total", "grand total"), "into": "brands_total"},
            },
            "all_markets": {  # the "all markets" tab: same columns as "diageo brands", wider scope
                "signature": ("Keyword Group", "Posts", "Engagements", "Potential Reach"),
                "columns": {
                    "Keyword Group": "brand",
                    "Posts": "posts",
                    "Engagements": "engagements",
                    "Est Impressions": "est_impressions",
                    "Video Views": "video_views",
                    "Potential Reach": "potential_reach",
                },
                "prefer": ("all markets",),
                "dates": (),
                "total_row": {"column": "brand", "labels": ("total", "grand total"), "into": "all_markets_total"},
            },
            "campaigns": {
                "signature": ("Start Date", "End Date", "Creators", "Total Spend"),
                "columns": {
                    "Start Date": "start_date",
                    "End Date": "end_date",
                    "Total Spend": "total_spend",
                    "Brand": "brand",
                },
                "prefer": (),
                "dates": ("start_date", "end_date"),
            },
        },
    },
}
# Tables made from a sheet's Total row (one row each). They are not data rows, so they are not counted as rows read.
TOTAL_TABLES = {rule["total_row"]["into"] for spec in SOURCES.values() for rule in spec["tables"].values() if "total_row" in rule}
# Files that cover many periods: keep only rows inside the chosen period.
DATE_FILTERS: dict[str, tuple[str, str]] = {"ServiceNow": ("main", "created")}
# Other tables of the same file that are also cut to the chosen period: source -> {table: date column}.
EXTRA_DATE_FILTERS: dict[str, dict[str, str]] = {"ServiceNow": {"requests": "created", "request_detail": "opened"}}
# Common column names that must hold numbers. sources.py only warns about bad values, it never changes the data.
NUMBER_COLUMNS = (
    "business_duration", "engagements", "reach", "spend", "impressions",
    "posts", "est_impressions", "video_views", "potential_reach", "total_spend",
    "vit_score", "visibility", "impact", "trust",
)
PROBE_ROWS = 15
MAX_SHEET_ROWS = 100_000  # a tab can claim a million blank rows (slow); real data ends well before this
TITLE_PERIOD = re.compile(r"From:\s*(\d{2}-\d{2}-\d{2,4})\s*To:\s*(\d{2}-\d{2}-\d{2,4})", re.IGNORECASE)


@dataclass
class FileResult:
    """Everything the dashboard needs to show after an upload."""

    file_name: str
    ok: bool = False
    source: str | None = None
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)  # table name -> mapped data
    table_sheets: dict[str, str] = field(default_factory=dict)     # table name -> sheet it came from
    period_start: date | None = None
    period_end: date | None = None
    period_note: str = ""                                          # how the period was found
    available_range: tuple[date, date] | None = None               # for multi-period files
    row_count: int = 0                                             # rows inside the detected period
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped_sheets: list[str] = field(default_factory=list)


# ---- Small helpers -----------------------------------------------------------
def _norm(name: object) -> str:
    """Lower-case, trimmed, single spaces - so header matching ignores formatting."""
    return re.sub(r"\s+", " ", str(name)).strip().lower()


def _find_header(raw: pd.DataFrame) -> int | None:
    """Row index of the header: the first row that is mostly filled and all text."""
    width = raw.shape[1]
    for i in range(len(raw)):
        values = raw.iloc[i].dropna()
        if len(values) >= 3 and len(values) >= 0.6 * width and all(isinstance(v, str) for v in values):
            return i
    return None


def _parse_title_date(text: str) -> date | None:
    for fmt in ("%m-%d-%y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _month_bounds(day: date) -> tuple[date, date]:
    last = calendar.monthrange(day.year, day.month)[1]
    return day.replace(day=1), day.replace(day=last)


def _expected_text() -> str:
    parts = []
    for source, spec in SOURCES.items():
        columns = sorted({c for t in spec["tables"].values() for c in t["signature"]})
        parts.append(f"{', '.join(columns)} for {source}")
    return "; ".join(parts)


def columns_by_table(tables: dict[str, pd.DataFrame]) -> dict[str, list[str]]:
    """Shape that kpi_config.split_by_columns() expects."""
    return {name: list(df.columns) for name, df in tables.items()}


def validate_period(start: date | None, end: date | None) -> list[str]:
    """Problems with a period the user typed or accepted. Empty list = fine."""
    if start is None or end is None:
        return ["Period is missing: enter a start and an end date."]
    if end < start:
        return ["Period end is before the period start."]
    return []


def _cut(df: pd.DataFrame, column: str, start: date, end: date) -> pd.DataFrame:
    """Rows whose date column falls inside the period (end day included)."""
    stamps = pd.to_datetime(df[column], errors="coerce")
    inside = (stamps >= pd.Timestamp(start)) & (stamps < pd.Timestamp(end) + timedelta(days=1))
    return df[inside].reset_index(drop=True)


def apply_period(source: str, tables: dict[str, pd.DataFrame], start: date, end: date) -> dict[str, pd.DataFrame]:
    """Keep only rows inside the period (only for sources that hold many periods)."""
    rule = DATE_FILTERS.get(source)
    if not rule or rule[0] not in tables:
        return tables
    table, column = rule
    cut = {**tables, table: _cut(tables[table], column, start, end)}
    for name, col in EXTRA_DATE_FILTERS.get(source, {}).items():
        if name in cut and col in cut[name].columns:
            cut[name] = _cut(cut[name], col, start, end)
    return cut
def check_numbers(df: pd.DataFrame, sheet: str, names: dict[str, str]) -> list[str]:
    """Warnings for text or negative values in number columns. names: common name -> real column name."""
    notes = []
    for column in NUMBER_COLUMNS:
        if column not in df.columns:
            continue
        raw = df[column]
        if pd.api.types.is_numeric_dtype(raw):
            numbers, text_count = raw, 0
        else:  # same cleaning as kpi_config._num: "$52.63" and "1,200" are fine
            numbers = pd.to_numeric(raw.astype(str).str.replace(r"[^0-9.\-]", "", regex=True), errors="coerce")
            text_count = int((raw.notna() & numbers.isna()).sum())
        negative_count = int((numbers < 0).sum())
        label = names.get(column, column)
        if text_count:
            notes.append(f"Sheet '{sheet}': {text_count} value(s) in '{label}' are not numbers. They are left out of the totals.")
        if negative_count:
            notes.append(f"Sheet '{sheet}': {negative_count} negative value(s) in '{label}'. Please check the file.")
    return notes

# ---- Period detection ----------------------------------------------------------
def _detect_period(result: FileResult, titles: list[str]) -> None:
    rule = SOURCES[result.source]["period_rule"]

    if rule == "month_of_latest_date":
        table, column = DATE_FILTERS[result.source]
        dates = result.tables.get(table, pd.DataFrame()).get(column, pd.Series(dtype="datetime64[ns]")).dropna()
        if dates.empty:
            result.period_note = f"No valid dates in '{column}'. Enter the period."
            return
        result.available_range = (dates.min().date(), dates.max().date())
        result.period_start, result.period_end = _month_bounds(dates.max().date())
        result.period_note = f"Latest month in the file (data covers {result.available_range[0]} to {result.available_range[1]})."

    elif rule == "title_text":
        found = []
        for title in titles:
            match = TITLE_PERIOD.search(title)
            if match:
                start, end = _parse_title_date(match.group(1)), _parse_title_date(match.group(2))
                if start and end:
                    found.append((start, end))
        if not found:
            result.period_note = "No TIME_INTERVAL line found in the sheet titles. Enter the period."
            return
        if len(set(found)) > 1:
            result.warnings.append("Sheets report different periods. Using the first one - please check.")
        result.period_start, result.period_end = found[0]
        result.period_note = "Read from the sheet title (TIME_INTERVAL)."

    elif rule == "date_range":
        dates = result.tables.get("campaigns", pd.DataFrame()).get("start_date", pd.Series(dtype="datetime64[ns]")).dropna()
        if dates.empty:
            result.period_note = "No campaign Start Dates found. Enter the period."
            return
        result.period_start, result.period_end = dates.min().date(), dates.max().date()
        result.period_note = "Earliest to latest campaign Start Date."


# ---- Main entry point ------------------------------------------------------------
def inspect_file(file_name: str, data: bytes) -> FileResult:
    """Detect, validate and map one uploaded workbook. Never raises."""
    result = FileResult(file_name=file_name)
    try:
        _inspect(result, data)
    except Exception as exc:  # noqa: BLE001 - the UI must get a message, not a crash
        result.ok = False
        result.errors.append(f"Could not read '{file_name}': {exc}")
    return result


def _inspect(result: FileResult, data: bytes) -> None:
    if not result.file_name.lower().endswith((".xlsx", ".xlsm", ".xls")):
        result.errors.append("Unsupported file type: upload an Excel file (.xlsx or .xls).")
        return
    if not data:
        result.errors.append("The file is empty.")
        return

    book = pd.ExcelFile(io.BytesIO(data))

    # 1. Probe every sheet: header row, column names, title text above the header.
    probes: dict[str, tuple[int, set[str], str]] = {}
    for sheet in book.sheet_names:
        raw = book.parse(sheet, header=None, nrows=PROBE_ROWS)
        header = _find_header(raw)
        if header is None:
            continue
        columns = {_norm(c) for c in raw.iloc[header].dropna()}
        title = " ".join(str(v) for r in range(header) for v in raw.iloc[r].dropna())
        probes[sheet] = (header, columns, title)

    # 2. Which source(s) do the sheets match?
    matches: dict[str, dict[str, list[str]]] = {}
    for source, spec in SOURCES.items():
        found: dict[str, list[str]] = {}
        for table, rule in spec["tables"].items():
            signature = {_norm(c) for c in rule["signature"]}
            sheets = [s for s, (_, cols, _) in probes.items() if signature <= cols]
            if sheets:
                found[table] = sheets
        if found:
            matches[source] = found

    if not matches:
        result.errors.append(f"Unrecognised file: expected columns {_expected_text()}.")
        return
    best = max(len(found) for found in matches.values())
    winners = [s for s, found in matches.items() if len(found) == best]
    if len(winners) > 1:
        result.errors.append(f"The file matches more than one source ({', '.join(winners)}). Cannot choose.")
        return
    result.source = winners[0]
    spec = SOURCES[result.source]

    # 3. Load only the matching sheets and the columns we need.
    titles: list[str] = []
    for table, sheets in matches[result.source].items():
        rule = spec["tables"][table]
        chosen = next((s for s in sheets if _norm(s) in rule["prefer"]), sheets[0])
        if len(sheets) > 1:
            result.warnings.append(f"Several sheets match '{table}' ({', '.join(sheets)}). Using '{chosen}'.")
        header = probes[chosen][0]
        lookup = {_norm(k): v for k, v in rule["columns"].items()}

        df = book.parse(chosen, header=header, usecols=lambda c: _norm(c) in lookup, nrows=MAX_SHEET_ROWS)
        if len(df) >= MAX_SHEET_ROWS:
            result.warnings.append(f"Sheet '{chosen}' has more than {MAX_SHEET_ROWS:,} rows. Only the first {MAX_SHEET_ROWS:,} were read.")
        df = df.rename(columns=lambda c: lookup.get(_norm(c), c))
        df = df[[c for c in df.columns if c in lookup.values()]].dropna(how="all")
        total_rule = rule.get("total_row")
        if total_rule and total_rule["column"] in df.columns:
            is_total = df[total_rule["column"]].map(_norm).isin(total_rule["labels"])
            totals, df = df[is_total], df[~is_total]
            if totals.empty:  # no Total row in the file: add the rows up instead
                sums = df.drop(columns=[total_rule["column"]]).apply(pd.to_numeric, errors="coerce").sum()
                totals = pd.DataFrame([{total_rule["column"]: "Total (sum of rows)", **sums.to_dict()}])
                result.warnings.append(f"'{chosen}' has no Total row, so totals are the sum of its rows. Posts shared by several brands may be counted more than once.")
            else:
                result.warnings.append(f"Headline totals come from the Total row in '{chosen}'. The brand rows are used only for the brand chart.")
            result.tables[total_rule["into"]] = totals.head(1).reset_index(drop=True)
            result.table_sheets[total_rule["into"]] = chosen
        for column in rule["dates"]:
            if column in df.columns:
                df[column] = pd.to_datetime(df[column], errors="coerce")

        if df.empty:
            result.warnings.append(f"Sheet '{chosen}' has no data rows and was skipped.")
            continue
        missing = [common for common in rule["columns"].values() if common not in df.columns]
        if missing:
            result.warnings.append(f"Sheet '{chosen}' is missing {', '.join(missing)}: KPIs that need them will be skipped.")
        result.warnings.extend(check_numbers(df, chosen, {v: k for k, v in rule["columns"].items()}))
        result.tables[table] = df.reset_index(drop=True)
        result.table_sheets[table] = chosen
        titles.append(probes[chosen][2])

    if not result.tables:
        result.errors.append("The file was recognised but contains no data.")
        return

    result.skipped_sheets = [s for s in book.sheet_names if s not in result.table_sheets.values()]

    # 4. Period and row count.
    _detect_period(result, titles)
    if result.period_start and result.period_end:
        inside = apply_period(result.source, result.tables, result.period_start, result.period_end)
        main_table = DATE_FILTERS.get(result.source, (None,))[0]  # many-period files: count the main table only
        result.row_count = len(inside[main_table]) if main_table in inside else sum(len(df) for name, df in inside.items() if name not in TOTAL_TABLES)
    else:
        result.row_count = sum(len(df) for name, df in result.tables.items() if name not in TOTAL_TABLES)
        result.warnings.append("Period could not be detected. Enter it before saving.")
    result.ok = True