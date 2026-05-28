# Naju

**An AI marketing analyst that turns any B2C dataset into a senior-analyst-grade dashboard, then runs synthetic market research on the same data — A/B tests, conjoint, pricing studies, Van Westendorp PSM — without recruiting a single respondent.**

Live demo: https://lumen-analytics-wo77.onrender.com

---

## The problem

Mid-market B2C teams need two things that a generic BI tool cannot give them:

1. **A senior analyst's interpretation of their data**, not a chart wall. RFM segments, cohort retention, ROAS by channel, MRR waterfalls — applied automatically to whatever shape of data was uploaded.
2. **Cheap research priors** before they spend $5–25k on a real survey, conjoint panel, or A/B test. "Should we charge $69 or $89?" is currently answered with vibes; Naju answers it with a synthetic study grounded in the company's own measured customer behaviour.

The pitch isn't "another AI dashboard." It's **a workflow that compresses what used to be a marketer + data analyst + research vendor into a single CSV upload**, and is honest about which of its answers are measurements vs. priors.

## How it works — the four ideas

### 1. B2C archetype detection (deterministic)
Every dataset is classified as **orders / customers / marketing / sessions / subscriptions / reviews / catalog / apps / generic** by column-name and shape heuristics in [`archetypes.py`](archetypes.py). A "user override" dropdown handles edge cases. This drives everything downstream — instead of a generic chart pass, the right analyst playbook runs.

- Validated on 9 real public B2C datasets (`fixtures/`). Current detector accuracy: **100% archetype recall** on the eval set, see `eval_archetype.py`.

### 2. Senior-analyst playbooks (deterministic)
[`playbooks.py`](playbooks.py) maps each archetype to the analysis a senior analyst would actually run:

| Archetype | Playbook output |
|-----------|-----------------|
| Orders | RFM segments, cohort retention heatmap, AOV by channel, return-rate funnel |
| Marketing | ROAS / CAC / LTV by channel, attribution overlap |
| Subscriptions | MRR waterfall, gross / net churn, cohort revenue |
| Sessions | Funnel drop-off, source quality, page-flow |
| Reviews | Sentiment distribution, low-rating SKU clusters |
| Catalog | Margin × velocity matrix, stockout-risk long tail |
| Apps | Install→retention curves, top crash signals |

The AI **never** generates these numbers — they're computed in pandas, then the AI is asked to *narrate* them.

### 3. Synthetic research with digital-twin personas
Generic synthetic-research tools (Synthetic Users, etc.) ask an LLM to role-play stereotypes. Naju does the opposite. [`personas.py`](personas.py) builds **digital twins of the actual customer segments measured in the uploaded data** — each persona carries that segment's real AOV, price band, discount behaviour, repeat rate, category mix. The LLM is asked to reason **as that measured segment**, not as an invented persona.

Five study types supported ([`synthetic_research.py`](synthetic_research.py)):

- **Pricing study** — purchase probability across a price grid, sanity-checked against the revealed-preference demand curve fitted from real sales.
- **Concept test** — purchase intent for a new product / feature.
- **Comparison** — preference share across 2–4 options (brands, messages, products).
- **Conjoint** — rank multi-attribute profiles, derive part-worths.
- **Van Westendorp PSM** — acceptable price range from the four classical questions.

One LLM call **per segment**, not per individual. The model produces a signal; aggregation, weighting, and confidence grading happen in deterministic Python.

### 4. RAG + calibration + Bayesian validation — the honesty layer
- **RAG grounding** ([`evidence.py`](evidence.py), [`rag.py`](rag.py)): every persona's prompt is fed actual retrieved rows from its segment — semantic embeddings via OpenAI when available, keyword fallback otherwise. This was added after a calibration finding that ungrounded personas were biased by flattering product *names* (LLMs ranked "Red Delicious" top; real shoppers rank it last).
- **Calibration harness** ([`calibration.py`](calibration.py)): the synthetic engine is backtested against real public DCE / pricing data. Results are written to `calibration_profile.json` and surfaced in the UI as a trust grade per study type. **Confidence is capped at the measured grade — the engine can never claim more confidence than the backtests support.**
- **Bayesian human-in-the-loop validation** ([`validation.py`](validation.py)): users can enter a small real-survey result (n, k). The synthetic estimate becomes the prior, the survey becomes the data, and a conjugate Beta update returns a corrected posterior with a 95% CI — the cleanest possible bridge between synthetic and measured.

## What's measured, not claimed

| Claim | Where it's measured |
|---|---|
| Archetype detector works on real B2C data | `eval_archetype.py` against `fixtures/` |
| Synthetic pricing tracks real demand | `calibration.py` against public DCE datasets, output in `calibration_profile.json` |
| RAG grounding beats ungrounded role-play | `calibration_colab.ipynb` — name-bias eliminated when retrieved records are forced into context |
| Synthetic confidence claims are honest | `_attach_calibration()` caps every result at the backtested trust grade |

## Quickstart

### Try the deployed version
1. Open https://lumen-analytics-wo77.onrender.com
2. Paste an OpenAI key (`sk-...`), Anthropic key (`sk-ant-...`), or Google key (`AIza...`). Auto-detected, never logged, server-session only.
3. Upload a CSV (up to 50 MB).
4. Either **Generate dashboard** (full archetype playbook + AI narrative + charts) or **Skip to synthetic data** (cleans + detects archetype only, jumps straight to research panel).

### Run it locally
```bash
pip install -r requirements.txt
python app.py
# open http://localhost:5000
```

### Deploy
The repo is wired for Render (free tier).
- [`render.yaml`](render.yaml) — service definition, `FLASK_SECRET_KEY` auto-generated
- [`Procfile`](Procfile) — gunicorn, single worker, 8 threads, 300s timeout
- [`requirements.txt`](requirements.txt) — flask, pandas, plotly, openai, anthropic, google-generativeai, openpyxl, reportlab, scipy

A push to `main` triggers an autodeploy.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `OPENAI_NARRATIVE_MODEL` | `gpt-5-mini` | Swap models without redeploying. Works with any OpenAI model that supports JSON mode (`gpt-5`, `gpt-5-mini`, `gpt-4.1-mini`, `gpt-4o`, `o3`, etc.). |
| `FLASK_SECRET_KEY` | random per restart | Set a stable value to keep sessions alive across worker restarts. `render.yaml` generates one automatically. |
| `PORT` | injected by Render / 5000 locally | Web server port |

## Architecture

```
templates/index.html      single-page UI
static/js/app.js          upload, Plotly rendering, chat, synthetic panel, exports
static/css/style.css      warm editorial palette (paper, emerald accent, Inter + Instrument Serif)

app.py                    Flask routes, cleaning, AI dispatch, exports
  ├─ archetypes.py        B2C archetype classifier (deterministic)
  ├─ playbooks.py         per-archetype senior-analyst pipelines
  ├─ personas.py          digital-twin segment profiles + demand curve
  ├─ synthetic_research.py  5 study engines (pricing / concept / comparison / conjoint / VW)
  ├─ evidence.py          RAG evidence layer over real rows
  ├─ rag.py               embeddings + cosine retrieval (+ keyword fallback)
  ├─ validation.py        Bayesian prior + survey update
  └─ calibration.py       backtest harness → calibration_profile.json
```

## Stability + UX details that matter

- **Session recovery** — Render's free-tier worker recycles between requests. Both the dashboard and the synthetic flow silently re-upload + re-prep server state on a state-loss error, so users never see "Run an analysis first" mid-session.
- **Parallel AI + local pipeline** — the AI narrative call runs concurrently with the deterministic chart / correlation / outlier work. Wall time = max(AI, local), not sum.
- **Hardened cleaning** — the fuzzy categorical canonicalizer is capped at columns with ≤200 unique values, after the original O(unique²) sweep was timing out on 100K-row CSVs with high-card product-name columns.
- **Strict button gating** — Generate / Skip stay disabled until both API key is connected *and* file upload finishes. Editing the key after Connect re-invalidates it.

## Limitations / honest disclosure

- **Single-worker in-memory store** — fine for the free-tier demo, not for multi-instance. A Redis or RDS-backed `STORE` is the right next step before any serious traffic.
- **Synthetic ≠ measurement** — every synthetic result ships with a confidence grade and caveats. Pricing studies outside the observed price band are flagged as extrapolation; comparison studies tie below ~10 points. The Bayesian validator exists specifically to fuse synthetic priors with small real surveys.
- **PDF export** captures Plotly charts client-side via `Plotly.toImage`. If a chart fails to rasterize, that page is skipped rather than aborting the whole PDF.
- **The "apps" archetype playbook** is the youngest; calibration on app-store data is still thin.

## Security

API keys live only in the browser's JS memory and the server's per-session dict. Nothing is written to disk, nothing is logged, nothing leaves the worker process. Frontend inputs are HTML-escaped before insertion.
