"""
AI-Powered Data Analytics Agent
Flask web application for marketing and digital analytics workflows.
"""
import os
import io
import json
import uuid
import base64
import logging
import traceback
from datetime import datetime
from threading import Lock

import numpy as np
import pandas as pd
from flask import (
    Flask, render_template, request, jsonify, session, send_file, abort
)
from werkzeug.utils import secure_filename

# Charting
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio
from plotly.utils import PlotlyJSONEncoder

# Stats
from scipy import stats

# Excel
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils.dataframe import dataframe_to_rows

# PDF
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table,
    TableStyle, Image as RLImage
)


# -----------------------------------------------------------------------------
# App setup
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("analytics-agent")

app = Flask(__name__)
# Stable secret key so session cookies survive restarts / across workers.
# If FLASK_SECRET_KEY isn't set, fall back to a random key (dev mode only).
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# SESSION_COOKIE_SECURE is left at default (False) so local HTTP dev works;
# on HTTPS hosts (Render/Railway) browsers still accept non-Secure cookies.

# In-memory store keyed by session token. Not persisted.
STORE: dict = {}
STORE_LOCK = Lock()

ACCENT = "#C0502D"  # Terracotta — Claude design accent
PALETTE = ["#C0502D", "#E07B4A", "#853425", "#8A8172", "#B07A1F", "#4E7C4A", "#C9A66B", "#1F1B17"]


# -----------------------------------------------------------------------------
# Session helpers
# -----------------------------------------------------------------------------
def get_sid() -> str:
    if "sid" not in session:
        session["sid"] = uuid.uuid4().hex
    return session["sid"]


def get_state() -> dict:
    sid = get_sid()
    with STORE_LOCK:
        if sid not in STORE:
            STORE[sid] = {}
        return STORE[sid]


# -----------------------------------------------------------------------------
# Data cleaning
# -----------------------------------------------------------------------------
def clean_dataframe(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Return cleaned df + summary of changes."""
    summary = {
        "original_shape": list(df.shape),
        "whitespace_columns_fixed": [],
        "duplicates_removed": 0,
        "nulls_filled": {},
        "nulls_dropped": {},
        "types_inferred": {},
        "casing_normalized": [],
    }

    # 1. Strip whitespace from column names
    new_cols = {c: str(c).strip() for c in df.columns}
    changed = [c for c, nc in new_cols.items() if c != nc]
    df = df.rename(columns=new_cols)
    summary["whitespace_columns_fixed"] = changed

    # 2. Remove duplicate rows
    before = len(df)
    df = df.drop_duplicates().reset_index(drop=True)
    summary["duplicates_removed"] = before - len(df)

    # 3. Handle strings: strip whitespace + conservative casing normalization.
    # Only collapse casing when a low-cardinality column has duplicate labels
    # that differ *only* by case (e.g. "USA" + "usa"). Preserve original
    # casing otherwise so acronyms and proper nouns (USA, iPhone) survive.
    for col in df.select_dtypes(include=["object"]).columns:
        s = df[col].astype(str).str.strip()
        nunique = s.nunique(dropna=True)
        if 0 < nunique <= max(20, int(len(s) * 0.05)):
            lower = s.str.lower()
            if lower.nunique(dropna=True) < nunique:
                # Canonicalize each label to its most-common original form.
                canon = (
                    s.groupby(lower).agg(lambda x: x.value_counts().idxmax())
                )
                s = lower.map(canon)
                summary["casing_normalized"].append(col)
        df[col] = s.replace({"nan": np.nan, "None": np.nan, "NaN": np.nan, "": np.nan})

    # 4. Type inference — booleans (strict), dates (strict), numerics.
    for col in df.columns:
        if df[col].dtype != object:
            continue
        sample = df[col].dropna().astype(str).head(200)
        if len(sample) == 0:
            continue

        lower_vals = set(sample.str.lower().str.strip().unique())
        # Boolean: require textual truthy/falsy tokens — do NOT hijack "0"/"1".
        bool_textual = {"true", "false", "yes", "no", "t", "f"}
        if lower_vals.issubset(bool_textual) and 1 <= len(lower_vals) <= 2:
            df[col] = df[col].astype(str).str.lower().str.strip().map(
                {"true": True, "false": False, "yes": True, "no": False,
                 "t": True, "f": False}
            )
            summary["types_inferred"][col] = "boolean"
            continue

        # Date: require ≥85% of non-null values to parse successfully AND
        # that at least two distinct dates are found (prevents integer IDs
        # like 20240101 from being interpreted as dates against the user's
        # intent).
        try:
            parsed = pd.to_datetime(sample, errors="coerce", utc=False)
            ok = parsed.notna().sum()
            if ok >= 0.85 * len(sample) and parsed.dropna().nunique() >= 2:
                looks_like_number = sample.str.match(r"^-?\d+(\.\d+)?$").mean() > 0.8
                if not looks_like_number:
                    df[col] = pd.to_datetime(df[col], errors="coerce")
                    summary["types_inferred"][col] = "datetime"
                    continue
        except (ValueError, TypeError):
            pass

        # Numeric: strip currency/thousands/percent/whitespace. Track whether
        # a percent sign was present so we can record it (values like "12%"
        # become 12.0 — we do NOT divide by 100 to preserve the user's units).
        stripped = sample.str.replace(r"[,$\s]", "", regex=True).str.rstrip("%")
        had_percent = sample.str.contains("%").any()
        numeric_try = pd.to_numeric(stripped, errors="coerce")
        if numeric_try.notna().sum() >= 0.9 * len(sample):
            full_stripped = df[col].astype(str).str.replace(r"[,$\s]", "", regex=True).str.rstrip("%")
            df[col] = pd.to_numeric(full_stripped, errors="coerce")
            summary["types_inferred"][col] = "numeric (percent)" if had_percent else "numeric"

    # 5. Fill / drop nulls
    for col in df.columns:
        n_null = int(df[col].isna().sum())
        if n_null == 0:
            continue
        null_pct = n_null / len(df) if len(df) else 0
        if null_pct > 0.5:
            df = df.drop(columns=[col])
            summary["nulls_dropped"][col] = n_null
            continue
        if pd.api.types.is_numeric_dtype(df[col]):
            df[col] = df[col].fillna(df[col].median())
            summary["nulls_filled"][col] = f"{n_null} filled with median"
        elif pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].ffill().bfill()
            summary["nulls_filled"][col] = f"{n_null} filled via forward/backward fill"
        else:
            mode = df[col].mode()
            if len(mode) > 0:
                df[col] = df[col].fillna(mode.iloc[0])
                summary["nulls_filled"][col] = f"{n_null} filled with mode"

    summary["cleaned_shape"] = list(df.shape)
    return df, summary


# -----------------------------------------------------------------------------
# Dataset profiling
# -----------------------------------------------------------------------------
def profile_dataframe(df: pd.DataFrame) -> dict:
    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    date_cols = df.select_dtypes(include=["datetime64[ns]", "datetime64"]).columns.tolist()
    bool_cols = df.select_dtypes(include=["bool"]).columns.tolist()
    cat_cols = [c for c in df.columns if c not in numeric_cols + date_cols + bool_cols]

    return {
        "shape": list(df.shape),
        "columns": list(df.columns),
        "dtypes": {c: str(df[c].dtype) for c in df.columns},
        "numeric_cols": numeric_cols,
        "date_cols": date_cols,
        "bool_cols": bool_cols,
        "categorical_cols": cat_cols,
        "null_counts": {c: int(df[c].isna().sum()) for c in df.columns},
        "nunique": {c: int(df[c].nunique()) for c in df.columns},
        "sample_rows": df.head(5).astype(str).to_dict(orient="records"),
        "describe_numeric": (
            df[numeric_cols].describe().round(4).to_dict() if numeric_cols else {}
        ),
    }


# -----------------------------------------------------------------------------
# Domain templates
# -----------------------------------------------------------------------------
TEMPLATES = {
    "email": {
        "name": "Email Marketing",
        "kpis": "open rate, click-through rate, unsubscribe rate, bounce rate, send volume, engagement over time, subject line performance",
        "guidance": "Focus on deliverability and engagement. Look for time-of-day or subject-line patterns. Compute rates as percentages when possible.",
    },
    "campaign": {
        "name": "Campaign Performance",
        "kpis": "impressions, clicks, CTR, conversions, cost per acquisition, ROAS, channel comparison, trend over time",
        "guidance": "Benchmark channels against each other. Surface efficiency metrics (CPA, ROAS) and trend shifts.",
    },
    "abtest": {
        "name": "A/B Testing",
        "kpis": "variant comparison, statistical significance, conversion lift, sample size adequacy, confidence intervals, winner recommendation",
        "guidance": "Run significance tests (chi-square or t-test) and state winner with confidence level. Call out if sample size is too small.",
    },
    "site": {
        "name": "Site Usage",
        "kpis": "sessions, bounce rate, pages per session, top pages, traffic sources, device breakdown, funnel drop-off",
        "guidance": "Identify top and bottom pages, device/source segments, and any funnel leaks.",
    },
    "sales": {
        "name": "Sales Performance",
        "kpis": "revenue by region or product, order volume, average order value, growth rate, top performers, period-over-period comparison",
        "guidance": "Compare segments and time periods. Highlight outperformers and laggards.",
    },
    "benchmark": {
        "name": "Benchmark Survey",
        "kpis": "response distribution, average scores by category, top/bottom performing segments, trend comparison",
        "guidance": "Summarize distributions and compare segments. If time data exists, show trend.",
    },
    "general": {
        "name": "General All-Inclusive Analysis",
        "kpis": "all relevant trends, distributions, segment comparisons, and notable relationships",
        "guidance": "Explore the dataset broadly. Pick the 6-10 most informative charts.",
    },
}


# -----------------------------------------------------------------------------
# AI provider abstraction
# -----------------------------------------------------------------------------
ANALYSIS_PROMPT = """You are a senior marketing analytics expert. Analyze this dataset and return ONLY valid JSON.

DATASET PROFILE:
{profile_json}

ANALYSIS MODE: {mode}
DOMAIN KPIS: {kpis}
DOMAIN GUIDANCE: {guidance}
CUSTOM USER QUESTIONS: {custom}
BENCHMARKS: {benchmarks}

Return a JSON object with EXACTLY this schema (no prose outside JSON):
{{
  "executive_summary": "3-5 sentences summarizing the dataset and main findings",
  "kpi_cards": [
    {{"label": "string", "value": "string (formatted)", "subtext": "string"}}
  ],
  "analyses": [
    {{
      "title": "Chart title",
      "chart_type": "bar|line|pie|scatter|histogram|box|area|heatmap",
      "x": "column_name or null",
      "y": "column_name or null",
      "color": "column_name or null",
      "agg": "sum|mean|count|median|none",
      "insight": "2-3 sentence plain English takeaway"
    }}
  ],
  "data_quality_notes": ["observation 1", "observation 2"],
  "followup_questions": ["question 1", "question 2", "question 3"],
  "sql_queries": [
    {{"title": "Query description", "sql": "SELECT ... (Snowflake compatible)"}}
  ]
}}

REQUIREMENTS:
- Produce between 6 and 10 analyses, prioritizing the most informative ones.
- Produce 4 to 6 KPI cards.
- Use ONLY columns from the dataset profile.
- If custom questions are provided, address each one in either analyses or summary.
- SQL must be Snowflake-compatible and assume the table is named `dataset`.
- Respond with ONLY the JSON object, no markdown fences, no commentary.
"""


def _safe_json_extract(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in model response")
    return json.loads(text[start:end + 1])


def _build_prompt(profile: dict, mode: str, custom: str, benchmarks: list) -> str:
    tpl = TEMPLATES.get(mode, TEMPLATES["general"])
    return ANALYSIS_PROMPT.format(
        profile_json=json.dumps(profile, default=str)[:12000],
        mode=tpl["name"],
        kpis=tpl["kpis"],
        guidance=tpl["guidance"],
        custom=custom or "None",
        benchmarks=json.dumps(benchmarks) if benchmarks else "None",
    )


def call_openai(api_key: str, prompt: str, strict: bool = False) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    system = "You are a data analytics expert. Return only valid JSON."
    if strict:
        system += " Your previous response was not valid JSON. Return ONLY a JSON object with no other text, no markdown, no fences."
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content


def call_anthropic(api_key: str, prompt: str, strict: bool = False) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    system = "You are a data analytics expert. Return only valid JSON, no markdown fences, no prose."
    if strict:
        system += " CRITICAL: Return ONLY a JSON object starting with { and ending with }. No other text."
    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


def call_gemini(api_key: str, prompt: str, strict: bool = False) -> str:
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    system = "You are a data analytics expert. Return only valid JSON with no markdown fences."
    if strict:
        system += " Return ONLY a JSON object. No prose. No code fences."
    model = genai.GenerativeModel(
        "gemini-1.5-pro",
        system_instruction=system,
        generation_config={"response_mime_type": "application/json", "temperature": 0.2},
    )
    resp = model.generate_content(prompt)
    return resp.text


def analyze(provider: str, api_key: str, profile: dict, mode: str,
            custom: str, benchmarks: list) -> dict:
    """Unified AI provider call with one retry on malformed JSON."""
    prompt = _build_prompt(profile, mode, custom, benchmarks)
    caller = {"openai": call_openai, "anthropic": call_anthropic, "gemini": call_gemini}.get(provider)
    if not caller:
        raise ValueError(f"Unknown provider: {provider}")

    try:
        raw = caller(api_key, prompt, strict=False)
        return _safe_json_extract(raw)
    except (json.JSONDecodeError, ValueError) as e:
        log.warning(f"First AI call produced invalid JSON ({e}); retrying with stricter prompt.")
        raw = caller(api_key, prompt, strict=True)
        return _safe_json_extract(raw)


# -----------------------------------------------------------------------------
# Chart builders
# -----------------------------------------------------------------------------
def _fig_layout(fig, title):
    fig.update_layout(
        title=dict(
            text=title,
            font=dict(size=15, family="Inter, -apple-system, system-ui, sans-serif", color="#141815"),
            x=0, xanchor="left", pad=dict(l=6),
        ),
        font=dict(family="Inter, -apple-system, system-ui, sans-serif", color="#141815", size=12),
        plot_bgcolor="#FFFFFF",
        paper_bgcolor="#FFFFFF",
        margin=dict(l=50, r=30, t=50, b=50),
        colorway=PALETTE,
        legend=dict(bgcolor="rgba(255,255,255,0)", bordercolor="#E2E7E2", borderwidth=0, font=dict(size=11)),
        hoverlabel=dict(bgcolor="#141815", font=dict(color="#F6F7F4", family="Inter")),
    )
    fig.update_xaxes(showgrid=True, gridcolor="#EDEFE9", zeroline=False, linecolor="#E2E7E2")
    fig.update_yaxes(showgrid=True, gridcolor="#EDEFE9", zeroline=False, linecolor="#E2E7E2")
    return fig


def build_chart(df: pd.DataFrame, spec: dict) -> dict | None:
    """Convert an AI chart spec into a Plotly figure dict. Returns None if columns missing."""
    try:
        ctype = (spec.get("chart_type") or "bar").lower()
        title = spec.get("title") or "Chart"
        x = spec.get("x")
        y = spec.get("y")
        color = spec.get("color")
        agg = (spec.get("agg") or "none").lower()

        cols = [c for c in [x, y, color] if c]
        missing = [c for c in cols if c and c not in df.columns]
        if missing:
            log.warning(f"Skipping chart '{title}': missing columns {missing}")
            return None

        d = df.copy()
        if agg in ("sum", "mean", "count", "median") and x and y and y in df.columns:
            group_cols = [x] + ([color] if color and color != x else [])
            if agg == "count":
                d = d.groupby(group_cols, dropna=False).size().reset_index(name=y)
            else:
                d = d.groupby(group_cols, dropna=False)[y].agg(agg).reset_index()

        if ctype == "bar":
            fig = px.bar(d, x=x, y=y, color=color)
        elif ctype == "line":
            if x and pd.api.types.is_datetime64_any_dtype(d[x]):
                d = d.sort_values(x)
            fig = px.line(d, x=x, y=y, color=color, markers=True)
        elif ctype == "pie":
            fig = px.pie(d, names=x, values=y if y else None)
        elif ctype == "scatter":
            fig = px.scatter(d, x=x, y=y, color=color, opacity=0.75)
        elif ctype == "histogram":
            fig = px.histogram(d, x=x, color=color)
        elif ctype == "box":
            fig = px.box(d, x=x, y=y, color=color)
        elif ctype == "area":
            fig = px.area(d, x=x, y=y, color=color)
        elif ctype == "heatmap":
            if x and y:
                pivot = df.pivot_table(index=y, columns=x, aggfunc="size", fill_value=0)
                fig = px.imshow(pivot, color_continuous_scale=[[0, "#ECFDF5"], [0.5, "#22C55E"], [1, "#14532D"]])
            else:
                return None
        else:
            fig = px.bar(d, x=x, y=y, color=color)

        fig = _fig_layout(fig, title)
        return json.loads(json.dumps(fig.to_dict(), cls=PlotlyJSONEncoder))
    except Exception as e:
        log.warning(f"Chart build failed for '{spec.get('title')}': {e}")
        return None


def correlation_heatmap(df: pd.DataFrame) -> dict | None:
    numeric = df.select_dtypes(include=[np.number])
    if numeric.shape[1] < 2:
        return None
    corr = numeric.corr().round(3)
    fig = px.imshow(
        corr, text_auto=True, color_continuous_scale="RdBu_r",
        zmin=-1, zmax=1, aspect="auto"
    )
    fig = _fig_layout(fig, "Correlation Heatmap (Numeric Columns)")
    return json.loads(json.dumps(fig.to_dict(), cls=PlotlyJSONEncoder))


def time_series_trend(df: pd.DataFrame) -> dict | None:
    date_cols = df.select_dtypes(include=["datetime64[ns]"]).columns.tolist()
    numeric = df.select_dtypes(include=[np.number]).columns.tolist()
    if not date_cols or not numeric:
        return None
    dc, nc = date_cols[0], numeric[0]
    d = df[[dc, nc]].dropna().sort_values(dc)
    if len(d) < 5:
        return None
    fig = px.line(d, x=dc, y=nc, markers=True, title=f"{nc} trend over {dc}")
    fig = _fig_layout(fig, f"Time Series — {nc} over {dc}")
    return json.loads(json.dumps(fig.to_dict(), cls=PlotlyJSONEncoder))


def detect_outliers(df: pd.DataFrame) -> pd.DataFrame:
    numeric = df.select_dtypes(include=[np.number])
    if numeric.empty:
        return pd.DataFrame()
    mask = pd.Series(False, index=df.index)
    reasons = pd.Series("", index=df.index, dtype=object)
    for col in numeric.columns:
        q1, q3 = numeric[col].quantile(0.25), numeric[col].quantile(0.75)
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        col_mask = (numeric[col] < lo) | (numeric[col] > hi)
        reasons.loc[col_mask] = reasons.loc[col_mask].astype(str) + f"{col} "
        mask |= col_mask
    out = df[mask].copy()
    if not out.empty:
        out["_outlier_cols"] = reasons[mask].str.strip()
    return out.head(100)


def run_ab_significance(df: pd.DataFrame) -> dict | None:
    """Try to detect variant + metric columns and run a significance test."""
    cols_lower = {c.lower(): c for c in df.columns}
    variant_col = next((cols_lower[k] for k in cols_lower
                        if "variant" in k or "group" in k or k in ("a_b", "ab", "test")), None)
    if not variant_col:
        for c in df.columns:
            if df[c].nunique() == 2 and df[c].dtype == object:
                variant_col = c
                break
    if not variant_col:
        return None

    groups = df[variant_col].dropna().unique()
    if len(groups) != 2:
        return None

    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    if not numeric_cols:
        return None
    metric = numeric_cols[0]

    a = df[df[variant_col] == groups[0]][metric].dropna()
    b = df[df[variant_col] == groups[1]][metric].dropna()
    if len(a) < 10 or len(b) < 10:
        return {"summary": "Sample too small for reliable A/B test.", "significant": False}

    t_stat, p_val = stats.ttest_ind(a, b, equal_var=False)
    mean_a, mean_b = a.mean(), b.mean()
    lift = ((mean_b - mean_a) / mean_a * 100) if mean_a else 0
    significant = p_val < 0.05
    winner = groups[1] if mean_b > mean_a else groups[0]

    return {
        "variant_col": variant_col,
        "metric": metric,
        "group_a": str(groups[0]), "mean_a": round(float(mean_a), 4), "n_a": int(len(a)),
        "group_b": str(groups[1]), "mean_b": round(float(mean_b), 4), "n_b": int(len(b)),
        "t_stat": round(float(t_stat), 4),
        "p_value": round(float(p_val), 5),
        "lift_pct": round(float(lift), 2),
        "significant": bool(significant),
        "winner": str(winner),
        "summary": (
            f"Variant '{winner}' has a mean {metric} of "
            f"{(mean_b if winner == groups[1] else mean_a):.3f} vs "
            f"{(mean_a if winner == groups[1] else mean_b):.3f} — "
            f"a {abs(lift):.1f}% {'lift' if lift > 0 else 'decline'}. "
            f"p = {p_val:.4f} — {'statistically significant' if significant else 'NOT statistically significant'} at α=0.05."
        ),
    }


# -----------------------------------------------------------------------------
# SQL generation
# -----------------------------------------------------------------------------
SNOWFLAKE_TYPE_MAP = {
    "int64": "NUMBER", "int32": "NUMBER", "float64": "FLOAT", "float32": "FLOAT",
    "bool": "BOOLEAN",
    "datetime64[ns]": "TIMESTAMP_NTZ",
    "object": "VARCHAR",
}


def sql_create_table(df: pd.DataFrame, table_name: str = "dataset") -> str:
    cols = []
    for c in df.columns:
        dtype = str(df[c].dtype)
        sql_type = SNOWFLAKE_TYPE_MAP.get(dtype, "VARCHAR")
        clean_name = '"' + str(c).replace('"', '') + '"'
        cols.append(f"  {clean_name} {sql_type}")
    return f"CREATE OR REPLACE TABLE dataset (\n" + ",\n".join(cols) + "\n);"


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"ok": True, "ts": datetime.utcnow().isoformat()})


def detect_provider(api_key: str) -> str | None:
    """Infer provider from API key format.

    - Anthropic keys: 'sk-ant-...'
    - Google Gemini keys: 'AIza...'  (Google API key pattern)
    - OpenAI keys: 'sk-...' or 'sk-proj-...'
    """
    if not api_key:
        return None
    k = api_key.strip()
    if k.startswith("sk-ant-"):
        return "anthropic"
    if k.startswith("AIza"):
        return "gemini"
    if k.startswith("sk-"):
        return "openai"
    return None


@app.route("/api/config", methods=["POST"])
def api_config():
    data = request.get_json(silent=True) or {}
    api_key = (data.get("api_key") or "").strip()
    if not api_key or len(api_key) < 10:
        return jsonify({"error": "API key missing or too short"}), 400

    provider = detect_provider(api_key)
    if not provider:
        return jsonify({
            "error": "Unrecognized API key format. Expected an OpenAI (sk-...), Anthropic (sk-ant-...), or Google Gemini (AIza...) key.",
        }), 400

    state = get_state()
    state["provider"] = provider
    state["api_key"] = api_key

    labels = {
        "openai": "OpenAI · gpt-4o",
        "anthropic": "Anthropic · claude-sonnet-4",
        "gemini": "Google · gemini-1.5-pro",
    }
    return jsonify({"ok": True, "provider": provider, "label": labels[provider]})


@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    filename = secure_filename(f.filename or "dataset.csv")

    try:
        raw = f.read()
        # Try common encodings
        for enc in ("utf-8", "utf-8-sig", "latin-1", "cp1252"):
            try:
                df = pd.read_csv(io.BytesIO(raw), encoding=enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            return jsonify({"error": "Could not decode CSV. Try UTF-8 encoding."}), 400

        if df.empty:
            return jsonify({"error": "CSV is empty"}), 400

        state = get_state()
        state["original_df"] = df.copy()
        state["filename"] = filename
        return jsonify({
            "ok": True,
            "filename": filename,
            "rows": int(len(df)),
            "cols": int(len(df.columns)),
            "columns": list(df.columns),
        })
    except pd.errors.ParserError as e:
        return jsonify({"error": f"Malformed CSV: {str(e)[:200]}"}), 400
    except Exception as e:
        log.error(f"Upload failed: {e}\n{traceback.format_exc()}")
        return jsonify({"error": f"Upload failed: {str(e)[:200]}"}), 500


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    state = get_state()
    body = request.get_json(silent=True) or {}

    # Accept api_key from request body (preferred, survives server restarts)
    # or fall back to server session state.
    api_key = (body.get("api_key") or state.get("api_key") or "").strip()
    provider = detect_provider(api_key) if api_key else state.get("provider")

    if "original_df" not in state:
        return jsonify({"error": "No dataset found on the server. Please re-upload your CSV."}), 400
    if not api_key or not provider:
        return jsonify({"error": "API key missing or unrecognized. Please re-enter your key and click Connect."}), 400

    # Cache for subsequent calls (chat, etc.)
    state["api_key"] = api_key
    state["provider"] = provider

    mode = body.get("mode", "general")
    custom = body.get("custom", "")
    benchmarks = body.get("benchmarks", [])  # list of {metric, value}

    try:
        # 1. Clean
        df, clean_summary = clean_dataframe(state["original_df"].copy())
        state["cleaned_df"] = df
        state["clean_summary"] = clean_summary

        # 2. Profile
        profile = profile_dataframe(df)

        # 3. AI analysis
        ai = analyze(provider, api_key, profile, mode, custom, benchmarks)

        # 4. Build charts
        charts = []
        for spec in ai.get("analyses", []):
            fig = build_chart(df, spec)
            if fig:
                charts.append({
                    "title": spec.get("title"),
                    "insight": spec.get("insight", ""),
                    "figure": fig,
                })

        # 5. Auto features
        corr = correlation_heatmap(df)
        ts = time_series_trend(df)
        outliers_df = detect_outliers(df)
        ab = run_ab_significance(df) if mode == "abtest" or mode == "general" else None

        # 6. SQL
        create_sql = sql_create_table(df)
        sql_queries = [{"title": "CREATE TABLE", "sql": create_sql}]
        sql_queries.extend(ai.get("sql_queries", []))

        # 7. Benchmark overlays — simple attachment to response, JS renders reference lines
        state["last_analysis"] = {
            "mode": mode,
            "custom": custom,
            "benchmarks": benchmarks,
            "ai": ai,
            "charts": charts,
            "correlation": corr,
            "timeseries": ts,
            "ab_test": ab,
            "sql_queries": sql_queries,
            "outliers_count": len(outliers_df),
        }

        # 8. Response payload
        return jsonify({
            "ok": True,
            "filename": state.get("filename"),
            "rows": int(len(df)),
            "cols": int(len(df.columns)),
            "mode": TEMPLATES.get(mode, TEMPLATES["general"])["name"],
            "clean_summary": clean_summary,
            "profile": {
                "columns": profile["columns"],
                "dtypes": profile["dtypes"],
                "null_counts": profile["null_counts"],
                "nunique": profile["nunique"],
            },
            "executive_summary": ai.get("executive_summary", ""),
            "kpi_cards": ai.get("kpi_cards", []),
            "data_quality_notes": ai.get("data_quality_notes", []),
            "followup_questions": ai.get("followup_questions", []),
            "charts": charts,
            "correlation": corr,
            "timeseries": ts,
            "ab_test": ab,
            "outliers": {
                "count": int(len(outliers_df)),
                "rows": outliers_df.head(50).astype(str).to_dict(orient="records"),
            },
            "sql_queries": sql_queries,
            "benchmarks": benchmarks,
            "preview": df.head(200).astype(str).to_dict(orient="records"),
        })
    except Exception as e:
        log.error(f"Analysis failed: {e}\n{traceback.format_exc()}")
        return jsonify({"error": f"Analysis failed: {str(e)[:300]}"}), 500


@app.route("/api/column/<name>", methods=["GET"])
def api_column(name):
    state = get_state()
    df = state.get("cleaned_df")
    if df is None or name not in df.columns:
        return jsonify({"error": "Column not found"}), 404

    s = df[name]
    info = {
        "name": name,
        "dtype": str(s.dtype),
        "null_pct": round(float(s.isna().mean() * 100), 2),
        "unique": int(s.nunique()),
        "total": int(len(s)),
    }
    if pd.api.types.is_numeric_dtype(s):
        info.update({
            "min": float(s.min()) if s.notna().any() else None,
            "max": float(s.max()) if s.notna().any() else None,
            "mean": round(float(s.mean()), 4) if s.notna().any() else None,
            "median": round(float(s.median()), 4) if s.notna().any() else None,
        })
        fig = px.histogram(s.dropna(), nbins=30)
    else:
        top = s.value_counts().head(15).reset_index()
        top.columns = [name, "count"]
        fig = px.bar(top, x=name, y="count")

    fig = _fig_layout(fig, f"{name} distribution")
    info["figure"] = json.loads(json.dumps(fig.to_dict(), cls=PlotlyJSONEncoder))
    return jsonify(info)


@app.route("/api/chat", methods=["POST"])
def api_chat():
    state = get_state()
    df = state.get("cleaned_df")
    if df is None:
        return jsonify({"error": "No dataset analyzed yet"}), 400

    body = request.get_json(silent=True) or {}
    api_key = (body.get("api_key") or state.get("api_key") or "").strip()
    provider = detect_provider(api_key) if api_key else state.get("provider")
    if not api_key or not provider:
        return jsonify({"error": "API key missing. Please re-enter your key."}), 400

    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"error": "Empty question"}), 400

    profile = profile_dataframe(df)
    prompt = f"""You are a data analyst. Answer the user's question in 2-4 sentences of plain English based on this dataset summary.

DATASET SUMMARY:
{json.dumps(profile, default=str)[:6000]}

USER QUESTION: {question}

Return a JSON object: {{"answer": "your plain-English answer"}}"""

    try:
        caller = {
            "openai": call_openai,
            "anthropic": call_anthropic,
            "gemini": call_gemini,
        }[provider]
        raw = caller(api_key, prompt, strict=False)
        try:
            parsed = _safe_json_extract(raw)
            return jsonify({"ok": True, "answer": parsed.get("answer", raw)})
        except Exception:
            return jsonify({"ok": True, "answer": raw.strip()})
    except Exception as e:
        log.error(f"Chat failed: {e}")
        return jsonify({"error": f"Chat failed: {str(e)[:200]}"}), 500


# -----------------------------------------------------------------------------
# Exports
# -----------------------------------------------------------------------------
@app.route("/api/export/excel", methods=["POST", "GET"])
def export_excel():
    """Prefers POST body (client-supplied payload, survives server restarts);
    falls back to server session state if POST body is empty."""
    state = get_state()
    body = request.get_json(silent=True) or {}

    df = None
    if body.get("rows") and body.get("columns"):
        # Client-supplied cleaned dataset (list of dicts) + column order
        df = pd.DataFrame(body["rows"], columns=body["columns"])
    else:
        df = state.get("cleaned_df")

    last = body.get("last_analysis") or state.get("last_analysis", {})
    clean = body.get("clean_summary") or state.get("clean_summary", {})
    filename = body.get("filename") or state.get("filename") or "dataset.csv"

    if df is None:
        return jsonify({"error": "No analysis data provided. Re-run the analysis first."}), 400

    wb = Workbook()

    # Summary tab
    ws = wb.active
    ws.title = "Summary"
    header_font = Font(bold=True, size=14, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="C0502D")

    ws["A1"] = "AI Analytics Report"
    ws["A1"].font = Font(bold=True, size=18)
    ws["A2"] = f"Dataset: {filename}"
    ws["A3"] = f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"

    ws["A5"] = "Executive Summary"
    ws["A5"].font = header_font
    ws["A5"].fill = header_fill
    ws["A6"] = last.get("ai", {}).get("executive_summary", "")
    ws["A6"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[6].height = 80
    ws.column_dimensions["A"].width = 100

    row = 8
    ws.cell(row=row, column=1, value="KPI Cards").font = header_font
    ws.cell(row=row, column=1).fill = header_fill
    row += 1
    ws.cell(row=row, column=1, value="Label").font = Font(bold=True)
    ws.cell(row=row, column=2, value="Value").font = Font(bold=True)
    ws.cell(row=row, column=3, value="Detail").font = Font(bold=True)
    row += 1
    for kpi in last.get("ai", {}).get("kpi_cards", []):
        ws.cell(row=row, column=1, value=str(kpi.get("label", "")))
        ws.cell(row=row, column=2, value=str(kpi.get("value", "")))
        ws.cell(row=row, column=3, value=str(kpi.get("subtext", "")))
        row += 1

    # Cleaned Data tab
    ws2 = wb.create_sheet("Cleaned Data")
    for r in dataframe_to_rows(df, index=False, header=True):
        ws2.append(r)
    for cell in ws2[1]:
        cell.font = header_font
        cell.fill = header_fill

    # Data Quality tab
    ws3 = wb.create_sheet("Data Quality")
    ws3["A1"] = "Cleaning Report"
    ws3["A1"].font = header_font
    ws3["A1"].fill = header_fill
    r = 3
    for key, label in [
        ("duplicates_removed", "Duplicate rows removed"),
        ("whitespace_columns_fixed", "Columns with whitespace trimmed"),
        ("casing_normalized", "Columns with normalized casing"),
        ("types_inferred", "Columns with type inference applied"),
        ("nulls_filled", "Nulls filled"),
        ("nulls_dropped", "Columns dropped (>50% null)"),
    ]:
        val = clean.get(key)
        ws3.cell(row=r, column=1, value=label).font = Font(bold=True)
        ws3.cell(row=r, column=2, value=json.dumps(val) if val else "None")
        r += 1

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf, as_attachment=True,
        download_name=f"analytics_report_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/api/export/pdf", methods=["POST", "GET"])
def export_pdf():
    """Prefers POST body (client payload, survives server restarts);
    falls back to server session state if body is empty."""
    state = get_state()
    body = request.get_json(silent=True) or {}

    df = None
    if body.get("rows") and body.get("columns"):
        df = pd.DataFrame(body["rows"], columns=body["columns"])
    else:
        df = state.get("cleaned_df")

    last = body.get("last_analysis") or state.get("last_analysis")
    clean_body = body.get("clean_summary")
    filename_override = body.get("filename")

    if df is None or not last:
        return jsonify({"error": "No analysis data provided. Re-run the analysis first."}), 400

    try:
        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=LETTER,
            leftMargin=0.7 * inch, rightMargin=0.7 * inch,
            topMargin=0.7 * inch, bottomMargin=0.7 * inch,
            title="AI Analytics Report",
        )
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "Title", parent=styles["Title"], fontSize=28,
            textColor=colors.HexColor(ACCENT), spaceAfter=20,
        )
        h2 = ParagraphStyle(
            "H2", parent=styles["Heading2"], fontSize=16,
            textColor=colors.HexColor(ACCENT), spaceBefore=14, spaceAfter=8,
        )
        body = styles["BodyText"]

        story = []

        # Title page
        story.append(Spacer(1, 1.5 * inch))
        story.append(Paragraph("AI Analytics Report", title_style))
        story.append(Paragraph(
            f"Dataset: <b>{filename_override or state.get('filename', '—')}</b>", body))
        story.append(Paragraph(
            f"Rows: {len(df):,} &nbsp;&nbsp; Columns: {len(df.columns)}", body))
        story.append(Paragraph(
            f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}", body))
        story.append(PageBreak())

        # Executive summary
        story.append(Paragraph("Executive Summary", h2))
        story.append(Paragraph(
            last.get("ai", {}).get("executive_summary", "").replace("\n", "<br/>"),
            body,
        ))

        # KPI cards
        kpis = last.get("ai", {}).get("kpi_cards", [])
        if kpis:
            story.append(Paragraph("Key Metrics", h2))
            data = [["Metric", "Value", "Detail"]]
            for k in kpis:
                data.append([
                    str(k.get("label", "")),
                    str(k.get("value", "")),
                    str(k.get("subtext", "")),
                ])
            t = Table(data, colWidths=[2.0 * inch, 1.6 * inch, 3.2 * inch])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(ACCENT)),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
            ]))
            story.append(t)

        # Cleaning report
        story.append(Paragraph("Data Cleaning Report", h2))
        clean = clean_body or state.get("clean_summary", {})
        clean_rows = [
            ["Original shape", str(clean.get("original_shape"))],
            ["Cleaned shape", str(clean.get("cleaned_shape"))],
            ["Duplicates removed", str(clean.get("duplicates_removed", 0))],
            ["Types inferred", json.dumps(clean.get("types_inferred", {}))[:400]],
            ["Nulls filled", json.dumps(clean.get("nulls_filled", {}))[:400]],
            ["Columns dropped", json.dumps(clean.get("nulls_dropped", {}))[:400]],
        ]
        t = Table(clean_rows, colWidths=[1.8 * inch, 5.0 * inch])
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(t)

        # Charts — insights only (plotly static export requires kaleido; we list insights)
        story.append(PageBreak())
        story.append(Paragraph("Analyses & Insights", h2))
        for c in last.get("charts", []):
            story.append(Paragraph(f"<b>{c.get('title', '')}</b>", body))
            story.append(Paragraph(c.get("insight", ""), body))
            story.append(Spacer(1, 0.12 * inch))

        # Quality + follow-ups
        dq = last.get("ai", {}).get("data_quality_notes", [])
        if dq:
            story.append(Paragraph("Data Quality Notes", h2))
            for n in dq:
                story.append(Paragraph(f"• {n}", body))

        fu = last.get("ai", {}).get("followup_questions", [])
        if fu:
            story.append(Paragraph("Recommended Follow-up Questions", h2))
            for q in fu:
                story.append(Paragraph(f"• {q}", body))

        def _page_num(canvas, doc_):
            canvas.saveState()
            canvas.setFont("Helvetica", 9)
            canvas.setFillColor(colors.HexColor("#64748B"))
            canvas.drawRightString(
                LETTER[0] - 0.7 * inch, 0.4 * inch, f"Page {doc_.page}"
            )
            canvas.drawString(0.7 * inch, 0.4 * inch, "AI Analytics Report")
            canvas.restoreState()

        doc.build(story, onFirstPage=_page_num, onLaterPages=_page_num)
        buf.seek(0)
        return send_file(
            buf, as_attachment=True,
            download_name=f"analytics_report_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.pdf",
            mimetype="application/pdf",
        )
    except Exception as e:
        log.error(f"PDF export failed: {e}\n{traceback.format_exc()}")
        # Fallback HTML print view
        html = "<html><body><h1>AI Analytics Report (fallback)</h1>"
        html += f"<p><b>Dataset:</b> {state.get('filename', '—')}</p>"
        html += f"<h2>Executive Summary</h2><p>{last.get('ai', {}).get('executive_summary', '')}</p>"
        html += "<h2>Insights</h2><ul>"
        for c in last.get("charts", []):
            html += f"<li><b>{c.get('title')}</b>: {c.get('insight')}</li>"
        html += "</ul></body></html>"
        return html, 200, {"Content-Type": "text/html"}


@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "File too large (max 50MB)"}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
