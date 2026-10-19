"""insights.py - business insights and recommendations from SAVED KPI snapshots (no Streamlit).

Three layers, each safe on its own:

  17. FACTS  - plain Python reads the saved KPIs (and the comparison with the previous period)
               and writes one factual sentence per finding. Every number comes from the snapshot.
  18. RULES  - plain Python decides which facts are worth flagging (a big change, a missing
               target, one value dominating a breakdown). All limits are in the CONFIG block.
  19. WORDING - optional. An AI model may reword each fact as a smoother sentence, but it only
               sees the fact sentences and every reworded sentence is checked:
               - every number must already be in that same fact (rounding is allowed),
               - no causes ("because", "driven by"), no invented business words or names,
               - the direction (rose / fell) must not be reversed.
               A sentence that fails any check is replaced by the plain fact sentence, so the
               panel is never wrong and never empty. If the AI is unavailable, the plain
               sentences are used.

Main entry point:  generate(current, previous, router=None) -> InsightResult
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from typing import Any

import comparison

# =============================================================================
# CONFIG - change the limits here (your lead can adjust them later)
# =============================================================================
CHANGE_FLAG_PCT = 10.0        # a count / currency / hours / score KPI is flagged when it moves this % or more
POINT_FLAG = 3.0              # a % KPI (e.g. completion rate) is flagged when it moves this many points or more
CATEGORY_FLAG_PCT = 25.0      # a category inside a breakdown is flagged when it moves this % or more ...
CATEGORY_MIN_BASE = 5         # ... and its previous value was at least this big (ignores tiny bases)
CONCENTRATION_PCT = 90.0      # a breakdown is flagged when one value holds this share of the total or more
MAX_INSIGHTS = 8              # most insights shown per source
STAT_MIN_VALUES = 5           # a breakdown needs at least this many values for the top-3 / average / gap sentences
TOP_N = 3                     # how many top values the "top N hold X%" sentence counts
MAX_RECOMMENDATIONS = 8       # most recommendations shown per source
INSIGHT_MAX_TOKENS = 1800

# Limits per KPI key. Empty until your lead gives real values. Example:
#   "sn_completion_rate": {"min": 90},   "sn_avg_duration": {"max": 4},
THRESHOLDS: dict[str, dict[str, float]] = {}
# Ratios between two saved KPIs: (top KPI key, bottom KPI key, "pct" or "ratio", sentence).
# {value} is the result; {num} and {den} are the two KPI values; {num_name} and {den_name} their names.
DERIVED = [
    ("sn_cancelled_tasks", "sn_total_tasks", "pct", "{num_name} are {value} of {den_name} ({num} of {den}).",
     "Review the cancelled tasks to see whether the share can be brought down."),
    ("tr_total_engagements", "tr_total_posts", "ratio", "Each of the {den} posts drew {value} engagements on average ({num} in total).",
     "Track engagements per post in the next period to see whether content performance is improving."),
    ("tr_total_video_views", "tr_total_impressions", "pct", "{num_name} equal {value} of {den_name} ({num} of {den}).",
     "Track the video view share of impressions in the next period."),
    ("sp_total_ad_spend", "sp_paid_engagements", "ratio", "Ad spend of {num} over {den} paid engagements is {value} per engagement.",
     "Compare the cost per paid engagement with the next period to judge paid efficiency."),
]

# +1 = higher is better, -1 = lower is better. Not listed = neutral (no good or bad).
POLARITY: dict[str, int] = {
    "sn_completion_rate": +1,
    "sn_cancelled_tasks": -1,
    "sn_avg_duration": -1,
    "sp_cpm": -1,
}

# =============================================================================
# Result types
# =============================================================================
@dataclass
class Fact:
    id: str
    kpi: str
    kind: str                  # "target" | "change" | "category" | "concentration" | "lead"
    text: str                  # the plain sentence (also the fallback insight)
    flagged: bool = False
    direction: str | None = None   # "up" | "down" | None
    advice: str = ""           # plain recommendation sentence, "" when none is needed
    priority: float = 0.0      # bigger = shown first


@dataclass
class Line:
    """One insight or recommendation, ready to show."""
    text: str
    kpi: str
    flagged: bool = False
    from_ai: bool = False
    fact_id: str = ""      # which fact this line came from (the report uses it to avoid repeats)


@dataclass
class InsightResult:
    source: str
    insights: list[Line] = field(default_factory=list)
    recommendations: list[Line] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # caveats: partial data, different period lengths
    ai_used: bool = False
    model: str | None = None
    fallbacks: int = 0                               # sentences replaced by the plain version


# =============================================================================
# Small helpers
# =============================================================================
def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _fmt(value: Any, unit: str) -> str:
    """Same look as the dashboard cards."""
    if value is None:
        return "n/a"
    if not _is_number(value):
        return str(value)
    if unit == "%":
        return f"{value:,.2f}%"
    if unit == "count":
        return f"{value:,.0f}"
    if unit == "currency":
        return f"{value:,.2f}"
    if unit == "hours":
        return f"{value:,.2f} hours"
    return f"{value:,.2f} {unit}".strip()


def _signed(value: float, unit: str) -> str:
    if unit == "count":
        return f"{value:+,.0f}"
    return f"{value:+,.2f}"


def cache_key(current: dict, previous: dict | None) -> str:
    """Changes when either snapshot is saved again, so the dashboard can reuse the result."""
    def part(snap: dict | None) -> str:
        return "-" if not snap else f"{snap.get('source')}|{snap.get('period_start')}|{snap.get('period_end')}|{snap.get('saved_at')}"
    return part(current) + "||" + part(previous)


def _plain_rows(snapshot: dict) -> list[dict]:
    """Rows without a comparison, in the same shape comparison.py returns."""
    rows = []
    for key, kpi in snapshot["kpis"].items():
        rows.append({
            "key": key, "name": kpi.get("name", key), "unit": kpi.get("unit", ""), "kind": kpi.get("kind", "single"),
            "current": kpi.get("value"), "previous": None, "change": None, "pct_change": None,
        })
    return rows


# =============================================================================
# 17 + 18. FACTS and RULES
# =============================================================================
def build_facts(current: dict, previous: dict | None) -> tuple[list[Fact], list[str]]:
    """Return (facts sorted most important first, notes about caveats)."""
    notes: list[str] = []
    rows = None
    if previous:
        comp = comparison.compare_snapshots(current, previous)
        notes += comp.warnings + comp.errors
        rows = comp.rows if comp.ok else None
    comparing = rows is not None
    rows = rows if comparing else _plain_rows(current)
    if not comparing:
        notes.append("No previous period is selected, so no changes are described. Choose one in 'Compare with'.")

    partial = bool(comparison.partial_note(current)) or bool(previous and comparison.partial_note(previous))

    facts: list[Fact] = []
    steady: list[str] = []

    def add(fact: Fact) -> None:
        facts.append(fact)

    for row in rows:
        key, name, unit = row["key"], row["name"], row["unit"]
        polarity = POLARITY.get(key, 0)

        # ---- targets (work with or without a comparison) --------------------
        limits = THRESHOLDS.get(key)
        value = row["current"]
        if limits and _is_number(value):
            if "min" in limits and value < limits["min"]:
                add(Fact("", key, "target", f"{name} is {_fmt(value, unit)}, below the set minimum of {_fmt(limits['min'], unit)}.",
                         True, "down", f"Review {name}, which is below its set minimum.", 100))
            if "max" in limits and value > limits["max"]:
                add(Fact("", key, "target", f"{name} is {_fmt(value, unit)}, above the set maximum of {_fmt(limits['max'], unit)}.",
                         True, "up", f"Review {name}, which is above its set maximum.", 100))

        # ---- single numbers: change against the previous period -------------
        if row["kind"] != "breakdown":
            if not (comparing and _is_number(row.get("change")) and _is_number(row.get("current")) and _is_number(row.get("previous"))):
                continue
            change, pct = row["change"], row.get("pct_change")
            if unit == "%":
                flagged, size = abs(change) >= POINT_FLAG, abs(change)
                delta = f"({change:+.2f} pts)"
            else:
                if pct is None:
                    flagged, size = (row["previous"] == 0 and row["current"] != 0), 100.0
                    delta = f"({_signed(change, unit)})"
                else:
                    flagged, size = abs(pct) >= CHANGE_FLAG_PCT, abs(pct)
                    delta = f"({pct:+.1f}%)"
            if change == 0 or not flagged:
                steady.append(name)
                continue
            direction = "up" if change > 0 else "down"
            verb = "rose" if direction == "up" else "fell"
            text = f"{name} {verb} from {_fmt(row['previous'], unit)} to {_fmt(row['current'], unit)} {delta}."
            partial_here = partial and unit in ("count", "currency")
            if partial_here:
                text += " This period has only partial data."
            worse = (polarity > 0 and direction == "down") or (polarity < 0 and direction == "up")
            if partial_here:
                advice = ""  # a partial period cannot support a conclusion
            elif worse:
                advice = f"Review {name}: it moved the wrong way against the previous period."
            elif polarity == 0:
                advice = f"Check what changed in {name} compared with the previous period."
            else:
                advice = f"{name} moved in the right direction against the previous period; keep the current approach and confirm it holds next period."
            add(Fact("", key, "change", text, True, direction, advice, 50 + min(size, 200) / 4 + (10 if worse else 0)))
            continue

        # ---- breakdowns -----------------------------------------------------
        values = {k: v for k, v in (row["current"] or {}).items() if _is_number(v)}
        if not values:
            continue
        positives = {k: v for k, v in values.items() if v > 0}
        top_label = max(values, key=values.get)
        low_label = min(values, key=values.get)

        if unit == "count" and positives:
            total = sum(positives.values())
            share = positives.get(top_label, 0) / total * 100 if total else 0
            if share >= CONCENTRATION_PCT:
                add(Fact("", key, "concentration",
                         f"{top_label} holds {share:.1f}% of {name} ({_fmt(values[top_label], unit)} of {_fmt(total, unit)}).",
                         True, None, f"Check whether the spread of {name} is intended, because one value holds most of the total.", 40))
            else:
                add(Fact("", key, "lead",
                         f"{top_label} is the largest in {name} with {_fmt(values[top_label], unit)} ({share:.1f}% of the total).",
                         False, None, f"Track whether {top_label} keeps its lead in {name} in the next period.", 10))
        elif len(values) >= 2:
            add(Fact("", key, "lead",
                     f"In {name}, {top_label} is highest at {_fmt(values[top_label], unit)} and {low_label} is lowest at {_fmt(values[low_label], unit)}.",
                     False, None, f"Review {low_label} in {name}, the lowest value, against {top_label}, the highest.", 10))

        if len(values) >= STAT_MIN_VALUES:  # statistics on the values of this breakdown (code only, no AI)
            ordered = sorted(values.items(), key=lambda kv: -kv[1])
            numbers = [v for _, v in ordered]
            count = len(numbers)
            average, middle = sum(numbers) / count, statistics.median(numbers)
            above = sum(1 for v in numbers if v > average)
            if unit == "count" and positives:
                top_sum, all_sum = sum(numbers[:TOP_N]), sum(positives.values())
                heavy = top_sum / all_sum * 100 >= 50
                add(Fact("", key, "stat", f"The top {TOP_N} of {count} values in {name} hold {_fmt(top_sum, unit)} of {_fmt(all_sum, unit)} ({top_sum / all_sum * 100:.1f}%).",
                         False, None,
                         f"Compare the top {TOP_N} values of {name} with the rest and confirm the concentration is intended." if heavy
                         else f"Keep tracking the top {TOP_N} values of {name} to see whether the balance changes.", 15))
            add(Fact("", key, "stat", f"Across the {count} values in {name}, the average is {_fmt(average, unit)} and the median is {_fmt(middle, unit)}, with {above} of {count} values above the average.",
                     False, None,
                     f"The average of {name} is well above the median, so review the largest values separately from the typical value." if middle > 0 and average > 2 * middle else "", 12))
            add(Fact("", key, "stat", f"In {name}, the highest value is {_fmt(numbers[0], unit)} ({ordered[0][0]}) and the lowest is {_fmt(numbers[-1], unit)} ({ordered[-1][0]}), a gap of {_fmt(numbers[0] - numbers[-1], unit)}.",
                     False, None,
                     f"Confirm that the zero values in {name} are expected." if numbers[-1] == 0
                     else (f"Look at {ordered[-1][0]} in {name}, which is far below the leader." if numbers[-1] < numbers[0] * 0.1 else ""), 11))
        if comparing and unit == "count" and row.get("categories"):
            movers = []
            for cat in row["categories"]:
                cur, prev = cat.get("current"), cat.get("previous")
                if not (_is_number(cur) and _is_number(prev)) or prev < CATEGORY_MIN_BASE or prev == 0:
                    continue
                pct = (cur - prev) / prev * 100
                if abs(pct) >= CATEGORY_FLAG_PCT:
                    movers.append((cat["label"], prev, cur, pct, cur - prev))
            for label, prev, cur, pct, diff in sorted(movers, key=lambda m: -abs(m[4]))[:1] + \
                    ([sorted(movers, key=lambda m: m[4])[0]] if movers and sorted(movers, key=lambda m: m[4])[0] != sorted(movers, key=lambda m: -abs(m[4]))[0] and sorted(movers, key=lambda m: m[4])[0][4] < 0 else []):
                direction = "up" if diff > 0 else "down"
                verb = "rose" if diff > 0 else "fell"
                text = f"In {name}, {label} {verb} from {_fmt(prev, unit)} to {_fmt(cur, unit)} ({pct:+.1f}%)."
                advice = "" if partial else f"Look at {label} in {name} to confirm the {'rise' if diff > 0 else 'fall'}."
                add(Fact("", key, "category", text, True, direction, advice, 45 + min(abs(pct), 200) / 8))
    by_key = {r["key"]: r for r in rows if r["kind"] != "breakdown" and _is_number(r.get("current"))}
    for num_key, den_key, kind, template, advice in DERIVED:  # ratios between two KPIs of this source
        num, den = by_key.get(num_key), by_key.get(den_key)
        if not num or not den or not den["current"]:
            continue
        ratio = num["current"] / den["current"]
        text = template.format(value=f"{ratio * 100:.2f}%" if kind == "pct" else f"{ratio:,.2f}",
                               num=_fmt(num["current"], num["unit"]), den=_fmt(den["current"], den["unit"]),
                               num_name=num["name"], den_name=den["name"])
        add(Fact("", num_key, "derived", text, False, None, advice, 20))
    if comparing and steady:
        shown = ", ".join(steady[:4]) + (" and others" if len(steady) > 4 else "")
        add(Fact("", "_steady", "lead", f"{shown} stayed within the change limits against the previous period.", False, None, "", 5))

    facts.sort(key=lambda f: -f.priority)
    for number, fact in enumerate(facts, start=1):
        fact.id = f"F{number}"
    return facts, notes


# =============================================================================
# 19. AI WORDING with checks
# =============================================================================
_NUMBER = re.compile(r"(?<![A-Za-z0-9])(\d[\d,]*(?:\.\d+)?)(\s?%|[KMBkmb]\b)?")
_SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9}
CAUSAL_WORDS = re.compile(r"\b(because|due to|driven by|caused by|as a result of|thanks to|owing to|led to|resulting from)\b", re.IGNORECASE)
INVENTED_TERMS = re.compile(
    r"\b(retention|retain\w*|churn\w*|attrition|revenue|profit\w*|margins?|conversion\w*|satisf\w+|loyal\w+"
    r"|up-?sell\w*|cross-?sell\w*|competit\w+|benchmark\w*|seasonal\w*|holidays?|festiv\w+|campaigns?"
    r"|best practices?|industry (?:norms?|standards?|averages?))\b",
    re.IGNORECASE,
)
UP_WORDS = re.compile(r"\b(rose|rise|rises|rising|increas\w*|grew|grow\w*|up|higher|gain\w*|improv\w*|climb\w*)\b", re.IGNORECASE)
DOWN_WORDS = re.compile(r"\b(fell|fall|falls|falling|decreas\w*|dropped|drop\w*|down|lower|declin\w*|reduc\w*|slipp\w*)\b", re.IGNORECASE)
_CAPITAL = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][A-Za-z0-9'’&-]{2,})\b")


def _numbers(text: str) -> list[tuple[str, float, int, str]]:
    found = []
    for match in _NUMBER.finditer(text):
        body = match.group(1).rstrip(",")
        suffix = (match.group(2) or "").strip().lower()
        value = float(body.replace(",", ""))
        decimals = len(body.split(".")[1]) if "." in body else 0
        found.append((match.group(0).strip(), value * _SUFFIX.get(suffix, 1), decimals, suffix))
    return found


def _bad_numbers(text: str, allowed: set[float]) -> list[str]:
    """Numbers in text that are not in the allowed set (rounding and K/M/B shortening are fine)."""
    bad = []
    for raw, value, decimals, suffix in _numbers(text):
        if suffix != "%" and not suffix and decimals == 0 and 0 <= value <= 10:
            continue  # small counts and ordinals ("top 3", "two of five")
        for a in allowed:
            if abs(a - value) <= 1e-9 * max(1.0, abs(a)):
                break
            if suffix in ("k", "m", "b"):
                if abs(a - value) <= 0.06 * max(abs(a), 1.0):
                    break
            elif round(a, decimals) == round(value, decimals):
                break
        else:
            bad.append(raw)
    return bad


def _problems(text: str, fact: Fact, vocab: str) -> list[str]:
    """Everything wrong with a reworded sentence. Empty list = keep it."""
    problems = []
    if not text or len(text) > 400:
        return ["empty or too long"]
    allowed = {value for _, value, _, _ in _numbers(fact.text)}
    bad = _bad_numbers(text, allowed)
    if bad:
        problems.append(f"numbers not in the fact: {', '.join(bad)}")
    if allowed and not any(not _bad_numbers(raw, allowed) and (_n[1] > 10 or _n[2] or _n[3]) for raw, *_ in [(t[0],) for t in _numbers(text)] for _n in [next((n for n in _numbers(raw)), ("", 0, 0, ""))]):
        problems.append("it dropped the figures of the fact")
    if CAUSAL_WORDS.search(text):
        problems.append("it states a cause")
    for match in INVENTED_TERMS.finditer(text):
        if match.group(0).lower() not in vocab:
            problems.append(f"invented word: {match.group(0)}")
    for match in _CAPITAL.finditer(text):
        if match.group(1).lower() not in vocab:
            problems.append(f"name not in the data: {match.group(1)}")
    if fact.direction == "up" and DOWN_WORDS.search(text) and not UP_WORDS.search(text):
        problems.append("direction reversed")
    if fact.direction == "down" and UP_WORDS.search(text) and not DOWN_WORDS.search(text):
        problems.append("direction reversed")
    return problems


SYSTEM_PROMPT = """You reword findings from a KPI dashboard for business readers.
You get a JSON list of items. Each has an "id", a "fact" (a finished, correct sentence) and a "role"
("insight" or "recommendation").

Rules:
- Rewrite each item as ONE short, clear sentence (max 30 words), keeping its id.
- For role "insight": say the same thing as the fact in smoother business wording.
- For role "recommendation": turn the fact's advice into one short action sentence (review, check, compare, track).
- Use ONLY the numbers and names that appear in that item's fact. Do not add, calculate or round to new numbers.
- Never give a reason or cause. Never use words like because, due to, driven by.
- Keep the direction: if the fact says fell, do not say rose.
- Do not mention anything that is not in the fact.
Return ONLY JSON: {"items":[{"id":"F1","text":"..."}, ...]}"""


def _parse_json(text: str) -> Any:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    try:
        return json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise


def _reword(requests: list[tuple[str, str, Fact]], source: str, router: Any) -> tuple[dict[str, str], str | None]:
    """Ask the model to reword. requests = [(id, role, fact)]. Returns ({id: checked text}, model label)."""
    payload = [{"id": rid, "role": role, "fact": (fact.text if role == "insight" else f"{fact.text} Advice: {fact.advice}")}
               for rid, role, fact in requests]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps({"source": source, "items": payload}, ensure_ascii=False)},
    ]
    try:  # group this call with the others in the usage page, when available
        import observability
        router.trace_id = observability.new_trace_id()
    except Exception:  # noqa: BLE001 - tracing is optional
        pass
    reply = router.chat(messages, temperature=0.2, max_tokens=INSIGHT_MAX_TOKENS, step="write-insights")
    parsed = _parse_json(reply.text)
    by_id = {str(i.get("id")): str(i.get("text", "")).strip() for i in parsed.get("items", []) if isinstance(i, dict)}
    model = getattr(getattr(reply, "model", None), "label", None)
    return by_id, model


# =============================================================================
# Entry point
# =============================================================================
def generate(current: dict, previous: dict | None = None, router: Any = None) -> InsightResult:
    """Insights and recommendations for one source and period. Never raises.

    router=None gives the plain (fully rule-based) sentences. Pass the app's LLMRouter to let
    the AI reword them; every reworded sentence is checked and falls back to the plain one.
    """
    source = current.get("source", "")
    result = InsightResult(source=source)
    try:
        facts, notes = build_facts(current, previous)
    except Exception as exc:  # noqa: BLE001 - the dashboard must keep working
        result.notes.append(f"Insights could not be built: {exc}")
        return result
    result.notes = notes

    insight_facts = facts[:MAX_INSIGHTS]
    rec_facts = [f for f in facts if f.advice][:MAX_RECOMMENDATIONS]
    reworded: dict[str, str] = {}
    if router is not None and (insight_facts or rec_facts):
        requests = [(f"I:{f.id}", "insight", f) for f in insight_facts] + [(f"R:{f.id}", "recommendation", f) for f in rec_facts]
        try:
            reworded, result.model = _reword(requests, source, router)
            result.ai_used = True
        except Exception as exc:  # noqa: BLE001 - AI missing or failed: plain sentences are used
            result.notes.append(f"AI wording was not used ({type(exc).__name__}); showing the plain sentences.")
            reworded = {}

    # vocabulary the AI may use: the facts themselves, the KPI names and the source name
    vocab = json.dumps([[f.text, f.advice] for f in facts] + [source, [k.get("name", "") for k in current.get("kpis", {}).values()]],
                       ensure_ascii=False).lower()
    facts_by_id = {f.id: f for f in facts}

    def pick(rid: str, fact: Fact, plain: str) -> tuple[str, bool]:
        candidate = reworded.get(rid)
        if candidate and not _problems(candidate, facts_by_id[fact.id], vocab):
            return candidate, True
        if rid in reworded:
            result.fallbacks += 1
        return plain, False

    for fact in insight_facts:
        text, ai = pick(f"I:{fact.id}", fact, fact.text)
        result.insights.append(Line(text, fact.kpi, fact.flagged, ai, fact.id))
    for fact in rec_facts:
        text, ai = pick(f"R:{fact.id}", fact, fact.advice)
        result.recommendations.append(Line(text, fact.kpi, True, ai, fact.id))
    return result