"""Free consensus sources: ClubElo + market-implied odds.

ClubElo documented API (http://clubelo.com/API):
  - http://api.clubelo.com/Fixtures — upcoming match probs (may be deactivated)
  - http://api.clubelo.com/YYYY-MM-DD — daily Elo ratings

Market odds from football-data.co.uk CSV fields stored in match.extra
(AvgH/AvgD/AvgA or B365H/D/A). Overround removed → implied 1X2.

Never scrape Forebet / PredictZ / Sofascore / Flashscore / tipster sites.
"""
from __future__ import annotations

import csv
import io
import math
import re
import time
import unicodedata
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.collector_core import CACHE_DIR, http_get_text, _norm_team

HKT = timezone(timedelta(hours=8))
CLUBELO_FIXTURES_URL = "http://api.clubelo.com/Fixtures"
CLUBELO_ELO_URL = "http://api.clubelo.com/{date}"
# Prefer ≥6h polite cache (collector default is 6h; we force cache key reuse)
CLUBELO_CACHE_MIN_SEC = 6 * 60 * 60
# Negative cache: avoid hammering deactivated / 502 endpoints every match
CLUBELO_NEG_CACHE_SEC = 3 * 60 * 60


def _write_neg_cache(path: Path, payload: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
    except OSError:
        pass


def _read_if_fresh(path: Path, ttl: float) -> Optional[str]:
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > ttl:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


# Default blend weights (also seeded into model_params for learning)
DEFAULT_BLEND = {
    "blend_w_model": 0.40,
    "blend_w_clubelo": 0.35,
    "blend_w_market": 0.25,
    # When only model + clubelo (no market)
    "blend_w_model_2": 0.55,
    "blend_w_clubelo_2": 0.45,
    "blend_w_experts": 0.15,
}

_SUFFIX_RE = re.compile(
    r"\b(fc|cf|afc|sc|fk|sk|ac|sv|as|ssc|rcd|ud|cd|sd|bsc|vfl|vfb|tsv|fsv|1\.?)\b",
    re.I,
)


def normalize_team_name(name: Optional[str]) -> str:
    """Normalize for fuzzy ClubElo matching: accents, FC/CF, punctuation."""
    s = (name or "").strip()
    if not s:
        return ""
    # Strip accents
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = s.replace("&", " and ")
    s = s.replace("ü", "u").replace("ö", "o").replace("ä", "a").replace("ß", "ss")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = _SUFFIX_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Reuse platform aliases via _norm_team
    return _norm_team(s) if s else ""


def _token_set(s: str) -> set:
    return {t for t in s.split() if len(t) > 1}


def fuzzy_team_match(query: str, candidates: List[str], min_score: float = 0.72) -> Optional[Tuple[str, float]]:
    """Return best (candidate, score) or None. Score in [0,1]."""
    qn = normalize_team_name(query)
    if not qn:
        return None
    qtok = _token_set(qn)
    best: Optional[Tuple[str, float]] = None
    for cand in candidates:
        cn = normalize_team_name(cand)
        if not cn:
            continue
        if qn == cn:
            return cand, 1.0
        # substring / containment
        if qn in cn or cn in qn:
            score = min(len(qn), len(cn)) / max(len(qn), len(cn))
            score = max(score, 0.85)
        else:
            ctok = _token_set(cn)
            if not qtok or not ctok:
                continue
            inter = len(qtok & ctok)
            union = len(qtok | ctok)
            jacc = inter / union if union else 0.0
            # bonus if all query tokens appear
            cover = inter / len(qtok) if qtok else 0.0
            score = 0.55 * jacc + 0.45 * cover
        if best is None or score > best[1]:
            best = (cand, score)
    if best and best[1] >= min_score:
        return best
    return None


def _safe_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    s = str(x).strip()
    if not s or s.upper() in ("NA", "N/A", "-", "NULL"):
        return None
    try:
        v = float(s)
        if not math.isfinite(v) or v <= 0:
            return None
        return v
    except (TypeError, ValueError):
        return None


def odds_to_implied(h: float, d: float, a: float) -> Tuple[float, float, float]:
    """Convert decimal odds → implied probs with overround removed."""
    ih, id_, ia = 1.0 / h, 1.0 / d, 1.0 / a
    s = ih + id_ + ia
    if s <= 0:
        return 1 / 3, 1 / 3, 1 / 3
    return ih / s, id_ / s, ia / s


def market_from_extra(extra: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Extract market-implied 1X2 from match.extra odds fields."""
    if not extra or not isinstance(extra, dict):
        return None
    # Prefer average odds, fall back to Bet365
    triples = [
        ("AvgH", "AvgD", "AvgA", "avg"),
        ("B365H", "B365D", "B365A", "b365"),
        ("MaxH", "MaxD", "MaxA", "max"),
    ]
    for hk, dk, ak, label in triples:
        h, d, a = _safe_float(extra.get(hk)), _safe_float(extra.get(dk)), _safe_float(extra.get(ak))
        if h and d and a:
            ph, pd, pa = odds_to_implied(h, d, a)
            out: Dict[str, Any] = {
                "source": "football-data.co.uk",
                "book": label,
                "odds": {"H": h, "D": d, "A": a},
                "p_home": round(ph, 4),
                "p_draw": round(pd, 4),
                "p_away": round(pa, 4),
                "method": "inverse_renorm",
            }
            # Shin (1993) open-source market correction when available
            try:
                from app.pi_ratings import shin_probs
                shin = shin_probs(h, d, a)
                if shin:
                    out["shin"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in shin.items()}
                    # Prefer Shin for blending when present
                    out["p_home"] = round(float(shin["p_home"]), 4)
                    out["p_draw"] = round(float(shin["p_draw"]), 4)
                    out["p_away"] = round(float(shin["p_away"]), 4)
                    out["method"] = shin.get("method", "shin1993")
            except Exception:
                pass
            # Optional O/U 2.5 for λ nudging
            ou_over = _safe_float(extra.get("Avg>2.5")) or _safe_float(extra.get("B365>2.5"))
            ou_under = _safe_float(extra.get("Avg<2.5")) or _safe_float(extra.get("B365<2.5"))
            if ou_over and ou_under:
                io, iu = 1.0 / ou_over, 1.0 / ou_under
                s = io + iu
                out["p_over_25"] = round(io / s, 4)
                out["p_under_25"] = round(iu / s, 4)
            return out
    return None


def _clubelo_cache_ok(path: Path) -> bool:
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < CLUBELO_CACHE_MIN_SEC


def fetch_clubelo_fixtures(force: bool = False) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fetch ClubElo Fixtures CSV. Returns (rows, meta). Never raises."""
    meta: Dict[str, Any] = {"ok": False, "url": CLUBELO_FIXTURES_URL}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / "clubelo_fixtures.csv"
    neg_file = CACHE_DIR / "clubelo_fixtures.neg"
    try:
        if not force:
            neg = _read_if_fresh(neg_file, CLUBELO_NEG_CACHE_SEC)
            if neg and "deactivated" in neg.lower():
                meta["error"] = "fixtures_api_deactivated"
                meta["message"] = neg.strip()[:200]
                meta["cache"] = "neg_hit"
                return [], meta
        raw = None
        if not force and _clubelo_cache_ok(cache_file):
            raw = cache_file.read_text(encoding="utf-8")
            meta["cache"] = "hit"
        else:
            raw = http_get_text(CLUBELO_FIXTURES_URL, use_cache=True)
            meta["cache"] = "fetch"
            if raw:
                try:
                    cache_file.write_text(raw, encoding="utf-8")
                except OSError:
                    pass
        if not raw:
            meta["error"] = "empty_response"
            _write_neg_cache(neg_file, "empty_response")
            return [], meta
        head = raw.strip()[:80].lower()
        if "deactivated" in head or "not available" in head:
            meta["error"] = "fixtures_api_deactivated"
            meta["message"] = raw.strip()[:200]
            _write_neg_cache(neg_file, raw.strip()[:200])
            return [], meta
        if "home" not in head and "date" not in head:
            # might still be CSV with header on first line
            pass
        rows = list(csv.DictReader(io.StringIO(raw)))
        if not rows:
            meta["error"] = "no_rows"
            meta["message"] = raw.strip()[:200]
            return [], meta
        meta["ok"] = True
        meta["n"] = len(rows)
        meta["columns"] = list(rows[0].keys())
        return rows, meta
    except Exception as e:
        meta["error"] = f"{type(e).__name__}: {e}"
        return [], meta


def fetch_clubelo_elo(day: Optional[str] = None, force: bool = False) -> Tuple[Dict[str, float], Dict[str, Any]]:
    """Fetch daily ClubElo ratings → {club_name: elo}. Never raises."""
    if not day:
        day = datetime.now(HKT).date().isoformat()
    meta: Dict[str, Any] = {"ok": False, "date": day, "url": CLUBELO_ELO_URL.format(date=day)}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"clubelo_elo_{day}.csv"
    try:
        raw = None
        if not force and _clubelo_cache_ok(cache_file):
            raw = cache_file.read_text(encoding="utf-8")
            meta["cache"] = "hit"
        else:
            neg_file = CACHE_DIR / f"clubelo_elo_{day}.neg"
            if not force:
                neg = _read_if_fresh(neg_file, CLUBELO_NEG_CACHE_SEC)
                if neg:
                    meta["error"] = neg.strip()[:120]
                    meta["cache"] = "neg_hit"
                    return {}, meta
            # Try requested day, then one day back only (502s are slow)
            candidates = [day]
            try:
                base = date.fromisoformat(day)
                candidates.append((base - timedelta(days=1)).isoformat())
            except ValueError:
                pass
            last_err = None
            for d in candidates:
                url = CLUBELO_ELO_URL.format(date=d)
                try:
                    raw = http_get_text(url, use_cache=True)
                except Exception as e:
                    last_err = e
                    raw = None
                head = (raw or "")[:300]
                if raw and ("Rank" in head or "Club" in head or "Elo" in head or (len(raw) > 100 and "," in raw.split("\n", 1)[0])):
                    meta["date"] = d
                    meta["url"] = url
                    break
                raw = None
            if not raw and last_err:
                meta["error"] = str(last_err)
            meta["cache"] = "fetch"
            if raw:
                try:
                    cache_file.write_text(raw, encoding="utf-8")
                except OSError:
                    pass
            else:
                _write_neg_cache(neg_file, meta.get("error") or "empty_response")
        if not raw:
            meta.setdefault("error", "empty_response")
            return {}, meta
        if "bad gateway" in raw.lower() or raw.strip().startswith("<"):
            meta["error"] = "bad_gateway_or_html"
            return {}, meta
        rows = list(csv.DictReader(io.StringIO(raw)))
        if not rows:
            meta["error"] = "no_rows"
            return {}, meta
        # Column names vary: Club / team, Elo / elo
        elo_map: Dict[str, float] = {}
        for r in rows:
            club = r.get("Club") or r.get("club") or r.get("Team") or r.get("team")
            elo_s = r.get("Elo") or r.get("elo") or r.get("EloRating")
            if not club:
                continue
            try:
                elo_map[str(club).strip()] = float(elo_s)
            except (TypeError, ValueError):
                continue
        meta["ok"] = bool(elo_map)
        meta["n"] = len(elo_map)
        return elo_map, meta
    except Exception as e:
        meta["error"] = f"{type(e).__name__}: {e}"
        return {}, meta


def _parse_score_cols(row: Dict[str, str]) -> List[Dict[str, Any]]:
    """Extract exact score probs from ClubElo fixture row (cols like '1-0', '2-1')."""
    scores: List[Dict[str, Any]] = []
    for k, v in row.items():
        if not k:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", k.strip())
        if not m:
            continue
        p = _safe_float(v)
        if p is None:
            continue
        # ClubElo sometimes stores percent 0-100; normalize if needed
        if p > 1.0:
            p = p / 100.0
        scores.append({"home": int(m.group(1)), "away": int(m.group(2)), "p": p})
    scores.sort(key=lambda x: -x["p"])
    return scores


def _gd_to_1x2(row: Dict[str, str]) -> Optional[Tuple[float, float, float]]:
    """Sum GD columns into home/draw/away if present."""
    ph = pd = pa = 0.0
    found = False
    for k, v in row.items():
        if not k:
            continue
        ks = k.strip()
        p = _safe_float(v)
        if p is None:
            continue
        if p > 1.0:
            p = p / 100.0
        # GD-5 … GD5, GD<-5, GD>5, or plain -5..5
        m = re.fullmatch(r"GD([+-]?\d+)", ks, re.I)
        m2 = re.fullmatch(r"([+-]?\d+)", ks)
        if ks.upper() in ("GD<-5", "GD<-5.0"):
            pa += p
            found = True
            continue
        if ks.upper() in ("GD>5", "GD>+5"):
            ph += p
            found = True
            continue
        gd = None
        if m:
            gd = int(m.group(1))
        elif m2 and ks.lstrip("+-").isdigit():
            gd = int(m2.group(1))
        if gd is None:
            continue
        found = True
        if gd > 0:
            ph += p
        elif gd == 0:
            pd += p
        else:
            pa += p
    if not found:
        return None
    s = ph + pd + pa
    if s <= 0:
        return None
    return ph / s, pd / s, pa / s


def _elo_expected_1x2(elo_h: float, elo_a: float, hfa: float = 65.0) -> Tuple[float, float, float]:
    """Rough ClubElo-style 1X2 from Elo (logistic + draw mass). Fallback when Fixtures down."""
    diff = (elo_h + hfa) - elo_a
    # P(home beats away) ignoring draws — ClubElo Elo%
    p_home_nd = 1.0 / (1.0 + 10 ** (-diff / 400.0))
    # Insert draw: higher when teams close
    closeness = math.exp(-((diff / 200.0) ** 2))
    p_draw = 0.18 + 0.10 * closeness  # ~18–28%
    p_home = p_home_nd * (1.0 - p_draw)
    p_away = (1.0 - p_home_nd) * (1.0 - p_draw)
    s = p_home + p_draw + p_away
    return p_home / s, p_draw / s, p_away / s


def find_clubelo_fixture(
    home: str,
    away: str,
    match_date: Optional[str],
    fixtures: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if not fixtures:
        return None
    # Build candidate name list once
    homes = []
    for r in fixtures:
        h = r.get("Home") or r.get("home") or r.get("Team") or ""
        a = r.get("Away") or r.get("away") or ""
        homes.append((r, str(h), str(a)))

    # Filter by date if column present
    dated = []
    for r, h, a in homes:
        rd = (r.get("Date") or r.get("date") or "").strip()
        if match_date and rd:
            # ClubElo dates often YYYY-MM-DD
            if rd[:10] != match_date[:10] and rd != match_date:
                # also try DD/MM/YYYY
                ok = False
                for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
                    try:
                        if datetime.strptime(rd[:10] if len(rd) >= 10 and fmt.startswith("%Y") else rd, fmt).date().isoformat() == match_date[:10]:
                            ok = True
                            break
                    except ValueError:
                        continue
                if not ok:
                    continue
        dated.append((r, h, a))
    pool = dated if dated else [(r, h, a) for r, h, a in homes]

    best = None
    best_score = 0.0
    for r, h, a in pool:
        mh = fuzzy_team_match(home, [h])
        ma = fuzzy_team_match(away, [a])
        if not mh or not ma:
            continue
        score = (mh[1] + ma[1]) / 2.0
        if score > best_score:
            best_score = score
            best = r
    if best and best_score >= 0.72:
        return best
    return None


def clubelo_consensus_for_match(
    home: str,
    away: str,
    match_date: Optional[str] = None,
    fixtures: Optional[List[Dict[str, Any]]] = None,
    elo_map: Optional[Dict[str, float]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Build consensus.clubelo for one match.
    Prefer Fixtures exact scores + GD; else Elo-derived 1X2 if names match.
    """
    if fixtures is None:
        fixtures, _fx_meta = fetch_clubelo_fixtures()
    else:
        _fx_meta = {"ok": bool(fixtures), "provided": True}

    row = find_clubelo_fixture(home, away, match_date, fixtures or [])
    if row:
        scores = _parse_score_cols(row)
        gd_probs = _gd_to_1x2(row)
        # Direct H/D/A columns if present
        ph = _safe_float(row.get("HomeWin") or row.get("p_home") or row.get("H"))
        pd = _safe_float(row.get("Draw") or row.get("p_draw") or row.get("D"))
        pa = _safe_float(row.get("AwayWin") or row.get("p_away") or row.get("A"))
        if ph and pd and pa and ph <= 1.5 and pd <= 1.5:
            if ph > 1 or pd > 1 or pa > 1:
                ph, pd, pa = ph / 100.0, pd / 100.0, pa / 100.0
            s = ph + pd + pa
            ph, pd, pa = ph / s, pd / s, pa / s
        elif gd_probs:
            ph, pd, pa = gd_probs
        elif scores:
            ph = pd = pa = 0.0
            for sc in scores:
                p = float(sc["p"])
                if sc["home"] > sc["away"]:
                    ph += p
                elif sc["home"] == sc["away"]:
                    pd += p
                else:
                    pa += p
            s = ph + pd + pa
            if s > 0:
                ph, pd, pa = ph / s, pd / s, pa / s
            else:
                ph = pd = pa = 1 / 3
        else:
            return None
        top = [
            {"home": int(s["home"]), "away": int(s["away"]), "p": round(float(s["p"]), 4)}
            for s in scores[:8]
        ]
        # renormalize top display probs if needed
        return {
            "source": "clubelo",
            "mode": "fixtures",
            "matched_home": row.get("Home") or row.get("home"),
            "matched_away": row.get("Away") or row.get("away"),
            "p_home": round(ph, 4),
            "p_draw": round(pd, 4),
            "p_away": round(pa, 4),
            "top_scorelines": top,
            "fixtures_meta": {k: _fx_meta.get(k) for k in ("ok", "error", "n", "cache")},
        }

    # Fallback: Elo ratings → soft 1X2 (no exact scores)
    if elo_map is None:
        elo_map, elo_meta = fetch_clubelo_elo(match_date)
    else:
        elo_meta = {"ok": bool(elo_map), "provided": True}
    if not elo_map:
        return None
    names = list(elo_map.keys())
    mh = fuzzy_team_match(home, names)
    ma = fuzzy_team_match(away, names)
    if not mh or not ma:
        return None
    eh, ea = elo_map[mh[0]], elo_map[ma[0]]
    ph, pd, pa = _elo_expected_1x2(eh, ea)
    return {
        "source": "clubelo",
        "mode": "elo_fallback",
        "matched_home": mh[0],
        "matched_away": ma[0],
        "match_score": round((mh[1] + ma[1]) / 2, 3),
        "elo_home": round(eh, 1),
        "elo_away": round(ea, 1),
        "p_home": round(ph, 4),
        "p_draw": round(pd, 4),
        "p_away": round(pa, 4),
        "top_scorelines": [],
        "note": "Fixtures API unavailable; 1X2 derived from ClubElo ratings",
        "elo_meta": {k: elo_meta.get(k) for k in ("ok", "error", "n", "cache", "date")},
    }


def blend_probs(
    model: Tuple[float, float, float],
    clubelo: Optional[Dict[str, Any]],
    market: Optional[Dict[str, Any]],
    weights: Optional[Dict[str, float]] = None,
    experts: Optional[Dict[str, Any]] = None,
) -> Tuple[Tuple[float, float, float], Dict[str, Any]]:
    """
    Blend available sources. Weights renormalized over present sources.
    Experts (manual tips) get modest weight (~0.15) when present.
    Returns ((ph,pd,pa), blend_info).
    """
    w = dict(DEFAULT_BLEND)
    if weights:
        w.update({k: float(v) for k, v in weights.items() if v is not None})

    parts: List[Tuple[str, Tuple[float, float, float], float]] = []
    has_c = bool(clubelo and clubelo.get("p_home") is not None)
    has_m = bool(market and market.get("p_home") is not None)
    has_e = bool(experts and experts.get("p_home") is not None)

    # Base model / clubelo / market allocation (same as before), then add experts
    if has_c and has_m:
        parts.append(("model", model, float(w.get("blend_w_model", 0.40))))
        parts.append(
            (
                "clubelo",
                (float(clubelo["p_home"]), float(clubelo["p_draw"]), float(clubelo["p_away"])),
                float(w.get("blend_w_clubelo", 0.35)),
            )
        )
        parts.append(
            (
                "market",
                (float(market["p_home"]), float(market["p_draw"]), float(market["p_away"])),
                float(w.get("blend_w_market", 0.25)),
            )
        )
    elif has_c:
        parts.append(("model", model, float(w.get("blend_w_model_2", 0.55))))
        parts.append(
            (
                "clubelo",
                (float(clubelo["p_home"]), float(clubelo["p_draw"]), float(clubelo["p_away"])),
                float(w.get("blend_w_clubelo_2", 0.45)),
            )
        )
    elif has_m:
        wm = float(w.get("blend_w_model", 0.40)) + float(w.get("blend_w_clubelo", 0.35)) * 0.5
        wmk = float(w.get("blend_w_market", 0.25)) + float(w.get("blend_w_clubelo", 0.35)) * 0.5
        parts.append(("model", model, wm))
        parts.append(
            (
                "market",
                (float(market["p_home"]), float(market["p_draw"]), float(market["p_away"])),
                wmk,
            )
        )
    else:
        parts.append(("model", model, 1.0))

    if has_e:
        parts.append(
            (
                "experts",
                (float(experts["p_home"]), float(experts["p_draw"]), float(experts["p_away"])),
                float(w.get("blend_w_experts", 0.15)),
            )
        )

    if len(parts) == 1 and parts[0][0] == "model":
        return model, {"blended": False, "sources": ["model"], "weights": {"model": 1.0}}

    tw = sum(p[2] for p in parts)
    if tw <= 0:
        return model, {"blended": False, "sources": ["model"], "weights": {"model": 1.0}}

    ph = pd = pa = 0.0
    used_w: Dict[str, float] = {}
    for name, (h, d, a), wt in parts:
        nw = wt / tw
        used_w[name] = round(nw, 4)
        ph += nw * h
        pd += nw * d
        pa += nw * a
    s = ph + pd + pa
    if s > 0:
        ph, pd, pa = ph / s, pd / s, pa / s
    return (ph, pd, pa), {
        "blended": True,
        "sources": list(used_w.keys()),
        "weights": used_w,
    }


def maybe_seed_elo_from_clubelo(
    home: str,
    away: str,
    match_date: Optional[str] = None,
    elo_map: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """If fuzzy match succeeds, override/seed team_elo with ClubElo values."""
    from app import db

    if elo_map is None:
        elo_map, meta = fetch_clubelo_elo(match_date)
    else:
        meta = {"ok": True}
    out: Dict[str, Any] = {"seeded": [], "meta": meta}
    if not elo_map:
        return out
    names = list(elo_map.keys())
    for team in (home, away):
        m = fuzzy_team_match(team, names)
        if not m:
            continue
        club, score = m
        elo = float(elo_map[club])
        tn = _norm_team(team)
        # Only seed if we have few local matches or large gap
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT elo, matches_played FROM team_elo WHERE team_norm=?", (tn,)
            ).fetchone()
        if row is None or int(row["matches_played"] or 0) < 8:
            db.set_elo(tn, elo, int(row["matches_played"]) if row else 0)
            out["seeded"].append({"team": team, "clubelo": club, "elo": elo, "score": score})
        elif abs(float(row["elo"]) - elo) > 120 and int(row["matches_played"] or 0) < 20:
            # Soft pull toward ClubElo
            blended = 0.6 * float(row["elo"]) + 0.4 * elo
            db.set_elo(tn, blended, int(row["matches_played"]))
            out["seeded"].append(
                {"team": team, "clubelo": club, "elo": blended, "score": score, "soft": True}
            )
    return out


# Module-level cache for one predict_all pass
_FX_CACHE: Optional[List[Dict[str, Any]]] = None
_ELO_CACHE: Optional[Dict[str, float]] = None
_ELO_CACHE_DATE: Optional[str] = None


def warm_clubelo_caches(match_date: Optional[str] = None) -> Dict[str, Any]:
    global _FX_CACHE, _ELO_CACHE, _ELO_CACHE_DATE
    fx, fx_meta = fetch_clubelo_fixtures()
    _FX_CACHE = fx
    elo, elo_meta = fetch_clubelo_elo(match_date)
    _ELO_CACHE = elo
    _ELO_CACHE_DATE = elo_meta.get("date") or match_date
    return {"fixtures": fx_meta, "elo": elo_meta}


def get_cached_fixtures() -> List[Dict[str, Any]]:
    global _FX_CACHE
    if _FX_CACHE is None:
        _FX_CACHE, _ = fetch_clubelo_fixtures()
    return _FX_CACHE or []


def get_cached_elo(match_date: Optional[str] = None) -> Dict[str, float]:
    global _ELO_CACHE, _ELO_CACHE_DATE
    if _ELO_CACHE is None:
        _ELO_CACHE, meta = fetch_clubelo_elo(match_date)
        _ELO_CACHE_DATE = meta.get("date") or match_date
    return _ELO_CACHE or {}


def experts_consensus_for_match(
    home: str,
    away: str,
    match_date: Optional[str] = None,
    match_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Aggregate manual expert_tips into soft 1X2 probs (majority + confidence)."""
    from app import db

    tips = db.expert_tips_for_match(home, away, match_date=match_date, match_id=match_id)
    if not tips:
        return None
    votes = {"H": 0.0, "D": 0.0, "A": 0.0}
    detail = []
    score_votes: Dict[Tuple[int, int], float] = {}
    for tip in tips:
        pick = (tip.get("pick_1x2") or "").upper()
        conf = tip.get("confidence")
        try:
            w = float(conf) if conf is not None else 1.0
        except (TypeError, ValueError):
            w = 1.0
        w = max(0.25, min(2.0, w))
        if pick in votes:
            votes[pick] += w
        sh, sa = tip.get("score_home"), tip.get("score_away")
        if sh is not None and sa is not None:
            try:
                key = (int(sh), int(sa))
                score_votes[key] = score_votes.get(key, 0.0) + w
            except (TypeError, ValueError):
                pass
        detail.append(
            {
                "analyst": tip.get("analyst_name"),
                "source": tip.get("source"),
                "pick_1x2": pick or None,
                "score": f"{sh}-{sa}" if sh is not None and sa is not None else None,
                "confidence": tip.get("confidence"),
                "url": tip.get("url"),
                "notes": tip.get("notes"),
            }
        )
    total = votes["H"] + votes["D"] + votes["A"]
    if total <= 0:
        # tips without 1X2 — still return list for UI
        return {
            "source": "expert_tips",
            "n": len(tips),
            "p_home": None,
            "p_draw": None,
            "p_away": None,
            "tips": detail,
            "note": "Tips present but no 1X2 picks to aggregate",
        }
    # Soften majority toward uniform with Dirichlet-like prior
    prior = 0.35
    ph = (votes["H"] + prior) / (total + 3 * prior)
    pd = (votes["D"] + prior) / (total + 3 * prior)
    pa = (votes["A"] + prior) / (total + 3 * prior)
    top_scores = [
        {"home": h, "away": a, "p": round(v / sum(score_votes.values()), 4)}
        for (h, a), v in sorted(score_votes.items(), key=lambda x: -x[1])
    ] if score_votes else []
    majority = max(votes.items(), key=lambda x: x[1])[0]
    return {
        "source": "expert_tips",
        "n": len(tips),
        "majority": majority,
        "votes": {k: round(v, 3) for k, v in votes.items()},
        "p_home": round(ph, 4),
        "p_draw": round(pd, 4),
        "p_away": round(pa, 4),
        "top_scorelines": top_scores[:5],
        "tips": detail,
        "note": "Manual import only (PredictZ / Facebook / Threads — no auto-scrape)",
    }


# Public sports RSS (headlines only — not tipster sites)
RSS_FEEDS = [
    ("BBC Sport Football", "https://feeds.bbci.co.uk/sport/football/rss.xml"),
    ("ESPN Soccer", "https://www.espn.com/espn/rss/soccer/news"),
]


def fetch_sports_rss(force: bool = False) -> Dict[str, Any]:
    """Fetch a few public sports RSS feeds; store headlines as media_notes. Never raises."""
    import re
    import xml.etree.ElementTree as ET
    from app import db

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    summary: Dict[str, Any] = {"feeds": [], "inserted": 0}
    for name, url in RSS_FEEDS:
        feed_meta: Dict[str, Any] = {"name": name, "url": url, "ok": False}
        try:
            raw = http_get_text(url, use_cache=not force)
            if not raw:
                feed_meta["error"] = "empty"
                summary["feeds"].append(feed_meta)
                continue
            # Strip default namespaces for simpler parsing
            cleaned = re.sub(r'\sxmlns="[^"]+"', "", raw, count=1)
            root = ET.fromstring(cleaned)
            items = root.findall(".//item")
            n = 0
            for it in items[:40]:
                title = (it.findtext("title") or "").strip()
                link = (it.findtext("link") or "").strip()
                desc = (it.findtext("description") or "").strip()
                # strip HTML tags from description
                desc = re.sub(r"<[^>]+>", " ", desc)
                desc = re.sub(r"\s+", " ", desc).strip()[:400]
                pub = (it.findtext("pubDate") or it.findtext("published") or "").strip()
                if not title:
                    continue
                # crude keywords from title words
                kws = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", title)]
                db.insert_media_note(
                    {
                        "published_at": pub,
                        "title": title,
                        "summary": desc,
                        "url": link or None,
                        "source": name,
                        "team_keywords": kws[:12],
                    }
                )
                n += 1
            feed_meta["ok"] = True
            feed_meta["items"] = n
            summary["inserted"] += n
        except Exception as e:
            feed_meta["error"] = f"{type(e).__name__}: {e}"
        summary["feeds"].append(feed_meta)
    return summary

