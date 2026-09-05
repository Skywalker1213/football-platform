# Football Platform — SUMMARY

Generated: 2026-09-05 (HKT)

## What works

- Full project at `/workspace/football-platform/` (created locally; no CloudAgent; no GitHub clone).
- Free-source collector (ESPN, TheSportsDB, OpenLigaDB, football-data.co.uk; optional football-data.org token).
- Allowlist filter for EN/DE/CH/SCO/ES/NL + UEFA cups + internationals when present.
- SQLite store with 300+ allowlisted matches for ~2026-09-02 → 2026-09-09.
- Elo + Poisson/Dixon–Coles predictions with feature breakdown UI.
- Accuracy panel (result / exact / Brier) on finished matches.
- Ops dashboard (not betting chrome) with filters + refresh API.
- Weather via Open-Meteo on match detail (deferred in bulk predict for speed).

## How to run

```bash
cd /workspace/football-platform
source .venv/bin/activate   # already created
./run.sh
```

Open **http://127.0.0.1:8765/** (binds `0.0.0.0:8765`).

**Dev server status:** RUNNING in background on port **8765**.

Refresh data from UI button or:

```bash
PYTHONPATH=. python scripts/collect_cli.py
PYTHONPATH=. FOOTBALL_SKIP_WEATHER=1 python scripts/predict_cli.py
```

## Sample predictions (after seed)

| Match | P(H/D/A) | Scoreline |
|-------|----------|-----------|
| Manchester City vs Coventry City | ~67% / 21% / 12% | 2–0 |
| Arsenal vs Chelsea | ~55% / 25% / 20% | 1–1 |
| Newcastle vs Bournemouth | ~56% / 24% / 19% | 1–1 |
| Ipswich Town 0–2 Liverpool (finished backtest) | ~40% / 27% / 33% | 1–1 |

Exact numbers drift as Elo recalculates.

## Live vs stubbed

| Area | State |
|------|-------|
| Collect fixtures/results | Live |
| Browse + filters | Live |
| Elo / Poisson predictions | Live |
| Fixture load + team form | Live (form sparse early season window) |
| Weather | Live on detail; deferred in bulk |
| Player form | Stub |
| Injuries | Stub |
| Mental state | Documented unavailable (load proxies only) |

## Blockers / caveats

- Free feeds duplicate names (`Man City` vs `Manchester City`) → near-duplicate rows until stronger entity resolution.
- Some ESPN cup/international slugs return HTTP 400 — removed from default list; cups still arrive via other sources when available.
- football-data.org empty without `FOOTBALL_DATA_API_TOKEN`.
- TheSportsDB free key is coverage/rate limited.
- With a short finished-match window, Elo needs club/tier priors; accuracy is near-chance until more results accumulate (~40% result accuracy on current finished set).
- No Flashscore/Sofascore/Bet365 (intentional ToS stance).

## Seed snapshot

- Matches: ~316 allowlisted
- Countries: England, Germany, Netherlands, Spain, Scotland, Europe, Switzerland
- Predictions: one per match
- DB: `data/football.db`
