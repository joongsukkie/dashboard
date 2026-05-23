"""
Archetype playbooks — senior-analyst-flavored analysis per dataset type.

Each playbook receives the cleaned DataFrame plus the role-column mapping
from archetypes.detect_archetype and returns a structured payload:

    {
      "kpis":     [{label, value, unit, detail}, ...],
      "segments": [{name, n, share, ...}, ...],       # optional
      "cohorts":  {periods: [...], values: [[...]]},  # optional
      "tables":   [{title, columns, rows, note}, ...],
      "alerts":   [str, ...],                         # honest data caveats
      "narrative_hooks": {sku: facts, ...},           # facts for the AI to cite
    }

The narrative LLM then writes a senior-analyst-voiced executive summary
*citing* this payload — never inventing numbers.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd


# -----------------------------------------------------------------------------
# Shared utilities
# -----------------------------------------------------------------------------
def _safe_div(a: float, b: float) -> float:
    return float(a) / float(b) if b not in (0, 0.0, None) and not pd.isna(b) else 0.0


def _kpi(label: str, value, unit: str = "", detail: str = "") -> dict:
    return {"label": label, "value": value, "unit": unit, "detail": detail}


def _money(x: float) -> str:
    if x is None or pd.isna(x):
        return "—"
    if abs(x) >= 1_000_000:
        return f"${x/1_000_000:.2f}M"
    if abs(x) >= 1_000:
        return f"${x/1_000:.1f}K"
    return f"${x:,.2f}"


def _pct(x: float) -> str:
    if x is None or pd.isna(x):
        return "—"
    return f"{x*100:.1f}%"


def _date_range_summary(s: pd.Series) -> str:
    nn = s.dropna()
    if nn.empty:
        return "—"
    return f"{nn.min().date()} to {nn.max().date()}"


# -----------------------------------------------------------------------------
# Orders playbook
# -----------------------------------------------------------------------------
def orders_playbook(df: pd.DataFrame, roles: dict) -> dict:
    """The bread-and-butter B2C playbook: AOV, repeat rate, RFM, cohorts."""
    out: dict = {"kpis": [], "segments": [], "tables": [],
                 "alerts": [], "narrative_hooks": {}}

    amount_col = roles.get("amount")
    qty_col    = roles.get("qty")
    price_col  = roles.get("unit_price")
    cust_col   = roles.get("customer_id") or roles.get("email")
    date_col   = roles.get("date")
    sku_col    = roles.get("sku")
    cost_col   = roles.get("cost")
    return_col = roles.get("return_flag")

    # Reconstruct line revenue if amount column missing but price+qty present.
    rev = None
    if amount_col and amount_col in df.columns:
        rev = df[amount_col]
    elif price_col and qty_col and price_col in df.columns and qty_col in df.columns:
        rev = df[price_col] * df[qty_col]
        out["alerts"].append(
            f"No explicit revenue column; reconstructed from {price_col} × {qty_col}."
        )

    n_rows = len(df)
    total_rev = float(rev.sum()) if rev is not None else None
    n_orders = (df[roles["order_id"]].nunique()
                if "order_id" in roles else n_rows)
    n_customers = (df[cust_col].nunique() if cust_col else None)

    # ---- KPI row ------------------------------------------------------------
    if total_rev is not None:
        out["kpis"].append(_kpi("Total revenue", _money(total_rev),
                                detail=f"{n_orders:,} orders"))
        aov = _safe_div(total_rev, n_orders)
        out["kpis"].append(_kpi("Average order value", _money(aov),
                                detail="revenue ÷ unique orders"))

    if n_customers:
        out["kpis"].append(_kpi("Unique customers", f"{n_customers:,}",
                                detail=f"{_safe_div(n_orders, n_customers):.2f} orders / customer"))

    if date_col and df[date_col].dtype.kind == "M":
        nn = df[date_col].dropna()
        if not nn.empty:
            span_days = (nn.max() - nn.min()).days
            out["kpis"].append(_kpi("Date range",
                                    _date_range_summary(df[date_col]),
                                    detail=f"{span_days} days"))

    # ---- Repeat-purchase rate ----------------------------------------------
    if cust_col and "order_id" in roles:
        per_cust_orders = df.groupby(cust_col)[roles["order_id"]].nunique()
        repeat_rate = (per_cust_orders > 1).mean()
        out["kpis"].append(_kpi("Repeat customers", _pct(repeat_rate),
                                detail=f"{int((per_cust_orders > 1).sum()):,} of {len(per_cust_orders):,}"))

    # ---- Return rate --------------------------------------------------------
    if return_col and return_col in df.columns:
        try:
            ret = df[return_col].astype(str).str.lower().isin(
                ("1", "true", "yes", "y", "t", "returned"))
            out["kpis"].append(_kpi("Return rate", _pct(ret.mean()),
                                    detail=f"{int(ret.sum()):,} of {n_rows:,} rows"))
        except Exception:
            pass

    # ---- Gross margin -------------------------------------------------------
    if cost_col and rev is not None and cost_col in df.columns:
        cogs = df[cost_col]
        if qty_col and qty_col in df.columns:
            cogs = cogs * df[qty_col]
        gross = (rev - cogs).sum()
        gm = _safe_div(gross, total_rev)
        out["kpis"].append(_kpi("Gross margin", _pct(gm),
                                detail=f"gross profit { _money(float(gross)) }"))

    # ---- Pareto: top decile of customers driving X% of revenue --------------
    if cust_col and rev is not None:
        per_cust = (pd.DataFrame({cust_col: df[cust_col], "_rev": rev})
                    .groupby(cust_col)["_rev"].sum()
                    .sort_values(ascending=False))
        if len(per_cust) >= 10:
            cum = per_cust.cumsum()
            top_10pct_count = max(1, int(len(per_cust) * 0.10))
            top_10pct_rev_share = _safe_div(per_cust.head(top_10pct_count).sum(),
                                            total_rev)
            out["kpis"].append(_kpi("Top 10% of customers drive",
                                    _pct(top_10pct_rev_share),
                                    detail=f"of total revenue ({top_10pct_count:,} customers)"))

    # ---- RFM segmentation ---------------------------------------------------
    if cust_col and date_col and rev is not None and df[date_col].dtype.kind == "M":
        try:
            snapshot = df[date_col].dropna().max() + pd.Timedelta(days=1)
            grp = df.groupby(cust_col).agg(
                last_order=(date_col, "max"),
                frequency=(roles.get("order_id", cust_col), "nunique"),
                monetary=("_rev_tmp", "sum") if False else (cust_col, "size"),
            ).reset_index()
            # Re-do monetary properly with the reconstructed revenue.
            monetary = (pd.DataFrame({cust_col: df[cust_col], "_rev": rev})
                        .groupby(cust_col)["_rev"].sum())
            grp["monetary"] = grp[cust_col].map(monetary).fillna(0)
            grp["recency_days"] = (snapshot - grp["last_order"]).dt.days

            # Tertile scoring 1..3 (3 = best). Lower recency_days is better.
            grp["R"] = pd.qcut(grp["recency_days"], q=3, labels=[3,2,1],
                               duplicates="drop").astype(int)
            grp["F"] = pd.qcut(grp["frequency"].rank(method="first"), q=3,
                               labels=[1,2,3], duplicates="drop").astype(int)
            grp["M"] = pd.qcut(grp["monetary"].rank(method="first"), q=3,
                               labels=[1,2,3], duplicates="drop").astype(int)
            grp["RFM"] = grp["R"] + grp["F"] + grp["M"]

            def _label(r):
                if r["R"] == 3 and r["F"] == 3 and r["M"] == 3:
                    return "Champions"
                if r["R"] == 3 and r["F"] >= 2:
                    return "Loyal"
                if r["R"] == 3 and r["F"] == 1:
                    return "New / Promising"
                if r["R"] == 1 and r["F"] >= 2:
                    return "At Risk"
                if r["R"] == 1 and r["F"] == 1:
                    return "Hibernating"
                return "Need Attention"
            grp["segment"] = grp.apply(_label, axis=1)

            seg = grp.groupby("segment").agg(
                customers=(cust_col, "count"),
                revenue=("monetary", "sum"),
                avg_freq=("frequency", "mean"),
                avg_recency=("recency_days", "mean"),
            ).reset_index().sort_values("revenue", ascending=False)
            tot_rev = float(seg["revenue"].sum()) or 1.0
            seg["revenue_share"] = seg["revenue"] / tot_rev

            out["segments"] = [{
                "name": row["segment"],
                "n": int(row["customers"]),
                "share_of_customers": round(float(row["customers"] / len(grp)), 4),
                "revenue": round(float(row["revenue"]), 2),
                "revenue_share": round(float(row["revenue_share"]), 4),
                "avg_frequency": round(float(row["avg_freq"]), 2),
                "avg_recency_days": round(float(row["avg_recency"]), 1),
            } for _, row in seg.iterrows()]

            out["tables"].append({
                "title": "RFM segments — customer count, revenue, and behavior",
                "columns": ["Segment", "Customers", "% of customers",
                            "Revenue", "% of revenue", "Avg orders", "Avg days since order"],
                "rows": [[
                    s["name"], f"{s['n']:,}", _pct(s["share_of_customers"]),
                    _money(s["revenue"]), _pct(s["revenue_share"]),
                    f"{s['avg_frequency']:.1f}", f"{s['avg_recency_days']:.0f}d",
                ] for s in out["segments"]],
                "note": "R/F/M scored in tertiles; segments use standard analyst labels."
            })
        except Exception as e:
            out["alerts"].append(f"RFM segmentation skipped: {e}")

    # ---- Cohort retention (% of customers active in month N+k) -------------
    if cust_col and date_col and df[date_col].dtype.kind == "M":
        try:
            d = df[[cust_col, date_col]].dropna().copy()
            d["_period"] = d[date_col].dt.to_period("M")
            first_period = d.groupby(cust_col)["_period"].min()
            d["_cohort"] = d[cust_col].map(first_period)
            d["_offset"] = (d["_period"].astype("int64")
                            - d["_cohort"].astype("int64")).astype(int)
            ct = d.groupby(["_cohort", "_offset"])[cust_col].nunique().unstack(fill_value=0)
            cohort_sizes = ct[0]
            retention = ct.divide(cohort_sizes, axis=0)
            # Keep first 12 month-offsets for compactness.
            retention = retention.iloc[:, :12]
            out["cohorts"] = {
                "periods": [str(p) for p in retention.index],
                "offsets": [int(c) for c in retention.columns],
                "values": [[round(float(v), 4) for v in row] for row in retention.values],
                "cohort_sizes": [int(s) for s in cohort_sizes.tolist()],
            }
            # Surface a single headline number for the AI: month-3 retention
            # for the most recent fully-aged cohort.
            try:
                aged = retention[retention.index <= retention.index.max() - 3]
                if not aged.empty and 3 in aged.columns:
                    m3 = aged[3].mean()
                    out["narrative_hooks"]["month_3_retention"] = round(float(m3), 4)
            except Exception:
                pass
        except Exception as e:
            out["alerts"].append(f"Cohort retention skipped: {e}")

    # ---- Top SKUs by revenue ------------------------------------------------
    if sku_col and rev is not None:
        sku_rev = (pd.DataFrame({sku_col: df[sku_col], "_rev": rev})
                   .groupby(sku_col)["_rev"]
                   .agg(["sum", "count"])
                   .sort_values("sum", ascending=False).head(15))
        out["tables"].append({
            "title": f"Top 15 {sku_col} by revenue",
            "columns": [sku_col, "Revenue", "Orders"],
            "rows": [[str(idx), _money(float(r["sum"])), f"{int(r['count']):,}"]
                     for idx, r in sku_rev.iterrows()],
            "note": "Revenue includes all rows for the SKU; orders = row count."
        })

    # Sample-size honesty.
    if n_rows < 100:
        out["alerts"].append(
            f"Small dataset ({n_rows} rows) — segment-level conclusions are low-confidence."
        )

    return out


# -----------------------------------------------------------------------------
# Marketing playbook
# -----------------------------------------------------------------------------
def marketing_playbook(df: pd.DataFrame, roles: dict) -> dict:
    """Channel ROAS, CAC, CTR, CVR, diminishing returns — the standard
    senior-marketing-analyst dashboard."""
    out: dict = {"kpis": [], "segments": [], "tables": [],
                 "alerts": [], "narrative_hooks": {}}

    spend_col = roles.get("spend")
    imp_col   = roles.get("impressions")
    click_col = roles.get("clicks")
    conv_col  = roles.get("conversions")
    rev_col   = roles.get("amount")
    chan_col  = roles.get("channel")
    camp_col  = roles.get("campaign")
    date_col  = roles.get("date")

    total_spend = float(df[spend_col].sum()) if spend_col else None
    total_imp   = float(df[imp_col].sum()) if imp_col else None
    total_click = float(df[click_col].sum()) if click_col else None
    total_conv  = float(df[conv_col].sum()) if conv_col else None
    total_rev   = float(df[rev_col].sum()) if rev_col else None

    if total_spend is not None:
        out["kpis"].append(_kpi("Total spend", _money(total_spend)))
    if total_rev is not None:
        out["kpis"].append(_kpi("Total revenue (attributed)", _money(total_rev)))
    if total_spend and total_rev is not None:
        roas = _safe_div(total_rev, total_spend)
        out["kpis"].append(_kpi("Blended ROAS", f"{roas:.2f}x",
                                detail="revenue ÷ spend"))
    if total_spend and total_conv:
        cac = _safe_div(total_spend, total_conv)
        out["kpis"].append(_kpi("Blended CAC", _money(cac),
                                detail="spend ÷ conversions"))
    if total_imp and total_click:
        ctr = _safe_div(total_click, total_imp)
        out["kpis"].append(_kpi("CTR", _pct(ctr),
                                detail="clicks ÷ impressions"))
    if total_click and total_conv:
        cvr = _safe_div(total_conv, total_click)
        out["kpis"].append(_kpi("CVR", _pct(cvr),
                                detail="conversions ÷ clicks"))
    if date_col and df[date_col].dtype.kind == "M":
        out["kpis"].append(_kpi("Date range",
                                _date_range_summary(df[date_col])))

    # ---- Channel matrix -----------------------------------------------------
    if chan_col and spend_col:
        agg_dict = {spend_col: "sum"}
        for c in (imp_col, click_col, conv_col, rev_col):
            if c:
                agg_dict[c] = "sum"
        g = df.groupby(chan_col).agg(agg_dict).reset_index()
        g = g.sort_values(spend_col, ascending=False)

        def _row(r):
            spend = float(r[spend_col])
            rev_v = float(r[rev_col]) if rev_col else None
            conv_v = float(r[conv_col]) if conv_col else None
            click_v = float(r[click_col]) if click_col else None
            imp_v = float(r[imp_col]) if imp_col else None
            roas_str = (
                f"{_safe_div(rev_v or 0, spend):.2f}x"
                if rev_v is not None and spend > 0 else "—"
            )
            return [
                str(r[chan_col]),
                _money(spend) if spend > 0 else "$0 (organic)",
                _money(rev_v) if rev_v is not None else "—",
                roas_str,
                _money(_safe_div(spend, conv_v or 0)) if conv_v and spend > 0 else "—",
                _pct(_safe_div(click_v or 0, imp_v or 0)) if imp_v else "—",
                _pct(_safe_div(conv_v or 0, click_v or 0)) if click_v else "—",
            ]

        out["tables"].append({
            "title": f"Channel performance ({chan_col})",
            "columns": ["Channel", "Spend", "Revenue", "ROAS", "CAC", "CTR", "CVR"],
            "rows": [_row(r) for _, r in g.iterrows()],
            "note": "Spend-weighted; revenue and conversions must be attributable to the channel."
        })

        # Best and worst ROAS channels surfaced as narrative hooks.
        if rev_col:
            g["_roas"] = g[rev_col] / g[spend_col].replace(0, np.nan)
            best = g.sort_values("_roas", ascending=False).iloc[0]
            worst = g.sort_values("_roas", ascending=True).iloc[0]
            out["narrative_hooks"]["best_channel"] = {
                "channel": str(best[chan_col]),
                "roas": round(float(best["_roas"]), 2),
                "spend": float(best[spend_col]),
            }
            out["narrative_hooks"]["worst_channel"] = {
                "channel": str(worst[chan_col]),
                "roas": round(float(worst["_roas"]), 2),
                "spend": float(worst[spend_col]),
            }

    # ---- Campaign-level leaderboard ----------------------------------------
    if camp_col and spend_col and rev_col:
        g = df.groupby(camp_col).agg(
            spend=(spend_col, "sum"),
            revenue=(rev_col, "sum"),
            conversions=(conv_col, "sum") if conv_col else (spend_col, "size"),
        ).reset_index()
        g["roas"] = g["revenue"] / g["spend"].replace(0, np.nan)
        g = g.sort_values("revenue", ascending=False).head(10)
        out["tables"].append({
            "title": "Top 10 campaigns by revenue",
            "columns": [camp_col, "Spend", "Revenue", "ROAS",
                        "Conversions" if conv_col else "Rows"],
            "rows": [[str(r[camp_col]), _money(float(r["spend"])),
                      _money(float(r["revenue"])),
                      f"{float(r['roas']):.2f}x" if pd.notna(r['roas']) else "—",
                      f"{int(r['conversions']):,}"] for _, r in g.iterrows()],
            "note": "Sorted by revenue; small-sample campaigns may have noisy ROAS.",
        })

    if len(df) < 100:
        out["alerts"].append(
            f"Small dataset ({len(df)} rows) — channel-level ROAS comparisons "
            "should be interpreted with caution."
        )
    return out


# -----------------------------------------------------------------------------
# Customers playbook — one row per person, no transaction detail
# -----------------------------------------------------------------------------
def customers_playbook(df: pd.DataFrame, roles: dict) -> dict:
    """When the file is a customer master (1 row/person), the right analysis
    is tenure cohorts, acquisition-channel mix, geographic mix, and (when
    available) simple CLV / repeat-purchase indicators."""
    out: dict = {"kpis": [], "segments": [], "tables": [],
                 "alerts": [], "narrative_hooks": {}}

    cust_col = roles.get("customer_id") or roles.get("email")
    date_col = roles.get("date")
    channel_col = roles.get("channel")
    n = len(df)

    if cust_col:
        out["kpis"].append(_kpi("Unique customers", f"{df[cust_col].nunique():,}"))
    else:
        out["kpis"].append(_kpi("Rows", f"{n:,}"))

    # Tenure (signup date) summary.
    if date_col and date_col in df.columns and df[date_col].dtype.kind == "M":
        nn = df[date_col].dropna()
        if not nn.empty:
            span_days = (nn.max() - nn.min()).days
            out["kpis"].append(_kpi("Signup range",
                                    _date_range_summary(df[date_col]),
                                    detail=f"{span_days} days"))
            # Average tenure as of today (or latest signup if before today).
            today = pd.Timestamp.utcnow().tz_localize(None)
            ref = max(today, nn.max())
            tenure_days = (ref - nn).dt.days
            out["kpis"].append(_kpi("Avg tenure",
                                    f"{tenure_days.mean():.0f} days",
                                    detail=f"median {tenure_days.median():.0f} days"))

    # Acquisition-channel mix.
    if channel_col and channel_col in df.columns:
        vc = df[channel_col].dropna().value_counts().head(10)
        out["tables"].append({
            "title": f"Acquisition-channel mix ({channel_col})",
            "columns": [channel_col, "Customers", "Share"],
            "rows": [[str(k), f"{int(v):,}", _pct(v / n)] for k, v in vc.items()],
            "note": "Counts of unique customer rows per channel.",
        })

    # Geographic mix (look for country / region / state column).
    geo_col = None
    for c in df.columns:
        cl = str(c).lower()
        if cl in ("country", "region", "state", "city") and df[c].dtype == object:
            geo_col = c
            break
    if geo_col:
        vc = df[geo_col].dropna().value_counts().head(10)
        out["tables"].append({
            "title": f"Top {geo_col}s",
            "columns": [geo_col, "Customers", "Share"],
            "rows": [[str(k), f"{int(v):,}", _pct(v / n)] for k, v in vc.items()],
            "note": "",
        })

    # Tenure cohorts — sign-up month → cohort size.
    if date_col and date_col in df.columns and df[date_col].dtype.kind == "M":
        period = df[date_col].dt.to_period("M")
        vc = period.value_counts().sort_index()
        out["tables"].append({
            "title": "Sign-ups by month (cohort sizes)",
            "columns": ["Cohort month", "New customers"],
            "rows": [[str(k), f"{int(v):,}"] for k, v in vc.items()][-12:],
            "note": "Showing most recent 12 months.",
        })
        # Cohort concentration as a narrative hook.
        if len(vc) >= 2:
            biggest = vc.idxmax()
            out["narrative_hooks"]["biggest_signup_cohort"] = str(biggest)
            out["narrative_hooks"]["biggest_signup_n"] = int(vc.max())

    if n < 100:
        out["alerts"].append(
            f"Small customer file ({n} rows) — channel/geo shares are noisy."
        )
    return out


# -----------------------------------------------------------------------------
# Subscriptions playbook — status timelines, MRR, churn
# -----------------------------------------------------------------------------
def subscriptions_playbook(df: pd.DataFrame, roles: dict) -> dict:
    out: dict = {"kpis": [], "segments": [], "tables": [],
                 "alerts": [], "narrative_hooks": {}}

    status_col = roles.get("status")
    plan_col   = roles.get("plan")
    mrr_col    = roles.get("mrr") or roles.get("amount")
    cust_col   = roles.get("customer_id") or roles.get("email")
    start_col  = roles.get("date")

    n = len(df)

    if cust_col:
        out["kpis"].append(_kpi("Subscribers", f"{df[cust_col].nunique():,}"))
    else:
        out["kpis"].append(_kpi("Subscription rows", f"{n:,}"))

    # ---- Status distribution -------------------------------------------------
    if status_col and status_col in df.columns:
        norm = df[status_col].dropna().astype(str).str.lower().str.strip()
        vc = norm.value_counts()
        active_n = int(vc.get("active", 0) + vc.get("trialing", 0))
        churned_n = int(vc.get("canceled", 0) + vc.get("cancelled", 0)
                        + vc.get("churned", 0))
        if active_n + churned_n > 0:
            churn_rate = churned_n / (active_n + churned_n)
            out["kpis"].append(_kpi("Gross churn rate", _pct(churn_rate),
                                    detail=f"{churned_n:,} canceled of "
                                           f"{active_n+churned_n:,} ever-active"))
        out["tables"].append({
            "title": "Status distribution",
            "columns": ["Status", "Subscribers", "Share"],
            "rows": [[str(k), f"{int(v):,}", _pct(v / max(1, len(norm)))]
                     for k, v in vc.items()],
            "note": "Lower-cased; trialing counted as active for churn math.",
        })

    # ---- Plan breakdown ------------------------------------------------------
    if plan_col and plan_col in df.columns and mrr_col and mrr_col in df.columns:
        g = (df.groupby(plan_col)[mrr_col]
               .agg(["count", "sum", "mean"])
               .sort_values("sum", ascending=False)
               .reset_index())
        out["tables"].append({
            "title": f"MRR by plan ({plan_col})",
            "columns": [plan_col, "Subscribers", "Total MRR", "Avg MRR / sub"],
            "rows": [[str(r[plan_col]), f"{int(r['count']):,}",
                      _money(float(r["sum"])), _money(float(r["mean"]))]
                     for _, r in g.iterrows()],
            "note": "MRR summed across all rows for the plan.",
        })

    # ---- MRR total + ARR -----------------------------------------------------
    if mrr_col and mrr_col in df.columns:
        total_mrr = float(df[mrr_col].dropna().sum())
        out["kpis"].insert(0, _kpi("Total MRR", _money(total_mrr),
                                   detail="sum of recurring revenue rows"))
        out["kpis"].insert(1, _kpi("Implied ARR", _money(total_mrr * 12),
                                   detail="MRR × 12"))
        # Active-only MRR if we can isolate it.
        if status_col and status_col in df.columns:
            active = df[df[status_col].astype(str).str.lower()
                        .isin(("active", "trialing"))]
            if len(active):
                out["kpis"].insert(2, _kpi("Active MRR",
                                           _money(float(active[mrr_col].sum())),
                                           detail=f"{len(active):,} active subs"))

    # ---- Cohort: sign-ups by month ------------------------------------------
    if start_col and start_col in df.columns and df[start_col].dtype.kind == "M":
        period = df[start_col].dt.to_period("M")
        vc = period.value_counts().sort_index()
        out["tables"].append({
            "title": "New-subscription cohorts (by start month)",
            "columns": ["Start month", "New subs"],
            "rows": [[str(k), f"{int(v):,}"] for k, v in vc.items()][-12:],
            "note": "Most recent 12 cohorts.",
        })

    # ---- Save-rate proxy (paused vs canceled) -------------------------------
    if status_col and status_col in df.columns:
        norm = df[status_col].dropna().astype(str).str.lower().str.strip()
        paused = int((norm == "paused").sum())
        ever_left = paused + int((norm.isin(("canceled","cancelled","churned"))).sum())
        if ever_left > 0:
            save_rate = paused / ever_left
            out["narrative_hooks"]["save_rate_proxy"] = round(save_rate, 4)
            out["kpis"].append(_kpi("Save-rate proxy", _pct(save_rate),
                                    detail="paused ÷ (paused + canceled)"))

    if n < 100:
        out["alerts"].append(f"Small subscription file ({n} rows) — churn rates are noisy.")
    return out


# -----------------------------------------------------------------------------
# Sessions playbook — funnel, sources, bounce
# -----------------------------------------------------------------------------
def sessions_playbook(df: pd.DataFrame, roles: dict) -> dict:
    out: dict = {"kpis": [], "segments": [], "tables": [],
                 "alerts": [], "narrative_hooks": {}}

    session_col = roles.get("session_id")
    event_col   = roles.get("event")
    page_col    = roles.get("page")
    channel_col = roles.get("channel")
    date_col    = roles.get("date")

    n = len(df)
    out["kpis"].append(_kpi("Total events", f"{n:,}"))
    if session_col and session_col in df.columns:
        n_sessions = df[session_col].nunique()
        out["kpis"].append(_kpi("Unique sessions", f"{n_sessions:,}",
                                detail=f"{n / max(1, n_sessions):.1f} events / session"))
        # Bounce = sessions with exactly 1 event.
        ev_per_sess = df.groupby(session_col).size()
        bounce_rate = (ev_per_sess == 1).mean()
        out["kpis"].append(_kpi("Bounce rate (1-event sessions)",
                                _pct(float(bounce_rate))))

    # ---- Funnel by event ----------------------------------------------------
    if event_col and event_col in df.columns:
        vc = df[event_col].dropna().value_counts()
        # Standard e-commerce ladder; only include events that exist.
        funnel_order = ["page_view", "view_item", "add_to_cart",
                        "begin_checkout", "purchase"]
        present = [e for e in funnel_order if e in vc.index]
        if len(present) >= 2:
            rows = []
            top = vc[present[0]]
            for i, ev in enumerate(present):
                c = int(vc[ev])
                drop = (1 - c / vc[present[i-1]]) if i > 0 else 0.0
                rows.append([ev, f"{c:,}", _pct(c / top),
                             _pct(drop) if i > 0 else "—"])
            out["tables"].append({
                "title": "Conversion funnel",
                "columns": ["Event", "Count", "% of top", "Step drop-off"],
                "rows": rows,
                "note": "Standard e-commerce ladder; only events present in your data.",
            })
            # Headline funnel finish-rate for the narrative.
            if "purchase" in vc.index and present:
                out["narrative_hooks"]["funnel_finish_rate"] = round(
                    float(vc["purchase"] / vc[present[0]]), 4)
        else:
            # Show generic top-N events instead.
            out["tables"].append({
                "title": f"Top events ({event_col})",
                "columns": ["Event", "Count", "Share"],
                "rows": [[str(k), f"{int(v):,}", _pct(v / n)] for k, v in vc.head(10).items()],
                "note": "",
            })

    # ---- Source / channel mix ----------------------------------------------
    if channel_col and channel_col in df.columns:
        vc = df[channel_col].dropna().value_counts().head(10)
        out["tables"].append({
            "title": f"Traffic source mix ({channel_col})",
            "columns": [channel_col, "Events", "Share"],
            "rows": [[str(k), f"{int(v):,}", _pct(v / n)] for k, v in vc.items()],
            "note": "",
        })

    # ---- Top pages ----------------------------------------------------------
    if page_col and page_col in df.columns:
        vc = df[page_col].dropna().value_counts().head(10)
        out["tables"].append({
            "title": f"Top {page_col}",
            "columns": [page_col, "Hits", "Share"],
            "rows": [[str(k), f"{int(v):,}", _pct(v / n)] for k, v in vc.items()],
            "note": "",
        })

    if n < 200:
        out["alerts"].append(f"Small sessions log ({n} events) — funnel rates are unstable.")
    return out


# -----------------------------------------------------------------------------
# Reviews playbook — rating distribution, low-rating SKUs, velocity
# -----------------------------------------------------------------------------
def reviews_playbook(df: pd.DataFrame, roles: dict) -> dict:
    out: dict = {"kpis": [], "segments": [], "tables": [],
                 "alerts": [], "narrative_hooks": {}}

    rating_col = roles.get("rating")
    text_col   = roles.get("review_text")
    sku_col    = roles.get("sku") or roles.get("product_name")
    date_col   = roles.get("date")

    n = len(df)
    out["kpis"].append(_kpi("Reviews", f"{n:,}"))

    if rating_col and rating_col in df.columns:
        ratings = pd.to_numeric(df[rating_col], errors="coerce").dropna()
        if len(ratings):
            avg = float(ratings.mean())
            out["kpis"].append(_kpi("Avg rating", f"{avg:.2f}",
                                    detail=f"median {ratings.median():.1f}"))
            # NPS-style buckets if ratings look 0–10.
            if ratings.max() <= 10 and ratings.max() > 5:
                promoters = (ratings >= 9).mean()
                detractors = (ratings <= 6).mean()
                nps = (promoters - detractors) * 100
                out["kpis"].append(_kpi("NPS", f"{nps:.0f}",
                                        detail=f"promoters {_pct(promoters)}  detractors {_pct(detractors)}"))
            else:
                # 1–5 star buckets.
                low = float((ratings <= 2).mean())
                high = float((ratings >= 4).mean())
                out["kpis"].append(_kpi("% low ratings (≤2)", _pct(low)))
                out["kpis"].append(_kpi("% high ratings (≥4)", _pct(high)))

            # Histogram table.
            vc = ratings.round().astype(int).value_counts().sort_index()
            out["tables"].append({
                "title": "Rating distribution",
                "columns": ["Rating", "Count", "Share"],
                "rows": [[str(k), f"{int(v):,}", _pct(v / len(ratings))]
                         for k, v in vc.items()],
                "note": "",
            })

    # ---- Low-rated SKUs / products -----------------------------------------
    if rating_col and sku_col and sku_col in df.columns:
        try:
            g = df.groupby(sku_col)[rating_col].agg(
                avg="mean", count="count").reset_index()
            g = g[g["count"] >= 5].sort_values("avg").head(10)
            if len(g):
                out["tables"].append({
                    "title": "Lowest-rated SKUs (≥5 reviews)",
                    "columns": [sku_col, "Avg rating", "Reviews"],
                    "rows": [[str(r[sku_col]), f"{float(r['avg']):.2f}",
                              f"{int(r['count']):,}"] for _, r in g.iterrows()],
                    "note": "Sorted ascending; investigate these first.",
                })
                worst = g.iloc[0]
                out["narrative_hooks"]["worst_sku"] = {
                    "sku": str(worst[sku_col]),
                    "avg_rating": round(float(worst["avg"]), 2),
                    "n_reviews": int(worst["count"]),
                }
        except Exception:
            pass

    # ---- Review velocity ----------------------------------------------------
    if date_col and date_col in df.columns and df[date_col].dtype.kind == "M":
        period = df[date_col].dt.to_period("M")
        vc = period.value_counts().sort_index()
        if len(vc) >= 2:
            out["tables"].append({
                "title": "Review velocity (monthly)",
                "columns": ["Month", "Reviews"],
                "rows": [[str(k), f"{int(v):,}"] for k, v in vc.items()][-12:],
                "note": "Most recent 12 months.",
            })

    # Average review length — a free hint for the AI narrative.
    if text_col and text_col in df.columns:
        try:
            avg_len = df[text_col].dropna().astype(str).str.len().mean()
            out["kpis"].append(_kpi("Avg review length", f"{avg_len:.0f} chars"))
        except Exception:
            pass

    if n < 100:
        out["alerts"].append(f"Few reviews ({n}) — averages are noisy.")
    return out


# -----------------------------------------------------------------------------
# Catalog playbook — margin, stockout risk, long-tail share
# -----------------------------------------------------------------------------
def catalog_playbook(df: pd.DataFrame, roles: dict) -> dict:
    out: dict = {"kpis": [], "segments": [], "tables": [],
                 "alerts": [], "narrative_hooks": {}}

    sku_col   = roles.get("sku")
    cat_col   = roles.get("category")
    price_col = roles.get("unit_price") or roles.get("amount")
    cost_col  = roles.get("cost")
    stock_col = roles.get("stock")

    n = len(df)
    out["kpis"].append(_kpi("SKUs", f"{n:,}"))

    # Margin per row.
    if price_col and cost_col and price_col in df.columns and cost_col in df.columns:
        margin = df[price_col] - df[cost_col]
        margin_pct = margin / df[price_col].replace(0, np.nan)
        avg_margin = float(margin_pct.dropna().mean())
        out["kpis"].append(_kpi("Avg margin %", _pct(avg_margin),
                                detail=f"median {float(margin_pct.dropna().median()):.0%}"))

        if cat_col and cat_col in df.columns:
            g = (df.assign(_m=margin_pct, _r=margin)
                   .groupby(cat_col).agg(skus=(cat_col, "size"),
                                          avg_margin=("_m", "mean"),
                                          total_gross=("_r", "sum"))
                   .sort_values("avg_margin", ascending=False).reset_index())
            out["tables"].append({
                "title": f"Margin by {cat_col}",
                "columns": [cat_col, "SKUs", "Avg margin %", "Total gross"],
                "rows": [[str(r[cat_col]), f"{int(r['skus']):,}",
                          _pct(float(r["avg_margin"])),
                          _money(float(r["total_gross"]))] for _, r in g.iterrows()],
                "note": "Average margin weighted by row, not by sales.",
            })

    # Stockout risk.
    if stock_col and stock_col in df.columns:
        stock = pd.to_numeric(df[stock_col], errors="coerce")
        out_of_stock = int((stock <= 0).sum())
        low_stock = int(((stock > 0) & (stock <= 5)).sum())
        out["kpis"].append(_kpi("Out of stock", f"{out_of_stock:,}",
                                detail=f"of {n:,} SKUs"))
        out["kpis"].append(_kpi("Low stock (≤5 units)", f"{low_stock:,}"))
        if cat_col and cat_col in df.columns:
            g = (df.assign(_oos=(stock <= 0))
                   .groupby(cat_col)["_oos"].agg(["sum", "size"])
                   .sort_values("sum", ascending=False).head(10).reset_index())
            out["tables"].append({
                "title": f"Stockout risk by {cat_col}",
                "columns": [cat_col, "Out-of-stock SKUs", "Total SKUs"],
                "rows": [[str(r[cat_col]), f"{int(r['sum']):,}", f"{int(r['size']):,}"]
                         for _, r in g.iterrows()],
                "note": "",
            })

    # Long-tail share — Pareto on a per-SKU value (price, or stock × price if both).
    if price_col and price_col in df.columns and sku_col and sku_col in df.columns:
        val = df[price_col].fillna(0)
        if stock_col and stock_col in df.columns:
            val = val * pd.to_numeric(df[stock_col], errors="coerce").fillna(0)
        val = val.sort_values(ascending=False)
        total = float(val.sum()) or 1.0
        top_20 = max(1, int(len(val) * 0.20))
        share = float(val.head(top_20).sum() / total)
        out["kpis"].append(_kpi("Top 20% of SKUs hold",
                                _pct(share),
                                detail="of total catalog value"))
        out["narrative_hooks"]["pareto_top20_share"] = round(share, 4)

    if n < 30:
        out["alerts"].append(f"Tiny catalog ({n} SKUs) — segment patterns won't be meaningful.")
    return out


# -----------------------------------------------------------------------------
# Apps playbook — mobile app-store catalogs (App Store / Play Store)
# -----------------------------------------------------------------------------
def apps_playbook(df: pd.DataFrame, roles: dict) -> dict:
    """Analysis a marketing analyst at an app company actually wants:
    genre mix, ratings (over RATED apps only), free vs paid, app size,
    top developers, content-rating mix, release trend."""
    out: dict = {"kpis": [], "segments": [], "tables": [],
                 "alerts": [], "narrative_hooks": {}}
    n = len(df)
    out["kpis"].append(_kpi("Apps in catalog", f"{n:,}"))

    genre_col  = roles.get("genre") or roles.get("category")
    rating_col = roles.get("rating")
    price_col  = roles.get("unit_price") or roles.get("amount")
    size_col   = roles.get("app_size")
    dev_col    = roles.get("developer")
    date_col   = roles.get("date")

    # ---- Free vs paid -------------------------------------------------------
    free_col = next((c for c in df.columns
                     if str(c).strip().lower() in ("free", "is_free")), None)
    if free_col is not None:
        try:
            is_free = df[free_col].astype(bool)
            free_pct = float(is_free.mean())
            out["kpis"].append(_kpi("Free apps", _pct(free_pct),
                                    detail=f"{int(is_free.sum()):,} of {n:,}"))
            out["narrative_hooks"]["free_share"] = round(free_pct, 4)
        except Exception:
            free_col = None
    if price_col and price_col in df.columns:
        paid = df[price_col][df[price_col] > 0]
        if len(paid):
            out["kpis"].append(_kpi("Avg price (paid apps)", _money(float(paid.mean())),
                                    detail=f"median {_money(float(paid.median()))}"))

    # ---- Ratings — RATED apps only (cleaner nullified the unrated 0.0s) -----
    if rating_col and rating_col in df.columns:
        rated = df[rating_col].dropna()
        if len(rated):
            out["kpis"].append(_kpi("Avg rating (rated apps)", f"{rated.mean():.2f}",
                                    detail=f"{len(rated):,} apps have a rating"))
            unrated = n - len(rated)
            out["kpis"].append(_kpi("Unrated apps", _pct(unrated / max(1, n)),
                                    detail=f"{unrated:,} have no rating yet"))
            out["narrative_hooks"]["avg_rating"] = round(float(rated.mean()), 2)
            vc = rated.round().astype(int).value_counts().sort_index()
            out["tables"].append({
                "title": "Rating distribution (rated apps only)",
                "columns": ["Stars", "Apps", "Share of rated"],
                "rows": [[str(k), f"{int(v):,}", _pct(v / len(rated))]
                         for k, v in vc.items()],
                "note": "Apps with no rating are excluded — a 0.0 rating means "
                        "unrated, not zero stars.",
            })

    # ---- App size -----------------------------------------------------------
    if size_col and size_col in df.columns and pd.api.types.is_numeric_dtype(df[size_col]):
        mb = df[size_col].dropna() / 1_000_000.0
        if len(mb):
            out["kpis"].append(_kpi("Median app size", f"{mb.median():.0f} MB",
                                    detail=f"90th pct {mb.quantile(0.9):.0f} MB"))

    # ---- Genre mix ----------------------------------------------------------
    if genre_col and genre_col in df.columns:
        vc = df[genre_col].dropna().value_counts().head(15)
        rows = []
        for g, cnt in vc.items():
            avg_r = ""
            if rating_col and rating_col in df.columns:
                gr = df.loc[df[genre_col] == g, rating_col].dropna()
                avg_r = f"{gr.mean():.2f}" if len(gr) else "—"
            rows.append([str(g), f"{int(cnt):,}", _pct(cnt / n), avg_r])
        out["tables"].append({
            "title": f"Genre mix ({genre_col})",
            "columns": [genre_col, "Apps", "Share", "Avg rating"],
            "rows": rows,
            "note": "Avg rating computed over rated apps in each genre.",
        })
        out["narrative_hooks"]["top_genre"] = str(vc.index[0])

    # ---- Content-rating mix -------------------------------------------------
    cr_col = roles.get("content_rating")
    if cr_col and cr_col in df.columns:
        vc = df[cr_col].dropna().value_counts().head(8)
        out["tables"].append({
            "title": f"Content-rating mix ({cr_col})",
            "columns": [cr_col, "Apps", "Share"],
            "rows": [[str(k), f"{int(v):,}", _pct(v / n)] for k, v in vc.items()],
            "note": "",
        })

    # ---- Top developers -----------------------------------------------------
    if dev_col and dev_col in df.columns:
        vc = df[dev_col].dropna().value_counts().head(10)
        out["tables"].append({
            "title": "Most prolific developers (by app count)",
            "columns": [dev_col, "Apps published"],
            "rows": [[str(k), f"{int(v):,}"] for k, v in vc.items()],
            "note": "",
        })

    # ---- Release trend ------------------------------------------------------
    if date_col and date_col in df.columns and df[date_col].dtype.kind == "M":
        per_year = df[date_col].dt.year.dropna().astype(int).value_counts().sort_index()
        out["tables"].append({
            "title": "Apps released per year",
            "columns": ["Year", "Apps released"],
            "rows": [[str(int(y)), f"{int(c):,}"] for y, c in per_year.items()][-12:],
            "note": "By first-release date.",
        })

    if n < 50:
        out["alerts"].append(f"Small catalog ({n} apps) — genre patterns are noisy.")
    return out


# -----------------------------------------------------------------------------
# Archetype-aware chart builders
# -----------------------------------------------------------------------------
def _fig_dict(fig) -> dict:
    """Convert a plotly fig to a JSON-safe dict (uses plotly's own encoder)."""
    import json as _json
    from plotly.utils import PlotlyJSONEncoder
    return _json.loads(_json.dumps(fig.to_dict(), cls=PlotlyJSONEncoder))


# Distinct categorical hues — same palette the rest of the app uses.
CHART_PALETTE = [
    "#15803D", "#D97706", "#2563EB", "#DC2626", "#7C3AED",
    "#0891B2", "#DB2777", "#CA8A04", "#4B5650", "#059669",
]


def _layout(fig, title: str):
    fig.update_layout(
        title=dict(text=title, font=dict(size=14, family="Inter, system-ui",
                                         color="#141815"),
                   x=0.02, xanchor="left"),
        margin=dict(l=40, r=20, t=46, b=40),
        plot_bgcolor="#FFFFFF", paper_bgcolor="#FFFFFF",
        font=dict(family="Inter, system-ui", size=12, color="#1A1A2E"),
        xaxis=dict(gridcolor="#F1F5F9", linecolor="#E2E8F0"),
        yaxis=dict(gridcolor="#F1F5F9", linecolor="#E2E8F0"),
    )
    return fig


def _orders_charts(df: pd.DataFrame, roles: dict, playbook: dict) -> list[dict]:
    import plotly.express as px
    import plotly.graph_objects as go
    charts = []
    date_col = roles.get("date")
    cust_col = roles.get("customer_id") or roles.get("email")

    # Cohort retention heatmap (the senior-analyst chart for orders).
    cohorts = playbook.get("cohorts") if playbook else None
    if cohorts and cohorts.get("values"):
        vals = np.array(cohorts["values"], dtype=float)
        fig = go.Figure(data=go.Heatmap(
            z=vals * 100,
            x=[f"M+{c}" for c in cohorts["offsets"]],
            y=cohorts["periods"],
            colorscale=[[0, "#FFFFFF"], [0.5, "#86EFAC"], [1.0, "#15803D"]],
            zmin=0, zmax=100,
            colorbar=dict(title="% retained", ticksuffix="%"),
            hovertemplate="Cohort %{y}<br>%{x}: %{z:.1f}% retained<extra></extra>",
        ))
        fig = _layout(fig, "Cohort retention — % of customers active N months after first purchase")
        charts.append({
            "title": "Cohort retention heatmap",
            "insight": "Each row is a sign-up cohort; columns are months after first purchase. "
                       "Look diagonally — if recent cohorts retain worse, acquisition quality is dropping.",
            "figure": _fig_dict(fig),
        })

    # RFM segment bar (revenue and customer count).
    segs = playbook.get("segments") if playbook else None
    if segs:
        seg_df = pd.DataFrame(segs)
        fig = px.bar(seg_df, x="name", y="revenue", color="name",
                     color_discrete_sequence=CHART_PALETTE,
                     hover_data={"n": True, "revenue_share": ":.1%", "avg_frequency": ":.1f"})
        fig.update_layout(showlegend=False)
        fig.update_xaxes(title_text="RFM segment")
        fig.update_yaxes(title_text="Revenue")
        fig = _layout(fig, "RFM segment revenue")
        charts.append({
            "title": "RFM segment revenue",
            "insight": "Champions and Loyal segments should dominate revenue. "
                       "If 'At Risk' or 'Hibernating' bars are tall, you have a retention problem, not a sales problem.",
            "figure": _fig_dict(fig),
        })
    return charts


def _marketing_charts(df: pd.DataFrame, roles: dict, playbook: dict) -> list[dict]:
    import plotly.express as px
    charts = []
    chan_col = roles.get("channel")
    spend_col = roles.get("spend")
    rev_col = roles.get("amount")

    if chan_col and spend_col and rev_col and all(c in df.columns for c in (chan_col, spend_col, rev_col)):
        g = df.groupby(chan_col).agg(spend=(spend_col, "sum"),
                                      revenue=(rev_col, "sum")).reset_index()
        g["roas"] = g["revenue"] / g["spend"].replace(0, np.nan)
        g = g.sort_values("spend", ascending=False)
        fig = px.bar(g, x=chan_col, y=["spend", "revenue"], barmode="group",
                     color_discrete_sequence=[CHART_PALETTE[0], CHART_PALETTE[1]])
        fig.update_layout(yaxis_title="$")
        fig = _layout(fig, "Spend vs revenue by channel")
        charts.append({
            "title": "Spend vs revenue by channel",
            "insight": "If revenue bars are shorter than spend bars on a channel, that channel is unprofitable. "
                       "Reallocate budget from the worst ROAS to the best.",
            "figure": _fig_dict(fig),
        })

    # Daily spend + revenue trend.
    date_col = roles.get("date")
    if date_col and spend_col and date_col in df.columns and df[date_col].dtype.kind == "M":
        d = df[[date_col, spend_col] + ([rev_col] if rev_col and rev_col in df.columns else [])].dropna().copy()
        d["_period"] = d[date_col].dt.to_period("W").dt.start_time
        agg = d.groupby("_period").sum(numeric_only=True).reset_index()
        ycols = [spend_col] + ([rev_col] if rev_col and rev_col in d.columns else [])
        fig = px.line(agg, x="_period", y=ycols, markers=True,
                      color_discrete_sequence=[CHART_PALETTE[0], CHART_PALETTE[1]])
        fig = _layout(fig, "Weekly spend and revenue trend")
        charts.append({
            "title": "Weekly spend & revenue",
            "insight": "Sustained gaps between the lines reveal periods of bad ROAS — investigate creative or audience fatigue then.",
            "figure": _fig_dict(fig),
        })
    return charts


def _subscriptions_charts(df: pd.DataFrame, roles: dict, playbook: dict) -> list[dict]:
    import plotly.express as px
    charts = []
    status_col = roles.get("status")
    plan_col   = roles.get("plan")
    mrr_col    = roles.get("mrr") or roles.get("amount")

    if status_col and status_col in df.columns:
        vc = df[status_col].astype(str).str.lower().value_counts().reset_index()
        vc.columns = [status_col, "count"]
        fig = px.bar(vc, x=status_col, y="count", color=status_col,
                     color_discrete_sequence=CHART_PALETTE)
        fig.update_layout(showlegend=False)
        fig = _layout(fig, "Subscription status distribution")
        charts.append({
            "title": "Status distribution",
            "insight": "Healthy SaaS shows >80% in active+trialing. A large 'paused' bucket signals a working save flow; "
                       "a large 'past_due' bucket signals a billing problem.",
            "figure": _fig_dict(fig),
        })

    if plan_col and mrr_col and plan_col in df.columns and mrr_col in df.columns:
        g = df.groupby(plan_col)[mrr_col].sum().reset_index().sort_values(mrr_col, ascending=False)
        fig = px.bar(g, x=plan_col, y=mrr_col, color=plan_col,
                     color_discrete_sequence=CHART_PALETTE)
        fig.update_layout(showlegend=False, yaxis_title="Total MRR")
        fig = _layout(fig, f"MRR by {plan_col}")
        charts.append({
            "title": f"MRR by {plan_col}",
            "insight": "The plan mix tells you where upgrade gravity is. Flat distribution = a pricing problem; "
                       "all on the lowest plan = no expansion path.",
            "figure": _fig_dict(fig),
        })
    return charts


def _sessions_charts(df: pd.DataFrame, roles: dict, playbook: dict) -> list[dict]:
    import plotly.graph_objects as go
    import plotly.express as px
    charts = []
    event_col = roles.get("event")

    # Funnel chart from the playbook's funnel rows.
    funnel_tbl = next((t for t in (playbook or {}).get("tables", [])
                       if t.get("title") == "Conversion funnel"), None)
    if funnel_tbl and len(funnel_tbl["rows"]) >= 2:
        stages = [r[0] for r in funnel_tbl["rows"]]
        counts = [int(str(r[1]).replace(",", "")) for r in funnel_tbl["rows"]]
        fig = go.Figure(go.Funnel(
            y=stages, x=counts,
            marker=dict(color=CHART_PALETTE[:len(stages)]),
            textinfo="value+percent initial",
        ))
        fig = _layout(fig, "Conversion funnel")
        charts.append({
            "title": "Conversion funnel",
            "insight": "Drop-off concentrated on one step is a UX bug; drop-off spread across all steps is a fit/audience problem.",
            "figure": _fig_dict(fig),
        })

    chan_col = roles.get("channel")
    if chan_col and chan_col in df.columns:
        vc = df[chan_col].dropna().value_counts().head(10).reset_index()
        vc.columns = [chan_col, "events"]
        fig = px.pie(vc, names=chan_col, values="events", hole=0.4,
                     color_discrete_sequence=CHART_PALETTE)
        fig = _layout(fig, f"Traffic source mix ({chan_col})")
        charts.append({
            "title": "Traffic source mix",
            "insight": "Single-source dependence (any segment >60%) is a concentration risk.",
            "figure": _fig_dict(fig),
        })
    return charts


def _reviews_charts(df: pd.DataFrame, roles: dict, playbook: dict) -> list[dict]:
    import plotly.express as px
    charts = []
    rating_col = roles.get("rating")
    sku_col = roles.get("sku") or roles.get("product_name")
    date_col = roles.get("date")

    if rating_col and rating_col in df.columns:
        ratings = pd.to_numeric(df[rating_col], errors="coerce").dropna().round().astype(int)
        vc = ratings.value_counts().sort_index().reset_index()
        vc.columns = ["rating", "count"]
        # Color ratings by sentiment: red→yellow→green.
        rating_palette = {1: "#DC2626", 2: "#F97316", 3: "#CA8A04",
                          4: "#65A30D", 5: "#15803D"}
        fig = px.bar(vc, x="rating", y="count",
                     color="rating",
                     color_discrete_map=rating_palette)
        fig.update_layout(showlegend=False)
        fig = _layout(fig, "Rating distribution")
        charts.append({
            "title": "Rating distribution",
            "insight": "Bimodal distributions (lots of 1s and 5s) reveal polarizing products — usually fit, sizing, or expectations.",
            "figure": _fig_dict(fig),
        })

    # Lowest-rated SKUs as a bar.
    low_tbl = next((t for t in (playbook or {}).get("tables", [])
                    if "Lowest-rated SKUs" in t.get("title", "")), None)
    if low_tbl and len(low_tbl["rows"]):
        sku_names = [r[0] for r in low_tbl["rows"]]
        avg = [float(r[1]) for r in low_tbl["rows"]]
        fig = px.bar(x=avg, y=sku_names, orientation="h",
                     color_discrete_sequence=[CHART_PALETTE[3]])
        fig.update_layout(showlegend=False)
        fig.update_xaxes(title_text="Avg rating")
        fig.update_yaxes(title_text="SKU", categoryorder="total descending")
        fig = _layout(fig, "Lowest-rated SKUs (≥5 reviews)")
        charts.append({
            "title": "Lowest-rated SKUs",
            "insight": "Start your product-fix backlog with the top of this list — every additional bad review compounds.",
            "figure": _fig_dict(fig),
        })
    return charts


def _catalog_charts(df: pd.DataFrame, roles: dict, playbook: dict) -> list[dict]:
    import plotly.express as px
    charts = []
    cat_col   = roles.get("category")
    price_col = roles.get("unit_price") or roles.get("amount")
    cost_col  = roles.get("cost")
    stock_col = roles.get("stock")

    # Margin by category bar.
    if cat_col and price_col and cost_col and all(c in df.columns for c in (cat_col, price_col, cost_col)):
        df2 = df.assign(_m=(df[price_col] - df[cost_col]) / df[price_col].replace(0, np.nan))
        g = df2.groupby(cat_col)["_m"].mean().reset_index().sort_values("_m", ascending=False)
        fig = px.bar(g, x=cat_col, y="_m", color=cat_col,
                     color_discrete_sequence=CHART_PALETTE)
        fig.update_layout(showlegend=False, yaxis_tickformat=".0%")
        fig.update_yaxes(title_text="Avg margin %")
        fig = _layout(fig, f"Margin by {cat_col}")
        charts.append({
            "title": f"Margin by {cat_col}",
            "insight": "Low-margin categories often deserve a price increase before a marketing investment.",
            "figure": _fig_dict(fig),
        })

    # Pareto curve on SKU value.
    if price_col and price_col in df.columns:
        val = df[price_col].fillna(0)
        if stock_col and stock_col in df.columns:
            val = val * pd.to_numeric(df[stock_col], errors="coerce").fillna(0)
        val = val.sort_values(ascending=False).reset_index(drop=True)
        if len(val) >= 5 and val.sum() > 0:
            cum = val.cumsum() / val.sum()
            pct_sku = (np.arange(len(val)) + 1) / len(val)
            fig = px.line(x=pct_sku * 100, y=cum * 100,
                          color_discrete_sequence=[CHART_PALETTE[0]])
            fig.update_xaxes(title_text="% of SKUs (ranked by value, desc)")
            fig.update_yaxes(title_text="% of total catalog value")
            fig = _layout(fig, "Pareto curve — SKU value concentration")
            charts.append({
                "title": "Pareto curve",
                "insight": "Read where the curve hits 80% on the Y-axis — that % of SKUs holds 80% of catalog value.",
                "figure": _fig_dict(fig),
            })
    return charts


def _apps_charts(df: pd.DataFrame, roles: dict, playbook: dict) -> list[dict]:
    import plotly.express as px
    charts = []
    genre_col  = roles.get("genre") or roles.get("category")
    rating_col = roles.get("rating")
    size_col   = roles.get("app_size")
    date_col   = roles.get("date")

    # Top genres by app count.
    if genre_col and genre_col in df.columns:
        vc = df[genre_col].dropna().value_counts().head(12).reset_index()
        vc.columns = [genre_col, "apps"]
        fig = px.bar(vc, x="apps", y=genre_col, orientation="h", color=genre_col,
                     color_discrete_sequence=CHART_PALETTE)
        fig.update_layout(showlegend=False, yaxis={"categoryorder": "total ascending"})
        fig = _layout(fig, f"Apps per {genre_col}")
        charts.append({
            "title": f"Apps per {genre_col}",
            "insight": "A crowded genre means tougher discovery; a thin genre "
                       "can be a positioning opening.",
            "figure": _fig_dict(fig)})

    # Rating distribution (rated apps only — cleaner nullified unrated 0.0s).
    if rating_col and rating_col in df.columns:
        rated = df[rating_col].dropna().round().astype(int)
        if len(rated):
            vc = rated.value_counts().sort_index().reset_index()
            vc.columns = ["stars", "apps"]
            cmap = {1:"#DC2626",2:"#F97316",3:"#CA8A04",4:"#65A30D",5:"#15803D"}
            fig = px.bar(vc, x="stars", y="apps", color="stars",
                         color_discrete_map=cmap)
            fig.update_layout(showlegend=False)
            fig = _layout(fig, "Rating distribution (rated apps only)")
            charts.append({
                "title": "Rating distribution",
                "insight": "App-store ratings skew high — most rated apps sit "
                           "at 4-5 stars, so a 3-star app is effectively bottom-tier.",
                "figure": _fig_dict(fig)})

    # App size distribution (MB).
    if size_col and size_col in df.columns and pd.api.types.is_numeric_dtype(df[size_col]):
        mb = (df[size_col].dropna() / 1_000_000.0)
        mb = mb[(mb > 0) & (mb < mb.quantile(0.98))]
        if len(mb):
            fig = px.histogram(mb, nbins=40, color_discrete_sequence=[CHART_PALETTE[0]])
            fig.update_layout(showlegend=False)
            fig.update_xaxes(title_text="App size (MB)")
            fig.update_yaxes(title_text="Apps")
            fig = _layout(fig, "App size distribution (MB)")
            charts.append({
                "title": "App size distribution",
                "insight": "Larger apps face download friction on cellular — "
                           "watch where your app sits in this distribution.",
                "figure": _fig_dict(fig)})

    # Releases per year.
    if date_col and date_col in df.columns and df[date_col].dtype.kind == "M":
        yr = df[date_col].dt.year.dropna().astype(int)
        yr = yr[(yr >= 2008) & (yr <= 2030)]
        if len(yr):
            vc = yr.value_counts().sort_index().reset_index()
            vc.columns = ["year", "apps"]
            fig = px.bar(vc, x="year", y="apps",
                         color_discrete_sequence=[CHART_PALETTE[2]])
            fig = _layout(fig, "Apps released per year")
            charts.append({
                "title": "Release trend",
                "insight": "Release volume per year shows how saturated the "
                           "store has become — recent years are the competition.",
                "figure": _fig_dict(fig)})
    return charts


_CHART_BUILDERS = {
    "orders":        _orders_charts,
    "marketing":     _marketing_charts,
    "subscriptions": _subscriptions_charts,
    "sessions":      _sessions_charts,
    "reviews":       _reviews_charts,
    "catalog":       _catalog_charts,
    "apps":          _apps_charts,
}


def build_archetype_charts(archetype_name: str, df: pd.DataFrame,
                           roles: dict, playbook: dict | None) -> list[dict]:
    """Return archetype-specific charts. Safe to call alongside the generic
    auto-chart builder — both lists are concatenated downstream."""
    fn = _CHART_BUILDERS.get(archetype_name)
    if not fn:
        return []
    try:
        return fn(df, roles, playbook or {})
    except Exception as e:
        return [{"title": f"({archetype_name} chart builder failed)",
                 "insight": str(e), "figure": None}]


# -----------------------------------------------------------------------------
# Dispatcher
# -----------------------------------------------------------------------------
PLAYBOOKS = {
    "orders":        orders_playbook,
    "marketing":     marketing_playbook,
    "customers":     customers_playbook,
    "subscriptions": subscriptions_playbook,
    "sessions":      sessions_playbook,
    "reviews":       reviews_playbook,
    "catalog":       catalog_playbook,
    "apps":          apps_playbook,
}


def run_playbook(archetype_name: str, df: pd.DataFrame, roles: dict) -> dict | None:
    fn = PLAYBOOKS.get(archetype_name)
    if not fn:
        return None
    try:
        return fn(df, roles)
    except Exception as e:
        return {"kpis": [], "tables": [], "segments": [], "alerts": [
            f"Playbook for '{archetype_name}' failed: {e}"
        ], "narrative_hooks": {}}
