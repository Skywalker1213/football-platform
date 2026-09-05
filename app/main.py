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
from app.collector_core import collect, today_hkt, parse_ymd

app = FastAPI(title="Football Platform", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(ROOT / "templates"))

HKT = timezone(timedelta(hours=8))
_collect_status: Dict[str, Any] = {"running": False, "last": None, "message": ""}


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


def _default_window():
    t = today_hkt()
    return (t - timedelta(days=3)).isoformat(), (t + timedelta(days=4)).isoformat()


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    country: Optional[str] = None,
    competition: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    df, dt = _default_window()
    date_from = date_from or df
    date_to = date_to or dt
    matches = db.query_matches(
        date_from=date_from,
        date_to=date_to,
        country=country or None,
        competition_key=competition or None,
        status=status or None,
        limit=300,
    )
    # attach predictions
    for m in matches:
        pred = db.get_prediction(m["id"])
        m["prediction"] = pred
    counts = db.counts()
    accuracy = db.accuracy_stats()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "matches": matches,
            "countries": countries(),
            "competitions": competitions_by_country(),
            "filters": {
                "country": country or "",
                "competition": competition or "",
                "status": status or "",
                "date_from": date_from,
                "date_to": date_to,
            },
            "counts": counts,
            "accuracy": accuracy,
            "collect_status": _collect_status,
            "disclaimer": (
                "Statistical estimates only — not gambling advice. "
                "Predictions are educational model outputs from free public data."
            ),
            "now_hkt": datetime.now(HKT).strftime("%Y-%m-%d %H:%M HKT"),
        },
    )


@app.get("/match/{match_id}", response_class=HTMLResponse)
async def match_detail(request: Request, match_id: int):
    import json as _json
    m = db.get_match(match_id)
    if not m:
        return HTMLResponse("Match not found", status_code=404)
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
    return templates.TemplateResponse(
        request,
        "match.html",
        {
            "match": m,
            "prediction": pred,
            "factors": feats.get("factors") or [],
            "feature_matrix": feats.get("feature_matrix") or {},
            "weather_json": _json.dumps(feats.get("weather") or {}, indent=2, ensure_ascii=False),
            "load_json": _json.dumps(
                {"home": feats.get("home_load"), "away": feats.get("away_load")},
                indent=2,
                ensure_ascii=False,
            ),
            "disclaimer": (
                "Statistical estimates only — not gambling advice."
            ),
            "now_hkt": datetime.now(HKT).strftime("%Y-%m-%d %H:%M HKT"),
        },
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
    df, dt = _default_window()
    matches = db.query_matches(
        date_from=date_from or df,
        date_to=date_to or dt,
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
    _collect_status = {"running": True, "last": None, "message": f"Collecting {date_from}→{date_to}…"}
    try:
        matches, meta = collect(parse_ymd(date_from), parse_ymd(date_to))
        n = db.upsert_matches(matches)
        rebuilt = rebuild_elo()
        predicted = predict_all(limit=400)
        _collect_status = {
            "running": False,
            "last": datetime.now(HKT).isoformat(),
            "message": (
                f"Upserted {n}/{meta.get('match_count')} allowlisted; "
                f"Elo from {rebuilt} finished; predicted {predicted}."
            ),
            "meta": meta,
        }
    except Exception as e:
        _collect_status = {
            "running": False,
            "last": datetime.now(HKT).isoformat(),
            "message": f"Collect failed: {e}",
        }


@app.post("/api/refresh")
async def api_refresh(
    background_tasks: BackgroundTasks,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    if _collect_status.get("running"):
        return {"ok": False, "message": "Collection already running", "status": _collect_status}
    df, dt = _default_window()
    background_tasks.add_task(_run_collect, date_from or df, date_to or dt)
    return {"ok": True, "message": "Collection started", "date_from": date_from or df, "date_to": date_to or dt}


@app.get("/api/refresh/status")
async def api_refresh_status():
    return _collect_status


@app.post("/api/predict/rebuild")
async def api_rebuild():
    n = rebuild_elo()
    p = predict_all()
    return {"elo_matches": n, "predictions": p, "accuracy": db.accuracy_stats()}
