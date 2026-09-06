"""Prediction vs result scorecard + Quant-Striker fine-tune.

Builds on app.learning (does not replace Elo/Poisson grid search).
"""
from __future__ import annotations

import copy
import json
import math
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app import db

HKT = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
SUMMARY_PATH = ROOT / "data" / "scorecard_summary.json"
BASELINE_BRIER = 2.0 / 3.0  # uniform 1/3 over H/D/A

FACTOR_KEYS = ("form30", "playerImpact", "geo", "commercial", "stakes")
NUDGE_STEPS = (-0.15, -0.10, -0.05, 0.05, 0.10, 0.15)


def _outcome_1x2(hs: int, aws: int) -> str:
    if hs > aws:
        return "H"
    if hs < aws:
        return "A"
    return "D"


def _pred_1x2(ph: float, pd: float, pa: float) -> str:
    return max([("H", ph), ("D", pd), ("A", pa)], key=lambda x: x[1])[0]


def _onehot(label: str) -> Tuple[float, float, float]:
    return {
        "H": (1.0, 0.0, 0.0),
        "D": (0.0, 1.0, 0.0),
        "A": (0.0, 0.0, 1.0),
    }[label]


def _brier(ph: float, pd: float, pa: float, actual: str) -> float:
    oh, od, oa = _onehot(actual)
    return (ph - oh) ** 2 + (pd - od) ** 2 + (pa - oa) ** 2


def _log_loss(ph: float, pd: float, pa: float, actual: str, eps: float = 1e-15) -> float:
    probs = {"H": ph, "D": pd, "A": pa}
    p = max(eps, min(1.0 - eps, float(probs[actual])))
    return -math.log(p)


def _extract_qs(features: Any) -> Dict[str, Any]:
    if isinstance(features, str):
        try:
            features = json.loads(features)
        except json.JSONDecodeError:
            features = {}
    if not isinstance(features, dict):
        return {}
    qs = features.get("quant_striker")
    if not qs:
        cons = features.get("consensus") or {}
        if isinstance(cons, dict):
            qs = cons.get("quant_striker")
    return qs if isinstance(qs, dict) else {}


def _qs_call_label(qs: Dict[str, Any]) -> Optional[str]:
    system = qs.get("system") or {}
    call = system.get("result_call") or system.get("outcome")
    if not call:
        return None
    c = str(call).strip().upper()
    if c in ("H", "1", "HOME", "主", "主勝"):
        return "H"
    if c in ("A", "2", "AWAY", "客", "客勝"):
        return "A"
    if c in ("D", "X", "DRAW", "和", "和局"):
        return "D"
    # e.g. "Home win" / team names — fall back to argmax of system probs if present
    op = system.get("outcome")
    if op in ("H", "D", "A"):
        return op
    return None


def score_row_from_finished(row: Dict[str, Any]) -> Dict[str, Any]:
    hs = int(row["home_score"] if "home_score" in row else row["actual_score_home"])
    aws = int(row["away_score"] if "away_score" in row else row["actual_score_away"])
    ph, pd, pa = float(row["p_home"]), float(row["p_draw"]), float(row["p_away"])
    pred_sh = row.get("pred_score_home", row.get("score_home"))
    pred_sa = row.get("pred_score_away", row.get("score_away"))
    actual = _outcome_1x2(hs, aws)
    pred = _pred_1x2(ph, pd, pa)
    hit_1x2 = 1 if pred == actual else 0
    hit_score = 0
    if pred_sh is not None and pred_sa is not None:
        hit_score = 1 if int(pred_sh) == hs and int(pred_sa) == aws else 0
    feats = row.get("features") or row.get("features_json") or {}
    qs = _extract_qs(feats)
    qs_conf = qs.get("confidence")
    qs_call = _qs_call_label(qs)
    qs_hit = None
    if qs_call is not None:
        qs_hit = 1 if qs_call == actual else 0
    return {
        "match_id": int(row["id"] if "id" in row else row["match_id"]),
        "actual_1x2": actual,
        "pred_1x2": pred,
        "hit_1x2": hit_1x2,
        "hit_score": hit_score,
        "brier": round(_brier(ph, pd, pa, actual), 6),
        "log_loss": round(_log_loss(ph, pd, pa, actual), 6),
        "qs_confidence": qs_conf,
        "qs_result_call": qs_call,
        "qs_hit": qs_hit,
        "p_home": ph,
        "p_draw": pd,
        "p_away": pa,
        "pred_score_home": int(pred_sh) if pred_sh is not None else None,
        "pred_score_away": int(pred_sa) if pred_sa is not None else None,
        "actual_score_home": hs,
        "actual_score_away": aws,
        "competition_key": row.get("competition_key"),
        "league": row.get("league"),
        "country": row.get("country"),
        "home": row.get("home"),
        "away": row.get("away"),
        "match_date": row.get("date") or row.get("match_date"),
        "scored_at": datetime.now(HKT).isoformat(),
    }


def aggregate_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {
            "n": 0,
            "hit_1x2_rate": None,
            "hit_score_rate": None,
            "mean_brier": None,
            "mean_logloss": None,
            "baseline_brier": round(BASELINE_BRIER, 4),
            "brier_vs_baseline": None,
            "qs_strong": {"n": 0, "hit_rate": None, "wrong": 0},
            "by_competition": {},
            "scored_at": datetime.now(HKT).isoformat(),
        }
    hit_1 = sum(int(r["hit_1x2"] or 0) for r in rows)
    hit_s = sum(int(r["hit_score"] or 0) for r in rows)
    mean_b = sum(float(r["brier"]) for r in rows) / n
    mean_ll = sum(float(r["log_loss"]) for r in rows) / n

    strong = [r for r in rows if (r.get("qs_confidence") or "") == "STRONG"]
    strong_wrong = sum(1 for r in strong if r.get("qs_hit") == 0)
    strong_hit = sum(1 for r in strong if r.get("qs_hit") == 1)

    by_comp: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        ck = r.get("competition_key") or "unknown"
        bucket = by_comp.setdefault(ck, {"n": 0, "hit_1x2": 0, "brier_sum": 0.0})
        bucket["n"] += 1
        bucket["hit_1x2"] += int(r["hit_1x2"] or 0)
        bucket["brier_sum"] += float(r["brier"])
    by_comp_out = {
        k: {
            "n": v["n"],
            "hit_1x2_rate": round(v["hit_1x2"] / v["n"], 4) if v["n"] else None,
            "mean_brier": round(v["brier_sum"] / v["n"], 4) if v["n"] else None,
        }
        for k, v in sorted(by_comp.items(), key=lambda kv: -kv[1]["n"])
    }
    return {
        "n": n,
        "hit_1x2_rate": round(hit_1 / n, 4),
        "hit_score_rate": round(hit_s / n, 4),
        "mean_brier": round(mean_b, 4),
        "mean_logloss": round(mean_ll, 4),
        "baseline_brier": round(BASELINE_BRIER, 4),
        "brier_vs_baseline": round(BASELINE_BRIER - mean_b, 4),
        "qs_strong": {
            "n": len(strong),
            "hit_rate": round(strong_hit / len(strong), 4) if strong else None,
            "wrong": strong_wrong,
        },
        "by_competition": by_comp_out,
        "scored_at": datetime.now(HKT).isoformat(),
    }


def build_scorecard(limit: int = 500) -> Dict[str, Any]:
    """Score all finished matches that have stored predictions; persist rows + summary."""
    db.init_db()
    db.ensure_scorecard_schema()
    finished = db.finished_with_predictions(limit=limit)
    rows = [score_row_from_finished(r) for r in finished]
    for row in rows:
        db.upsert_scorecard_row(row)
    summary = aggregate_summary(rows)
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"summary": summary, "rows": rows, "path": str(SUMMARY_PATH)}


def get_summary(refresh: bool = False) -> Dict[str, Any]:
    if refresh or not SUMMARY_PATH.exists():
        return build_scorecard()["summary"]
    try:
        return json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return build_scorecard()["summary"]


def _clamp_weight(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def _local_probs_for_match(
    match: Dict[str, Any],
    cfg: Dict[str, Any],
    params: Dict[str, float],
    elos: Dict[str, float],
) -> Tuple[float, float, float]:
    """Offline QS+Poisson(+optional stored market) probs — no network."""
    from app.collector_core import _norm_team
    from app.predict import (
        elo_prior,
        lambdas_from_elo_and_features,
        score_matrix,
        outcome_probs,
        apply_calibration,
    )
    from app.quant_striker import apply_qs_to_lambdas
    from app.features import build_features_for_match

    home, away = match["home"], match["away"]
    date = match.get("date") or ""
    hn, an = _norm_team(home), _norm_team(away)
    ck = match.get("competition_key")
    elo_h = elos.get(hn, elo_prior(ck, hn))
    elo_a = elos.get(an, elo_prior(ck, an))
    home_hist = db.team_matches_before(home, date, limit=30)
    away_hist = db.team_matches_before(away, date, limit=30)
    features = build_features_for_match(match, home_hist, away_hist)

    extra = match.get("extra") or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except json.JSONDecodeError:
            extra = {}
    market = None
    try:
        from app.consensus import market_from_extra
        market = market_from_extra(extra if isinstance(extra, dict) else {})
    except Exception:
        market = None

    qs_early = apply_qs_to_lambdas(
        elo_h, elo_a, features, match, home_hist, away_hist, market=market, cfg=cfg
    )
    elo_h_use = float(qs_early["elo_home"])
    elo_a_use = float(qs_early["elo_away"])
    lam_h, lam_a = lambdas_from_elo_and_features(elo_h_use, elo_a_use, features, params=params)
    lam_h = max(0.35, min(3.2, lam_h * float(qs_early.get("lam_mul_home") or 1.0)))
    lam_a = max(0.30, min(3.0, lam_a * float(qs_early.get("lam_mul_away") or 1.0)))
    rho = float(params.get("rho", -0.05))
    mat = score_matrix(lam_h, lam_a, rho=rho)
    ph, pd, pa = outcome_probs(mat)
    ph, pd, pa = apply_calibration(ph, pd, pa, params)

    # Soft market blend using QS marketBlendWeight (local only)
    mbw = float((cfg.get("model") or {}).get("marketBlendWeight", 0.0) or 0.0)
    if market and market.get("p_home") is not None and mbw > 0:
        mh = float(market["p_home"])
        md = float(market.get("p_draw") or 0.0)
        ma = float(market["p_away"])
        s = mh + md + ma
        if s > 0:
            mh, md, ma = mh / s, md / s, ma / s
            w = _clamp_weight(mbw, 0.0, 1.0)
            ph = (1 - w) * ph + w * mh
            pd = (1 - w) * pd + w * md
            pa = (1 - w) * pa + w * ma
            z = ph + pd + pa
            if z > 0:
                ph, pd, pa = ph / z, pd / z, pa / z
    return ph, pd, pa


def _eval_qs_config_brier(cfg: Dict[str, Any], matches: List[Dict[str, Any]]) -> float:
    """Local re-score with candidate QS config (no network / no persist)."""
    os.environ.setdefault("FOOTBALL_SKIP_WEATHER", "1")
    params = db.get_model_params()
    from app.collector_core import _norm_team
    from app.predict import elo_prior

    elos: Dict[str, float] = {}
    with db.get_db() as conn:
        for r in conn.execute("SELECT team_norm, elo FROM team_elo").fetchall():
            elos[str(r["team_norm"])] = float(r["elo"])

    if not matches:
        return 1.0
    total = 0.0
    n = 0
    for m in matches:
        try:
            ph, pd, pa = _local_probs_for_match(m, cfg, params, elos)
            actual = _outcome_1x2(int(m["home_score"]), int(m["away_score"]))
            total += _brier(ph, pd, pa, actual)
            n += 1
        except Exception:
            continue
    return total / n if n else 1.0


def fine_tune_quant_striker(
    recent_n: int = 200,
    max_evals: int = 40,
) -> Dict[str, Any]:
    """Nudge factorWeights / marketBlendWeight if held-set Brier improves."""
    from app.quant_striker import load_config, save_config

    db.init_db()
    matches = db.finished_with_predictions(limit=recent_n)
    # Need full match dicts with scores for predict_match
    held: List[Dict[str, Any]] = []
    for r in matches:
        m = db.get_match(int(r["id"]))
        if m and m.get("home_score") is not None:
            held.append(m)
    if len(held) < 3:
        note = f"QS fine-tune skipped (held n={len(held)} < 3)"
        return {
            "ok": False,
            "n": len(held),
            "brier_before": None,
            "brier_after": None,
            "changed": False,
            "notes": note,
            "weights_before": None,
            "weights_after": None,
        }

    base_cfg = copy.deepcopy(load_config(force=True))
    before_brier = _eval_qs_config_brier(base_cfg, held)
    best_cfg = copy.deepcopy(base_cfg)
    best_brier = before_brier
    evals = 0
    notes: List[str] = []

    # Coordinate descent over factor weights
    weights = dict(best_cfg.get("factorWeights") or {})
    for key in FACTOR_KEYS:
        cur = float(weights.get(key, 1.0))
        for step in NUDGE_STEPS:
            if evals >= max_evals:
                break
            cand_w = _clamp_weight(cur + step, 0.0, 2.0)
            if abs(cand_w - cur) < 1e-9:
                continue
            trial = copy.deepcopy(best_cfg)
            trial.setdefault("factorWeights", {})
            trial["factorWeights"] = dict(best_cfg.get("factorWeights") or {})
            trial["factorWeights"][key] = round(cand_w, 4)
            b = _eval_qs_config_brier(trial, held)
            evals += 1
            if b < best_brier - 1e-6:
                best_brier = b
                best_cfg = trial
                weights = dict(best_cfg.get("factorWeights") or {})
                cur = float(weights.get(key, 1.0))
                notes.append(f"{key}→{cur:.2f} (brier {b:.4f})")

    # marketBlendWeight
    model = dict(best_cfg.get("model") or {})
    cur_mb = float(model.get("marketBlendWeight", 0.5))
    for step in NUDGE_STEPS:
        if evals >= max_evals:
            break
        cand = _clamp_weight(cur_mb + step, 0.0, 1.0)
        if abs(cand - cur_mb) < 1e-9:
            continue
        trial = copy.deepcopy(best_cfg)
        trial.setdefault("model", {})
        trial["model"] = dict(best_cfg.get("model") or {})
        trial["model"]["marketBlendWeight"] = round(cand, 4)
        b = _eval_qs_config_brier(trial, held)
        evals += 1
        if b < best_brier - 1e-6:
            best_brier = b
            best_cfg = trial
            cur_mb = cand
            notes.append(f"marketBlendWeight→{cand:.2f} (brier {b:.4f})")

    changed = abs(best_brier - before_brier) > 1e-6 or notes
    if best_brier < before_brier - 1e-6:
        save_config(best_cfg)
        load_config(force=True)
        # Mirror market blend into model_params so blend_probs picks it up
        try:
            params = db.get_model_params()
            params["blend_w_market"] = float(
                (best_cfg.get("model") or {}).get("marketBlendWeight", params.get("blend_w_market", 0.25))
            )
            db.set_model_params(params)
        except Exception:
            pass
        note = "QS fine-tune improved: " + (", ".join(notes) if notes else "weights updated")
        note += f"; brier {before_brier:.4f}→{best_brier:.4f} on n={len(held)} evals={evals}"
    else:
        note = f"QS fine-tune: no Brier improvement (before={before_brier:.4f}, n={len(held)}, evals={evals})"
        changed = False

    # Attach stored-prediction accuracy so index「每日學習」card stays meaningful
    sc_metrics = aggregate_summary(
        [score_row_from_finished(r) for r in db.finished_with_predictions(limit=recent_n)]
    )
    run_id = db.append_learning_run(
        finished_n=int(sc_metrics.get("n") or len(held)),
        result_acc=sc_metrics.get("hit_1x2_rate"),
        exact_acc=sc_metrics.get("hit_score_rate"),
        brier=sc_metrics.get("mean_brier"),
        params={
            "kind": "qs_finetune",
            "qs_factorWeights": (best_cfg.get("factorWeights") or {}),
            "qs_marketBlendWeight": (best_cfg.get("model") or {}).get("marketBlendWeight"),
            "eval_brier_before": round(before_brier, 4),
            "eval_brier_after": round(best_brier, 4),
            "local_qs_brier_before": round(before_brier, 4),
            "local_qs_brier_after": round(best_brier, 4),
        },
        notes=note,
    )
    return {
        "ok": True,
        "run_id": run_id,
        "n": len(held),
        "brier_before": round(before_brier, 4),
        "brier_after": round(best_brier, 4),
        "changed": bool(best_brier < before_brier - 1e-6),
        "notes": note,
        "evals": evals,
        "weights_before": {
            "factorWeights": (base_cfg.get("factorWeights") or {}),
            "marketBlendWeight": (base_cfg.get("model") or {}).get("marketBlendWeight"),
        },
        "weights_after": {
            "factorWeights": (best_cfg.get("factorWeights") or {}),
            "marketBlendWeight": (best_cfg.get("model") or {}).get("marketBlendWeight"),
        },
        "ran_at": datetime.now(HKT).isoformat(),
    }


def run_scorecard_and_finetune(recent_n: int = 200) -> Dict[str, Any]:
    """Pipeline piece: scorecard → learning grid search → QS weight nudge."""
    from app.learning import run_learning

    scored = build_scorecard(limit=recent_n)
    learning = run_learning(recent_n=recent_n)
    qs = fine_tune_quant_striker(recent_n=recent_n)
    summary = scored["summary"]
    summary["learning"] = {
        "run_id": learning.get("run_id"),
        "eval_brier_before": learning.get("eval_brier_before"),
        "eval_brier_after": learning.get("eval_brier_after"),
        "notes": learning.get("notes"),
    }
    summary["qs_finetune"] = {
        "brier_before": qs.get("brier_before"),
        "brier_after": qs.get("brier_after"),
        "changed": qs.get("changed"),
        "notes": qs.get("notes"),
        "ran_at": qs.get("ran_at"),
    }
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "scorecard": summary,
        "learning": learning,
        "qs_finetune": qs,
    }
