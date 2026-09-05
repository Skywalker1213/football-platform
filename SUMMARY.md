# Football Platform — SUMMARY

Generated: 2026-09-05 (HKT)

## Scope (narrowed)

**Level-1 only** for EN/DE/CH/SCO/ES/NL + internationals + **UCL**:
`eng_pl`, `ger_bl1`, `sui_sl`, `sco_pre`, `esp_ll`, `ned_ere`, `uefa_ucl`, `int_wcq`, `int_euroq`, `int_nl`, `int_fr`.

Dropped lower divisions, domestic cups, Europa League, Conference League.

## Scoreline bias (before → after)

| Metric | Before | After |
|--------|--------|-------|
| Predicted mode mass | ~338× `1-1` + 11× `1-0` (almost all) | Still often `1-1` as Poisson mode, but **top-5** now shown (2-1 / 1-0 / 2-0 / …) |
| `avg_goals` baseline | 1.30–1.35 (learning) | Defaults **1.45**; live param bumped ≈**1.42** |
| UI | Single mode only | 「最可能比分」+ 「其他可能」bars |
| Consensus | Model only | Model + **market** (fd.uk odds) + ClubElo when API up + **experts** (manual) |

ClubElo `Fixtures` currently returns *deactivated*; daily Elo API often 502 — predict falls back model/market-only without crashing (negative cache ≥3h).

## Consensus sources

| Source | Status |
|--------|--------|
| ClubElo Fixtures / Elo | Live API with cache; graceful fallback |
| football-data.co.uk Avg/B365 odds | Live in `extra` → `consensus.market` |
| Expert tips (PredictZ/FB/Threads/media) | **Manual CSV/JSON import only** — no scrapers |
| BBC / ESPN soccer RSS | Headlines → `media_notes` (not tipster scrapes) |

Blend defaults: model/clubelo/market **0.40/0.35/0.25**; model/clubelo **0.55/0.45**; experts **~0.15** when tips match. Weights in `model_params`.

## How to refresh

```bash
cd /workspace/football-platform
source .venv/bin/activate
PYTHONPATH=. FOOTBALL_SKIP_WEATHER=1 python scripts/daily_update.py
# or
PYTHONPATH=. python scripts/collect_cli.py
PYTHONPATH=. FOOTBALL_SKIP_WEATHER=1 python scripts/predict_cli.py
PYTHONPATH=. python scripts/import_expert_tips.py data/expert_tips.example.csv --link-matches
PYTHONPATH=. python scripts/import_expert_tips.py --fetch-rss
./run.sh   # http://127.0.0.1:8765/
```

## Files touched (this pass)

- `app/consensus.py` — ClubElo, market odds, experts aggregate, RSS
- `app/predict.py` — `top_scorelines`, blend, raised AVG_GOALS
- `app/db.py` — blend defaults, `expert_tips` / `media_notes`
- `app/collector_core.py` — odds in extra; Level-1 ESPN/FD filters
- `app/competitions.py` — Level-1 allowlist
- `templates/match.html`, `index.html`, `static/style.css`
- `scripts/import_expert_tips.py`, `data/expert_tips.example.csv`
- `docs/SOURCES.md`

## Caveats

- Free feeds still duplicate club names across sources.
- Exact-score mode remains Poisson-heavy toward 1-1 at λ≈1.4–1.6; diversity is in **top-5** + market/ClubElo when available.
- No Flashscore / Sofascore / Forebet / PredictZ auto-scrape (intentional).
