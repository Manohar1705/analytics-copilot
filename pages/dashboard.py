"""pages/dashboard.py - upload ServiceNow / Sprinklr / Traackr files, save KPI snapshots, compare periods.

Uses the KPI layer: sources.py (read file) -> kpi_engine.py (calculate) -> snapshot_store.py (save)
-> comparison.py (change between two saved periods). No LLM, no raw rows are stored.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.express as px
import streamlit as st

import comparison
import kpi_engine
import snapshot_store
import sources

st.set_page_config(page_title="KPI Dashboard", page_icon="📈", layout="wide")

SOURCE_NAMES = list(sources.SOURCES.keys())  # ServiceNow, Sprinklr, Traackr
CARDS_PER_ROW = 4
MAX_COMPARE_BARS = 12


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
    ready = []  # (file name, result, start, end)
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
            ready.append((file.name, result, start, end))

    if st.button(f"Calculate and save {len(ready)} file(s)", type="primary", disabled=not ready or not all_ok):
        for name, result, start, end in ready:
            calc = kpi_engine.calculate_snapshot(result, start, end)
            if not calc.ok:
                for message in calc.errors:
                    st.error(f"{name}: {message}")
                continue
            st.success(f"{name} saved: {snapshot_store.save_snapshot(calc.snapshot)}")
            if calc.warnings:
                with st.expander(f"{name}: {len(calc.warnings)} note(s) from this calculation"):
                    for message in calc.warnings:
                        st.write("- " + message)


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


def _breakdown_chart(row: dict, previous_label: str | None, current_label: str) -> None:
    if row.get("categories") and previous_label:
        frame = pd.DataFrame(row["categories"]).rename(columns={"current": current_label, "previous": previous_label})
        frame = frame.sort_values(current_label, ascending=False, na_position="last").head(MAX_COMPARE_BARS)
        frame = frame.melt(id_vars="label", value_vars=[current_label, previous_label], var_name="Period", value_name="Value")
        fig = px.bar(frame, x="label", y="Value", color="Period", barmode="group", title=row["name"])
    else:
        values = row["current"] or {}
        frame = pd.DataFrame({"label": list(values.keys()), "Value": list(values.values())})
        frame = frame.sort_values("Value", ascending=False).head(MAX_COMPARE_BARS)
        fig = px.bar(frame, x="label", y="Value", title=row["name"])
    fig.update_layout(xaxis_title=None, yaxis_title=row["unit"], margin=dict(t=50, b=10))
    st.plotly_chart(fig, width="stretch")


def source_dashboard(source: str) -> None:
    snaps = snapshot_store.list_snapshots(source, latest_only=True)  # newest period first
    if not snaps:
        st.info(f"No saved {source} periods yet. Upload a file above and save it.")
        return

    pick_col, compare_col = st.columns(2)
    current = snaps[pick_col.selectbox("Period", range(len(snaps)), format_func=lambda i: _label(snaps[i]), key=f"cur:{source}")]
    others = [s for s in snaps if s["id"] != current["id"]]
    previous = None
    if others:
        choice = compare_col.selectbox(
            "Compare with", [None] + list(range(len(others))),
            format_func=lambda i: "No comparison" if i is None else _label(others[i]), key=f"prev:{source}",
        )
        previous = None if choice is None else others[choice]

    # Rows: with comparison if a second period is chosen.
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
        f"Saved {current['saved_at']} from {', '.join(current.get('source_files', []))} "
        f"- {current['period_days']} days - KPI version {current.get('kpi_version', '?')}"
    )

    # KPI cards
    cards = [r for r in rows if r["kind"] != "breakdown"]
    for start in range(0, len(cards), CARDS_PER_ROW):
        columns = st.columns(CARDS_PER_ROW)
        for column, row in zip(columns, cards[start:start + CARDS_PER_ROW]):
            column.metric(row["name"], comparison.format_value(row["current"], row["unit"]), comparison.format_delta(row))

    # Breakdown charts
    breakdowns = [r for r in rows if r["kind"] == "breakdown"]
    for start in range(0, len(breakdowns), 2):
        columns = st.columns(2)
        for column, row in zip(columns, breakdowns[start:start + 2]):
            with column:
                _breakdown_chart(row, previous["period_start"] if previous else None, current["period_start"])

    # Trend across all saved periods
    numeric = [(k, v["name"]) for k, v in current["kpis"].items() if v["kind"] != "breakdown"]
    if len(snaps) >= 2 and numeric:
        with st.expander("Trend across saved periods"):
            key = st.selectbox("KPI", [k for k, _ in numeric], format_func=dict(numeric).get, key=f"trend:{source}")
            points = comparison.trend_series(snaps, key)
            if len(points) >= 2:
                fig = px.line(pd.DataFrame(points), x="label", y="value", markers=True)
                fig.update_layout(xaxis_title=None, yaxis_title=current["kpis"][key]["unit"], margin=dict(t=10))
                st.plotly_chart(fig, width="stretch")
            else:
                st.caption("This KPI exists in fewer than two saved periods.")

    if current.get("skipped_kpis"):
        with st.expander(f"{len(current['skipped_kpis'])} KPI(s) skipped"):
            for item in current["skipped_kpis"]:
                st.write(f"- **{item['name']}**: {item['reason']}")


# =============================================================================
# Page
# =============================================================================
st.title("KPI Dashboard")
upload_section()

st.divider()
st.subheader("Saved periods")
for tab, name in zip(st.tabs(SOURCE_NAMES), SOURCE_NAMES):
    with tab:
        source_dashboard(name)

st.caption("Saved periods live in memory and are lost when the server restarts. Set STORE_BACKEND=sqlite to keep them.")