"""dashboard_ai.py - AI insight and recommendation for every dashboard ID (plain Python, no Streamlit).

Flow (one AI call per period, then one repair call only if needed):
  1. PAYLOAD   build_payload()  the saved KPIs of each source with their dashboard IDs
                                (SN1, TC1, SP1 ...), in presentation order, plus short
                                summaries of the raw data (summarise_tables()).
  2. ASK       generate()       dashboard_prompts.py wording -> router.chat() -> JSON keyed by ID.
  3. CHECK     every sentence must use only numbers that were in the prompt, must not state a cause,
                                and must not be empty or too long. Failing IDs get ONE repair attempt.
  4. FALLBACK  an ID that still fails gets a plain sentence made from its value, so no box is empty.

The AI never calculates numbers. The numbers on the dashboard always come from kpi_engine.py.
Nothing here stores anything: generate() returns a result, the caller saves it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

import dashboard_prompts as prompts
from insights import CAUSAL_WORDS, _bad_numbers, _numbers, _parse_json

SOURCE_ORDER = ["ServiceNow", "Traackr", "Sprinklr"]  # order of presentation (and of the IDs)
STEP_NAME = "dashboard-insights"  # shown on the LLM Insights page
MAX_TOKENS = 8000                 # about 30 IDs x ~120 tokens, with room to spare
MAX_SENTENCE_CHARS = 500          # longer text is rejected
TOP_N = 5                         # items listed per summary line


# =============================================================================
# 1. Payload
# =============================================================================
def _id_number(dash_id: str) -> int:
    match = re.search(r"\d+", dash_id)
    return int(match.group()) if match else 0


def build_payload(snapshots: dict[str, dict], notes: dict[str, list[str]] | None = None) -> list[dict]:
    """snapshots: source -> saved snapshot. notes: source -> summary lines of the raw data.

    Sources are written in SOURCE_ORDER. Only KPIs that have a dashboard ID and a value are included.
    """
    notes = notes or {}
    payload = []
    for source in SOURCE_ORDER:
        snap = snapshots.get(source)
        if not snap:
            continue
        items = []
        for key, kpi in snap["kpis"].items():
            dash_id = kpi.get("dashboard_id")
            if not dash_id or kpi.get("value") is None:
                continue
            items.append({"id": dash_id, "name": kpi["name"], "unit": kpi["unit"], "value": kpi["value"]})
        items.sort(key=lambda i: _id_number(i["id"]))
        if items:
            payload.append({"source": source, "items": items, "data_notes": notes.get(source, [])})
    return payload


# =============================================================================
# Summaries of the raw data (short, no personal data, only inside the chosen period)
# =============================================================================
def _n(value: Any) -> str:
    return f"{float(value):,.0f}" if float(value).is_integer() or abs(float(value)) >= 100 else f"{float(value):,.2f}"


def _num(series: pd.Series) -> pd.Series:
    """Numbers, also from text such as '$52.63' or '1,200'."""
    if not pd.api.types.is_numeric_dtype(series):
        series = series.astype(str).str.replace(r"[^0-9.\-]", "", regex=True)
    return pd.to_numeric(series, errors="coerce")


def _servicenow(tables: dict[str, pd.DataFrame]) -> list[str]:
    notes: list[str] = []
    main = tables.get("main")
    if main is not None and len(main):
        counts = main["state"].astype(str).value_counts()
        notes.append("Task states: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        weekly = main.dropna(subset=["created"]).set_index("created").resample("W-SUN").size()
        weekly = weekly[weekly > 0]
        if len(weekly):
            notes.append("Tasks created per week (week ending): " + ", ".join(f"{d:%d %b} {v}" for d, v in weekly.items()))
        hours = _num(main["business_duration"]).dropna() / 3600
        if len(hours):
            notes.append(f"Business duration: median {hours.median():.1f} hours, longest {hours.max():.1f} hours.")
    requests = tables.get("requests")
    if requests is not None and len(requests):
        hours = _num(requests["business_duration"]) / 3600
        grouped = pd.DataFrame({"type": requests["request_type"].astype(str), "hours": hours}).groupby("type")["hours"].agg(["size", "mean"])
        grouped = grouped.sort_values("size", ascending=False).head(TOP_N)
        notes.append("Most common request types: " + "; ".join(f"{t} ({int(r['size'])} tasks, {r['mean']:.1f} avg hours)" for t, r in grouped.iterrows()))
    return notes


def _traackr(tables: dict[str, pd.DataFrame]) -> list[str]:
    notes: list[str] = []
    brands = tables.get("brands")
    if brands is not None and len(brands):
        notes.append(f"{len(brands)} brands are listed in the Diageo brands sheet.")
        top = brands.assign(engagements=_num(brands["engagements"])).sort_values("engagements", ascending=False).head(TOP_N)
        notes.append("Top brands by engagements (posts, video views, est. impressions): " + "; ".join(
            f"{r['brand']} ({_n(r['engagements'])} engagements, {_n(_num(pd.Series([r['posts']])).iloc[0])} posts, "
            f"{_n(_num(pd.Series([r['video_views']])).iloc[0])} video views, {_n(_num(pd.Series([r['est_impressions']])).iloc[0])} est. impressions)"
            for _, r in top.iterrows()))
        posts = brands.assign(posts=_num(brands["posts"])).sort_values("posts", ascending=False).head(TOP_N)
        notes.append("Brands with the most posts: " + ", ".join(f"{r['brand']} ({_n(r['posts'])})" for _, r in posts.iterrows()))
    campaigns = tables.get("campaigns")
    if campaigns is not None and len(campaigns):
        spend = _num(campaigns["total_spend"])
        with_spend = int((spend.fillna(0) > 0).sum())
        notes.append(f"{len(campaigns)} campaigns started in the period; {with_spend} have a recorded spend.")
        by_brand = campaigns.assign(spend=spend.fillna(0)).groupby(campaigns["brand"].astype(str))["spend"].agg(["size", "sum"])
        by_brand = by_brand.sort_values("size", ascending=False).head(TOP_N)
        notes.append("Campaigns by brand (count, total spend): " + "; ".join(f"{b} ({int(r['size'])}, {_n(r['sum'])})" for b, r in by_brand.iterrows()))
    return notes


def _sprinklr(tables: dict[str, pd.DataFrame]) -> list[str]:
    notes: list[str] = []
    organic = tables.get("organic")
    if organic is not None and len(organic):
        frame = pd.DataFrame({
            "type": organic["account_type"].astype(str),
            "eng": _num(organic["engagements"]),
            "reach": _num(organic["reach"]),
        })
        grouped = frame.groupby("type").agg(posts=("eng", "size"), eng=("eng", "sum"), reach=("reach", "sum")).sort_values("posts", ascending=False)
        notes.append("Organic posts by account type (posts, engagements, reach): " + "; ".join(
            f"{t} ({int(r['posts'])}, {_n(r['eng'])}, {_n(r['reach'])})" for t, r in grouped.head(TOP_N).iterrows()))
        if "status" in organic.columns:
            notes.append("Organic post status: " + ", ".join(f"{k} {v}" for k, v in organic["status"].astype(str).value_counts().head(TOP_N).items()))
    paid = tables.get("paid")
    if paid is not None and len(paid):
        spend = _num(paid["spend"]).fillna(0)
        notes.append(f"Paid: {len(paid):,} ad rows, {int((spend > 0).sum()):,} of them with spend above zero.")
    return notes


_SUMMARISERS = {"ServiceNow": _servicenow, "Traackr": _traackr, "Sprinklr": _sprinklr}


def summarise_tables(source: str, tables: dict[str, pd.DataFrame]) -> list[str]:
    """Short summary lines of one source's raw data (tables = sources.apply_period output).

    These lines are all the AI sees of the raw data. It never receives rows. A failure here
    returns what was built so far, because the summaries are an extra, not a requirement.
    """
    builder = _SUMMARISERS.get(source)
    if builder is None:
        return []
    try:
        return builder(tables)
    except Exception:  # noqa: BLE001 - the KPIs alone are enough to continue
        return []


# =============================================================================
# 2-4. Ask, check, fall back
# =============================================================================
@dataclass
class Entry:
    insight: str
    recommendation: str
    by: str = "ai"  # "ai" or "fallback"


@dataclass
class AIResult:
    entries: dict[str, Entry] = field(default_factory=dict)  # dashboard ID -> text
    model: str | None = None
    fallbacks: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def ai_used(self) -> bool:
        return any(e.by == "ai" for e in self.entries.values())

    def to_dict(self) -> dict:
        """JSON-safe form, ready to be saved next to the snapshot."""
        return {
            "model": self.model,
            "fallbacks": self.fallbacks,
            "notes": self.notes,
            "entries": {k: {"insight": v.insight, "recommendation": v.recommendation, "by": v.by} for k, v in self.entries.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AIResult":
        entries = {k: Entry(v["insight"], v["recommendation"], v.get("by", "ai")) for k, v in (data.get("entries") or {}).items()}
        return cls(entries=entries, model=data.get("model"), fallbacks=data.get("fallbacks", 0), notes=data.get("notes", []))


def make_router():
    """A model router built from .env, for pages that did not get one from the main page."""
    import engine

    return engine.LLMRouter(engine.Settings.from_env())


def _field(entry: Any, *names: str) -> str:
    if not isinstance(entry, dict):
        return ""
    lowered = {str(k).strip().lower().replace(" ", "_"): v for k, v in entry.items()}
    for name in names:
        value = lowered.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _problems(entry: Any, allowed: set[float]) -> list[str]:
    insight = _field(entry, "insight")
    recommendation = _field(entry, "recommendation", "business_recommendation")
    problems = []
    if not insight:
        problems.append("insight is missing")
    if not recommendation:
        problems.append("recommendation is missing")
    for label, text in (("insight", insight), ("recommendation", recommendation)):
        if not text:
            continue
        if len(text) > MAX_SENTENCE_CHARS:
            problems.append(f"{label} is too long")
        bad = _bad_numbers(text, allowed)
        if bad:
            problems.append(f"{label} uses numbers that were not given: {', '.join(bad)}")
        if CAUSAL_WORDS.search(text):
            problems.append(f"{label} states a cause")
    return problems


def _fallback(item: dict) -> Entry:
    shown = prompts._fmt_value(item.get("value"), item.get("unit", ""))
    return Entry(insight=f"{item['name']} is {shown}.", recommendation="No recommendation was generated for this item.", by="fallback")


def _trace(router: Any) -> None:
    try:
        import observability

        router.trace_id = observability.new_trace_id()
    except Exception:  # noqa: BLE001 - tracing is optional
        pass


def _ask(router: Any, messages: list[dict]) -> tuple[Any, str | None]:
    """One AI call. Returns (parsed JSON or None, model label)."""
    reply = router.chat(messages, temperature=0.2, max_tokens=MAX_TOKENS, step=STEP_NAME)
    model = getattr(getattr(reply, "model", None), "label", None)
    try:
        return _parse_json(reply.text), model, reply.text
    except (ValueError, TypeError):
        return None, model, reply.text


def generate(payload: list[dict], router: Any, period: tuple[str, str] | None = None) -> AIResult:
    """Ask the AI for an insight and a recommendation per dashboard ID. Never raises."""
    items = {item["id"]: item for block in payload for item in block["items"]}
    result = AIResult()
    if not items:
        result.notes.append("There were no KPIs to write about.")
        return result

    messages = prompts.build_messages(payload, period)
    prompt_text = messages[1]["content"]
    allowed = {value for _, value, _, _ in _numbers(prompt_text)}
    parsed: Any = None
    raw = ""
    try:
        _trace(router)
        parsed, result.model, raw = _ask(router, messages)
    except Exception as exc:  # noqa: BLE001 - the dashboard must still work without the AI
        result.notes.append(f"The AI could not be reached: {exc}")

    answers = parsed if isinstance(parsed, dict) else {}
    accepted: dict[str, Entry] = {}
    problems: dict[str, str] = {}
    for dash_id in items:
        entry = answers.get(dash_id)
        found = _problems(entry, allowed)
        if found:
            problems[dash_id] = "; ".join(found)
        else:
            accepted[dash_id] = Entry(_field(entry, "insight"), _field(entry, "recommendation", "business_recommendation"))

    # One repair attempt, only for the IDs that failed, and only if the first answer was usable at all.
    if problems and parsed is not None:
        try:
            repair_messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": prompts.build_repair_prompt(problems)},
            ]
            fixed, model, _ = _ask(router, repair_messages)
            result.model = model or result.model
            if isinstance(fixed, dict):
                for dash_id in list(problems):
                    entry = fixed.get(dash_id)
                    if entry is not None and not _problems(entry, allowed):
                        accepted[dash_id] = Entry(_field(entry, "insight"), _field(entry, "recommendation", "business_recommendation"))
                        del problems[dash_id]
        except Exception as exc:  # noqa: BLE001
            result.notes.append(f"The repair attempt failed: {exc}")

    if parsed is None and not result.notes:
        result.notes.append("The AI reply could not be read as JSON.")

    for dash_id, item in items.items():  # keep presentation order
        if dash_id in accepted:
            result.entries[dash_id] = accepted[dash_id]
        else:
            result.entries[dash_id] = _fallback(item)
            result.fallbacks += 1
    if result.fallbacks:
        result.notes.append(f"{result.fallbacks} item(s) use plain wording because the AI text failed a check.")
    return result


def to_json(result: AIResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False)