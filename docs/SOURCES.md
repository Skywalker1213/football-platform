# Data sources / 資料來源

## Live free sources（目前接線）

| Source | Use | Notes |
|--------|-----|-------|
| **ClubElo** (`http://api.clubelo.com/`) | Consensus 1X2 + exact scores (Fixtures); daily Elo seed | Documented public API. Cache ≥6h under `cache/`. Fixtures endpoint may report *deactivated* — we fall back to Elo-derived 1X2 or model-only. |
| **football-data.co.uk** CSV | Fixtures/results + **AvgH/D/A**, **B365H/D/A**, O/U 2.5 | Odds → implied probs (overround removed) as `consensus.market`. UI: 「市場隱含機率（公開賠率，非投注建議）」 |
| ESPN scoreboard JSON | Fixtures / results | Primary schedule feed |
| TheSportsDB (free key) | Fixtures | Rate / coverage limited |
| OpenLigaDB | German leagues | Fallback |
| Open-Meteo | Match-detail weather | Deferred in bulk predict |
| football-data.org v4 | Optional | Needs `FOOTBALL_DATA_API_TOKEN` |

### 中文摘要
- **ClubElo**：免費公開 Elo／賽事機率 API；禮貌快取 6 小時以上。Fixtures 若停用則改用 Elo 推 1X2，或只用模型。
- **football-data.co.uk**：公開 CSV 賠率轉「市場隱含機率」；**非投注建議**。
- 其餘賽程／天氣來源見上表。

## Paid / ToS — stub hooks only（不抓）

| Source | Status |
|--------|--------|
| Opta | Paid — stub hook only |
| StatsBomb | Paid / licensed — stub hook only |
| The Athletic | Paywalled analysis — stub hook only |
| Expert tipster feeds | Commercial — stub hook only |

## Explicitly NOT scraped

Do **not** scrape: Forebet, PredictZ, Sofascore, Flashscore, or betting tipster sites.

## Blend weights

Stored in `model_params` for later learning:
- 3-way (model / clubelo / market): default **0.40 / 0.35 / 0.25**
- 2-way (model / clubelo): default **0.55 / 0.45**
- Missing sources: weights renormalized over what is available.

## Disclaimer

統計估計／教育用途，並非博彩建議。Market-implied probabilities are for calibration comparison only — not betting advice.


## Scope / 比賽範圍

Only **Level-1 domestic** leagues for selected countries + internationals + UCL:

- `eng_pl`, `ger_bl1`, `sui_sl`, `sco_pre`, `esp_ll`, `ned_ere`
- `int_wcq`, `int_euroq`, `int_nl`, `int_fr`
- `uefa_ucl` (Champions League only — no Europa / Conference)

Dropped: Championship / League One–Two / La Liga 2 / 2. Bundesliga / Challenge League / Eerste Divisie / domestic cups / UEL / UECL.

## Expert tips / 專家提示（人手匯入）

PredictZ、Facebook、Threads、Forebet 等**不會自動爬取**（ToS、無官方 API、易被 ban）。

How to import manually:

```bash
# Edit a copy of the example, then:
PYTHONPATH=. python scripts/import_expert_tips.py data/expert_tips.example.csv --link-matches

# Optional: public sports RSS headlines only (BBC / ESPN soccer)
PYTHONPATH=. python scripts/import_expert_tips.py --fetch-rss
```

CSV columns: `match_date,home,away,source,analyst_name,pick_1x2,score_home,score_away,confidence,url,notes`

Imported tips become `consensus.experts` (majority 1X2 → soft probs) with modest blend weight `blend_w_experts` ≈ 0.15.


## How to refresh / 如何更新

```bash
PYTHONPATH=. FOOTBALL_SKIP_WEATHER=1 python scripts/daily_update.py
PYTHONPATH=. python scripts/import_expert_tips.py your_tips.csv --link-matches
PYTHONPATH=. python scripts/import_expert_tips.py --fetch-rss
```

ClubElo responses are cached under `cache/` (≥6h positive; ~3h negative for deactivated/502).


## Innovative open-source calculations (added)

### Pi-ratings (Constantinou & Fenton 2013)
- Separate home/away ability, goal-margin updates, zero-centered.
- Implemented in `app/pi_ratings.py` (literature / open ports such as penaltyblog — no scraped tipster data).

### Shin (1993) odds de-vig
- Converts decimal odds to implied probabilities with insider-trading correction.
- Prefer Shin over raw 1/odds when market odds exist in `match.extra`.

### Quantum-inspired Born interference (experimental)
- Classical complex amplitudes + phase shifts from Elo/Pi/form/market disagreement.
- Collapse via Born rule `|A|²`. **Not quantum hardware** — documented analogy in `app/quantum_inspired.py`.
