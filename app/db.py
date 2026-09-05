"""SQLite persistence for matches, ratings, predictions, features."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "football.db"
HKT = timezone(timedelta(hours=8))

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT,
    kickoff_utc TEXT,
    kickoff_hkt TEXT,
    league TEXT,
    country TEXT,
    competition_key TEXT,
    home TEXT,
    away TEXT,
    home_score INTEGER,
    away_score INTEGER,
    status TEXT,
    venue TEXT,
    source TEXT,
    source_id TEXT,
    extra TEXT,
    dedupe_key TEXT UNIQUE,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS team_elo (
    team_norm TEXT PRIMARY KEY,
    elo REAL NOT NULL,
    matches_played INTEGER DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS predictions (
    match_id INTEGER PRIMARY KEY,
    p_home REAL,
    p_draw REAL,
    p_away REAL,
    score_home INTEGER,
    score_away INTEGER,
    lambda_home REAL,
    lambda_away REAL,
    model TEXT,
    features_json TEXT,
    created_at TEXT,
    FOREIGN KEY(match_id) REFERENCES matches(id)
);

CREATE TABLE IF NOT EXISTS match_features (
    match_id INTEGER PRIMARY KEY,
    features_json TEXT,
    updated_at TEXT,
    FOREIGN KEY(match_id) REFERENCES matches(id)
);

CREATE INDEX IF NOT EXISTS idx_matches_date ON matches(date);
CREATE INDEX IF NOT EXISTS idx_matches_status ON matches(status);
CREATE INDEX IF NOT EXISTS idx_matches_country ON matches(country);
CREATE INDEX IF NOT EXISTS idx_matches_comp ON matches(competition_key);
CREATE INDEX IF NOT EXISTS idx_matches_home ON matches(home);
CREATE INDEX IF NOT EXISTS idx_matches_away ON matches(away);

CREATE TABLE IF NOT EXISTS team_pi (
    team_norm TEXT PRIMARY KEY,
    home_pi REAL NOT NULL DEFAULT 0,
    away_pi REAL NOT NULL DEFAULT 0,
    updated_at TEXT
);

"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()



def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    d = dict(row)
    for k in ("extra", "features_json"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                pass
    return d


def upsert_matches(matches: Iterable[Dict[str, Any]]) -> int:
    """Insert or update matches by dedupe_key. Returns count written."""
    from app.competitions import match_competition
    from app.collector_core import dedupe_key

    now = datetime.now(timezone.utc).isoformat()
    n = 0
    with get_db() as conn:
        for m in matches:
            comp = match_competition(m.get("league"), m.get("country"))
            if not comp:
                continue
            key = dedupe_key(m)
            extra = m.get("extra") or {}
            if isinstance(extra, str):
                extra_s = extra
            else:
                extra_s = json.dumps(extra, ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO matches (
                    date, kickoff_utc, kickoff_hkt, league, country, competition_key,
                    home, away, home_score, away_score, status, venue, source, source_id,
                    extra, dedupe_key, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(dedupe_key) DO UPDATE SET
                    kickoff_utc=excluded.kickoff_utc,
                    kickoff_hkt=excluded.kickoff_hkt,
                    league=excluded.league,
                    country=excluded.country,
                    competition_key=excluded.competition_key,
                    home_score=COALESCE(excluded.home_score, matches.home_score),
                    away_score=COALESCE(excluded.away_score, matches.away_score),
                    status=CASE
                        WHEN excluded.status IN ('FINISHED','LIVE') THEN excluded.status
                        WHEN matches.status IN ('FINISHED','LIVE') THEN matches.status
                        ELSE COALESCE(excluded.status, matches.status)
                    END,
                    venue=COALESCE(excluded.venue, matches.venue),
                    source=excluded.source,
                    source_id=COALESCE(excluded.source_id, matches.source_id),
                    extra=excluded.extra,
                    updated_at=excluded.updated_at
                """,
                (
                    m.get("date"),
                    m.get("kickoff_utc"),
                    m.get("kickoff_hkt"),
                    comp["name"],
                    comp["country"],
                    comp["key"],
                    m.get("home"),
                    m.get("away"),
                    m.get("home_score"),
                    m.get("away_score"),
                    m.get("status"),
                    m.get("venue"),
                    m.get("source"),
                    m.get("source_id"),
                    extra_s,
                    key,
                    now,
                ),
            )
            n += 1
    return n


def query_matches(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    country: Optional[str] = None,
    competition_key: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    clauses = ["1=1"]
    params: List[Any] = []
    if date_from:
        clauses.append("date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("date <= ?")
        params.append(date_to)
    if country:
        clauses.append("country = ?")
        params.append(country)
    if competition_key:
        clauses.append("competition_key = ?")
        params.append(competition_key)
    if status:
        clauses.append("status = ?")
        params.append(status)
    sql = f"""
        SELECT * FROM matches
        WHERE {' AND '.join(clauses)}
        ORDER BY date, kickoff_utc, league, home
        LIMIT ?
    """
    params.append(limit)
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [row_to_dict(r) for r in rows]  # type: ignore


def get_match(match_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
    return row_to_dict(row)


def get_prediction(match_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM predictions WHERE match_id=?", (match_id,)).fetchone()
    d = row_to_dict(row)
    if d and "features_json" in d:
        feats = d.pop("features_json")
        d["features"] = feats if isinstance(feats, dict) else feats
        if isinstance(d["features"], dict):
            if "top_scorelines" in d["features"] and "top_scorelines" not in d:
                d["top_scorelines"] = d["features"]["top_scorelines"]
            if "consensus" in d["features"] and "consensus" not in d:
                d["consensus"] = d["features"]["consensus"]
    return d


def save_prediction(match_id: int, pred: Dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO predictions (
                match_id, p_home, p_draw, p_away, score_home, score_away,
                lambda_home, lambda_away, model, features_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(match_id) DO UPDATE SET
                p_home=excluded.p_home, p_draw=excluded.p_draw, p_away=excluded.p_away,
                score_home=excluded.score_home, score_away=excluded.score_away,
                lambda_home=excluded.lambda_home, lambda_away=excluded.lambda_away,
                model=excluded.model, features_json=excluded.features_json,
                created_at=excluded.created_at
            """,
            (
                match_id,
                pred["p_home"],
                pred["p_draw"],
                pred["p_away"],
                pred["score_home"],
                pred["score_away"],
                pred.get("lambda_home"),
                pred.get("lambda_away"),
                pred.get("model", "elo_poisson"),
                json.dumps(
                    {
                        **(pred.get("features") or {}),
                        **({"top_scorelines": pred["top_scorelines"]} if pred.get("top_scorelines") is not None else {}),
                        **({"consensus": pred["consensus"]} if pred.get("consensus") is not None else {}),
                    },
                    ensure_ascii=False,
                ),
                now,
            ),
        )


def save_features(match_id: int, features: Dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO match_features (match_id, features_json, updated_at)
            VALUES (?,?,?)
            ON CONFLICT(match_id) DO UPDATE SET
                features_json=excluded.features_json, updated_at=excluded.updated_at
            """,
            (match_id, json.dumps(features, ensure_ascii=False), now),
        )


def get_elo(team_norm: str) -> float:
    with get_db() as conn:
        row = conn.execute("SELECT elo FROM team_elo WHERE team_norm=?", (team_norm,)).fetchone()
    return float(row["elo"]) if row else 1500.0


def set_elo(team_norm: str, elo: float, matches_played: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO team_elo (team_norm, elo, matches_played, updated_at)
            VALUES (?,?,?,?)
            ON CONFLICT(team_norm) DO UPDATE SET
                elo=excluded.elo, matches_played=excluded.matches_played, updated_at=excluded.updated_at
            """,
            (team_norm, elo, matches_played, now),
        )


def all_finished_chronological() -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM matches
            WHERE status='FINISHED' AND home_score IS NOT NULL AND away_score IS NOT NULL
            ORDER BY date, kickoff_utc, id
            """
        ).fetchall()
    return [row_to_dict(r) for r in rows]  # type: ignore


def team_matches_before(team: str, before_date: str, limit: int = 20) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM matches
            WHERE status='FINISHED' AND home_score IS NOT NULL
              AND date < ? AND (home=? OR away=?)
            ORDER BY date DESC, kickoff_utc DESC
            LIMIT ?
            """,
            (before_date, team, team, limit),
        ).fetchall()
    return [row_to_dict(r) for r in rows]  # type: ignore


def accuracy_stats() -> Dict[str, Any]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT m.home_score, m.away_score, p.p_home, p.p_draw, p.p_away,
                   p.score_home, p.score_away
            FROM predictions p
            JOIN matches m ON m.id = p.match_id
            WHERE m.status='FINISHED' AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
            """
        ).fetchall()
    if not rows:
        return {
            "n": 0,
            "result_accuracy": None,
            "exact_score_accuracy": None,
            "brier": None,
            "note": "No finished matches with stored predictions yet.",
        }
    correct_result = 0
    exact = 0
    brier_sum = 0.0
    for r in rows:
        hs, aws = int(r["home_score"]), int(r["away_score"])
        if hs > aws:
            outcome = (1, 0, 0)
            pred_out = "H"
        elif hs < aws:
            outcome = (0, 0, 1)
            pred_out = "A"
        else:
            outcome = (0, 1, 0)
            pred_out = "D"
        ph, pd, pa = float(r["p_home"]), float(r["p_draw"]), float(r["p_away"])
        pred_label = max([("H", ph), ("D", pd), ("A", pa)], key=lambda x: x[1])[0]
        if pred_label == pred_out:
            correct_result += 1
        if int(r["score_home"]) == hs and int(r["score_away"]) == aws:
            exact += 1
        brier_sum += (ph - outcome[0]) ** 2 + (pd - outcome[1]) ** 2 + (pa - outcome[2]) ** 2
    n = len(rows)
    return {
        "n": n,
        "result_accuracy": round(correct_result / n, 4),
        "exact_score_accuracy": round(exact / n, 4),
        "brier": round(brier_sum / n, 4),
        "note": "Brier score over 3-way outcome vectors (lower is better; random≈0.67).",
    }


def counts() -> Dict[str, Any]:
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM matches").fetchone()["c"]
        by_status = {
            r["status"]: r["c"]
            for r in conn.execute(
                "SELECT status, COUNT(*) c FROM matches GROUP BY status"
            ).fetchall()
        }
        by_country = {
            r["country"]: r["c"]
            for r in conn.execute(
                "SELECT country, COUNT(*) c FROM matches GROUP BY country ORDER BY c DESC"
            ).fetchall()
        }
        preds = conn.execute("SELECT COUNT(*) c FROM predictions").fetchone()["c"]
    return {"matches": total, "by_status": by_status, "by_country": by_country, "predictions": preds}


# --- Self-learning tables (appended to SCHEMA via ensure_learning_schema) ---

LEARNING_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_params (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS learning_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at TEXT NOT NULL,
    finished_n INTEGER,
    result_acc REAL,
    exact_acc REAL,
    brier REAL,
    params_json TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS prediction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL,
    p_home REAL,
    p_draw REAL,
    p_away REAL,
    score_home INTEGER,
    score_away INTEGER,
    lambda_home REAL,
    lambda_away REAL,
    model TEXT,
    params_json TEXT,
    created_at TEXT,
    FOREIGN KEY(match_id) REFERENCES matches(id)
);

CREATE INDEX IF NOT EXISTS idx_prediction_log_match ON prediction_log(match_id);
CREATE INDEX IF NOT EXISTS idx_learning_runs_ran ON learning_runs(ran_at);
"""


EXPERT_SCHEMA = """
CREATE TABLE IF NOT EXISTS expert_tips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_date TEXT,
    home TEXT,
    away TEXT,
    home_norm TEXT,
    away_norm TEXT,
    match_id INTEGER,
    source TEXT,
    analyst_name TEXT,
    pick_1x2 TEXT,
    score_home INTEGER,
    score_away INTEGER,
    confidence REAL,
    url TEXT,
    notes TEXT,
    created_at TEXT,
    FOREIGN KEY(match_id) REFERENCES matches(id)
);

CREATE INDEX IF NOT EXISTS idx_expert_tips_date ON expert_tips(match_date);
CREATE INDEX IF NOT EXISTS idx_expert_tips_home ON expert_tips(home_norm);
CREATE INDEX IF NOT EXISTS idx_expert_tips_away ON expert_tips(away_norm);
CREATE INDEX IF NOT EXISTS idx_expert_tips_match ON expert_tips(match_id);

CREATE TABLE IF NOT EXISTS media_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    published_at TEXT,
    title TEXT,
    summary TEXT,
    url TEXT,
    source TEXT,
    team_keywords TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_media_notes_published ON media_notes(published_at);
"""

DEFAULT_MODEL_PARAMS = {
    "home_adv_elo": 80.0,
    "home_adv_goals": 0.25,
    "avg_goals": 1.45,  # raised vs 1.35 to soften 1-1/1-0 mode bias
    "k_factor": 20.0,
    "rho": -0.05,
    "calibration_home": 1.0,
    "calibration_draw": 1.0,
    "calibration_away": 1.0,
    # Consensus blend weights (learning may tune later)
    "blend_w_model": 0.40,
    "blend_w_clubelo": 0.35,
    "blend_w_market": 0.25,
    "blend_w_model_2": 0.55,
    "blend_w_clubelo_2": 0.45,
    "blend_w_experts": 0.15,
}


def ensure_learning_schema(conn: Optional[sqlite3.Connection] = None) -> None:
    """Create learning-related tables if missing."""
    if conn is not None:
        conn.executescript(LEARNING_SCHEMA)
        conn.executescript(EXPERT_SCHEMA)
        return
    with get_db() as c:
        c.executescript(LEARNING_SCHEMA)
        c.executescript(EXPERT_SCHEMA)


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(SCHEMA)
        conn.executescript(LEARNING_SCHEMA)
        conn.executescript(EXPERT_SCHEMA)
        # Seed default model params if empty
        row = conn.execute("SELECT COUNT(*) c FROM model_params").fetchone()
        if row and int(row["c"]) == 0:
            now = datetime.now(timezone.utc).isoformat()
            for k, v in DEFAULT_MODEL_PARAMS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO model_params (key, value, updated_at) VALUES (?,?,?)",
                    (k, float(v), now),
                )


def get_model_params() -> Dict[str, float]:
    params = dict(DEFAULT_MODEL_PARAMS)
    try:
        with get_db() as conn:
            ensure_learning_schema(conn)
            rows = conn.execute("SELECT key, value FROM model_params").fetchall()
        for r in rows:
            params[str(r["key"])] = float(r["value"])
    except sqlite3.Error:
        pass
    return params


def set_model_params(params: Dict[str, float]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        ensure_learning_schema(conn)
        for k, v in params.items():
            conn.execute(
                """
                INSERT INTO model_params (key, value, updated_at) VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (k, float(v), now),
            )


def append_learning_run(
    finished_n: int,
    result_acc: Optional[float],
    exact_acc: Optional[float],
    brier: Optional[float],
    params: Dict[str, float],
    notes: str = "",
) -> int:
    now = datetime.now(HKT).isoformat()
    with get_db() as conn:
        ensure_learning_schema(conn)
        cur = conn.execute(
            """
            INSERT INTO learning_runs
                (ran_at, finished_n, result_acc, exact_acc, brier, params_json, notes)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                now,
                finished_n,
                result_acc,
                exact_acc,
                brier,
                json.dumps(params, ensure_ascii=False),
                notes,
            ),
        )
        return int(cur.lastrowid)


def latest_learning_run() -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        ensure_learning_schema(conn)
        row = conn.execute(
            "SELECT * FROM learning_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get("params_json"), str):
        try:
            d["params"] = json.loads(d["params_json"])
        except json.JSONDecodeError:
            d["params"] = {}
    else:
        d["params"] = {}
    return d


def log_prediction_snapshot(match_id: int, pred: Dict[str, Any], params: Optional[Dict[str, float]] = None) -> None:
    """Snapshot probs before kickoff for later calibration (append-only)."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        ensure_learning_schema(conn)
        # Avoid duplicate snapshots within same minute for same match
        conn.execute(
            """
            INSERT INTO prediction_log (
                match_id, p_home, p_draw, p_away, score_home, score_away,
                lambda_home, lambda_away, model, params_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                match_id,
                pred.get("p_home"),
                pred.get("p_draw"),
                pred.get("p_away"),
                pred.get("score_home"),
                pred.get("score_away"),
                pred.get("lambda_home"),
                pred.get("lambda_away"),
                pred.get("model", "elo_poisson_dixon_coles"),
                json.dumps(params or {}, ensure_ascii=False),
                now,
            ),
        )


def finished_with_predictions(limit: int = 200) -> List[Dict[str, Any]]:
    """Recent finished matches that have stored predictions (newest first, then reverse chrono for eval)."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT m.id, m.date, m.kickoff_utc, m.home, m.away, m.home_score, m.away_score,
                   m.competition_key, m.status, m.league, m.country, m.venue,
                   p.p_home, p.p_draw, p.p_away, p.score_home AS pred_score_home,
                   p.score_away AS pred_score_away, p.lambda_home, p.lambda_away,
                   p.features_json
            FROM predictions p
            JOIN matches m ON m.id = p.match_id
            WHERE m.status='FINISHED' AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
            ORDER BY m.date DESC, m.kickoff_utc DESC, m.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = row_to_dict(r)
        if d and isinstance(d.get("features_json"), str):
            try:
                d["features"] = json.loads(d["features_json"])
            except json.JSONDecodeError:
                d["features"] = {}
        elif d:
            d["features"] = d.get("features_json") or {}
        out.append(d)
    return list(reversed(out))  # chronological for stable eval


def ensure_expert_schema() -> None:
    with get_db() as conn:
        conn.executescript(EXPERT_SCHEMA)


def upsert_expert_tip(tip: Dict[str, Any]) -> int:
    """Insert an expert tip row. Returns new id."""
    from app.collector_core import _norm_team
    now = datetime.now(timezone.utc).isoformat()
    home = tip.get("home") or ""
    away = tip.get("away") or ""
    pick = (tip.get("pick_1x2") or "").strip().upper()
    if pick in ("1", "HOME", "H", "主", "主勝"):
        pick = "H"
    elif pick in ("X", "DRAW", "D", "和", "和局"):
        pick = "D"
    elif pick in ("2", "AWAY", "A", "客", "客勝"):
        pick = "A"
    with get_db() as conn:
        ensure_learning_schema(conn)
        cur = conn.execute(
            """
            INSERT INTO expert_tips (
                match_date, home, away, home_norm, away_norm, match_id,
                source, analyst_name, pick_1x2, score_home, score_away,
                confidence, url, notes, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                tip.get("match_date") or tip.get("date"),
                home,
                away,
                tip.get("home_norm") or _norm_team(home),
                tip.get("away_norm") or _norm_team(away),
                tip.get("match_id"),
                tip.get("source") or "manual",
                tip.get("analyst_name") or tip.get("analyst") or "",
                pick or None,
                tip.get("score_home"),
                tip.get("score_away"),
                float(tip["confidence"]) if tip.get("confidence") not in (None, "") else None,
                tip.get("url"),
                tip.get("notes"),
                tip.get("created_at") or now,
            ),
        )
        return int(cur.lastrowid)


def expert_tips_for_match(
    home: str,
    away: str,
    match_date: Optional[str] = None,
    match_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    from app.collector_core import _norm_team
    hn, an = _norm_team(home), _norm_team(away)
    with get_db() as conn:
        ensure_learning_schema(conn)
        rows = []
        if match_id:
            rows = conn.execute(
                "SELECT * FROM expert_tips WHERE match_id=? ORDER BY id",
                (match_id,),
            ).fetchall()
        if not rows:
            if match_date:
                rows = conn.execute(
                    """
                    SELECT * FROM expert_tips
                    WHERE home_norm=? AND away_norm=?
                      AND (match_date IS NULL OR match_date=? OR match_date=?)
                    ORDER BY id
                    """,
                    (hn, an, match_date, (match_date or "")[:10]),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM expert_tips WHERE home_norm=? AND away_norm=? ORDER BY id",
                    (hn, an),
                ).fetchall()
        # Fuzzy fallback: same date, soft name containment
        if not rows and match_date:
            cand = conn.execute(
                "SELECT * FROM expert_tips WHERE match_date=? OR match_date=? ORDER BY id",
                (match_date, (match_date or "")[:10]),
            ).fetchall()
            out = []
            for r in cand:
                d = dict(r)
                rh, ra = (d.get("home_norm") or ""), (d.get("away_norm") or "")
                if (hn and rh and (hn in rh or rh in hn)) and (an and ra and (an in ra or ra in an)):
                    out.append(d)
            return out
    return [dict(r) for r in rows]


def insert_media_note(note: Dict[str, Any]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    kws = note.get("team_keywords")
    if isinstance(kws, (list, tuple)):
        kws = json.dumps(list(kws), ensure_ascii=False)
    with get_db() as conn:
        ensure_learning_schema(conn)
        # Dedupe by url
        url = note.get("url")
        if url:
            existing = conn.execute("SELECT id FROM media_notes WHERE url=?", (url,)).fetchone()
            if existing:
                return int(existing["id"])
        cur = conn.execute(
            """
            INSERT INTO media_notes (published_at, title, summary, url, source, team_keywords, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                note.get("published_at"),
                note.get("title"),
                note.get("summary"),
                url,
                note.get("source"),
                kws,
                now,
            ),
        )
        return int(cur.lastrowid)


def media_notes_for_teams(home: str, away: str, limit: int = 8) -> List[Dict[str, Any]]:
    """Loose keyword match of media_notes to home/away names."""
    from app.collector_core import _norm_team
    tokens = set()
    for name in (home, away):
        n = _norm_team(name)
        for tok in n.split():
            if len(tok) >= 4:
                tokens.add(tok)
        # also raw significant words
        for tok in (name or "").lower().replace("-", " ").split():
            if len(tok) >= 4 and tok.isascii():
                tokens.add(tok)
    if not tokens:
        return []
    with get_db() as conn:
        ensure_learning_schema(conn)
        rows = conn.execute(
            "SELECT * FROM media_notes ORDER BY published_at DESC, id DESC LIMIT 80"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        blob = " ".join(
            str(x or "") for x in (d.get("title"), d.get("summary"), d.get("team_keywords"))
        ).lower()
        hits = [t for t in tokens if t in blob]
        if hits:
            d["matched_keywords"] = hits
            out.append(d)
        if len(out) >= limit:
            break
    return out

