"""
Synthetic Research — Phase 2: the survey engine.

Takes a research question + the digital-twin segment profiles from Phase 1,
runs a synthetic survey, and aggregates the results.

Design decisions a research scientist would insist on:

1. ONE LLM call PER SEGMENT, not per individual. We ask the model to act
   as an estimator of a *described population* ("what fraction of this
   segment would buy at $X?") rather than to role-play one person. LLMs
   are far better at the former, and it costs 6 calls instead of 200.

2. The model only ever produces a SIGNAL. It never sees the final number
   as truth. Aggregation, weighting, and (in Phase 3) calibration happen
   in deterministic Python.

3. Every result ships with explicit caveats and a confidence grade. A
   synthetic study is a prior, not a measurement — the output says so.

4. Pricing studies are sanity-checked against the Phase-1 revealed-
   preference demand curve. Points outside the observed price band are
   flagged as extrapolation.

The LLM caller is injected (so this module has no dependency on app.py and
is unit-testable with a fake caller).
"""
from __future__ import annotations

import json
import statistics

import numpy as np


# -----------------------------------------------------------------------------
# LLM plumbing
# -----------------------------------------------------------------------------
def _extract_json(text: str) -> dict:
    """Pull a JSON object out of a model response (tolerates fences/prose)."""
    if text is None:
        raise ValueError("empty model response")
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
    a, b = t.find("{"), t.rfind("}")
    if a == -1 or b == -1:
        raise ValueError("no JSON object in model response")
    return json.loads(t[a:b + 1])


def _ask(caller, api_key: str, prompt: str, retries: int = 1) -> dict:
    """Call the injected LLM caller, parse JSON, retry once on bad JSON.

    `caller` has the signature caller(api_key, prompt, strict=False) -> str,
    matching app.py's call_openai / call_anthropic / call_gemini.
    """
    last_err = None
    for attempt in range(retries + 1):
        try:
            raw = caller(api_key, prompt, strict=(attempt > 0))
            return _extract_json(raw)
        except (json.JSONDecodeError, ValueError) as e:
            last_err = e
    raise ValueError(f"model did not return valid JSON: {last_err}")


def _norm_weights(profiles: list[dict]) -> list[float]:
    w = np.array([p.get("weight", 0) or 0 for p in profiles], dtype=float)
    if w.sum() <= 0:
        w = np.ones(len(profiles))
    return (w / w.sum()).tolist()


def _confidence(spread: float, extrapolating: bool, n_segments: int) -> str:
    """Confidence grade from segment agreement + extrapolation + panel size."""
    if n_segments < 2:
        return "low"
    score = 0
    score += 1 if spread < 0.15 else (0 if spread < 0.30 else -1)
    score += -1 if extrapolating else 0
    score += 1 if n_segments >= 4 else 0
    return "high" if score >= 2 else ("medium" if score >= 0 else "low")


# -----------------------------------------------------------------------------
# Persona prompt (kept local to avoid a hard dependency on personas.py at
# call time — the caller passes profiles already built by Phase 1)
# -----------------------------------------------------------------------------
def _persona_block(profile: dict, brand: str) -> str:
    p = profile
    lines = [
        f"You are estimating how a REAL, measured customer segment of {brand} "
        f"would respond to a market-research question. Do NOT role-play one "
        f"person — estimate the behaviour of the whole segment.",
        f"",
        f"Segment: {p['name']}  ({p['weight']*100:.0f}% of customers, "
        f"{p['n_rows']:,} real purchases)",
        f"Measured behaviour:",
    ]
    if p.get("avg_order_value") is not None:
        lines.append(f"  - Average order value: ${p['avg_order_value']:,.2f}")
    if p.get("price_median") is not None:
        lines.append(f"  - Typical price paid per item: ${p['price_median']:,.2f} "
                     f"(usual range ${p.get('price_min',0):,.0f}-${p.get('price_max',0):,.0f})")
    if p.get("discount_rate") is not None:
        depth = p.get("discount_depth") or 0
        lines.append(f"  - Discount behaviour: {p['discount_rate']*100:.0f}% of "
                     f"purchases discounted" + (f", ~{depth*100:.0f}% off" if depth else ""))
    if p.get("repeat_rate") is not None:
        lines.append(f"  - {p['repeat_rate']*100:.0f}% are repeat customers")
    if p.get("top_categories"):
        lines.append(f"  - Buys most: {', '.join(p['top_categories'])}")
    lines.append("")
    lines.append("Estimate realistically and consistently with EVERY number "
                 "above. A discount-driven segment resists full price; a "
                 "full-price segment is less moved by small discounts.")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Study 1 — Pricing
# -----------------------------------------------------------------------------
def run_pricing_study(config: dict, profiles: list[dict], demand_curve: dict | None,
                      caller, api_key: str, brand: str = "the brand") -> dict:
    """Estimate purchase probability across a price grid, per segment, then
    aggregate to an expected demand + revenue curve."""
    product = config.get("product", "the product")
    prices = config.get("price_points") or []
    prices = sorted({float(p) for p in prices if _is_num(p)})
    if len(prices) < 2:
        return {"ok": False, "error": "Need at least 2 price points."}
    if not profiles:
        return {"ok": False, "error": "No segment profiles to survey."}

    weights = _norm_weights(profiles)
    per_segment = []

    for prof in profiles:
        prompt = _persona_block(prof, brand) + f"""

SURVEY TASK — PRICING
Product being tested: {product}

For EACH price below, estimate the fraction of THIS segment (0.0-1.0) that
would purchase the product at that price. Anchor to what this segment
normally pays. Purchase probability must be non-increasing as price rises.

Prices: {prices}

Return ONLY this JSON:
{{"purchase_probability": {{{', '.join(f'"{p}": <0..1>' for p in prices)}}},
  "reasoning": "<2 sentences on this segment's price reaction>"}}"""
        try:
            resp = _ask(caller, api_key, prompt)
            probs = {float(k): _clip01(v) for k, v in
                     resp.get("purchase_probability", {}).items() if _is_num(v)}
            # Enforce monotonicity (purchase prob can't rise with price).
            probs = _enforce_monotone(prices, probs)
            per_segment.append({
                "segment": prof["name"],
                "weight": prof.get("weight"),
                "purchase_probability": probs,
                "reasoning": resp.get("reasoning", ""),
            })
        except Exception as e:
            per_segment.append({"segment": prof["name"], "weight": prof.get("weight"),
                                 "error": str(e)[:160]})

    ok_segments = [s for s in per_segment if "purchase_probability" in s]
    if not ok_segments:
        return {"ok": False, "error": "Every segment call failed.",
                "per_segment": per_segment}

    # Aggregate — weighted mean purchase probability per price.
    agg_curve = []
    for price in prices:
        num = den = 0.0
        seg_vals = []
        for s, w in zip(per_segment, weights):
            if "purchase_probability" in s and price in s["purchase_probability"]:
                num += s["purchase_probability"][price] * w
                den += w
                seg_vals.append(s["purchase_probability"][price])
        prob = (num / den) if den else 0.0
        agg_curve.append({
            "price": price,
            "purchase_probability": round(prob, 4),
            "revenue_index": round(prob * price, 2),
            "segment_spread": round(float(np.std(seg_vals)), 4) if seg_vals else 0.0,
        })

    optimal = max(agg_curve, key=lambda c: c["revenue_index"])

    # Sanity check vs the revealed-preference demand curve.
    caveats = ["Synthetic estimate from digital-twin segments — a research "
               "PRIOR, not a measurement. Validate with a small real survey "
               "before a pricing decision (Phase 3/6)."]
    extrapolating = False
    if demand_curve and demand_curve.get("usable"):
        lo, hi = demand_curve.get("price_min"), demand_curve.get("price_max")
        out = [p for p in prices if (lo and p < lo) or (hi and p > hi)]
        if out:
            extrapolating = True
            caveats.append(f"Prices {out} fall outside the observed "
                           f"${lo:,.0f}-${hi:,.0f} band — those points are "
                           f"extrapolation, not anchored to real demand.")
        # Compare implied elasticity.
        synth_e = _implied_elasticity(agg_curve)
        real_e = demand_curve.get("elasticity")
        if synth_e is not None and real_e is not None:
            caveats.append(f"Synthetic curve implies elasticity {synth_e:.2f} "
                           f"vs {real_e:.2f} measured from real sales — "
                           f"{'consistent' if abs(synth_e-real_e)<0.6 else 'DIVERGENT, treat with caution'}.")
    else:
        caveats.append("No usable real demand curve to cross-check against.")

    spread = float(np.mean([c["segment_spread"] for c in agg_curve]))
    conf = _confidence(spread, extrapolating, len(ok_segments))

    return {
        "ok": True,
        "study_type": "pricing",
        "brand": brand,
        "product": product,
        "per_segment": per_segment,
        "aggregate": {"curve": agg_curve,
                      "optimal_price": optimal["price"],
                      "optimal_revenue_index": optimal["revenue_index"]},
        "recommendation": (
            f"Expected revenue is maximised at ${optimal['price']:,.0f} "
            f"(purchase probability {optimal['purchase_probability']*100:.0f}%). "
            + _segment_pricing_note(per_segment, prices)),
        "confidence": conf,
        "caveats": caveats,
    }


def _segment_pricing_note(per_segment: list[dict], prices: list[float]) -> str:
    """Spot the most and least price-tolerant segment for a marketer note."""
    mid = prices[len(prices) // 2]
    scored = [(s["segment"], s["purchase_probability"].get(mid))
              for s in per_segment if "purchase_probability" in s
              and mid in s.get("purchase_probability", {})]
    if len(scored) < 2:
        return ""
    scored.sort(key=lambda x: x[1] or 0, reverse=True)
    return (f"Most price-tolerant: {scored[0][0]}; most price-sensitive: "
            f"{scored[-1][0]} — consider segment-specific pricing or offers.")


# -----------------------------------------------------------------------------
# Study 2 — Concept test
# -----------------------------------------------------------------------------
def run_concept_test(config: dict, profiles: list[dict],
                     caller, api_key: str, brand: str = "the brand") -> dict:
    """Estimate purchase intent for a new product/feature concept."""
    concept = config.get("concept", "").strip()
    if not concept:
        return {"ok": False, "error": "Describe the concept to test."}
    if not profiles:
        return {"ok": False, "error": "No segment profiles to survey."}

    weights = _norm_weights(profiles)
    per_segment = []
    for prof in profiles:
        prompt = _persona_block(prof, brand) + f"""

SURVEY TASK — CONCEPT TEST
New concept being tested: {concept}

Estimate how THIS segment would respond.

Return ONLY this JSON:
{{"purchase_intent": <0..1 fraction who would buy>,
  "sentiment": "positive" | "neutral" | "negative",
  "key_appeal": "<what this segment would like, 1 sentence>",
  "key_objection": "<the main hesitation, 1 sentence>"}}"""
        try:
            resp = _ask(caller, api_key, prompt)
            per_segment.append({
                "segment": prof["name"], "weight": prof.get("weight"),
                "purchase_intent": _clip01(resp.get("purchase_intent", 0)),
                "sentiment": resp.get("sentiment", "neutral"),
                "key_appeal": resp.get("key_appeal", ""),
                "key_objection": resp.get("key_objection", ""),
            })
        except Exception as e:
            per_segment.append({"segment": prof["name"], "weight": prof.get("weight"),
                                 "error": str(e)[:160]})

    ok = [s for s in per_segment if "purchase_intent" in s]
    if not ok:
        return {"ok": False, "error": "Every segment call failed.",
                "per_segment": per_segment}

    overall = sum(s["purchase_intent"] * w for s, w in zip(per_segment, weights)
                  if "purchase_intent" in s)
    spread = float(np.std([s["purchase_intent"] for s in ok]))
    best = max(ok, key=lambda s: s["purchase_intent"])
    worst = min(ok, key=lambda s: s["purchase_intent"])

    return {
        "ok": True, "study_type": "concept", "brand": brand, "concept": concept,
        "per_segment": per_segment,
        "aggregate": {"overall_purchase_intent": round(overall, 4),
                      "best_segment": best["segment"],
                      "best_segment_intent": round(best["purchase_intent"], 4),
                      "weakest_segment": worst["segment"],
                      "weakest_segment_intent": round(worst["purchase_intent"], 4)},
        "recommendation": (
            f"Estimated overall purchase intent {overall*100:.0f}%. Lead the "
            f"launch with '{best['segment']}' ({best['purchase_intent']*100:.0f}% "
            f"intent); '{worst['segment']}' is the hardest sell "
            f"({worst['purchase_intent']*100:.0f}%)."),
        "confidence": _confidence(spread, False, len(ok)),
        "caveats": ["Synthetic estimate — a directional prior, not a measured "
                    "intent rate. Concept-test intent from LLMs tends to be "
                    "optimistic; validate the absolute number with real "
                    "respondents before forecasting volume."],
    }


# -----------------------------------------------------------------------------
# Study 3 — Comparison (brands / products / messages)
# -----------------------------------------------------------------------------
def run_comparison_study(config: dict, profiles: list[dict],
                         caller, api_key: str, brand: str = "the brand") -> dict:
    """Estimate preference share across 2-4 options. Works for brand
    comparison, product comparison, and message/value-prop testing."""
    options = [str(o).strip() for o in (config.get("options") or []) if str(o).strip()]
    question = config.get("question", "Which option do you prefer?")
    if len(options) < 2:
        return {"ok": False, "error": "Need at least 2 options to compare."}
    if not profiles:
        return {"ok": False, "error": "No segment profiles to survey."}

    weights = _norm_weights(profiles)
    labels = [chr(65 + i) for i in range(len(options))]  # A, B, C, D
    opt_block = "\n".join(f"  {l}: {o}" for l, o in zip(labels, options))
    per_segment = []

    for prof in profiles:
        prompt = _persona_block(prof, brand) + f"""

SURVEY TASK — COMPARISON
Question: {question}

Options:
{opt_block}

Estimate the share of THIS segment preferring each option (must sum to ~1.0).

Return ONLY this JSON:
{{"shares": {{{', '.join(f'"{l}": <0..1>' for l in labels)}}},
  "reasoning": "<why this segment splits the way it does, 2 sentences>"}}"""
        try:
            resp = _ask(caller, api_key, prompt)
            shares = {l: _clip01(resp.get("shares", {}).get(l, 0)) for l in labels}
            tot = sum(shares.values()) or 1.0
            shares = {l: v / tot for l, v in shares.items()}  # renormalize
            per_segment.append({
                "segment": prof["name"], "weight": prof.get("weight"),
                "shares": {l: round(v, 4) for l, v in shares.items()},
                "reasoning": resp.get("reasoning", ""),
            })
        except Exception as e:
            per_segment.append({"segment": prof["name"], "weight": prof.get("weight"),
                                 "error": str(e)[:160]})

    ok = [s for s in per_segment if "shares" in s]
    if not ok:
        return {"ok": False, "error": "Every segment call failed.",
                "per_segment": per_segment}

    agg = {}
    for l in labels:
        num = den = 0.0
        for s, w in zip(per_segment, weights):
            if "shares" in s:
                num += s["shares"].get(l, 0) * w
                den += w
        agg[l] = round(num / den, 4) if den else 0.0

    winner = max(agg, key=agg.get)
    winner_idx = labels.index(winner)
    spread = float(np.std([s["shares"].get(winner, 0) for s in ok]))

    return {
        "ok": True, "study_type": "comparison", "brand": brand,
        "question": question,
        "options": {l: o for l, o in zip(labels, options)},
        "per_segment": per_segment,
        "aggregate": {"preference_share": agg, "winner": winner,
                      "winner_option": options[winner_idx]},
        "recommendation": (
            f"Estimated winner: Option {winner} — \"{options[winner_idx]}\" "
            f"({agg[winner]*100:.0f}% preference share). "
            + _comparison_segment_note(per_segment, winner, labels)),
        "confidence": _confidence(spread, False, len(ok)),
        "caveats": ["Synthetic estimate — preference SHARES are more reliable "
                    "than absolute intent, but still a prior. If the top two "
                    "options are within ~10 points, treat it as a tie until "
                    "validated with real respondents."],
    }


def _comparison_segment_note(per_segment: list[dict], winner: str,
                              labels: list[str]) -> str:
    """Flag any segment that disagrees with the overall winner."""
    dissent = []
    for s in per_segment:
        if "shares" not in s:
            continue
        seg_winner = max(s["shares"], key=s["shares"].get)
        if seg_winner != winner:
            dissent.append(f"{s['segment']} prefers {seg_winner}")
    if dissent:
        return "Not unanimous — " + "; ".join(dissent) + "."
    return "Preference is consistent across all segments."


# -----------------------------------------------------------------------------
# Dispatcher
# -----------------------------------------------------------------------------
def run_study(study_type: str, config: dict, profiles: list[dict],
              demand_curve: dict | None, caller, api_key: str,
              brand: str = "the brand") -> dict:
    study_type = (study_type or "").lower()
    if study_type == "pricing":
        return run_pricing_study(config, profiles, demand_curve, caller, api_key, brand)
    if study_type == "concept":
        return run_concept_test(config, profiles, caller, api_key, brand)
    if study_type == "comparison":
        return run_comparison_study(config, profiles, caller, api_key, brand)
    return {"ok": False, "error": f"Unknown study type: {study_type}"}


# -----------------------------------------------------------------------------
# Small numeric helpers
# -----------------------------------------------------------------------------
def _is_num(x) -> bool:
    try:
        float(x)
        return True
    except (TypeError, ValueError):
        return False


def _clip01(x) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def _enforce_monotone(prices: list[float], probs: dict) -> dict:
    """Purchase probability must not increase as price rises. Clamp any
    violation down to the previous (lower-price) value."""
    out = {}
    prev = 1.0
    for p in sorted(prices):
        v = probs.get(p, prev)
        v = min(v, prev)
        out[p] = round(v, 4)
        prev = v
    return out


def _implied_elasticity(curve: list[dict]) -> float | None:
    """Log-log slope of purchase probability vs price — the synthetic
    curve's implied elasticity, for cross-checking against real demand."""
    pts = [(c["price"], c["purchase_probability"]) for c in curve
           if c["price"] > 0 and c["purchase_probability"] > 0]
    if len(pts) < 2:
        return None
    logp = np.log([p for p, _ in pts])
    logq = np.log([q for _, q in pts])
    if np.std(logp) < 1e-9:
        return None
    slope = float(np.polyfit(logp, logq, 1)[0])
    return round(slope, 3)
