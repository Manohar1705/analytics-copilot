"""kpi_config.py - the KPI catalogue (plain Python, no Streamlit).

A KPI is ONE number with a FIXED formula, defined here in code (never by an LLM).
Formulas read COMMON column names. sources.py maps each file's real columns to them:

  ServiceNow  table "main"      Stage->state, SLA definition->sla_definition,
                                Business duration->business_duration, Created->created
  Sprinklr    table "organic"   Account Type->account_type, Total Engagements (SUM)->engagements
              table "paid"      Spent in USD (SUM)->spend
  Traackr     table "brands"    Keyword Group->brand, Posts->posts, Engagements->engagements,
                                Est Impressions->est_impressions, Video Views->video_views,
                                Potential Reach->potential_reach
              table "campaigns" Start Date->start_date, Total Spend->total_spend

STATUS: placeholder KPIs - "to be confirmed with the business".
Change a formula here and bump KPI_VERSION so old snapshots stay traceable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

KPI_VERSION = "0.3-placeholder"

# ---- Assumed values (confirm with the business) ------------------------------
# ServiceNow "Stage" values seen in the file: Completed, Cancelled, Paused, In progress
COMPLETED_STATES = {"completed", "complete", "closed complete"}
CANCELLED_STATES = {"cancelled", "canceled"}


@dataclass(frozen=True)
class KPI:
    key: str                       # stable id, used in snapshots
    name: str                      # label shown on the dashboard
    source: str                    # "ServiceNow" | "Sprinklr" | "Traackr"
    formula: Callable[[pd.DataFrame], Any]  # df -> number, dict (breakdown) or None
    required_columns: tuple[str, ...]       # common column names it needs
    chart_type: str                # "card" | "bar" | "donut" | "line"
    unit: str                      # "count" | "%" | "currency" | "hours"
    kind: str = "single"           # "single" number or "breakdown" (dict by category)
    table: str = "main"            # which sheet/table it reads


# ---- Helpers -----------------------------------------------------------------
def _lower(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().str.lower()


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    """Column as numbers. Also handles text like '$52.63' or '1,200'."""
    series = df[col]
    if not pd.api.types.is_numeric_dtype(series):  # text such as "$52.63"
        series = series.astype(str).str.replace(r"[^0-9.\-]", "", regex=True)
    return pd.to_numeric(series, errors="coerce")


def _share(mask: pd.Series) -> float | None:
    """Percent of rows where mask is True. None if there are no rows."""
    return round(float(mask.mean()) * 100, 2) if len(mask) else None


def _by_category(df: pd.DataFrame, value_col: str, cat_col: str, top: int | None = None) -> dict[str, float]:
    grouped = _num(df, value_col).groupby(df[cat_col].astype(str)).sum().sort_values(ascending=False)
    if top:
        grouped = grouped.head(top)
    return {str(k): float(v) for k, v in grouped.items()}


# ---- ServiceNow formulas -------------------------------------------------------
def total_tasks(df: pd.DataFrame) -> int:
    return int(len(df))


def completion_rate(df: pd.DataFrame) -> float | None:
    return _share(_lower(df["state"]).isin(COMPLETED_STATES))


def cancelled_tasks(df: pd.DataFrame) -> int:
    return int(_lower(df["state"]).isin(CANCELLED_STATES).sum())


def tasks_by_sla(df: pd.DataFrame) -> dict[str, int]:
    counts = df["sla_definition"].astype(str).value_counts()
    return {str(k): int(v) for k, v in counts.items()}


def avg_business_duration(df: pd.DataFrame) -> float | None:
    """Average business duration in HOURS. The ServiceNow column is in seconds, so divide by 3600."""
    mean = _num(df, "business_duration").mean() / 3600
    return round(float(mean), 2) if pd.notna(mean) else None

def _clean_labels(series: pd.Series) -> pd.Series:
    """Text labels without hidden zero-width or non-breaking spaces; blanks become '(blank)'."""
    return series.fillna("(blank)").astype(str).str.replace("[\u200b\u00a0]", " ", regex=True).str.strip()


def _count_by(df: pd.DataFrame, col: str) -> dict[str, int]:
    return {str(k): int(v) for k, v in _clean_labels(df[col]).value_counts().items()}


def tasks_by_request_type(df: pd.DataFrame) -> dict[str, int]:
    return _count_by(df, "request_type")


def avg_hours_by_request_type(df: pd.DataFrame) -> dict[str, float]:
    hours = _num(df, "business_duration") / 3600  # file value is in seconds
    means = hours.groupby(_clean_labels(df["request_type"])).mean().dropna().sort_values(ascending=False)
    return {str(k): round(float(v), 2) for k, v in means.items()}


def tasks_by_region(df: pd.DataFrame) -> dict[str, int]:
    return _count_by(df, "region")


def tasks_by_country(df: pd.DataFrame) -> dict[str, int]:
    return _count_by(df, "country")


def tasks_by_priority(df: pd.DataFrame) -> dict[str, int]:
    return _count_by(df, "priority")
# ---- Sprinklr formulas -------------------------------------------------------
def total_engagements(df: pd.DataFrame) -> float:
    return float(_num(df, "engagements").sum())


def engagements_by_account_type(df: pd.DataFrame) -> dict[str, float]:
    return _by_category(df, "engagements", "account_type")

def paid_engagements(df: pd.DataFrame) -> float:
    return float(_num(df, "engagements").sum())


def paid_impressions(df: pd.DataFrame) -> float:
    return float(_num(df, "impressions").sum())


def paid_cpm(df: pd.DataFrame) -> float | None:
    """Cost per 1,000 impressions = spend / impressions x 1000 (worked out from the totals, not summed)."""
    impressions = _num(df, "impressions").sum()
    return round(float(_num(df, "spend").sum()) / impressions * 1000, 2) if impressions else None
def total_ad_spend(df: pd.DataFrame) -> float:
    return float(_num(df, "spend").sum())


# ---- Traackr formulas ----------------------------------------------------------
def tr_total_posts(df: pd.DataFrame) -> float:
    return float(_num(df, "posts").sum())


def tr_total_engagements(df: pd.DataFrame) -> float:
    return float(_num(df, "engagements").sum())


def tr_total_impressions(df: pd.DataFrame) -> float:
    return float(_num(df, "est_impressions").sum())


def tr_total_video_views(df: pd.DataFrame) -> float:
    return float(_num(df, "video_views").sum())


def tr_engagement_rate(df: pd.DataFrame) -> float | None:
    # Same definition as the Traackr file: engagements / potential reach, as a percent.
    reach = _num(df, "potential_reach").sum()
    return round(float(_num(df, "engagements").sum()) / reach * 100, 3) if reach else None
def tr_vit_score(df: pd.DataFrame) -> float:
    return round(float(_num(df, "vit_score").sum()), 2)


def tr_visibility(df: pd.DataFrame) -> float:
    return round(float(_num(df, "visibility").sum()), 2)


def tr_impact(df: pd.DataFrame) -> float:
    return round(float(_num(df, "impact").sum()), 2)


def tr_trust(df: pd.DataFrame) -> float:
    return round(float(_num(df, "trust").sum()), 3)

def tr_engagements_by_brand(df: pd.DataFrame) -> dict[str, float]:
    return _by_category(df, "engagements", "brand", top=10)


def tr_campaign_count(df: pd.DataFrame) -> int:
    return int(len(df))


def tr_campaign_spend(df: pd.DataFrame) -> float:
    # Note: in the sample file only 3 of 18 campaigns had a spend value.
    return float(_num(df, "total_spend").sum())


# ---- The catalogue ---------------------------------------------------------------
KPIS: list[KPI] = [
    # ServiceNow
    KPI("sn_total_tasks", "Total tasks", "ServiceNow", total_tasks, ("state",), "card", "count"),
    KPI("sn_completion_rate", "Completion rate", "ServiceNow", completion_rate, ("state",), "card", "%"),
    KPI("sn_cancelled_tasks", "Cancelled tasks", "ServiceNow", cancelled_tasks, ("state",), "card", "count"),
    KPI("sn_tasks_by_sla", "Tasks by SLA type", "ServiceNow", tasks_by_sla,
        ("sla_definition",), "bar", "count", kind="breakdown"),
    KPI("sn_avg_duration", "Avg business duration", "ServiceNow", avg_business_duration,
        ("business_duration",), "card", "hours"),  # column is in seconds; the formula converts to hours
    KPI("sn_tasks_by_request_type", "Tasks by request type", "ServiceNow", tasks_by_request_type,
        ("request_type",), "bar", "count", kind="breakdown", table="requests"),
    KPI("sn_hours_by_request_type", "Avg hours by request type", "ServiceNow", avg_hours_by_request_type,
        ("request_type", "business_duration"), "bar", "hours", kind="breakdown", table="requests"),
    KPI("sn_tasks_by_region", "Tasks by region", "ServiceNow", tasks_by_region,
        ("region",), "bar", "count", kind="breakdown", table="request_detail"),
    KPI("sn_tasks_by_country", "Tasks by country", "ServiceNow", tasks_by_country,
        ("country",), "bar", "count", kind="breakdown", table="request_detail"),
    KPI("sn_tasks_by_priority", "Tasks by priority", "ServiceNow", tasks_by_priority,
        ("priority",), "bar", "count", kind="breakdown", table="request_detail"),    
    # Sprinklr
    KPI("sp_total_engagements", "Total engagements", "Sprinklr", total_engagements,
        ("engagements",), "card", "count", table="organic"),
    KPI("sp_engagements_by_account", "Engagements by account type", "Sprinklr", engagements_by_account_type,
        ("engagements", "account_type"), "bar", "count", kind="breakdown", table="organic"),
    KPI("sp_total_ad_spend", "Total ad spend", "Sprinklr", total_ad_spend,
        ("spend",), "card", "currency", table="paid"),
    KPI("sp_paid_engagements", "Paid engagements", "Sprinklr", paid_engagements,
        ("engagements",), "card", "count", table="paid"),
    KPI("sp_paid_impressions", "Paid impressions", "Sprinklr", paid_impressions,
        ("impressions",), "card", "count", table="paid"),
    KPI("sp_cpm", "CPM (cost per 1,000 impressions)", "Sprinklr", paid_cpm,
        ("spend", "impressions"), "card", "currency", table="paid"),
    # Traackr
    KPI("tr_total_posts", "Total posts", "Traackr", tr_total_posts,
        ("posts",), "card", "count", table="brands_total"),
    KPI("tr_total_engagements", "Total engagements", "Traackr", tr_total_engagements,
        ("engagements",), "card", "count", table="brands_total"),
    KPI("tr_total_impressions", "Est. impressions", "Traackr", tr_total_impressions,
        ("est_impressions",), "card", "count", table="brands_total"),
    KPI("tr_total_video_views", "Video views", "Traackr", tr_total_video_views,
        ("video_views",), "card", "count", table="brands_total"),
    KPI("tr_engagement_rate", "Engagement rate", "Traackr", tr_engagement_rate,
        ("engagements", "potential_reach"), "card", "%", table="brands_total"),
    KPI("tr_vit_score", "Brand Vitality Score", "Traackr", tr_vit_score,
        ("vit_score",), "card", "score", table="brands_total"),
    KPI("tr_visibility", "Visibility score", "Traackr", tr_visibility,
        ("visibility",), "card", "score", table="brands_total"),
    KPI("tr_impact", "Impact score", "Traackr", tr_impact,
        ("impact",), "card", "score", table="brands_total"),
    KPI("tr_trust", "Trust score", "Traackr", tr_trust,
        ("trust",), "card", "score", table="brands_total"),
    KPI("tr_engagements_by_brand", "Top brands by engagements", "Traackr", tr_engagements_by_brand,
        ("engagements", "brand"), "bar", "count", kind="breakdown", table="brands"),
    KPI("tr_am_total_posts", "Total posts (all markets)", "Traackr", tr_total_posts,
        ("posts",), "card", "count", table="all_markets_total"),
    KPI("tr_am_total_engagements", "Total engagements (all markets)", "Traackr", tr_total_engagements,
        ("engagements",), "card", "count", table="all_markets_total"),
    KPI("tr_am_engagements_by_brand", "Top brands by engagements (all markets)", "Traackr", tr_engagements_by_brand,
        ("engagements", "brand"), "bar", "count", kind="breakdown", table="all_markets"),
    KPI("tr_campaign_count", "Campaigns", "Traackr", tr_campaign_count,
        ("start_date",), "card", "count", table="campaigns"),
    KPI("tr_campaign_spend", "Campaign spend", "Traackr", tr_campaign_spend,
        ("total_spend",), "card", "currency", table="campaigns"),
]


# ---- Lookups used by kpi_engine.py ------------------------------------------------
def kpis_for_source(source: str) -> list[KPI]:
    """All KPIs defined for a source."""
    return [k for k in KPIS if k.source == source]


def split_by_columns(source: str, columns_by_table: dict[str, list[str]]) -> tuple[list[KPI], list[KPI]]:
    """Return (runnable, skipped). A KPI is skipped if its table or a needed column is missing.

    columns_by_table example: {"main": ["state", "created", ...]}
    """
    runnable, skipped = [], []
    for kpi in kpis_for_source(source):
        have = set(columns_by_table.get(kpi.table, []))
        (runnable if set(kpi.required_columns) <= have else skipped).append(kpi)
    return runnable, skipped