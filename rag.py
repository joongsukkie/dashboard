"""
Lightweight RAG primitives for ReturnLens.

Design choices:
- In-memory cosine-similarity index per session (no extra infra; survives
  inside the same Flask process). On Render free tier the corpus gets
  rebuilt on cold start — acceptable for an MVP demo.
- OpenAI embeddings (text-embedding-3-small) — the openai SDK is already
  installed for the analysis path. One embedding key feeds the whole RAG
  layer regardless of which LLM the user picked for narrative.
- Metadata-first retrieval. We pre-filter by structured metadata (sku,
  rating, source_type, date) BEFORE the cosine pass — this is what makes
  customer-voice retrieval actually useful instead of "vibes-based."
"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime
from typing import Any, Iterable

import numpy as np
import pandas as pd


EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536
EMBED_BATCH = 96


# -----------------------------------------------------------------------------
# Text normalization
# -----------------------------------------------------------------------------
def _clean_text(s: str) -> str:
    """Strip boilerplate, collapse whitespace, drop empty bracket noise."""
    if s is None:
        return ""
    s = str(s)
    # Strip HTML tags conservatively.
    s = re.sub(r"<[^>]+>", " ", s)
    # Drop email signatures of the form "Sent from my iPhone" / "Get Outlook for iOS".
    s = re.sub(r"(?i)\bsent from my [^\n]+", " ", s)
    s = re.sub(r"(?i)\bget outlook for [^\n]+", " ", s)
    # Collapse runs of whitespace.
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalize_rating(v: Any) -> float | None:
    """Accept '5', '5/5', '4.0 stars', '★★★★', etc."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    s = str(v).strip()
    stars = s.count("★") + s.count("⭐")
    if stars:
        return float(stars)
    m = re.search(r"(-?\d+(?:\.\d+)?)", s)
    if not m:
        return None
    try:
        n = float(m.group(1))
        # Heuristic: if the source uses 0-100, squish to 0-5.
        if n > 5 and n <= 100:
            n = n / 20.0
        return max(0.0, min(5.0, n))
    except ValueError:
        return None


def _parse_date(v: Any) -> str | None:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        return pd.to_datetime(v, errors="coerce").date().isoformat()
    except Exception:
        return None


# -----------------------------------------------------------------------------
# Corpus shaping
# -----------------------------------------------------------------------------
def detect_corpus_columns(df: pd.DataFrame) -> dict:
    """Guess which CSV columns map to {text, sku, rating, date, source_type}.

    The user can override; this is just a sensible default surfacing in the UI.
    """
    cols = {c: str(c).lower() for c in df.columns}

    def find(keywords: tuple[str, ...]) -> str | None:
        # Walk keywords in priority order so more specific terms (e.g.
        # "product_name") match before more generic ones (e.g. "product"
        # which would otherwise also match "product_id").
        for kw in keywords:
            for c, cl in cols.items():
                if kw in cl:
                    return c
        return None

    return {
        "text":   find(("review", "comment", "feedback", "body", "text",
                        "message", "reason", "verbatim", "description")),
        "sku":    find(("sku", "product_id", "product id", "product_code",
                        "asin", "item_id", "style")),
        "product_name": find(("product_name", "title", "item_name", "product")),
        "rating": find(("rating", "stars", "score", "nps")),
        "date":   find(("date", "created", "timestamp", "submitted")),
        "source": find(("source", "channel", "type")),
        "verified": find(("verified", "verified_purchase")),
    }


def rows_to_chunks(df: pd.DataFrame, mapping: dict,
                   default_source: str = "review") -> list[dict]:
    """Turn one CSV row → one RAG chunk. Each chunk is one customer utterance."""
    chunks: list[dict] = []
    text_col = mapping.get("text")
    if not text_col or text_col not in df.columns:
        return chunks

    for i, row in df.iterrows():
        text = _clean_text(row.get(text_col, ""))
        if not text or len(text) < 8:
            continue
        meta = {
            "id": f"chunk_{i}",
            "sku": str(row.get(mapping["sku"])).strip() if mapping.get("sku") and mapping["sku"] in df.columns else None,
            "product_name": str(row.get(mapping["product_name"])).strip() if mapping.get("product_name") and mapping["product_name"] in df.columns else None,
            "rating": _normalize_rating(row.get(mapping["rating"])) if mapping.get("rating") and mapping["rating"] in df.columns else None,
            "date": _parse_date(row.get(mapping["date"])) if mapping.get("date") and mapping["date"] in df.columns else None,
            "source_type": str(row.get(mapping["source"])).strip() if mapping.get("source") and mapping["source"] in df.columns else default_source,
            "verified": bool(row.get(mapping["verified"])) if mapping.get("verified") and mapping["verified"] in df.columns else None,
        }
        # Clean nones from metadata for prettier JSON.
        meta = {k: v for k, v in meta.items() if v is not None and v != "nan" and v != "None"}
        meta["text"] = text
        chunks.append(meta)
    return chunks


# -----------------------------------------------------------------------------
# Embedding + index
# -----------------------------------------------------------------------------
def embed_texts(texts: list[str], openai_api_key: str) -> np.ndarray:
    """Batch-embed via OpenAI. Returns float32 array shape (n, EMBED_DIM)."""
    from openai import OpenAI
    client = OpenAI(api_key=openai_api_key)

    out = np.zeros((len(texts), EMBED_DIM), dtype=np.float32)
    for start in range(0, len(texts), EMBED_BATCH):
        batch = texts[start:start + EMBED_BATCH]
        resp = client.embeddings.create(model=EMBED_MODEL, input=batch)
        for j, item in enumerate(resp.data):
            out[start + j] = np.asarray(item.embedding, dtype=np.float32)
    # L2-normalize so cosine == dot product.
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return out / norms


def build_index(chunks: list[dict], openai_api_key: str) -> dict:
    """Embed every chunk and stash everything in a flat dict.

    Returns: {
        "embeddings": (n, dim) float32,
        "chunks":     list[dict] of length n (incl. text + metadata),
        "built_at":   isoformat str,
    }
    """
    if not chunks:
        return {"embeddings": np.zeros((0, EMBED_DIM), dtype=np.float32),
                "chunks": [], "built_at": datetime.utcnow().isoformat()}
    texts = [c["text"] for c in chunks]
    embeddings = embed_texts(texts, openai_api_key)
    return {
        "embeddings": embeddings,
        "chunks": chunks,
        "built_at": datetime.utcnow().isoformat(),
    }


def _matches_filters(chunk: dict, filters: dict) -> bool:
    """Filter is a dict of {field: value | (op, value)}.

    Supported ops: 'eq' (default), 'lte', 'gte', 'in', 'icontains'.
    """
    for key, raw in filters.items():
        if raw is None:
            continue
        op, val = ("eq", raw) if not isinstance(raw, tuple) else raw
        actual = chunk.get(key)
        if actual is None:
            return False
        if op == "eq" and str(actual).lower() != str(val).lower():
            return False
        if op == "lte":
            try:
                if float(actual) > float(val):
                    return False
            except (TypeError, ValueError):
                return False
        if op == "gte":
            try:
                if float(actual) < float(val):
                    return False
            except (TypeError, ValueError):
                return False
        if op == "in" and str(actual).lower() not in [str(x).lower() for x in val]:
            return False
        if op == "icontains" and str(val).lower() not in str(actual).lower():
            return False
    return True


def retrieve(index: dict, query: str, openai_api_key: str,
             k: int = 6, filters: dict | None = None) -> list[dict]:
    """Metadata pre-filter → cosine top-k. Returns a list of chunk dicts
    with a `_score` added."""
    if not index or not len(index.get("chunks", [])):
        return []

    candidates = index["chunks"]
    cand_idx = list(range(len(candidates)))
    if filters:
        cand_idx = [i for i in cand_idx if _matches_filters(candidates[i], filters)]
    if not cand_idx:
        return []

    q_emb = embed_texts([query], openai_api_key)[0]  # (dim,)
    cand_embs = index["embeddings"][cand_idx]  # (m, dim)
    scores = cand_embs @ q_emb  # cosine, since both normalized

    order = np.argsort(-scores)[:k]
    results = []
    for o in order:
        i = cand_idx[int(o)]
        ch = dict(candidates[i])
        ch["_score"] = float(scores[int(o)])
        results.append(ch)
    return results


# -----------------------------------------------------------------------------
# Returns-aware CSV stats
# -----------------------------------------------------------------------------
RETURN_HINTS = ("return", "rma", "refund")


def detect_return_columns(df: pd.DataFrame) -> dict:
    """Identify likely return-related columns in the main dataset."""
    out = {"is_return_flag": None, "return_reason": None,
           "return_date": None, "refund_amount": None}
    for c in df.columns:
        cl = str(c).lower()
        if any(h in cl for h in RETURN_HINTS):
            if "reason" in cl and out["return_reason"] is None:
                out["return_reason"] = c
            elif "date" in cl and out["return_date"] is None:
                out["return_date"] = c
            elif ("amount" in cl or "refund" in cl) and out["refund_amount"] is None:
                out["refund_amount"] = c
            elif ("is" in cl or "flag" in cl or cl in ("return", "returned",
                                                       "is_return", "is_returned")):
                out["is_return_flag"] = c
    return out


def compute_returns_stats(df: pd.DataFrame, sku_col: str | None = None) -> dict:
    """Roll up: overall return rate, per-SKU return rate, top return reasons."""
    rcols = detect_return_columns(df)
    stats: dict = {"detected_columns": rcols}

    flag = rcols["is_return_flag"]
    reason = rcols["return_reason"]
    if flag and flag in df.columns:
        as_bool = df[flag].astype(str).str.strip().str.lower().isin(
            ("1", "true", "yes", "y", "returned", "t"))
        stats["overall_return_rate"] = round(float(as_bool.mean()), 4)
        stats["total_returns"] = int(as_bool.sum())
        if sku_col and sku_col in df.columns:
            g = df.assign(_r=as_bool).groupby(sku_col)["_r"].agg(["sum", "mean", "count"])
            g = g.sort_values("sum", ascending=False).head(20).reset_index()
            stats["per_sku"] = [
                {"sku": str(r[sku_col]),
                 "returns": int(r["sum"]),
                 "orders": int(r["count"]),
                 "return_rate": round(float(r["mean"]), 4)}
                for _, r in g.iterrows()
            ]
    if reason and reason in df.columns:
        vc = df[reason].dropna().astype(str).str.strip().str.lower().value_counts().head(15)
        stats["top_reasons"] = [{"reason": k, "count": int(v)} for k, v in vc.items()]
    return stats
