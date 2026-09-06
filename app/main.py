"""FastAPI ops dashboard for football fixtures + predictions."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db
from app.competitions import competitions_by_country, countries
from app.predict import rebuild_elo, predict_all, predict_match
from app.db import latest_learning_run, get_model_params, get_scorecard
from app.scorecard import get_summary as get_scorecard_summary
from app.collector_core import collect, today_hkt, parse_ymd

app = FastAPI(title="Quant-Striker | 足球量化預測系統", version="0.2.0")
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(ROOT / "templates"))

HKT = timezone(timedelta(hours=8))
_collect_status: Dict[str, Any] = {"running": False, "last": None, "message": ""}

STATUS_ZH = {
    "SCHEDULED": "未開賽",
    "LIVE": "進行中",
    "FINISHED": "完場",
    "POSTPONED": "延期",
    "CANCELLED": "取消",
}
COUNTRY_ZH = {
    "England": "英格蘭",
    "Germany": "德國",
    "Switzerland": "瑞士",
    "Scotland": "蘇格蘭",
    "Spain": "西班牙",
    "Netherlands": "荷蘭",
    "Europe": "歐洲",
    "International": "國際",
}
DISCLAIMER = (
    "僅供統計估計，並非博彩建議。"
    "預測為教育用途，來自公開免費資料。"
)


def _ui_ctx(**extra: Any) -> Dict[str, Any]:
    base = {
        "status_zh": STATUS_ZH,
        "country_zh": COUNTRY_ZH,
        "disclaimer": DISCLAIMER,
        "now_hkt": datetime.now(HKT).strftime("%Y-%m-%d %H:%M 香港時間"),
    }
    base.update(extra)
    return base


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


def _default_window():
    """Dashboard window: 2 days before today → 3 days after (HKT)."""
    t = today_hkt()
    return (t - timedelta(days=2)).isoformat(), (t + timedelta(days=3)).isoformat()


def _clamp_to_dashboard_window(date_from: Optional[str], date_to: Optional[str]):
    """Force index/API listings into the -2…+3 HKT window."""
    lo, hi = _default_window()
    date_from = max(date_from, lo) if date_from else lo
    date_to = min(date_to, hi) if date_to else hi
    if date_from > date_to:
        date_from, date_to = lo, hi
    return date_from, date_to



def dedupe_matches_for_display(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse same-fixture rows from different sources (spelling variants)."""
    from app.collector_core import teams_likely_same
    out: List[Dict[str, Any]] = []
    for m in matches:
        merged = False
        for keep in out:
            if keep.get("date") != m.get("date"):
                continue
            if keep.get("competition_key") != m.get("competition_key"):
                continue
            if teams_likely_same(keep.get("home"), m.get("home")) and teams_likely_same(keep.get("away"), m.get("away")):
                # Prefer row with venue / longer names / espn
                score_keep = (2 if keep.get("venue") else 0) + (1 if keep.get("source") == "espn" else 0) + len(keep.get("home") or "")
                score_m = (2 if m.get("venue") else 0) + (1 if m.get("source") == "espn" else 0) + len(m.get("home") or "")
                if score_m > score_keep:
                    # swap content but keep list position
                    pred = keep.get("prediction")
                    keep.clear()
                    keep.update(m)
                    if pred and not keep.get("prediction"):
                        keep["prediction"] = pred
                merged = True
                break
        if not merged:
            out.append(m)
    return out


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    country: Optional[str] = None,
    competition: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    date_from, date_to = _clamp_to_dashboard_window(date_from, date_to)
    matches = db.query_matches(
        date_from=date_from,
        date_to=date_to,
        country=country or None,
        competition_key=competition or None,
        status=status or None,
        limit=300,
    )
    matches = dedupe_matches_for_display(matches)
    # attach predictions
    for m in matches:
        pred = db.get_prediction(m["id"])
        m["prediction"] = pred
    counts = db.counts()
    accuracy = db.accuracy_stats()
    learning = latest_learning_run()
    model_params = get_model_params()
    try:
        scorecard = get_scorecard_summary(refresh=False)
    except Exception:
        scorecard = {"n": 0}
    report_path = ROOT / "data" / "daily_report.md"
    daily_report_time = None
    if report_path.exists():
        # file mtime is UTC epoch; display as HKT
        ts = datetime.fromtimestamp(report_path.stat().st_mtime, tz=timezone.utc).astimezone(HKT)
        daily_report_time = ts.strftime("%Y-%m-%d %H:%M 香港時間")
    return templates.TemplateResponse(
        request,
        "index.html",
        _ui_ctx(
            matches=matches,
            countries=countries(),
            competitions=competitions_by_country(),
            filters={
                "country": country or "",
                "competition": competition or "",
                "status": status or "",
                "date_from": date_from,
                "date_to": date_to,
            },
            counts=counts,
            accuracy=accuracy,
            collect_status=_collect_status,
            learning=learning,
            model_params=model_params,
            scorecard=scorecard,
            window_label="香港時間：今日前 2 日 → 後 3 日",
            daily_report_time=daily_report_time,
        ),
    )


@app.get("/match/{match_id}", response_class=HTMLResponse)
async def match_detail(request: Request, match_id: int):
    import json as _json
    m = db.get_match(match_id)
    if not m:
        return HTMLResponse("找不到比賽", status_code=404)
    # Live weather on detail view
    import os, app.features as features
    features.SKIP_WEATHER = False
    os.environ["FOOTBALL_SKIP_WEATHER"] = "0"
    pred = predict_match(m, persist=True)  # refresh with weather
    feats = (pred or {}).get("features") or {}
    if isinstance(feats, str):
        try:
            feats = _json.loads(feats)
        except Exception:
            feats = {}
    consensus = (pred or {}).get("consensus") or feats.get("consensus") or {}
    top_scorelines = (pred or {}).get("top_scorelines") or feats.get("top_scorelines") or []
    blend = (consensus.get("blend") if isinstance(consensus, dict) else None) or {}
    scorecard_row = None
    if (m.get("status") or "").upper() == "FINISHED":
        try:
            scorecard_row = get_scorecard(int(match_id))
            if scorecard_row is None and pred and m.get("home_score") is not None:
                from app.scorecard import score_row_from_finished
                row = {
                    **m,
                    "p_home": pred.get("p_home"),
                    "p_draw": pred.get("p_draw"),
                    "p_away": pred.get("p_away"),
                    "pred_score_home": pred.get("score_home"),
                    "pred_score_away": pred.get("score_away"),
                    "features": pred.get("features") or {},
                }
                scorecard_row = score_row_from_finished(row)
                db.upsert_scorecard_row(scorecard_row)
        except Exception:
            scorecard_row = None
    return templates.TemplateResponse(
        request,
        "match.html",
        _ui_ctx(
            match=m,
            prediction=pred,
            scorecard=scorecard_row,
            factors=feats.get("factors") or [],
            feature_matrix=feats.get("feature_matrix") or {},
            weather_json=_json.dumps(feats.get("weather") or {}, indent=2, ensure_ascii=False),
            load_json=_json.dumps(
                {"home": feats.get("home_load"), "away": feats.get("away_load")},
                indent=2,
                ensure_ascii=False,
            ),
            consensus=consensus,
            consensus_blend=blend,
            top_scorelines=top_scorelines,
        ),
    )


@app.get("/api/matches")
async def api_matches(
    country: Optional[str] = None,
    competition: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = Query(200, le=500),
):
    date_from, date_to = _clamp_to_dashboard_window(date_from, date_to)
    matches = db.query_matches(
        date_from=date_from,
        date_to=date_to,
        country=country,
        competition_key=competition,
        status=status,
        limit=limit,
    )
    for m in matches:
        m["prediction"] = db.get_prediction(m["id"])
    return {"matches": matches, "count": len(matches)}


@app.get("/api/match/{match_id}/prediction")
async def api_prediction(match_id: int, refresh: bool = False):
    m = db.get_match(match_id)
    if not m:
        return JSONResponse({"error": "not found"}, status_code=404)
    if refresh or not db.get_prediction(match_id):
        pred = predict_match(m, persist=True)
    else:
        pred = db.get_prediction(match_id)
    return {"match": m, "prediction": pred}


@app.get("/api/accuracy")
async def api_accuracy():
    return db.accuracy_stats()


@app.get("/api/stats")
async def api_stats():
    return db.counts()


@app.get("/api/competitions")
async def api_competitions():
    return competitions_by_country()


def _run_collect(date_from: str, date_to: str) -> None:
    global _collect_status
    _collect_status = {"running": True, "last": None, "message": f"正在收集 {date_from}→{date_to}…"}
    try:
        matches, meta = collect(parse_ymd(date_from), parse_ymd(date_to))
        n = db.upsert_matches(matches)
        from app.scorecard import run_scorecard_and_finetune
        pipe = run_scorecard_and_finetune()
        learn = pipe.get("learning") or {}
        sc = pipe.get("scorecard") or {}
        predicted = predict_all(limit=400)
        _collect_status = {
            "running": False,
            "last": datetime.now(HKT).isoformat(),
            "message": (
                f"已寫入 {n}/{meta.get('match_count')} 場允許賽事；"
                f"Elo 用咗 {learn.get('elo_finished')} 場完場；"
                f"計分卡 n={sc.get('n')} 1X2={sc.get('hit_1x2_rate')}；"
                f"學習 Brier={learn.get('brier')}；"
                f"預測 {predicted} 場。"
            ),
            "meta": meta,
            "learning": learn,
            "scorecard": sc,
            "qs_finetune": pipe.get("qs_finetune"),
        }
    except Exception as e:
        _collect_status = {
            "running": False,
            "last": datetime.now(HKT).isoformat(),
            "message": f"收集失敗：{e}",
        }


@app.post("/api/refresh")
async def api_refresh(
    background_tasks: BackgroundTasks,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    if _collect_status.get("running"):
        return {"ok": False, "message": "已有收集任務進行中", "status": _collect_status}
    df, dt = _default_window()
    background_tasks.add_task(_run_collect, date_from or df, date_to or dt)
    return {"ok": True, "message": "已開始收集", "date_from": date_from or df, "date_to": date_to or dt}


@app.get("/api/refresh/status")
async def api_refresh_status():
    return _collect_status


@app.post("/api/predict/rebuild")
async def api_rebuild():
    n = rebuild_elo()
    p = predict_all()
    return {"elo_matches": n, "predictions": p, "accuracy": db.accuracy_stats()}



@app.get("/api/learning")
async def api_learning():
    return {
        "latest": latest_learning_run(),
        "params": get_model_params(),
        "accuracy": db.accuracy_stats(),
    }


@app.get("/api/scorecard")
async def api_scorecard(refresh: bool = False):
    return get_scorecard_summary(refresh=refresh)
