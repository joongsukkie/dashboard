"""
B2C dataset archetype detection.

A senior data analyst looking at an unknown CSV first asks: "What kind of
business artifact is this?" The answer dictates the analytical playbook
(RFM and cohort retention for orders, ROAS and CAC for marketing, MRR
waterfall for subscriptions, etc.).

This module codifies that first-pass classification. We score each of
seven canonical B2C archetypes against the schema, then return the best
match with a confidence score. If the top score is weak (< 0.4), the
caller should fall back to the generic analytical path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class ArchetypeMatch:
    name: str                    # one of the seven archetypes
    confidence: float            # 0..1
    signals: list[str]           # the column-level matches that drove the score
    role_columns: dict[str, str] # mapped roles (order_id, customer_id, amount, date, ...)


# -----------------------------------------------------------------------------
# Role detection — find the column that fills a semantic role.
# -----------------------------------------------------------------------------
ROLE_PATTERNS: dict[str, list[str]] = {
    "order_id":    [r"order[\s_-]?id", r"order[\s_-]?no", r"order[\s_-]?number",
                    r"transaction[\s_-]?id", r"invoice[\s_-]?(id|no|number)",
                    r"^invoice$",         # UCI Online Retail uses bare "Invoice"
                    r"\bdocument[\s_-]?no\b",  # ERP exports
                    r"\border\b"],
    "line_item_id":[r"line[\s_-]?item", r"line[\s_-]?id", r"item[\s_-]?id"],
    "customer_id": [r"customer[\s_-]?id", r"customer[\s_-]?no", r"user[\s_-]?id",
                    r"account[\s_-]?id", r"member[\s_-]?id", r"buyer[\s_-]?id"],
    "email":       [r"^email$", r"e[\s_-]?mail", r"customer[\s_-]?email"],
    "sku":         [r"^sku$", r"product[\s_-]?id", r"product[\s_-]?code",
                    r"item[\s_-]?id", r"item[\s_-]?code", r"asin", r"style"],
    "product_name":[r"product[\s_-]?name", r"product[\s_-]?title", r"item[\s_-]?name",
                    r"^title$", r"^product$"],
    "category":    [r"^category$", r"product[\s_-]?category", r"sub[\s_-]?category",
                    r"department"],
    "qty":         [r"^qty$", r"quantity", r"units", r"items[\s_-]?count"],
    "unit_price":  [r"unit[\s_-]?price", r"^price$", r"mrp", r"list[\s_-]?price",
                    r"item[\s_-]?price"],
    "amount":      [r"revenue", r"sales", r"gross", r"net[\s_-]?sales",
                    r"total[\s_-]?(amount|sales|revenue|paid)",
                    r"order[\s_-]?(total|value|amount)", r"gmv"],
    "discount":    [r"discount", r"promo", r"coupon[\s_-]?value"],
    "cost":        [r"^cost$", r"cogs", r"cost[\s_-]?of[\s_-]?goods", r"unit[\s_-]?cost"],
    "spend":       [r"^spend$", r"ad[\s_-]?spend", r"marketing[\s_-]?cost", r"budget"],
    "impressions": [r"impression"],
    "clicks":      [r"^clicks?$"],
    "conversions": [r"conversion", r"purchases?$"],
    "channel":     [r"^channel$", r"^source$", r"medium", r"utm[\s_-]?source",
                    r"utm[\s_-]?medium", r"traffic[\s_-]?source"],
    "campaign":    [r"campaign[\s_-]?name", r"^campaign$", r"utm[\s_-]?campaign",
                    r"ad[\s_-]?name", r"ad[\s_-]?set"],
    "session_id":  [r"session[\s_-]?id", r"visit[\s_-]?id"],
    "event":       [r"^event$", r"event[\s_-]?name", r"action", r"event[\s_-]?type"],
    "page":        [r"^page$", r"page[\s_-]?path", r"page[\s_-]?url", r"url"],
    "status":      [r"^status$", r"subscription[\s_-]?status", r"state",
                    r"\bchurn(ed)?$",     # Telco churn data uses bare "Churn"
                    r"churn[\s_-]?flag"],
    "plan":        [r"^plan$", r"tier", r"subscription[\s_-]?plan", r"package",
                    r"^contract$", r"contract[\s_-]?type"],  # telco / saas contracts
    "mrr":         [r"\bmrr\b", r"monthly[\s_-]?recurring",
                    r"monthly[\s_-]?charge", r"recurring[\s_-]?charge"],
    "subscription_tenure": [r"^tenure$", r"tenure[\s_-]?months", r"months[\s_-]?as[\s_-]?customer"],
    "rating":      [r"^rating$", r"stars?", r"score", r"^nps$", r"^csat$"],
    "review_text": [r"review[\s_-]?(text|body|content)", r"^review$", r"comment",
                    r"feedback", r"verbatim"],
    "date":        [r"^date$", r"created[\s_-]?at", r"timestamp", r"event[\s_-]?date",
                    r"order[\s_-]?date", r"purchase[\s_-]?date", r"submitted",
                    r"signup", r"signed[\s_-]?up",
                    r"invoice[\s_-]?date",        # UCI Online Retail
                    r"purchase[\s_-]?timestamp",  # Olist
                    r"approved[\s_-]?at"],
    "return_flag": [r"is[\s_-]?return", r"returned", r"refund(ed)?", r"is[\s_-]?refund"],
    "return_reason":[r"return[\s_-]?reason", r"refund[\s_-]?reason"],
    "stock":       [r"^stock$", r"inventory", r"on[\s_-]?hand", r"qty[\s_-]?available"],
}


def _name_match(col: str, patterns: list[str]) -> bool:
    cl = str(col).strip().lower().replace("-", "_").replace(" ", "_")
    return any(re.search(p, cl) for p in patterns)


def detect_roles(df: pd.DataFrame) -> dict[str, str]:
    """Map each semantic role to a single best-matching column (or omit)."""
    roles: dict[str, str] = {}
    for role, pats in ROLE_PATTERNS.items():
        for c in df.columns:
            if _name_match(c, pats):
                # Prefer columns whose dtype matches what we expect.
                if role in ("qty", "unit_price", "amount", "discount", "cost",
                            "spend", "impressions", "clicks", "conversions",
                            "rating", "mrr", "stock"):
                    if not pd.api.types.is_numeric_dtype(df[c]):
                        continue
                if role == "date":
                    if df[c].dtype.kind != "M":
                        # accept object/string too — date parsing may not have
                        # run yet on the raw frame
                        pass
                roles[role] = c
                break
    return roles


# -----------------------------------------------------------------------------
# Archetype scoring
# -----------------------------------------------------------------------------
def _row_grain_unique_ratio(df: pd.DataFrame, col: str) -> float:
    """How close to 1:1 is this column with the rows?"""
    if col not in df.columns:
        return 0.0
    try:
        return df[col].nunique(dropna=True) / max(1, len(df))
    except TypeError:
        return 0.0


ARCHETYPES = ("orders", "customers", "marketing", "sessions",
              "subscriptions", "reviews", "catalog")


def score_archetypes(df: pd.DataFrame, roles: dict[str, str]) -> dict[str, dict]:
    """Score each archetype 0..1 and record the signals that drove it.

    Scoring is intentionally rule-based and additive — easier to debug
    and tweak than a black-box classifier, and the schemas are small
    enough that linear rules dominate.
    """
    scores: dict[str, dict] = {a: {"score": 0.0, "signals": []} for a in ARCHETYPES}
    n = max(1, len(df))

    def add(arch: str, points: float, signal: str):
        scores[arch]["score"] += points
        scores[arch]["signals"].append(signal)

    # ---------------- orders / transactions ----------------------------------
    if "order_id" in roles:
        # Don't over-credit when "order_id" is really 1:1 with rows and
        # there's no per-line breakdown (still orders, just header level).
        add("orders", 0.35, f"order_id column ({roles['order_id']})")
    if "customer_id" in roles or "email" in roles:
        add("orders", 0.10, "customer identifier present")
        add("customers", 0.20, "customer identifier present")
    if "qty" in roles:
        add("orders", 0.15, f"qty/units column ({roles['qty']})")
    if "unit_price" in roles or "amount" in roles:
        add("orders", 0.15, "price/amount column")
        add("catalog", 0.10, "price column")
    if "sku" in roles:
        add("orders", 0.10, f"sku column ({roles['sku']})")
        add("catalog", 0.30, f"sku column ({roles['sku']})")
    if "discount" in roles:
        add("orders", 0.10, "discount column")
    if "return_flag" in roles or "return_reason" in roles:
        add("orders", 0.10, "return / refund column")

    # ---------------- customers (single row per customer) --------------------
    cust_role = roles.get("customer_id") or roles.get("email")
    if cust_role:
        u = _row_grain_unique_ratio(df, cust_role)
        if u >= 0.95:
            # IMPORTANT: if order_id is also present, this is just a 1:1
            # order-to-customer log (very common in DTC) — not a customer
            # master. Don't penalize orders in that case.
            if "order_id" not in roles:
                add("customers", 0.40,
                    f"~1 row per customer ({cust_role}: {u:.0%} unique)")
                scores["orders"]["score"] -= 0.20
                scores["orders"]["signals"].append(
                    "(penalty) customer column is unique per row")
            else:
                # Mild bump only — orders is still the primary read.
                add("customers", 0.10,
                    f"customer-unique grain (but order_id present, so orders)")

    # ---------------- marketing campaigns ------------------------------------
    marketing_cols = sum(1 for r in ("spend", "impressions", "clicks",
                                     "conversions", "campaign", "channel")
                         if r in roles)
    if marketing_cols >= 2:
        add("marketing", 0.20 + 0.10 * marketing_cols,
            f"{marketing_cols} marketing-metric columns")
    if "spend" in roles:
        add("marketing", 0.20, f"spend column ({roles['spend']})")
    if "impressions" in roles or "clicks" in roles:
        add("marketing", 0.15, "impressions/clicks present")
    if "channel" in roles or "campaign" in roles:
        add("marketing", 0.10, "channel/campaign present")

    # ---------------- web / app sessions -------------------------------------
    if "session_id" in roles:
        add("sessions", 0.40, f"session_id ({roles['session_id']})")
    if "event" in roles:
        add("sessions", 0.25, f"event column ({roles['event']})")
    if "page" in roles:
        add("sessions", 0.20, f"page/url column ({roles['page']})")

    # ---------------- subscriptions ------------------------------------------
    if "status" in roles:
        # Status alone is a weak signal; subscriptions also need a customer
        # and a date.
        add("subscriptions", 0.20, f"status column ({roles['status']})")
    if "plan" in roles:
        add("subscriptions", 0.30, f"plan/tier column ({roles['plan']})")
    if "mrr" in roles:
        add("subscriptions", 0.40, f"MRR/monthly-charge column ({roles['mrr']})")
    if "subscription_tenure" in roles:
        # Tenure (months as a customer) is a hallmark subscription metric.
        add("subscriptions", 0.30,
            f"tenure column ({roles['subscription_tenure']})")
        # When tenure is present, "customer-unique grain" is actually
        # subscriptions, not a plain customer master.
        scores["customers"]["score"] -= 0.20
        scores["customers"]["signals"].append(
            "(penalty) tenure column suggests subscriptions, not customer master")
    # Status values that look subscription-y. Now includes telco-style
    # Churn yes/no and Contract enum values.
    status_col = roles.get("status")
    if status_col and df[status_col].dtype == object:
        vals = set(df[status_col].dropna().astype(str).str.lower().unique())
        sub_vals = {"active", "canceled", "cancelled", "paused", "trialing",
                    "past_due", "churned", "yes", "no"}
        if vals & sub_vals:
            add("subscriptions", 0.25,
                f"status values look subscription-y: {sorted(vals & sub_vals)}")
    plan_col = roles.get("plan")
    if plan_col and df[plan_col].dtype == object:
        vals = set(df[plan_col].dropna().astype(str).str.lower().unique())
        contract_vals = {"month-to-month", "one year", "two year",
                         "monthly", "annual", "yearly", "quarterly"}
        if vals & contract_vals:
            add("subscriptions", 0.25,
                f"plan values look like contract terms: {sorted(vals & contract_vals)}")

    # ---------------- reviews / NPS ------------------------------------------
    if "review_text" in roles:
        add("reviews", 0.45, f"review/comment text ({roles['review_text']})")
    if "rating" in roles:
        add("reviews", 0.25, f"rating/stars/NPS column ({roles['rating']})")
    # Free-text object columns with high avg length — looks like reviews.
    for c in df.select_dtypes(include=["object"]).columns:
        try:
            avg_len = df[c].dropna().astype(str).str.len().mean()
            if avg_len and avg_len > 80:
                add("reviews", 0.15, f"long free-text column ({c}, avg {avg_len:.0f} chars)")
                break
        except Exception:
            pass

    # ---------------- product catalog ----------------------------------------
    sku_col = roles.get("sku")
    if sku_col and _row_grain_unique_ratio(df, sku_col) >= 0.95:
        add("catalog", 0.30, f"~1 row per SKU ({sku_col} fully unique)")
    if "stock" in roles:
        add("catalog", 0.25, f"stock/inventory column ({roles['stock']})")
    if "category" in roles:
        add("catalog", 0.10, f"category column ({roles['category']})")
        add("orders", 0.05, "category column")

    # Penalize 'orders' when there's no date and no qty — it's probably
    # something else.
    if "date" not in roles:
        scores["orders"]["score"] -= 0.10
        scores["sessions"]["score"] -= 0.10

    # Clamp.
    for a in scores:
        scores[a]["score"] = max(0.0, min(1.0, scores[a]["score"]))
    return scores


def detect_archetype(df: pd.DataFrame) -> ArchetypeMatch:
    """Top-level detector. Returns the best-matching archetype."""
    roles = detect_roles(df)
    scores = score_archetypes(df, roles)
    best_name = max(scores, key=lambda a: scores[a]["score"])
    best = scores[best_name]

    # If the top score is anemic, label as 'generic' so callers can fall
    # back to the legacy auto-chart path.
    if best["score"] < 0.40:
        return ArchetypeMatch(name="generic", confidence=round(best["score"], 3),
                              signals=best["signals"], role_columns=roles)

    return ArchetypeMatch(
        name=best_name,
        confidence=round(best["score"], 3),
        signals=best["signals"],
        role_columns=roles,
    )


def archetype_description(name: str) -> str:
    """One-line description for the UI badge."""
    return {
        "orders":        "Orders / transactions — purchases over time",
        "customers":     "Customer file — one row per person",
        "marketing":     "Marketing performance — spend, clicks, ROAS",
        "sessions":      "Web / app sessions — events and funnels",
        "subscriptions": "Subscription events — MRR and churn",
        "reviews":       "Reviews / NPS — customer voice text",
        "catalog":       "Product catalog — one row per SKU",
        "generic":       "Unrecognized B2C archetype — running generalist analysis",
    }.get(name, "B2C dataset")
