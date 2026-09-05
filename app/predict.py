"""Elo + Poisson (Dixon-Coles-lite) result prediction."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from app.collector_core import _norm_team
from app import db
from app.features import build_features_for_match

# Elo
K_FACTOR = 20.0
HOME_ADV_ELO = 80.0
INITIAL_ELO = 1500.0

TIER_ELO = {
    "eng_pl": 1650, "eng_ch": 1480, "eng_l1": 1380, "eng_l2": 1320, "eng_fa": 1500, "eng_efl": 1500,
    "ger_bl1": 1620, "ger_bl2": 1460, "ger_dfb": 1500,
    "esp_ll": 1630, "esp_ll2": 1450, "esp_copa": 1500,
    "ned_ere": 1550, "ned_eer": 1400, "ned_knvb": 1480,
    "sco_pre": 1480, "sco_ch": 1360, "sco_cup": 1450,
    "sui_sl": 1480, "sui_chl": 1360,
    "uefa_ucl": 1680, "uefa_uel": 1580, "uefa_uecl": 1500,
    "int_wcq": 1550, "int_euroq": 1520, "int_nl": 1550, "int_fr": 1500,
}



# Best-effort club priors (sparse free history in MVP window)
CLUB_ELO = {
    "manchester city": 1780, "liverpool": 1765, "arsenal": 1755, "chelsea": 1705,
    "manchester united": 1690, "tottenham hotspur": 1685, "newcastle united": 1675,
    "aston villa": 1665, "brighton and hove albion": 1635, "west ham united": 1625,
    "crystal palace": 1610, "fulham": 1605, "brentford": 1600, "bournemouth": 1590,
    "wolverhampton wanderers": 1585, "nottingham forest": 1580, "everton": 1575,
    "ipswich town": 1520, "leicester city": 1540, "southampton": 1525, "leeds united": 1560,
    "sunderland": 1545, "coventry city": 1480, "burnley": 1510,
    "real madrid": 1800, "barcelona": 1780, "atletico madrid": 1720, "sevilla": 1620,
    "bayern munich": 1790, "borussia dortmund": 1720, "bayer leverkusen": 1700, "rb leipzig": 1680,
    "ajax": 1650, "psv eindhoven": 1640, "feyenoord": 1630,
    "celtic": 1600, "rangers": 1585,
}


def elo_prior(competition_key: Optional[str], team_norm: Optional[str] = None) -> float:
    if team_norm and team_norm in CLUB_ELO:
        return float(CLUB_ELO[team_norm])
    if not competition_key:
        return INITIAL_ELO
    return float(TIER_ELO.get(competition_key, INITIAL_ELO))



# Poisson
AVG_GOALS = 1.35  # per team baseline
HOME_ADV_GOALS = 0.25


def expected_score(elo_a: float, elo_b: float) -> float:
    return 1.0 / (1.0 + 10 ** ((elo_b - elo_a) / 400.0))


def result_score(home_goals: int, away_goals: int) -> Tuple[float, float]:
    if home_goals > away_goals:
        return 1.0, 0.0
    if home_goals < away_goals:
        return 0.0, 1.0
    return 0.5, 0.5


def ensure_elo(team_norm: str, competition_key: Optional[str] = None) -> float:
    with db.get_db() as conn:
        row = conn.execute("SELECT elo FROM team_elo WHERE team_norm=?", (team_norm,)).fetchone()
    if row:
        return float(row["elo"])
    prior = elo_prior(competition_key, team_norm)
    db.set_elo(team_norm, prior, 0)
    return prior


def update_elo(home: str, away: str, hs: int, aws: int, competition_key: Optional[str] = None) -> None:
    hn, an = _norm_team(home), _norm_team(away)
    eh = ensure_elo(hn, competition_key)
    ea = ensure_elo(an, competition_key)
    # store matches_played via raw query
    with db.get_db() as conn:
        rh = conn.execute("SELECT matches_played FROM team_elo WHERE team_norm=?", (hn,)).fetchone()
        ra = conn.execute("SELECT matches_played FROM team_elo WHERE team_norm=?", (an,)).fetchone()
    mph = int(rh["matches_played"]) if rh else 0
    mpa = int(ra["matches_played"]) if ra else 0

    exp_h = expected_score(eh + HOME_ADV_ELO, ea)
    exp_a = 1.0 - exp_h
    sh, sa = result_score(hs, aws)
    # goal-diff scaling (modest)
    gd = abs(hs - aws)
    mult = min(1.5, 1.0 + 0.1 * max(0, gd - 1))
    new_h = eh + K_FACTOR * mult * (sh - exp_h)
    new_a = ea + K_FACTOR * mult * (sa - exp_a)
    db.set_elo(hn, new_h, mph + 1)
    db.set_elo(an, new_a, mpa + 1)


def rebuild_elo() -> int:
    """Recalculate Elo from all finished matches chronologically."""
    with db.get_db() as conn:
        conn.execute("DELETE FROM team_elo")
    finished = db.all_finished_chronological()
    for m in finished:
        update_elo(m["home"], m["away"], int(m["home_score"]), int(m["away_score"]), m.get("competition_key"))
    return len(finished)


def poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * lam**k / math.factorial(k)


def dixon_coles_tau(i: int, j: int, lam_h: float, lam_a: float, rho: float = -0.05) -> float:
    """Simple Dixon-Coles low-score correction."""
    if i == 0 and j == 0:
        return 1.0 - lam_h * lam_a * rho
    if i == 0 and j == 1:
        return 1.0 + lam_h * rho
    if i == 1 and j == 0:
        return 1.0 + lam_a * rho
    if i == 1 and j == 1:
        return 1.0 - rho
    return 1.0


def score_matrix(lam_h: float, lam_a: float, max_goals: int = 8) -> List[List[float]]:
    mat = []
    for i in range(max_goals + 1):
        row = []
        for j in range(max_goals + 1):
            p = poisson_pmf(i, lam_h) * poisson_pmf(j, lam_a) * dixon_coles_tau(i, j, lam_h, lam_a)
            row.append(max(0.0, p))
        mat.append(row)
    # renormalize
    s = sum(sum(r) for r in mat)
    if s > 0:
        mat = [[c / s for c in r] for r in mat]
    return mat


def outcome_probs(mat: List[List[float]]) -> Tuple[float, float, float]:
    ph = pd = pa = 0.0
    for i, row in enumerate(mat):
        for j, p in enumerate(row):
            if i > j:
                ph += p
            elif i == j:
                pd += p
            else:
                pa += p
    return ph, pd, pa


def most_likely_score(mat: List[List[float]]) -> Tuple[int, int]:
    best = (0, 0)
    best_p = -1.0
    for i, row in enumerate(mat):
        for j, p in enumerate(row):
            if p > best_p:
                best_p = p
                best = (i, j)
    return best


def lambdas_from_elo_and_features(
    elo_h: float, elo_a: float, features: Dict[str, Any]
) -> Tuple[float, float]:
    # Elo difference → expected goal edge
    diff = (elo_h + HOME_ADV_ELO) - elo_a
    # map ~400 elo to ~0.6 goals
    edge = diff / 500.0
    lam_h = AVG_GOALS + HOME_ADV_GOALS + edge / 2
    lam_a = AVG_GOALS - HOME_ADV_GOALS - edge / 2

    # Form adjustment
    hf = features.get("home_form") or {}
    af = features.get("away_form") or {}
    if hf.get("ppg") is not None and af.get("ppg") is not None:
        form_edge = (hf["ppg"] - af["ppg"]) * 0.12
        lam_h += form_edge / 2
        lam_a -= form_edge / 2

    # Fixture congestion: tired teams score/concede slightly worse
    hl = features.get("home_load") or {}
    al = features.get("away_load") or {}
    if (hl.get("matches_last_7d") or 0) >= 2:
        lam_h -= 0.08
        lam_a += 0.04
    if (al.get("matches_last_7d") or 0) >= 2:
        lam_a -= 0.08
        lam_h += 0.04
    if hl.get("rest_days") is not None and hl["rest_days"] <= 3:
        lam_h -= 0.05
    if al.get("rest_days") is not None and al["rest_days"] <= 3:
        lam_a -= 0.05

    # Weather mild scoring suppression
    wadj = features.get("weather_scoring_adj") or 0.0
    lam_h += wadj
    lam_a += wadj

    lam_h = max(0.35, min(3.2, lam_h))
    lam_a = max(0.30, min(3.0, lam_a))
    return lam_h, lam_a


def predict_match(match: Dict[str, Any], persist: bool = True) -> Dict[str, Any]:
    home, away = match["home"], match["away"]
    date = match.get("date") or ""
    hn, an = _norm_team(home), _norm_team(away)
    ck = match.get("competition_key")
    elo_h = ensure_elo(hn, ck)
    elo_a = ensure_elo(an, ck)

    home_hist = db.team_matches_before(home, date, limit=30)
    away_hist = db.team_matches_before(away, date, limit=30)
    # Also include any matches from same DB where team appears (team_matches_before uses exact name)
    features = build_features_for_match(match, home_hist, away_hist)

    lam_h, lam_a = lambdas_from_elo_and_features(elo_h, elo_a, features)
    mat = score_matrix(lam_h, lam_a)
    ph, pd, pa = outcome_probs(mat)
    sh, sa = most_likely_score(mat)

    # Feature factor breakdown for UI
    factors = [
        {"name": "Home Elo", "value": round(elo_h, 1), "impact": "strength"},
        {"name": "Away Elo", "value": round(elo_a, 1), "impact": "strength"},
        {"name": "Elo edge (home+HA)", "value": round((elo_h + HOME_ADV_ELO) - elo_a, 1), "impact": "positive" if elo_h + HOME_ADV_ELO > elo_a else "negative"},
        {"name": "λ home", "value": round(lam_h, 3), "impact": "attack"},
        {"name": "λ away", "value": round(lam_a, 3), "impact": "attack"},
    ]
    if features.get("home_form", {}).get("form_string"):
        factors.append({"name": "Home form (last N)", "value": features["home_form"]["form_string"], "impact": "form"})
    if features.get("away_form", {}).get("form_string"):
        factors.append({"name": "Away form (last N)", "value": features["away_form"]["form_string"], "impact": "form"})
    if features.get("home_load", {}).get("rest_days") is not None:
        factors.append({"name": "Home rest days", "value": features["home_load"]["rest_days"], "impact": "load"})
    if features.get("away_load", {}).get("rest_days") is not None:
        factors.append({"name": "Away rest days", "value": features["away_load"]["rest_days"], "impact": "load"})
    w = features.get("weather") or {}
    if w.get("status") == "live":
        factors.append({
            "name": "Weather (°C / precip mm)",
            "value": f"{w.get('temperature_c')}°C / {w.get('precipitation_mm')}mm",
            "impact": "weather",
        })
    factors.append({"name": "Injuries", "value": "stub — not used", "impact": "stub"})
    factors.append({"name": "Player form", "value": "stub — team form used", "impact": "stub"})

    features["factors"] = factors
    features["elo"] = {"home": elo_h, "away": elo_a, "home_norm": hn, "away_norm": an}

    pred = {
        "p_home": round(ph, 4),
        "p_draw": round(pd, 4),
        "p_away": round(pa, 4),
        "score_home": sh,
        "score_away": sa,
        "lambda_home": round(lam_h, 4),
        "lambda_away": round(lam_a, 4),
        "model": "elo_poisson_dixon_coles",
        "features": features,
    }
    if persist and match.get("id"):
        db.save_features(int(match["id"]), features)
        db.save_prediction(int(match["id"]), pred)
    return pred


def predict_all(limit: int = 500) -> int:
    """Predict upcoming + recent matches; also backfill finished for accuracy."""
    import os
    prev = os.environ.get("FOOTBALL_SKIP_WEATHER")
    os.environ["FOOTBALL_SKIP_WEATHER"] = "1"
    # reload flag in features module
    import app.features as features
    features.SKIP_WEATHER = True
    try:
        matches = db.query_matches(limit=limit)
        n = 0
        for m in matches:
            predict_match(m, persist=True)
            n += 1
        return n
    finally:
        if prev is None:
            os.environ.pop("FOOTBALL_SKIP_WEATHER", None)
        else:
            os.environ["FOOTBALL_SKIP_WEATHER"] = prev
        features.SKIP_WEATHER = os.environ.get("FOOTBALL_SKIP_WEATHER", "0") == "1"
