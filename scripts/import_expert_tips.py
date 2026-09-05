#!/usr/bin/env python3
"""Import expert / tipster picks from CSV or JSON (manual curation only).

Do NOT scrape PredictZ, Forebet, Sofascore, Flashscore, Facebook, or Threads.
Copy publicly visible tips into a file by hand, then import here.

CSV columns (header required):
  match_date, home, away, source, analyst_name, pick_1x2,
  score_home, score_away, confidence, url, notes

  - match_date: YYYY-MM-DD
  - pick_1x2: H / D / A (also accepts 1/X/2, home/draw/away)
  - score_home / score_away: optional integers
  - confidence: optional float (default 1.0; typical 0.5–1.5)
  - source: free text e.g. predictz_manual, threads_manual, bbc_roundup

JSON: array of objects with the same keys.

Usage:
  PYTHONPATH=. python scripts/import_expert_tips.py data/expert_tips.example.csv
  PYTHONPATH=. python scripts/import_expert_tips.py tips.json --link-matches
  PYTHONPATH=. python scripts/import_expert_tips.py --fetch-rss
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db
from app.collector_core import _norm_team


def _load_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _load_json(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "tips" in data:
        data = data["tips"]
    if not isinstance(data, list):
        raise ValueError("JSON must be a list of tip objects")
    return data


def _link_match_id(tip: dict) -> None:
    date = tip.get("match_date") or tip.get("date")
    home, away = tip.get("home"), tip.get("away")
    if not (date and home and away):
        return
    hn, an = _norm_team(home), _norm_team(away)
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT id, home, away FROM matches WHERE date=? ORDER BY id",
            (str(date)[:10],),
        ).fetchall()
    for r in rows:
        if _norm_team(r["home"]) == hn and _norm_team(r["away"]) == an:
            tip["match_id"] = int(r["id"])
            return
        # soft containment
        if hn in _norm_team(r["home"]) or _norm_team(r["home"]) in hn:
            if an in _norm_team(r["away"]) or _norm_team(r["away"]) in an:
                tip["match_id"] = int(r["id"])
                return


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", nargs="?", help="CSV or JSON file of tips")
    ap.add_argument("--link-matches", action="store_true", help="Set match_id when fixture exists")
    ap.add_argument("--fetch-rss", action="store_true", help="Fetch BBC/ESPN football RSS headlines only")
    args = ap.parse_args()

    db.init_db()
    inserted = 0

    if args.fetch_rss:
        from app.consensus import fetch_sports_rss
        summary = fetch_sports_rss(force=True)
        print(json.dumps({"rss": summary}, ensure_ascii=False, indent=2))

    if args.path:
        path = Path(args.path)
        if not path.exists():
            print(f"File not found: {path}", file=sys.stderr)
            return 1
        tips = _load_csv(path) if path.suffix.lower() == ".csv" else _load_json(path)
        for tip in tips:
            if args.link_matches:
                _link_match_id(tip)
            # normalize empty strings
            for k, v in list(tip.items()):
                if isinstance(v, str) and v.strip() == "":
                    tip[k] = None
            if tip.get("score_home") not in (None, ""):
                tip["score_home"] = int(float(tip["score_home"]))
            if tip.get("score_away") not in (None, ""):
                tip["score_away"] = int(float(tip["score_away"]))
            db.upsert_expert_tip(tip)
            inserted += 1
        print(json.dumps({"imported": inserted, "path": str(path)}, ensure_ascii=False, indent=2))

    if not args.path and not args.fetch_rss:
        ap.print_help()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
