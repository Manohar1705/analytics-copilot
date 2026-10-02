"""pages/create_report.py - pick files, name the report, download a PowerPoint.

Uses the files already uploaded on the main page (session state), so nothing is
uploaded twice. All the work happens in report_engine.build_report().
"""

from __future__ import annotations

import re

import pandas as pd
import streamlit as st

import engine
import report_engine

st.set_page_config(page_title="Create Report", page_icon="📑", layout="wide")
st.title("Create Report")
st.caption("Tick the files to include, name the report, and get a PowerPoint with charts, insights and recommendations.")

MAX_FILES = 4
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

settings = st.session_state.get("settings")
router = st.session_state.get("router")
prepared = st.session_state.get("prepared")

# ---- need uploaded files first ---------------------------------------------
if settings is None or router is None or prepared is None or not prepared.datasets:
    st.info("Upload your files on the main page first, then come back here.")
    try:
        st.page_link("app.py", label="Go to the upload page", icon="📊")
    except Exception:  # noqa: BLE001 - the link is only a convenience
        pass
    st.stop()

problems = settings.problems()
if problems:
    st.error(" ".join(problems))
    st.stop()


# ---- helpers ----------------------------------------------------------------
def unique_columns(columns: list) -> list[str]:
    seen: dict[str, int] = {}
    result = []
    for column in columns:
        name = str(column)
        seen[name] = seen.get(name, 0) + 1
        result.append(name if seen[name] == 1 else f"{name} ({seen[name]})")
    return result


def show_slide(number: int, slide: dict) -> None:
    kind = slide.get("type")
    with st.expander(f"Slide {number}: {slide.get('title', '')}", expanded=number <= 2):
        if kind == "kpis":
            tiles = slide.get("kpis", [])
            for row_start in range(0, len(tiles), 4):
                cols = st.columns(4)
                for col, tile in zip(cols, tiles[row_start : row_start + 4]):
                    col.metric(tile["label"], tile["value"])
        elif kind in ("bar", "line", "donut"):
            if slide.get("chart_title"):
                st.caption(f"{kind.title()} chart: {slide['chart_title']}")
            data = {s["name"]: s["values"] for s in slide.get("series", [])}
            st.dataframe(pd.DataFrame(data, index=slide.get("categories", [])))
        elif kind == "table":
            frame = pd.DataFrame(slide.get("rows", []), columns=unique_columns(slide.get("columns", [])))
            st.dataframe(frame, hide_index=True)
        elif kind == "summary":
            for index, item in enumerate(slide.get("items", []), start=1):
                st.markdown(f"**{index}. {item.get('heading', '')}**  \n{item.get('text', '')}")

        insights = slide.get("insights") or []
        if insights:
            st.markdown("**Business insights**")
            st.markdown("\n".join(f"- {text}" for text in insights))
        if slide.get("recommendation"):
            st.markdown(f"**Recommendation** (a suggestion based on this data): {slide['recommendation']}")
        if slide.get("source"):
            st.caption(f"Source: {slide['source']}")


def show_result(result: report_engine.ReportResult) -> None:
    if not result.ok:
        st.error(result.error or "The report could not be built.")
        return
    deck = result.deck
    slides = deck["slides"]
    for warning in result.warnings:
        st.warning(warning)

    file_name = re.sub(r"[^A-Za-z0-9]+", "_", deck["title"]).strip("_") or "report"
    st.download_button(
        "Download PowerPoint", data=result.pptx, file_name=f"{file_name}.pptx", mime=PPTX_MIME, type="primary"
    )
    st.caption(f"{len(slides) + 1} slides including the cover. Check the preview below before sharing.")

    with st.expander("Slide 1: Cover", expanded=False):
        st.markdown(f"**{deck['title']}**")
        if deck.get("subtitle"):
            st.write(deck["subtitle"])
        if deck.get("sources"):
            st.caption("Data source: " + ", ".join(deck["sources"]))
    for number, slide in enumerate(slides, start=2):
        show_slide(number, slide)

    if result.removed:
        with st.expander(f"{len(result.removed)} sentence(s) removed by the number check"):
            st.caption("These contained a number that is not in the slide's data, or gave a cause the data does not state.")
            for text in result.removed:
                st.markdown(f"- {text}")


# ---- choose files -----------------------------------------------------------
files: dict[str, list] = {}
for dataset in prepared.datasets:
    files.setdefault(dataset.file_name, []).append(dataset)

st.subheader("1. Choose files")
picked: list[str] = []
for position, (name, tables) in enumerate(files.items()):
    rows = sum(len(t.df) for t in tables)
    sheets = f", {len(tables)} sheets" if len(tables) > 1 else ""
    if st.checkbox(f"{name}  ({rows:,} rows{sheets})", value=position < MAX_FILES, key=f"report_pick_{name}"):
        picked.append(name)
too_many = len(picked) > MAX_FILES
if too_many:
    st.warning(f"Choose at most {MAX_FILES} files.")

st.subheader("2. Name the report")
left, right = st.columns(2)
title = left.text_input("Report title", max_chars=80, key="report_title")
period = right.text_input("Period", max_chars=40, key="report_period")

st.subheader("3. Generate")
if st.button("Generate report", type="primary", disabled=not picked or too_many):
    chosen = [d for d in prepared.datasets if d.file_name in picked]
    subset = engine.prepare_data(chosen, settings.sample_rows)
    with st.status("Building your report", expanded=True) as status:
        result = report_engine.build_report(subset, router, title.strip(), period.strip(), on_step=st.write)
        status.update(label="Report ready" if result.ok else "Report failed", state="complete" if result.ok else "error")
    st.session_state.report_result = result

result = st.session_state.get("report_result")
if result is not None:
    st.divider()
    show_result(result)