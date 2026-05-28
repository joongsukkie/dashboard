"""
Granular confidence rating for synthetic-research results.

The original engine returned a single low / medium / high grade. That was
honest about uncertainty in the aggregate but invisible about WHY — the
user couldn't tell whether "high" meant "the panel was huge" or "the
segments all agreed" or "this is calibrated." It also missed two real
failure modes that the user's first audited run exposed:

  1. Domain mismatch: a concept about a $30 water bottle scored against
     a dataset whose median unit price is $303 across every category
     deserves a confidence haircut, even when the panel agrees.

  2. Thin demographic coverage: a dataset with only a 'State' column
     supports geographic calibration but not generational or income
     calibration, so the prompts are partially uncalibrated.

This module decomposes the trust judgment into six measurable components,
each capped, summing to a 0–100 score with a grade band attached. Each
component reports its own value + a one-line explanation, so the result
card can show a meter the user can audit.

Components (max points):
   - Panel quality           (25): segment count × min sample sizes
   - Segment agreement       (20): inverse of cross-segment spread
   - Calibration grade       (20): backtest-derived trust grade
   - Domain fit              (15): concept matches the segment price band
   - Demographic coverage    (10): demographic dimensions detected
   - Bounds check            (10): no extrapolation outside observed range
                              ---
                              100

Public API:
    score_confidence(result, profiles=None, demographic_context=None,
                     config=None, calibration=None) -> dict
"""
from __future__ import annotations

import math
import re


# -----------------------------------------------------------------------------
# Score → grade banding
# -----------------------------------------------------------------------------
def _grade_from_score(score: float) -> str:
    if score >= 80: return "very-high"
    if score >= 65: return "high"
    if score >= 45: return "medium"
    if score >= 25: return "low"
    return "very-low"


# -----------------------------------------------------------------------------
# Component scorers — each returns (points, max, note). Points may be 0.
# -----------------------------------------------------------------------------

def _score_panel_quality(result: dict, profiles: list | None) -> tuple[float, float, str]:
    """How big and well-populated is the synthetic panel?

    - 6+ segments AND every segment ≥ 30 rows ⇒ full marks
    - Fewer segments OR thin sample sizes lose points roughly linearly
    """
    MAX = 25.0
    ok_segments = [s for s in result.get("per_segment", [])
                   if not s.get("error")]
    n_seg = len(ok_segments)
    n_rows_per = []
    for s in ok_segments:
        rows = s.get("n_rows")
        if rows is None and profiles:
            match = next((p for p in profiles if p.get("name") == s.get("segment")), None)
            if match: rows = match.get("n_rows")
        if rows: n_rows_per.append(int(rows))

    if n_seg < 2:
        return 0.0, MAX, "Fewer than 2 segments responded — no triangulation."

    seg_pts = min(15.0, (n_seg / 6.0) * 15.0)
    if n_rows_per:
        min_n = min(n_rows_per)
        # 30 is the 'small but credible' bar; below 10 is dangerous.
        sample_pts = min(10.0, max(0.0, (min_n - 5) / 25.0 * 10.0))
        note = (f"{n_seg} segments, smallest = {min_n:,} real rows.")
    else:
        sample_pts = 0.0
        note = (f"{n_seg} segments responded but row counts unavailable.")
    total = round(seg_pts + sample_pts, 1)
    return total, MAX, note


def _score_agreement(result: dict) -> tuple[float, float, str]:
    """How tightly did segments agree? Tight agreement = stronger signal."""
    MAX = 20.0
    spread = _measure_spread(result)
    if spread is None:
        return 10.0, MAX, "Spread not measurable for this study type."
    # 0.05 spread or less ⇒ full marks; 0.30 spread ⇒ zero points.
    pts = max(0.0, min(MAX, MAX * (1 - (spread - 0.05) / 0.25)))
    if spread < 0.10:
        note = f"Strong agreement (spread {spread:.2f}) — segments converged."
    elif spread < 0.20:
        note = f"Moderate agreement (spread {spread:.2f}) — typical."
    else:
        note = f"Wide spread ({spread:.2f}) — segments disagree; treat with caution."
    return round(pts, 1), MAX, note


def _measure_spread(result: dict) -> float | None:
    """Return a single representative spread number for this study."""
    st = result.get("study_type")
    if st == "pricing":
        curve = result.get("aggregate", {}).get("curve", [])
        if curve:
            return float(sum(c.get("segment_spread", 0) for c in curve) / len(curve))
    if st == "concept":
        intents = [s.get("purchase_intent") for s in result.get("per_segment", [])
                   if s.get("purchase_intent") is not None]
        if len(intents) >= 2:
            m = sum(intents) / len(intents)
            return float(math.sqrt(sum((x - m) ** 2 for x in intents) / len(intents)))
    if st == "comparison":
        per_seg = [s for s in result.get("per_segment", []) if s.get("shares")]
        winner = result.get("aggregate", {}).get("winner")
        if per_seg and winner:
            vals = [s["shares"].get(winner, 0) for s in per_seg]
            m = sum(vals) / len(vals)
            return float(math.sqrt(sum((x - m) ** 2 for x in vals) / len(vals)))
    if st == "conjoint":
        per_seg = [s for s in result.get("per_segment", []) if s.get("ranking")]
        order = result.get("aggregate", {}).get("predicted_order_labels", [])
        if per_seg and order:
            top = order[0]
            ranks = [s["ranking"].get(top, 0) for s in per_seg]
            if ranks:
                m = sum(ranks) / len(ranks)
                # Normalize to roughly 0..1 by dividing by the count of profiles
                n_profiles = len(order)
                return float(math.sqrt(sum((x - m) ** 2 for x in ranks) / len(ranks)) / max(1, n_profiles))
    return None


def _score_calibration(calibration: dict | None) -> tuple[float, float, str]:
    """Borrow the backtested trust grade — full points for 'high', etc."""
    MAX = 20.0
    if not calibration or calibration.get("status") != "calibrated":
        return 6.0, MAX, ("Engine not yet backtested for this study type — "
                          "trust the relative ranking more than the absolute number.")
    trust = (calibration.get("trust") or "medium").lower()
    pts = {"high": 20.0, "medium": 13.0, "low": 5.0}.get(trust, 13.0)
    note = (f"Backtested as {trust} trust on real {calibration.get('validated_as','')} "
            f"datasets.")
    return pts, MAX, note


# Concept-word → expected price band heuristic. Loose, but captures the
# common 'wrong-shaped product' failure mode (user's Amazon CSV vs. a
# water-bottle concept).
_PRICE_BAND_HINTS = [
    # (regex over concept text, (typical_low, typical_high))
    (r"\bwater bottle|tumbler|hydroflask|owala|stanley\b", (15, 60)),
    (r"\bsubscription|sub box|sub-box\b", (10, 80)),
    (r"\bsoftware|saas|app\b", (5, 200)),
    (r"\bphone|smartphone|tablet\b", (300, 1500)),
    (r"\blaptop|notebook computer|macbook\b", (700, 3000)),
    (r"\bheadphones?|earbuds?\b", (30, 400)),
    (r"\bcoffee\b", (3, 25)),
    (r"\bcandle|incense\b", (10, 60)),
    (r"\bsneakers?|running shoe|trainers?\b", (40, 250)),
    (r"\bjacket|coat\b", (60, 600)),
    (r"\bcar|vehicle\b", (5000, 80000)),
]


def _expected_price_band(concept: str) -> tuple[float, float] | None:
    """Best-effort guess at a price band the concept implies."""
    if not concept:
        return None
    c = concept.lower()
    for pat, band in _PRICE_BAND_HINTS:
        if re.search(pat, c):
            return band
    return None


def _segment_price_band(profiles: list | None) -> tuple[float, float] | None:
    """Empirical price band from the profiles' measured price_min/max."""
    if not profiles:
        return None
    lows = [p.get("price_min") for p in profiles
            if isinstance(p.get("price_min"), (int, float))]
    highs = [p.get("price_max") for p in profiles
             if isinstance(p.get("price_max"), (int, float))]
    if not lows or not highs:
        return None
    return min(lows), max(highs)


def _score_domain_fit(result: dict, profiles: list | None,
                      config: dict | None) -> tuple[float, float, str]:
    """Does the concept being studied match the data the personas were
    built from? Detects two failure modes:

      1. The concept implies a price the segments never paid (water-bottle
         concept vs. $300-median-per-unit dataset).
      2. The concept references categories absent from the data.
    """
    MAX = 15.0
    cfg = config or {}
    # Build a 'what is being studied' string from the relevant config field.
    concept_text = " ".join(str(v) for v in (
        cfg.get("concept", ""), cfg.get("product", ""),
        cfg.get("question", ""), " ".join(cfg.get("options", []) or [])
    ) if v).strip()

    if not concept_text:
        # No concept text (e.g. pricing-only config). Give partial credit.
        return 10.0, MAX, "No concept text to cross-check against the data."

    notes = []
    pts = MAX

    # Price-band check
    band = _expected_price_band(concept_text)
    seg_band = _segment_price_band(profiles)
    if band and seg_band:
        # Geometric overlap on log scale
        lo = max(band[0], seg_band[0])
        hi = min(band[1], seg_band[1])
        if hi <= lo:
            # No overlap at all
            pts -= 8.0
            notes.append(
                f"Concept implies ~${band[0]:.0f}-${band[1]:.0f} price band "
                f"but your data's measured price band is ${seg_band[0]:.0f}-"
                f"${seg_band[1]:.0f}. Personas have no anchor for this product.")
        else:
            # Some overlap — score the fraction
            data_span = math.log(seg_band[1]) - math.log(max(1, seg_band[0]))
            overlap = math.log(hi) - math.log(max(1, lo))
            frac = overlap / max(0.01, data_span)
            if frac < 0.30:
                pts -= 4.0
                notes.append(
                    f"Concept's implied price band only partially overlaps the "
                    f"data's ${seg_band[0]:.0f}-${seg_band[1]:.0f} range.")
            else:
                notes.append("Concept's price band fits the data's range.")
    elif band and not seg_band:
        notes.append("Could not measure the data's price band for comparison.")
    elif not band:
        notes.append("Concept's price band unknown — fit not penalized.")

    # Category-overlap check using profiles' top_categories
    cats_in_data = set()
    for p in profiles or []:
        for c in p.get("top_categories", []) or []:
            cats_in_data.add(str(c).lower())
    if cats_in_data:
        concept_lower = concept_text.lower()
        any_match = any(cat and cat in concept_lower for cat in cats_in_data)
        # Penalize only if the concept clearly mentions a specific category
        # the data doesn't contain.
        has_category_word = bool(re.search(
            r"\b(shoe|bottle|jacket|laptop|phone|coffee|software|saas|"
            r"car|jewelry|game|book|food|drink|electronic|toy|"
            r"clothing|kitchen|home|grocery|wine|beer|tea)s?\b",
            concept_lower))
        if has_category_word and not any_match:
            pts -= 3.0
            notes.append("Concept mentions a category absent from your "
                         "data's top categories — directional only.")

    pts = max(0.0, round(pts, 1))
    return pts, MAX, " ".join(notes) if notes else "Concept appears to fit the data."


def _score_demographic_coverage(demo_ctx: dict | None) -> tuple[float, float, str]:
    """Reward datasets with rich demographic detail — the prompts can
    calibrate more aggressively when generation, income, and geography
    are all known, not just one of them."""
    MAX = 10.0
    if not demo_ctx:
        return 2.0, MAX, ("No demographic columns detected — prompts have no "
                          "geographic / generational / income calibration anchor.")
    n = len(demo_ctx)
    # 1 dim → 4 pts, 2 → 7, 3+ → full
    pts = min(MAX, 2.0 + n * 2.5)
    detected = ", ".join(demo_ctx.keys())
    note = (f"Detected: {detected}. " +
            ("Rich coverage — prompts calibrated on multiple dimensions."
             if n >= 3 else "Partial coverage."))
    return round(pts, 1), MAX, note


def _score_bounds(result: dict) -> tuple[float, float, str]:
    """Are any prices / options outside the observed range of the data?
    Extrapolation deserves a haircut even if the model gives a confident
    answer about it."""
    MAX = 10.0
    caveats = result.get("caveats", []) or []
    extrap = any(("extrapolation" in c.lower() or "outside" in c.lower())
                 for c in caveats)
    if extrap:
        return 3.0, MAX, ("Some inputs sit outside the observed price band — "
                          "the answer there is extrapolation, not anchored.")
    return MAX, MAX, "All inputs lie inside the observed data range."


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------
def score_confidence(result: dict,
                     profiles: list | None = None,
                     demographic_context: dict | None = None,
                     config: dict | None = None,
                     calibration: dict | None = None) -> dict:
    """Score the trustworthiness of a synthetic-research result.

    Returns a dict:
        {
          "score": 0-100,
          "grade": "very-low" | "low" | "medium" | "high" | "very-high",
          "components": [
             {"name": ..., "points": 19.0, "max": 25.0, "note": "..."},
             ...
          ],
          "warnings": ["..."]  # serious issues to surface front-and-center
        }
    """
    components = [
        ("Panel quality",         *_score_panel_quality(result, profiles)),
        ("Segment agreement",     *_score_agreement(result)),
        ("Calibration grade",     *_score_calibration(calibration)),
        ("Domain fit",            *_score_domain_fit(result, profiles, config)),
        ("Demographic coverage",  *_score_demographic_coverage(demographic_context)),
        ("Bounds check",          *_score_bounds(result)),
    ]
    total = round(sum(c[1] for c in components), 1)
    grade = _grade_from_score(total)

    # Bubble serious issues to the top so users see them above the meter.
    warnings = []
    for name, pts, mx, note in components:
        if mx >= 10 and pts < mx * 0.4:
            warnings.append(f"{name}: {note}")

    return {
        "score": total,
        "grade": grade,
        "components": [
            {"name": name, "points": pts, "max": mx, "note": note}
            for (name, pts, mx, note) in components
        ],
        "warnings": warnings,
    }
