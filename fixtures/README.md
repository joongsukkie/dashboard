# Eval fixtures

Drop labeled B2C CSVs in here, one folder per archetype:

```
fixtures/
├── orders/         # transactions: one row = one line item or one order
├── customers/      # one row per person, no transaction detail
├── marketing/      # spend, impressions, clicks, conversions, ROAS-shaped data
├── sessions/       # web/app events, session_id + event_name + timestamp
├── subscriptions/  # plan + status + MRR + tenure
├── reviews/        # rating + text + product_id
└── catalog/        # one row per SKU, price + cost + stock
```

The eval harness picks them up automatically:

```bash
python3 eval_archetype.py --fixtures
```

It runs `detect_archetype` against every CSV (only the first 5,000 rows
for speed), grades precision / recall per archetype, and prints a
confusion matrix plus the list of misclassifications with the signals
that drove each wrong call. That's the tuning signal — when something
misclassifies, look at the printed signals and patch `archetypes.py`.

## Recommended public datasets

These all work out of the box once you place them in the right folder:

| Folder | Dataset | Where to get it |
|---|---|---|
| orders/ | UCI Online Retail II | https://archive.ics.uci.edu/dataset/502/online+retail+ii |
| orders/ | Olist orders + order_items | https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce |
| customers/ | Olist customers | (same Olist archive) |
| reviews/ | Olist order reviews | (same Olist archive) |
| reviews/ | Amazon reviews | https://www.kaggle.com/datasets/karkavelrajaj/amazon-sales-dataset |
| catalog/ | Olist products | (same Olist archive) |
| subscriptions/ | Telco Customer Churn | https://www.kaggle.com/datasets/blastchar/telco-customer-churn |

CSV files themselves are gitignored — keep them local. Commit only
`.gitkeep` placeholders to preserve the directory structure.
