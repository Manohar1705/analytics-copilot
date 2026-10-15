"""kpi_engine.py - calculate KPIs and chart data from a recognised file (plain Python, no Streamlit).

Input : a FileResult from sources.inspect_file() and the period the user confirmed.
Output: a CalcResult holding a snapshot DICT of calculated results only (no raw rows).
        snapshot_store.py adds saved_at and the version number when it saves it.

All numbers come from pandas and the fixed formulas in kpi_config.py. No LLM.
A bad file or period returns errors, so nothing empty is ever saved.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import sources
from kpi_config import KPI_VERSION, split_by_columns

MAX_CATEGORIES = 12  # charts show the top N categories, the rest become "Other"


@dataclass
class CalcResult:
    ok: bool = False
    snapshot: dict | None = None  # JSON-safe dict, ready for snapshot_store.save_snapshot()
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---- Small helpers -----------------------------------------------------------
def _clean(value):
    """Make a value JSON-safe: NaN/inf -> None, numbers -> plain Python."""
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isnan(number) or math.isinf(number):
        return None
    return int(number) if number.is_integer() and not isinstance(value, float) else number


def _chart_from_breakdown(key: str, name: str, unit: str, breakdown: dict) -> dict | None:
    """Bar + share data for one breakdown KPI. Extra categories are grouped as 'Other'."""
    items = [(k, v) for k, v in breakdown.items() if v is not None]
    items.sort(key=lambda kv: kv[1], reverse=True)
    if not items:
        return None
    if len(items) > MAX_CATEGORIES:
        other = sum(v for _, v in items[MAX_CATEGORIES:])
        items = items[:MAX_CATEGORIES] + [("Other", other)]
    total = sum(v for _, v in items)
    return {
        "kpi_key": key,
        "title": name,
        "unit": unit,
        "labels": [k for k, _ in items],
        "values": [v for _, v in items],
        "shares": [round(v / total * 100, 2) if total else 0.0 for _, v in items],
    }


# ---- Main entry point ------------------------------------------------------------
def calculate_snapshot(
    file_result: sources.FileResult,
    period_start: date | None = None,
    period_end: date | None = None,
) -> CalcResult:
    """Calculate every KPI that can run for this file. Never raises."""
    out = CalcResult()
    try:
        _calculate(out, file_result, period_start, period_end)
    except Exception as exc:  # noqa: BLE001 - the UI must get a message, not a crash
        out.ok = False
        out.snapshot = None
        out.errors.append(f"Calculation failed: {exc}")
    return out


def _calculate(out: CalcResult, file_result: sources.FileResult, start: date | None, end: date | None) -> None:
    if not file_result.ok or not file_result.source:
        out.errors.extend(file_result.errors or ["The file was not recognised."])
        return

    # The user-confirmed period wins; otherwise use what was detected.
    start = start or file_result.period_start
    end = end or file_result.period_end
    problems = sources.validate_period(start, end)
    if problems:
        out.errors.extend(problems)
        return

    # Keep only rows inside the period (matters for files that hold many months).
    tables = sources.apply_period(file_result.source, file_result.tables, start, end)
    rows_by_table = {name: int(len(df)) for name, df in tables.items()}
    if sum(rows_by_table.values()) == 0:
        hint = ""
        if file_result.available_range:
            hint = f" The file covers {file_result.available_range[0]} to {file_result.available_range[1]}."
        out.errors.append(f"No rows fall between {start} and {end}.{hint}")
        return

    # Which KPIs can run with the columns we have?
    runnable, skipped = split_by_columns(file_result.source, sources.columns_by_table(tables))

    kpis: dict[str, dict] = {}
    charts: list[dict] = []
    skipped_info = [{"key": k.key, "name": k.name, "reason": "required column or sheet missing"} for k in skipped]

    for kpi in runnable:
        df = tables.get(kpi.table)
        if df is None or df.empty:
            skipped_info.append({"key": kpi.key, "name": kpi.name, "reason": f"no rows in '{kpi.table}'"})
            continue
        try:
            value = _clean(kpi.formula(df))
        except Exception as exc:  # noqa: BLE001 - one bad KPI must not stop the others
            skipped_info.append({"key": kpi.key, "name": kpi.name, "reason": f"calculation error: {exc}"})
            continue
        kpis[kpi.key] = {
            "name": kpi.name,
            "unit": kpi.unit,
            "kind": kpi.kind,
            "chart_type": kpi.chart_type,
            "table": kpi.table,
            "value": value,
        }
        if kpi.kind == "breakdown" and isinstance(value, dict):
            chart = _chart_from_breakdown(kpi.key, kpi.name, kpi.unit, value)
            if chart:
                charts.append(chart)

    if not kpis:
        out.errors.append("No KPI could be calculated from this file.")
        return
    for item in skipped_info:
        out.warnings.append(f"KPI '{item['name']}' skipped: {item['reason']}.")
    out.warnings.extend(file_result.warnings)

    out.snapshot = {
        "source": file_result.source,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "period_days": (end - start).days + 1,
        "kpis": kpis,
        "charts": charts,
        "skipped_kpis": skipped_info,
        "rows_by_table": rows_by_table,
        "source_files": [file_result.file_name],
        "sheets_used": dict(file_result.table_sheets),
        "kpi_version": KPI_VERSION,
    }
    out.ok = True