"""
Synthetic Research — Phase 6: human-in-the-loop validation.

The market-research brief's final prescription: "LLMs as informative
priors — validate with small survey samples (Prediction-Powered
Inference, Bayesian methods)." This module does exactly that.

  PRIOR     the synthetic engine's estimate. Its width is NOT guessed —
            it comes from the calibration harness: how far synthetic
            estimates landed from truth on the real-data backtests IS
            the prior's uncertainty. A worse-calibrated engine yields a
            wider, weaker prior.
  DATA      a small real survey the user ran (n respondents, k chose the
            focal option) — unbiased but high-variance.
  POSTERIOR a conjugate Normal-Normal update fuses them: an estimate that
            is close to unbiased (like the survey) but lower-variance
            (because the prior adds information) — with a real 95% CI.

Pure statistics — no LLM calls — so it is fully deterministic and tested.
"""
from __future__ import annotations

import math

# E|X| = sigma * sqrt(2/pi) for a normal — converts a mean-absolute-error
# (what the calibration harness reports) into a standard deviation.
_MAE_TO_SD = 0.7978845608


# -----------------------------------------------------------------------------
# Core statistics
# -----------------------------------------------------------------------------
def agresti_coull(k: int, n: int) -> tuple[float, float]:
    """Robust proportion estimate + standard error for SMALL samples.

    Adds 2 pseudo-successes and 2 pseudo-failures (Agresti-Coull). This
    avoids a zero SE at k=0 or k=n — important because real validation
    surveys are small (the brief says "small survey samples").
    """
    if n <= 0:
        return 0.5, 0.5
    n_adj = n + 4
    p = (k + 2) / n_adj
    se = math.sqrt(max(p * (1.0 - p), 1e-9) / n_adj)
    return p, se


def normal_update(prior_mean: float, prior_sd: float,
                  data_mean: float, data_sd: float) -> tuple[float, float]:
    """Conjugate Normal-Normal posterior. Returns (posterior_mean,
    posterior_sd). Precision-weighted: the more precise source pulls more."""
    prior_sd = max(prior_sd, 1e-6)
    data_sd = max(data_sd, 1e-6)
    tau0 = 1.0 / prior_sd ** 2          # prior precision
    taud = 1.0 / data_sd ** 2           # data precision
    post_var = 1.0 / (tau0 + taud)
    post_mean = post_var * (tau0 * prior_mean + taud * data_mean)
    return post_mean, math.sqrt(post_var)


# -----------------------------------------------------------------------------
# Survey parsing
# -----------------------------------------------------------------------------
def proportion_from_survey(df, focal_value: str) -> tuple[int, int]:
    """Count (k, n) from a real-survey dataframe: how many of n respondents
    chose `focal_value`. Picks the most likely choice column — the one
    whose values most overlap the focal value / look like choices."""
    import pandas as pd

    if df is None or len(df) == 0:
        return 0, 0
    focal = str(focal_value).strip().lower()

    best_col, best_hits = None, -1
    for c in df.columns:
        col = df[c].dropna().astype(str).str.strip().str.lower()
        if col.empty:
            continue
        hits = int((col == focal).sum())
        # Prefer a column that actually contains the focal value; tie-break
        # toward low-cardinality (looks like a choice field).
        if hits > best_hits:
            best_hits, best_col = hits, c

    if best_col is None:
        return 0, 0
    col = df[best_col].dropna().astype(str).str.strip().str.lower()
    n = int(len(col))
    k = int((col == focal).sum())
    return k, n


# -----------------------------------------------------------------------------
# The validation
# -----------------------------------------------------------------------------
def validate_proportion(synthetic_share: float, k: int, n: int,
                        calibration_mae: float | None = None,
                        focal_label: str = "the option") -> dict:
    """Fuse a synthetic share estimate (prior) with a real survey (k of n
    chose the focal option) into a Bayesian posterior + 95% CI."""
    if n is None or n <= 0:
        return {"ok": False, "error": "Real survey needs at least 1 respondent."}

    # --- Prior width from the calibration harness -----------------------------
    # The calibration measured how far synthetic shares land from truth
    # (comparison share MAE). Convert that MAE to an SD. Floor at 0.08 so
    # the prior stays appropriately humble for a brand-new question the
    # engine was never backtested on — a real survey should be able to move it.
    if calibration_mae and calibration_mae > 0:
        prior_sd = max(calibration_mae / _MAE_TO_SD, 0.08)
        prior_src = (f"calibration backtests (share MAE "
                     f"{calibration_mae:.3f})")
    else:
        prior_sd = 0.12
        prior_src = "uncalibrated default"

    syn = max(0.0, min(1.0, float(synthetic_share)))
    p_data, se_data = agresti_coull(int(k), int(n))

    post_mean, post_sd = normal_update(syn, prior_sd, p_data, se_data)
    ci_lo = max(0.0, post_mean - 1.96 * post_sd)
    ci_hi = min(1.0, post_mean + 1.96 * post_sd)

    # Disagreement: how many SDs apart are the prior and the survey?
    spread = math.sqrt(prior_sd ** 2 + se_data ** 2)
    z = (p_data - syn) / spread if spread > 0 else 0.0

    if abs(z) < 1.0:
        verdict = "consistent"
        interp = (f"The real survey ({k}/{n}) agrees with the synthetic "
                  f"estimate — they are {abs(z):.1f} SD apart. The synthetic "
                  f"prior is supported.")
    elif abs(z) < 2.0:
        verdict = "minor disagreement"
        interp = (f"The real survey is {abs(z):.1f} SD from the synthetic "
                  f"estimate — a mild gap. The posterior splits the "
                  f"difference; collect more responses to sharpen it.")
    else:
        verdict = "contradicted"
        interp = (f"The real survey is {abs(z):.1f} SD from the synthetic "
                  f"estimate — a significant contradiction. Trust the survey "
                  f"over the synthetic prior for this question; the engine "
                  f"appears biased here.")

    data_half = 1.96 * se_data
    post_half = 1.96 * post_sd

    return {
        "ok": True,
        "focal_label": focal_label,
        "prior": {
            "estimate": round(syn, 4),
            "sd": round(prior_sd, 4),
            "source": f"synthetic engine — prior width from {prior_src}",
        },
        "survey": {
            "n": int(n),
            "chose_focal": int(k),
            "estimate": round(p_data, 4),
            "se": round(se_data, 4),
            "source": "real survey (Agresti-Coull)",
        },
        "posterior": {
            "estimate": round(post_mean, 4),
            "sd": round(post_sd, 4),
            "ci95": [round(ci_lo, 4), round(ci_hi, 4)],
        },
        "shift_from_synthetic": round(post_mean - syn, 4),
        "z_disagreement": round(z, 2),
        "ci_narrowed": {
            "survey_alone_halfwidth": round(data_half, 4),
            "combined_halfwidth": round(post_half, 4),
            "tightening": (round(1.0 - post_half / data_half, 3)
                           if data_half > 0 else 0.0),
        },
        "verdict": verdict,
        "interpretation": interp,
        "recommendation": (
            f"Best estimate for '{focal_label}': "
            f"{post_mean*100:.0f}% (95% CI {ci_lo*100:.0f}-{ci_hi*100:.0f}%). "
            + (f"Combining the synthetic prior with the survey narrowed the "
               f"95% CI by {(1.0 - post_half/data_half)*100:.0f}% vs the "
               f"survey alone." if data_half > 0 else "")),
    }
