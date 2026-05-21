"""
Synthetic Research — Phase 1: digital-twin persona foundation.

Generic synthetic-research tools (Synthetic Users, Outset, etc.) invent
demographic stereotypes and ask an LLM to role-play them. That is exactly
what the "missing human data" / "objective misalignment" critique skewers:
the personas are ungrounded LLM imagination.

This module does the opposite. Every persona here is a DIGITAL TWIN of a
segment that was actually measured in the uploaded sales data — it carries
that segment's real behavioral statistics (order value, price band,
discount behavior, category mix, repeat rate). The LLM is later asked to
reason *as* that measured segment, not as a stereotype.

It also fits a revealed-preference demand curve straight from the sales
data. That curve is the calibration anchor: in Phase 4, synthetic pricing
research is only trusted to extrapolate *beyond* the observed price range —
inside it, the real curve wins.

Nothing in this file calls an LLM. It is fully deterministic and testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd


# -----------------------------------------------------------------------------
# Segment profile — the digital-twin spec
# -----------------------------------------------------------------------------
@dataclass
class SegmentProfile:
    name: str                       # e.g. "Sales_Channel: Online"
    dimension: str                  # the column the segment was cut on
    n_rows: int                     # purchases/rows in this segment
    n_customers: int | None         # unique customers, if a customer column exists
    weight: float                   # share of population (0..1)
    avg_order_value: float | None
    aov_std: float | None
    price_median: float | None
    price_min: float | None
    price_max: float | None
    discount_rate: float | None     # share of purchases that used a discount
    discount_depth: float | None    # avg discount size when discounted (0..1)
    repeat_rate: float | None       # share of customers with >1 order
    top_categories: list[str] = field(default_factory=list)
    top_region: str | None = None
    top_channel: str | None = None
    description: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _revenue_series(df: pd.DataFrame, roles: dict) -> pd.Series | None:
    """Best available per-row revenue: explicit amount, else price*qty,
    else price alone."""
    amt = roles.get("amount")
    if amt and amt in df.columns and pd.api.types.is_numeric_dtype(df[amt]):
        return df[amt]
    price, qty = roles.get("unit_price"), roles.get("qty")
    if (price and qty and price in df.columns and qty in df.columns
            and pd.api.types.is_numeric_dtype(df[price])
            and pd.api.types.is_numeric_dtype(df[qty])):
        return df[price] * df[qty]
    if price and price in df.columns and pd.api.types.is_numeric_dtype(df[price]):
        return df[price]
    return None


def _price_series(df: pd.DataFrame, roles: dict) -> pd.Series | None:
    """Best available per-row *effective* unit price — i.e. what the
    customer actually paid.

    We prefer revenue/qty (net of discounts) over an explicit unit_price
    column, because columns like MRP / list_price are pre-discount and
    overstate the real price point a customer responded to. Demand
    analysis and persona price bands both need the price actually paid.
    """
    amt, qty = roles.get("amount"), roles.get("qty")
    if (amt and qty and amt in df.columns and qty in df.columns
            and pd.api.types.is_numeric_dtype(df[amt])
            and pd.api.types.is_numeric_dtype(df[qty])):
        q = df[qty].replace(0, np.nan)
        eff = df[amt] / q
        if eff.notna().sum() >= 0.5 * len(df):
            return eff
    price = roles.get("unit_price")
    if price and price in df.columns and pd.api.types.is_numeric_dtype(df[price]):
        return df[price]
    return None


# Marketing-meaningful segmentation dimensions, in priority order. We want
# segments a marketer would actually act on.
_SEG_PRIORITY = [
    ("explicit",   ("segment", "persona", "tier", "customer_type", "cohort")),
    ("channel",    ("channel", "source", "medium")),
    ("demographic",("gender", "age_group", "age_band", "generation")),
    ("plan",       ("plan", "subscription", "contract")),
    ("geography",  ("region", "country", "state", "market")),
    ("product",    ("product_line", "category", "department", "product_type")),
]


def _choose_segmentation_column(df: pd.DataFrame, roles: dict,
                                min_segs: int = 2, max_segs: int = 8) -> str | None:
    """Pick the column to cut personas on. Prefers marketing-meaningful,
    low-cardinality categoricals."""
    cols_lower = {c: str(c).lower().replace(" ", "_") for c in df.columns}
    for _kind, keywords in _SEG_PRIORITY:
        for c, cl in cols_lower.items():
            if any(k in cl for k in keywords):
                nun = df[c].dropna().nunique()
                if min_segs <= nun <= max_segs:
                    return c
    # Fallback: any object column with a usable cardinality.
    for c in df.select_dtypes(include=["object"]).columns:
        nun = df[c].dropna().nunique()
        if min_segs <= nun <= max_segs:
            return c
    return None


def _fingerprint(group: pd.DataFrame, full_n: int, seg_col: str,
                 seg_value: str, roles: dict) -> SegmentProfile:
    """Compute one segment's real behavioral fingerprint."""
    n_rows = len(group)
    weight = n_rows / max(1, full_n)

    cust_col = roles.get("customer_id") or roles.get("email")
    n_customers = int(group[cust_col].nunique()) if cust_col and cust_col in group else None

    order_col = roles.get("order_id")
    rev = _revenue_series(group, roles)

    # Average order value — at the order grain if possible.
    # min_count=1 keeps all-NaN orders as NaN (instead of pandas' default
    # of summing them to 0.0, which would silently deflate the average).
    aov = aov_std = None
    if rev is not None:
        if order_col and order_col in group.columns:
            tmp = group.assign(_rev=rev.values)
            per_order = tmp.groupby(order_col)["_rev"].sum(min_count=1)
            per_order = per_order.dropna()
            aov = float(per_order.mean()) if len(per_order) else None
            aov_std = float(per_order.std()) if len(per_order) > 1 else 0.0
        else:
            aov = float(rev.mean())
            aov_std = float(rev.std()) if n_rows > 1 else 0.0

    # Price band.
    price = _price_series(group, roles)
    price_median = price_min = price_max = None
    if price is not None:
        pnn = price.dropna()
        pnn = pnn[pnn > 0]
        if len(pnn):
            price_median = float(pnn.median())
            price_min = float(pnn.quantile(0.05))
            price_max = float(pnn.quantile(0.95))

    # Discount behavior.
    #  - discount_rate: share of ALL purchases that used a discount. An
    #    explicit has-discount boolean is the most reliable source; else
    #    we treat a positive numeric discount as "discounted" and a NaN
    #    discount as "no discount recorded".
    #  - discount_depth: average discount size when one was applied.
    discount_rate = discount_depth = None
    has_disc_col = next(
        (c for c in group.columns
         if "has_discount" in str(c).lower().replace(" ", "_")
         or str(c).lower().replace(" ", "_") in ("discounted", "is_discounted")),
        None,
    )
    if has_disc_col is not None:
        try:
            discount_rate = float(group[has_disc_col].astype(bool).mean())
        except Exception:
            discount_rate = None

    disc_col = roles.get("discount")
    if disc_col and disc_col in group.columns and pd.api.types.is_numeric_dtype(group[disc_col]):
        d_all = group[disc_col]
        scale = 100.0 if d_all.dropna().max() and d_all.dropna().max() > 1.5 else 1.0
        d_all = d_all / scale
        used = d_all.fillna(0) > 0
        if discount_rate is None:
            discount_rate = float(used.mean())
        discount_depth = float(d_all[used].mean()) if used.any() else 0.0

    # Repeat rate.
    repeat_rate = None
    if cust_col and order_col and cust_col in group and order_col in group:
        per_cust = group.groupby(cust_col)[order_col].nunique()
        if len(per_cust):
            repeat_rate = float((per_cust > 1).mean())

    # Category mix — a category column distinct from the segmentation column.
    top_categories: list[str] = []
    cat_col = roles.get("category")
    cand_cats = [cat_col] if cat_col else []
    for c in group.columns:
        cl = str(c).lower().replace(" ", "_")
        if c != seg_col and ("product_line" in cl or "category" in cl
                             or "department" in cl or "product_type" in cl):
            cand_cats.append(c)
    for c in cand_cats:
        if c and c != seg_col and c in group.columns and group[c].dtype == object:
            vc = group[c].dropna().value_counts().head(3)
            if len(vc):
                top_categories = [str(x) for x in vc.index]
                break

    # Region / channel context, if not the segmentation column.
    def _top_of(role_key):
        col = roles.get(role_key)
        if col and col != seg_col and col in group.columns:
            vc = group[col].dropna().value_counts()
            return str(vc.index[0]) if len(vc) else None
        return None
    top_region = _top_of("channel") if False else None
    region_col = next((c for c in group.columns
                       if str(c).lower() in ("region", "country", "state", "market")), None)
    if region_col and region_col != seg_col:
        vc = group[region_col].dropna().value_counts()
        top_region = str(vc.index[0]) if len(vc) else None
    chan_col = roles.get("channel")
    top_channel = None
    if chan_col and chan_col != seg_col and chan_col in group.columns:
        vc = group[chan_col].dropna().value_counts()
        top_channel = str(vc.index[0]) if len(vc) else None

    prof = SegmentProfile(
        name=f"{seg_col}: {seg_value}",
        dimension=seg_col,
        n_rows=n_rows,
        n_customers=n_customers,
        weight=round(weight, 4),
        avg_order_value=round(aov, 2) if aov is not None else None,
        aov_std=round(aov_std, 2) if aov_std is not None else None,
        price_median=round(price_median, 2) if price_median is not None else None,
        price_min=round(price_min, 2) if price_min is not None else None,
        price_max=round(price_max, 2) if price_max is not None else None,
        discount_rate=round(discount_rate, 4) if discount_rate is not None else None,
        discount_depth=round(discount_depth, 4) if discount_depth is not None else None,
        repeat_rate=round(repeat_rate, 4) if repeat_rate is not None else None,
        top_categories=top_categories,
        top_region=top_region,
        top_channel=top_channel,
    )
    prof.description = _describe(prof)
    return prof


def _describe(p: SegmentProfile) -> str:
    """Human-readable behavioral sentence for the segment."""
    bits = [f"{p.weight*100:.0f}% of customers"]
    if p.avg_order_value is not None:
        bits.append(f"average order value ${p.avg_order_value:,.0f}")
    if p.price_median is not None:
        bits.append(f"typically buys items around ${p.price_median:,.0f}")
    if p.discount_rate is not None:
        if p.discount_rate >= 0.6:
            bits.append(f"highly discount-driven ({p.discount_rate*100:.0f}% of orders use a promo)")
        elif p.discount_rate >= 0.25:
            bits.append(f"moderately discount-sensitive ({p.discount_rate*100:.0f}% of orders discounted)")
        else:
            bits.append(f"largely full-price buyers (only {p.discount_rate*100:.0f}% discounted)")
    if p.repeat_rate is not None:
        bits.append(f"{p.repeat_rate*100:.0f}% are repeat customers")
    if p.top_categories:
        bits.append(f"favors {', '.join(p.top_categories[:2])}")
    if p.top_region:
        bits.append(f"concentrated in {p.top_region}")
    return "; ".join(bits) + "."


# -----------------------------------------------------------------------------
# Public API — build the persona set
# -----------------------------------------------------------------------------
def build_segment_profiles(df: pd.DataFrame, roles: dict,
                           playbook: dict | None = None,
                           max_segments: int = 6) -> dict:
    """Turn the cleaned sales data into a set of digital-twin segment
    profiles. Returns:
        {
          "segmentation_column": str | None,
          "profiles": [SegmentProfile.as_dict(), ...],
          "note": str,
        }
    """
    seg_col = _choose_segmentation_column(df, roles)
    if seg_col is None:
        return {
            "segmentation_column": None,
            "profiles": [],
            "note": ("No low-cardinality categorical column found to cut "
                     "personas on. Synthetic research needs at least one "
                     "segmentable dimension (channel, region, product line, "
                     "plan, etc.)."),
        }

    full_n = len(df)
    profiles: list[SegmentProfile] = []
    # Largest segments first — they carry the most weight in the panel.
    order = df[seg_col].value_counts().index.tolist()
    for seg_value in order[:max_segments]:
        group = df[df[seg_col] == seg_value]
        if len(group) < 3:
            continue
        profiles.append(_fingerprint(group, full_n, seg_col, str(seg_value), roles))

    return {
        "segmentation_column": seg_col,
        "profiles": [p.as_dict() for p in profiles],
        "note": (f"Built {len(profiles)} digital-twin segment(s) from "
                 f"'{seg_col}'. Each persona carries the real measured "
                 f"behavior of its segment — not an invented stereotype."),
    }


# -----------------------------------------------------------------------------
# Persona prompt rendering — the digital twin the LLM is asked to become
# -----------------------------------------------------------------------------
def segment_to_persona_prompt(profile: dict, brand: str = "the brand") -> str:
    """Render a grounded digital-twin persona prompt for one segment.

    Unlike the slide's invented '34-year-old marketing manager', every
    number here was measured from the uploaded data.
    """
    p = profile
    lines = [
        f"You are a synthetic survey respondent representing a REAL, measured "
        f"customer segment of {brand}.",
        f"",
        f"Segment: {p['name']}",
        f"This segment was built from {p['n_rows']:,} actual purchases"
        + (f" by {p['n_customers']:,} unique customers" if p.get('n_customers') else "")
        + f" — {p['weight']*100:.0f}% of the customer base.",
        f"",
        f"Measured behavior of this segment (be consistent with ALL of it):",
    ]
    if p.get("avg_order_value") is not None:
        lines.append(f"  - Average order value: ${p['avg_order_value']:,.2f}")
    if p.get("price_median") is not None:
        lines.append(f"  - Typical item price: ${p['price_median']:,.2f} "
                     f"(usual range ${p.get('price_min', 0):,.0f}–${p.get('price_max', 0):,.0f})")
    if p.get("discount_rate") is not None:
        depth = p.get("discount_depth") or 0
        lines.append(f"  - Discount behavior: {p['discount_rate']*100:.0f}% of their "
                     f"purchases use a promotion"
                     + (f", averaging {depth*100:.0f}% off" if depth else ""))
    if p.get("repeat_rate") is not None:
        lines.append(f"  - Loyalty: {p['repeat_rate']*100:.0f}% are repeat customers")
    if p.get("top_categories"):
        lines.append(f"  - Buys most often: {', '.join(p['top_categories'])}")
    if p.get("top_region"):
        lines.append(f"  - Located mostly in: {p['top_region']}")

    lines += [
        f"",
        f"Answer survey questions the way a typical customer in THIS segment "
        f"would realistically answer — consistent with the spending level and "
        f"discount sensitivity above. A discount-driven segment should hesitate "
        f"at full price; a full-price segment should not over-react to small "
        f"discounts. Be concise, realistic, and honest. Do NOT behave like a "
        f"generic enthusiastic shopper.",
    ]
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Panel sampling — within-segment heterogeneity
# -----------------------------------------------------------------------------
def sample_panel(profiles: list[dict], n: int = 200, seed: int = 0) -> list[dict]:
    """Draw n individual synthetic respondents, allocated across segments by
    real weight, each with attributes jittered around the segment's measured
    distribution. This is how we represent within-segment heterogeneity
    instead of one flat 'average customer' per segment.
    """
    if not profiles:
        return []
    rng = np.random.default_rng(seed)
    weights = np.array([p.get("weight", 0) or 0 for p in profiles], dtype=float)
    if weights.sum() <= 0:
        weights = np.ones(len(profiles))
    weights = weights / weights.sum()
    counts = np.round(weights * n).astype(int)

    panel: list[dict] = []
    rid = 0
    for prof, cnt in zip(profiles, counts):
        aov = prof.get("avg_order_value") or 0.0
        std = prof.get("aov_std") or (aov * 0.3)
        drate = prof.get("discount_rate")
        for _ in range(int(cnt)):
            sampled_aov = float(max(0.0, rng.normal(aov, max(std, 1e-6)))) if aov else None
            panel.append({
                "id": f"R{rid:04d}",
                "segment": prof["name"],
                "sampled_order_value": round(sampled_aov, 2) if sampled_aov else None,
                "discount_prone": bool(rng.random() < drate) if drate is not None else None,
            })
            rid += 1
    return panel


# -----------------------------------------------------------------------------
# Revealed-preference demand curve — the calibration anchor
# -----------------------------------------------------------------------------
def fit_demand_curve(df: pd.DataFrame, roles: dict) -> dict:
    """Fit a log-log demand curve from real price/quantity variation.

    elasticity = slope of log(quantity) ~ log(price). This is the
    revealed-preference anchor: in Phase 4, the synthetic pricing model is
    only trusted to extrapolate OUTSIDE [price_min, price_max]; inside that
    range, this real curve is ground truth.

    Caveat surfaced in the output: this is a correlational elasticity, not
    a causal one (no controls for product mix / seasonality). Phase 4
    refines it per-product.
    """
    price = _price_series(df, roles)
    qty_col = roles.get("qty")
    if price is None or not qty_col or qty_col not in df.columns:
        return {"usable": False,
                "note": "Need both a unit price and a quantity column to fit demand."}

    work = pd.DataFrame({"price": price, "qty": df[qty_col]}).dropna()
    work = work[(work["price"] > 0) & (work["qty"] > 0)]
    if len(work) < 30:
        return {"usable": False,
                "note": f"Only {len(work)} valid price/qty rows — too few to fit demand."}

    # Need genuine price variation.
    if work["price"].nunique() < 5 or work["price"].std() < 1e-6:
        return {"usable": False,
                "note": "Not enough price variation in the data to estimate elasticity."}

    # Bin price into quantiles, total quantity per bin → smooths noise.
    try:
        work["_bin"] = pd.qcut(work["price"], q=min(10, work["price"].nunique()),
                               duplicates="drop")
    except ValueError:
        return {"usable": False, "note": "Could not bin prices for demand fitting."}

    binned = work.groupby("_bin", observed=True).agg(
        price=("price", "median"), qty=("qty", "sum")).dropna()
    binned = binned[(binned["price"] > 0) & (binned["qty"] > 0)]
    if len(binned) < 4:
        return {"usable": False, "note": "Too few price bins to fit a curve."}

    logp = np.log(binned["price"].values)
    logq = np.log(binned["qty"].values)
    slope, intercept = np.polyfit(logp, logq, 1)
    pred = slope * logp + intercept
    ss_res = float(np.sum((logq - pred) ** 2))
    ss_tot = float(np.sum((logq - logq.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return {
        "usable": True,
        "elasticity": round(float(slope), 3),
        "r_squared": round(r2, 3),
        "price_min": round(float(work["price"].quantile(0.02)), 2),
        "price_max": round(float(work["price"].quantile(0.98)), 2),
        "n_observations": int(len(work)),
        "curve": [{"price": round(float(p), 2), "quantity": int(q)}
                  for p, q in zip(binned["price"], binned["qty"])],
        "interpretation": _elasticity_text(float(slope)),
        "note": ("Correlational elasticity from binned revealed preference — "
                 "not a controlled causal estimate. Trust it INSIDE the "
                 f"${round(float(work['price'].quantile(0.02)),2)}–"
                 f"${round(float(work['price'].quantile(0.98)),2)} band; "
                 "synthetic research extrapolates beyond it."),
    }


def _elasticity_text(slope: float) -> str:
    if slope > -0.0:
        return ("Positive slope — likely a product-mix confound (premium items "
                "sell more), not true demand. Treat with caution.")
    if slope > -1.0:
        return (f"Inelastic (elasticity {slope:.2f}): demand falls slowly as "
                "price rises — there is room to raise price.")
    if slope > -2.0:
        return (f"Elastic (elasticity {slope:.2f}): demand is fairly sensitive "
                "to price — discounts drive meaningful volume.")
    return (f"Highly elastic (elasticity {slope:.2f}): demand drops sharply with "
            "price — this segment is very price-driven.")
