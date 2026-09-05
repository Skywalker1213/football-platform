# Football Platform MVP

Ops-dashboard style web app for **fixtures/results collection** and **statistical result prediction**.

- Path: `/workspace/football-platform/`
- Intended GitHub repo: https://github.com/Skywalker1213/football-platform
- Timezone display: **Asia/Hong_Kong (UTC+8)**
- User: Joseph Ng

**Disclaimer:** Outputs are statistical estimates for education / ops visibility — **not gambling advice**.

## Quick start

```bash
cd /workspace/football-platform
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Seed DB (legacy JSON if present + live free sources for today±window)
PYTHONPATH=. FOOTBALL_SKIP_WEATHER=1 python scripts/seed.py

# Or collect / predict separately
PYTHONPATH=. python scripts/collect_cli.py --from 2026-09-02 --to 2026-09-09
PYTHONPATH=. FOOTBALL_SKIP_WEATHER=1 python scripts/predict_cli.py

# Run UI
./run.sh
# → http://127.0.0.1:8765/
```

Optional env:

| Variable | Purpose |
|----------|---------|
| `FOOTBALL_DATA_API_TOKEN` | football-data.org v4 |
| `FOOTBALL_SKIP_WEATHER=1` | Skip Open-Meteo during bulk predict (faster); match detail still fetches weather |

## Product features

1. Collect / refresh fixtures & results (button in UI or CLI)
2. Browse upcoming/recent matches with country / competition filters
3. Predictions: P(home)/P(draw)/P(away) + likely scoreline + feature breakdown
4. Accuracy panel (result %, exact score %, Brier) on finished matches with stored preds
5. Disclaimer banner on every page

## Competitions allowlist

| Region | Competitions |
|--------|----------------|
| England | Premier League, Championship, League One, League Two, FA Cup, EFL Cup |
| Germany | Bundesliga, 2. Bundesliga, DFB-Pokal |
| Switzerland | Super League, Challenge League |
| Scotland | Premiership, Championship, Scottish Cup |
| Spain | La Liga, La Liga 2, Copa del Rey |
| Netherlands | Eredivisie, Eerste Divisie, KNVB Cup |
| Europe | UCL, UEL, UECL |
| International | WC/Euro quals, Nations League, friendlies (when free sources provide them) |

Out-of-scope leagues from free feeds are dropped at upsert time.

## Data sources (legal / free only)

| Source | Auth | Role |
|--------|------|------|
| ESPN public scoreboard JSON | none | Primary fixtures/results |
| TheSportsDB | free key `3` | Secondary |
| OpenLigaDB | none | German leagues |
| football-data.co.uk CSVs | none | EN/EU fixtures & results |
| football-data.org v4 | optional token | Enrichment |
| Open-Meteo (+ geocoding) | none | Kickoff weather |

**Not used:** Flashscore, Sofascore, Bet365, or other ToS-hostile scrapes.

Collector core adapted from `/workspace/football-data/collect.py`.

### Normalized match schema

`date`, `kickoff_utc`, `kickoff_hkt`, `league`, `country`, `home`, `away`, `home_score`, `away_score`, `status`, `venue`, `source`, `source_id`, `extra` (+ `competition_key` in DB).

## Feature matrix

| Feature | Status | Notes |
|---------|--------|-------|
| Elo ratings (tier + club priors) | **Live** | Recalc from finished results |
| Poisson / Dixon–Coles score matrix | **Live** | λ from Elo + form + load + weather adj |
| Fixture load (rest days, 7/14d matches, midweek) | **Live** | From collected fixtures |
| Team recent form (WDL / PPG) | **Live** (sparse if few finished rows) | Local DB only |
| Weather at kickoff | **Live** (Open-Meteo) | Deferred in bulk CLI; on-demand on match detail |
| Venue / city geocode | **Best-effort** | Static city map + Open-Meteo geocode |
| Player recent form | **Stub** | Interface only — free sources sparse |
| Injuries / availability | **Stub** | No reliable free cross-league feed |
| True mental state | **Documented unavailable** | Minutes/load proxies only |

## Prediction model

1. Assign Elo priors (big-club table + competition tier).
2. Update Elo chronologically on finished matches (K=20, home adv, GD scaling).
3. Map Elo difference + home advantage + form + congestion + mild weather to Poisson λ.
4. Apply Dixon–Coles low-score correction; read 1X2 probs + mode scoreline.
5. Store feature factors for the UI breakdown.

Accuracy metrics: 3-way result hit-rate, exact score hit-rate, multi-class Brier.

## Project layout

```
app/
  main.py            # FastAPI + Jinja UI
  db.py              # SQLite
  competitions.py    # Allowlist
  collector_core.py  # Free-source collector
  features.py        # Weather / load / stubs
  predict.py         # Elo + Poisson
scripts/
  seed.py, collect_cli.py, predict_cli.py
templates/, static/
data/football.db
```

## Tech stack

**Option C (shipped):** FastAPI + Jinja2 HTML dashboard + SQLite + stdlib HTTP collector.
