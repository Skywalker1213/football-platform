"""Pi-ratings (Constantinou & Fenton) — open football-specific rating system.

Reference (open literature / open-source ports such as penaltyblog):
  Constantinou & Fenton (2013), "Determining the level of ability of football
  teams by dynamic ratings based on the relative discrepancies in scores".

Innovations vs Elo:
  - separate home & away ratings per team
  - goal margin matters (diminishing returns)
  - zero-sum relative scale (0 = average)
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from app import db
from app.collector_core import _norm_team

# Published-style defaults used by open ports
LR = 0.04          # learning rate λ
GAMMA = 0.7        # cross-venue spillover
DRAW_BASE = 0.28   # baseline draw mass for pi→probs


def _squash(err: float) -> float:
    """Diminishing returns on large goal margins (open-source common form)."""
    return math.copysign(math.log1p(abs(err)), err)


def get_pi(team_norm: str) -> Tuple[float, float]:
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT home_pi, away_pi FROM team_pi WHERE team_norm=?", (team_norm,)
        ).fetchone()
    if not row:
        return 0.0, 0.0
    return float(row["home_pi"]), float(row["away_pi"])


def set_pi(team_norm: str, home_pi: float, away_pi: float) -> None:
    now = db._now() if hasattr(db, "_now") else None
    from datetime import datetime, timezone
    ts = now or datetime.now(timezone.utc).isoformat()
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO team_pi(team_norm, home_pi, away_pi, updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(team_norm) DO UPDATE SET
              home_pi=excluded.home_pi,
              away_pi=excluded.away_pi,
              updated_at=excluded.updated_at
            """,
            (team_norm, home_pi, away_pi, ts),
        )


def update_pi(home: str, away: str, hs: int, aws: int, lr: float = LR, gamma: float = GAMMA) -> None:
    hn, an = _norm_team(home), _norm_team(away)
    hh, ha = get_pi(hn)
    ah, aa = get_pi(an)
    exp = hh - aa  # home team's home rating vs away team's away rating
    obs = float(hs - aws)
    err = _squash(obs - exp)
    set_pi(hn, hh + lr * err, ha + lr * err * gamma)
    set_pi(an, ah - lr * err * gamma, aa - lr * err)


def rebuild_pi() -> int:
    with db.get_db() as conn:
        conn.execute("DELETE FROM team_pi")
    finished = db.all_finished_chronological()
    for m in finished:
        update_pi(m["home"], m["away"], int(m["home_score"]), int(m["away_score"]))
    return len(finished)


def expected_goal_diff(home: str, away: str) -> float:
    hh, _ = get_pi(_norm_team(home))
    _, aa = get_pi(_norm_team(away))
    return hh - aa


def pi_match_probs(home: str, away: str) -> Dict[str, float]:
    """Map pi expected goal-diff to 1X2 via logistic ordered-style split."""
    gd = expected_goal_diff(home, away)
    # Softmax-ish over {away, draw, home} on latent z=gd
    # P(home) rises with gd; draw peaks near 0
    z = gd
    # two thresholds around 0
    t1, t2 = -0.45, 0.45
    def sig(x: float) -> float:
        return 1.0 / (1.0 + math.exp(-x))
    p_away = sig(t1 - z)
    p_home = 1.0 - sig(t2 - z)
    p_draw = max(0.05, 1.0 - p_home - p_away)
    s = p_home + p_draw + p_away
    return {
        "p_home": p_home / s,
        "p_draw": p_draw / s,
        "p_away": p_away / s,
        "expected_gd": round(gd, 4),
        "home_pi_home": get_pi(_norm_team(home))[0],
        "away_pi_away": get_pi(_norm_team(away))[1],
        "source": "pi_ratings_constantinou_fenton",
    }


def shin_probs(odds_h: float, odds_d: float, odds_a: float) -> Optional[Dict[str, float]]:
    """Shin (1993) implied probabilities — open market-efficiency correction.

    Better than raw 1/odds renormalization when books shade favorites.
    """
    try:
        o = [float(odds_h), float(odds_d), float(odds_a)]
    except Exception:
        return None
    if any(x <= 1.01 for x in o):
        return None
    inv = [1.0 / x for x in o]
    z = sum(inv)
    # Shin fixed-point for insider trading parameter
    # Solve for z_shin in (1, z): sum( (sqrt(z^2 + 4*(1-z)*inv_i) - z) / (2*(1-z)) ) = 1
    # Iterate
    if z <= 1.0001:
        p = [x / z for x in inv]
        return {"p_home": p[0], "p_draw": p[1], "p_away": p[2], "method": "basic"}

    def f(zz: float) -> float:
        if abs(1 - zz) < 1e-9:
            return 0.0
        s = 0.0
        for qi in inv:
            s += (math.sqrt(zz * zz + 4 * (1 - zz) * qi) - zz) / (2 * (1 - zz))
        return s - 1.0

    lo, hi = 1.0, z
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
    zz = 0.5 * (lo + hi)
    ps = []
    for qi in inv:
        ps.append((math.sqrt(zz * zz + 4 * (1 - zz) * qi) - zz) / (2 * (1 - zz)))
    s = sum(ps) or 1.0
    return {
        "p_home": ps[0] / s,
        "p_draw": ps[1] / s,
        "p_away": ps[2] / s,
        "method": "shin1993",
        "z": round(zz, 5),
    }
