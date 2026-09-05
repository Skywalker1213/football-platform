"""Elo + Poisson (Dixon-Coles-lite) result prediction."""
from __future__ import annotations

import hashlib
import math
import random
from typing import Any, Dict, List, Optional, Tuple

from app.collector_core import _norm_team
from app import db
from app.features import build_features_for_match

# Elo
K_FACTOR = 20.0
HOME_ADV_ELO = 80.0
INITIAL_ELO = 1500.0

TIER_ELO = {
    "eng_pl": 1650,
    "ger_bl1": 1620,
    "esp_ll": 1630,
    "ned_ere": 1550,
    "sco_pre": 1480,
    "sui_sl": 1480,
    "uefa_ucl": 1680,
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
AVG_GOALS = 1.45  # per team baseline (raised to reduce 1-1 mode collapse)
HOME_ADV_GOALS = 0.25
RHO = -0.05


def get_active_params() -> Dict[str, float]:
    """Read tunable params from DB (fall back to module defaults)."""
    defaults = {
        "home_adv_elo": HOME_ADV_ELO,
        "home_adv_goals": HOME_ADV_GOALS,
        "avg_goals": AVG_GOALS,
        "k_factor": K_FACTOR,
        "rho": RHO,
        "calibration_home": 1.0,
        "calibration_draw": 1.0,
        "calibration_away": 1.0,
        "blend_w_model": 0.40,
        "blend_w_clubelo": 0.35,
        "blend_w_market": 0.25,
        "blend_w_model_2": 0.55,
        "blend_w_clubelo_2": 0.45,
        "blend_w_experts": 0.15,
    }
    try:
        params = db.get_model_params()
        defaults.update(params)
    except Exception:
        pass
    return defaults


def apply_calibration(ph: float, pd: float, pa: float, params: Optional[Dict[str, float]] = None) -> Tuple[float, float, float]:
    params = params or get_active_params()
    ph *= float(params.get("calibration_home", 1.0))
    pd *= float(params.get("calibration_draw", 1.0))
    pa *= float(params.get("calibration_away", 1.0))
    s = ph + pd + pa
    if s <= 0:
        return 1 / 3, 1 / 3, 1 / 3
    return ph / s, pd / s, pa / s


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

    params = get_active_params()
    home_adv = float(params.get("home_adv_elo", HOME_ADV_ELO))
    k_factor = float(params.get("k_factor", K_FACTOR))
    exp_h = expected_score(eh + home_adv, ea)
    exp_a = 1.0 - exp_h
    sh, sa = result_score(hs, aws)
    # goal-diff scaling (modest)
    gd = abs(hs - aws)
    mult = min(1.5, 1.0 + 0.1 * max(0, gd - 1))
    new_h = eh + k_factor * mult * (sh - exp_h)
    new_a = ea + k_factor * mult * (sa - exp_a)
    db.set_elo(hn, new_h, mph + 1)
    db.set_elo(an, new_a, mpa + 1)


def rebuild_elo() -> int:
    """Recalculate Elo from all finished matches chronologically."""
    with db.get_db() as conn:
        conn.execute("DELETE FROM team_elo")
    finished = db.all_finished_chronological()
    for m in finished:
        update_elo(m["home"], m["away"], int(m["home_score"]), int(m["away_score"]), m.get("competition_key"))
    try:
        from app.pi_ratings import rebuild_pi
        rebuild_pi()
    except Exception:
        pass
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


def score_matrix(lam_h: float, lam_a: float, max_goals: int = 8, rho: float = -0.05) -> List[List[float]]:
    mat = []
    for i in range(max_goals + 1):
        row = []
        for j in range(max_goals + 1):
            p = poisson_pmf(i, lam_h) * poisson_pmf(j, lam_a) * dixon_coles_tau(i, j, lam_h, lam_a, rho=rho)
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





def _recent_score_trend(hist: List[Dict[str, Any]], team: str, n: int = 5) -> Dict[str, Any]:
    """Summarize last N finished results for narrative scoring."""
    rows = [h for h in (hist or []) if h.get("home_score") is not None and h.get("away_score") is not None][:n]
    if not rows:
        return {"n": 0, "avg_gf": None, "avg_ga": None, "avg_total": None, "draw_rate": None, "high_score_rate": None, "common": []}
    gf = ga = draws = high = 0
    scores: List[Tuple[int, int]] = []
    for h in rows:
        hs, aws = int(h["home_score"]), int(h["away_score"])
        if (h.get("home") or "") == team:
            g_for, g_against = hs, aws
        else:
            g_for, g_against = aws, hs
        gf += g_for
        ga += g_against
        if hs == aws:
            draws += 1
        if hs + aws >= 4:
            high += 1
        scores.append((hs, aws))
    from collections import Counter
    common = [f"{a}-{b}" for (a, b), _ in Counter(scores).most_common(3)]
    n_ = len(rows)
    return {
        "n": n_,
        "avg_gf": gf / n_,
        "avg_ga": ga / n_,
        "avg_total": (gf + ga) / n_,
        "draw_rate": draws / n_,
        "high_score_rate": high / n_,
        "common": common,
    }


def human_scoreline(
    lam_h: float,
    lam_a: float,
    ph: float,
    pd: float,
    pa: float,
    tops: List[Dict[str, Any]],
    match_id: Any = None,
    competition_key: Optional[str] = None,
    home_hist: Optional[List[Dict[str, Any]]] = None,
    away_hist: Optional[List[Dict[str, Any]]] = None,
    home: Optional[str] = None,
    away: Optional[str] = None,
) -> Tuple[int, int, int, int]:
    """Return (human_h, human_a, stat_h, stat_a) with league/trend-aware narrative.

    - 50/50 games: avoid blowouts like 3-1; prefer 1-1 / 2-2 / 2-1 / 1-2
    - Recent 5-match trends (e.g. many 2-2) nudge candidates
    - Bundesliga / Eredivisie: allow 4-1, 5-2, 5-1 when favorite or high-scoring trend
    """
    if tops:
        stat_h, stat_a = int(tops[0]["home"]), int(tops[0]["away"])
    else:
        stat_h, stat_a = 1, 1

    outcome = max(("H", ph), ("D", pd), ("A", pa), key=lambda x: x[1])[0]
    edge = abs(ph - pa)
    # coin-flip / tight: max outcome not dominant OR home≈away
    tight = (max(ph, pd, pa) < 0.42) or (edge < 0.10 and pd >= 0.26) or (edge < 0.08)

    ht = _recent_score_trend(home_hist or [], home or "", 5)
    at = _recent_score_trend(away_hist or [], away or "", 5)
    league_high = competition_key in {"ger_bl1", "ned_ere"}

    seed = int(hashlib.md5(
        f"human2:{match_id}:{lam_h:.3f}:{lam_a:.3f}:{competition_key}".encode()
    ).hexdigest()[:8], 16)
    rng = random.Random(seed)

    def rnd_goals(x: float, favor_up: bool = False) -> int:
        base = int(math.floor(x))
        frac = x - base
        thresh = 0.38 if favor_up else 0.52
        return max(0, min(6, base + (1 if frac >= thresh else 0)))

    cands: List[Tuple[int, int, float]] = []

    if tight:
        # No 3-1 style blowouts for coin-flips
        base_pool = [
            (1, 1, 1.2),
            (2, 2, 0.85 if ((ht.get("draw_rate") or 0) + (at.get("draw_rate") or 0)) / 2 >= 0.3 or (ht.get("avg_total") or 0) >= 2.8 else 0.35),
            (2, 1, 0.7),
            (1, 2, 0.7),
            (0, 0, 0.35 if (lam_h + lam_a) < 2.2 else 0.12),
            (3, 3, 0.15 if league_high else 0.05),
            (1, 0, 0.25),
            (0, 1, 0.25),
        ]
        # If recent common scores include 2-2, boost it
        for label, wboost in (("2-2", 1.1), ("1-1", 0.5), ("2-1", 0.3), ("1-2", 0.3)):
            if label in (ht.get("common") or []) or label in (at.get("common") or []):
                a, b = map(int, label.split("-"))
                base_pool.append((a, b, 0.9 + wboost))
        cands.extend(base_pool)
    else:
        eh = rnd_goals(lam_h, favor_up=(outcome == "H" and ph >= 0.48))
        ea = rnd_goals(lam_a, favor_up=(outcome == "A" and pa >= 0.48))
        if outcome == "H" and eh <= ea:
            eh = min(6, ea + 1)
        elif outcome == "A" and ea <= eh:
            ea = min(6, eh + 1)
        elif outcome == "D":
            ea = eh
        cands.append((eh, ea, 1.1))

        for t in tops[:10]:
            h, a = int(t["home"]), int(t["away"])
            w = float(t["p"]) ** 0.55
            boring = (h, a) in {(1, 0), (0, 1), (2, 1), (1, 2), (1, 1)}
            w *= 0.75 if boring else 1.45
            if outcome == "H" and h > a:
                cands.append((h, a, w))
            elif outcome == "A" and a > h:
                cands.append((h, a, w))
            elif outcome == "D" and h == a:
                cands.append((h, a, w))

        # Strong favorite narratives
        if outcome == "H" and ph >= 0.55:
            for sc, w0 in [((2, 0), 0.5), ((3, 1), 0.4), ((2, 1), 0.35), ((1, 0), 0.22)]:
                cands.append((sc[0], sc[1], w0 * (0.5 + ph)))
        if outcome == "A" and pa >= 0.55:
            for sc, w0 in [((0, 2), 0.5), ((1, 3), 0.4), ((1, 2), 0.35), ((0, 1), 0.22)]:
                cands.append((sc[0], sc[1], w0 * (0.5 + pa)))

        # Bundesliga / Eredivisie high-scoring culture
        if league_high:
            hi_home = [((4, 1), 0.55), ((5, 2), 0.35), ((5, 1), 0.3), ((4, 2), 0.28), ((3, 1), 0.4), ((3, 2), 0.33)]
            hi_away = [((1, 4), 0.55), ((2, 5), 0.35), ((1, 5), 0.3), ((2, 4), 0.28), ((1, 3), 0.4), ((2, 3), 0.33)]
            hi_draw = [((2, 2), 0.5), ((3, 3), 0.25)]
            trend_hi = ((ht.get("high_score_rate") or 0) + (at.get("high_score_rate") or 0)) / 2
            boost = 0.7 + 0.9 * trend_hi + (0.35 if (ht.get("avg_total") or 0) >= 3.0 or (at.get("avg_total") or 0) >= 3.0 else 0)
            if outcome == "H" and ph >= 0.48:
                for sc, w0 in hi_home:
                    cands.append((sc[0], sc[1], w0 * boost * (0.55 + ph)))
            elif outcome == "A" and pa >= 0.48:
                for sc, w0 in hi_away:
                    cands.append((sc[0], sc[1], w0 * boost * (0.55 + pa)))
            elif outcome == "D" or tight:
                for sc, w0 in hi_draw:
                    cands.append((sc[0], sc[1], w0 * boost))

        # Recent exact score echoes (e.g. both sides in 2-2 games)
        for label in (ht.get("common") or []) + (at.get("common") or []):
            try:
                a, b = map(int, label.split("-"))
            except Exception:
                continue
            w = 0.55
            if tight and a == b:
                w = 1.2
            if not tight and outcome == "H" and a > b:
                w = 0.7
            if not tight and outcome == "A" and b > a:
                w = 0.7
            cands.append((a, b, w))

    # Hard filter: never pick 3+ margin in tight games
    filtered: List[Tuple[int, int, float]] = []
    for h, a, w in cands:
        h, a = max(0, min(6, h)), max(0, min(6, a))
        if tight and abs(h - a) >= 2:
            continue
        if tight and (h + a) >= 7:
            continue
        # Avoid 3-1 specifically when probabilities are close
        if edge < 0.12 and (h, a) in {(3, 1), (1, 3), (4, 1), (1, 4), (5, 1), (1, 5)}:
            continue
        filtered.append((h, a, w))
    if not filtered:
        filtered = [(1, 1, 1.0), (2, 1, 0.5), (1, 2, 0.5), (2, 2, 0.4)]

    merged: Dict[Tuple[int, int], float] = {}
    for h, a, w in filtered:
        merged[(h, a)] = max(merged.get((h, a), 0.0), float(w))
    items = list(merged.items())
    total = sum(w for _, w in items) or 1.0
    r = rng.random() * total
    acc = 0.0
    human = (stat_h, stat_a)
    for (h, a), w in items:
        acc += w
        if r <= acc:
            human = (h, a)
            break
    return human[0], human[1], stat_h, stat_a


def pick_scoreline_for_probs(
    tops: List[Dict[str, Any]],
    ph: float,
    pd: float,
    pa: float,
) -> Tuple[int, int]:
    """Pick primary scoreline aligned with blended 1X2, not raw Poisson mode."""
    if not tops:
        return 1, 1
    outcome = max(("H", ph), ("D", pd), ("A", pa), key=lambda x: x[1])[0]

    def ok(h: int, a: int) -> bool:
        if outcome == "H":
            return h > a
        if outcome == "A":
            return a > h
        return h == a

    for s in tops:
        h, a = int(s["home"]), int(s["away"])
        if ok(h, a):
            return h, a
    # Fallback: force a plausible score for the outcome
    if outcome == "H":
        return 2, 1
    if outcome == "A":
        return 1, 2
    return 1, 1

def top_scorelines(mat: List[List[float]], n: int = 5) -> List[Dict[str, Any]]:
    """Top-N (home, away, p) from the score matrix — not only the mode."""
    cells: List[Tuple[float, int, int]] = []
    for i, row in enumerate(mat):
        for j, p in enumerate(row):
            cells.append((float(p), i, j))
    cells.sort(key=lambda x: -x[0])
    return [
        {"home": i, "away": j, "p": round(p, 4)}
        for p, i, j in cells[:n]
    ]


def lambdas_from_elo_and_features(
    elo_h: float, elo_a: float, features: Dict[str, Any],
    params: Optional[Dict[str, float]] = None,
) -> Tuple[float, float]:
    params = params or get_active_params()
    home_adv_elo = float(params.get("home_adv_elo", HOME_ADV_ELO))
    home_adv_goals = float(params.get("home_adv_goals", HOME_ADV_GOALS))
    avg_goals = float(params.get("avg_goals", AVG_GOALS))
    # Elo difference → expected goal edge
    diff = (elo_h + home_adv_elo) - elo_a
    # map ~400 elo to ~0.6 goals
    edge = diff / 500.0
    lam_h = avg_goals + home_adv_goals + edge / 2
    lam_a = avg_goals - home_adv_goals - edge / 2

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

    params = get_active_params()
    lam_h, lam_a = lambdas_from_elo_and_features(elo_h, elo_a, features, params=params)
    rho = float(params.get("rho", RHO))
    # Mild λ nudge from market O/U 2.5 when present (prefer market to drive scoring level)
    extra = match.get("extra") or {}
    if isinstance(extra, str):
        try:
            import json as _json
            extra = _json.loads(extra)
        except Exception:
            extra = {}
    try:
        from app.consensus import market_from_extra, clubelo_consensus_for_match, blend_probs
        from app.consensus import get_cached_fixtures, get_cached_elo, maybe_seed_elo_from_clubelo
        from app.consensus import experts_consensus_for_match
    except Exception:
        market_from_extra = None  # type: ignore
        experts_consensus_for_match = None  # type: ignore

    market = market_from_extra(extra) if market_from_extra else None
    if market and market.get("p_over_25") is not None:
        # Market O/U strongly informs expected goals (fixes 1-1 mode collapse)
        target_total = 2.2 + 2.0 * (float(market["p_over_25"]) - 0.45)
        target_total = max(1.8, min(3.6, target_total))
        cur = lam_h + lam_a
        if cur > 0.1 and target_total > 0:
            scale = max(0.75, min(1.45, target_total / cur))
            lam_h *= scale
            lam_a *= scale
            # Also nudge from 1X2 market share of goals
            if market.get("p_home") is not None:
                edge = (float(market["p_home"]) - float(market["p_away"])) * 0.35
                lam_h = max(0.35, min(3.2, lam_h + edge / 2))
                lam_a = max(0.30, min(3.0, lam_a - edge / 2))
            else:
                lam_h = max(0.35, min(3.2, lam_h))
                lam_a = max(0.30, min(3.0, lam_a))

    mat = score_matrix(lam_h, lam_a, rho=rho)
    ph_m, pd_m, pa_m = outcome_probs(mat)
    ph_m, pd_m, pa_m = apply_calibration(ph_m, pd_m, pa_m, params)
    tops = top_scorelines(mat, n=5)
    sh, sa = (tops[0]["home"], tops[0]["away"]) if tops else most_likely_score(mat)
    home_adv_elo = float(params.get("home_adv_elo", HOME_ADV_ELO))

    # --- Consensus: ClubElo + market ---
    consensus: Dict[str, Any] = {}
    clubelo = None
    try:
        # Seed Elo from ClubElo when names match (best-effort; no crash)
        try:
            maybe_seed_elo_from_clubelo(home, away, date, get_cached_elo(date))
            # refresh local elo after possible seed
            elo_h = ensure_elo(hn, ck)
            elo_a = ensure_elo(an, ck)
        except Exception:
            pass
        clubelo = clubelo_consensus_for_match(
            home, away, date,
            fixtures=get_cached_fixtures(),
            elo_map=get_cached_elo(date),
        )
        if clubelo:
            consensus["clubelo"] = clubelo
            # If ClubElo provides exact scores, merge into top_scorelines display
            ce_scores = clubelo.get("top_scorelines") or []
            if ce_scores:
                # Blend model + ClubElo exact-score mass for diversity
                merged: Dict[Tuple[int, int], float] = {}
                for s in tops:
                    merged[(int(s["home"]), int(s["away"]))] = 0.55 * float(s["p"])
                for s in ce_scores[:8]:
                    key = (int(s["home"]), int(s["away"]))
                    merged[key] = merged.get(key, 0.0) + 0.45 * float(s["p"])
                tops = [
                    {"home": h, "away": a, "p": round(p, 4)}
                    for (h, a), p in sorted(merged.items(), key=lambda x: -x[1])[:5]
                ]
                sh, sa = tops[0]["home"], tops[0]["away"]
    except Exception as e:
        consensus["clubelo_error"] = f"{type(e).__name__}: {e}"

    if market:
        consensus["market"] = market

    experts = None
    try:
        if experts_consensus_for_match:
            experts = experts_consensus_for_match(
                home, away, date, match_id=match.get("id")
            )
            if experts:
                consensus["experts"] = experts
    except Exception as e:
        consensus["experts_error"] = f"{type(e).__name__}: {e}"

    # Media headlines (RSS) — informational only, not blended into probs
    try:
        from app import db as _db
        notes = _db.media_notes_for_teams(home, away, limit=6)
        if notes:
            consensus["media"] = [
                {
                    "title": n.get("title"),
                    "source": n.get("source"),
                    "url": n.get("url"),
                    "published_at": n.get("published_at"),
                    "matched_keywords": n.get("matched_keywords"),
                }
                for n in notes
            ]
    except Exception:
        pass

    blend_info: Dict[str, Any] = {"blended": False, "sources": ["model"], "weights": {"model": 1.0}}
    ph, pd, pa = ph_m, pd_m, pa_m
    try:
        (ph, pd, pa), blend_info = blend_probs(
            (ph_m, pd_m, pa_m), clubelo, market, weights=params, experts=experts
        )
    except Exception as e:
        consensus["blend_error"] = f"{type(e).__name__}: {e}"
        ph, pd, pa = ph_m, pd_m, pa_m

    consensus["blend"] = blend_info
    consensus["model_only"] = {
        "p_home": round(ph_m, 4),
        "p_draw": round(pd_m, 4),
        "p_away": round(pa_m, 4),
    }

    # --- Open-source innovations: Pi-ratings + quantum-inspired Born interference ---
    try:
        from app.pi_ratings import pi_match_probs, expected_goal_diff
        pi = pi_match_probs(home, away)
        consensus["pi_ratings"] = {
            "p_home": round(pi["p_home"], 4),
            "p_draw": round(pi["p_draw"], 4),
            "p_away": round(pi["p_away"], 4),
            "expected_gd": pi.get("expected_gd"),
            "source": pi.get("source"),
        }
        # Mild blend toward Pi (open Constantinou-Fenton)
        w_pi = 0.18
        ph = (1 - w_pi) * ph + w_pi * float(pi["p_home"])
        pd = (1 - w_pi) * pd + w_pi * float(pi["p_draw"])
        pa = (1 - w_pi) * pa + w_pi * float(pi["p_away"])
        s = ph + pd + pa
        ph, pd, pa = ph / s, pd / s, pa / s
        blend_info = dict(blend_info)
        blend_info["sources"] = list(blend_info.get("sources") or []) + ["pi_ratings"]
        blend_info["blended"] = True
        blend_info.setdefault("weights", {})["pi_ratings"] = w_pi
    except Exception as e:
        consensus["pi_error"] = f"{type(e).__name__}: {e}"
        pi = {"expected_gd": 0.0}

    try:
        from app.quantum_inspired import quantum_inspired_probs, blend_with_quantum
        hf = features.get("home_form") or {}
        af = features.get("away_form") or {}
        form_edge = 0.0
        if hf.get("ppg") is not None and af.get("ppg") is not None:
            form_edge = float(hf["ppg"]) - float(af["ppg"])
        q = quantum_inspired_probs(
            ph, pd, pa,
            elo_edge=(elo_h + home_adv_elo) - elo_a,
            pi_gd=float((pi or {}).get("expected_gd") or 0.0),
            form_edge=form_edge,
            market_ph=(market or {}).get("p_home"),
            market_pd=(market or {}).get("p_draw"),
            market_pa=(market or {}).get("p_away"),
            league_high_scoring=(ck in {"ger_bl1", "ned_ere"}),
        )
        consensus["quantum_inspired"] = {
            "p_home": round(q["p_home"], 4),
            "p_draw": round(q["p_draw"], 4),
            "p_away": round(q["p_away"], 4),
            "method": q.get("method"),
            "note": q.get("note"),
            "amplitudes": q.get("amplitudes"),
        }
        ph, pd, pa = blend_with_quantum((ph, pd, pa), q, weight_q=0.22)
        blend_info = dict(blend_info)
        blend_info["sources"] = list(blend_info.get("sources") or []) + ["quantum_inspired"]
        blend_info["blended"] = True
        blend_info.setdefault("weights", {})["quantum_inspired"] = 0.22
        consensus["blend"] = blend_info
    except Exception as e:
        consensus["quantum_error"] = f"{type(e).__name__}: {e}"

    # Statistical mode (aligned to 1X2) + human narrative pick (diversified, stable per match)
    if not tops:
        tops = top_scorelines(mat, n=10)
    stat_h, stat_a = pick_scoreline_for_probs(tops, ph, pd, pa)
    sh, sa, stat_h2, stat_a2 = human_scoreline(
        lam_h, lam_a, ph, pd, pa, tops,
        match_id=match.get("id"),
        competition_key=ck,
        home_hist=home_hist,
        away_hist=away_hist,
        home=home,
        away=away,
    )
    # Keep stat_* from pick_scoreline (more consistent with 1X2 than tops[0] alone)
    stat_h, stat_a = stat_h, stat_a
    _ = (stat_h2, stat_a2)
    primary_p = next((float(t["p"]) for t in tops if int(t["home"]) == sh and int(t["away"]) == sa), None)
    if primary_p is None:
        # estimate rough mass near λ
        primary_p = float(poisson_pmf(sh, lam_h) * poisson_pmf(sa, lam_a))
    stat_p = next((float(t["p"]) for t in tops if int(t["home"]) == stat_h and int(t["away"]) == stat_a), 0.1)
    # Put human first in display list, then statistical, then others
    rest = [t for t in tops if not (
        (int(t["home"]) == sh and int(t["away"]) == sa) or
        (int(t["home"]) == stat_h and int(t["away"]) == stat_a)
    )]
    tops = [{"home": sh, "away": sa, "p": round(float(primary_p), 4), "kind": "human"}]
    if (stat_h, stat_a) != (sh, sa):
        tops.append({"home": stat_h, "away": stat_a, "p": round(float(stat_p), 4), "kind": "stat"})
    tops.extend(rest)
    tops = tops[:6]

    # Feature factor breakdown for UI
    factors = [
        {"name": "Home Elo", "value": round(elo_h, 1), "impact": "strength"},
        {"name": "Away Elo", "value": round(elo_a, 1), "impact": "strength"},
        {"name": "Elo edge (home+HA)", "value": round((elo_h + home_adv_elo) - elo_a, 1), "impact": "positive" if elo_h + home_adv_elo > elo_a else "negative"},
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
    if blend_info.get("blended"):
        factors.append({
            "name": "Consensus blend",
            "value": "/".join(f"{k}={v}" for k, v in (blend_info.get("weights") or {}).items()),
            "impact": "consensus",
        })

    features["factors"] = factors
    features["elo"] = {"home": elo_h, "away": elo_a, "home_norm": hn, "away_norm": an}
    features["top_scorelines"] = tops
    features["consensus"] = consensus

    features["stat_score"] = {"home": stat_h, "away": stat_a}
    features["human_score"] = {"home": sh, "away": sa}
    pred = {
        "p_home": round(ph, 4),
        "p_draw": round(pd, 4),
        "p_away": round(pa, 4),
        "score_home": sh,
        "score_away": sa,
        "stat_score_home": stat_h,
        "stat_score_away": stat_a,
        "lambda_home": round(lam_h, 4),
        "lambda_away": round(lam_a, 4),
        "model": "elo_poisson_dixon_coles_human",
        "top_scorelines": tops,
        "consensus": consensus,
        "features": features,
    }
    if persist and match.get("id"):
        db.save_features(int(match["id"]), features)
        db.save_prediction(int(match["id"]), pred)
        # Snapshot scheduled (pre-kickoff) probs for later calibration
        if (match.get("status") or "").upper() in ("SCHEDULED", "TIMED", "NS", ""):
            try:
                db.log_prediction_snapshot(int(match["id"]), pred, params)
            except Exception:
                pass
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
        try:
            from app.consensus import warm_clubelo_caches, fetch_sports_rss
            warm_clubelo_caches()
            fetch_sports_rss(force=False)
        except Exception:
            pass
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
