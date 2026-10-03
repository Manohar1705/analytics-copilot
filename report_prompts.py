"""report_prompts.py - every piece of wording the report builder sends to the AI.

Edit the text here to change how reports read. Nothing in this file calls the AI or
touches data; report_engine.py imports these and does the work.

Two AI calls:
  1. PLAN  - sees only the profile of the ticked files, returns a slide plan (JSON spec).
  2. WRITE - sees the numbers Python computed for each slide, returns insights and
             recommendations (JSON).
The AI never does maths and never writes code in either call.
"""

from __future__ import annotations

import json
from typing import Any

# ---------------------------------------------------------------------------
# Limits shared with report_engine.py
# ---------------------------------------------------------------------------
AGGREGATIONS = ["count", "sum", "mean", "median", "min", "max", "nunique"]
FREQUENCIES = ["week", "month", "quarter", "year"]
SLIDE_TYPES = ["kpis", "bar", "line", "donut", "table"]

MIN_SLIDES_ONE_FILE = 4  # content slides, not counting cover and closing recommendations
MAX_SLIDES_ONE_FILE = 5
MAX_SLIDES_PER_EXTRA_FILE = 3
MAX_SLIDES_TOTAL = 14


def slide_budget(file_count: int) -> tuple[int, int]:
    """(minimum, maximum) planned content slides for this many ticked files."""
    n = max(1, file_count)
    low = MIN_SLIDES_ONE_FILE
    high = min(MAX_SLIDES_TOTAL, MAX_SLIDES_ONE_FILE + MAX_SLIDES_PER_EXTRA_FILE * (n - 1))
    return low, high


# ---------------------------------------------------------------------------
# Call 1: PLAN
# ---------------------------------------------------------------------------
PLAN_SYSTEM_PROMPT = """You plan a business report deck from spreadsheet data.
You are given a profile of the data files (tables, columns, types, a few sample rows).
You do NOT calculate anything and you do NOT write code. You return a JSON plan; the
application validates it, computes every number with pandas and draws the charts.

OUTPUT
Return ONLY one JSON object, no markdown fences, no commentary:
{"slides": [ <slide>, <slide>, ... ]}

Every slide has: "type", "title", "dataset".
- "title" is a short, specific headline (max 70 characters) that names what the slide
  shows, for example "Revenue by region" or "Open requests per month".
  Do not put conclusions or numbers in a title, because the numbers are not computed yet.
- "dataset" is the table alias exactly as shown in the profile.

A "metric" is {"column": <column name or null>, "agg": <one of %(aggs)s>, "label": <short name>}.
Use "column": null only with "agg": "count" (counts rows). Use "sum", "mean", "median",
"min", "max" only on numeric columns. Use "nunique" for distinct counts.

SLIDE TYPES
1. kpis - headline numbers (use at most once, as the first slide).
   {"type":"kpis","title":...,"dataset":...,
    "kpis":[{"label":"Total engagements","column":null,"agg":"count"}, ...]}
   4 to 8 kpis. Each kpi may add "dataset" to override the slide's table.

2. bar - compare categories.
   {"type":"bar","title":...,"dataset":...,"category":<column>,
    "metrics":[<metric>, ...],      # 1 to 3 metrics
    "top_n":10,"sort":"desc",       # sort: "desc" | "asc"
    "horizontal":true,"stacked":false}

3. line - trend over time. ONLY if the table has a date column.
   {"type":"line","title":...,"dataset":...,"date_column":<column>,
    "freq":"month",                 # one of %(freqs)s
    "metrics":[<metric>, ...]}      # 1 to 3 metrics

4. donut - share of a total across at most 6 categories.
   {"type":"donut","title":...,"dataset":...,"category":<column>,
    "metric":<metric>,"top_n":6}    # metric agg must be "count" or "sum"

5. table - the detail behind a point.
   {"type":"table","title":...,"dataset":...,
    "group_by":<column or null>,    # null = show raw rows
    "columns":[<column>, ...],      # raw rows: columns to show (max 7)
    "metrics":[<metric>, ...],      # grouped: aggregated columns (max 5)
    "sort_by":<column or metric label>,"descending":true,"limit":10}

RULES
- Use ONLY table aliases and column names that appear in the profile, spelled exactly.
- Never invent a column. If the data has no date column, plan no line slide.
- Plan %(low)d to %(high)d content slides in total. Do not plan a cover slide or a final
  recommendations slide; the application adds those.
- Start with one kpis slide, then charts, then at most one table slide.
- Cover the data broadly: with several files give each file its own slides; with one file
  vary the angle (a category breakdown, a trend, a share, a detail table).
- Never repeat the same category and metric on two slides.
- Prefer columns that carry business meaning (status, region, owner, amount, date) over
  ids, free text or columns that are mostly empty.
- Skip columns the profile marks as masked or sensitive.
- Do not plan joins or calculations across files. Each slide uses one table.
- Do not plan chart types other than those listed.""" % {
    "aggs": ", ".join(f'"{a}"' for a in AGGREGATIONS),
    "freqs": ", ".join(f'"{f}"' for f in FREQUENCIES),
    "low": MIN_SLIDES_ONE_FILE,
    "high": MAX_SLIDES_ONE_FILE,
}  # the real budget is injected per request in build_plan_messages()


def build_plan_messages(
    profile_text: str,
    title: str,
    period: str,
    file_count: int,
    repair_note: str | None = None,
) -> list[dict[str, str]]:
    """Messages for the PLAN call. repair_note carries validation errors on a second try."""
    low, high = slide_budget(file_count)
    system = PLAN_SYSTEM_PROMPT.replace(
        f"Plan {MIN_SLIDES_ONE_FILE} to {MAX_SLIDES_ONE_FILE} content slides",
        f"Plan {low} to {high} content slides",
    )
    user = (
        f"Report title: {title or 'Report'}\n"
        f"Period: {period or 'not given'}\n"
        f"Number of ticked files: {file_count}\n\n"
        f"DATA PROFILE\n{profile_text}\n\n"
        "Return the JSON plan now."
    )
    if repair_note:
        user += (
            "\n\nYour previous plan had these problems. Fix them and return the full "
            f"corrected plan:\n{repair_note}"
        )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ---------------------------------------------------------------------------
# Call 2: WRITE
# ---------------------------------------------------------------------------
WRITE_SYSTEM_PROMPT = """You write the commentary for a business report deck.
You are given the slides of the report. Each slide has its table, some notes and a list of
"facts" that were already calculated for you. Write insights and recommendations using ONLY
the numbers and facts you were given. You do not calculate anything.

OUTPUT
Return ONLY one JSON object, no markdown fences, no commentary:
{"slides":[{"id":<slide id>,"insights":[<string>, ...],"recommendation":<string>}, ...],
 "summary":[{"heading":<string>,"text":<string>}, ...]}

Include every slide id you were given, once each.

INSIGHTS (per slide)
- Chart slides (bar, line, donut): write 5 insights; 4 if the data truly supports no more.
  Headline-numbers slide: 3 or 4. Table slide: 3 or 4.
- One sentence each, 15 to 32 words, plain business English.
- Each insight has two parts: a FINDING taken from the data, and what that finding means for
  how the business is spread out or exposed (for example concentrated, uneven, balanced,
  dependent on a few values, a long tail, a sharp swing). Describe the pattern; never give a cause.
- Build the insights from the "facts" list first (ties, gaps, averages, medians, how many values
  are above average, the share held by the top 3, first-half against second-half, periods at 0).
  Use the table only to name the leader and the lowest value. The reader can already see every
  bar, so never write an insight that only repeats one row of the table.
- Every insight takes a different angle and uses a different fact. Do not repeat a number.
- If a note says that values were merged or cut off, say so once, in the insight where it matters.

RECOMMENDATION (per slide)
- Two sentences, three at most. The first word of the first sentence is an action verb such as
  Compare, Check, Review, Confirm, Prioritise, Investigate, Track.
- Write it as a CHECK for the team, not as a claim. Name the specific items and their numbers,
  then say what to find out, for example "Compare <item A> (<number>) with <item B> (<number>)
  and confirm whether the gap is intended."
- Only recommend what can be done with this data: compare, review, confirm, correct the data,
  prioritise by size, track over time. Never recommend actions that need information you were
  not given, such as hiring, pricing, budgets, campaigns, targets or customer retention.
- If a note says that spellings were merged, make one recommendation about standardising that
  column in the source file.

SUMMARY (closing slide)
- 4 to 6 items, one per theme (for example workload balance, concentration by category,
  trend over time, data quality). Each item must be different from the others.
- "heading" is 2 to 5 words. "text" is two sentences: first the pattern across the slides with
  its key number, then the specific check to carry out.
- Cover the recommendation of every slide in at least one item. If there are fewer than 4
  themes, split a theme by slide rather than invent one.

NUMBERS - STRICT
- Use only numbers that appear in the slide's table, notes or facts. Copy them exactly as
  written, with the same rounding. Do not round further, convert units or add up new totals.
- A percentage is allowed only if it is written in the data ("share of total (%)" column or a fact).
  Never turn a count into a percentage yourself.
- Every number must say what it counts and which item it belongs to. Check that the number
  really belongs to that item before you write it ("South has 9", not "South has 1").
- Use the singular for 1 ("1 Client") and the plural otherwise ("8 Clients").
- If a slide has little data, write fewer insights. Never pad.

NO INVENTED CONTEXT
- Do not explain causes ("because of", "due to", "driven by"). Do not mention events, seasons,
  campaigns, competitors, targets, benchmarks or industry norms. Describe what the data shows,
  not why.
- Never use a business word that is not in the column names or values of the data. For
  example do not write retention, churn, revenue, profit, satisfaction or conversion unless a
  column or value says so.
- Never write a field name or a heading with an underscore. Write "share of total", not
  "share_pct".
- Do not claim a trend from fewer than 3 periods. Do not call something good or bad unless the
  numbers make that unambiguous; use words like higher, lower, concentrated, uneven.
- Words such as "significant" or "dramatic" need a clearly large gap in the numbers.
- If values are missing or truncated (a note says so), do not draw conclusions from them.

STYLE
- No emojis, no markdown, no bullet characters, no slide numbers.
- Do not start every insight the same way.
- Use the column and category names from the data, not your own labels.
- Write in the language of the column names and values."""


def build_write_messages(
    title: str,
    period: str,
    computed_slides: list[dict[str, Any]],
    repair_note: str | None = None,
) -> list[dict[str, str]]:
    """Messages for the WRITE call.

    computed_slides: one dict per slide, already reduced by report_engine to what the AI
    may see, for example
      {"id": 2, "type": "bar", "title": "...", "source": "clients.xlsx",
       "columns": ["Region", "Revenue"], "rows": [["North", "1,240"], ...],
       "notes": ["Top 10 of 23 regions shown"]}
    """
    payload = json.dumps(computed_slides, ensure_ascii=False, default=str, indent=1)
    user = (
        f"Report title: {title or 'Report'}\n"
        f"Period: {period or 'not given'}\n\n"
        f"COMPUTED SLIDES\n{payload}\n\n"
        "Write the commentary now."
    )
    if repair_note:
        user += (
            "\n\nYour previous answer had these problems. Fix them and return the full "
            f"corrected JSON:\n{repair_note}"
        )
    return [{"role": "system", "content": WRITE_SYSTEM_PROMPT}, {"role": "user", "content": user}]


def number_problem_note(problems: list[str]) -> str:
    """Repair note when the checks find figures, words or gaps that the data does not support."""
    lines = "\n".join(f"- {p}" for p in problems[:12])
    return (
        "Your text has these problems. Remove or rewrite the affected sentences using only "
        "the numbers, facts and words in the data, and fix any missing items, then return the "
        f"full corrected JSON:\n{lines}"
    )