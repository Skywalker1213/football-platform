"""Self-learning: calibrate model params from finished matches."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app import db
from app.collector_core import _norm_team

HKT = timezone(timedelta(hours=8))

# Sane bounds and max step per learning cycle
BOUNDS = {
    "home_adv_elo": (40.0, 120.0),
    "home_adv_goals": (0.05, 0.45),
    "avg_goals": (1.15, 1.75),
    "k_factor": (12.0, 32.0),
    "rho": (-0.15, 0.05),
    "calibration_home": (0.75, 1.30),
    "calibration_draw": (0.75, 1.30),
    "calibration_away": (0.75, 1.30),
    "blend_w_model": (0.20, 0.70),
    "blend_w_clubelo": (0.10, 0.55),
    "blend_w_market": (0.05, 0.45),
    "blend_w_model_2": (0.35, 0.80),
    "blend_w_clubelo_2": (0.20, 0.65),
    "blend_w_experts": (0.05, 0.30),
}
MAX_DELTA = {
    "home_adv_elo": 12.0,
    "home_adv_goals": 0.06,
    "avg_goals": 0.10,
    "k_factor": 3.0,
    "rho": 0.03,
    "calibration_home": 0.12,
    "calibration_draw": 0.12,
    "calibration_away": 0.12,
    "blend_w_model": 0.08,
    "blend_w_clubelo": 0.08,
    "blend_w_market": 0.08,
    "blend_w_model_2": 0.08,
    "blend_w_clubelo_2": 0.08,
    "blend_w_experts": 0.05,
}


def _clamp(key: str, value: float, base: float) -> float:
    lo, hi = BOUNDS[key]
    md = MAX_DELTA[key]
    value = max(base - md, min(base + md, value))
    return max(lo, min(hi, value))


def _outcome_onehot(hs: int, aws: int) -> Tuple[float, float, float]:
    if hs > aws:
        return 1.0, 0.0, 0.0
    if hs < aws:
        return 0.0, 0.0, 1.0
    return 0.0, 1.0, 0.0


def _apply_calibration(ph: float, pd: float, pa: float, params: Dict[str, float]) -> Tuple[float, float, float]:
    ph *= float(params.get("calibration_home", 1.0))
    pd *= float(params.get("calibration_draw", 1.0))
    pa *= float(params.get("calibration_away", 1.0))
    s = ph + pd + pa
    if s <= 0:
        return 1 / 3, 1 / 3, 1 / 3
    return ph / s, pd / s, pa / s


def _metrics_from_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {"n": 0, "result_acc": None, "exact_acc": None, "brier": None}
    correct = exact = 0
    brier_sum = 0.0
    for r in rows:
        hs, aws = int(r["home_score"]), int(r["away_score"])
        oh, od, oa = _outcome_onehot(hs, aws)
        ph, pd, pa = float(r["p_home"]), float(r["p_draw"]), float(r["p_away"])
        label = max([("H", ph), ("D", pd), ("A", pa)], key=lambda x: x[1])[0]
        actual = "H" if hs > aws else ("A" if hs < aws else "D")
        if label == actual:
            correct += 1
        pred_sh = r.get("pred_score_home", r.get("score_home"))
        pred_sa = r.get("pred_score_away", r.get("score_away"))
        if pred_sh is not None and pred_sa is not None:
            if int(pred_sh) == hs and int(pred_sa) == aws:
                exact += 1
        brier_sum += (ph - oh) ** 2 + (pd - od) ** 2 + (pa - oa) ** 2
    n = len(rows)
    return {
        "n": n,
        "result_acc": round(correct / n, 4),
        "exact_acc": round(exact / n, 4),
        "brier": round(brier_sum / n, 4),
    }


def _predict_probs_for_match(
    match: Dict[str, Any],
    params: Dict[str, float],
    elo_h: float,
    elo_a: float,
) -> Tuple[float, float, float, int, int]:
    """Compute H/D/A + most-likely score using candidate params (no persist)."""
    from app.predict import score_matrix, outcome_probs, most_likely_score

    features = match.get("features") or {}
    if isinstance(features, str):
        try:
            features = json.loads(features)
        except json.JSONDecodeError:
            features = {}

    home_adv_elo = float(params["home_adv_elo"])
    home_adv_goals = float(params["home_adv_goals"])
    avg_goals = float(params["avg_goals"])
    rho = float(params.get("rho", -0.05))

    diff = (elo_h + home_adv_elo) - elo_a
    edge = diff / 500.0
    lam_h = avg_goals + home_adv_goals + edge / 2
    lam_a = avg_goals - home_adv_goals - edge / 2

    hf = features.get("home_form") or {}
    af = features.get("away_form") or {}
    if hf.get("ppg") is not None and af.get("ppg") is not None:
        form_edge = (hf["ppg"] - af["ppg"]) * 0.12
        lam_h += form_edge / 2
        lam_a -= form_edge / 2

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

    wadj = features.get("weather_scoring_adj") or 0.0
    lam_h += wadj
    lam_a += wadj
    lam_h = max(0.35, min(3.2, lam_h))
    lam_a = max(0.30, min(3.0, lam_a))

    mat = score_matrix(lam_h, lam_a, rho=rho)
    ph, pd, pa = outcome_probs(mat)
    ph, pd, pa = _apply_calibration(ph, pd, pa, params)
    sh, sa = most_likely_score(mat)
    return ph, pd, pa, sh, sa


def _elo_map() -> Dict[str, float]:
    with db.get_db() as conn:
        rows = conn.execute("SELECT team_norm, elo FROM team_elo").fetchall()
    return {str(r["team_norm"]): float(r["elo"]) for r in rows}


def _brier_for_params(
    matches: List[Dict[str, Any]],
    params: Dict[str, float],
    elos: Dict[str, float],
) -> float:
    from app.predict import elo_prior

    if not matches:
        return 1.0
    brier_sum = 0.0
    for m in matches:
        hn = _norm_team(m["home"])
        an = _norm_team(m["away"])
        ck = m.get("competition_key")
        elo_h = elos.get(hn, elo_prior(ck, hn))
        elo_a = elos.get(an, elo_prior(ck, an))
        ph, pd, pa, _, _ = _predict_probs_for_match(m, params, elo_h, elo_a)
        oh, od, oa = _outcome_onehot(int(m["home_score"]), int(m["away_score"]))
        brier_sum += (ph - oh) ** 2 + (pd - od) ** 2 + (pa - oa) ** 2
    return brier_sum / len(matches)


def _grid_search(matches: List[Dict[str, Any]], base: Dict[str, float], elos: Dict[str, float]) -> Tuple[Dict[str, float], float]:
    """Coarse grid over home_adv_goals / home_adv_elo / avg_goals; keep deltas small."""
    best = dict(base)
    best_b = _brier_for_params(matches, best, elos)

    ha_goals = [
        _clamp("home_adv_goals", base["home_adv_goals"] + d, base["home_adv_goals"])
        for d in (-0.06, -0.03, 0.0, 0.03, 0.06)
    ]
    ha_elo = [
        _clamp("home_adv_elo", base["home_adv_elo"] + d, base["home_adv_elo"])
        for d in (-12.0, -6.0, 0.0, 6.0, 12.0)
    ]
    avg_g = [
        _clamp("avg_goals", base["avg_goals"] + d, base["avg_goals"])
        for d in (-0.10, -0.05, 0.0, 0.05, 0.10)
    ]
    # Unique while preserving order
    def uniq(xs):
        seen = set()
        out = []
        for x in xs:
            k = round(x, 6)
            if k not in seen:
                seen.add(k)
                out.append(x)
        return out

    ha_goals, ha_elo, avg_g = uniq(ha_goals), uniq(ha_elo), uniq(avg_g)

    for g in ha_goals:
        for e in ha_elo:
            for a in avg_g:
                cand = dict(base)
                cand["home_adv_goals"] = g
                cand["home_adv_elo"] = e
                cand["avg_goals"] = a
                b = _brier_for_params(matches, cand, elos)
                if b < best_b - 1e-6:
                    best_b = b
                    best = cand
    return best, best_b


def _calibrate_multipliers(
    matches: List[Dict[str, Any]],
    params: Dict[str, float],
    elos: Dict[str, float],
) -> Dict[str, float]:
    """If model systematically over/under-predicts outcomes, tweak calibration_* and renormalize."""
    from app.predict import elo_prior

    if len(matches) < 8:
        return params

    sum_ph = sum_pd = sum_pa = 0.0
    sum_oh = sum_od = sum_oa = 0.0
    for m in matches:
        hn = _norm_team(m["home"])
        an = _norm_team(m["away"])
        ck = m.get("competition_key")
        elo_h = elos.get(hn, elo_prior(ck, hn))
        elo_a = elos.get(an, elo_prior(ck, an))
        # Use unit calibration for bias estimate
        p0 = dict(params)
        p0["calibration_home"] = 1.0
        p0["calibration_draw"] = 1.0
        p0["calibration_away"] = 1.0
        ph, pd, pa, _, _ = _predict_probs_for_match(m, p0, elo_h, elo_a)
        oh, od, oa = _outcome_onehot(int(m["home_score"]), int(m["away_score"]))
        sum_ph += ph
        sum_pd += pd
        sum_pa += pa
        sum_oh += oh
        sum_od += od
        sum_oa += oa

    n = len(matches)
    # Multiplier ≈ actual_rate / predicted_rate (damped)
    def ratio(actual: float, pred: float) -> float:
        if pred < 1e-6:
            return 1.0
        raw = (actual / n) / (pred / n)
        # damp toward 1.0
        return 1.0 + 0.5 * (raw - 1.0)

    out = dict(params)
    out["calibration_home"] = _clamp(
        "calibration_home",
        ratio(sum_oh, sum_ph),
        float(params.get("calibration_home", 1.0)),
    )
    out["calibration_draw"] = _clamp(
        "calibration_draw",
        ratio(sum_od, sum_pd),
        float(params.get("calibration_draw", 1.0)),
    )
    out["calibration_away"] = _clamp(
        "calibration_away",
        ratio(sum_oa, sum_pa),
        float(params.get("calibration_away", 1.0)),
    )

    # Keep only if Brier improves
    b0 = _brier_for_params(matches, params, elos)
    b1 = _brier_for_params(matches, out, elos)
    return out if b1 <= b0 else params


def run_learning(recent_n: int = 200) -> Dict[str, Any]:
    """
    Full learning cycle:
      1. rebuild Elo
      2. score stored predictions on finished matches
      3. grid-search + calibrate params to minimize Brier
      4. persist model_params + learning_runs row
    """
    from app.predict import rebuild_elo

    db.init_db()
    before_params = db.get_model_params()

    elo_n = rebuild_elo()
    rows = db.finished_with_predictions(limit=recent_n)
    stored_metrics = _metrics_from_rows(rows)

    elos = _elo_map()
    base_brier = _brier_for_params(rows, before_params, elos) if rows else None

    after_params = dict(before_params)
    searched_brier = base_brier
    notes_parts: List[str] = []

    if rows:
        after_params, searched_brier = _grid_search(rows, before_params, elos)
        after_params = _calibrate_multipliers(rows, after_params, elos)
        searched_brier = _brier_for_params(rows, after_params, elos)

        # Small optional k_factor / rho nudge based on result accuracy (very conservative)
        if stored_metrics.get("result_acc") is not None and stored_metrics["result_acc"] < 0.38:
            after_params["k_factor"] = _clamp(
                "k_factor", before_params["k_factor"] + 1.0, before_params["k_factor"]
            )
            notes_parts.append("bumped k_factor (+1) due to low result_acc")
        elif stored_metrics.get("result_acc") is not None and stored_metrics["result_acc"] > 0.52:
            after_params["k_factor"] = _clamp(
                "k_factor", before_params["k_factor"] - 0.5, before_params["k_factor"]
            )

        changed = {
            k: {"before": before_params.get(k), "after": after_params.get(k)}
            for k in after_params
            if abs(float(after_params[k]) - float(before_params.get(k, after_params[k]))) > 1e-9
        }
        if changed:
            notes_parts.append("updated: " + ", ".join(changed.keys()))
        else:
            notes_parts.append("no param change (grid found no Brier improvement)")
        if base_brier is not None and searched_brier is not None:
            notes_parts.append(
                f"eval_brier {base_brier:.4f}→{searched_brier:.4f} on n={len(rows)}"
            )
    else:
        notes_parts.append("no finished matches with predictions; kept defaults")

    # Round for stable storage / UI
    after_params = {
        k: (round(float(v), 4) if isinstance(v, float) else v)
        for k, v in after_params.items()
    }
    db.set_model_params(after_params)

    # If elo-affecting params changed, rebuild once more so ratings match new HA/K
    elo_keys = {"home_adv_elo", "k_factor"}
    if any(
        abs(float(after_params[k]) - float(before_params.get(k, 0))) > 1e-9 for k in elo_keys
    ):
        elo_n = rebuild_elo()
        notes_parts.append("rebuilt Elo after HA/K change")

    notes = "; ".join(notes_parts)
    run_id = db.append_learning_run(
        finished_n=stored_metrics["n"],
        result_acc=stored_metrics["result_acc"],
        exact_acc=stored_metrics["exact_acc"],
        brier=stored_metrics["brier"],
        params=after_params,
        notes=notes,
    )

    return {
        "run_id": run_id,
        "elo_finished": elo_n,
        "finished_n": stored_metrics["n"],
        "result_acc": stored_metrics["result_acc"],
        "exact_acc": stored_metrics["exact_acc"],
        "brier": stored_metrics["brier"],
        "eval_brier_before": round(base_brier, 4) if base_brier is not None else None,
        "eval_brier_after": round(searched_brier, 4) if searched_brier is not None else None,
        "params_before": before_params,
        "params_after": after_params,
        "notes": notes,
        "ran_at": datetime.now(HKT).isoformat(),
    }
