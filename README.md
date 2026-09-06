# Quant-Striker | 足球量化預測系統

Ops-dashboard style web app for **fixtures/results collection** and **statistical result prediction** (Quant-Striker v3 ideas ported into the Python stack).

- Path: `/workspace/football-platform/`
- Intended GitHub repo: https://github.com/Skywalker1213/football-platform
- Timezone display: **Asia/Hong_Kong (UTC+8)**
- User: Joseph Ng

**Disclaimer:** Outputs are statistical estimates for education / ops visibility — **not gambling advice**.

## Competition scope

Level-1 domestic + internationals + UCL only. See docs/SOURCES.md and app/competitions.py.
Expert tips: manual import via scripts/import_expert_tips.py (no PredictZ/FB scrape).


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

# Daily collect + self-learn + predict + report
PYTHONPATH=. FOOTBALL_SKIP_WEATHER=1 python scripts/daily_update.py

# Windows Task Scheduler (Documents\\football-platform copy; admin may be needed)
# powershell -ExecutionPolicy Bypass -File scripts\\install_windows_daily_task.ps1

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
| Quant-Striker five-factor layer | **Live** (ported) | form30/geo/stakes live; playerImpact estimated stub; commercial if market odds |
| QS confidence + EV scan | **Live** | System card in consensus/features_json; EV research-only |

## Prediction model

1. Assign Elo priors (big-club table + competition tier).
2. Update Elo chronologically on finished matches (K=20, home adv, GD scaling).
3. **Quant-Striker layer** (ported concepts from local QS v3): composite Elo (0.6 real + 0.4 PPG/GD power-rank proxy) + five capped factors (form30, playerImpact, geo, commercial, stakes) → adjusted Elo / λ multipliers.
4. Map adjusted Elo + home advantage + form + congestion + mild weather to Poisson λ; apply QS λ multipliers.
5. Dixon–Coles low-score correction → model 1X2; blend with ClubElo / market / experts / Pi-ratings / quantum-inspired.
6. Attach **Quant-Striker system card** from *final* blended probs: result call, consistent scoreline, confidence tier (STRONG ≥72% / LEAN ≥58% / TOSS-UP), optional EV scan vs public odds (**research only — not betting advice**).

**Pipeline order (documented):** base Elo → QS factors → Poisson → market/Pi/quantum blend → QS system card.

Accuracy metrics: 3-way result hit-rate, exact score hit-rate, multi-class Brier.

### Quant-Striker factor mapping

| QS factor | Platform source | Cap |
|-----------|-----------------|-----|
| form30 | Decay-weighted PPG/GD from local finished history | ±60 Elo |
| playerImpact | injuries / player_form (stubs → δ=0, marked estimated) | ±40 Elo |
| geo | Open-Meteo weather + `weather_scoring_adj` → λ multipliers | ~8% λ |
| commercial | Market disagreement vs model (football-data.co.uk odds in `extra`) | ±30 Elo |
| stakes | UCL / friendly / fixture-load congestion heuristics | ~5% λ |

Config: `config/quant_striker.json`. Module: `app/quant_striker.py`. Attribution: user's local Quant-Striker v3 engine concepts (no Transfermarkt / fragile HTML odds scrapers).

## Project layout

```
app/
  main.py            # FastAPI + Jinja UI (Quant-Striker branding)
  db.py              # SQLite
  competitions.py    # Allowlist
  collector_core.py  # Free-source collector
  features.py        # Weather / load / stubs
  predict.py         # Elo + QS factors + Poisson + blends
  quant_striker.py   # QS v3 port (factors, system card, EV)
config/
  quant_striker.json # QS model / factor weights & caps
scripts/
  seed.py, collect_cli.py, predict_cli.py
templates/, static/
data/football.db
```

## Tech stack

**Option C (shipped):** FastAPI + Jinja2 HTML dashboard + SQLite + stdlib HTTP collector.

## YouTube short-form pipeline

Daily ~3 min landscape video from top-6 Quant-Striker confidence picks (zh-Hant).

```bash
PYTHONPATH=. python scripts/make_youtube_video.py
```

Outputs: `data/youtube/quant_striker_picks.mp4` + title/description/tags.  
Details: [docs/YOUTUBE_PIPELINE.md](docs/YOUTUBE_PIPELINE.md).  
**Does not upload** — research/education only, not gambling advice.

