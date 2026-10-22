"""pages/dashboard.py - upload ServiceNow / Sprinklr / Traackr files, save KPI snapshots, compare periods.

Uses the KPI layer: sources.py (read file) -> kpi_engine.py (calculate) -> snapshot_store.py (save)
-> comparison.py (change between two saved periods). No LLM, no raw rows are stored.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import plotly.express as px
import streamlit as st

import comparison
import dashboard_ai
import insights
import kpi_engine
import pptx_writer
import report_deck
import snapshot_store
import sources

st.set_page_config(page_title="KPI Dashboard", page_icon="📈", layout="wide")

SOURCE_ORDER = ["ServiceNow", "Traackr", "Sprinklr"]  # order of the sections on the page
CARDS_PER_ROW = 4
MAX_COMPARE_BARS = 12

# ---- Chart look (change these in one place) ----------------------------------
PALETTE = ["#73AEF7", "#FA99C7", "#A3A3A3", "#FEE920", "#F59691", "#C4EBC1", "#FFC000", "#8E5572"]  # colours from the report template
PREVIOUS_COLOR = "#A3A3A3"   # the older period in a comparison chart
SKEW_LIMIT = 0.75            # a donut is not used when one value holds more than this share
COLOR_EACH_UP_TO = 8         # bars get one colour each up to this many bars (more bars: one colour)
MAX_DONUT_SLICES = 8         # a donut is not used with more slices than this
LONG_LABEL = 14              # bars turn horizontal when a label is longer than this ...
MANY_LABELS = 6              # ... or when there are more categories than this

def _get_router():
    """Reuse the main page's model, or build one here so the dashboard works when opened first."""
    router = st.session_state.get("router")
    if router is None:
        try:
            router = dashboard_ai.make_router()
        except Exception as exc:  # noqa: BLE001 - the dashboard must work without the AI
            st.warning(f"AI text skipped: the model could not be set up ({exc}).")
            return None
        st.session_state["router"] = router
    return router


def _run_ai(start: str, end: str) -> bool:
    """One AI call for a period, only when ServiceNow, Traackr and Sprinklr are all saved for it."""
    period = (start, end)
    snaps = {}
    for name in SOURCE_ORDER:
        found = _find(snapshot_store.list_snapshots(name, latest_only=True), period)
        if found is None:
            st.info(f"AI text for {start} to {end} will be written once {name} is saved for this period.")
            return False
        snaps[name] = found
    router = _get_router()
    if router is None:
        return False
    notes_store = st.session_state.get("ai_notes", {})
    notes = {name: notes_store.get((start, end, name), []) for name in SOURCE_ORDER}
    payload = dashboard_ai.build_payload(snaps, notes)
    with st.spinner(f"Writing insights and recommendations for {start} to {end}..."):
        result = dashboard_ai.generate(payload, router, period)
    for message in result.notes:
        st.caption("AI note: " + message)
    if not result.entries:  # nothing usable came back: keep whatever text was saved before
        st.warning("No AI text was written (the AI was unavailable or rate-limited). Try 'Regenerate AI insights' again in a moment.")
        return False
    snapshot_store.save_ai_insights(start, end, result.to_dict())
    return True
# =============================================================================
# Upload and save
# =============================================================================
def upload_section() -> None:
    st.subheader("1. Upload files")
    files = st.file_uploader(
        "Excel exports (ServiceNow, Sprinklr, Traackr). You can select several at once.",
        type=["xlsx", "xlsm", "xls"], accept_multiple_files=True,
    )
    if not files:
        st.caption("The source is detected from the column names. Nothing is saved until you press the save button.")
        return

    # Read each file once per upload, not on every click.
    cache = st.session_state.setdefault("file_results", {})
    ready = []  # (file_key, result, start, end)
    all_ok = True
    st.subheader("2. Confirm the period for each file")
    for file in files:
        file_key = f"{file.name}:{file.size}"
        if file_key not in cache:
            cache[file_key] = sources.inspect_file(file.name, file.getvalue())
        result: sources.FileResult = cache[file_key]

        with st.container(border=True):
            if not result.ok:
                st.markdown(f"**{file.name}**")
                for message in result.errors:
                    st.error(message)
                continue
            st.markdown(f"**{file.name}** - recognised as **{result.source}** ({result.row_count:,} rows in the detected period)")
            st.caption(f"Sheets used: {', '.join(f'{t} = {s}' for t, s in result.table_sheets.items())}. {result.period_note}")
            for message in result.warnings:
                st.warning(message)
            left, right = st.columns(2)
            start = left.date_input("Period start", value=result.period_start or date.today(), key=f"start:{file_key}")
            end = right.date_input("Period end", value=result.period_end or date.today(), key=f"end:{file_key}")
            problems = sources.validate_period(start, end)
            for message in problems:
                st.error(message)
                all_ok = False
            if not problems:
                inside = sources.apply_period(result.source, result.tables, start, end)
                counts = []
                for table, df in result.tables.items():
                    if table in sources.TOTAL_TABLES:
                        continue
                    in_period = len(inside[table])
                    counts.append(f"{result.table_sheets[table]}: {len(df):,} rows" + (f" ({in_period:,} in this period)" if in_period != len(df) else ""))
                st.caption("Rows read - " + "; ".join(counts))
            if result.skipped_sheets:
                st.caption("Sheets not read - " + ", ".join(result.skipped_sheets))
            if result.source not in sources.DATE_FILTERS:
                detected = (result.period_start, result.period_end)
                if detected[0] and (start, end) != detected:
                    st.warning(f"The detected period is {detected[0]} to {detected[1]}. {result.source} numbers cover the whole file, so changing the dates only changes the label.")
                else:
                    st.caption(f"{result.source}: the period is only a label. The numbers cover the whole file and do not change with the dates.")
            ready.append((file.name, result, start, end))

    if st.button(f"Calculate and save {len(ready)} file(s)", type="primary", disabled=not ready or not all_ok):
        for name, result, start, end in ready:
            calc = kpi_engine.calculate_snapshot(result, start, end)
            if not calc.ok:
                for message in calc.errors:
                    st.error(f"{name}: {message}")
                continue
            st.success(f"{name} saved: {snapshot_store.save_snapshot(calc.snapshot)}")
            inside = sources.apply_period(result.source, result.tables, start, end)
            st.session_state.setdefault("ai_notes", {})[(str(start), str(end), result.source)] = dashboard_ai.summarise_tables(result.source, inside)
            if calc.warnings:
                with st.expander(f"{name}: {len(calc.warnings)} note(s) from this calculation"):
                    for message in calc.warnings:
                        st.write("- " + message)
        for p_start, p_end in sorted({(str(s), str(e)) for _, _, s, e in ready}):
            _run_ai(p_start, p_end)

# =============================================================================
# Dashboard for one source
# =============================================================================
def _label(snap: dict) -> str:
    return f"{snap['period_start']} to {snap['period_end']}  (v{snap['version']})"


def _plain_rows(snapshot: dict) -> list[dict]:
    """Rows without a comparison, in the same shape comparison.py returns."""
    rows = []
    for key, kpi in snapshot["kpis"].items():
        rows.append({
            "key": key, "name": kpi["name"], "unit": kpi["unit"], "kind": kpi["kind"],
            "current": kpi["value"], "previous": None, "change": None, "pct_change": None,
            "change_unit": kpi["unit"], "direction": "n/a",
        })
    return rows


# ---- Charts ------------------------------------------------------------------
def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _chart_kind(chart_type: str, unit: str, values: dict) -> tuple[str, str | None]:
    """Pick the chart from the shape of the data. Returns (kind, note); kind is "donut" or "bar".

    Donut: counts split into 2 to MAX_DONUT_SLICES parts where no single part dominates (SKEW_LIMIT).
    Bar: everything else (rankings, many items, averages, one dominant value).
    chart_type in kpi_config.py is no longer needed.
    """
    positives = {k: v for k, v in (values or {}).items() if _is_number(v) and v > 0}
    if unit != "count" or not 2 <= len(positives) <= MAX_DONUT_SLICES:
        return "bar", None
    top = max(positives.values()) / sum(positives.values())
    if top > SKEW_LIMIT:
        return "bar", f"Shown as bars: one value holds {top:.0%} of the total, which a donut cannot show clearly."
    return "donut", None


def _bar_figure(row: dict, previous_label: str | None, current_label: str):
    """Bar chart for one breakdown KPI: grouped by period when a comparison exists."""
    comparing = bool(row.get("categories") and previous_label)
    if comparing:
        frame = pd.DataFrame(row["categories"]).rename(columns={"current": current_label, "previous": previous_label})
        frame = frame.sort_values(current_label, ascending=False, na_position="last").head(MAX_COMPARE_BARS)
        order = list(frame["label"])
        frame = frame.melt(id_vars="label", value_vars=[current_label, previous_label], var_name="Period", value_name="Value")
    else:
        values = row["current"] or {}
        frame = pd.DataFrame({"label": list(values.keys()), "Value": list(values.values())})
        frame = frame.sort_values("Value", ascending=False).head(MAX_COMPARE_BARS)
        order = list(frame["label"])

    horizontal = len(order) > MANY_LABELS or any(len(str(x)) > LONG_LABEL for x in order)
    common = dict(title=row["name"])
    if comparing:
        common.update(color="Period", barmode="group", color_discrete_map={current_label: PALETTE[0], previous_label: PREVIOUS_COLOR})
    if horizontal:
        fig = px.bar(frame, x="Value", y="label", orientation="h", **common)
        fig.update_yaxes(categoryorder="array", categoryarray=order[::-1])  # largest on top
        fig.update_layout(xaxis_title=row["unit"], yaxis_title=None)
    else:
        fig = px.bar(frame, x="label", y="Value", **common)
        fig.update_layout(xaxis_title=None, yaxis_title=row["unit"])
    if not comparing:  # one colour per bar when there are few bars, one colour when there are many
        fig.update_traces(marker_color=[PALETTE[i % len(PALETTE)] for i in range(len(frame))] if len(frame) <= COLOR_EACH_UP_TO else PALETTE[0])
    fig.update_layout(margin=dict(t=50, b=10))
    return fig


def _donut_figure(row: dict):
    """Donut of the current period's shares (one slice per category)."""
    values = {k: v for k, v in (row["current"] or {}).items() if _is_number(v) and v > 0}
    frame = pd.DataFrame({"label": list(values.keys()), "Value": list(values.values())}).sort_values("Value", ascending=False)
    fig = px.pie(frame, names="label", values="Value", hole=0.5, title=row["name"], color_discrete_sequence=PALETTE)
    fig.update_traces(textinfo="percent", sort=False)
    fig.update_layout(margin=dict(t=50, b=10), legend_title_text=None)
    return fig


def _change_table(row: dict, previous_label: str | None, current_label: str) -> None:
    """Small table under a donut: a donut cannot show two periods, so the change is listed."""
    if not (row.get("categories") and previous_label):
        return
    frame = pd.DataFrame(row["categories"])[["label", "current", "previous", "change"]]
    frame.columns = ["", current_label, previous_label, "Change"]
    st.dataframe(frame.head(MAX_COMPARE_BARS), hide_index=True, width="stretch")


def _breakdown_chart(source: str, row: dict, chart_type: str, previous_label: str | None, current_label: str) -> None:
    kind, note = _chart_kind(chart_type, row["unit"], row["current"])
    if kind == "donut" and previous_label and row.get("categories"):
        kind = "bar"  # same rule as the report: two months are shown as grouped bars
    key = f"chart:{source}:{row['key']}"
    if kind == "donut":
        st.plotly_chart(_donut_figure(row), width="stretch", key=key)
        _change_table(row, previous_label, current_label)
    else:
        st.plotly_chart(_bar_figure(row, previous_label, current_label), width="stretch", key=key)
        if note:
            st.caption(note)


def _trend_section(source: str, current: dict, snaps: list[dict]) -> None:
    """Line chart of one KPI across all saved periods (shown directly, not hidden)."""
    numeric = [(k, v["name"]) for k, v in current["kpis"].items() if v["kind"] != "breakdown"]
    if not numeric:
        return
    st.markdown("**Trend across saved periods**")
    if len(snaps) < 2:
        st.caption("Save at least two periods of this source to see a trend.")
        return
    key = st.selectbox("KPI", [k for k, _ in numeric], format_func=dict(numeric).get, key=f"trend:{source}")
    points = comparison.trend_series(snaps, key)
    if len(points) < 2:
        st.caption("This KPI exists in fewer than two saved periods.")
        return
    fig = px.line(pd.DataFrame(points), x="label", y="value", markers=True, color_discrete_sequence=PALETTE)
    fig.update_layout(xaxis_title=None, yaxis_title=current["kpis"][key]["unit"], margin=dict(t=10))
    st.plotly_chart(fig, width="stretch", key=f"trendchart:{source}")


def _table_section(source: str, current: dict, previous: dict | None) -> None:
    """The same top-10 table as the report slide. It calls the report's own builder, so rows and columns always match."""
    spec = report_deck._table_slide(source, current, previous)
    if not spec:
        return
    st.markdown("**" + spec["title"].split(": ", 1)[-1] + "**")
    st.dataframe(pd.DataFrame(spec["rows"], columns=spec["columns"]), hide_index=True, width="stretch")
    # for line in spec["insights"]:
    #     st.caption(line)

def _ai_text(ai, dash_id: str | None) -> None:
    """Insight and recommendation for one dashboard ID (nothing is drawn when there is no AI text)."""
    entry = ai.entries.get(dash_id) if ai and dash_id else None
    if entry is None:
        if ai and dash_id:
            st.caption(f"{dash_id}: AI insight not available. Press 'Regenerate AI insights' to try again.")
        return
    st.markdown(f"**{dash_id} - Insight**")
    st.write(entry.insight)
    st.markdown("**Business recommendation**")
    st.write(entry.recommendation)

def _find(snaps: list[dict], period: tuple[str, str] | None) -> dict | None:
    """The saved snapshot of this source for one (start, end) period, or None."""
    if period is None:
        return None
    return next((s for s in snaps if (s["period_start"], s["period_end"]) == period), None)


def _nice_time(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d %b %Y, %H:%M UTC")


def _get_insights(source: str, current: dict, previous: dict | None):
    """Insights for one source, reused from the cache when the snapshots and the AI setting are unchanged."""
    router = st.session_state.get("router")
    use_ai = bool(st.session_state.get("insights_use_ai")) and router is not None
    cache = st.session_state.setdefault("insights_cache", {})
    key = f"{insights.cache_key(current, previous)}|ai={use_ai}"
    if key not in cache:
        with st.spinner(f"Writing {source} insights..."):
            cache[key] = insights.generate(current, previous, router=router if use_ai else None)
    return cache[key]


def _insights_panel(source: str, current: dict, previous: dict | None) -> None:
    """Insights and recommendations for one source. Plain sentences always; AI wording only if ticked."""
    router = st.session_state.get("router")
    result = _get_insights(source, current, previous)

    st.subheader("Insights and recommendations")
    left, right = st.columns(2)
    with left:
        st.markdown("**Insights**")
        for line in result.insights:
            st.markdown(("⚠️ " if line.flagged else "• ") + line.text)
        if not result.insights:
            st.caption("Nothing stands out for this period.")
    with right:
        st.markdown("**Recommendations**")
        for line in result.recommendations:
            st.markdown("• " + line.text)
        if not result.recommendations:
            st.caption("No action needed from the current numbers.")
    if st.session_state.get("insights_use_ai") and router is None:
        st.caption("AI wording is off: open the main app page once so the model is set up. Showing the plain sentences.")
    elif result.ai_used:
        st.caption(f"Reworded by AI ({result.model}) and checked against the numbers. {result.fallbacks} sentence(s) kept in plain wording.")
    for message in result.notes:
        st.caption("Note: " + message)


def render_source(source: str, period: tuple[str, str], compare_period: tuple[str, str] | None) -> None:
    st.header(source)
    snaps = snapshot_store.list_snapshots(source, latest_only=True)
    current = _find(snaps, period)
    if current is None:
        st.info(f"No saved {source} data for {period[0]} to {period[1]}.")
        return
    previous = _find(snaps, compare_period)
    if compare_period and previous is None:
        st.info(f"No saved {source} data for {compare_period[0]} to {compare_period[1]}, so no comparison is shown.")
    saved_ai = snapshot_store.get_ai_insights(period[0], period[1])
    ai = dashboard_ai.AIResult.from_dict(saved_ai) if saved_ai else None

    # Rows: with comparison if a second period is available.
    if previous:
        comp = comparison.compare_snapshots(current, previous)
        for message in comp.errors:
            st.error(message)
        for message in comp.warnings:
            st.warning(message)
        rows = comp.rows if comp.ok else _plain_rows(current)
    else:
        rows = _plain_rows(current)

    st.caption(
        f"Saved {_nice_time(current['saved_at'])} from {', '.join(current.get('source_files', []))} "
        f"- {current['period_days']} days - KPI version {current.get('kpi_version', '?')}"
    )
    note = comparison.partial_note(current)
    if note and not previous:
        st.warning(note)
    # KPI cards
    cards = [r for r in rows if r["kind"] != "breakdown"]
    for row in cards:  # one KPI per row: the number on the left, its own insight and recommendation on the right
        number_col, text_col = st.columns([3, 2])
        with number_col:
            st.metric(row["name"], comparison.format_value(row["current"], row["unit"]), comparison.format_delta(row))
        with text_col:
            _ai_text(ai, current["kpis"].get(row["key"], {}).get("dashboard_id"))
    # Breakdown charts: bar, horizontal bar or donut, chosen from each KPI's chart_type and its data
    breakdowns = [r for r in rows if r["kind"] == "breakdown"]
    for row in breakdowns:  # one chart per row: the chart on the left, its insight and recommendation on the right
        chart_col, text_col = st.columns([3, 2])
        with chart_col:
            chart_type = current["kpis"].get(row["key"], {}).get("chart_type", "bar")
            _breakdown_chart(source, row, chart_type, previous["period_start"] if previous else None, current["period_start"])
        with text_col:
            _ai_text(ai, current["kpis"].get(row["key"], {}).get("dashboard_id"))

    # Top-10 table (same one as the report)
    _table_section(source, current, previous)

    # Trend across all saved periods of this source
    # _trend_section(source, current, snaps)

    if current.get("skipped_kpis"):
        with st.expander(f"{source}: {len(current['skipped_kpis'])} KPI(s) skipped"):
            for item in current["skipped_kpis"]:
                st.write(f"- **{item['name']}**: {item['reason']}")


# =============================================================================
# Page
# =============================================================================
st.title("KPI Dashboard")
with st.expander("Upload files", expanded=not snapshot_store.list_snapshots(latest_only=True)):
    upload_section()
all_snaps = snapshot_store.list_snapshots(latest_only=True)  # read again: the upload above may have just saved

# One shared period for every section: only periods that have been saved are offered.
periods = sorted({(s["period_start"], s["period_end"]) for s in all_snaps}, reverse=True)
if not periods:
    st.info("No saved data yet. Upload files above and save them.")
    st.stop()

# pick_col, compare_col, ppt_col = st.columns([2, 2, 1])
# period = periods[pick_col.selectbox("Period", range(len(periods)), format_func=lambda i: f"{periods[i][0]} to {periods[i][1]}")]
# others = [p for p in periods if p != period]
# older = [p for p in others if p < period]  # saved periods before the chosen one, newest first
# compare_period = None
# if others:
#     default = 1 + others.index(older[0]) if older else 0  # pre-select the previous period
#     choice = compare_col.selectbox(
#         "Compare with", [None] + list(range(len(others))), index=default,
#         format_func=lambda i: "No comparison" if i is None else f"{others[i][0]} to {others[i][1]}",
#     )
#     compare_period = None if choice is None else others[choice]
# ppt_col.write("")  # lines the checkbox up with the pickers
# ppt_col.checkbox("Reword insights with AI", key="insights_use_ai", help="Off: plain sentences from the numbers. On: the AI rewords them and every sentence is checked.")

pick_col, _ = st.columns([2, 3])
period = periods[pick_col.selectbox("Period", range(len(periods)), format_func=lambda i: f"{periods[i][0]} to {periods[i][1]}")]
compare_period = None  # comparison is switched off


updated = [s["saved_at"] for s in all_snaps if (s["period_start"], s["period_end"]) == period]
st.caption(f"Reporting period: {period[0]} to {period[1]}  |  Last updated: {_nice_time(max(updated))}")
if st.button("Regenerate AI insights for this period"):
    if _run_ai(period[0], period[1]):
        st.rerun()

for name in SOURCE_ORDER:
    st.divider()
    render_source(name, period, compare_period)

# ---- Report -------------------------------------------------------------------
st.divider()
st.header("Report")
st.caption("Builds a PowerPoint from the saved KPIs, charts, insights and recommendations shown above.")
if st.button("Generate report (PPT)", type="primary"):
    report_items = []
    for name in SOURCE_ORDER:
        source_snaps = snapshot_store.list_snapshots(name, latest_only=True)
        current_snap = _find(source_snaps, period)
        if current_snap is None:
            continue
        previous_snap = _find(source_snaps, compare_period)
        report_items.append((name, current_snap, previous_snap, _get_insights(name, current_snap, previous_snap)))
    if not report_items:
        st.error("There is no saved data for this period.")
    else:
        try:
            saved_ai = snapshot_store.get_ai_insights(period[0], period[1])
            ai = dashboard_ai.AIResult.from_dict(saved_ai) if saved_ai else None
            deck = report_deck.build(period, report_items, ai)
            data, deck_warnings = pptx_writer.build_deck(deck)
            st.session_state["report_file"] = (f"kpi_report_{period[0]}_{period[1]}.pptx", data, deck_warnings)
        except Exception as exc:  # noqa: BLE001 - show the problem instead of a crash
            st.error(f"Could not build the report: {exc}")
report_file = st.session_state.get("report_file")
if report_file:
    st.download_button(
        f"Download {report_file[0]}", data=report_file[1], file_name=report_file[0],
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
    for message in report_file[2]:
        st.warning(message)

# st.caption("Saved periods live in memory ...