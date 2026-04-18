# AI Analytics Agent

A Flask-based web app that turns a CSV upload into an interactive marketing analytics dashboard powered by the AI provider of your choice (OpenAI, Anthropic, or Google Gemini).

## Features

- Upload CSV → automated cleaning, AI analysis, interactive dashboard
- Provider-agnostic: OpenAI `gpt-4o`, Anthropic `claude-sonnet-4-20250514`, Google `gemini-1.5-pro`
- Domain templates: Email, Campaign, A/B Testing, Site Usage, Sales, Benchmark Survey, General, or Custom KPIs
- Auto-generated charts, correlation heatmap, time series detection, outlier detection (IQR), A/B significance test
- Executive summary, KPI cards, data-quality notes, follow-up questions
- Snowflake-compatible SQL generation with copy buttons
- Export: multi-page PDF report, Excel (3 tabs), cleaned dataset
- Chat-with-your-data sidebar
- Column profiling popup (double-click a column header)
- Optional benchmark overlays on charts
- API keys kept in server session memory only — never logged or persisted

## Local development

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000

## Deploy to Railway (one command)

1. Install the [Railway CLI](https://docs.railway.app/develop/cli):
   ```bash
   npm i -g @railway/cli
   railway login
   ```

2. From the project root:
   ```bash
   railway up
   ```

That's it. Railway detects `railway.json` + `Procfile` + `requirements.txt`, builds with Nixpacks, and starts `gunicorn`.

3. Get a public URL:
   ```bash
   railway domain
   ```

The printed `*.up.railway.app` URL is shareable immediately.

### Environment variables (optional)

| Variable | Default | Purpose |
| --- | --- | --- |
| `FLASK_SECRET_KEY` | random per restart | Stable secret for session cookies across restarts |
| `PORT` | 5000 locally / injected by Railway | Web server port |

Set on Railway with `railway variables set FLASK_SECRET_KEY=...`.

## Project structure

```
.
├── app.py                 # Flask backend: routes, cleaning, AI calls, exports
├── templates/index.html   # Single-page UI
├── static/css/style.css   # Dashboard styling
├── static/js/app.js       # Frontend interactivity + Plotly rendering
├── requirements.txt
├── Procfile               # web: gunicorn app:app ...
├── railway.json
├── runtime.txt            # python-3.11.9
└── README.md
```

## How it works

1. **Upload** — CSV is parsed by Pandas. Encoding auto-detected (UTF-8, Latin-1, CP1252).
2. **Clean** — column-name whitespace trimmed, duplicates removed, types inferred (dates, numbers, booleans), nulls filled via median / mode / ffill, columns with >50% null dropped. A cleaning report is produced.
3. **Profile** — shape, dtypes, null counts, cardinality, sample rows, describe() output.
4. **AI analysis** — a unified `analyze()` function routes to the chosen provider, sending the profile + domain template. All three providers receive the same prompt and return the same JSON schema. One retry with a stricter system prompt if the response is malformed.
5. **Render** — charts built with Plotly from the AI's specs. Charts that reference missing columns are skipped with a warning.
6. **Extras** — correlation heatmap for numeric columns, auto time-series trend if a date column is present, IQR outlier detection, chi-square/t-test if an A/B structure is detected.
7. **Export** — PDF via ReportLab, Excel via openpyxl (Summary / Cleaned Data / Data Quality tabs).

## Security

- API keys are posted over HTTPS, stored only in the in-memory `STORE` keyed by `session["sid"]`, and never written to disk or logged.
- Max upload size 50 MB.
- Inputs are HTML-escaped in the frontend.

## Limitations

- In-memory session store — not suitable for multi-instance deployments without a shared cache (Redis). Fine for a single Railway instance.
- PDF export includes insights/summary but not rasterized chart images (requires `kaleido`, a large native dep). Falls back to HTML print view if PDF build fails.
