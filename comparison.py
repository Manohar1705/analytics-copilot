"""comparison.py - change between two snapshots (plain Python, no Streamlit).

compare_snapshots(current, previous) gives, for every KPI: value, absolute change,
percent change and direction. It also warns when the periods have different lengths.
Percent KPIs (unit "%") change in percentage POINTS: 95% -> 86% = -9 pts.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FLAT_LIMIT = 1e-9  # changes smaller than this count as "flat"


@dataclass
class Comparison:
    ok: bool = False
    rows: list[dict] = field(default_factory=list)  # one entry per KPI
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---- Small helpers -----------------------------------------------------------
def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _delta(current, previous, unit: str) -> dict:
    """value change between two numbers: absolute, percent and direction."""
    if not (_is_number(current) and _is_number(previous)):
        return {"change": None, "pct_change": None, "direction": "n/a"}
    change = round(current - previous, 4)
    if unit == "%":
        pct = None  # a percent KPI is compared in points, not as a percent of a percent
    else:
        pct = round(change / previous * 100, 2) if previous else None
    direction = "flat" if abs(change) < FLAT_LIMIT else ("up" if change > 0 else "down")
    return {"change": change, "pct_change": pct, "direction": direction}


def _category_rows(current: dict | None, previous: dict | None, unit: str) -> list[dict]:
    """Per-category change for a breakdown KPI (e.g. engagements by account type)."""
    current, previous = current or {}, previous or {}
    labels = list(current) + [k for k in previous if k not in current]
    rows = []
    for label in labels:
        cur, prev = current.get(label), previous.get(label)
        rows.append({"label": label, "current": cur, "previous": prev, **_delta(cur, prev, unit)})
    return rows

def partial_note(snapshot: dict) -> str | None:
    """Text when the file's data ends before the period end (ISO dates compare correctly as text)."""
    covered, end = snapshot.get("data_covered_to"), snapshot.get("period_end")
    if covered and end and covered < end:
        return f"{snapshot['source']} data for {snapshot['period_start']} to {end} only goes up to {covered}."
    return None

# ---- Main comparison ---------------------------------------------------------------
def compare_snapshots(current: dict | None, previous: dict | None) -> Comparison:
    """Compare two snapshots of the SAME source. Never raises."""
    out = Comparison()
    if not current or not previous:
        out.errors.append("Pick two saved periods to compare.")
        return out
    if current.get("source") != previous.get("source"):
        out.errors.append("Cannot compare different sources.")
        return out
    if current.get("id") and current.get("id") == previous.get("id"):
        out.errors.append("Pick two different periods to compare.")
        return out

    # Warnings the user should see before trusting the change.
    days_now, days_before = current.get("period_days"), previous.get("period_days")
    if days_now and days_before and days_now != days_before:
        out.warnings.append(
            f"The periods have different lengths ({days_now} days vs {days_before} days), "
            "so totals are not directly comparable."
        )
    for snap in (current, previous):
        note = partial_note(snap)
        if note:
            out.warnings.append(note + " The comparison may be misleading.")
    if current.get("kpi_version") != previous.get("kpi_version"):
        out.warnings.append("The two snapshots were calculated with different KPI definitions.")

    cur_kpis, prev_kpis = current.get("kpis", {}), previous.get("kpis", {})
    for key, cur in cur_kpis.items():
        prev = prev_kpis.get(key)
        unit = cur.get("unit", "")
        row = {
            "key": key,
            "name": cur.get("name", key),
            "unit": unit,
            "kind": cur.get("kind", "single"),
            "current": cur.get("value"),
            "previous": prev.get("value") if prev else None,
            "change_unit": "pts" if unit == "%" else unit,
        }
        if row["kind"] == "breakdown":
            row["categories"] = _category_rows(row["current"], row["previous"], unit)
            row.update({"change": None, "pct_change": None, "direction": "n/a"})
        else:
            row.update(_delta(row["current"], row["previous"], unit))
        out.rows.append(row)

    out.ok = True
    return out


# ---- Trend across saved periods ----------------------------------------------------
def trend_series(snapshots: list[dict], kpi_key: str) -> list[dict]:
    """Points for a trend line: one per period, oldest first. Pass list_snapshots(latest_only=True)."""
    points = []
    for snap in sorted(snapshots, key=lambda s: (s["period_start"], s["period_end"])):
        kpi = snap.get("kpis", {}).get(kpi_key)
        if kpi and _is_number(kpi.get("value")):
            points.append({
                "label": f"{snap['period_start']} to {snap['period_end']}",
                "period_start": snap["period_start"],
                "value": kpi["value"],
            })
    return points


# ---- Text for the dashboard cards --------------------------------------------------
def format_value(value, unit: str) -> str:
    """Readable text for a KPI value."""
    if value is None:
        return "n/a"
    if not _is_number(value):
        return str(value)
    if unit == "%":
        return f"{value:,.2f}%"
    if unit == "count":
        return f"{value:,.0f}"
    if unit == "currency":
        return f"{value:,.2f}"
    return f"{value:,.2f} {unit}".strip()


def format_delta(row: dict) -> str | None:
    """Text for st.metric's delta, e.g. '+300 (+4.4%)' or '-9.00 pts'. None if no comparison."""
    change = row.get("change")
    if change is None:
        return None
    unit = row.get("change_unit", "")
    number = f"{change:+,.0f}" if unit == "count" else f"{change:+,.2f}"
    suffix = f" {unit}" if unit and unit not in ("count", "currency") else ""
    pct = row.get("pct_change")
    return f"{number}{suffix}" + (f" ({pct:+.1f}%)" if pct is not None else "")