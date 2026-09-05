#!/usr/bin/env python3
"""CLI: rebuild Elo and refresh predictions."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db
from app.predict import rebuild_elo, predict_all


def main() -> int:
    db.init_db()
    n = rebuild_elo()
    p = predict_all()
    print(json.dumps({"elo_matches": n, "predictions": p, "accuracy": db.accuracy_stats()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
