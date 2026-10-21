"""report_deck.py - builds the deck dictionary for pptx_writer.build_deck() from SAVED KPIs (no Streamlit).

Order: ServiceNow, then Traackr, then Sprinklr. A source with no saved data is skipped, and the
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
SOURCE_ORDER = ["ServiceNow", "Traackr", "Sprinklr"]
KPIS_PER_SLIDE = 8        # pptx_writer.MAX_KPIS
MAX_BARS = 12
MAX_DONUT_SLICES = 8      # same chart rule as pages/dashboard.py
SKEW_LIMIT = 0.75
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


def _row_insight(r: dict) -> str | None:
    """One real sentence from a KPI row's own numbers (used when no rule fact exists)."""
    name, unit, value = r["name"], r.get("unit", ""), r.get("current")
    if r.get("kind") == "breakdown":
        values = {k: v for k, v in (value or {}).items() if _is_number(v)}
        if not values:
            return None
        top, low = max(values, key=values.get), min(values, key=values.get)
        text = f"{top} is the largest in {name} at {comparison.format_value(values[top], unit)}"
        total = sum(v for v in values.values() if v > 0)
        if unit == "count" and total > 0:
            text += f", {values[top] / total * 100:.1f}% of the total"
        if low != top:
            text += f"; {low} is the lowest at {comparison.format_value(values[low], unit)}"
        return text + "."
    if not _is_number(value):
        return None
    text = f"{name} is {comparison.format_value(value, unit)}"
    prev, change, pct = r.get("previous"), r.get("change"), r.get("pct_change")
    if _is_number(prev) and _is_number(change):
        before = comparison.format_value(prev, unit)
        if change == 0:
            text += f", unchanged from {before} in the previous period"
        elif unit == "%":
            text += f", {'up' if change > 0 else 'down'} {abs(change):.2f} points from {before}"
        elif pct is not None:
            text += f", {'up' if change > 0 else 'down'} {abs(pct):.1f}% from {before}"
        else:
            text += f", {'up' if change > 0 else 'down'} from {before}"
    return text + "."


def _row_advice(r: dict) -> str | None:
    """One specific recommendation for a KPI row: worse, better or steady."""
    name, unit, value = r["name"], r.get("unit", ""), r.get("current")
    if r.get("kind") == "breakdown":
        values = {k: v for k, v in (value or {}).items() if _is_number(v)}
        if len(values) < 2:
            return f"Keep tracking {name} next period."
        top, low = max(values, key=values.get), min(values, key=values.get)
        total = sum(v for v in values.values() if v > 0)
        if unit == "count" and total > 0 and values[top] / total >= 0.5:
            return f"Check whether {top} holding {values[top] / total * 100:.0f}% of {name} is intended, and look at {low} for room to grow."
        return f"Compare {low} with {top} in {name} and decide whether the gap needs action."
    change = r.get("change")
    polarity = insights.POLARITY.get(r["key"], 0)
    if _is_number(change) and change != 0:
        worse = (polarity > 0 and change < 0) or (polarity < 0 and change > 0)
        if worse:
            return f"Review {name}: it moved the wrong way against the previous period."
        if polarity != 0:
            return f"{name} moved in the right direction; keep the current approach and confirm it holds next period."
        return f"Check what changed in {name} compared with the previous period."
    return f"Keep tracking {name} next period; this period's value is the baseline to compare against."


def _lines(keys: set[str], result: "insights.InsightResult", facts: list["insights.Fact"], rows: list[dict] | None = None) -> tuple[list[str], list[str]]:
    """Insights and recommendations for the KPIs in `keys`: checked lines first, then rule facts,
    then sentences built from the slide's own KPI rows. Never returns filler text."""
    shown = [l for l in result.insights if l.kpi in keys]
    found = [l.text for l in shown]
    used = {l.fact_id for l in shown}
    seen = {l.kpi for l in shown}
    for fact in facts:
        if len(found) >= MAX_PER_SLIDE:
            break
        if fact.kpi in keys and fact.id not in used:
            found.append(fact.text)
            seen.add(fact.kpi)
    shown_rec = [l for l in result.recommendations if l.kpi in keys]
    advice = [l.text for l in shown_rec]
    used_rec = {l.fact_id for l in shown_rec}
    seen_rec = {l.kpi for l in shown_rec}
    for fact in facts:
        if len(advice) >= MAX_RECS_PER_SLIDE:
            break
        if fact.kpi in keys and fact.advice and fact.id not in used_rec:
            advice.append(fact.advice)
            seen_rec.add(fact.kpi)
    # guarantee: real sentences from this slide's own numbers, biggest movers first
    ranked = sorted(rows or [], key=lambda r: -(abs(r["pct_change"]) if _is_number(r.get("pct_change")) else 0))
    for r in ranked:
        if len(found) >= MAX_PER_SLIDE:
            break
        text = _row_insight(r)
        if text and r["key"] not in seen and text not in found:
            found.append(text)
    for r in ranked:
        if len(advice) >= MAX_RECS_PER_SLIDE:
            break
        text = _row_advice(r)
        if text and r["key"] not in seen_rec and text not in advice:
            advice.append(text)
    return found, advice

def _ai_lines(ai: Any, current: dict, keys: set[str]) -> tuple[list[str], list[str]]:
    """Saved AI text for the KPIs in `keys`. Empty lists when there is none, so the rule text is used instead."""
    if not ai:
        return [], []
    ins, rec = [], []
    for key, kpi in current["kpis"].items():
        if key not in keys:
            continue
        entry = ai.entries.get(kpi.get("dashboard_id"))
        if entry is None or entry.by != "ai":  # plain fallback sentences are not worth a slide box
            continue
        ins.append(entry.insight)
        rec.append(entry.recommendation)
    return ins[:MAX_PER_SLIDE], rec[:MAX_RECS_PER_SLIDE]

def _source_slides(source: str, current: dict, previous: dict | None, result: "insights.InsightResult", ai: Any = None) -> list[dict]:
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
        ins, rec = _lines(keys, result, facts, chunk)
        a_ins, a_rec = _ai_lines(ai, current, keys)
        ins, rec = (a_ins or ins), (a_rec or rec)
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
        if comparing and row.get("categories"):
            kind = "bar"  # a donut cannot show two months, so compare with grouped bars
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
        ins, rec = _lines({row["key"]}, result, facts, [row])
        a_ins, a_rec = _ai_lines(ai, current, {row["key"]})
        ins, rec = (a_ins or ins), (a_rec or rec)
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
            recs = [t for t in slide["recommendation"] if t]
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
        lead, last_name = top[0][0], top[-1][0]
        if total and shown / total >= 0.5:
            rec = [f"Check whether having the top {len(top)} hold {shown / total * 100:.0f}% of {name} is intended, and review {last_name} against {lead}."]
        else:
            rec = [f"Track whether {lead} keeps its lead in {name} next period, and look at {last_name} for room to grow."]
    return {
        "type": "table", "title": f"{source}: {name} (table)", "source": ", ".join(current.get("source_files", [])),
        "columns": ["Name", "Current", "Previous", "Change", "Change %"] if comparing else ["Name", "Value", "Share"],
        "rows": rows, "insights": ins, "recommendation": rec,
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
    if pct is not None and abs(pct) >= 10:
        rec = [f"Find out what drove the {pct:+.1f}% change in {name}, and check whether the latest period continues the pattern."]
    elif pct is not None:
        rec = [f"{name} moved {pct:+.1f}% across {len(points)} periods; keep tracking it monthly and review it if the change passes 10%."]
    else:
        rec = [f"Keep tracking {name} monthly; {comparison.format_value(last['value'], unit)} is the latest level."]
    return {
        "type": "line", "title": f"{source}: {name} trend", "chart_title": f"{name} by period",
        "source": ", ".join(current.get("source_files", [])),
        "categories": [p["period_start"][:7] for p in points],
        "series": [{"name": name, "values": [p["value"] for p in points]}],
        "number_format": "#,##0" if unit == "count" else "#,##0.00",
        "insights": ins, "recommendation": rec,
    }
def build(period: tuple[str, str], items: list[tuple[str, dict, dict | None, "insights.InsightResult"]], ai: Any = None) -> dict:
    """items = [(source, current_snapshot, previous_snapshot_or_None, InsightResult), ...]"""
    ordered = sorted(items, key=lambda i: SOURCE_ORDER.index(i[0]) if i[0] in SOURCE_ORDER else len(SOURCE_ORDER))
    slides: list[dict] = []
    for source, current, previous, result in ordered:
        source_slides = _source_slides(source, current, previous, result, ai)
        slides += source_slides
        table = _table_slide(source, current, previous)
        if table:
            slides.append(table)
        trend = _trend_slide(source, current)
        if trend:
            slides.append(trend)
        closing = _closing_items(source_slides)
        if closing:
            slides.append({"type": "summary", "title": f"{source}: key recommendations", "items": closing})
    return {
        "title": "KPI Report",
        "subtitle": f"{period[0]} to {period[1]}",
        "sources": [i[0] for i in ordered],
        "slides": slides,
    }