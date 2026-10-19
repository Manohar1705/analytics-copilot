"""report_deck.py - builds the deck dictionary for pptx_writer.build_deck() from SAVED KPIs (no Streamlit).

Order: ServiceNow, then Sprinklr, then Traackr. A source with no saved data is skipped, and the
number of slides follows the saved KPIs:
  - headline numbers: one slide per 8 single-number KPIs
  - each breakdown KPI: one chart slide (bar, grouped bar when a previous period is selected, or donut)
  - one closing slide with the recommendations that were flagged
Every slide gets insights and a recommendation. They come from insights.py (already checked);
a slide that has none of its own gets the plain fact sentences of that KPI, never new text.
The template and layout are untouched: this file only prepares what pptx_writer draws.
"""

from __future__ import annotations

from typing import Any

import comparison
import insights
import snapshot_store
SOURCE_ORDER = ["ServiceNow", "Sprinklr", "Traackr"]
KPIS_PER_SLIDE = 8        # pptx_writer.MAX_KPIS
MAX_BARS = 12
MAX_DONUT_SLICES = 8      # same chart rule as pages/dashboard.py
SKEW_LIMIT = 0.75
NO_ACTION = "No action needed from the current numbers."
NO_INSIGHT = "The figures for this period are shown above."
MAX_PER_SLIDE = 4         # insights shown in one slide box
MAX_RECS_PER_SLIDE = 2    # recommendations shown in one slide box
MAX_CLOSING = 6           # recommendations on one closing slide (pptx_writer shows 6)
TREND_KPI = {"ServiceNow": "sn_total_tasks", "Sprinklr": "sp_total_engagements", "Traackr": "tr_total_engagements"}
TABLE_KPI = {"ServiceNow": "sn_tasks_by_request_type", "Sprinklr": "sp_engagements_by_account", "Traackr": "tr_engagements_by_brand"}
TABLE_ROWS = 10
def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _chart_kind(chart_type: str, unit: str, values: dict) -> str:
    """"donut" or "bar" - the same rule as the dashboard (chosen from the data shape only)."""
    positives = {k: v for k, v in (values or {}).items() if _is_number(v) and v > 0}
    if unit != "count" or not 2 <= len(positives) <= MAX_DONUT_SLICES:
        return "bar"
    return "bar" if max(positives.values()) / sum(positives.values()) > SKEW_LIMIT else "donut"


def _plain_rows(snapshot: dict) -> list[dict]:
    return [{"key": k, "name": v.get("name", k), "unit": v.get("unit", ""), "kind": v.get("kind", "single"),
             "current": v.get("value"), "previous": None, "change": None, "pct_change": None, "change_unit": v.get("unit", "")}
            for k, v in snapshot["kpis"].items()]


def _lines(keys: set[str], result: "insights.InsightResult", facts: list["insights.Fact"]) -> tuple[list[str], list[str]]:
    """Insights and recommendations for the KPIs in `keys`: the shown (checked) lines first, then plain facts until the box is full."""
    shown = [l for l in result.insights if l.kpi in keys]
    found = [l.text for l in shown]
    used = {l.fact_id for l in shown}
    for fact in facts:
        if len(found) >= MAX_PER_SLIDE:
            break
        if fact.kpi in keys and fact.id not in used:
            found.append(fact.text)
    shown_rec = [l for l in result.recommendations if l.kpi in keys]
    advice = [l.text for l in shown_rec]
    used_rec = {l.fact_id for l in shown_rec}
    for fact in facts:
        if len(advice) >= MAX_RECS_PER_SLIDE:
            break
        if fact.kpi in keys and fact.advice and fact.id not in used_rec:
            advice.append(fact.advice)
    return found or [NO_INSIGHT], advice or [NO_ACTION]


def _source_slides(source: str, current: dict, previous: dict | None, result: "insights.InsightResult") -> list[dict]:
    facts, _ = insights.build_facts(current, previous)
    comp = comparison.compare_snapshots(current, previous) if previous else None
    rows = comp.rows if comp and comp.ok else _plain_rows(current)
    comparing = bool(comp and comp.ok)
    files = ", ".join(current.get("source_files", []))
    slides: list[dict] = []

    # ---- headline numbers ---------------------------------------------------
    singles = [r for r in rows if r["kind"] != "breakdown"]
    for number, start in enumerate(range(0, len(singles), KPIS_PER_SLIDE), start=1):
        chunk = singles[start:start + KPIS_PER_SLIDE]
        keys = {r["key"] for r in chunk} | ({"_steady"} if number == 1 else set())
        ins, rec = _lines(keys, result, facts)
        if number == 1 and comparison.partial_note(current):
            ins = [comparison.partial_note(current)] + ins  # warn that the month is only partly covered
        suffix = f" ({number})" if len(singles) > KPIS_PER_SLIDE else ""
        slides.append({
            "type": "kpis", "title": f"{source}: headline numbers{suffix}", "source": files,
            "kpis": [{"label": r["name"], "value": comparison.format_value(r["current"], r["unit"]),
                      "note": (comparison.format_delta(r) or "") if comparing else ""} for r in chunk],
            "insights": ins, "recommendation": rec,
        })

    # ---- one chart per breakdown KPI ---------------------------------------
    for row in (r for r in rows if r["kind"] == "breakdown"):
        values = {k: v for k, v in (row["current"] or {}).items() if _is_number(v)}
        if not values:
            continue
        chart_type = current["kpis"].get(row["key"], {}).get("chart_type", "bar")
        kind = _chart_kind(chart_type, row["unit"], values)
        fmt = "#,##0" if row["unit"] == "count" else "#,##0.00"
        now_label = current["period_start"]
        if kind == "donut":
            top = sorted(((k, v) for k, v in values.items() if v > 0), key=lambda kv: -kv[1])
            categories, series = [k for k, _ in top], [{"name": now_label, "values": [v for _, v in top]}]
        elif comparing and row.get("categories"):
            cats = sorted(row["categories"], key=lambda c: -(c["current"] if _is_number(c["current"]) else -1))[:MAX_BARS]
            categories = [c["label"] for c in cats]
            series = [{"name": now_label, "values": [c["current"] for c in cats]},
                      {"name": previous["period_start"], "values": [c["previous"] for c in cats]}]
        else:
            top = sorted(values.items(), key=lambda kv: -kv[1])[:MAX_BARS]
            categories, series = [k for k, _ in top], [{"name": now_label, "values": [v for _, v in top]}]
        ins, rec = _lines({row["key"]}, result, facts)
        slides.append({
            "type": kind, "title": f"{source}: {row['name']}", "chart_title": row["name"], "source": files,
            "categories": categories, "series": series, "number_format": fmt,
            "insights": ins, "recommendation": rec,
        })
    return slides


def _closing_items(slides: list[dict]) -> list[dict]:
    """Recommendations of the slides of one source, as items for a closing slide (heading = the slide's subject)."""
    items, seen = [], set()
    for rank in range(MAX_RECS_PER_SLIDE):  # first the best recommendation of every slide, then the second ones
        for slide in slides:
            recs = [t for t in slide["recommendation"] if t != NO_ACTION]
            if rank < len(recs) and recs[rank] not in seen:
                seen.add(recs[rank])
                heading = slide.get("chart_title") or slide["title"].split(": ", 1)[-1].capitalize()
                items.append({"heading": heading, "text": recs[rank]})
    return items[:MAX_CLOSING]
def _table_slide(source: str, current: dict, previous: dict | None) -> dict | None:
    """Top rows of one breakdown KPI as a table (plus previous period and change when one is selected)."""
    key = TABLE_KPI.get(source)
    kpi = current["kpis"].get(key) if key else None
    values = {k: v for k, v in ((kpi or {}).get("value") or {}).items() if _is_number(v)}
    if len(values) < 2:
        return None
    name, unit = kpi.get("name", key), kpi.get("unit", "")
    top = sorted(values.items(), key=lambda kv: -kv[1])[:TABLE_ROWS]
    total = sum(values.values())
    before = ((previous or {}).get("kpis", {}).get(key) or {}).get("value") or {}
    comparing = bool(previous) and any(_is_number(v) for v in before.values())
    places = 0 if unit == "count" else 2
    rows = []
    for label, value in top:
        if comparing:
            old = before.get(label)
            change = value - old if _is_number(old) else None
            pct = change / old * 100 if change is not None and old else None
            rows.append([label, comparison.format_value(value, unit),
                         comparison.format_value(old, unit) if _is_number(old) else "n/a",
                         f"{change:+,.{places}f}" if change is not None else "n/a",
                         f"{pct:+.1f}%" if pct is not None else "n/a"])
        else:
            rows.append([label, comparison.format_value(value, unit), f"{value / total * 100:.1f}%" if total else ""])
    shown = sum(v for _, v in top)
    ins = [f"Showing the top {len(top)} of {len(values)} values in {name}."]
    if total:
        ins.append(f"These {len(top)} hold {comparison.format_value(shown, unit)} of {comparison.format_value(total, unit)} ({shown / total * 100:.1f}%).")
    return {
        "type": "table", "title": f"{source}: {name} (table)", "source": ", ".join(current.get("source_files", [])),
        "columns": ["Name", "Current", "Previous", "Change", "Change %"] if comparing else ["Name", "Value", "Share"],
        "rows": rows, "insights": ins, "recommendation": [NO_ACTION],
    }
def _trend_slide(source: str, current: dict) -> dict | None:
    """One line chart of a headline KPI across the saved periods up to the selected one (None if fewer than 2)."""
    key = TREND_KPI.get(source)
    kpi = current["kpis"].get(key) if key else None
    if not kpi:
        return None
    snaps = [s for s in snapshot_store.list_snapshots(source, latest_only=True) if s["period_end"] <= current["period_end"]]
    points = comparison.trend_series(snaps, key)
    if len(points) < 2:
        return None
    name, unit = kpi.get("name", key), kpi.get("unit", "")
    first, last = points[0], points[-1]
    pct = (last["value"] - first["value"]) / first["value"] * 100 if first["value"] else None
    ins = [f"{name} was {comparison.format_value(first['value'], unit)} in {first['period_start'][:7]} "
           f"and {comparison.format_value(last['value'], unit)} in {last['period_start'][:7]}."]
    if pct is not None:
        ins.append(f"That is a change of {pct:+.1f}% across {len(points)} periods.")
    if comparison.partial_note(current):
        ins.append(comparison.partial_note(current))
    rec = [f"Find out what drove the {pct:+.1f}% change in {name}."] if pct is not None and abs(pct) >= 10 else [NO_ACTION]
    return {
        "type": "line", "title": f"{source}: {name} trend", "chart_title": f"{name} by period",
        "source": ", ".join(current.get("source_files", [])),
        "categories": [p["period_start"][:7] for p in points],
        "series": [{"name": name, "values": [p["value"] for p in points]}],
        "number_format": "#,##0" if unit == "count" else "#,##0.00",
        "insights": ins, "recommendation": rec,
    }
def build(period: tuple[str, str], items: list[tuple[str, dict, dict | None, "insights.InsightResult"]]) -> dict:
    """items = [(source, current_snapshot, previous_snapshot_or_None, InsightResult), ...]"""
    ordered = sorted(items, key=lambda i: SOURCE_ORDER.index(i[0]) if i[0] in SOURCE_ORDER else len(SOURCE_ORDER))
    slides: list[dict] = []
    for source, current, previous, result in ordered:
        source_slides = _source_slides(source, current, previous, result)
        slides += source_slides
        table = _table_slide(source, current, previous)
        if table:
            slides.append(table)
        trend = _trend_slide(source, current)
        if trend:
            slides.append(trend)
        closing = _closing_items(source_slides)
        if closing:
            slides.append({"type": "summary", "title": f"{source}: key recommendations(Experimental Feature)", "items": closing})
    return {
        "title": "KPI Report",
        "subtitle": f"{period[0]} to {period[1]}",
        "sources": [i[0] for i in ordered],
        "slides": slides,
    }