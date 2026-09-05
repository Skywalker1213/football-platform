#!/usr/bin/env python3
"""CLI: collect fixtures/results into SQLite (allowlisted comps only)."""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db
from app.collector_core import collect, parse_ymd, today_hkt
from app.predict import rebuild_elo, predict_all


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    p.add_argument("--date")
    args = p.parse_args()
    db.init_db()
    if args.date:
        start = end = parse_ymd(args.date)
    elif args.date_from and args.date_to:
        start, end = parse_ymd(args.date_from), parse_ymd(args.date_to)
    else:
        t = today_hkt()
        start, end = t - timedelta(days=3), t + timedelta(days=4)
    matches, meta = collect(start, end)
    n = db.upsert_matches(matches)
    rebuilt = rebuild_elo()
    predicted = predict_all()
    print({
        "raw_matches": meta.get("match_count"),
        "allowlisted_upserted": n,
        "elo_finished": rebuilt,
        "predictions": predicted,
        "notes": meta.get("sources_notes"),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
