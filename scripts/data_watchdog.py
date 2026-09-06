#!/usr/bin/env python3
"""Watchdog: catch duplicate fixtures and bad competition maps, then auto-heal.

Run after collect / before predict (also from daily_update).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.collector_core import _norm_team, dedupe_key, teams_likely_same
from app.db import init_db

DB = ROOT / "data" / "football.db"
REPORT = ROOT / "data" / "watchdog_report.json"

# Clubs that must never sit under Swiss Super League (sui_sl)
_NON_SWISS_SUI = {
    "ofi crete", "kifisia", "aek", "aris", "paok", "olympiacos", "olympiakos",
    "olympiakol", "panathinaikos", "asteras", "asteras tripolis", "volos",
    "volos nfc", "lamia", "atromitos", "panetolikos", "levadeiakos", "kalamata",
    "iraklis", "aek athens",
}
_SWISS_HINTS = {
    "young boys", "basel", "zurich", "servette", "lugano", "luzern", "sion",
    "grasshopper", "winterthur", "st gallen", "lausanne", "yverdon", "thun",
}


SOURCE_PREF = {"espn": 3, "openligadb": 2, "football-data.co.uk": 2, "thesportsdb": 1}


def _score_row(r: Dict[str, Any]) -> float:
    s = float(SOURCE_PREF.get(r.get("source") or "", 0))
    if r.get("kickoff_utc"):
        s += 2
    if r.get("venue"):
        s += 1
    if r.get("home_score") is not None:
        s += 3
    st = (r.get("status") or "").upper()
    if st == "FINISHED":
        s += 4
    elif st == "LIVE":
        s += 3
    s += min(len(r.get("home") or ""), 40) / 40
    s += min(len(r.get("away") or ""), 40) / 40
    if r.get("source") == "espn":
        s += 0.5
    return s


def _merge_extra(a: Any, b: Any) -> str | None:
    def parse(x: Any) -> dict:
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


def _is_bogus_swiss(r: Dict[str, Any]) -> bool:
    if r.get("competition_key") != "sui_sl":
        return False
    h, a = _norm_team(r.get("home")), _norm_team(r.get("away"))
    if any(g in h or g in a or h == g or a == g for g in _NON_SWISS_SUI):
        return True
    league = (r.get("league") or "").lower().strip()
    country = (r.get("country") or "").lower()
    if league in {"super league", "superleague"} and "swiss" not in league:
        # require at least one Swiss-hint team; else reject
        if not any(any(tok in n for tok in _SWISS_HINTS) for n in (h, a)):
            return True
    if "greece" in country or "greek" in league:
        return True
    return False


def find_issues(con: sqlite3.Connection) -> Dict[str, Any]:
    rows = [dict(r) for r in con.execute("SELECT * FROM matches").fetchall()]
    issues: Dict[str, Any] = {
        "bogus_swiss": [],
        "duplicate_groups": [],
        "stale_dedupe_keys": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "match_count": len(rows),
    }
    for r in rows:
        if _is_bogus_swiss(r):
            issues["bogus_swiss"].append(
                {"id": r["id"], "home": r["home"], "away": r["away"], "league": r["league"], "country": r["country"]}
            )
        nk = dedupe_key(r)
        if (r.get("dedupe_key") or "") != nk and "|tmp" not in (r.get("dedupe_key") or ""):
            # stale if old key used broken accent fold (space inside team token)
            old = r.get("dedupe_key") or ""
            if " s|" in old or "|m " in old or "alav s" in old or "m laga" in old or old != nk:
                if old != nk:
                    issues["stale_dedupe_keys"].append({"id": r["id"], "old": old, "new": nk, "home": r["home"]})

    used = set()
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
        if len(cluster) > 1:
            issues["duplicate_groups"].append(
                [{"id": x["id"], "home": x["home"], "away": x["away"], "source": x["source"]} for x in cluster]
            )
    return issues


def heal(con: sqlite3.Connection, issues: Dict[str, Any]) -> Dict[str, Any]:
    deleted_bogus = 0
    merged = 0
    keys_rewritten = 0

    for item in issues.get("bogus_swiss") or []:
        mid = item["id"]
        for table in ("predictions", "match_features", "prediction_log", "scorecard"):
            if con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone():
                try:
                    con.execute(f"DELETE FROM {table} WHERE match_id=?", (mid,))
                except Exception:
                    pass
        con.execute("DELETE FROM matches WHERE id=?", (mid,))
        deleted_bogus += 1

    rows = [dict(r) for r in con.execute("SELECT * FROM matches").fetchall()]
    used = set()
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
        if len(cluster) == 1:
            continue
        items = sorted(cluster, key=_score_row, reverse=True)
        keep = dict(items[0])
        for d0 in items[1:]:
            d = dict(d0)
            if not keep.get("kickoff_utc") and d.get("kickoff_utc"):
                keep["kickoff_utc"] = d["kickoff_utc"]
                keep["kickoff_hkt"] = d.get("kickoff_hkt")
            if keep.get("home_score") is None and d.get("home_score") is not None:
                keep["home_score"] = d["home_score"]
                keep["away_score"] = d["away_score"]
            if not keep.get("venue") and d.get("venue"):
                keep["venue"] = d["venue"]
            if d.get("source") == "espn":
                keep["home"] = d["home"]
                keep["away"] = d["away"]
                if d.get("venue"):
                    keep["venue"] = d["venue"]
                keep["source"] = "espn"
                keep["source_id"] = d.get("source_id")
            elif len(d.get("home") or "") > len(keep.get("home") or ""):
                keep["home"] = d["home"]
            if len(d.get("away") or "") > len(keep.get("away") or "") and d.get("source") != "espn":
                keep["away"] = d["away"]
            keep["extra"] = _merge_extra(keep.get("extra"), d.get("extra"))
            for table in ("predictions", "match_features", "prediction_log", "scorecard"):
                if not con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone():
                    continue
                keep_has = con.execute(
                    f"SELECT 1 FROM {table} WHERE match_id=? LIMIT 1", (keep["id"],)
                ).fetchone()
                if not keep_has:
                    drops = con.execute(f"SELECT * FROM {table} WHERE match_id=?", (d["id"],)).fetchall()
                    con.execute(f"DELETE FROM {table} WHERE match_id=?", (d["id"],))
                    for dr in drops:
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
            merged += 1
        nk = dedupe_key(keep)
        extra = keep.get("extra")
        if not isinstance(extra, str):
            extra = json.dumps(extra or {}, ensure_ascii=False)
        con.execute(
            """UPDATE matches SET home=?, away=?, kickoff_utc=?, kickoff_hkt=?, venue=?,
               extra=?, source=?, source_id=?, dedupe_key=? WHERE id=?""",
            (
                keep["home"], keep["away"], keep.get("kickoff_utc"), keep.get("kickoff_hkt"),
                keep.get("venue"), extra, keep.get("source"), keep.get("source_id"), nk, keep["id"],
            ),
        )

    # Rewrite all dedupe keys (two-phase to avoid UNIQUE clashes)
    ids = [r[0] for r in con.execute("SELECT id FROM matches").fetchall()]
    for i in ids:
        con.execute("UPDATE matches SET dedupe_key=? WHERE id=?", (f"tmp-watchdog-{i}", i))
    for r in con.execute(
        "SELECT id, date, home, away, league, country, competition_key FROM matches"
    ).fetchall():
        rd = dict(r)
        nk = dedupe_key(rd)
        con.execute("UPDATE matches SET dedupe_key=? WHERE id=?", (nk, rd["id"]))
        keys_rewritten += 1

    con.commit()
    return {
        "deleted_bogus_swiss": deleted_bogus,
        "merged_duplicates": merged,
        "keys_rewritten": keys_rewritten,
        "remaining": con.execute("SELECT COUNT(*) FROM matches").fetchone()[0],
    }


def run(auto_heal: bool = True) -> Dict[str, Any]:
    init_db()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    before = find_issues(con)
    heal_result = None
    after = before
    if auto_heal and (
        before["bogus_swiss"] or before["duplicate_groups"] or before["stale_dedupe_keys"]
    ):
        heal_result = heal(con, before)
        after = find_issues(con)
    report = {
        "before": {
            "match_count": before["match_count"],
            "bogus_swiss_n": len(before["bogus_swiss"]),
            "duplicate_groups_n": len(before["duplicate_groups"]),
            "stale_dedupe_keys_n": len(before["stale_dedupe_keys"]),
            "bogus_swiss": before["bogus_swiss"][:20],
            "duplicate_groups": before["duplicate_groups"][:10],
        },
        "heal": heal_result,
        "after": {
            "match_count": after["match_count"],
            "bogus_swiss_n": len(after["bogus_swiss"]),
            "duplicate_groups_n": len(after["duplicate_groups"]),
            "stale_dedupe_keys_n": len(after["stale_dedupe_keys"]),
        },
        "ok": len(after["bogus_swiss"]) == 0 and len(after["duplicate_groups"]) == 0,
        "checked_at": before["checked_at"],
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    con.close()
    return report


def main() -> int:
    report = run(auto_heal=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
