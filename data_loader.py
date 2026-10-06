"""data_loader.py - reads uploaded CSV / Excel files and builds data profiles.

What this module does
---------------------
1. Reads every CSV file and EVERY sheet of every Excel workbook.
2. Cleans the data lightly (header row detection, column names, number and
   date detection) and records a note for every change it makes.
3. Builds a profile (columns, types, missing values, sample rows, key
   candidates, links between files) that is safe to send to an LLM.

Nothing in here is specific to any dataset: no column names are hardcoded.
The LLM never receives the full data, only this profile.
"""

from __future__ import annotations

import csv
import io
import itertools
import os
import re
import warnings
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

SUPPORTED_EXTENSIONS = (".csv", ".xlsx", ".xls")
CSV_ENCODINGS = ("utf-8", "utf-8-sig", "cp1252", "latin-1")
CSV_DELIMITERS = ",;\t|"
MAX_HEADER_SCAN_ROWS = 10
MAX_CSV_COLUMNS = 300
MAX_OVERLAP_VALUES = 5000
MAX_SHARED_RESULTS = 30

# Whole-word identifier names (Customer ID, order_id, Zip Code) plus camelCase
# such as customerId. Plain words that merely end in "id" (Paid, Valid) or mean
# a quantity ("Number of Engagements") are NOT identifiers.
_ID_NAME_PATTERN = re.compile(
    r"(^|[^a-z])(id|key|code|zip|postal|phone|mobile|sku|ref)([^a-z]|$)|(?-i:[a-z]Id$)",
    re.IGNORECASE,
)
_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_PATTERN = re.compile(r"\+?\d[\d\s().-]{8,}\d")
_ISO_DATE_PATTERN = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class Dataset:
    """One table: a CSV file, or one sheet of an Excel workbook."""

    key: str  # human-readable id, e.g. "sales.xlsx :: Q1"
    alias: str  # safe Python variable name, e.g. "sales_q1"
    file_name: str
    sheet_name: str | None
    df: pd.DataFrame
    notes: list[str] = field(default_factory=list)


@dataclass
class LoadResult:
    datasets: list[Dataset] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _get_name_and_bytes(file: Any) -> tuple[str, bytes]:
    """Accept a Streamlit UploadedFile, a file object, or a path."""
    if isinstance(file, (str, os.PathLike)):
        with open(file, "rb") as handle:
            return os.path.basename(str(file)), handle.read()
    name = getattr(file, "name", "uploaded_file")
    if hasattr(file, "getvalue"):
        return name, file.getvalue()
    return name, file.read()


def _slug(text: str) -> str:
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_").lower()
    return slug or "data"


def _make_alias(file_name: str, sheet_name: str | None, used: set[str]) -> str:
    stem = os.path.splitext(file_name)[0]
    base = _slug(stem if sheet_name is None else f"{stem}_{sheet_name}")
    if base[0].isdigit():
        base = f"df_{base}"
    alias, counter = base, 2
    while alias in used:
        alias = f"{base}_{counter}"
        counter += 1
    used.add(alias)
    return alias


def _looks_like_identifier_name(name: str) -> bool:
    return bool(_ID_NAME_PATTERN.search(str(name)))


def _kind(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    if pd.api.types.is_numeric_dtype(series):
        return "number"
    return "text"


def _safe_nunique(series: pd.Series) -> int:
    try:
        return int(series.nunique(dropna=True))
    except TypeError:  # unhashable values such as lists
        return int(series.astype(str).nunique(dropna=True))


def _to_python(value: Any) -> Any:
    """Convert numpy / pandas values into plain JSON-friendly Python values."""
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp,)):
        return None if pd.isna(value) else value.strftime("%Y-%m-%d %H:%M:%S").replace(" 00:00:00", "")
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if np.isnan(value) else round(float(value), 4)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _mask_text(value: Any) -> Any:
    """Hide e-mail addresses and phone numbers in text before sending samples."""
    if not isinstance(value, str):
        return value

    def phone_repl(match: re.Match) -> str:
        text = match.group()
        digits = len(re.sub(r"\D", "", text))
        if digits < 10 or _ISO_DATE_PATTERN.match(text):
            return text
        return "[phone]"

    value = _EMAIL_PATTERN.sub("[email]", value)
    return _PHONE_PATTERN.sub(phone_repl, value)


# ---------------------------------------------------------------------------
# Header detection and cleaning
# ---------------------------------------------------------------------------
def _detect_header_row(raw: pd.DataFrame) -> int:
    """Find the first row that looks like column headings (skips title rows)."""
    if raw.empty:
        return 0
    widest = int(raw.notna().sum(axis=1).max())
    if widest == 0:
        return 0
    threshold = max(1, int(np.ceil(0.6 * widest)))
    for pos in range(len(raw)):
        row = raw.iloc[pos].dropna()
        if len(row) < threshold:
            continue
        text_share = sum(isinstance(v, str) for v in row) / len(row)
        if text_share >= 0.8:
            return pos
    return 0


def _clean_column_names(columns: Any, notes: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: dict[str, int] = {}
    for position, column in enumerate(columns, start=1):
        name = re.sub(r"\s+", " ", str(column)).strip()
        if not name or name.lower().startswith("unnamed:") or name.lower() == "nan":
            name = f"column_{position}"
            notes.append(f"Column {position} had no heading and was named '{name}'.")
        if name in seen:
            seen[name] += 1
            new_name = f"{name}_{seen[name]}"
            notes.append(f"Duplicate heading '{name}' renamed to '{new_name}'.")
            name = new_name
        else:
            seen[name] = 1
        cleaned.append(name)
    return cleaned


_NUMBER_CLEAN = re.compile(r"[\s,$€£₹¥%]")


def _try_numeric(series: pd.Series) -> tuple[pd.Series | None, bool]:
    """Convert text like '1,234', '$5.00', '12%' or '(300)' to numbers."""
    values = series.dropna().astype(str).str.strip()
    if values.empty:
        return None, False
    had_percent = bool(values.str.contains("%", regex=False).any())
    text = series.astype(str).str.strip()
    text = text.str.replace(r"^\((.*)\)$", r"-\1", regex=True)
    text = text.str.replace(_NUMBER_CLEAN, "", regex=True)
    converted = pd.to_numeric(text.where(series.notna()), errors="coerce")
    ok_share = converted.notna().sum() / max(1, series.notna().sum())
    if ok_share >= 0.9:
        return converted, had_percent
    return None, False


def _try_datetime(series: pd.Series) -> pd.Series | None:
    values = series.dropna().astype(str).str.strip()
    if values.empty or values.str.len().mean() < 6:
        return None
    if not values.str.contains(r"[-/:.]|[A-Za-z]{3}", regex=True).mean() > 0.8:
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        parsed = pd.to_datetime(series, errors="coerce")
    if parsed.notna().sum() / max(1, series.notna().sum()) >= 0.9:
        return parsed
    return None


def _convert_types(df: pd.DataFrame, notes: list[str]) -> pd.DataFrame:
    for column in df.columns:
        series = df[column]
        if not (series.dtype == object or pd.api.types.is_string_dtype(series)):
            continue  # already numeric / date / bool (pandas 3 uses a "str" dtype for text)
        stripped = series.map(lambda v: v.strip() if isinstance(v, str) else v)
        stripped = stripped.replace({"": np.nan})
        df[column] = stripped

        if _looks_like_identifier_name(column):
            continue  # keep identifiers as text (keeps leading zeros)
        sample = stripped.dropna().astype(str)
        if not sample.empty and sample.str.match(r"^0\d+$").any():
            continue  # zero-padded codes stay as text

        numeric, had_percent = _try_numeric(stripped)
        if numeric is not None:
            df[column] = numeric
            extra = " (values kept as written, e.g. 12% becomes 12)" if had_percent else ""
            notes.append(f"Column '{column}' was text and was converted to numbers{extra}.")
            continue
        parsed = _try_datetime(stripped)
        if parsed is not None:
            df[column] = parsed
            notes.append(f"Column '{column}' was text and was converted to dates.")
    return df


def _clean_dataframe(df: pd.DataFrame, notes: list[str]) -> pd.DataFrame:
    df = df.copy()
    df.columns = _clean_column_names(df.columns, notes)
    before_rows, before_cols = df.shape
    df = df.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
    if df.shape[0] < before_rows:
        notes.append(f"Removed {before_rows - df.shape[0]} completely empty row(s).")
    if df.shape[1] < before_cols:
        notes.append(f"Removed {before_cols - df.shape[1]} completely empty column(s).")
    return _convert_types(df, notes)


# ---------------------------------------------------------------------------
# Reading files
# ---------------------------------------------------------------------------
def _decode_text(data: bytes) -> str:
    for encoding in CSV_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def _detect_delimiter(text: str) -> str:
    sample = text[:8192]
    try:
        return csv.Sniffer().sniff(sample, delimiters=CSV_DELIMITERS).delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in CSV_DELIMITERS}
        best = max(counts, key=counts.get)
        return best if counts[best] > 0 else ","


def _read_csv(data: bytes, notes: list[str]) -> pd.DataFrame:
    text = _decode_text(data)
    delimiter = _detect_delimiter(text)
    raw = pd.read_csv(
        io.StringIO(text),
        sep=delimiter,
        header=None,
        names=list(range(MAX_CSV_COLUMNS)),
        engine="python",
        nrows=MAX_HEADER_SCAN_ROWS,
        dtype=str,
    ).dropna(axis=1, how="all")
    offset = _detect_header_row(raw)
    if offset:
        notes.append(f"Skipped {offset} line(s) above the column headings.")
    try:
        return pd.read_csv(io.StringIO(text), sep=delimiter, skiprows=offset, header=0)
    except pd.errors.ParserError:
        notes.append("Some malformed lines were skipped while reading the file.")
        return pd.read_csv(
            io.StringIO(text),
            sep=delimiter,
            skiprows=offset,
            header=0,
            engine="python",
            on_bad_lines="skip",
        )


def _read_excel_sheets(data: bytes) -> list[tuple[str, pd.DataFrame, list[str]]]:
    workbook = pd.ExcelFile(io.BytesIO(data))
    sheets = []
    for sheet in workbook.sheet_names:
        notes: list[str] = []
        raw = workbook.parse(sheet, header=None, nrows=MAX_HEADER_SCAN_ROWS)
        offset = _detect_header_row(raw)
        if offset:
            notes.append(f"Skipped {offset} row(s) above the column headings.")
        sheets.append((sheet, workbook.parse(sheet, header=offset), notes))
    return sheets


def load_uploaded_files(files: list[Any]) -> LoadResult:
    """Read all uploaded files. Problems are reported, never raised."""
    result = LoadResult()
    used_aliases: set[str] = set()

    for file in files:
        try:
            name, data = _get_name_and_bytes(file)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"Could not open a file: {exc}")
            continue

        extension = os.path.splitext(name)[1].lower()
        if extension not in SUPPORTED_EXTENSIONS:
            result.errors.append(f"{name}: unsupported type. Use CSV, XLSX or XLS.")
            continue

        try:
            if extension == ".csv":
                notes: list[str] = []
                tables = [(None, _read_csv(data, notes), notes)]
            else:
                tables = _read_excel_sheets(data)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"{name}: could not be read ({exc}).")
            continue

        for sheet_name, raw_df, notes in tables:
            df = _clean_dataframe(raw_df, notes)
            label = name if sheet_name is None else f"{name} :: {sheet_name}"
            if df.empty:
                result.errors.append(f"{label}: no data found, skipped.")
                continue
            result.datasets.append(
                Dataset(
                    key=label,
                    alias=_make_alias(name, sheet_name, used_aliases),
                    file_name=name,
                    sheet_name=sheet_name,
                    df=df,
                    notes=notes,
                )
            )
    return result


def datasets_to_namespace(datasets: list[Dataset]) -> dict[str, pd.DataFrame]:
    """Map each alias to its DataFrame. The code runner exposes these to the model's code."""
    return {dataset.alias: dataset.df for dataset in datasets}


# ---------------------------------------------------------------------------
# Profiling
# ---------------------------------------------------------------------------
def profile_column(series: pd.Series, total_rows: int) -> dict[str, Any]:
    non_null = series.dropna()
    missing = int(series.isna().sum())
    info: dict[str, Any] = {
        "name": str(series.name),
        "kind": _kind(series),
        "dtype": str(series.dtype),
        "missing": missing,
        "missing_pct": round(100 * missing / total_rows, 1) if total_rows else 0.0,
        "unique": _safe_nunique(non_null),
    }
    if non_null.empty:
        return info
    if info["kind"] == "number":
        info.update(
            min=_to_python(non_null.min()),
            max=_to_python(non_null.max()),
            mean=_to_python(non_null.mean()),
        )
    elif info["kind"] == "datetime":
        info.update(min=_to_python(non_null.min()), max=_to_python(non_null.max()))
    elif info["kind"] in ("text", "boolean"):
        counts = non_null.astype(str).value_counts()
        info["top_share"] = round(100 * counts.iloc[0] / len(non_null), 1)
        info["top_values"] = [
            f"{_mask_text(v)[:40]} ({100 * n / len(non_null):.1f}%)" for v, n in counts.head(3).items()
        ]
    return info


def _key_candidates(df: pd.DataFrame) -> list[str]:
    rows = len(df)
    found: list[str] = []
    if rows < 2:
        return found
    for column in df.columns:
        series = df[column]
        kind = _kind(series)
        if kind in ("datetime", "boolean"):
            continue
        non_null = int(series.notna().sum())
        id_like = _looks_like_identifier_name(column)
        if kind == "number" and not id_like and not pd.api.types.is_integer_dtype(series):
            continue
        if non_null >= 0.9 * rows and _safe_nunique(series) / max(1, non_null) >= 0.98:
            found.append(f"{column} (unique per row)")
        elif id_like:
            found.append(f"{column} (identifier-like, values repeat)")
    return found


def quality_issues(dataset: Dataset) -> list[str]:
    df = dataset.df
    issues: list[str] = []
    try:
        duplicates = int(df.duplicated().sum())
    except TypeError:
        duplicates = 0
    if duplicates:
        issues.append(f"{duplicates} fully duplicated row(s).")
    for column in df.columns:
        series = df[column]
        pct = 100 * series.isna().mean()
        if pct >= 20:
            issues.append(f"'{column}' is {pct:.0f}% empty.")
        if len(df) > 1 and _safe_nunique(series) <= 1 and series.notna().any():
            issues.append(f"'{column}' has only one distinct value.")
    return issues


def sample_records(dataset: Dataset, sample_rows: int = 5, mask: bool = True) -> list[dict[str, Any]]:
    records = []
    for row in dataset.df.head(sample_rows).to_dict(orient="records"):
        cleaned = {k: _to_python(v) for k, v in row.items()}
        if mask:
            cleaned = {k: _mask_text(v) for k, v in cleaned.items()}
        records.append(cleaned)
    return records


def _value_overlap(a: pd.Series, b: pd.Series) -> float | None:
    values_a = set(a.dropna().astype(str).unique()[:MAX_OVERLAP_VALUES])
    values_b = set(b.dropna().astype(str).unique()[:MAX_OVERLAP_VALUES])
    if not values_a or not values_b:
        return None
    return len(values_a & values_b) / min(len(values_a), len(values_b))


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def find_shared_columns(datasets: list[Dataset]) -> list[dict[str, Any]]:
    """Find columns with matching names across tables and how well their values overlap."""
    links: list[dict[str, Any]] = []
    for first, second in itertools.combinations(datasets, 2):
        lookup = {_norm_name(c): c for c in second.df.columns}
        for col_a in first.df.columns:
            col_b = lookup.get(_norm_name(col_a))
            if col_b is None:
                continue
            kind_a, kind_b = _kind(first.df[col_a]), _kind(second.df[col_b])
            if kind_a != kind_b or kind_a == "boolean":
                continue
            if kind_a == "number" and not (
                pd.api.types.is_integer_dtype(first.df[col_a]) or _looks_like_identifier_name(col_a)
            ):
                continue  # measures such as 'Spend' are not join keys
            overlap = _value_overlap(first.df[col_a], second.df[col_b])
            if overlap is None:
                continue
            links.append(
                {
                    "table_a": first.alias,
                    "column_a": col_a,
                    "table_b": second.alias,
                    "column_b": col_b,
                    "kind": kind_a,
                    "value_overlap": round(overlap, 2),
                    "strength": "strong" if overlap >= 0.5 else "weak",
                }
            )
    links.sort(key=lambda item: item["value_overlap"], reverse=True)
    return links[:MAX_SHARED_RESULTS]


# ---------------------------------------------------------------------------
# Output for the LLM and for the screen
# ---------------------------------------------------------------------------
def _describe_column(info: dict[str, Any]) -> str:
    parts = [f"{info['name']} [{info['kind']}]", f"missing {info['missing_pct']}%", f"unique {info['unique']}"]
    if info["kind"] == "number" and "min" in info:
        parts.append(f"min {info['min']}, max {info['max']}, mean {info['mean']}")
    elif info["kind"] == "datetime" and "min" in info:
        parts.append(f"from {info['min']} to {info['max']}")
    elif info["kind"] in ("text", "boolean") and info.get("top_values"):
        parts.append("common: " + ", ".join(info["top_values"]))
        if info.get("top_share", 0) > 98 or info["unique"] <= 2:
            parts.append("FLAG OR SKEWED - avoid as a chart category if another column works")
    return " | ".join(parts)


def profile_to_prompt_text(datasets: list[Dataset], sample_rows: int = 5, mask: bool = True) -> str:
    """Compact text description of all tables. This is what the LLM sees instead of the data."""
    if not datasets:
        return "No datasets loaded."
    blocks: list[str] = []
    for number, dataset in enumerate(datasets, start=1):
        df = dataset.df
        lines = [
            f"TABLE {number}: variable `{dataset.alias}`",
            f"  Source file: {dataset.file_name} | Sheet: {dataset.sheet_name or 'n/a (CSV)'}",
            f"  Size: {len(df)} rows x {len(df.columns)} columns",
            "  Columns:",
        ]
        lines += [f"   - {_describe_column(profile_column(df[c], len(df)))}" for c in df.columns]
        keys = _key_candidates(df)
        if keys:
            lines.append("  Key candidates: " + "; ".join(keys))
        issues = quality_issues(dataset)
        if issues or dataset.notes:
            lines.append("  Data notes: " + " ".join(dataset.notes + issues))
        lines.append(f"  First {sample_rows} rows:")
        for record in sample_records(dataset, sample_rows, mask):
            lines.append("   " + str(record))
        blocks.append("\n".join(lines))

    links = find_shared_columns(datasets)
    if len(datasets) > 1:
        if links:
            rows = [
                f"   - {l['table_a']}.{l['column_a']} <-> {l['table_b']}.{l['column_b']} "
                f"({l['strength']}, {int(l['value_overlap'] * 100)}% of values match)"
                for l in links
            ]
            blocks.append("POSSIBLE LINKS BETWEEN TABLES (same column name):\n" + "\n".join(rows))
        else:
            blocks.append("POSSIBLE LINKS BETWEEN TABLES: none found. Ask the user which columns to match on.")
    return "\n\n".join(blocks)


def datasets_summary_frame(datasets: list[Dataset]) -> pd.DataFrame:
    """One row per table, for a summary panel on screen."""
    rows = []
    for dataset in datasets:
        df = dataset.df
        try:
            duplicates = int(df.duplicated().sum())
        except TypeError:
            duplicates = 0
        rows.append(
            {
                "File": dataset.file_name,
                "Sheet": dataset.sheet_name or "-",
                "Variable": dataset.alias,
                "Rows": len(df),
                "Columns": len(df.columns),
                "Empty cells %": round(100 * df.isna().sum().sum() / max(1, df.size), 1),
                "Duplicate rows": duplicates,
            }
        )
    return pd.DataFrame(rows)


def column_profile_frame(dataset: Dataset) -> pd.DataFrame:
    """One row per column, for the data-profile expander on screen."""
    rows = []
    for column in dataset.df.columns:
        info = profile_column(dataset.df[column], len(dataset.df))
        if info["kind"] == "number" and "min" in info:
            detail = f"{info['min']} to {info['max']} (mean {info['mean']})"
        elif info["kind"] == "datetime" and "min" in info:
            detail = f"{info['min']} to {info['max']}"
        elif info.get("top_values"):
            detail = ", ".join(info["top_values"])
        else:
            detail = ""
        rows.append(
            {
                "Column": info["name"],
                "Type": info["kind"],
                "Missing %": info["missing_pct"],
                "Unique": info["unique"],
                "Detail": detail,
            }
        )
    return pd.DataFrame(rows)
