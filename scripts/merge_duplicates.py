#!/usr/bin/env python3
"""Merge fixtures that are the same match under different team-name spellings."""
from __future__ import annotations
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.collector_core import dedupe_key, teams_likely_same
from app.db import init_db


def main() -> int:
    init_db()
    con = sqlite3.connect(ROOT / "data" / "football.db")
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM matches").fetchall()]
    SOURCE_PREF = {"espn": 3, "openligadb": 2, "football-data.co.uk": 2, "thesportsdb": 1}

    def score_row(r):
        s = SOURCE_PREF.get(r.get("source") or "", 0)
        if r.get("kickoff_utc"):
            s += 2
        if r.get("home_score") is not None:
            s += 3
        st = (r.get("status") or "").upper()
        if st == "FINISHED":
            s += 4
        elif st == "LIVE":
            s += 3
        if r.get("venue"):
            s += 1
        s += min(len(r.get("home") or ""), 40) / 40
        s += min(len(r.get("away") or ""), 40) / 40
        return s

    def merge_extra(a, b):
        def parse(x):
            if not x:
                return {}
            if isinstance(x, dict):
                return dict(x)
            try:
                return json.loads(x)
            except Exception:
                return {}
        ea, eb = parse(a), parse(b)
        out = dict(ea)
        for k, v in eb.items():
            if k not in out or out[k] in (None, "", {}, []):
                out[k] = v
            elif k == "odds" and isinstance(v, dict):
                out[k] = {**(out.get(k) or {}), **v}
        return json.dumps(out, ensure_ascii=False) if out else None

    used = set()
    clusters = []
    for r in rows:
        if r["id"] in used:
            continue
        cluster = [r]
        used.add(r["id"])
        for o in rows:
            if o["id"] in used:
                continue
            if r["date"] != o["date"] or r["competition_key"] != o["competition_key"]:
                continue
            if teams_likely_same(r["home"], o["home"]) and teams_likely_same(r["away"], o["away"]):
                cluster.append(o)
                used.add(o["id"])
        clusters.append(cluster)

    deleted = 0
    for cluster in clusters:
        if len(cluster) == 1:
            r = cluster[0]
            nk = dedupe_key({
                "date": r["date"], "home": r["home"], "away": r["away"],
                "league": r["league"], "country": r["country"],
                "competition_key": r["competition_key"],
            })
            if r["dedupe_key"] != nk:
                other = con.execute(
                    "SELECT id FROM matches WHERE dedupe_key=? AND id!=?", (nk, r["id"])
                ).fetchone()
                if not other:
                    con.execute("UPDATE matches SET dedupe_key=? WHERE id=?", (nk, r["id"]))
            continue
        items = sorted(cluster, key=score_row, reverse=True)
        keep = dict(items[0])
        for d0 in items[1:]:
            d = dict(d0)
            if not keep.get("kickoff_utc") and d.get("kickoff_utc"):
                keep["kickoff_utc"] = d["kickoff_utc"]
                keep["kickoff_hkt"] = d.get("kickoff_hkt")
            if keep.get("home_score") is None and d.get("home_score") is not None:
                keep["home_score"] = d["home_score"]
                keep["away_score"] = d["away_score"]
            st_k = (keep.get("status") or "").upper()
            st_d = (d.get("status") or "").upper()
            if st_k not in ("FINISHED", "LIVE") and st_d in ("FINISHED", "LIVE"):
                keep["status"] = d["status"]
            if not keep.get("venue") and d.get("venue"):
                keep["venue"] = d["venue"]
            if len(d.get("home") or "") > len(keep.get("home") or ""):
                keep["home"] = d["home"]
            if len(d.get("away") or "") > len(keep.get("away") or ""):
                keep["away"] = d["away"]
            keep["extra"] = merge_extra(keep.get("extra"), d.get("extra"))
            for table in ("predictions", "match_features", "prediction_log"):
                exists = con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if not exists:
                    continue
                cols = [c[1] for c in con.execute(f"PRAGMA table_info({table})").fetchall()]
                if "match_id" not in cols:
                    continue
                keep_has = con.execute(
                    f"SELECT 1 FROM {table} WHERE match_id=? LIMIT 1", (keep["id"],)
                ).fetchone()
                if not keep_has:
                    drop_rows = con.execute(f"SELECT * FROM {table} WHERE match_id=?", (d["id"],)).fetchall()
                    con.execute(f"DELETE FROM {table} WHERE match_id=?", (d["id"],))
                    for dr in drop_rows:
                        dd = dict(dr)
                        dd["match_id"] = keep["id"]
                        try:
                            con.execute(
                                f"INSERT OR REPLACE INTO {table} ({','.join(dd.keys())}) VALUES ({','.join('?'*len(dd))})",
                                tuple(dd.values()),
                            )
                        except Exception:
                            pass
                else:
                    con.execute(f"DELETE FROM {table} WHERE match_id=?", (d["id"],))
            con.execute("DELETE FROM matches WHERE id=?", (d["id"],))
            deleted += 1
        nk = dedupe_key({
            "date": keep["date"], "home": keep["home"], "away": keep["away"],
            "league": keep["league"], "country": keep["country"],
            "competition_key": keep["competition_key"],
        })
        con.execute(
            """UPDATE matches SET home=?, away=?, kickoff_utc=?, kickoff_hkt=?,
               home_score=?, away_score=?, status=?, venue=?, extra=?, dedupe_key=? WHERE id=?""",
            (
                keep["home"], keep["away"], keep.get("kickoff_utc"), keep.get("kickoff_hkt"),
                keep.get("home_score"), keep.get("away_score"), keep.get("status"), keep.get("venue"),
                keep.get("extra") if isinstance(keep.get("extra"), str) else json.dumps(keep.get("extra") or {}, ensure_ascii=False),
                nk, keep["id"],
            ),
        )
    con.commit()
    after = con.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    print(f"merge_duplicates: deleted={deleted} remaining={after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
