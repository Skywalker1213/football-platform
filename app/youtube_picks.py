"""Select high-confidence Quant-Striker picks for YouTube short-form videos.

Selection rules:
  - Exactly N upcoming (non-FINISHED) matches with predictions
  - Confidence order: STRONG > LEAN > TOSS-UP
  - Within tier: higher max(p_home, p_draw, p_away), then confidence_p
  - Allowlisted competitions only (competition_key in COMPETITIONS)
  - Prefer HKT date window today-2..today+3; expand to +7 days if < N
  - Deduplicate fixtures; skip if no Quant-Striker card / prediction
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.competitions import COMPETITIONS
from app.db import DB_PATH, HKT, ROOT

TIER_RANK = {"STRONG": 0, "LEAN": 1, "TOSS-UP": 2}
ALLOWLIST_KEYS = {str(c["key"]) for c in COMPETITIONS}
DISCLAIMER_ZH = "僅供統計估計，並非博彩建議。"


def today_hkt(now: Optional[datetime] = None) -> datetime:
    if now is None:
        now = datetime.now(HKT)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=HKT)
    else:
        now = now.astimezone(HKT)
    return now


def _parse_features(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            return json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return {}
    return {}


def extract_quant_striker(features: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pull QS card from features_json (consensus.quant_striker or top-level)."""
    if not features:
        return None
    consensus = features.get("consensus") or {}
    qs = None
    if isinstance(consensus, dict):
        qs = consensus.get("quant_striker") or consensus.get("quantStriker")
    if not qs:
        qs = features.get("quant_striker") or features.get("quantStriker")
    return qs if isinstance(qs, dict) else None


def extract_confidence(qs: Dict[str, Any]) -> Tuple[str, float]:
    """Return (tier, confidence_p) from QS card."""
    system = qs.get("system") if isinstance(qs.get("system"), dict) else {}
    tier = (
        (system.get("confidence") if system else None)
        or qs.get("confidence")
        or "TOSS-UP"
    )
    tier = str(tier).upper().replace("_", "-")
    if tier not in TIER_RANK:
        # normalize variants
        if "STRONG" in tier:
            tier = "STRONG"
        elif "LEAN" in tier:
            tier = "LEAN"
        else:
            tier = "TOSS-UP"
    conf_p = system.get("confidence_p") if system else None
    if conf_p is None:
        conf_p = qs.get("confidence_p")
    try:
        conf_p_f = float(conf_p) if conf_p is not None else 0.0
    except (TypeError, ValueError):
        conf_p_f = 0.0
    return tier, conf_p_f


def _to_zh_note(s: str) -> str:
    """Light localization of common QS English factor notes → zh-Hant."""
    low = s.lower()
    if "commercial" in low and ("favourite" in low or "favorite" in low):
        return "市場傾向訊號（商業／賠率因子）"
    if "commercial" in low and "market" in low:
        return "市場分歧／商業因子有輕微訊號"
    if "form-30" in low or "form30" in low.replace(" ", ""):
        return "近況（Form-30）樣本不足，權重偏低"
    if "stakes" in low and "champions" in low:
        return "賽事利害／歐冠層級權重"
    if "stakes" in low and "midweek" in low:
        return "週中賽程負荷較高"
    if "geo" in low and "weather" in low:
        return "天氣／地理條件調整"
    if "player" in low:
        return "主力球員影響估計"
    return s


def _brief_factor_note(qs: Dict[str, Any], max_len: int = 80) -> str:
    """Human-readable brief note from QS factor notes (skip pipeline boilerplate)."""
    notes: List[str] = []
    for b in qs.get("factors") or []:
        if not isinstance(b, dict):
            continue
        for n in b.get("notes") or []:
            s = str(n).strip()
            if not s:
                continue
            low = s.lower()
            if "pipeline:" in low or "quant-striker v3" in low:
                continue
            if "insufficient" in low or "estimated delta=0" in low:
                continue
            if "no injury" in low or "deferred/unavailable" in low:
                continue
            if "baseline" in low and "stakes" in low:
                continue
            notes.append(_to_zh_note(s))
    for n in qs.get("notes") or []:
        s = str(n).strip()
        if s and "Pipeline:" not in s and "Quant-Striker" not in s:
            notes.append(_to_zh_note(s))
    if not notes:
        return ""
    text = notes[0]
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text


def _fixture_key(row: Dict[str, Any]) -> str:
    return "|".join(
        [
            str(row.get("date") or ""),
            str(row.get("home") or "").strip().lower(),
            str(row.get("away") or "").strip().lower(),
            str(row.get("competition_key") or ""),
        ]
    )


def _kickoff_hkt_display(kickoff_hkt: Optional[str], date: Optional[str]) -> str:
    if kickoff_hkt:
        s = str(kickoff_hkt)
        # 2026-09-05T19:30:00+08:00 → 09-05 19:30 HKT
        try:
            if "T" in s:
                date_part, rest = s.split("T", 1)
                time_part = rest[:5]
                md = date_part[5:] if len(date_part) >= 10 else date_part
                return f"{md} {time_part} HKT"
        except Exception:
            pass
        return s
    return f"{date or '—'} HKT"


def _load_candidates(
    date_from: str,
    date_to: str,
    db_path: Path = DB_PATH,
) -> List[Dict[str, Any]]:
    sql = """
        SELECT m.id, m.date, m.kickoff_utc, m.kickoff_hkt, m.league, m.country,
               m.competition_key, m.home, m.away, m.status, m.venue,
               p.p_home, p.p_draw, p.p_away, p.score_home, p.score_away,
               p.features_json, p.model
        FROM matches m
        JOIN predictions p ON p.match_id = m.id
        WHERE UPPER(COALESCE(m.status, '')) != 'FINISHED'
          AND UPPER(COALESCE(m.status, '')) != 'LIVE'
          AND m.date >= ? AND m.date <= ?
          AND m.competition_key IS NOT NULL
          AND m.competition_key != ''
        ORDER BY m.date, m.kickoff_utc, m.id
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(sql, (date_from, date_to)).fetchall()]
    finally:
        conn.close()

    out: List[Dict[str, Any]] = []
    for r in rows:
        if r.get("competition_key") not in ALLOWLIST_KEYS:
            continue
        feats = _parse_features(r.get("features_json"))
        qs = extract_quant_striker(feats)
        if not qs:
            continue
        tier, conf_p = extract_confidence(qs)
        system = qs.get("system") if isinstance(qs.get("system"), dict) else {}
        ph = float(r.get("p_home") or 0)
        pd = float(r.get("p_draw") or 0)
        pa = float(r.get("p_away") or 0)
        max_p = max(ph, pd, pa)
        human = feats.get("human_score") or {}
        score_h = human.get("home", r.get("score_home"))
        score_a = human.get("away", r.get("score_away"))
        if score_h is None and system.get("scoreline"):
            try:
                parts = str(system["scoreline"]).split("-")
                score_h, score_a = int(parts[0]), int(parts[1])
            except Exception:
                score_h, score_a = r.get("score_home"), r.get("score_away")
        out.append(
            {
                "match_id": r["id"],
                "date": r["date"],
                "kickoff_utc": r.get("kickoff_utc"),
                "kickoff_hkt": r.get("kickoff_hkt"),
                "kickoff_hkt_display": _kickoff_hkt_display(
                    r.get("kickoff_hkt"), r.get("date")
                ),
                "league": r.get("league"),
                "country": r.get("country"),
                "competition_key": r.get("competition_key"),
                "home": r.get("home"),
                "away": r.get("away"),
                "status": r.get("status"),
                "p_home": round(ph, 4),
                "p_draw": round(pd, 4),
                "p_away": round(pa, 4),
                "max_p": round(max_p, 4),
                "score_home": score_h,
                "score_away": score_a,
                "scoreline": f"{score_h}-{score_a}"
                if score_h is not None and score_a is not None
                else (system.get("scoreline") or "—"),
                "confidence": tier,
                "confidence_p": round(conf_p, 4),
                "result_call": system.get("result_call"),
                "outcome": system.get("outcome"),
                "factor_note": _brief_factor_note(qs),
                "model": r.get("model"),
            }
        )
    return out


def rank_picks(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def key(c: Dict[str, Any]) -> Tuple:
        return (
            TIER_RANK.get(c.get("confidence") or "TOSS-UP", 9),
            -float(c.get("max_p") or 0),
            -float(c.get("confidence_p") or 0),
            c.get("date") or "",
            c.get("match_id") or 0,
        )

    return sorted(candidates, key=key)


def select_youtube_picks(
    n: int = 6,
    now: Optional[datetime] = None,
    db_path: Path = DB_PATH,
    persist: bool = True,
) -> Dict[str, Any]:
    """Select exactly n high-confidence upcoming picks (or fewer if not enough)."""
    now_hkt = today_hkt(now)
    today = now_hkt.date()
    primary_from = (today - timedelta(days=2)).isoformat()
    primary_to = (today + timedelta(days=3)).isoformat()
    expand_to = (today + timedelta(days=7)).isoformat()

    primary = _load_candidates(primary_from, primary_to, db_path=db_path)
    window_used = {"from": primary_from, "to": primary_to, "expanded": False}
    pool = primary
    if len(rank_picks(primary)) < n:
        expanded = _load_candidates(primary_from, expand_to, db_path=db_path)
        pool = expanded
        window_used = {"from": primary_from, "to": expand_to, "expanded": True}

    ranked = rank_picks(pool)
    selected: List[Dict[str, Any]] = []
    seen = set()
    for c in ranked:
        fk = _fixture_key(c)
        if fk in seen:
            continue
        seen.add(fk)
        selected.append(c)
        if len(selected) >= n:
            break

    # stamp ranks
    for i, c in enumerate(selected, 1):
        c["rank"] = i

    payload = {
        "generated_at_hkt": now_hkt.strftime("%Y-%m-%d %H:%M:%S HKT"),
        "date_hkt": today.isoformat(),
        "window": window_used,
        "count": len(selected),
        "requested": n,
        "disclaimer": DISCLAIMER_ZH,
        "picks": selected,
    }

    if persist:
        out_dir = ROOT / "data"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = today.strftime("%Y%m%d")
        path = out_dir / f"youtube_picks_{stamp}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        payload["persisted_path"] = str(path)

    return payload


def picks_to_narration(payload: Dict[str, Any]) -> str:
    """Build zh-Hant narration markdown for TTS / script."""
    date = payload.get("date_hkt") or ""
    lines = [
        "# Quant-Striker 旁白腳本（繁體中文）",
        "",
        f"日期：{date}",
        "",
        "## 開場",
        "",
        f"歡迎收看 Quant-Striker。今日六場高信心預測。日期 {date}。",
        f"{DISCLAIMER_ZH}",
        "",
        "## 六場預測",
        "",
    ]
    for p in payload.get("picks") or []:
        ph = int(round(float(p["p_home"]) * 100))
        pd = int(round(float(p["p_draw"]) * 100))
        pa = int(round(float(p["p_away"]) * 100))
        note = p.get("factor_note") or ""
        conf = p.get("confidence") or "TOSS-UP"
        conf_zh = {"STRONG": "高信心", "LEAN": "偏傾", "TOSS-UP": "膠著"}.get(
            conf, conf
        )
        lines.append(f"### 第 {p.get('rank')} 場")
        lines.append("")
        lines.append(
            f"{p.get('home')} 對 {p.get('away')}，聯賽 {p.get('league')}，"
            f"開賽 {p.get('kickoff_hkt_display')}。"
        )
        lines.append(
            f"信心等級 {conf_zh}。主勝 {ph}%、和局 {pd}%、客勝 {pa}%。"
            f"參考比分 {p.get('scoreline')}。"
        )
        if note:
            lines.append(f"因素提示：{note}")
        lines.append("")
    lines.extend(
        [
            "## 結尾",
            "",
            "感謝收看。請訂閱 Quant-Striker，獲取每日統計預測摘要。",
            f"{DISCLAIMER_ZH}",
            "",
        ]
    )
    return "\n".join(lines)


def youtube_metadata(payload: Dict[str, Any]) -> Dict[str, str]:
    date = payload.get("date_hkt") or ""
    picks = payload.get("picks") or []
    title = f"Quant-Striker｜今日六場高信心預測（{date}）"
    desc_lines = [
        "Quant-Striker 統計模型 — 今日六場高信心預測摘要",
        f"日期（香港時間）：{date}",
        "",
        "⚠️ 免責聲明：僅供統計估計與教育用途，並非博彩建議。",
        "",
        "本集場次：",
    ]
    for p in picks:
        conf = p.get("confidence")
        desc_lines.append(
            f"{p.get('rank')}. {p.get('home')} vs {p.get('away')} "
            f"（{p.get('league')}｜{p.get('kickoff_hkt_display')}｜{conf}）"
            f" 主{int(round(p['p_home']*100))}% "
            f"和{int(round(p['p_draw']*100))}% "
            f"客{int(round(p['p_away']*100))}% "
            f"參考比分 {p.get('scoreline')}"
        )
    desc_lines.extend(
        [
            "",
            "訂閱 Quant-Striker 獲取每日更新。",
            "Research / education only — not gambling advice.",
        ]
    )
    tags = [
        "Quant-Striker",
        "足球預測",
        "統計模型",
        "Premier League",
        "La Liga",
        "Bundesliga",
        "Champions League",
        "足球分析",
        "今日預測",
        "香港足球",
        date,
    ]
    return {
        "title": title,
        "description": "\n".join(desc_lines) + "\n",
        "tags": ",".join(tags),
    }


if __name__ == "__main__":
    import pprint

    result = select_youtube_picks(n=6, persist=True)
    print(f"selected {result['count']} / {result['requested']}")
    print(f"window {result['window']}")
    for p in result["picks"]:
        print(
            f"  #{p['rank']} {p['confidence']} max_p={p['max_p']:.3f} "
            f"{p['home']} vs {p['away']} ({p['league']})"
        )
    if result.get("persisted_path"):
        print("wrote", result["persisted_path"])
