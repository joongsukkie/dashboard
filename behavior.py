"""
Behavioral knowledge base for the synthetic-research engine.

Generic LLM personas estimate intent like an uncalibrated marketing brochure:
optimistic, monotone, and blind to the consumer-psychology findings that
real research practitioners have been measuring for 50 years. This module
injects those findings directly into every persona prompt.

What's encoded here is conservative — only effects that are well-replicated
across multiple meta-analyses or are textbook canon in marketing science.
Each block below has at least one anchor citation in the docstring; the
prompt text itself is plain English so the LLM can apply the principle
without a literature lecture in-context.

Public API:
    build_behavioral_block(study_type, demographic_context=None) -> str
    detect_demographic_context(df, roles) -> dict
"""
from __future__ import annotations

import pandas as pd


# -----------------------------------------------------------------------------
# Core behavioral instructions per study type
# -----------------------------------------------------------------------------

# Cross-cutting findings every study should respect.
_UNIVERSAL = """
HUMAN-BEHAVIOR REALITY (apply these — they are MEASURED effects, not opinion):

- Hypothetical bias: stated purchase intent overstates actual behavior by
  about 2-4x for new products (Morwitz et al., 2007 meta-analysis). When a
  prompt asks "would you buy this", the honest answer pulls the number
  DOWN from a naive maximum, not up.

- Social desirability bias: people understate price sensitivity, overstate
  willingness to pay for sustainable / premium / ethical products, and
  overstate trial of healthy / educational / pro-social concepts. Strip
  the halo — judge as the segment would actually behave when nobody is
  watching.

- Within-segment heterogeneity is the norm, not the exception. A segment
  is a distribution, not a point. There is ALWAYS a tail of customers who
  do the opposite of the segment mean — bargain hunters in a premium
  segment, splurgers in a value segment. Your aggregate should reflect
  the segment's CENTRAL TENDENCY, not its unanimous opinion.

- Status quo bias: most customers keep doing what they're doing. New
  concepts, new prices, and new brands face a 'switching cost' tax of
  roughly 20-30% intent reduction vs. an identical incumbent (Samuelson
  & Zeckhauser, 1988).
"""

_PRICING = """
PRICING-SPECIFIC PSYCHOLOGY:

- Anchoring (Tversky & Kahneman, 1974): the FIRST price the segment has
  seen — their historical paid price — is the anchor. Probability drops
  sharply as you move above this anchor and only modestly as you move
  below it. Treat the segment's measured price_median as the anchor.

- Price-quality heuristic: for unfamiliar / aspirational / gift / luxury
  categories, very low prices INCREASE skepticism, not purchase intent
  ('too cheap to be good'). Purchase probability is non-monotonic — it
  can dip at the bottom of the price range for these categories.

- Charm pricing / left-digit bias (Manning & Sprott, 2009): $X.99 reads
  as meaningfully lower than $(X+1).00 — a ~5-10% boost to purchase
  probability vs. a round-number neighbor, larger for price-sensitive
  segments.

- Loss aversion (Kahneman & Tversky, 1979): a price INCREASE from the
  segment's anchor hurts roughly 2x more than an equivalent decrease helps.

- Pain of paying decays with income: high-AOV segments have flatter
  demand curves; low-AOV segments have steep ones. Calibrate elasticity
  to the segment's measured AOV.
"""

_CONCEPT = """
CONCEPT-TEST PSYCHOLOGY:

- Concept-test hypothetical bias is the worst of any study type. The
  standard correction in marketing research: real top-2-box intent runs
  ~30-50% of stated top-2-box intent for new categories, ~50-70% for line
  extensions of familiar brands (Jamieson & Bass, 1989; Morrison, 1979).
  Adjust DOWN aggressively from the naive prompt-following number.

- Novelty halo: any new concept gets an initial bump of curiosity that
  decays within 6-8 weeks. Strip that — estimate sustained intent at
  steady state, not first-look excitement.

- Stated objections under-report price as a concern (people don't want
  to sound cheap). If price isn't called out as the objection but the
  concept's price implication is high for this segment, weight that
  toward the real key_objection.
"""

_COMPARISON = """
COMPARISON-STUDY PSYCHOLOGY:

- Compromise effect (Simonson, 1989): when three options are presented,
  the middle option gets a ~15-25% share boost beyond what its attributes
  would predict. If the options are clearly ordered (cheap / mid / premium),
  the middle wins disproportionately.

- Decoy effect / asymmetric dominance (Huber, Payne & Puto, 1982): an
  option that is dominated by another shifts share toward the dominating
  option. Watch for this when one option is strictly worse than another.

- Brand familiarity tax: an unfamiliar brand starts at roughly 60-70% of
  an established brand's preference, all else equal. Account for which
  options the segment plausibly knows.

- 'No basis for preference' rule: if the options are bare labels with no
  concrete attributes, return NEAR-EQUAL shares (~1/n each, +/- 5%). Do
  not invent preferences from nothing.
"""

_CONJOINT = """
CONJOINT-RANKING PSYCHOLOGY:

- Price is almost always the highest-magnitude part-worth in real
  conjoint studies. If your ranking ignores price, you are wrong.

- Attribute non-compensation: real customers use cutoff rules (won't
  consider above $X, must have feature Y). The ranking should reflect
  some non-linearity — large rank gaps when a cutoff is crossed.

- Familiar brand levels dominate part-worths. A brand the segment buys
  in the real data should rank above an equivalent unfamiliar brand
  even when other attributes are matched.

- Cosmetic naming effects (the 'Red Delicious' finding from this
  engine's own calibration): a profile whose name SOUNDS premium is not
  preferred unless its measured attributes warrant it. Ignore flattering
  marketing words inside attribute values; judge concrete attributes only.
"""

_VAN_WESTENDORP = """
VAN WESTENDORP PSM PSYCHOLOGY:

- The four questions ("too cheap", "bargain", "expensive", "too expensive")
  produce a recognizable signature. Make sure 'too cheap' (suspiciously
  low) sits BELOW 'bargain', 'expensive' sits ABOVE 'bargain', and 'too
  expensive' sits above 'expensive'. If a segment is genuinely insensitive
  to quality signals, the 'too cheap' lower bound can collapse to 0 — that
  is itself a finding, not an error.

- Anchor every threshold to the segment's measured price_median: typical
  PSM bands run from roughly 0.5x to 2x the median paid price for that
  segment. A premium segment will quote a wider absolute band; a price-
  sensitive segment will quote a tighter, lower band.

- The optimal price point (OPP) sits NEAR the intersection of 'too cheap'
  and 'too expensive' — historically slightly below the anchor for
  established categories, slightly above for status / aspirational goods.
"""

_BLOCKS = {
    "pricing": _PRICING,
    "concept": _CONCEPT,
    "comparison": _COMPARISON,
    "conjoint": _CONJOINT,
    "van_westendorp": _VAN_WESTENDORP,
}


def build_behavioral_block(study_type: str,
                           demographic_context: dict | None = None) -> str:
    """Return the full behavioral instruction block to inject into a
    persona prompt for the given study type.

    Combines: the universal block (always), the study-specific block, and
    a demographic / geographic context block when the data supports one.
    """
    parts = [_UNIVERSAL.strip()]
    study_key = (study_type or "").lower()
    if study_key in _BLOCKS:
        parts.append(_BLOCKS[study_key].strip())
    if demographic_context:
        demo = _format_demographic_block(demographic_context)
        if demo:
            parts.append(demo)
    return "\n\n".join(parts)


# -----------------------------------------------------------------------------
# Demographic / geographic context detection — purely deterministic. Picks
# up only the signals that are present in the uploaded data; never invents
# demographics that weren't measured.
# -----------------------------------------------------------------------------

# Heuristic name → role mapping. We deliberately stay narrow: only the
# columns whose values we can credibly summarize without an LLM.
_DEMO_KEYWORDS = {
    "region":      ("region", "country", "market", "geo", "territory"),
    "state":       ("state", "province"),
    "city":        ("city", "metro"),
    "age":         ("age", "birth_year", "yob", "dob"),
    "age_band":    ("age_band", "age_group", "generation", "age_bracket"),
    "gender":      ("gender", "sex"),
    "income":      ("income", "household_income", "salary"),
    "urban":       ("urban", "urbanicity", "rural", "msa", "population_density"),
}

# Rough generational boundaries (US Census reference points, used widely
# in marketing literature). Borders are approximate by design.
_GENERATIONS = [
    (1928, 1945, "Silent Generation (80+)"),
    (1946, 1964, "Boomers (60-78)"),
    (1965, 1980, "Gen X (45-60)"),
    (1981, 1996, "Millennials (29-44)"),
    (1997, 2012, "Gen Z (13-28)"),
    (2013, 2030, "Gen Alpha (under 13)"),
]


def _detect_demo_columns(df: pd.DataFrame) -> dict:
    """Map each demographic role to the best-matching column name, if any."""
    cols_lower = {c: str(c).lower().replace(" ", "_") for c in df.columns}
    out: dict[str, str] = {}
    for role, kws in _DEMO_KEYWORDS.items():
        for c, cl in cols_lower.items():
            if any(kw in cl for kw in kws) and role not in out:
                out[role] = c
                break
    return out


def _age_to_generation(age_years: float) -> str | None:
    """Convert a numeric age into a generation label (approx current year)."""
    if age_years is None or age_years < 0 or age_years > 120:
        return None
    # Convert age → approximate birth year (assume 2025 as the reference).
    by = 2025 - int(age_years)
    for lo, hi, label in _GENERATIONS:
        if lo <= by <= hi:
            return label
    return None


def detect_demographic_context(df: pd.DataFrame, roles: dict | None = None) -> dict:
    """Build a lightweight, factual snapshot of the dataset's demographics.

    Returns a dict like:
        {
          "geography":    {"label": "predominantly US",
                            "top_regions": [("US", 0.62), ...]},
          "age":          {"label": "skews Millennial / Gen X",
                            "median": 36, "top_generation": "Millennials"},
          "gender":       {"label": "60% female", "split": {"F": 0.60, "M": 0.40}},
          "income":       {"label": "middle-to-upper income tier"},
          "urbanicity":   {"label": "urban-leaning"},
        }

    Missing dimensions are simply omitted — nothing is invented.
    """
    if df is None or len(df) == 0:
        return {}
    found = _detect_demo_columns(df)
    out: dict[str, dict] = {}

    # Geography
    geo_col = found.get("region") or found.get("state") or found.get("city")
    if geo_col:
        vc = df[geo_col].dropna().astype(str).value_counts(normalize=True)
        if len(vc) > 0:
            top = list(vc.head(3).items())
            label = (f"predominantly {top[0][0]} ({top[0][1]*100:.0f}%)"
                     if top[0][1] >= 0.55
                     else f"mixed: top markets are " +
                          ", ".join(f"{k} ({v*100:.0f}%)" for k, v in top))
            out["geography"] = {"label": label, "top_regions": top,
                                 "column": geo_col}

    # Age — either numeric age, or pre-bucketed age_band
    if found.get("age"):
        s = pd.to_numeric(df[found["age"]], errors="coerce").dropna()
        # Birth-year columns will show up as 4-digit values
        if len(s) and s.median() > 1900 and s.median() < 2025:
            s = 2025 - s
        if len(s):
            med = float(s.median())
            gen = _age_to_generation(med) or ""
            out["age"] = {"label": f"median age {med:.0f}" +
                           (f", {gen}" if gen else ""),
                          "median": med, "top_generation": gen,
                          "column": found["age"]}
    elif found.get("age_band"):
        vc = df[found["age_band"]].dropna().astype(str).value_counts(normalize=True)
        if len(vc):
            top = vc.head(2)
            out["age"] = {"label": f"age skews {top.index[0]} "
                           f"({top.iloc[0]*100:.0f}%)",
                          "top_band": str(top.index[0]),
                          "column": found["age_band"]}

    # Gender
    if found.get("gender"):
        vc = df[found["gender"]].dropna().astype(str).str.upper().str[0].value_counts(normalize=True)
        if len(vc) >= 2:
            f = float(vc.get("F", 0))
            m = float(vc.get("M", 0))
            if f + m > 0.5:
                dom = "female" if f > m else ("male" if m > f else "balanced")
                pct = max(f, m)
                out["gender"] = {"label": f"{pct*100:.0f}% {dom}" if dom != "balanced"
                                  else "roughly balanced",
                                  "split": {"F": f, "M": m},
                                  "column": found["gender"]}

    # Income
    if found.get("income"):
        s = pd.to_numeric(df[found["income"]], errors="coerce").dropna()
        if len(s):
            med = float(s.median())
            tier = ("low-income (<$40k)" if med < 40_000
                    else "middle-income ($40-100k)" if med < 100_000
                    else "upper-middle ($100-200k)" if med < 200_000
                    else "high-income ($200k+)")
            out["income"] = {"label": f"median income ~${med:,.0f}, {tier}",
                              "median": med, "tier": tier,
                              "column": found["income"]}

    # Urbanicity
    if found.get("urban"):
        vc = df[found["urban"]].dropna().astype(str).str.lower().value_counts(normalize=True)
        if len(vc):
            urban_share = sum(v for k, v in vc.items() if "urban" in k or "metro" in k)
            rural_share = sum(v for k, v in vc.items() if "rural" in k)
            if urban_share > rural_share + 0.15:
                lab = f"urban-leaning ({urban_share*100:.0f}% urban)"
            elif rural_share > urban_share + 0.15:
                lab = f"rural-leaning ({rural_share*100:.0f}% rural)"
            else:
                lab = "mixed urban/rural"
            out["urbanicity"] = {"label": lab, "column": found["urban"]}

    return out


def _format_demographic_block(ctx: dict) -> str:
    """Render the detected context as a prompt-injectable paragraph."""
    if not ctx:
        return ""
    bits = []
    if "geography" in ctx:
        bits.append("- Geography: " + ctx["geography"]["label"] +
                    ". Adjust price sensitivity, brand familiarity, and channel "
                    "norms to this market's reality (US vs EU vs APAC differ on "
                    "discount cadence, online penetration, and brand trust).")
    if "age" in ctx:
        bits.append("- Age cohort: " + ctx["age"]["label"] +
                    ". Generation matters for category adoption, price elasticity, "
                    "and channel preference. Apply the relevant generation's "
                    "documented norms, not blanket 'consumer' averages.")
    if "gender" in ctx:
        bits.append("- Gender split: " + ctx["gender"]["label"] +
                    ". Use only when category-relevant.")
    if "income" in ctx:
        bits.append("- Income: " + ctx["income"]["label"] +
                    ". Price sensitivity scales inversely with income; align the "
                    "elasticity assumption accordingly.")
    if "urbanicity" in ctx:
        bits.append("- Urbanicity: " + ctx["urbanicity"]["label"] +
                    ". Urban segments have stronger online / DTC penetration and "
                    "higher price tolerance for convenience.")
    if not bits:
        return ""
    return ("REAL DEMOGRAPHIC CONTEXT OF THIS DATASET (use to calibrate, not "
            "to stereotype):\n" + "\n".join(bits))
