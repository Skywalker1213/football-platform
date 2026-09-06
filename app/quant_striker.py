"""Quant-Striker v3 ideas ported into football-platform.

Pipeline (documented choice):
  base Elo → QS composite + five-factor layer → adjust Elo/λ →
  existing Dixon–Coles Poisson → market / Pi / quantum blend →
  attach QS system card from *final* blended probs.

Attribution: local Quant-Striker v3 engine concepts (factors, confidence tiers,
EV scan). Research / education only — not betting advice. No Transfermarkt or
fragile odds scrapers; uses platform form / injuries stubs / weather / market /
fixture-load / competition heuristics.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "quant_striker.json"

_CFG_CACHE: Optional[Dict[str, Any]] = None


def load_config(force: bool = False) -> Dict[str, Any]:
    global _CFG_CACHE
    if _CFG_CACHE is not None and not force:
        return _CFG_CACHE
    if CONFIG_PATH.exists():
        _CFG_CACHE = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    else:
        _CFG_CACHE = {
            "model": {
                "eloBlend": {"realElo": 0.6, "rankElo": 0.4},
                "fallbackRating": 1500,
                "ppgEloScale": 220.0,
                "gdpgEloScale": 80.0,
            },
            "factorWeights": {
                "form30": 1.0,
                "playerImpact": 1.0,
                "geo": 1.0,
                "commercial": 1.0,
                "stakes": 1.0,
            },
            "factorCaps": {
                "form30MaxDelta": 60,
                "playerImpactMaxDelta": 40,
                "commercialMaxDelta": 30,
                "geoMaxLambdaPenalty": 0.08,
                "stakesMaxLambdaSwing": 0.05,
            },
            "form30": {"decayXi": 0.005, "baselinePpg": 1.55, "maxMatches": 30},
            "evThreshold": 0.03,
            "confidence": {"strong": 0.72, "lean": 0.58},
        }
    return _CFG_CACHE


def save_config(cfg: Dict[str, Any]) -> None:
    """Persist Quant-Striker config and refresh in-process cache."""
    global _CFG_CACHE
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _CFG_CACHE = cfg


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _r2(v: float) -> float:
    return round(float(v), 2)


def _r4(v: float) -> float:
    return round(float(v), 4)


def _neutral() -> Dict[str, Any]:
    return {
        "delta_home": 0.0,
        "delta_away": 0.0,
        "lam_mul_home": 1.0,
        "lam_mul_away": 1.0,
        "notes": [],
    }


def _parse_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _decay_form(
    team: str,
    before_date: str,
    matches: List[Dict[str, Any]],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Decay-weighted PPG / GDPG over up to ~30 finished matches (QS form30)."""
    form_cfg = cfg.get("form30") or {}
    xi = float(form_cfg.get("decayXi", 0.005))
    max_n = int(form_cfg.get("maxMatches", 30))
    cur = _parse_date(before_date)
    rows: List[Dict[str, Any]] = []
    for m in matches or []:
        if (m.get("status") or "").upper() != "FINISHED":
            continue
        if m.get("home_score") is None or m.get("away_score") is None:
            continue
        if m.get("home") != team and m.get("away") != team:
            continue
        if before_date and (m.get("date") or "") >= before_date:
            continue
        rows.append(m)
    rows.sort(key=lambda m: m.get("date") or "", reverse=True)
    rows = rows[:max_n]
    if not rows:
        return {
            "matches": 0,
            "decayed_ppg": None,
            "decayed_gdpg": None,
            "estimated": True,
        }

    w_sum = 0.0
    pts_w = 0.0
    gd_w = 0.0
    for m in rows:
        md = _parse_date(m.get("date"))
        days = 0.0
        if cur and md:
            days = max(0.0, float((cur - md).days))
        w = math.exp(-xi * days)
        hs, aws = int(m["home_score"]), int(m["away_score"])
        if m.get("home") == team:
            gf, ga = hs, aws
        else:
            gf, ga = aws, hs
        if gf > ga:
            pts = 3.0
        elif gf == ga:
            pts = 1.0
        else:
            pts = 0.0
        w_sum += w
        pts_w += w * pts
        gd_w += w * (gf - ga)
    if w_sum <= 0:
        return {
            "matches": len(rows),
            "decayed_ppg": None,
            "decayed_gdpg": None,
            "estimated": True,
        }
    return {
        "matches": len(rows),
        "decayed_ppg": pts_w / w_sum,
        "decayed_gdpg": gd_w / w_sum,
        "estimated": len(rows) < 5,
    }


def power_rank_proxy_elo(
    form: Optional[Dict[str, Any]],
    cfg: Dict[str, Any],
) -> Optional[float]:
    """Map recent PPG / GD into an Elo-scale proxy (no Transfermarkt ranks)."""
    if not form:
        return None
    ppg = form.get("ppg")
    if ppg is None and form.get("decayed_ppg") is not None:
        ppg = form["decayed_ppg"]
    if ppg is None:
        return None
    gd = form.get("gd")
    n = form.get("n") or form.get("matches") or 0
    gdpg = None
    if form.get("decayed_gdpg") is not None:
        gdpg = float(form["decayed_gdpg"])
    elif gd is not None and n:
        gdpg = float(gd) / float(n)
    else:
        gdpg = 0.0
    model = cfg.get("model") or {}
    base = float(model.get("fallbackRating", 1500))
    baseline = float((cfg.get("form30") or {}).get("baselinePpg", 1.55))
    ppg_scale = float(model.get("ppgEloScale", 220.0))
    gd_scale = float(model.get("gdpgEloScale", 80.0))
    return base + (float(ppg) - baseline) * ppg_scale + float(gdpg) * gd_scale


def composite_rating(
    real_elo: float,
    form: Optional[Dict[str, Any]],
    cfg: Dict[str, Any],
) -> Tuple[float, Dict[str, Any]]:
    blend = (cfg.get("model") or {}).get("eloBlend") or {}
    w_real = float(blend.get("realElo", 0.6))
    w_rank = float(blend.get("rankElo", 0.4))
    proxy = power_rank_proxy_elo(form, cfg)
    meta = {"real_elo": _r2(real_elo), "rank_proxy": None, "blend": {"real": w_real, "rank": w_rank}}
    if proxy is None:
        meta["note"] = "rank proxy unavailable — using real Elo only"
        return float(real_elo), meta
    meta["rank_proxy"] = _r2(proxy)
    return w_real * float(real_elo) + w_rank * float(proxy), meta


# ---------------------------------------------------------------------------
# Five factors
# ---------------------------------------------------------------------------

def factor_form30(
    home_form30: Dict[str, Any],
    away_form30: Dict[str, Any],
    home_name: str,
    away_name: str,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    out = _neutral()
    cap = float((cfg.get("factorCaps") or {}).get("form30MaxDelta", 60))
    base = float((cfg.get("form30") or {}).get("baselinePpg", 1.55))

    def score(f: Dict[str, Any]) -> Optional[float]:
        if not f or not f.get("matches") or f.get("decayed_ppg") is None:
            return None
        return 0.6 * (float(f["decayed_ppg"]) - base) / 0.8 + 0.4 * float(f.get("decayed_gdpg") or 0.0) / 1.2

    sh, sa = score(home_form30), score(away_form30)
    if sh is not None:
        out["delta_home"] = _clamp(cap * math.tanh(sh), -cap, cap)
    if sa is not None:
        out["delta_away"] = _clamp(cap * math.tanh(sa), -cap, cap)
    if sh is not None or sa is not None:
        est = (home_form30 or {}).get("estimated") or (away_form30 or {}).get("estimated")
        note = (
            f"Form-30 (decay-weighted): {home_name} {out['delta_home']:+.1f} / "
            f"{away_name} {out['delta_away']:+.1f} Elo-pts"
        )
        if est:
            note += " [sparse history — estimated]"
        out["notes"].append(note)
    else:
        out["notes"].append("Form-30: insufficient finished history — delta=0")
    return out


def factor_player_impact(
    features: Dict[str, Any],
    home_name: str,
    away_name: str,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Injuries / player_form proxies. Stubs → delta 0 + estimated note."""
    out = _neutral()
    cap = float((cfg.get("factorCaps") or {}).get("playerImpactMaxDelta", 40))
    ih = features.get("injuries_home") or {}
    ia = features.get("injuries_away") or {}
    ph = features.get("player_form_home") or {}
    pa = features.get("player_form_away") or {}

    def impact(inj: Dict[str, Any], pform: Dict[str, Any], label: str) -> Tuple[float, List[str]]:
        notes: List[str] = []
        unavailable = inj.get("unavailable") or inj.get("injuries") or []
        players = pform.get("players") or []
        if (inj.get("status") == "stub" and pform.get("status") == "stub") or (
            not unavailable and not players
        ):
            notes.append(f"{label}: no injury/player-form feed — estimated delta=0")
            return 0.0, notes
        missing = 0.0
        hot = 0.0
        for p in unavailable:
            share = float(p.get("goalShare") or p.get("goal_share") or 0.08)
            missing += share
            notes.append(f"{label}: {p.get('name', 'player')} OUT")
        for p in players:
            if p.get("hot") or p.get("form") == "hot":
                share = float(p.get("goalShare") or p.get("goal_share") or 0.08)
                hot += 0.25 * share
                notes.append(f"{label}: {p.get('name', 'player')} hot form")
        d = _clamp(-cap * missing * 2 + cap * hot, -cap, cap)
        return d, notes

    dh, nh = impact(ih, ph, home_name)
    da, na = impact(ia, pa, away_name)
    out["delta_home"] = dh
    out["delta_away"] = da
    out["notes"].extend(nh + na)
    out["estimated"] = True
    return out


def factor_geo(features: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Weather / venue proxies → λ multipliers (cap ~8%)."""
    out = _neutral()
    max_pen = float((cfg.get("factorCaps") or {}).get("geoMaxLambdaPenalty", 0.08))
    wadj = float(features.get("weather_scoring_adj") or 0.0)
    weather = features.get("weather") or {}
    # Map mild scoring adj (±~0.08 goals) into proportional λ penalty/bonus
    # Negative wadj (rain/wind) suppresses both sides' λ.
    if weather.get("status") == "live" or wadj != 0.0:
        # Convert absolute adj into [0,1] penalty fraction of max_pen
        # wadj typical range ~[-0.08, 0]; also heat from temperature
        pen = 0.0
        why: List[str] = []
        precip = weather.get("precipitation_mm") or 0
        wind = weather.get("windspeed_kmh") or 0
        temp = weather.get("temperature_c")
        if precip and float(precip) > 2:
            pen += min(1.0, float(precip) / 10.0) * 0.5
            why.append(f"precip {precip}mm")
        if wind and float(wind) > 35:
            pen += min(1.0, (float(wind) - 35) / 40.0) * 0.35
            why.append(f"wind {wind}km/h")
        if temp is not None and float(temp) >= 30:
            pen += min(1.0, (float(temp) - 28) / 12.0) * 0.4
            why.append(f"heat {temp}°C")
        if temp is not None and float(temp) <= 0:
            pen += min(1.0, (0 - float(temp)) / 10.0) * 0.35
            why.append(f"cold {temp}°C")
        # If only scoring_adj without live weather detail
        if not why and wadj < 0:
            pen = min(1.0, abs(wadj) / 0.08)
            why.append(f"weather_scoring_adj={wadj}")
        mul = 1.0 - _clamp(pen, 0.0, 1.0) * max_pen
        out["lam_mul_home"] = mul
        out["lam_mul_away"] = mul
        if why:
            out["notes"].append(f"Geo — {', '.join(why)} (λ ×{mul:.3f})")
        else:
            out["notes"].append("Geo — live weather neutral / no significant edge")
    else:
        out["notes"].append("Geo — weather deferred/unavailable — λ ×1.00")
    return out


def factor_commercial(
    market: Optional[Dict[str, Any]],
    model_probs: Optional[Tuple[float, float, float]],
    home_name: str,
    away_name: str,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Mild market disagreement / value-gap signal (no squad-value scrape)."""
    out = _neutral()
    cap = float((cfg.get("factorCaps") or {}).get("commercialMaxDelta", 30))
    if not market or market.get("p_home") is None:
        out["notes"].append("Commercial — no market odds; delta=0")
        return out
    mph = float(market["p_home"])
    mpd = float(market.get("p_draw") or 0.0)
    mpa = float(market["p_away"])
    if model_probs:
        ph, pd, pa = model_probs
        # Disagreement: if market favours home more than a neutral mid, mild Elo toward market favourite
        # Use gap between market home edge and model home edge
        m_edge = mph - mpa
        mod_edge = ph - pa
        gap = m_edge - mod_edge
        d = _clamp(cap * math.tanh(gap / 0.25), -cap, cap)
        out["delta_home"] = d / 2
        out["delta_away"] = -d / 2
        side = home_name if d >= 0 else away_name
        out["notes"].append(
            f"Commercial — market vs model edge gap {gap:+.3f} → {d:+.1f} Elo toward {side}"
        )
        # Steam proxy: if market favourite strongly disagrees with model top pick
        if abs(gap) > 0.08:
            out["steam"] = {"side": side, "magnitude": abs(gap)}
            out["notes"].append(
                f"Market steam/disagreement: {abs(gap)*100:.1f}pp lean toward {side} "
                "(research signal only)"
            )
    else:
        # Mild signal from market alone vs 1/3
        edge = mph - mpa
        d = _clamp(cap * 0.35 * math.tanh(edge / 0.3), -cap * 0.5, cap * 0.5)
        out["delta_home"] = d / 2
        out["delta_away"] = -d / 2
        out["notes"].append(
            f"Commercial — mild market favourite signal {edge:+.3f} (no model gap yet)"
        )
    _ = mpd  # keep draw available for future steam open/close
    return out


def factor_stakes(
    match: Dict[str, Any],
    features: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Competition importance + fixture-load rotation heuristic."""
    out = _neutral()
    swing = float((cfg.get("factorCaps") or {}).get("stakesMaxLambdaSwing", 0.05))
    ck = (match.get("competition_key") or "") or ""
    league = (match.get("league") or "").lower()
    hl = features.get("home_load") or {}
    al = features.get("away_load") or {}

    knockout_keys = {"uefa_ucl"}
    knockout_words = ("final", "semi", "quarter", "round of", "knockout", "play-off", "playoff")
    is_knockout = ck in knockout_keys and any(w in league for w in knockout_words)
    # UCL group/league phase still high stakes but not single-elim
    is_ucl = ck == "uefa_ucl"
    is_intl = ck.startswith("int_")

    if is_knockout:
        out["notes"].append(
            "Stakes: knockout / elimination — both sides maximum motivation (λ neutral)"
        )
    elif is_ucl:
        out["lam_mul_home"] = 1.0 + swing * 0.4
        out["lam_mul_away"] = 1.0 + swing * 0.4
        out["notes"].append(
            f"Stakes: UCL — elevated motivation (λ ×{out['lam_mul_home']:.3f})"
        )
    elif is_intl and "fr" in ck:
        # Friendlies: mild rotation / lower intensity
        out["lam_mul_home"] = 1.0 - swing * 0.6
        out["lam_mul_away"] = 1.0 - swing * 0.6
        out["notes"].append(
            f"Stakes: international friendly — lower intensity (λ ×{out['lam_mul_home']:.3f})"
        )
    else:
        out["notes"].append("Stakes: domestic league baseline")

    # Congestion → rotation risk (λ down for congested side)
    def cong(load: Dict[str, Any], side: str) -> None:
        m7 = int(load.get("matches_last_7d") or 0)
        rest = load.get("rest_days")
        if m7 >= 2 or (rest is not None and rest <= 3):
            mul = 1.0 - swing
            if side == "home":
                out["lam_mul_home"] *= mul
            else:
                out["lam_mul_away"] *= mul
            out["notes"].append(
                f"Stakes/load: {side} congestion (7d={m7}, rest={rest}) λ ×{mul:.3f}"
            )

    cong(hl, "home")
    cong(al, "away")
    return out


def apply_factors(
    match: Dict[str, Any],
    features: Dict[str, Any],
    home_form30: Dict[str, Any],
    away_form30: Dict[str, Any],
    market: Optional[Dict[str, Any]],
    model_probs: Optional[Tuple[float, float, float]],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    home = match.get("home") or "Home"
    away = match.get("away") or "Away"
    weights = cfg.get("factorWeights") or {}
    registry = [
        (
            "form30",
            "Form (last 30, decay-weighted)",
            factor_form30(home_form30, away_form30, home, away, cfg),
        ),
        (
            "playerImpact",
            "Key-player impact",
            factor_player_impact(features, home, away, cfg),
        ),
        ("geo", "Geo-conditions", factor_geo(features, cfg)),
        (
            "commercial",
            "Commercial (market disagreement)",
            factor_commercial(market, model_probs, home, away, cfg),
        ),
        ("stakes", "Stakes & fixture-load", factor_stakes(match, features, cfg)),
    ]
    delta_h = delta_a = 0.0
    mul_h = mul_a = 1.0
    breakdown: List[Dict[str, Any]] = []
    for key, label, raw in registry:
        w = float(weights.get(key, 1.0))
        d_h = float(raw["delta_home"]) * w
        d_a = float(raw["delta_away"]) * w
        m_h = 1.0 + (float(raw["lam_mul_home"]) - 1.0) * w
        m_a = 1.0 + (float(raw["lam_mul_away"]) - 1.0) * w
        delta_h += d_h
        delta_a += d_a
        mul_h *= m_h
        mul_a *= m_a
        entry = {
            "key": key,
            "label": label,
            "delta_home": _r2(d_h),
            "delta_away": _r2(d_a),
            "lam_mul_home": _r4(m_h),
            "lam_mul_away": _r4(m_a),
            "notes": list(raw.get("notes") or []),
            "weight": w,
        }
        if raw.get("estimated"):
            entry["estimated"] = True
        if raw.get("steam"):
            entry["steam"] = raw["steam"]
        breakdown.append(entry)
    return {
        "delta_home": delta_h,
        "delta_away": delta_a,
        "lam_mul_home": mul_h,
        "lam_mul_away": mul_a,
        "breakdown": breakdown,
    }


def top_score_for_outcome(
    tops: List[Dict[str, Any]],
    outcome: str,
) -> Optional[Dict[str, Any]]:
    for s in tops or []:
        h, a = int(s["home"]), int(s["away"])
        ok = (outcome == "H" and h > a) or (outcome == "A" and a > h) or (outcome == "D" and h == a)
        if ok:
            return {"home": h, "away": a, "p": float(s.get("p") or 0), "scoreline": f"{h}-{a}"}
    fallback = {"H": (2, 1), "D": (1, 1), "A": (1, 2)}[outcome]
    return {"home": fallback[0], "away": fallback[1], "p": None, "scoreline": f"{fallback[0]}-{fallback[1]}"}


def confidence_tier(p: float, cfg: Dict[str, Any]) -> str:
    conf = cfg.get("confidence") or {}
    strong = float(conf.get("strong", 0.72))
    lean = float(conf.get("lean", 0.58))
    if p >= strong:
        return "STRONG"
    if p >= lean:
        return "LEAN"
    return "TOSS-UP"


def system_prediction(
    ph: float,
    pd: float,
    pa: float,
    home: str,
    away: str,
    tops: Optional[List[Dict[str, Any]]],
    cfg: Dict[str, Any],
    rating_home: Optional[float] = None,
    rating_away: Optional[float] = None,
) -> Dict[str, Any]:
    picks = [("H", ph), ("D", pd), ("A", pa)]
    picks.sort(key=lambda x: -x[1])
    pick, pick_p = picks[0]
    # Advance-style confidence: for league matches use top outcome; for knockout-ish
    # also tilt draws via ratings (QS advanceProbs).
    rh = float(rating_home or 1500)
    ra = float(rating_away or 1500)
    shootout_home = 0.5 + 0.1 * math.tanh((rh - ra) / 200.0)
    adv_home = ph + pd * shootout_home
    adv_away = pa + pd * (1.0 - shootout_home)
    adv_p = max(adv_home, adv_away)
    # Confidence uses max(top 1X2, advance fav) so league STRONG can fire on clear favourites
    conf_p = max(pick_p, adv_p)
    tier = confidence_tier(conf_p, cfg)
    score = top_score_for_outcome(tops or [], pick)
    result_call = home if pick == "H" else (away if pick == "A" else "Draw")
    return {
        "outcome": pick,
        "outcome_p": _r4(pick_p),
        "result_call": result_call,
        "scoreline": score["scoreline"] if score else None,
        "scoreline_p": _r4(score["p"]) if score and score.get("p") is not None else None,
        "advancer": home if adv_home >= adv_away else away,
        "advancer_p": _r4(adv_p),
        "confidence": tier,
        "confidence_p": _r4(conf_p),
    }


def ev_scan(
    probs: Tuple[float, float, float],
    market: Optional[Dict[str, Any]],
    threshold: float = 0.03,
) -> Dict[str, Any]:
    """Positive EV vs decimal odds (research flag only — not betting advice)."""
    if not market:
        return {"available": False, "flags": [], "note": "no market odds"}
    odds = market.get("odds") or {}
    oh, od, oa = odds.get("H"), odds.get("D"), odds.get("A")
    if not oh or not od or not oa:
        return {"available": False, "flags": [], "note": "incomplete decimal odds"}
    ph, pd, pa = probs
    try:
        oh_f, od_f, oa_f = float(oh), float(od), float(oa)
    except (TypeError, ValueError):
        return {"available": False, "flags": [], "note": "bad odds"}
    ev = {
        "H": _r4(ph * oh_f - 1.0),
        "D": _r4(pd * od_f - 1.0),
        "A": _r4(pa * oa_f - 1.0),
    }
    labels = {"H": "Home", "D": "Draw", "A": "Away"}
    flags = []
    for k in ("H", "D", "A"):
        if ev[k] > threshold:
            flags.append({"outcome": k, "label": labels[k], "ev": ev[k]})
    flags.sort(key=lambda x: -x["ev"])
    return {
        "available": True,
        "ev": ev,
        "threshold": threshold,
        "flags": flags,
        "value_flag": flags[0] if flags else None,
        "note": "Research only — positive EV ≠ likely winner; not betting advice",
    }


def build_quant_striker(
    match: Dict[str, Any],
    elo_home: float,
    elo_away: float,
    features: Dict[str, Any],
    home_hist: Optional[List[Dict[str, Any]]] = None,
    away_hist: Optional[List[Dict[str, Any]]] = None,
    market: Optional[Dict[str, Any]] = None,
    model_probs: Optional[Tuple[float, float, float]] = None,
    final_probs: Optional[Tuple[float, float, float]] = None,
    tops: Optional[List[Dict[str, Any]]] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Full QS card: factors + adjusted Elo + system (from final probs) + EV."""
    cfg = cfg or load_config()
    date = match.get("date") or ""
    home = match.get("home") or ""
    away = match.get("away") or ""

    home_form30 = _decay_form(home, date, home_hist or [], cfg)
    away_form30 = _decay_form(away, date, away_hist or [], cfg)

    # Prefer decay form for power-rank; fall back to features.home_form
    hf = dict(features.get("home_form") or {})
    af = dict(features.get("away_form") or {})
    if home_form30.get("decayed_ppg") is not None:
        hf = {
            **hf,
            "ppg": home_form30["decayed_ppg"],
            "decayed_ppg": home_form30["decayed_ppg"],
            "decayed_gdpg": home_form30.get("decayed_gdpg"),
            "matches": home_form30.get("matches"),
            "n": home_form30.get("matches"),
        }
    if away_form30.get("decayed_ppg") is not None:
        af = {
            **af,
            "ppg": away_form30["decayed_ppg"],
            "decayed_ppg": away_form30["decayed_ppg"],
            "decayed_gdpg": away_form30.get("decayed_gdpg"),
            "matches": away_form30.get("matches"),
            "n": away_form30.get("matches"),
        }

    base_h, meta_h = composite_rating(elo_home, hf, cfg)
    base_a, meta_a = composite_rating(elo_away, af, cfg)

    fx = apply_factors(
        match, features, home_form30, away_form30, market, model_probs, cfg
    )
    adj_h = base_h + fx["delta_home"]
    adj_a = base_a + fx["delta_away"]

    notes: List[str] = []
    for b in fx["breakdown"]:
        notes.extend(b.get("notes") or [])
    notes.append(
        "Pipeline: base Elo → QS factors → Poisson → market/Pi/quantum blend → "
        "QS system card from final probs"
    )
    notes.append(str(cfg.get("attribution") or "Quant-Striker v3 concepts (local port)"))

    probs = final_probs or model_probs or (1 / 3, 1 / 3, 1 / 3)
    system = system_prediction(
        probs[0], probs[1], probs[2], home, away, tops, cfg, adj_h, adj_a
    )
    thr = float(cfg.get("evThreshold", 0.03))
    ev = ev_scan(probs, market, threshold=thr)

    return {
        "version": "qs-v3-port",
        "factors": fx["breakdown"],
        "factor_totals": {
            "delta_home": _r2(fx["delta_home"]),
            "delta_away": _r2(fx["delta_away"]),
            "lam_mul_home": _r4(fx["lam_mul_home"]),
            "lam_mul_away": _r4(fx["lam_mul_away"]),
        },
        "adjusted_elo": {
            "home_base": _r2(base_h),
            "away_base": _r2(base_a),
            "home": _r2(adj_h),
            "away": _r2(adj_a),
            "home_meta": meta_h,
            "away_meta": meta_a,
        },
        "form30": {"home": home_form30, "away": away_form30},
        "system": system,
        "confidence": system["confidence"],
        "ev": ev,
        "notes": notes,
    }


def apply_qs_to_lambdas(
    elo_home: float,
    elo_away: float,
    features: Dict[str, Any],
    match: Dict[str, Any],
    home_hist: List[Dict[str, Any]],
    away_hist: List[Dict[str, Any]],
    market: Optional[Dict[str, Any]] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Early-stage: composite + factors → adjusted Elo and λ multipliers for Poisson."""
    cfg = cfg or load_config()
    date = match.get("date") or ""
    home = match.get("home") or ""
    away = match.get("away") or ""
    home_form30 = _decay_form(home, date, home_hist or [], cfg)
    away_form30 = _decay_form(away, date, away_hist or [], cfg)
    hf = dict(features.get("home_form") or {})
    af = dict(features.get("away_form") or {})
    if home_form30.get("decayed_ppg") is not None:
        hf.update(
            {
                "ppg": home_form30["decayed_ppg"],
                "decayed_ppg": home_form30["decayed_ppg"],
                "decayed_gdpg": home_form30.get("decayed_gdpg"),
                "matches": home_form30.get("matches"),
                "n": home_form30.get("matches"),
            }
        )
    if away_form30.get("decayed_ppg") is not None:
        af.update(
            {
                "ppg": away_form30["decayed_ppg"],
                "decayed_ppg": away_form30["decayed_ppg"],
                "decayed_gdpg": away_form30.get("decayed_gdpg"),
                "matches": away_form30.get("matches"),
                "n": away_form30.get("matches"),
            }
        )
    base_h, meta_h = composite_rating(elo_home, hf, cfg)
    base_a, meta_a = composite_rating(elo_away, af, cfg)
    # Commercial without model probs yet (mild market-only)
    fx = apply_factors(
        match, features, home_form30, away_form30, market, None, cfg
    )
    return {
        "elo_home": base_h + fx["delta_home"],
        "elo_away": base_a + fx["delta_away"],
        "lam_mul_home": fx["lam_mul_home"],
        "lam_mul_away": fx["lam_mul_away"],
        "fx": fx,
        "form30": {"home": home_form30, "away": away_form30},
        "base_meta": {"home": meta_h, "away": meta_a},
        "base_home": base_h,
        "base_away": base_a,
        "cfg": cfg,
    }
