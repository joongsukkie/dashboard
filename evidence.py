"""
RAG evidence layer for synthetic research.

The synthetic engine's weak point: a persona is described to the LLM by a
handful of aggregate statistics ("average order value $X, 26% discount").
The LLM never sees a real customer — exactly the "missing human data"
problem. This module fixes it with retrieval-augmented generation.

The canonical RAG pattern, applied:

  INDEX     every (sampled) real data row becomes a natural-language
            "record document" and is embedded into a vector store.
  RETRIEVE  the research question is the query; we pull the top-k real
            records per segment (metadata-filtered).
  GENERATE  the retrieved real records are injected into each digital-twin
            persona prompt, so the LLM reasons from real evidence.

Embedding + cosine retrieval reuse rag.py. If no OpenAI key is available
(embeddings need one), we fall back to keyword retrieval — still real
records, still relevant, just matched lexically instead of semantically.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

import rag


# -----------------------------------------------------------------------------
# Row → record document
# -----------------------------------------------------------------------------
_GEO_NAMES = ("region", "country", "state", "city", "market", "location")


def _find_geo_column(df: pd.DataFrame) -> str | None:
    for c in df.columns:
        if str(c).strip().lower() in _GEO_NAMES and df[c].dtype == object:
            return c
    return None


def row_to_document(row: pd.Series, roles: dict, geo_col: str | None) -> str:
    """Turn one real data row into a compact natural-language record — the
    'document' the embedding model indexes and the LLM later reads."""
    who = f"A customer in {row[geo_col]}" if (geo_col and pd.notna(row.get(geo_col))) \
        else "A customer"

    # What was bought.
    product = None
    for rk in ("product_name", "category", "sku"):
        c = roles.get(rk)
        if c and c in row.index and pd.notna(row.get(c)):
            product = str(row[c])
            break

    qty_col = roles.get("qty")
    qty = None
    if qty_col and qty_col in row.index and pd.notna(row.get(qty_col)):
        try:
            qty = int(float(row[qty_col]))
        except (TypeError, ValueError):
            qty = None

    # Effective price paid.
    price = None
    amt, q = roles.get("amount"), roles.get("qty")
    if amt and q and amt in row.index and q in row.index:
        try:
            if pd.notna(row[amt]) and pd.notna(row[q]) and float(row[q]) > 0:
                price = float(row[amt]) / float(row[q])
        except (TypeError, ValueError, ZeroDivisionError):
            price = None
    if price is None:
        up = roles.get("unit_price")
        if up and up in row.index and pd.notna(row.get(up)):
            try:
                price = float(row[up])
            except (TypeError, ValueError):
                price = None

    # Discount.
    disc_txt = ""
    dc = roles.get("discount")
    if dc and dc in row.index and pd.notna(row.get(dc)):
        try:
            d = float(row[dc])
            d = d / 100.0 if d > 1.5 else d
            if d > 0:
                disc_txt = f", with a {d*100:.0f}% discount"
        except (TypeError, ValueError):
            pass

    # Rating / sentiment, if present.
    rating_txt = ""
    rc = roles.get("rating")
    if rc and rc in row.index and pd.notna(row.get(rc)):
        rating_txt = f", and rated it {row[rc]}"

    bought = "purchased"
    qtytxt = f" {qty} unit(s) of" if qty else ""
    prodtxt = f" {product}" if product else " an item"
    pricetxt = f" at about ${price:,.0f} each" if price else ""

    return (f"{who} {bought}{qtytxt}{prodtxt}{pricetxt}{disc_txt}{rating_txt}."
            ).replace("  ", " ").strip()


# -----------------------------------------------------------------------------
# Index building
# -----------------------------------------------------------------------------
def build_evidence_index(df: pd.DataFrame, roles: dict,
                         segment_col: str | None,
                         segment_labels: pd.Series | None,
                         openai_key: str | None = None,
                         max_records: int = 1200) -> dict:
    """Convert real rows into record documents and index them.

    If an OpenAI key is supplied the documents are embedded (true semantic
    RAG via rag.py). Otherwise we keep them raw for keyword retrieval.
    Returns an index dict carrying an 'embedded' flag.
    """
    if df is None or len(df) == 0:
        return {"embedded": False, "chunks": [], "note": "empty dataset"}

    geo_col = _find_geo_column(df)

    work = df.copy()
    if segment_labels is not None:
        work = work.assign(_seg=segment_labels.values)
    else:
        work = work.assign(_seg="All customers")

    # Stratified sample so every segment keeps a usable retrieval pool.
    if len(work) > max_records:
        frames = []
        for seg, grp in work.groupby("_seg"):
            take = max(60, int(max_records * len(grp) / len(work)))
            frames.append(grp.sample(min(take, len(grp)), random_state=0))
        work = pd.concat(frames).reset_index(drop=True)

    chunks: list[dict] = []
    for i, row in work.iterrows():
        doc = row_to_document(row, roles, geo_col)
        if not doc or len(doc) < 12:
            continue
        seg = str(row["_seg"])
        seg_name = f"{segment_col}: {seg}" if segment_col else seg
        chunks.append({
            "id": f"rec_{i}",
            "text": doc,
            "segment": seg_name,
            "source_type": "transaction",
        })

    if not chunks:
        return {"embedded": False, "chunks": [], "note": "no usable records"}

    if openai_key:
        try:
            index = rag.build_index(chunks, openai_api_key=openai_key)
            index["embedded"] = True
            index["n_records"] = len(chunks)
            return index
        except Exception as e:
            # Embedding failed (bad key, rate limit) — degrade gracefully.
            return {"embedded": False, "chunks": chunks, "n_records": len(chunks),
                    "note": f"embedding failed, keyword fallback: {str(e)[:120]}"}

    return {"embedded": False, "chunks": chunks, "n_records": len(chunks),
            "note": "keyword retrieval (no OpenAI key for embeddings)"}


# -----------------------------------------------------------------------------
# Retrieval
# -----------------------------------------------------------------------------
_STOP = {"a", "an", "the", "of", "for", "to", "in", "on", "at", "and", "or",
         "new", "our", "is", "are", "would", "with", "this", "that", "be"}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", str(text).lower())
            if w not in _STOP and len(w) > 2}


def _keyword_retrieve(chunks: list[dict], query: str, segment: str,
                      k: int) -> list[dict]:
    """Lexical fallback: score segment records by query-token overlap;
    if nothing overlaps, return a diverse sample."""
    pool = [c for c in chunks if c.get("segment") == segment] or chunks
    q = _tokens(query)
    scored = []
    for c in pool:
        overlap = len(q & _tokens(c["text"]))
        scored.append((overlap, c))
    scored.sort(key=lambda x: -x[0])
    if scored and scored[0][0] > 0:
        return [c for _, c in scored[:k]]
    # No lexical match — spread the sample across the pool.
    step = max(1, len(pool) // k)
    return pool[::step][:k]


def retrieve_evidence(index: dict, query: str, segment: str,
                      openai_key: str | None = None, k: int = 6) -> list[dict]:
    """Retrieve the top-k real records for a segment, relevant to the query."""
    if not index or not index.get("chunks") and not index.get("embeddings") is not None:
        pass
    chunks = index.get("chunks", [])
    if not len(chunks):
        return []

    if index.get("embedded") and openai_key:
        try:
            hits = rag.retrieve(index, query, openai_api_key=openai_key, k=k,
                                filters={"segment": ("eq", segment)})
            if hits:
                return hits
        except Exception:
            pass  # fall through to keyword

    return _keyword_retrieve(chunks, query, segment, k)


# -----------------------------------------------------------------------------
# Prompt formatting
# -----------------------------------------------------------------------------
def format_evidence(records: list[dict], max_records: int = 6) -> str:
    """Format retrieved real records for injection into a persona prompt."""
    if not records:
        return ""
    lines = ["Real customer records retrieved from THIS segment, relevant to "
             "the question (treat these as concrete ground truth — your answer "
             "must be consistent with them):"]
    for i, r in enumerate(records[:max_records], 1):
        lines.append(f"  {i}. {r.get('text', '')}")
    return "\n".join(lines)
