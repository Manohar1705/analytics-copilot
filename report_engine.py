"""report_engine.py - turns ticked files into a finished report deck.

Flow (the AI never does maths and never writes code):
  1. PLAN   AI sees the data profile and returns a slide plan (JSON).
  2. CHECK  every slide spec is validated and computed with pandas. A bad spec is
            reported back to the AI once for repair; slides that still fail are dropped.
  3. WRITE  AI sees the computed numbers and writes insights and recommendations.
  4. VERIFY every number in the text must exist in that slide's computed data, and
            causal wording ("because of ...") is not allowed. Failing sentences get one
            repair attempt, then are removed.
  5. BUILD  pptx_writer.build_deck() draws the .pptx.

Public entry point: build_report(prepared, router, title, period, on_step).
Wording sent to the AI lives in report_prompts.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

import observability
import pptx_writer
import report_prompts as prompts
from data_loader import Dataset, _mask_text
from engine import LLMRouter, LLMUnavailable, PreparedData

PLAN_MAX_TOKENS = 3000
WRITE_MAX_TOKENS = 6000

NUMERIC_AGGS = {"sum", "mean", "median", "min", "max"}
FREQ_CODES = {"week": "W", "month": "M", "quarter": "Q", "year": "Y"}
MAX_PERIODS = 24
CAUSAL_WORDS = re.compile(
    r"\b(because|due to|driven by|caused by|as a result of|thanks to|owing to)\b", re.IGNORECASE
)


class PlanError(Exception):
    """A slide spec that cannot be used. The message is shown to the AI and the user."""


@dataclass
class ReportResult:
    ok: bool
    pptx: bytes | None = None
    deck: dict[str, Any] = field(default_factory=dict)  # what was drawn, for the on-screen preview
    warnings: list[str] = field(default_factory=list)  # slides skipped, commentary problems
    removed: list[str] = field(default_factory=list)  # sentences removed by the number check
    steps: list[str] = field(default_factory=list)
    model: str | None = None
    error: str | None = None


@dataclass
class _Slide:
    key: tuple
    slide: dict[str, Any]  # what pptx_writer draws (without commentary yet)
    ai: dict[str, Any]  # what the writing AI sees
    files: list[str]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _clean(value: Any, limit: int = 80) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return _clean(value, 40)
    if np.isnan(v) or np.isinf(v):
        return "n/a"
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v)):,}"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 100:
        return f"{v:,.1f}"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def _signed(value: float) -> str:
    return ("+" if value > 0 else "") + _fmt(value)


def _label(value: Any) -> str:
    """Category label: readable, short, with e-mails and phone numbers hidden."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        text = value.strftime("%d %b %Y")
    elif isinstance(value, float) and value.is_integer():
        text = str(int(value))
    else:
        text = str(value)
    return _mask_text(text.strip())[:40]


def _cell(value: Any) -> str:
    if isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, (bool, np.bool_)):
        return _fmt(value)
    return _label(value)


def _number_format(values: list[Any]) -> str:
    vals = [abs(float(v)) for v in values if v is not None and not pd.isna(v)]
    if not vals or all(abs(v - round(v)) < 1e-9 for v in vals):
        return "#,##0"
    return "#,##0.00" if max(vals) < 10 else "#,##0.0"


def _chart_value(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), 2)


def _col(df: pd.DataFrame, name: Any, what: str) -> str:
    if name in (None, ""):
        raise PlanError(f"{what}: no column given")
    if name in df.columns:
        return name
    lookup = {str(c).lower().strip(): c for c in df.columns}
    key = str(name).lower().strip()
    if key in lookup:
        return lookup[key]
    raise PlanError(f"{what}: column '{name}' does not exist in this table")


_AGG_WORDS = {
    "count": "Count of", "sum": "Total", "mean": "Average", "median": "Median",
    "min": "Lowest", "max": "Highest", "nunique": "Distinct",
}


def _metrics(df: pd.DataFrame, raw: Any, limit: int) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise PlanError("no metrics given")
    result: list[dict[str, Any]] = []
    used: set[str] = set()
    for item in raw[:limit]:
        if not isinstance(item, dict):
            raise PlanError("a metric must be an object with column and agg")
        agg = str(item.get("agg", "")).lower().strip()
        if agg not in prompts.AGGREGATIONS:
            raise PlanError(f"unknown agg '{agg}'")
        column = item.get("column")
        if column in (None, "", "null"):
            if agg != "count":
                raise PlanError(f"agg '{agg}' needs a column")
            column, default = None, "Count of rows"
        else:
            column = _col(df, column, "metric")
            if agg in NUMERIC_AGGS and (
                not pd.api.types.is_numeric_dtype(df[column]) or pd.api.types.is_bool_dtype(df[column])
            ):
                raise PlanError(f"agg '{agg}' needs a numeric column, but '{column}' is not numeric")
            default = f"{_AGG_WORDS[agg]} {column}"
        label = _clean(item.get("label"), 40) or default
        base, n = label, 2
        while label in used:
            label, n = f"{base} ({n})", n + 1
        used.add(label)
        result.append({"column": column, "agg": agg, "label": label})
    return result


def _whole(df: pd.DataFrame, metric: dict[str, Any]) -> float | None:
    if metric["column"] is None:
        return float(len(df))
    value = getattr(df[metric["column"]], metric["agg"])()
    return None if pd.isna(value) else float(value)


def _group(df: pd.DataFrame, category: str, metrics: list[dict[str, Any]]) -> pd.DataFrame:
    keys = df[category].map(_label)
    keep = keys != ""
    if not keep.any():
        raise PlanError(f"'{category}' has no values")
    data, keys = df[keep], keys[keep]
    grouped = data.groupby(keys, sort=False)
    parts = {
        m["label"]: grouped.size() if m["column"] is None else grouped[m["column"]].agg(m["agg"])
        for m in metrics
    }
    result = pd.DataFrame(parts).dropna(how="all")
    if result.empty:
        raise PlanError("no data after grouping")
    return result


def _top(result: pd.DataFrame, first: str, ascending: bool) -> pd.DataFrame:
    return result.sort_values(first, ascending=ascending, na_position="last", kind="mergesort")


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _shares(result: pd.DataFrame, metrics: list[dict[str, Any]]) -> pd.Series | None:
    """Share of the total, only where it means something (one count or sum, no negatives)."""
    if len(metrics) != 1 or metrics[0]["agg"] not in ("count", "sum"):
        return None
    values = result[metrics[0]["label"]].fillna(0)
    total = values.sum()
    if total <= 0 or (values < 0).any():
        return None
    return values / total * 100


# ---------------------------------------------------------------------------
# One compute function per slide type
# ---------------------------------------------------------------------------
def _slide_head(spec: dict[str, Any], ds: Dataset, default_title: str) -> dict[str, Any]:
    return {"title": _clean(spec.get("title")) or default_title, "source": ds.key}


def _c_bar(spec: dict[str, Any], ds: Dataset) -> _Slide:
    df = ds.df
    cat = _col(df, spec.get("category"), "category")
    metrics = _metrics(df, spec.get("metrics"), 3)
    ascending = str(spec.get("sort", "desc")).lower() == "asc"
    top_n = _clamp(spec.get("top_n"), 3, 15, 10)

    result = _top(_group(df, cat, metrics), metrics[0]["label"], ascending)
    total_groups = len(result)
    shares = _shares(result, metrics)
    shown = result.head(top_n)
    share_shown = shares.loc[shown.index] if shares is not None else None

    notes = []
    if total_groups > top_n:
        notes.append(f"{top_n} of {total_groups} values of {cat} shown, ranked by {metrics[0]['label']}")
    if share_shown is not None:
        notes.append(f"share_pct is the share of the total across all {total_groups} values of {cat}")

    names = [m["label"] for m in metrics]
    columns = [cat] + names + (["share_pct"] if share_shown is not None else [])
    rows = []
    for index, row in shown.iterrows():
        line = [index] + [_fmt(row[n]) for n in names]
        if share_shown is not None:
            line.append(f"{share_shown[index]:.1f}")
        rows.append(line)

    horizontal = spec.get("horizontal")
    if not isinstance(horizontal, bool):
        horizontal = len(shown) > 6 or max(len(str(i)) for i in shown.index) > 14
    series = [{"name": n, "values": [_chart_value(v) for v in shown[n]]} for n in names]
    slide = {
        **_slide_head(spec, ds, f"{' and '.join(names)} by {cat}"),
        "type": "bar", "chart_title": f"{' / '.join(names)} by {cat}",
        "categories": [str(i) for i in shown.index], "series": series,
        "stacked": bool(spec.get("stacked")) and len(names) > 1, "horizontal": horizontal,
        "number_format": _number_format([v for s in series for v in s["values"]]),
    }
    ai = {"columns": columns, "rows": rows, "notes": notes}
    return _Slide(("bar", ds.alias, cat, tuple((m["column"], m["agg"]) for m in metrics)), slide, ai, [ds.file_name])


def _period_label(period: pd.Period, code: str) -> str:
    if code == "M":
        return period.strftime("%b %Y")
    if code == "Q":
        return f"Q{period.quarter} {period.year}"
    if code == "Y":
        return str(period.year)
    return period.start_time.strftime("%d %b %y")


def _c_line(spec: dict[str, Any], ds: Dataset) -> _Slide:
    df = ds.df
    date_col = _col(df, spec.get("date_column"), "date_column")
    if not pd.api.types.is_datetime64_any_dtype(df[date_col]):
        raise PlanError(f"'{date_col}' is not a date column")
    metrics = _metrics(df, spec.get("metrics"), 3)
    freq = str(spec.get("freq", "month")).lower()
    code = FREQ_CODES.get(freq, "M")

    data = df[df[date_col].notna()]
    dates = data[date_col]
    if getattr(dates.dt, "tz", None) is not None:
        dates = dates.dt.tz_localize(None)
    if data.empty:
        raise PlanError(f"'{date_col}' has no dates")
    periods = dates.dt.to_period(code)
    grouped = data.groupby(periods)
    parts = {
        m["label"]: grouped.size() if m["column"] is None else grouped[m["column"]].agg(m["agg"])
        for m in metrics
    }
    result = pd.DataFrame(parts)
    full = pd.period_range(result.index.min(), result.index.max(), freq=code)
    truncated = len(full) > MAX_PERIODS
    full = full[-MAX_PERIODS:]
    result = result.reindex(full)
    for m in metrics:
        if m["agg"] in ("count", "sum", "nunique"):
            result[m["label"]] = result[m["label"]].fillna(0)
    if len(result) < 3:
        raise PlanError(f"only {len(result)} {freq} period(s) of data, too few for a trend")

    labels = [_period_label(p, code) for p in result.index]
    names = [m["label"] for m in metrics]
    rows = [[labels[i]] + [_fmt(result[n].iloc[i]) for n in names] for i in range(len(labels))]
    notes = [f"one row per {freq}, {len(labels)} periods"]
    if truncated:
        notes.append(f"only the last {MAX_PERIODS} periods are shown")
    change: dict[str, Any] = {}
    for n in names:
        first, last = result[n].iloc[0], result[n].iloc[-1]
        if pd.isna(first) or pd.isna(last):
            continue
        entry: dict[str, Any] = {
            "first_period": labels[0], "first": _fmt(first), "last_period": labels[-1], "last": _fmt(last),
            "change": _signed(last - first),
        }
        if first != 0:
            entry["change_pct"] = f"{(last - first) / abs(first) * 100:+.1f}%"
        peak = result[n].idxmax()
        entry["highest_period"] = _period_label(peak, code)
        entry["highest_value"] = _fmt(result[n].max())
        change[n] = entry

    series = [{"name": n, "values": [_chart_value(v) for v in result[n]]} for n in names]
    slide = {
        **_slide_head(spec, ds, f"{' and '.join(names)} per {freq}"),
        "type": "line", "chart_title": f"{' / '.join(names)} per {freq}",
        "categories": labels, "series": series,
        "number_format": _number_format([v for s in series for v in s["values"]]),
    }
    ai = {"columns": ["Period"] + names, "rows": rows, "notes": notes, "change": change}
    return _Slide(("line", ds.alias, date_col, tuple((m["column"], m["agg"]) for m in metrics)), slide, ai, [ds.file_name])


def _c_donut(spec: dict[str, Any], ds: Dataset) -> _Slide:
    df = ds.df
    cat = _col(df, spec.get("category"), "category")
    metrics = _metrics(df, [spec.get("metric")], 1)
    metric = metrics[0]
    if metric["agg"] not in ("count", "sum"):
        raise PlanError("a donut needs agg 'count' or 'sum'")
    top_n = _clamp(spec.get("top_n"), 3, 6, 6)

    result = _top(_group(df, cat, metrics), metric["label"], False)
    values = result[metric["label"]].fillna(0)
    if (values < 0).any() or values.sum() <= 0:
        raise PlanError("a donut cannot show negative or zero totals")
    total = float(values.sum())
    shown = values.head(top_n)
    notes = []
    labels, amounts = [str(i) for i in shown.index], [float(v) for v in shown]
    if len(values) > top_n:
        other = float(values.iloc[top_n:].sum())
        labels.append("Other")
        amounts.append(other)
        notes.append(f"'Other' combines the {len(values) - top_n} smaller values of {cat}")
    rows = [[lab, _fmt(a), f"{a / total * 100:.1f}"] for lab, a in zip(labels, amounts)]
    slide = {
        **_slide_head(spec, ds, f"Share of {metric['label']} by {cat}"),
        "type": "donut", "chart_title": f"{metric['label']} by {cat}",
        "categories": labels, "series": [{"name": metric["label"], "values": [round(a, 2) for a in amounts]}],
        "number_format": _number_format(amounts),
    }
    ai = {"columns": [cat, metric["label"], "share_pct"], "rows": rows, "notes": notes}
    return _Slide(("donut", ds.alias, cat, (metric["column"], metric["agg"])), slide, ai, [ds.file_name])


def _c_table(spec: dict[str, Any], ds: Dataset) -> _Slide:
    df = ds.df
    limit = _clamp(spec.get("limit"), 3, pptx_writer.MAX_TABLE_ROWS, 10)
    descending = spec.get("descending") is not False
    notes: list[str] = []

    if spec.get("group_by") not in (None, "", "null"):
        group = _col(df, spec.get("group_by"), "group_by")
        metrics = _metrics(df, spec.get("metrics"), 5)
        result = _group(df, group, metrics)
        names = [m["label"] for m in metrics]
        sort_by = _clean(spec.get("sort_by"), 60)
        sort_col = sort_by if sort_by in names else names[0]
        result = _top(result, sort_col, not descending)
        if len(result) > limit:
            notes.append(f"{limit} of {len(result)} values of {group} shown, ranked by {sort_col}")
        columns = [group] + names
        rows = [[i] + [_fmt(r[n]) for n in names] for i, r in result.head(limit).iterrows()]
        key = ("table", ds.alias, group, tuple((m["column"], m["agg"]) for m in metrics))
    else:
        raw = spec.get("columns")
        if not isinstance(raw, list) or not raw:
            raise PlanError("a table needs columns")
        columns = []
        for name in raw[:7]:
            col = _col(df, name, "table column")
            if col not in columns:
                columns.append(col)
        data = df
        sort_by = spec.get("sort_by")
        if sort_by not in (None, ""):
            try:
                data = df.sort_values(_col(df, sort_by, "sort_by"), ascending=not descending, na_position="last", kind="mergesort")
            except TypeError:
                data = df
        if len(df) > limit:
            notes.append(f"first {limit} of {len(df)} rows shown")
        rows = [[_cell(v) for v in rec] for rec in data[columns].head(limit).itertuples(index=False, name=None)]
        key = ("table", ds.alias, "rows", tuple(columns))

    if not rows:
        raise PlanError("the table has no rows")
    slide = {**_slide_head(spec, ds, f"{ds.alias} detail"), "type": "table", "columns": columns, "rows": rows}
    return _Slide(key, slide, {"columns": columns, "rows": rows, "notes": notes}, [ds.file_name])


def _c_kpis(spec: dict[str, Any], datasets: dict[str, Dataset], default: Dataset) -> _Slide:
    raw = spec.get("kpis")
    if not isinstance(raw, list) or not raw:
        raise PlanError("no kpis given")
    tiles, rows, files, seen = [], [], [], set()
    for item in raw[: pptx_writer.MAX_KPIS]:
        if not isinstance(item, dict):
            continue
        ds = _dataset(datasets, item.get("dataset")) if item.get("dataset") else default
        try:
            metric = _metrics(ds.df, [item], 1)[0]
            value = _whole(ds.df, metric)
        except PlanError:
            continue  # one bad headline number does not sink the slide
        if value is None:
            continue
        label = _clean(item.get("label"), 40) or metric["label"]
        if label in seen:
            continue
        seen.add(label)
        tiles.append({"label": label, "value": _fmt(value), "note": ds.file_name if ds is not default else ""})
        rows.append([label, _fmt(value)])
        files.append(ds.file_name)
    if not tiles:
        raise PlanError("none of the headline numbers could be computed")
    slide = {
        "title": _clean(spec.get("title")) or "Headline numbers", "type": "kpis", "kpis": tiles,
        "source": ", ".join(dict.fromkeys(files)),
    }
    return _Slide(("kpis",), slide, {"columns": ["metric", "value"], "rows": rows, "notes": []}, list(dict.fromkeys(files)))


def _dataset(datasets: dict[str, Dataset], alias: Any) -> Dataset:
    if alias in datasets:
        return datasets[alias]
    lookup = {k.lower(): v for k, v in datasets.items()}
    if str(alias).lower().strip() in lookup:
        return lookup[str(alias).lower().strip()]
    raise PlanError(f"table '{alias}' does not exist")


def _compute(spec: Any, datasets: dict[str, Dataset]) -> _Slide:
    if not isinstance(spec, dict):
        raise PlanError("a slide must be an object")
    kind = str(spec.get("type", "")).lower()
    if kind not in prompts.SLIDE_TYPES:
        raise PlanError(f"unknown slide type '{kind}'")
    ds = _dataset(datasets, spec.get("dataset"))
    if kind == "kpis":
        return _c_kpis(spec, datasets, ds)
    return {"bar": _c_bar, "line": _c_line, "donut": _c_donut, "table": _c_table}[kind](spec, ds)


def _build_slides(plan: Any, datasets: dict[str, Dataset], limit: int) -> tuple[list[_Slide], list[str]]:
    """Validate and compute every planned slide. Returns (good slides, problems)."""
    problems: list[str] = []
    if not isinstance(plan, dict) or not isinstance(plan.get("slides"), list) or not plan["slides"]:
        return [], ['the plan must be {"slides": [ ... ]} with at least one slide']
    good: list[_Slide] = []
    keys: set[tuple] = set()
    for number, spec in enumerate(plan["slides"], start=1):
        name = _clean(spec.get("title"), 50) if isinstance(spec, dict) else ""
        try:
            slide = _compute(spec, datasets)
        except PlanError as exc:
            problems.append(f"slide {number} ('{name}'): {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - any pandas surprise only loses that slide
            problems.append(f"slide {number} ('{name}'): could not be computed ({type(exc).__name__})")
            continue
        if slide.key in keys:
            problems.append(f"slide {number} ('{name}'): repeats an earlier slide")
            continue
        keys.add(slide.key)
        good.append(slide)
    good.sort(key=lambda s: s.key[0] != "kpis")  # headline numbers first (sort is stable)
    return good[:limit], problems


# ---------------------------------------------------------------------------
# JSON from the AI
# ---------------------------------------------------------------------------
def _parse_json(text: str) -> Any:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    try:
        return json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise ValueError("no JSON object found in the reply")


# ---------------------------------------------------------------------------
# Number check
# ---------------------------------------------------------------------------
_NUMBER = re.compile(r"(?<![A-Za-z0-9])(\d[\d,]*(?:\.\d+)?)(\s?%|[KMBkmb]\b)?")
_SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9}


def _numbers(text: str) -> list[tuple[str, float, int, str]]:
    """(as written, value, decimals, suffix) for every number in the text."""
    found = []
    for match in _NUMBER.finditer(text):
        body = match.group(1).rstrip(",")
        suffix = (match.group(2) or "").strip().lower()
        value = float(body.replace(",", ""))
        decimals = len(body.split(".")[1]) if "." in body else 0
        multiplier = _SUFFIX.get(suffix, 1)
        found.append((match.group(0).strip(), value * multiplier, decimals, suffix))
    return found


def _allowed(*views: Any) -> set[float]:
    return {value for view in views for _, value, _, _ in _numbers(json.dumps(view, ensure_ascii=False, default=str))}


def _bad_numbers(text: str, allowed: set[float]) -> list[str]:
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


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def _sentence_ok(text: str, allowed: set[float]) -> bool:
    return not CAUSAL_WORDS.search(text) and not _bad_numbers(text, allowed)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []


def _parse_commentary(parsed: Any, count: int) -> tuple[dict[int, dict[str, Any]], list[dict[str, str]]]:
    """Normalise the AI's JSON into {slide id: {insights, recommendation}} and summary items."""
    if not isinstance(parsed, dict):
        raise ValueError("the reply must be a JSON object")
    by_id: dict[int, dict[str, Any]] = {}
    for item in parsed.get("slides") or []:
        try:
            number = int(item.get("id"))
        except (AttributeError, TypeError, ValueError):
            continue
        if 1 <= number <= count and number not in by_id:
            by_id[number] = {
                "insights": _strings(item.get("insights"))[:4],
                "recommendation": " ".join(_strings(item.get("recommendation"))),
            }
    summary = []
    for item in parsed.get("summary") or []:
        if isinstance(item, dict) and _strings(item.get("text")):
            summary.append({"heading": _clean(item.get("heading"), 50), "text": " ".join(_strings(item.get("text")))})
    return by_id, summary[:6]


def _number_problems(by_id: dict[int, dict[str, Any]], summary: list[dict[str, str]], slides: list[_Slide]) -> list[str]:
    problems = []
    everything = _allowed([s.ai for s in slides])
    for number, entry in by_id.items():
        allowed = _allowed(slides[number - 1].ai)
        title = slides[number - 1].slide["title"]
        for text in entry["insights"] + _sentences(entry["recommendation"]):
            for raw in _bad_numbers(text, allowed):
                problems.append(f"Slide {number} ('{title}'): '{raw}' in \"{text[:90]}\"")
            if CAUSAL_WORDS.search(text):
                problems.append(f"Slide {number} ('{title}'): gives a cause, which the data does not state: \"{text[:90]}\"")
    for item in summary:
        for sentence in _sentences(item["text"]):
            for raw in _bad_numbers(sentence, everything):
                problems.append(f"Summary '{item['heading']}': '{raw}' in \"{sentence[:90]}\"")
    return problems


def _strip_unsupported(
    by_id: dict[int, dict[str, Any]], summary: list[dict[str, str]], slides: list[_Slide]
) -> tuple[dict[int, dict[str, Any]], list[dict[str, str]], list[str]]:
    removed: list[str] = []
    everything = _allowed([s.ai for s in slides])
    for number, entry in by_id.items():
        allowed = _allowed(slides[number - 1].ai)
        keep = []
        for text in entry["insights"]:
            (keep if _sentence_ok(text, allowed) else removed).append(text)
        entry["insights"] = keep
        kept, dropped = [], []
        for sentence in _sentences(entry["recommendation"]):
            (kept if _sentence_ok(sentence, allowed) else dropped).append(sentence)
        entry["recommendation"] = " ".join(kept)
        removed += dropped
    cleaned = []
    for item in summary:
        kept = [s for s in _sentences(item["text"]) if _sentence_ok(s, everything)]
        removed += [s for s in _sentences(item["text"]) if s not in kept]
        if kept:
            cleaned.append({"heading": item["heading"], "text": " ".join(kept)})
    return by_id, cleaned, removed


# ---------------------------------------------------------------------------
# The whole flow
# ---------------------------------------------------------------------------
def build_report(
    prepared: PreparedData,
    router: LLMRouter,
    title: str,
    period: str,
    on_step: Callable[[str], None] | None = None,
) -> ReportResult:
    steps: list[str] = []

    def log(message: str) -> None:
        steps.append(message)
        if on_step:
            on_step(message)

    if not prepared.datasets:
        return ReportResult(ok=False, error="Tick at least one file first.", steps=steps)
    datasets = {d.alias: d for d in prepared.datasets}
    file_count = len({d.file_name for d in prepared.datasets})
    low, high = prompts.slide_budget(file_count)
    router.trace_id = observability.new_trace_id()
    warnings: list[str] = []
    model: str | None = None

    # 1-2. plan and compute ---------------------------------------------------
    log("Planning the slides")
    base = prompts.build_plan_messages(prepared.profile_text, title, period, file_count)
    try:
        reply = router.chat(base, temperature=0.2, max_tokens=PLAN_MAX_TOKENS, step="plan-report")
    except LLMUnavailable as exc:
        return ReportResult(ok=False, error=f"No model could plan the report. {exc}", steps=steps)
    model = reply.model.label
    try:
        slides, problems = _build_slides(_parse_json(reply.text), datasets, high)
    except ValueError as exc:
        slides, problems = [], [f"the reply was not valid JSON ({exc})"]

    if problems:
        log("Fixing the slide plan")
        note = "\n".join(f"- {p}" for p in problems[:12])
        retry = base + [
            {"role": "assistant", "content": reply.text},
            {"role": "user", "content": f"Your previous plan had these problems. Fix them and return the full corrected plan:\n{note}"},
        ]
        try:
            reply2 = router.chat(retry, temperature=0.2, max_tokens=PLAN_MAX_TOKENS, step="plan-report")
            model = reply2.model.label
            slides2, problems2 = _build_slides(_parse_json(reply2.text), datasets, high)
            if len(slides2) >= len(slides):
                slides, problems = slides2, problems2
        except (LLMUnavailable, ValueError):
            pass  # keep the slides that were already valid
        warnings += [f"Dropped: {p}" for p in problems]

    if not slides:
        detail = "; ".join(problems[:3]) or "no usable slides"
        return ReportResult(ok=False, error=f"Could not plan a report from this data ({detail}).", steps=steps, model=model)
    if len(slides) < low:
        warnings.append(f"Only {len(slides)} slide(s) could be built from this data.")

    # 3-4. write and verify ---------------------------------------------------
    log("Writing insights and recommendations")
    views = [{"id": n, "type": s.slide["type"], "title": s.slide["title"], "source": s.slide.get("source", ""), **s.ai}
             for n, s in enumerate(slides, start=1)]
    by_id: dict[int, dict[str, Any]] = {}
    summary: list[dict[str, str]] = []
    removed: list[str] = []
    messages = prompts.build_write_messages(title, period, views)
    try:
        reply = router.chat(messages, temperature=0.3, max_tokens=WRITE_MAX_TOKENS, step="write-report")
        model = reply.model.label
        by_id, summary = _parse_commentary(_parse_json(reply.text), len(slides))
        found = _number_problems(by_id, summary, slides)
        if found:
            log("Checking the numbers in the text")
            retry = messages + [
                {"role": "assistant", "content": reply.text},
                {"role": "user", "content": prompts.number_problem_note(found)},
            ]
            try:
                reply2 = router.chat(retry, temperature=0.2, max_tokens=WRITE_MAX_TOKENS, step="write-report")
                by_id2, summary2 = _parse_commentary(_parse_json(reply2.text), len(slides))
                if len(_number_problems(by_id2, summary2, slides)) <= len(found):
                    by_id, summary = by_id2, summary2
            except (LLMUnavailable, ValueError):
                pass
        by_id, summary, removed = _strip_unsupported(by_id, summary, slides)
    except LLMUnavailable as exc:
        warnings.append(f"Insights and recommendations could not be written: {exc}")
    except ValueError as exc:
        warnings.append(f"The written commentary could not be read ({exc}), so slides have no insights.")
    if removed:
        warnings.append(f"{len(removed)} sentence(s) removed because their numbers or wording did not match the data.")

    # 5. build ----------------------------------------------------------------
    log("Building the PowerPoint")
    deck_slides = []
    for number, s in enumerate(slides, start=1):
        entry = by_id.get(number, {})
        deck_slides.append({**s.slide, "insights": entry.get("insights", []), "recommendation": entry.get("recommendation", "")})
    if not summary:  # fall back to the per-slide recommendations
        summary = [{"heading": _clean(d["title"], 50), "text": d["recommendation"]} for d in deck_slides if d["recommendation"]][:6]
    if summary:
        deck_slides.append({"type": "summary", "title": "Key recommendations", "items": summary})

    files = list(dict.fromkeys(f for s in slides for f in s.files))
    deck = {"title": _clean(title) or "Report", "subtitle": _clean(period), "sources": files, "slides": deck_slides}
    try:
        data, build_warnings = pptx_writer.build_deck(deck)
    except Exception as exc:  # noqa: BLE001
        return ReportResult(ok=False, error=f"The PowerPoint could not be built ({type(exc).__name__}: {exc}).",
                            steps=steps, model=model, warnings=warnings)
    return ReportResult(ok=True, pptx=data, deck=deck, warnings=warnings + build_warnings,
                        removed=removed, steps=steps, model=model)