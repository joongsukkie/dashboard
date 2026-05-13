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
# Dispatcher
# -----------------------------------------------------------------------------
PLAYBOOKS = {
    "orders":    orders_playbook,
    "marketing": marketing_playbook,
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
