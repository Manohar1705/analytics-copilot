"""app.py - the Streamlit screen for Analytics Copilot.

Run with:  streamlit run app.py

Flow: upload files -> look at your data -> ask questions -> get an answer,
tables, charts and downloads. All analysis logic lives in engine.py and
data_loader.py; this file only draws the screen and keeps session state.
"""

from __future__ import annotations

import io
import re
import uuid
import pandas as pd
import plotly.io as pio
import streamlit as st

import engine
from data_loader import (
    column_profile_frame,
    datasets_summary_frame,
    find_shared_columns,
    load_uploaded_files,
    quality_issues,
)

st.set_page_config(page_title="Analytics Copilot", page_icon="📊", layout="wide")

STARTER_QUESTIONS = [
    "Give me a short overview of this data.",
    "Which columns have missing values or other data quality problems?",
    "What are the main patterns or trends in this data?",
]
PREVIEW_ROWS = 100


# =============================================================================
# Session state
# =============================================================================
def init_state() -> None:
    if "settings" not in st.session_state:
        settings = engine.Settings.from_env()
        st.session_state.settings = settings
        st.session_state.router = engine.LLMRouter(settings)
        st.session_state.router.session_id = uuid.uuid4().hex[:12]
    defaults = {
        "uploader_key": 0,
        "signature": (),
        "datasets": [],
        "prepared": None,
        "load_errors": [],
        "cache": {},
        "messages": [],  # what is drawn on screen
        "history": [],  # what the model remembers (question/answer pairs)
        "pending_prompt": None,
        "notice": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def cached(key: str, builder):
    """Compute something once per upload (profiles are slow on big files)."""
    cache = st.session_state.cache
    if key not in cache:
        cache[key] = builder()
    return cache[key]


def reset_conversation() -> None:
    st.session_state.messages = []
    st.session_state.history = []


def clear_chat() -> None:
    reset_conversation()


def reload_settings() -> None:
    settings = engine.Settings.from_env()
    st.session_state.settings = settings
    st.session_state.router = engine.LLMRouter(settings)
    if st.session_state.datasets:
        st.session_state.prepared = engine.prepare_data(st.session_state.datasets, settings.sample_rows)
    st.session_state.notice = "Settings reloaded from .env."


def end_session() -> None:
    """Forget the uploaded data, the chat and any model pauses."""
    st.session_state.uploader_key += 1  # gives the upload box a fresh, empty state
    st.session_state.signature = ()
    st.session_state.datasets = []
    st.session_state.prepared = None
    st.session_state.load_errors = []
    st.session_state.cache = {}
    reset_conversation()
    st.session_state.router = engine.LLMRouter(st.session_state.settings)
    st.session_state.notice = "Session ended. Your uploaded data and the chat were cleared from this app."


def use_starter(question: str) -> None:
    st.session_state.pending_prompt = question


def process_upload(files: list) -> None:
    """Read the uploaded files, but only when the set of files has changed."""
    signature = tuple((f.name, f.size) for f in files)
    if signature == st.session_state.signature:
        return
    st.session_state.signature = signature
    st.session_state.cache = {}
    reset_conversation()  # a new set of files means a new conversation
    if not files:
        st.session_state.datasets, st.session_state.prepared, st.session_state.load_errors = [], None, []
        return
    with st.spinner("Reading your files..."):
        result = load_uploaded_files(files)
        settings = st.session_state.settings
        st.session_state.datasets = result.datasets
        st.session_state.load_errors = result.errors
        st.session_state.prepared = (
            engine.prepare_data(result.datasets, settings.sample_rows) if result.datasets else None
        )


# =============================================================================
# Downloads
# =============================================================================
def _sheet_name(title: str, used: set[str]) -> str:
    base = re.sub(r"[\[\]:*?/\\]", "", title).strip()[:28] or "Result"
    name, n = base, 2
    while name.lower() in used:
        name = f"{base}_{n}"
        n += 1
    used.add(name.lower())
    return name


def build_downloads(res: engine.AnswerResult, question: str) -> dict:
    """Prepare download files once, when the answer is created."""
    files: dict = {"xlsx": None, "csv": [], "png": list(res.figures), "code": None}
    if res.tables:
        files["csv"] = [(t.title, t.df.to_csv(index=False).encode("utf-8-sig")) for t in res.tables]
        try:
            buffer, used = io.BytesIO(), set()
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                for table in res.tables:
                    table.df.to_excel(writer, sheet_name=_sheet_name(table.title, used), index=False)
            files["xlsx"] = buffer.getvalue()
        except Exception:  # noqa: BLE001 - CSV downloads still work
            files["xlsx"] = None
    if res.code:
        note = " ".join(question.split())
        files["code"] = (
            f"# Question: {note}\n# Variables named after your files are the uploaded tables (pandas DataFrames).\n"
            f"{res.code}\n"
        ).encode("utf-8")
    return files


def display_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Make a table safe to draw: columns that mix text and numbers are shown as text."""
    df = df.copy()
    for position, dtype in enumerate(df.dtypes):
        if dtype == object:
            values = df.iloc[:, position]
            if values.dropna().map(type).nunique() > 1:
                df.isetitem(position, values.where(values.isna(), values.astype(str)))
    return df


# =============================================================================
# Drawing an answer
# =============================================================================
def render_downloads(files: dict, idx: int) -> None:
    buttons: list[tuple[str, bytes, str, str, str]] = []  # label, data, file name, mime, key
    if files.get("xlsx"):
        buttons.append(("Excel (all tables)", files["xlsx"], f"answer_{idx + 1}.xlsx",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", f"dl_{idx}_xlsx"))
    # many = len(files.get("csv", [])) > 1
    # for n, (title, data) in enumerate(files.get("csv", [])):
    #     label = f"CSV: {title}" if many else "CSV"
    #     buttons.append((label, data, f"answer_{idx + 1}_{n + 1}.csv", "text/csv", f"dl_{idx}_csv_{n}"))
    many = len(files.get("png", [])) > 1
    for n, data in enumerate(files.get("png", [])):
        label = f"Chart {n + 1} (PNG)" if many else "Chart (PNG)"
        buttons.append((label, data, f"chart_{idx + 1}_{n + 1}.png", "image/png", f"dl_{idx}_png_{n}"))
    # if files.get("code"):
    #     buttons.append(("Code (.py)", files["code"], f"analysis_{idx + 1}.py", "text/x-python", f"dl_{idx}_code"))
    for start in range(0, len(buttons), 4):
        row = buttons[start:start + 4]
        for column, (label, data, name, mime, key) in zip(st.columns(4), row):
            column.download_button(label, data=data, file_name=name, mime=mime, key=key)


def render_answer(msg: dict, idx: int) -> None:
    res: engine.AnswerResult = msg["res"]
    if res.ok:
        st.markdown(msg["text"])
    else:
        st.warning(msg["text"])

    for table in res.tables:
        if len(res.tables) > 1:
            st.markdown(f"**{table.title}**")
        st.dataframe(display_frame(table.df), hide_index=True)
        if table.truncated:
            st.caption(f"Showing the first {len(table.df):,} of {table.total_rows:,} rows.")
    for number, figure_json in enumerate(res.plotly_figures):
        st.plotly_chart(pio.from_json(figure_json), key=f"plotly_{idx}_{number}")
    for figure in res.figures:
        st.image(figure, width=760)


    if res.steps or res.code or res.error:
        with st.expander("How this was calculated"):
            # if res.model:
            #     st.caption(f"Answered by {res.model} · times the code was run: {res.runs}")
            for step in res.steps:
                st.markdown(f"- {step}")
            # if res.code:
            #     st.caption("Code used")
            #     st.code(res.code, language="python")
            if res.error and not res.ok:
                st.error(res.error)
            if res.result_text:
                st.text(res.result_text)
            if res.stdout.strip():
                st.text(res.stdout.strip())

    render_downloads(msg.get("downloads", {}), idx)


# =============================================================================
# Data panel
# =============================================================================
def render_data_panel() -> None:
    datasets = st.session_state.datasets
    with st.expander(f"Your data: {len(datasets)} table(s)", expanded=not st.session_state.messages):
        st.dataframe(cached("summary", lambda: datasets_summary_frame(datasets)), hide_index=True)

        keys = [d.key for d in datasets]
        chosen = st.selectbox("Look at a table", keys) if len(keys) > 1 else keys[0]
        dataset = next(d for d in datasets if d.key == chosen)

        preview, columns, checks = st.tabs(["Preview", "Columns", "Cleaning notes and checks"])
        with preview:
            st.dataframe(display_frame(dataset.df.head(PREVIEW_ROWS)), hide_index=True)
            if len(dataset.df) > PREVIEW_ROWS:
                st.caption(f"First {PREVIEW_ROWS} of {len(dataset.df):,} rows.")
        with columns:
            st.dataframe(cached(f"cols::{dataset.key}", lambda: column_profile_frame(dataset)), hide_index=True)
            st.caption(f"In answers, this table is the variable `{dataset.alias}`.")
        with checks:
            issues = cached(f"issues::{dataset.key}", lambda: quality_issues(dataset))
            if dataset.notes:
                st.markdown("**What was cleaned automatically**")
                for note in dataset.notes:
                    st.markdown(f"- {note}")
            if issues:
                st.markdown("**Things to be aware of**")
                for issue in issues:
                    st.markdown(f"- {issue}")
            if not dataset.notes and not issues:
                st.write("Nothing to report.")

        if len(datasets) > 1:
            links = cached("links", lambda: find_shared_columns(datasets))
            if links:
                st.markdown("**Columns shared between tables** (how tables can be combined)")
                st.dataframe(pd.DataFrame(links).drop(columns=["kind"]), hide_index=True)


# =============================================================================
# Sidebar
# =============================================================================
def render_sidebar() -> None:
    settings: engine.Settings = st.session_state.settings
    router: engine.LLMRouter = st.session_state.router
    with st.sidebar:
        st.header("📊 Analytics Copilot")
        files = st.file_uploader(
            "Upload CSV or Excel files",
            type=["csv", "xlsx", "xls"],
            accept_multiple_files=True,
            key=f"uploader_{st.session_state.uploader_key}",
        )
        process_upload(files or [])
        for error in st.session_state.load_errors:
            st.warning(error)

        states = [row["state"] for row in router.status()]
        if "ready" in states:
            st.markdown("🟢 AI engine ready")
        elif any(state.startswith("paused") for state in states):
            st.markdown("🟠 AI engine is busy. It will retry shortly.")
        else:
            st.markdown("🔴 AI engine unavailable. Check your setup.")
        for warning in settings.warnings:
            st.caption(f"⚠️ {warning}")

        st.subheader("Session")
        st.button("Clear chat", on_click=clear_chat, disabled=not st.session_state.messages)

        st.button("End session & clear data", on_click=end_session, type="primary",
                  help="Removes your uploaded data and the chat from this app.")

        rows = settings.sample_rows
        with st.expander("Privacy"):
            st.markdown(
                "- Your files are used in this session only and are cleared when you end it.\n"
                "- The AI service receives column names, summary statistics"
                + (f", {rows} sample rows per table" if rows else "")
                + " and the top rows of each result, never the full files.\n"
                "- E-mail addresses and phone numbers are masked."
            )


# =============================================================================
# Main screen
# =============================================================================
def ask(prompt: str) -> None:
    settings: engine.Settings = st.session_state.settings
    router: engine.LLMRouter = st.session_state.router
    st.session_state.messages.append({"role": "user", "text": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    st.iframe(
        """<!DOCTYPE html><html><body><script>
        const doc = window.parent.document;
        setTimeout(() => {
            const messages = doc.querySelectorAll('[data-testid="stChatMessage"]');
            const last = messages[messages.length - 1];
            if (last) last.scrollIntoView({behavior: 'smooth', block: 'start'});
        }, 150);
        </script></body></html>""",
        height=1,
    )
        
    with st.chat_message("assistant"):
        with st.status("Analysing your data...", expanded=True) as status:
            try:
                res = engine.answer_question(
                    prompt,
                    st.session_state.prepared,
                    router,
                    settings,
                    history=st.session_state.history,
                    on_step=lambda step: st.write(f"• {step}"),
                )
            except Exception as exc:  # noqa: BLE001 - never show a traceback to the user
                res = engine.AnswerResult(
                    ok=False,
                    text="Something unexpected went wrong. Try again, or rephrase the question.",
                    error=f"{type(exc).__name__}: {exc}",
                )
            status.update(
                label="Done" if res.ok else "Could not finish",
                state="complete" if res.ok else "error",
                expanded=False,
            )

        message = {"role": "assistant", "text": res.text, "res": res, "downloads": build_downloads(res, prompt)}
        st.session_state.messages.append(message)
        if res.ok:
            st.session_state.history.extend(res.history)
        render_answer(message, len(st.session_state.messages) - 1)


def main() -> None:
    init_state()
    render_sidebar()

    st.title("Analytics Copilot")
    st.caption("Upload spreadsheets, then ask questions in plain English.")

    if st.session_state.notice:
        st.info(st.session_state.notice)
        st.session_state.notice = None

    settings: engine.Settings = st.session_state.settings
    problems = settings.problems()
    if problems:
        st.error(" ".join(problems))
        st.markdown(
            "**To fix:** copy `.env.example` to `.env`, add your API key, save, then restart the app."
        )

    if not st.session_state.datasets:
        st.markdown(
            "**How it works**\n\n"
            "1. Upload one or more CSV or Excel files in the sidebar. Every Excel sheet is read.\n"
            "2. Check the preview and the automatic cleaning notes.\n"
            "3. Ask a question. You get an answer, the tables and charts behind it, and downloads."
        )
    else:
        render_data_panel()

    for idx, message in enumerate(st.session_state.messages):
        with st.chat_message(message["role"]):
            if message["role"] == "user":
                st.markdown(message["text"])
            else:
                render_answer(message, idx)

    ready = bool(st.session_state.datasets) and not problems
    if ready and not st.session_state.messages:
        st.markdown("**Try asking**")
        for number, question in enumerate(STARTER_QUESTIONS):
            st.button(question, key=f"starter_{number}", on_click=use_starter, args=(question,))

    typed = st.chat_input("Ask a question about your data" if ready else "Upload a file first", disabled=not ready)
    prompt = typed or st.session_state.pop("pending_prompt", None)
    st.session_state.pending_prompt = None
    if prompt and ready:
        ask(prompt)


main()
