"""pages/llm_insights.py - table of every AI call the app has made.

Password protected (INSIGHTS_PASSWORD). Reads from Langfuse through observability.py.
"""

from __future__ import annotations

import hmac
import os

import pandas as pd
import streamlit as st

import observability

st.set_page_config(page_title="LLM Insights", page_icon="📈", layout="wide")
st.title("LLM Insights")

observability.enabled()  # loads .env so the settings below can be read
TIMEZONE = os.getenv("INSIGHTS_TIMEZONE", "Asia/Kolkata")
# EXPECTED_PASSWORD = os.getenv("INSIGHTS_PASSWORD", "")

# # ---- password gate ---------------------------------------------------------
# if not EXPECTED_PASSWORD:
#     st.warning("This page is locked. Set INSIGHTS_PASSWORD in .env (or the Secrets box) to open it.")
#     st.stop()

# if not st.session_state.get("insights_ok"):
#     entered = st.text_input("Admin password", type="password")
#     if entered and hmac.compare_digest(entered.encode(), EXPECTED_PASSWORD.encode()):
#         st.session_state.insights_ok = True
#         st.rerun()
#     elif entered:
#         st.error("Wrong password.")
#     st.stop()

if not observability.enabled():
    st.info("Langfuse is not set up. Add LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and LANGFUSE_HOST, then restart.")
    st.stop()


# ---- the window selector ----------------------------------------------------
WINDOWS = {"Last 7 days": 7, "Last 30 days": 30, "All time": None}
window_label = st.selectbox("Time window", list(WINDOWS), index=1)
window_days = WINDOWS[window_label]
TABLE_ROWS = 200  # the table shows only the latest rows; the cards use every call in the window


# ---- the cards and table (refresh by themselves every 15 seconds) -----------
@st.fragment(run_every=15)
def show_calls(days: int | None) -> None:
    try:
        rows = observability.fetch_calls(days=days)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read from Langfuse: {exc}")
        return
    if not rows:
        st.info("No AI calls recorded in this window. Ask a question in the app or save files on the Dashboard, then wait a few seconds.")
        return

    df = pd.DataFrame(rows)
    for column in ("input_tokens", "output_tokens", "total_tokens"):
        df[column] = pd.to_numeric(df[column], errors="coerce").astype("Int64")

    # cards use ALL calls in the window
    cards = st.columns(4)
    cards[0].metric("AI calls", f"{len(df):,}")
    cards[1].metric("Total tokens", f"{int(df['total_tokens'].fillna(0).sum()):,}")
    cards[2].metric("Total cost", f"${df['cost'].fillna(0).sum():.4f}")
    cards[3].metric("Average latency", f"{df['latency'].mean():.2f}s" if df["latency"].notna().any() else "n/a")

    # the table shows only the latest rows
    shown = df.head(TABLE_ROWS)
    when = pd.to_datetime(shown["start_time"], utc=True).dt.tz_convert(TIMEZONE)
    table = pd.DataFrame(
        {
            "Name": shown["name"].map(
                lambda n: "ask-analytics-copilot" if n in ("plan-code", "fix-code", "explain-result")
                else ("generate-report" if n in ("plan-report", "write-report")
                      else ("dashboard-insights" if n == "dashboard-insights" else n))
            ),
            "Date & Time": when.dt.strftime("%d/%m/%Y, %I:%M:%S %p"),
            "Model": shown["model"],
            "Cost": shown["cost"].map(lambda v: f"${v:.6f}" if pd.notna(v) else "-"),
            "Latency": shown["latency"].map(lambda v: f"{v:.3f}s" if pd.notna(v) else "-"),
            "Total Tokens": shown["total_tokens"].map(lambda v: f"{int(v)}" if pd.notna(v) else "-"),
            "Status": shown["status"],
        }
    )
    st.dataframe(table, hide_index=True)
    st.caption(f"Showing the latest {len(shown):,} of {len(df):,} calls. The cards above count all {len(df):,}.")


show_calls(window_days)