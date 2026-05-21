"""
Synthetic Research — Phase 3: the calibration harness.

The slides' core demand: "LLMs as informative priors — validate with small
survey samples." This module is that validation. It runs the synthetic
engine against datasets whose real answer is KNOWN and measures the error,
so every live synthetic result can be reported with an evidence-based trust
level instead of false confidence.

Three backtests, three different things they measure:

1. CONJOINT (conjointpizzadata.csv) — a genuine predictive test. The 16
   profiles are fully described (brand, price, crust, ...), so the engine
   *can* reason about them. We score rank correlation vs the real ranking.

2. OPTIMISM (AB_Test_Results.csv) — control vs a "new redesigned variant".
   The real experiment: control WON. If the synthetic panel favours the
   new variant, that quantifies the optimism bias the slides warn about.

3. IGNORANCE CALIBRATION (abtest1.csv) — three undescribed promotions. A
   well-calibrated engine, given options it cannot distinguish, should
   return near-uniform shares. Overconfidence here = miscalibration.

Output: a printed report + calibration_profile.json that the live
/api/synthetic_research endpoint reads to attach an honest trust level.

Run it:  python calibration.py --provider anthropic --key sk-ant-...
(or set ANTHROPIC_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY in the env)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

import synthetic_research as SR


# -----------------------------------------------------------------------------
# Generic baseline panel
# -----------------------------------------------------------------------------
# For a backtest we deliberately use a GENERIC consumer panel, not data-grounded
# personas. This makes the harness self-contained and gives a conservative
# lower bound: real digital-twin personas (Phase 1) should only do better.
GENERIC_PANEL = [
    {"name": "Budget-conscious", "weight": 0.38, "n_rows": 380,
     "avg_order_value": 42.0, "price_median": 14.0, "price_min": 5.0,
     "price_max": 30.0, "discount_rate": 0.72, "discount_depth": 0.32,
     "repeat_rate": 0.30, "top_categories": ["essentials", "value picks"]},
    {"name": "Mainstream", "weight": 0.40, "n_rows": 400,
     "avg_order_value": 88.0, "price_median": 30.0, "price_min": 12.0,
     "price_max": 65.0, "discount_rate": 0.41, "discount_depth": 0.24,
     "repeat_rate": 0.46, "top_categories": ["popular lines"]},
    {"name": "Premium / quality-driven", "weight": 0.22, "n_rows": 220,
     "avg_order_value": 175.0, "price_median": 60.0, "price_min": 32.0,
     "price_max": 140.0, "discount_rate": 0.14, "discount_depth": 0.18,
     "repeat_rate": 0.63, "top_categories": ["premium", "new arrivals"]},
]


# -----------------------------------------------------------------------------
# Dataset loaders → ground-truth cases
# -----------------------------------------------------------------------------
def _find(name: str) -> str | None:
    """Locate a dataset by trying the project root and fixtures/surveys/."""
    for cand in (name, os.path.join("fixtures", "surveys", name)):
        if os.path.exists(cand):
            return cand
    return None


def load_ab_test_results(path: str) -> dict:
    """control vs variant, 10k users. Real winner = whoever converts more."""
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    var_col = next(c for c in df.columns if "variant" in c or "group" in c)
    rev_col = next(c for c in df.columns if "revenue" in c or "value" in c)
    conv = df.assign(_c=df[rev_col] > 0).groupby(var_col)["_c"].mean()
    # Order options so index 0 = control (existing), index 1 = variant (new).
    names = list(conv.index)
    ctrl = next((n for n in names if "control" in str(n).lower()), names[0])
    var = next((n for n in names if n != ctrl), names[-1])
    total = conv[ctrl] + conv[var]
    return {
        "name": "AB_Test_Results — control vs new variant",
        "type": "comparison", "subtype": "optimism",
        "question": "Which version would more customers prefer / convert on?",
        "options": ["the existing, current version of the experience",
                    "a newly redesigned variant of the experience"],
        "true_shares": [float(conv[ctrl] / total), float(conv[var] / total)],
        "true_winner_index": 0 if conv[ctrl] >= conv[var] else 1,
        "new_option_index": 1,
        "note": (f"Real conversion: control {conv[ctrl]*100:.2f}% vs "
                 f"variant {conv[var]*100:.2f}% — control won."),
    }


def load_marketing_ab(path: str) -> dict:
    """Fast-food campaign: 3 undescribed promotions. Real winner = top sales."""
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    promo_col = next(c for c in df.columns if "promotion" in c.lower())
    sales_col = next(c for c in df.columns if "sales" in c.lower())
    means = df.groupby(promo_col)[sales_col].mean().sort_index()
    shares = (means / means.sum()).tolist()
    return {
        "name": "abtest1 — 3 fast-food promotions (undescribed)",
        "type": "comparison", "subtype": "ignorance",
        "question": "Which promotion would customers respond to most?",
        "options": [f"Promotion {int(p)}" for p in means.index],
        "true_shares": shares,
        "true_winner_index": int(np.argmax(means.values)),
        "note": (f"Real mean sales/promo: "
                 + ", ".join(f"P{int(p)} ${m:.1f}K"
                             for p, m in means.items())
                 + ". Options are undescribed — a calibrated engine should "
                   "return near-uniform shares."),
    }


def load_conjoint(path: str) -> dict:
    """16 fully-described pizza profiles ranked 1..16 (1 = best)."""
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    rank_col = next(c for c in df.columns if "rank" in c)
    df = df.sort_values(rank_col).reset_index(drop=True)  # best -> worst
    attrs = [c for c in df.columns if c != rank_col]
    cards = df[attrs].astype(str).to_dict("records")
    return {
        "name": "conjoint pizza — 16 profiles",
        "type": "conjoint",
        "profiles": cards,                       # already in true-best->worst order
        "attributes": attrs,
        "true_ranking": list(range(len(cards))),  # card i has true rank i
        "note": f"{len(cards)} fully-described profiles with a known ranking.",
    }


# -----------------------------------------------------------------------------
# Scoring
# -----------------------------------------------------------------------------
def _spearman(a: list[float], b: list[float]) -> float:
    """Spearman rank correlation (no scipy dependency needed)."""
    n = len(a)
    if n < 2:
        return 0.0
    ra, rb = _rankdata(a), _rankdata(b)
    ra, rb = np.array(ra), np.array(rb)
    if ra.std() < 1e-9 or rb.std() < 1e-9:
        return 0.0
    return float(np.corrcoef(ra, rb)[0, 1])


def _rankdata(x: list[float]) -> list[float]:
    order = np.argsort(x)
    ranks = np.empty(len(x), dtype=float)
    ranks[order] = np.arange(len(x), dtype=float)
    return ranks.tolist()


def score_comparison(case: dict, result: dict) -> dict:
    """Score a comparison backtest against known shares + winner."""
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error")}
    shares = result["aggregate"]["preference_share"]
    labels = sorted(shares.keys())  # A, B, C...
    pred = [shares[l] for l in labels]
    true = case["true_shares"]
    pred_winner = int(np.argmax(pred))
    out = {
        "ok": True,
        "predicted_shares": [round(p, 3) for p in pred],
        "true_shares": [round(t, 3) for t in true],
        "share_mae": round(float(np.mean(np.abs(np.array(pred) - np.array(true)))), 4),
        "winner_correct": pred_winner == case["true_winner_index"],
    }
    if case.get("subtype") == "optimism":
        # How much does the panel over-favour the NEW option vs reality?
        ni = case["new_option_index"]
        out["optimism_bias"] = round(pred[ni] - true[ni], 4)
    if case.get("subtype") == "ignorance":
        # Distance from uniform — should be near 0 for undescribed options.
        k = len(pred)
        out["overconfidence"] = round(
            float(np.mean(np.abs(np.array(pred) - 1.0 / k))), 4)
    return out


def score_conjoint(case: dict, result: dict) -> dict:
    """Score a conjoint backtest against the known ranking."""
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error")}
    pred_order = result["aggregate"]["predicted_ranking"]  # card indices best->worst
    n = len(case["true_ranking"])
    # Position of each card in each ranking.
    true_pos = {i: i for i in range(n)}                    # true: card i at pos i
    pred_pos = {card: pos for pos, card in enumerate(pred_order)}
    a = [true_pos[i] for i in range(n)]
    b = [pred_pos.get(i, n) for i in range(n)]
    top3_true = set(range(min(3, n)))
    top3_pred = set(pred_order[:3])
    return {
        "ok": True,
        "spearman": round(_spearman(a, b), 3),
        "top1_correct": pred_order[0] == 0,
        "top3_overlap": len(top3_true & top3_pred),
        "predicted_order": pred_order,
    }


# -----------------------------------------------------------------------------
# Backtest runners
# -----------------------------------------------------------------------------
def backtest_comparison(case: dict, caller, api_key: str) -> dict:
    res = SR.run_comparison_study(
        {"question": case["question"], "options": case["options"]},
        GENERIC_PANEL, caller, api_key, brand="the brand")
    score = score_comparison(case, res)
    return {"case": case["name"], "subtype": case.get("subtype"),
            "score": score, "note": case["note"]}


def backtest_conjoint(case: dict, caller, api_key: str) -> dict:
    res = SR.run_conjoint_study(
        {"profiles": case["profiles"], "attributes": case["attributes"]},
        GENERIC_PANEL, caller, api_key, brand="the brand")
    score = score_conjoint(case, res)
    return {"case": case["name"], "score": score, "note": case["note"]}


# -----------------------------------------------------------------------------
# Calibration suite
# -----------------------------------------------------------------------------
def run_calibration(caller, api_key: str, write: bool = True) -> dict:
    results = []

    ab1 = _find("AB_Test_Results.csv")
    if ab1:
        print(f"  · running optimism backtest on {ab1} ...")
        results.append(backtest_comparison(load_ab_test_results(ab1), caller, api_key))

    ab2 = _find("abtest1.csv")
    if ab2:
        print(f"  · running ignorance-calibration backtest on {ab2} ...")
        results.append(backtest_comparison(load_marketing_ab(ab2), caller, api_key))

    cj = _find("conjointpizzadata.csv")
    if cj:
        print(f"  · running conjoint backtest on {cj} ...")
        results.append(backtest_conjoint(load_conjoint(cj), caller, api_key))

    # Aggregate into a calibration profile.
    comp = [r for r in results if r["score"].get("ok")
            and "share_mae" in r["score"]]
    conj = [r for r in results if r["score"].get("ok")
            and "spearman" in r["score"]]

    profile: dict = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "n_backtests": len(results),
        "results": results,
    }

    if comp:
        profile["comparison"] = {
            "winner_accuracy": round(
                float(np.mean([1.0 if r["score"]["winner_correct"] else 0.0
                               for r in comp])), 3),
            "share_mae": round(
                float(np.mean([r["score"]["share_mae"] for r in comp])), 4),
        }
        opt = [r["score"]["optimism_bias"] for r in comp
               if "optimism_bias" in r["score"]]
        if opt:
            profile["comparison"]["optimism_bias"] = round(float(np.mean(opt)), 4)
        ign = [r["score"]["overconfidence"] for r in comp
               if "overconfidence" in r["score"]]
        if ign:
            profile["comparison"]["ignorance_overconfidence"] = round(
                float(np.mean(ign)), 4)

    if conj:
        profile["conjoint"] = {
            "spearman": round(float(np.mean([r["score"]["spearman"]
                                             for r in conj])), 3),
            "top1_accuracy": round(float(np.mean([1.0 if r["score"]["top1_correct"]
                                                  else 0.0 for r in conj])), 3),
            "avg_top3_overlap": round(float(np.mean([r["score"]["top3_overlap"]
                                                     for r in conj])), 2),
        }

    profile["trust"] = _trust_grades(profile)
    profile["recommended_corrections"] = _corrections(profile)
    profile["notes"] = _notes(profile)

    if write:
        with open("calibration_profile.json", "w") as f:
            json.dump(profile, f, indent=2)
        print("  · wrote calibration_profile.json")
    return profile


def _trust_grades(p: dict) -> dict:
    grades = {}
    c = p.get("conjoint", {})
    s = c.get("spearman")
    grades["conjoint"] = ("high" if s and s >= 0.6 else
                          "medium" if s and s >= 0.3 else "low")
    comp = p.get("comparison", {})
    bias = abs(comp.get("optimism_bias", 1.0))
    grades["comparison"] = ("high" if bias <= 0.10 else
                            "medium" if bias <= 0.25 else "low")
    # Pricing has no direct backtest yet — inherit conjoint as a proxy.
    grades["pricing"] = grades["conjoint"]
    return grades


def _corrections(p: dict) -> dict:
    """Corrections the live engine can apply: shrink optimism, widen CIs."""
    comp = p.get("comparison", {})
    out = {}
    if "optimism_bias" in comp:
        # Subtract the measured bias from the 'new' option's share.
        out["optimism_shift"] = -round(comp["optimism_bias"], 4)
    if "share_mae" in comp:
        # Use measured MAE as a +/- band on every predicted share.
        out["share_uncertainty"] = round(comp["share_mae"], 4)
    return out


def _notes(p: dict) -> list[str]:
    notes = []
    comp = p.get("comparison", {})
    if comp.get("optimism_bias", 0) > 0.10:
        notes.append(f"Optimism bias detected: the panel over-favours the new "
                     f"option by {comp['optimism_bias']*100:.0f} points. The "
                     f"live engine will shrink 'new'-option shares accordingly.")
    if comp.get("ignorance_overconfidence", 0) > 0.10:
        notes.append(f"On undescribed options the panel was "
                     f"{comp['ignorance_overconfidence']*100:.0f} points away "
                     f"from a uniform split — it manufactures signal where "
                     f"there is none. Treat comparison shares cautiously when "
                     f"options are vague.")
    conj = p.get("conjoint", {})
    if conj.get("spearman") is not None:
        notes.append(f"Conjoint rank correlation with real preferences: "
                     f"{conj['spearman']:.2f}. "
                     + ("Strong — the engine recovers real preference order."
                        if conj["spearman"] >= 0.6 else
                        "Moderate/weak — treat conjoint output as directional only."))
    return notes


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def _resolve_caller(provider: str, key: str):
    from app import call_openai, call_anthropic, call_gemini
    return {"openai": call_openai, "anthropic": call_anthropic,
            "gemini": call_gemini}[provider]


def _print_report(p: dict) -> None:
    print("\n" + "=" * 64)
    print("  SYNTHETIC ENGINE — CALIBRATION REPORT")
    print("=" * 64)
    print(f"  Backtests run: {p['n_backtests']}   ({p['generated_at']})\n")

    if "conjoint" in p:
        c = p["conjoint"]
        print("  CONJOINT (predictive accuracy vs known ranking)")
        print(f"    Spearman rank correlation : {c['spearman']}")
        print(f"    Top-1 profile correct     : {c['top1_accuracy']}")
        print(f"    Avg top-3 overlap (of 3)  : {c['avg_top3_overlap']}\n")

    if "comparison" in p:
        c = p["comparison"]
        print("  COMPARISON")
        print(f"    Winner accuracy           : {c.get('winner_accuracy')}")
        print(f"    Share MAE                 : {c.get('share_mae')}")
        if "optimism_bias" in c:
            print(f"    Optimism bias (new opt.)  : {c['optimism_bias']:+}")
        if "ignorance_overconfidence" in c:
            print(f"    Overconfidence (blind)    : {c['ignorance_overconfidence']}\n")

    print("  TRUST GRADES:", p.get("trust"))
    print("  RECOMMENDED CORRECTIONS:", p.get("recommended_corrections"))
    print("\n  NOTES:")
    for n in p.get("notes", []):
        print(f"    - {n}")
    print("=" * 64 + "\n")


def main():
    ap = argparse.ArgumentParser(description="Calibrate the synthetic engine.")
    ap.add_argument("--provider", choices=["openai", "anthropic", "gemini"],
                    required=True)
    ap.add_argument("--key", default=None,
                    help="API key (or set OPENAI/ANTHROPIC/GOOGLE_API_KEY env).")
    args = ap.parse_args()

    key = args.key or os.environ.get(
        {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
         "gemini": "GOOGLE_API_KEY"}[args.provider])
    if not key:
        print("No API key provided (use --key or the env var).", file=sys.stderr)
        sys.exit(1)

    print(f"Calibrating synthetic engine with {args.provider} ...")
    caller = _resolve_caller(args.provider, key)
    profile = run_calibration(caller, key)
    _print_report(profile)


if __name__ == "__main__":
    main()
