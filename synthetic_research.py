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


def _all_failed(per_segment: list[dict]) -> dict:
    """Build a failure result that SURFACES the real underlying errors —
    so 'every call failed' is never a dead end. The per-segment loops catch
    the actual API/parse exception into each segment's 'error' field; we
    bubble a sample of them up into the error string the caller sees."""
    errs = [str(s.get("error", "")).strip() for s in per_segment
            if s.get("error")]
    uniq = list(dict.fromkeys(errs))[:2]
    detail = (" Causes: " + " || ".join(uniq)) if uniq else ""
    return {"ok": False,
            "error": "Every segment LLM call failed." + detail,
            "per_segment": per_segment}


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
def _persona_block(profile: dict, brand: str, evidence: str = "") -> str:
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
    # RAG: real retrieved records grounding this persona.
    if evidence:
        lines.append("")
        lines.append(evidence)
    lines.append("")
    lines.append("Estimate realistically and consistently with EVERY number "
                 "above" + (" AND with the real customer records shown"
                            if evidence else "")
                 + ". A discount-driven segment resists full price; a "
                 "full-price segment is less moved by small discounts.")
    # Calibration finding: the engine was fooled by flattering product NAMES
    # (it ranked 'Red Delicious' top — real shoppers rank it last). Judge by
    # attributes and real experience, never by how a name reads.
    lines.append("CRITICAL: Judge every option by its concrete attributes "
                 "and what customers actually experience — NOT by how "
                 "appealing its NAME or LABEL sounds. Flattering words inside "
                 "a name ('Delicious', 'Premium', 'Pro', 'Ultra', 'Deluxe') "
                 "are marketing, not evidence of quality. A familiar product "
                 "with a nice-sounding name is often one customers dislike.")
    return "\n".join(lines)


def _seg_evidence(evidence_index: dict | None, query: str, segment: str,
                  openai_key: str | None) -> str:
    """Retrieve + format real records for one segment (RAG step). Returns ''
    when no evidence index is available, so the engine still works without it."""
    if not evidence_index:
        return ""
    try:
        import evidence as _ev
        recs = _ev.retrieve_evidence(evidence_index, query, segment,
                                     openai_key=openai_key, k=6)
        return _ev.format_evidence(recs)
    except Exception:
        return ""


# -----------------------------------------------------------------------------
# Study 1 — Pricing
# -----------------------------------------------------------------------------
def run_pricing_study(config: dict, profiles: list[dict], demand_curve: dict | None,
                      caller, api_key: str, brand: str = "the brand",
                      evidence_index: dict | None = None,
                      openai_key: str | None = None) -> dict:
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
        ev = _seg_evidence(evidence_index, product, prof["name"], openai_key)
        prompt = _persona_block(prof, brand, ev) + f"""

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
        return _all_failed(per_segment)

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
                     caller, api_key: str, brand: str = "the brand",
                     evidence_index: dict | None = None,
                     openai_key: str | None = None) -> dict:
    """Estimate purchase intent for a new product/feature concept."""
    concept = config.get("concept", "").strip()
    if not concept:
        return {"ok": False, "error": "Describe the concept to test."}
    if not profiles:
        return {"ok": False, "error": "No segment profiles to survey."}

    weights = _norm_weights(profiles)
    per_segment = []
    for prof in profiles:
        ev = _seg_evidence(evidence_index, concept, prof["name"], openai_key)
        prompt = _persona_block(prof, brand, ev) + f"""

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
        return _all_failed(per_segment)

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
                         caller, api_key: str, brand: str = "the brand",
                         evidence_index: dict | None = None,
                         openai_key: str | None = None) -> dict:
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
    ev_query = question + " " + " ".join(options)
    per_segment = []

    for prof in profiles:
        ev = _seg_evidence(evidence_index, ev_query, prof["name"], openai_key)
        prompt = _persona_block(prof, brand, ev) + f"""

SURVEY TASK — COMPARISON
Question: {question}

Options:
{opt_block}

Estimate the share of THIS segment preferring each option (must sum to ~1.0).

IMPORTANT — calibration rule: if the options are NOT meaningfully
distinguishable (e.g. bare labels like "Option 1 / 2 / 3" with no
descriptive detail to judge), you have no basis for a preference — return
near-EQUAL shares. Only predict a skewed split when the options give real,
concrete differences a customer could actually react to. Do not invent a
preference where there is no information.

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
        return _all_failed(per_segment)

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
# Study 4 — Conjoint (rank multi-attribute product profiles)
# -----------------------------------------------------------------------------
def run_conjoint_study(config: dict, profiles: list[dict],
                       caller, api_key: str, brand: str = "the brand",
                       evidence_index: dict | None = None,
                       openai_key: str | None = None) -> dict:
    """Each segment ranks a set of multi-attribute product profiles. We
    aggregate to a panel ranking and derive part-worths — the average rank
    contribution of each attribute level."""
    cards = config.get("profiles") or []
    attributes = config.get("attributes") or (
        list(cards[0].keys()) if cards else [])
    if len(cards) < 3:
        return {"ok": False, "error": "Need at least 3 product profiles."}
    if not profiles:
        return {"ok": False, "error": "No segment profiles to survey."}

    labels = [f"P{i+1}" for i in range(len(cards))]
    card_block = "\n".join(
        f"  {lab}: " + ", ".join(f"{k}={v}" for k, v in card.items())
        for lab, card in zip(labels, cards))
    ev_query = " ".join(str(v) for card in cards for v in card.values())
    weights = _norm_weights(profiles)
    per_segment = []

    for prof in profiles:
        ev = _seg_evidence(evidence_index, ev_query, prof["name"], openai_key)
        if ev:
            # Chain-of-thought RAG: dumping retrieved data upstream of the
            # task does not work — the model ignores it and follows its
            # name prior. Forcing it to FIRST extract the per-attribute
            # order from the observed data, THEN rank consistent with that,
            # makes it actually use the evidence.
            task = f"""

SURVEY TASK — CONJOINT RANKING (data-grounded, two steps)
You were shown OBSERVED REAL CHOICE DATA above. It is the primary basis
for your answer and OVERRIDES any intuition from how a product's name
sounds ("Delicious", "Premium" etc. are marketing, not evidence).

STEP 1 — From the observed data, write the preference order of every
attribute's levels, MOST-chosen first. Compare the real pick rates
(e.g. "8 of 40" is a lower rate than "19 of 37").
STEP 2 — Rank the {len(cards)} profiles 1 (best) to {len(cards)} (worst),
strictly consistent with STEP 1: a profile whose attribute levels were
chosen MORE in the real data must rank higher. Unique ranks.

{card_block}

Return ONLY this JSON:
{{"data_order": {{"<attribute>": ["level most-chosen", "...", "least"]}},
  "ranking": {{{', '.join(f'"{l}": <1..{len(cards)}>' for l in labels)}}}}}"""
        else:
            task = f"""

SURVEY TASK — CONJOINT RANKING
Rank these {len(cards)} product profiles from 1 (THIS segment's most
preferred) to {len(cards)} (least preferred). Every profile gets a unique
rank. Judge them as this segment would, weighing price against the other
attributes.

{card_block}

Return ONLY this JSON:
{{"ranking": {{{', '.join(f'"{l}": <1..{len(cards)}>' for l in labels)}}}}}"""
        prompt = _persona_block(prof, brand, ev) + task
        try:
            resp = _ask(caller, api_key, prompt)
            raw = resp.get("ranking", {})
            ranks = {l: float(raw[l]) for l in labels if l in raw and _is_num(raw[l])}
            if len(ranks) < len(labels):
                raise ValueError("incomplete ranking")
            per_segment.append({"segment": prof["name"], "weight": prof.get("weight"),
                                 "ranking": ranks})
        except Exception as e:
            per_segment.append({"segment": prof["name"], "weight": prof.get("weight"),
                                 "error": str(e)[:160]})

    ok = [s for s in per_segment if "ranking" in s]
    if not ok:
        return _all_failed(per_segment)

    # Weighted mean rank per profile (lower = more preferred).
    mean_rank = {}
    for lab in labels:
        num = den = 0.0
        for s, w in zip(per_segment, weights):
            if "ranking" in s and lab in s["ranking"]:
                num += s["ranking"][lab] * w
                den += w
        mean_rank[lab] = round(num / den, 3) if den else float(len(labels))

    order = sorted(labels, key=lambda l: mean_rank[l])
    predicted_ranking = [labels.index(l) for l in order]  # 0-based card indices

    # Part-worths: mean profile rank per attribute level (lower = preferred).
    part_worths: dict = {}
    for attr in attributes:
        levels: dict = {}
        for lab, card in zip(labels, cards):
            lvl = str(card.get(attr))
            levels.setdefault(lvl, []).append(mean_rank[lab])
        part_worths[attr] = {lvl: round(float(np.mean(v)), 2)
                             for lvl, v in levels.items()}

    spread = float(np.std([mean_rank[order[0]] for _ in [0]]) if False else 0.0)
    top_card = cards[predicted_ranking[0]]

    return {
        "ok": True, "study_type": "conjoint", "brand": brand,
        "per_segment": per_segment,
        "aggregate": {"mean_rank": mean_rank,
                      "predicted_ranking": predicted_ranking,
                      "predicted_order_labels": order,
                      "part_worths": part_worths,
                      "top_profile": top_card},
        "recommendation": (
            f"Top profile for {brand}: " +
            ", ".join(f"{k}={v}" for k, v in top_card.items()) +
            ". Part-worths show which attribute levels drive preference."),
        "confidence": _confidence(0.1, False, len(ok)),
        "caveats": ["Synthetic conjoint — directional. Part-worths from a "
                    "small synthetic panel are a prior; validate level "
                    "preferences with a real conjoint survey before "
                    "committing a product spec."],
    }


# -----------------------------------------------------------------------------
# Study 5 — Van Westendorp Price Sensitivity Meter
# -----------------------------------------------------------------------------
def run_van_westendorp(config: dict, profiles: list[dict],
                       caller, api_key: str, brand: str = "the brand",
                       evidence_index: dict | None = None,
                       openai_key: str | None = None) -> dict:
    """Ask each segment the four Van Westendorp questions, then build the
    cumulative price-sensitivity curves and locate OPP / IPP and the range
    of acceptable prices."""
    product = config.get("product", "the product")
    if not profiles:
        return {"ok": False, "error": "No segment profiles to survey."}

    weights = _norm_weights(profiles)
    per_segment = []
    for prof in profiles:
        ev = _seg_evidence(evidence_index, product, prof["name"], openai_key)
        prompt = _persona_block(prof, brand, ev) + f"""

SURVEY TASK — VAN WESTENDORP PRICE SENSITIVITY
Product: {product}

Give four price points (numbers only) for a TYPICAL customer in this
segment, anchored to what the segment normally pays:
  too_cheap     — so cheap they'd doubt the quality
  cheap         — a clear bargain, great value
  expensive     — getting pricey, but they'd still consider it
  too_expensive — so expensive they would NOT buy

Constraint: too_cheap < cheap < expensive < too_expensive.

Return ONLY this JSON:
{{"too_cheap": <n>, "cheap": <n>, "expensive": <n>, "too_expensive": <n>}}"""
        try:
            resp = _ask(caller, api_key, prompt)
            pts = {k: float(resp[k]) for k in
                   ("too_cheap", "cheap", "expensive", "too_expensive")
                   if k in resp and _is_num(resp[k])}
            if len(pts) < 4:
                raise ValueError("incomplete price set")
            # Enforce ordering.
            ordered = sorted(pts.values())
            pts = dict(zip(("too_cheap", "cheap", "expensive", "too_expensive"),
                           ordered))
            per_segment.append({"segment": prof["name"], "weight": prof.get("weight"),
                                 **pts})
        except Exception as e:
            per_segment.append({"segment": prof["name"], "weight": prof.get("weight"),
                                 "error": str(e)[:160]})

    ok = [s for s in per_segment if "too_cheap" in s]
    if not ok:
        return _all_failed(per_segment)

    # Build a price grid and the four weighted cumulative curves.
    all_prices = [s[k] for s in ok for k in
                  ("too_cheap", "cheap", "expensive", "too_expensive")]
    lo, hi = min(all_prices), max(all_prices)
    grid = np.linspace(lo, hi, 60)
    ws = [s.get("weight") or 0 for s in ok]
    wsum = sum(ws) or 1.0
    ws = [w / wsum for w in ws]

    def cum(price, key, ge=True):
        # weighted fraction whose `key` value is >= price (ge) or <= price.
        return sum(w for s, w in zip(ok, ws)
                   if (s[key] >= price if ge else s[key] <= price))

    too_cheap     = [cum(p, "too_cheap", ge=True) for p in grid]      # descends
    not_expensive = [cum(p, "expensive", ge=True) for p in grid]      # descends
    cheap_curve   = [cum(p, "cheap", ge=True) for p in grid]          # descends
    too_expensive = [cum(p, "too_expensive", ge=False) for p in grid] # ascends
    expensive     = [cum(p, "expensive", ge=False) for p in grid]     # ascends

    def intersect(a, b):
        diffs = [abs(x - y) for x, y in zip(a, b)]
        return float(grid[int(np.argmin(diffs))])

    opp = intersect(too_cheap, too_expensive)      # optimal price point
    ipp = intersect(cheap_curve, expensive)        # indifference price point
    pmc = intersect(too_cheap, expensive)          # lower bound of acceptable
    pme = intersect(cheap_curve, too_expensive)    # upper bound of acceptable

    return {
        "ok": True, "study_type": "van_westendorp", "brand": brand,
        "product": product,
        "per_segment": per_segment,
        "aggregate": {
            "optimal_price_point": round(opp, 2),
            "indifference_price_point": round(ipp, 2),
            "acceptable_range": [round(min(pmc, pme), 2), round(max(pmc, pme), 2)],
            "curves": {"grid": [round(float(g), 2) for g in grid],
                       "too_cheap": [round(x, 3) for x in too_cheap],
                       "too_expensive": [round(x, 3) for x in too_expensive]},
        },
        "recommendation": (
            f"Van Westendorp suggests an acceptable price range of "
            f"${min(pmc, pme):,.2f}-${max(pmc, pme):,.2f} for {product}. "
            f"The optimal price point (lowest price resistance) is "
            f"${opp:,.2f}; the indifference price point is ${ipp:,.2f}."),
        "confidence": _confidence(0.2, False, len(ok)),
        "caveats": ["Synthetic Van Westendorp — the curves are built from a "
                    "small per-segment panel, so the intersections are coarse. "
                    "Treat the price RANGE as directional and validate with a "
                    "real PSM survey before setting price."],
    }


# -----------------------------------------------------------------------------
# Dispatcher
# -----------------------------------------------------------------------------
def run_study(study_type: str, config: dict, profiles: list[dict],
              demand_curve: dict | None, caller, api_key: str,
              brand: str = "the brand",
              evidence_index: dict | None = None,
              openai_key: str | None = None) -> dict:
    study_type = (study_type or "").lower()
    ev = {"evidence_index": evidence_index, "openai_key": openai_key}
    if study_type == "pricing":
        return run_pricing_study(config, profiles, demand_curve, caller,
                                 api_key, brand, **ev)
    if study_type == "concept":
        return run_concept_test(config, profiles, caller, api_key, brand, **ev)
    if study_type == "comparison":
        return run_comparison_study(config, profiles, caller, api_key, brand, **ev)
    if study_type == "conjoint":
        return run_conjoint_study(config, profiles, caller, api_key, brand, **ev)
    if study_type in ("van_westendorp", "vw", "psm"):
        return run_van_westendorp(config, profiles, caller, api_key, brand, **ev)
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
