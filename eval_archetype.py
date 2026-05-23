"""
Evaluation harness for the archetype detector.

Runs detect_archetype against:
  (a) a synthetic fixture suite with hand-labeled archetypes, and
  (b) any real labeled CSVs the user drops in ./fixtures/<archetype>/*.csv

Outputs per-archetype precision/recall, a confusion matrix, and the list of
miss-classified cases so the heuristic can be tuned with a real signal.

Usage:
    python eval_archetype.py                # synthetic only
    python eval_archetype.py --fixtures     # synthetic + ./fixtures/
    python eval_archetype.py --json out.json # machine-readable output

Drop real datasets in fixtures/<archetype>/, e.g.:
    fixtures/orders/online_retail_ii.csv      # UCI Online Retail
    fixtures/subscriptions/telco_churn.csv    # IBM Telco Churn
    fixtures/marketing/marketing_campaign.csv # Kaggle marketing campaign
    fixtures/sessions/ga_sample.csv           # Google Analytics sample
    fixtures/reviews/amazon_reviews.csv       # HF amazon_reviews sample
    fixtures/catalog/products.csv             # any product master
    fixtures/customers/customers.csv          # any customer master
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import pandas as pd

from archetypes import detect_archetype, ARCHETYPES


# -----------------------------------------------------------------------------
# Synthetic fixtures — small, deterministic, hand-labeled
# -----------------------------------------------------------------------------
@dataclass
class Fixture:
    name: str
    expected: str
    df: pd.DataFrame


def _synthetic_fixtures(seed: int = 7) -> list[Fixture]:
    rng = np.random.default_rng(seed)
    out: list[Fixture] = []

    # --- orders: classic e-commerce transactions -----------------------------
    n = 400
    out.append(Fixture("orders_basic", "orders", pd.DataFrame({
        "order_id":   [f"O{i}" for i in range(n)],
        "customer_id": rng.integers(1, 80, n),
        "sku":         rng.choice(["A", "B", "C", "D"], n),
        "qty":         rng.integers(1, 4, n),
        "unit_price":  rng.uniform(20, 100, n).round(2),
        "discount":    rng.uniform(0, 0.3, n).round(3),
        "order_date":  pd.date_range("2024-01-01", periods=n, freq="6h"),
    })))

    # --- orders without explicit order_id (header-level revenue) ------------
    out.append(Fixture("orders_no_order_id", "orders", pd.DataFrame({
        "customer_id": rng.integers(1, 50, 200),
        "transaction_id": [f"TX{i}" for i in range(200)],
        "items_count": rng.integers(1, 5, 200),
        "total_amount": rng.uniform(20, 300, 200).round(2),
        "date": pd.date_range("2024-06-01", periods=200, freq="D"),
    })))

    # --- orders with returns + decoration jargon (apparel) ------------------
    n = 300
    out.append(Fixture("orders_apparel_returns", "orders", pd.DataFrame({
        "order_no":      [f"AO{i}" for i in range(n)],
        "customer_email": [f"u{i%80}@brand.co" for i in range(n)],
        "product_id":    rng.choice(["TEE-001","TEE-002","HOOD-A","HOOD-B"], n),
        "quantity":      rng.integers(1, 3, n),
        "list_price":    rng.uniform(15, 90, n).round(2),
        "is_returned":   rng.choice(["Y", "N", "N", "N"], n),
        "return_reason": rng.choice(["fit","color","damage","other",""], n),
        "created_at":    pd.date_range("2024-03-01", periods=n, freq="3h"),
    })))

    # --- customers: one row per person ---------------------------------------
    n = 250
    out.append(Fixture("customers_master", "customers", pd.DataFrame({
        "customer_id":   range(1, n + 1),
        "email":         [f"user{i}@x.com" for i in range(n)],
        "signup_date":   pd.date_range("2023-01-01", periods=n, freq="D"),
        "first_order_date": pd.date_range("2023-02-01", periods=n, freq="D"),
        "country":       rng.choice(["US","CA","UK","DE","FR"], n),
        "channel":       rng.choice(["organic","paid_social","referral"], n),
    })))

    # --- marketing campaigns -------------------------------------------------
    n = 180
    out.append(Fixture("marketing_campaigns", "marketing", pd.DataFrame({
        "date":          pd.date_range("2024-01-01", periods=n, freq="D"),
        "channel":       rng.choice(["paid_social","google_ads","email","tiktok"], n),
        "campaign_name": [f"camp_{i%30}" for i in range(n)],
        "spend":         rng.uniform(100, 2000, n).round(2),
        "impressions":   rng.integers(1000, 80000, n),
        "clicks":        rng.integers(50, 3000, n),
        "conversions":   rng.integers(2, 120, n),
        "revenue":       rng.uniform(200, 7000, n).round(2),
    })))

    # --- marketing minimal (spend + clicks only) ----------------------------
    n = 60
    out.append(Fixture("marketing_minimal", "marketing", pd.DataFrame({
        "utm_source":  rng.choice(["facebook","google","newsletter"], n),
        "utm_campaign":[f"q1_{i}" for i in range(n)],
        "ad_spend":    rng.uniform(50, 500, n),
        "clicks":      rng.integers(10, 800, n),
    })))

    # --- web/app sessions ---------------------------------------------------
    n = 500
    out.append(Fixture("sessions_events", "sessions", pd.DataFrame({
        "session_id": [f"s{i//4}" for i in range(n)],
        "event_name": rng.choice(["page_view","click","add_to_cart","purchase"], n),
        "page_url":   rng.choice(["/home","/p/123","/cart","/checkout"], n),
        "user_id":    rng.integers(1, 200, n),
        "timestamp":  pd.date_range("2024-04-01", periods=n, freq="2min"),
    })))

    # --- subscriptions ------------------------------------------------------
    n = 220
    out.append(Fixture("subscriptions_status", "subscriptions", pd.DataFrame({
        "customer_id":  range(1, n + 1),
        "plan":         rng.choice(["basic","pro","premium"], n),
        "status":       rng.choice(["active","canceled","paused","trialing","past_due"], n),
        "mrr":          rng.uniform(9, 99, n).round(2),
        "started_at":   pd.date_range("2023-01-01", periods=n, freq="D"),
        "canceled_at":  pd.date_range("2024-01-01", periods=n, freq="D"),
    })))

    # --- reviews / NPS ------------------------------------------------------
    n = 150
    out.append(Fixture("reviews_with_text", "reviews", pd.DataFrame({
        "product_id":  rng.choice(["P1","P2","P3"], n),
        "rating":      rng.integers(1, 6, n),
        "review_text": ["I really loved this product but it ran a bit small for me, would buy again in a larger size next time around"] * n,
        "submitted":   pd.date_range("2024-01-01", periods=n, freq="D"),
        "verified_purchase": rng.choice([True, False], n),
    })))

    # --- catalog ------------------------------------------------------------
    n = 120
    out.append(Fixture("catalog_skus", "catalog", pd.DataFrame({
        "sku":          [f"SKU-{i:04d}" for i in range(n)],
        "product_name": [f"Product {i}" for i in range(n)],
        "category":     rng.choice(["shirts","pants","shoes","accessories"], n),
        "unit_price":   rng.uniform(20, 200, n).round(2),
        "cost":         rng.uniform(8, 80, n).round(2),
        "stock":        rng.integers(0, 500, n),
    })))

    # --- mobile app catalog (App Store / Play Store shape) -----------------
    n = 200
    out.append(Fixture("apps_catalog", "apps", pd.DataFrame({
        "App_Id":               [f"com.example.app{i}" for i in range(n)],
        "App_Name":             [f"App {i}" for i in range(n)],
        "Primary_Genre":        rng.choice(["Games","Education","Productivity",
                                            "Lifestyle","Utilities","Health & Fitness"], n),
        "Content_Rating":       rng.choice(["4+","9+","12+","17+"], n),
        "Size_Bytes":           rng.integers(5_000_000, 500_000_000, n),
        "Required_IOS_Version": rng.choice(["12.0","13.0","14.0","15.0"], n),
        "Released":             pd.date_range("2018-01-01", periods=n, freq="3D"),
        "Price":                np.where(rng.random(n) > 0.9,
                                          rng.uniform(0.99, 9.99, n).round(2), 0.0),
        "Free":                 rng.random(n) > 0.1,
        "Average_User_Rating":  rng.choice([0.0, 4.0, 4.5, 5.0], n,
                                            p=[0.5, 0.2, 0.2, 0.1]),
        "Reviews":              rng.integers(0, 5000, n),
        "Developer":            rng.choice([f"Dev {i}" for i in range(50)], n),
    })))

    return out


# -----------------------------------------------------------------------------
# Real fixtures loader
# -----------------------------------------------------------------------------
def _real_fixtures(root: str = "fixtures") -> list[Fixture]:
    """Load any CSVs found under fixtures/<archetype>/*.csv."""
    out: list[Fixture] = []
    if not os.path.isdir(root):
        return out
    for arch in ARCHETYPES:
        d = os.path.join(root, arch)
        if not os.path.isdir(d):
            continue
        for path in sorted(glob.glob(os.path.join(d, "*.csv"))):
            try:
                # Cheap read — first 5000 rows is enough for archetype detection.
                df = pd.read_csv(path, nrows=5000, encoding="utf-8",
                                 on_bad_lines="skip", low_memory=False)
                out.append(Fixture(
                    name=f"{arch}/{os.path.basename(path)}",
                    expected=arch,
                    df=df,
                ))
            except Exception as e:
                print(f"  ! skipped {path}: {e}", file=sys.stderr)
    return out


# -----------------------------------------------------------------------------
# Scoring
# -----------------------------------------------------------------------------
def evaluate(fixtures: list[Fixture]) -> dict:
    results: list[dict] = []
    for fx in fixtures:
        m = detect_archetype(fx.df)
        results.append({
            "name": fx.name,
            "expected": fx.expected,
            "predicted": m.name,
            "confidence": m.confidence,
            "correct": m.name == fx.expected,
            "signals": m.signals[:5],
        })

    # Per-archetype precision / recall.
    by_label = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0, "support": 0})
    for r in results:
        by_label[r["expected"]]["support"] += 1
        if r["predicted"] == r["expected"]:
            by_label[r["expected"]]["tp"] += 1
        else:
            by_label[r["expected"]]["fn"] += 1
            by_label[r["predicted"]]["fp"] += 1

    per_class = {}
    for arch, d in by_label.items():
        tp, fp, fn = d["tp"], d["fp"], d["fn"]
        precision = tp / (tp + fp) if (tp + fp) else None
        recall    = tp / (tp + fn) if (tp + fn) else None
        per_class[arch] = {
            "support":   d["support"],
            "precision": round(precision, 3) if precision is not None else None,
            "recall":    round(recall, 3) if recall is not None else None,
            "tp": tp, "fp": fp, "fn": fn,
        }

    n = len(results)
    accuracy = sum(1 for r in results if r["correct"]) / n if n else 0.0

    # Confusion matrix.
    labels = sorted(ARCHETYPES)
    conf: dict[str, dict[str, int]] = {a: {b: 0 for b in labels + ["generic"]}
                                       for a in labels + ["generic"]}
    for r in results:
        # Guard against unknown predicted labels.
        pred = r["predicted"] if r["predicted"] in conf[r["expected"]] else "generic"
        conf[r["expected"]][pred] += 1

    return {
        "n_cases":   n,
        "accuracy":  round(accuracy, 3),
        "per_class": per_class,
        "confusion": conf,
        "cases":     results,
    }


def _print_report(report: dict) -> None:
    print(f"\n=== Archetype Detector Evaluation ===")
    print(f"Cases: {report['n_cases']}  Accuracy: {report['accuracy']*100:.1f}%\n")

    print(f"{'Archetype':<16}{'Support':>8}{'Prec':>8}{'Recall':>8}"
          f"{'TP':>5}{'FP':>5}{'FN':>5}")
    print("-" * 55)
    for arch in sorted(report["per_class"].keys()):
        m = report["per_class"][arch]
        p = f"{m['precision']*100:.0f}%" if m['precision'] is not None else "—"
        r = f"{m['recall']*100:.0f}%"    if m['recall']    is not None else "—"
        print(f"{arch:<16}{m['support']:>8}{p:>8}{r:>8}"
              f"{m['tp']:>5}{m['fp']:>5}{m['fn']:>5}")

    print("\nConfusion matrix (rows = expected, cols = predicted):")
    labels = sorted(report["confusion"].keys())
    print("            " + "".join(f"{c[:5]:>6}" for c in labels))
    for r in labels:
        row = report["confusion"][r]
        cells = "".join(f"{row[c]:>6}" for c in labels)
        print(f"{r:<12}{cells}")

    misses = [c for c in report["cases"] if not c["correct"]]
    if misses:
        print(f"\nMisclassified ({len(misses)}):")
        for c in misses:
            print(f"  {c['name']:<40}  expected={c['expected']:<12} "
                  f"got={c['predicted']:<12} (conf={c['confidence']})")
            for s in c["signals"][:3]:
                print(f"      ↳ {s}")


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", action="store_true",
                    help="Also load real CSVs from ./fixtures/<archetype>/")
    ap.add_argument("--json", type=str, default=None,
                    help="Write machine-readable JSON report here.")
    args = ap.parse_args()

    fixtures = _synthetic_fixtures()
    print(f"Loaded {len(fixtures)} synthetic fixtures.")
    if args.fixtures:
        real = _real_fixtures()
        print(f"Loaded {len(real)} real fixtures from ./fixtures/")
        fixtures.extend(real)

    report = evaluate(fixtures)
    _print_report(report)

    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\nWrote {args.json}")

    # Exit code 1 if accuracy < 90% on synthetic-only, so CI can fail.
    if not args.fixtures and report["accuracy"] < 0.90:
        sys.exit(1)


if __name__ == "__main__":
    main()
