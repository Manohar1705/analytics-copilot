"""dashboard_prompts.py - every piece of wording the dashboard sends to the AI.

Edit the text here to change how the insights and recommendations read.
Nothing in this file calls the AI or touches data; dashboard_ai.py imports these and does the work.

One AI call per upload:
  INPUT  - for each source (ServiceNow, Traackr, Sprinklr, in that order) the calculated KPIs
           with their dashboard IDs (SN1, TC1, SP1 ...) and small summaries of the raw data.
  OUTPUT - JSON keyed by dashboard ID, each with an "insight" and a "recommendation".

The AI never calculates the numbers. It only writes about the numbers it is given.

The payload given to build_user_prompt() looks like this (built by dashboard_ai.py):

    [
      {
        "source": "ServiceNow",
        "items": [
          {"id": "SN1", "name": "Total tasks", "unit": "count", "value": 106},
          {"id": "SN4", "name": "Tasks by SLA type", "unit": "count",
           "value": {"OverAll_Diageo_P4_SLA": 98, "OverAll_Diageo_P3_SLA": 8}},
        ],
        "data_notes": ["Top request types in the period: ...", ...],   # summaries of the raw data
      },
      ...
    ]
"""

from __future__ import annotations

import json

# ---- Limits (change here) -------------------------------------------------------
MAX_INSIGHT_SENTENCES = 3
MAX_RECOMMENDATION_SENTENCES = 2
MAX_BREAKDOWN_ITEMS = 10  # categories shown to the AI for a breakdown KPI

# ---- Business context (edit freely: this is the "Diageo context" part) -----------
BUSINESS_CONTEXT = """\
Diageo is a global alcoholic beverages company. Its brands include Guinness, Johnnie Walker, \
Smirnoff, Baileys, Don Julio, Tanqueray, Captain Morgan and many others. The people reading this \
dashboard are on the central social media and digital team. They support brand and market teams \
around the world and want to know what is going well, what needs attention and what to do next.

You will receive results from three systems:

- ServiceNow: the service desk the markets and brand teams use to raise requests to the social \
media team (for example access requests, reconnecting accounts, editing or hiding posts). Tasks \
have an SLA type, a completion state and a business duration. It shows workload and how well \
requests are served.
- Traackr: influencer marketing. It shows how many posts creators published for Diageo brands, \
the engagement and reach those posts earned, brand scores (vitality, visibility, impact, trust) \
and campaign activity and spend.
- Sprinklr: the brands' own social media accounts (organic posts) and paid advertising. It shows \
engagement by account type, ad spend, impressions and the cost of reaching people (CPM).\
"""

# ---- System prompt -------------------------------------------------------------
SYSTEM_PROMPT = f"""\
You are a senior marketing analyst writing short commentary for a monthly KPI dashboard.

{BUSINESS_CONTEXT}

Your job: for every dashboard ID you are given (for example SN1, TC1, SP1), write
1. an "insight": what this number says, using the value itself and, where useful, the data \
summaries provided for that source. At most {MAX_INSIGHT_SENTENCES} short sentences.
2. a "recommendation": one practical next step for the team, based on the insight. At most \
{MAX_RECOMMENDATION_SENTENCES} short sentences.

Rules you must follow:
- Use only the numbers you are given. Never calculate new totals, percentages or averages, and never \
invent a number, a brand, a market or a date. Quote a number exactly as given (you may round it \
and use words such as "about").
- Do not state causes as facts. Do not write "because", "due to" or "driven by". You may say \
something "may be worth checking".
- Do not guess targets or benchmarks that you were not given. If a number is neither good nor \
bad without a target, describe what it is and recommend how to track or investigate it.
- Write plain business English for a non-technical reader. No jargon, no hype, no bullet points \
inside a sentence.
- If an item has no value or nothing useful can be said, write a short neutral insight and \
recommend checking the data. Do not leave it empty.
- Each ID must be written about on its own. Do not repeat the same sentence for several IDs.

Output format: return ONLY valid JSON, with no text before or after it and no markdown fences. \
Use exactly the dashboard IDs you were given as the keys:

{{
  "SN1": {{"insight": "...", "recommendation": "..."}},
  "TC1": {{"insight": "...", "recommendation": "..."}}
}}
"""


# ---- Turning the payload into text -----------------------------------------------
def _fmt_number(value: float) -> str:
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _fmt_value(value, unit: str) -> str:
    """One KPI value as short text. Breakdowns become 'name: value, name: value'."""
    if value is None:
        return "no value"
    if isinstance(value, dict):
        items = [(k, v) for k, v in value.items() if v is not None]
        items.sort(key=lambda kv: kv[1], reverse=True)
        shown = ", ".join(f"{k}: {_fmt_number(v)}" for k, v in items[:MAX_BREAKDOWN_ITEMS])
        more = len(items) - MAX_BREAKDOWN_ITEMS
        return shown + (f" (and {more} more)" if more > 0 else "")
    if isinstance(value, (int, float)):
        suffix = {"%": "%", "hours": " hours", "currency": " USD"}.get(unit, "")
        return f"{_fmt_number(value)}{suffix}"
    return str(value)


def build_user_prompt(payload: list[dict], period: tuple[str, str] | None = None) -> str:
    """The data part of the prompt. Sources are written in the order they are given."""
    lines: list[str] = []
    if period:
        lines.append(f"Reporting period: {period[0]} to {period[1]}.")
        lines.append("")
    lines.append("Here are this period's calculated results. Write an insight and a recommendation for every ID.")
    for block in payload:
        lines.append("")
        lines.append(f"{block['source']}")
        for item in block["items"]:
            lines.append(f"{item['id']} - {item['name']}: {_fmt_value(item.get('value'), item.get('unit', ''))}")
        notes = block.get("data_notes") or []
        if notes:
            lines.append(f"Extra detail from the {block['source']} data (for context only):")
            lines.extend(f"- {note}" for note in notes)
    ids = [item["id"] for block in payload for item in block["items"]]
    lines.append("")
    lines.append("Return JSON with exactly these keys: " + ", ".join(ids) + ".")
    return "\n".join(lines)


def build_messages(payload: list[dict], period: tuple[str, str] | None = None) -> list[dict]:
    """Chat messages ready for the model router."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(payload, period)},
    ]


# ---- Repair (used once when the first answer has problems) ---------------------------
def build_repair_prompt(problems: dict[str, str]) -> str:
    """problems: dashboard ID -> what was wrong (missing, number not in the data, causal wording ...)."""
    listed = "\n".join(f"- {dash_id}: {reason}" for dash_id, reason in problems.items())
    return (
        "Some items in your answer need fixing:\n"
        f"{listed}\n\n"
        "Return ONLY valid JSON containing just these IDs, each with \"insight\" and \"recommendation\", "
        "following the same rules as before. Use only the numbers you were given."
    )


def example_output() -> str:
    """A tiny example of the expected reply. Handy for tests."""
    return json.dumps(
        {"SN1": {"insight": "...", "recommendation": "..."}, "TC1": {"insight": "...", "recommendation": "..."}},
        indent=2,
    )