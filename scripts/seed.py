#!/usr/bin/env python3
"""Seed DB from existing football-data pull and/or live collect for today±window."""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db
from app.collector_core import collect, today_hkt
from app.predict import rebuild_elo, predict_all

LEGACY = Path("/workspace/football-data/data/matches.json")


def seed_from_legacy() -> int:
    if not LEGACY.exists():
        print("No legacy matches.json")
        return 0
    payload = json.loads(LEGACY.read_text(encoding="utf-8"))
    matches = payload.get("matches") or []
    n = db.upsert_matches(matches)
    print(f"Legacy upsert allowlisted: {n} / {len(matches)}")
    return n


def main() -> int:
    db.init_db()
    seed_from_legacy()
    t = today_hkt()
    start, end = t - timedelta(days=3), t + timedelta(days=4)
    print(f"Live collect {start} → {end} …")
    matches, meta = collect(start, end)
    n = db.upsert_matches(matches)
    print(f"Live upsert allowlisted: {n} / {meta.get('match_count')}")
    rebuilt = rebuild_elo()
    predicted = predict_all()
    print(json.dumps({
        "elo_finished": rebuilt,
        "predictions": predicted,
        "counts": db.counts(),
        "accuracy": db.accuracy_stats(),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
