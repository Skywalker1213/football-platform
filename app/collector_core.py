#!/usr/bin/env python3
"""
Reusable football-match data collector.

Sources (free, no user API keys required):
  1. ESPN public scoreboard JSON (primary — broad league coverage)
  2. TheSportsDB free API key "3" (secondary)
  3. OpenLigaDB (German leagues fallback)
  4. football-data.org v4 (optional; skipped if token missing / empty)

Usage:
  python collect.py
  python collect.py --date 2026-09-05
  python collect.py --from 2026-09-03 --to 2026-09-06
  python collect.py --from 2026-09-05 --to 2026-09-06 --out data/weekend
"""

from __future__ import annotations

import argparse
import re
import csv
import json
import os
import sys
import time
import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import requests  # type: ignore

    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache"
DATA_DIR = ROOT / "data"
DEFAULT_OUT = DATA_DIR / "matches"

HKT = timezone(timedelta(hours=8))
UTC = timezone.utc

# Polite defaults
REQUEST_TIMEOUT = 20
MIN_INTERVAL_SEC = 0.55
CACHE_TTL_SEC = 6 * 60 * 60  # 6 hours

# ESPN soccer league slugs (public scoreboard endpoints)
ESPN_LEAGUES: List[Tuple[str, str, str]] = [
    # Level-1 domestic + UCL + internationals only
    ("eng.1", "England", "English Premier League"),
    ("esp.1", "Spain", "Spanish La Liga"),
    ("ger.1", "Germany", "German Bundesliga"),
    ("ned.1", "Netherlands", "Dutch Eredivisie"),
    ("sco.1", "Scotland", "Scottish Premiership"),
    ("sui.1", "Switzerland", "Swiss Super League"),
    ("uefa.champions", "Europe", "UEFA Champions League"),
    ("uefa.nations", "International", "UEFA Nations League"),
    ("fifa.worldq", "International", "FIFA World Cup Qualifiers"),
]

OPENLIGA_SHORTCUTS = [
    ("bl1", "Germany", "1. Bundesliga"),
]

_last_request_at = 0.0
_source_notes: List[str] = []


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def today_hkt() -> date:
    return datetime.now(HKT).date()


def parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def daterange(start: date, end: date) -> List[date]:
    if end < start:
        raise ValueError("--to must be on/after --from")
    out = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def ensure_dirs() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def cache_path(url: str) -> Path:
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:40]
    return CACHE_DIR / f"{h}.json"


def throttle() -> None:
    global _last_request_at
    now = time.monotonic()
    wait = MIN_INTERVAL_SEC - (now - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def http_get_json(url: str, headers: Optional[Dict[str, str]] = None, use_cache: bool = True) -> Any:
    """GET JSON with disk cache, timeout, polite throttling, and 403 backoff.

    Prefer stdlib urllib — some CDNs (ESPN) block the requests library fingerprint
    from datacenter IPs more aggressively.
    """
    ensure_dirs()
    cp = cache_path(url + "|" + json.dumps(headers or {}, sort_keys=True))
    if use_cache and cp.exists():
        age = time.time() - cp.stat().st_mtime
        if age < CACHE_TTL_SEC:
            try:
                return json.loads(cp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass

    hdrs = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; football-data-collector/1.0; "
            "+educational research; polite cache)"
        ),
        "Accept": "application/json,text/plain,*/*",
    }
    if headers:
        hdrs.update(headers)

    last_err: Optional[Exception] = None
    for attempt in range(4):
        throttle()
        try:
            req = Request(url, headers=hdrs)
            with urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                raw = r.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            try:
                cp.write_text(json.dumps(data), encoding="utf-8")
            except OSError:
                pass
            return data
        except HTTPError as e:
            last_err = e
            if e.code == 404:
                return None
            if e.code in (403, 429, 503):
                sleep_for = 2.5 * (attempt + 1)
                log(f"  HTTP {e.code} for {url[:80]}… backing off {sleep_for:.1f}s")
                time.sleep(sleep_for)
                continue
            raise
        except (URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
            continue
    if last_err:
        raise last_err
    return None



def http_get_text(url: str, use_cache: bool = True) -> Optional[str]:
    """GET text/CSV with disk cache."""
    ensure_dirs()
    cp = cache_path(url + "|text")
    if use_cache and cp.exists():
        age = time.time() - cp.stat().st_mtime
        if age < CACHE_TTL_SEC:
            return cp.read_text(encoding="utf-8")
    last_err: Optional[Exception] = None
    for attempt in range(3):
        throttle()
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; football-data-collector/1.0)",
                    "Accept": "text/csv,text/plain,*/*",
                },
            )
            with urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                raw = r.read().decode("utf-8-sig", errors="replace")
            try:
                cp.write_text(raw, encoding="utf-8")
            except OSError:
                pass
            return raw
        except HTTPError as e:
            last_err = e
            if e.code == 404:
                return None
            time.sleep(2 * (attempt + 1))
        except (URLError, TimeoutError) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    if last_err:
        log(f"  text fetch failed: {url} ({last_err})")
    return None


def empty_match() -> Dict[str, Any]:
    return {
        "date": None,
        "kickoff_utc": None,
        "kickoff_hkt": None,
        "league": None,
        "country": None,
        "home": None,
        "away": None,
        "home_score": None,
        "away_score": None,
        "status": None,
        "venue": None,
        "source": None,
        "source_id": None,
        "extra": {},
    }


def parse_iso_to_utc_hkt(iso: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (date_ymd, kickoff_utc_iso, kickoff_hkt_iso)."""
    if not iso:
        return None, None, None
    s = iso.strip()
    # Normalize Z
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # ESPN sometimes uses 2026-09-05T11:30Z already handled
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Try without seconds / with space
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(iso.replace("Z", ""), fmt)
                dt = dt.replace(tzinfo=UTC)
                break
            except ValueError:
                dt = None  # type: ignore
        if dt is None:
            return None, None, None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    utc = dt.astimezone(UTC)
    hkt = dt.astimezone(HKT)
    return utc.date().isoformat(), utc.isoformat(), hkt.isoformat()


def norm_status(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = raw.strip().upper()
    mapping = {
        "NS": "SCHEDULED",
        "NOT STARTED": "SCHEDULED",
        "STATUS_SCHEDULED": "SCHEDULED",
        "STATUS_IN_PROGRESS": "LIVE",
        "STATUS_HALFTIME": "LIVE",
        "STATUS_FIRST_HALF": "LIVE",
        "STATUS_SECOND_HALF": "LIVE",
        "STATUS_END_PERIOD": "LIVE",
        "IN PLAY": "LIVE",
        "1H": "LIVE",
        "2H": "LIVE",
        "HT": "LIVE",
        "LIVE": "LIVE",
        "FT": "FINISHED",
        "AET": "FINISHED",
        "PEN": "FINISHED",
        "STATUS_FULL_TIME": "FINISHED",
        "STATUS_FINAL": "FINISHED",
        "FINAL": "FINISHED",
        "MATCH FINISHED": "FINISHED",
        "PST": "POSTPONED",
        "POSTP": "POSTPONED",
        "STATUS_POSTPONED": "POSTPONED",
        "CANC": "CANCELLED",
        "STATUS_CANCELED": "CANCELLED",
        "STATUS_CANCELLED": "CANCELLED",
        "ABD": "ABANDONED",
        "SUSP": "SUSPENDED",
    }
    return mapping.get(s, s)


_TEAM_ALIASES = {
    "man city": "manchester city",
    "man utd": "manchester united",
    "manchester utd": "manchester united",
    "man united": "manchester united",
    "spurs": "tottenham hotspur",
    "tottenham": "tottenham hotspur",
    "nottingham forest": "nottingham forest",
    "nott m forest": "nottingham forest",
    "nottm forest": "nottingham forest",
    "hull": "hull city",
    "newcastle": "newcastle united",
    "wolves": "wolverhampton wanderers",
    "wolverhampton": "wolverhampton wanderers",
    "brighton": "brighton and hove albion",
    "brighton and hove albion": "brighton and hove albion",
    "leicester": "leicester city",
    "leicester city": "leicester city",
    "west ham": "west ham united",
    "birmingham": "birmingham city",
    "cardiff": "cardiff city",
    "swansea": "swansea city",
    "leeds": "leeds united",
    "inter": "inter milan",
    "milan": "ac milan",
    "psg": "paris saint germain",
    "paris sg": "paris saint germain",
    "paris saint germain": "paris saint germain",
    "ath bilbao": "athletic club",
    "athletic bilbao": "athletic club",
    "atletico": "atletico madrid",
    "atletico madrid": "atletico madrid",
    "betis": "real betis",
    "sociedad": "real sociedad",
    "real sociedad": "real sociedad",
    # Scotland
    "hearts": "heart of midlothian",
    "heart of midlothian": "heart of midlothian",
    "hibs": "hibernian",
    "hibernian": "hibernian",
    "rangers": "rangers",
    "celtic": "celtic",
    "st mirren": "st mirren",
    "st johnstone": "st johnstone",
    "dundee utd": "dundee united",
    "dundee united": "dundee united",
    # Spain
    "celta": "celta vigo",
    "celta vigo": "celta vigo",
    "rc celta": "celta vigo",
    "sevilla": "sevilla",
    "barca": "barcelona",
    "barcelona": "barcelona",
    "espanyol": "espanyol",
    "villarreal": "villarreal",
    "valencia": "valencia",
    "getafe": "getafe",
    "osasuna": "osasuna",
    "mallorca": "mallorca",
    "girona": "girona",
    "alaves": "alaves",
    "rayo": "rayo vallecano",
    "rayo vallecano": "rayo vallecano",
    # Netherlands
    "zwolle": "pec zwolle",
    "pec zwolle": "pec zwolle",
    "ajax": "ajax",
    "psv": "psv eindhoven",
    "psv eindhoven": "psv eindhoven",
    "feyenoord": "feyenoord",
    "az": "az alkmaar",
    "az alkmaar": "az alkmaar",
    "sparta rotterdam": "sparta rotterdam",
    "twente": "twente",
    "utrecht": "utrecht",
    "heerenveen": "heerenveen",
    "nec": "nec nijmegen",
    "nec nijmegen": "nec nijmegen",
    "goa ahead eagles": "go ahead eagles",
    "go ahead eagles": "go ahead eagles",
    # Germany
    "koln": "koln",
    "fc koln": "koln",
    "1 fc koln": "koln",
    "cologne": "koln",
    "bayern": "bayern munich",
    "bayern munich": "bayern munich",
    "bayern munchen": "bayern munich",
    "dortmund": "borussia dortmund",
    "borussia dortmund": "borussia dortmund",
    "bvb": "borussia dortmund",
    "leverkusen": "bayer leverkusen",
    "bayer leverkusen": "bayer leverkusen",
    "gladbach": "borussia monchengladbach",
    "monchengladbach": "borussia monchengladbach",
    "borussia monchengladbach": "borussia monchengladbach",
    "leipzig": "rb leipzig",
    "rb leipzig": "rb leipzig",
    "frankfurt": "eintracht frankfurt",
    "eintracht frankfurt": "eintracht frankfurt",
    "stuttgart": "stuttgart",
    "vfb stuttgart": "stuttgart",
    "wolfsburg": "wolfsburg",
    "mainz": "mainz",
    "augsburg": "augsburg",
    "freiburg": "freiburg",
    "hoffenheim": "hoffenheim",
    "union berlin": "union berlin",
    "werder bremen": "werder bremen",
    "bremen": "werder bremen",
    "heidenheim": "heidenheim",
    "st pauli": "st pauli",
    "coventry": "coventry city",
    "coventry city": "coventry city",
    "ipswich": "ipswich town",
    "ipswich town": "ipswich town",
    "nijmegen": "nec nijmegen",
    "feyenoord rotterdam": "feyenoord",
    "ajax amsterdam": "ajax",
    "schalke 04": "schalke 04",
    "fc schalke 04": "schalke 04",
    "hamburg": "hamburg sv",
    "hamburg sv": "hamburg sv",
    "vallecano": "rayo vallecano",
    "santander": "racing santander",
    "racing santander": "racing santander",
    "elversberg": "sv elversberg",
    "sv 07 elversberg": "sv elversberg",
    "sv elversberg": "sv elversberg",
    "paderborn": "sc paderborn 07",
    "sc paderborn 07": "sc paderborn 07",
    "tsg hoffenheim": "hoffenheim",
    "union berlin": "union berlin",
    "1 fc union berlin": "union berlin",
    "fc cologne": "koln",
    "cologne": "koln",
}



def _norm_team(name: Optional[str]) -> str:
    s = (name or "").lower().strip()
    s = s.replace("&", " and ")
    s = s.replace("ü", "u").replace("ö", "o").replace("ä", "a").replace("ß", "ss")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # drop common club prefixes/suffixes for matching
    base = re.sub(r"\b(fc|cf|afc|sc|fk|sk|ac|rc|vfb|sv|tsv|1)\b", " ", s)
    base = re.sub(r"\s+", " ", base).strip()
    for cand in (s, base):
        if cand in _TEAM_ALIASES:
            return _TEAM_ALIASES[cand]
    return base or s


def teams_likely_same(a: Optional[str], b: Optional[str]) -> bool:
    """True if two display names refer to the same club (alias or containment)."""
    na, nb = _norm_team(a), _norm_team(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # containment: "celta" vs "celta vigo", "zwolle" vs "pec zwolle"
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(shorter) >= 4 and (longer == shorter or longer.startswith(shorter + " ") or longer.endswith(" " + shorter) or f" {shorter} " in f" {longer} "):
        return True
    # token overlap (majority of shorter tokens in longer)
    ta, tb = set(na.split()), set(nb.split())
    if not ta or not tb:
        return False
    inter = ta & tb
    if len(inter) >= 1 and (inter == ta or inter == tb):
        return True
    return False


def _norm_league(name: Optional[str]) -> str:
    s = (name or "").lower().strip()
    s = s.replace("laliga", "la liga")
    replacements = [
        ("english premier league", "premier league"),
        ("english league championship", "championship"),
        ("english league one", "league one"),
        ("english league two", "league two"),
        ("german bundesliga", "bundesliga"),
        ("1. fußball-bundesliga", "bundesliga"),
        ("1. fussball-bundesliga", "bundesliga"),
        ("italian serie a", "serie a"),
        ("french ligue 1", "ligue 1"),
        ("spanish la liga", "la liga"),
        ("spanish laliga", "la liga"),
        ("dutch eredivisie", "eredivisie"),
        ("portuguese primeira liga", "primeira liga"),
        ("belgian pro league", "pro league"),
        ("scottish premiership", "scottish premiership"),
        ("american usl championship", "usl championship"),
    ]
    for a, b in replacements:
        if a in s:
            return b
    # strip season suffixes like 2025/2026
    s = re.sub(r"\b20\d{2}(/\d{2,4})?\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def dedupe_key(m: Dict[str, Any]) -> str:
    """Stable identity: date + teams + competition_key (not raw league label).

    ESPN uses "Scottish Premiership" while football-data.co.uk uses "Premiership";
    both map to sco_pre — using competition_key prevents duplicate UI rows.
    """
    comp = (m.get("competition_key") or "").strip()
    if not comp:
        try:
            from app.competitions import match_competition
            c = match_competition(m.get("league"), m.get("country"))
            comp = (c or {}).get("key") or ""
        except Exception:
            comp = ""
    if not comp:
        comp = _norm_league(m.get("league"))
    return "|".join(
        [
            (m.get("date") or ""),
            _norm_team(m.get("home")),
            _norm_team(m.get("away")),
            comp,
        ]
    )


def merge_matches(existing: Dict[str, Dict[str, Any]], new_list: List[Dict[str, Any]]) -> None:
    for m in new_list:
        if not m.get("home") or not m.get("away"):
            continue
        k = dedupe_key(m)
        if k not in existing:
            existing[k] = m
            continue
        # Prefer richer record: fill nulls; prefer non-null scores/venue
        cur = existing[k]
        for field in (
            "kickoff_utc",
            "kickoff_hkt",
            "country",
            "home_score",
            "away_score",
            "status",
            "venue",
            "source_id",
        ):
            if cur.get(field) in (None, "", []) and m.get(field) not in (None, "", []):
                cur[field] = m[field]
        # Prefer FINISHED / LIVE status over SCHEDULED if both present
        prefer = {"FINISHED": 3, "LIVE": 2, "SCHEDULED": 1}
        if prefer.get(str(m.get("status")), 0) > prefer.get(str(cur.get("status")), 0):
            cur["status"] = m["status"]
            if m.get("home_score") is not None:
                cur["home_score"] = m["home_score"]
            if m.get("away_score") is not None:
                cur["away_score"] = m["away_score"]
        extra = dict(cur.get("extra") or {})
        extra.update(m.get("extra") or {})
        sources = set(extra.get("sources") or [])
        sources.add(m.get("source") or "")
        sources.add(cur.get("source") or "")
        sources.discard("")
        extra["sources"] = sorted(sources)
        cur["extra"] = extra


# ---------------------------------------------------------------------------
# ESPN
# ---------------------------------------------------------------------------

def fetch_espn_day(day: date) -> List[Dict[str, Any]]:
    ymd = day.strftime("%Y%m%d")
    matches: List[Dict[str, Any]] = []
    failures = 0
    for slug, country_hint, league_fallback in ESPN_LEAGUES:
        url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard?dates={ymd}"
        try:
            data = http_get_json(url)
        except Exception as e:
            failures += 1
            log(f"  ESPN {slug} {day}: {e}")
            continue
        if not data:
            continue
        league_name = league_fallback
        leagues = data.get("leagues") or []
        if leagues:
            league_name = leagues[0].get("name") or league_fallback
            country = leagues[0].get("country") or country_hint
        else:
            country = country_hint

        for ev in data.get("events") or []:
            comps = (ev.get("competitions") or [{}])[0]
            competitors = comps.get("competitors") or []
            home = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away = next((c for c in competitors if c.get("homeAway") == "away"), None)
            if not home or not away:
                # fallback order
                if len(competitors) >= 2:
                    # ESPN away-at-home naming; prefer homeAway
                    home = competitors[0]
                    away = competitors[1]
                else:
                    continue
            status_obj = (comps.get("status") or {}).get("type") or {}
            status = norm_status(status_obj.get("name") or status_obj.get("description"))
            venue = ((comps.get("venue") or {}).get("fullName")) or None
            kick_iso = ev.get("date") or comps.get("date")
            date_ymd, kick_utc, kick_hkt = parse_iso_to_utc_hkt(kick_iso)
            # Prefer calendar day from kickoff; fall back to requested day
            if not date_ymd:
                date_ymd = day.isoformat()

            hs = home.get("score")
            as_ = away.get("score")
            # ESPN returns "0" for scheduled; keep None until started/finished
            if status == "SCHEDULED":
                hs = None
                as_ = None
            else:
                try:
                    hs = int(hs) if hs is not None and str(hs) != "" else None
                except ValueError:
                    hs = None
                try:
                    as_ = int(as_) if as_ is not None and str(as_) != "" else None
                except ValueError:
                    as_ = None

            m = empty_match()
            m.update(
                {
                    "date": date_ymd,
                    "kickoff_utc": kick_utc,
                    "kickoff_hkt": kick_hkt,
                    "league": league_name,
                    "country": country,
                    "home": (home.get("team") or {}).get("displayName") or home.get("name"),
                    "away": (away.get("team") or {}).get("displayName") or away.get("name"),
                    "home_score": hs,
                    "away_score": as_,
                    "status": status,
                    "venue": venue,
                    "source": "espn",
                    "source_id": str(ev.get("id") or comps.get("id") or ""),
                    "extra": {
                        "espn_slug": slug,
                        "espn_short_name": ev.get("shortName"),
                        "state": status_obj.get("state"),
                        "detail": status_obj.get("detail"),
                    },
                }
            )
            matches.append(m)
    if failures:
        _source_notes.append(f"ESPN: {failures} league/day requests failed for {day}")
    return matches


# ---------------------------------------------------------------------------
# TheSportsDB
# ---------------------------------------------------------------------------

def fetch_thesportsdb_day(day: date) -> List[Dict[str, Any]]:
    # Free test key "3"
    url = f"https://www.thesportsdb.com/api/v1/json/3/eventsday.php?d={day.isoformat()}&s=Soccer"
    matches: List[Dict[str, Any]] = []
    try:
        data = http_get_json(url)
    except Exception as e:
        _source_notes.append(f"TheSportsDB failed for {day}: {e}")
        return matches
    if not data:
        return matches
    events = data.get("events")
    if not events:
        return matches
    for ev in events:
        ts = ev.get("strTimestamp") or (
            f"{ev.get('dateEvent')}T{ev.get('strTime') or '00:00:00'}"
            if ev.get("dateEvent")
            else None
        )
        if ts and " " in ts and "T" not in ts:
            ts = ts.replace(" ", "T")
        if ts and not ts.endswith("Z") and "+" not in ts[10:]:
            # TheSportsDB timestamps are UTC
            if len(ts) == 19:
                ts = ts + "+00:00"
        date_ymd, kick_utc, kick_hkt = parse_iso_to_utc_hkt(ts)
        if not date_ymd:
            date_ymd = ev.get("dateEvent") or day.isoformat()

        hs = ev.get("intHomeScore")
        as_ = ev.get("intAwayScore")
        try:
            hs = int(hs) if hs is not None and str(hs) != "" else None
        except (TypeError, ValueError):
            hs = None
        try:
            as_ = int(as_) if as_ is not None and str(as_) != "" else None
        except (TypeError, ValueError):
            as_ = None

        status = norm_status(ev.get("strStatus"))
        if (ev.get("strPostponed") or "").lower() == "yes":
            status = "POSTPONED"

        m = empty_match()
        m.update(
            {
                "date": date_ymd,
                "kickoff_utc": kick_utc,
                "kickoff_hkt": kick_hkt,
                "league": ev.get("strLeague"),
                "country": ev.get("strCountry"),
                "home": ev.get("strHomeTeam"),
                "away": ev.get("strAwayTeam"),
                "home_score": hs,
                "away_score": as_,
                "status": status,
                "venue": ev.get("strVenue") or None,
                "source": "thesportsdb",
                "source_id": str(ev.get("idEvent") or ""),
                "extra": {
                    "season": ev.get("strSeason"),
                    "round": ev.get("intRound"),
                    "city": ev.get("strCity"),
                    "thumb": ev.get("strThumb"),
                },
            }
        )
        matches.append(m)
    return matches


# ---------------------------------------------------------------------------
# OpenLigaDB (Germany)
# ---------------------------------------------------------------------------

def _openliga_season_for_day(day: date) -> int:
    # German season label is start year (e.g. 2025 for 2025/26)
    return day.year if day.month >= 7 else day.year - 1


def fetch_openligadb_range(start: date, end: date) -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []
    seasons = sorted({_openliga_season_for_day(start), _openliga_season_for_day(end)})
    for shortcut, country, league_name in OPENLIGA_SHORTCUTS:
        for season in seasons:
            url = f"https://api.openligadb.de/getmatchdata/{shortcut}/{season}"
            try:
                data = http_get_json(url)
            except Exception as e:
                _source_notes.append(f"OpenLigaDB {shortcut}/{season}: {e}")
                continue
            if not isinstance(data, list):
                continue
            for mraw in data:
                utc_s = mraw.get("matchDateTimeUTC") or mraw.get("matchDateTime")
                date_ymd, kick_utc, kick_hkt = parse_iso_to_utc_hkt(
                    utc_s if utc_s and ("Z" in utc_s or "+" in utc_s or "T" in utc_s) else None
                )
                if not date_ymd and utc_s:
                    # local german time without Z — treat as Europe/Berlin approx CET/CEST messy;
                    # OpenLiga usually provides matchDateTimeUTC
                    date_ymd, kick_utc, kick_hkt = parse_iso_to_utc_hkt(utc_s + ("Z" if "T" in utc_s and "Z" not in utc_s else ""))
                if not date_ymd:
                    continue
                d = parse_ymd(date_ymd)
                if d < start or d > end:
                    continue
                results = mraw.get("matchResults") or []
                hs = as_ = None
                # Prefer endergebnis (typeID 2) over halbzeit (1)
                end_res = None
                for r in results:
                    if r.get("resultTypeID") == 2 or (r.get("resultName") or "").lower() == "endergebnis":
                        end_res = r
                if end_res is None and results:
                    end_res = results[-1]
                if end_res:
                    hs = end_res.get("pointsTeam1")
                    as_ = end_res.get("pointsTeam2")
                finished = bool(mraw.get("matchIsFinished"))
                status = "FINISHED" if finished else "SCHEDULED"
                loc = mraw.get("location") or {}
                venue = loc.get("locationStadium") or loc.get("locationCity") or None
                league = mraw.get("leagueName") or league_name
                m = empty_match()
                m.update(
                    {
                        "date": date_ymd,
                        "kickoff_utc": kick_utc,
                        "kickoff_hkt": kick_hkt,
                        "league": league,
                        "country": country,
                        "home": (mraw.get("team1") or {}).get("teamName"),
                        "away": (mraw.get("team2") or {}).get("teamName"),
                        "home_score": hs if finished else (hs if hs is not None and finished else (hs if finished else None)),
                        "away_score": as_ if finished else None,
                        "status": status,
                        "venue": venue,
                        "source": "openligadb",
                        "source_id": str(mraw.get("matchID") or ""),
                        "extra": {
                            "group": (mraw.get("group") or {}).get("groupName"),
                            "league_shortcut": shortcut,
                            "season": season,
                        },
                    }
                )
                if finished:
                    m["home_score"] = hs
                    m["away_score"] = as_
                matches.append(m)
    return matches


# ---------------------------------------------------------------------------
# football-data.co.uk (free CSV fixtures + results)
# ---------------------------------------------------------------------------

FD_UK_DIV_META = {
    # Level-1 allowlist only (lower tiers / cups filtered out via competitions.match_competition)
    "E0": ("England", "Premier League"),
    "SP1": ("Spain", "La Liga"),
    "D1": ("Germany", "Bundesliga"),
    "N1": ("Netherlands", "Eredivisie"),
    "SC0": ("Scotland", "Premiership"),
    # Switzerland not always in fd.uk major dump; keep empty — allowlist still filters
}


def _parse_fd_uk_date(s: str) -> Optional[date]:
    s = (s or "").strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None



def _fd_uk_extra(r: Dict[str, str], div: str) -> Dict[str, Any]:
    """Keep referee/div plus market odds for consensus blending."""
    extra: Dict[str, Any] = {"div": div, "referee": r.get("Referee") or None}
    odds_keys = [
        "B365H", "B365D", "B365A",
        "AvgH", "AvgD", "AvgA",
        "MaxH", "MaxD", "MaxA",
        "Avg>2.5", "Avg<2.5", "B365>2.5", "B365<2.5",
    ]
    for k in odds_keys:
        v = r.get(k)
        if v is None or str(v).strip() == "":
            continue
        try:
            extra[k] = float(v)
        except (TypeError, ValueError):
            extra[k] = str(v).strip()
    return extra


def fetch_football_data_uk(start: date, end: date) -> List[Dict[str, Any]]:
    """Fixtures CSV + a few major-league results CSVs from football-data.co.uk."""
    import csv
    import io

    matches: List[Dict[str, Any]] = []

    def ingest_rows(rows: List[Dict[str, str]], *, has_scores: bool) -> int:
        n = 0
        for r in rows:
            d = _parse_fd_uk_date(r.get("Date") or "")
            if not d or d < start or d > end:
                continue
            div = (r.get("Div") or "").strip()
            if div not in FD_UK_DIV_META:
                continue  # skip non Level-1 divisions early
            country, league = FD_UK_DIV_META[div]
            t = (r.get("Time") or "00:00").strip()
            if len(t) == 5:
                t = t + ":00"
            # Times are local kickoff; treat as Europe/London-ish naive → store as date+time UTC unknown.
            # Annotate as local wall-clock with Z unset; convert assuming UK local ≈ UTC+1 in Sep (BST).
            try:
                local = datetime.strptime(f"{d.isoformat()} {t}", "%Y-%m-%d %H:%M:%S")
            except ValueError:
                local = datetime.strptime(d.isoformat(), "%Y-%m-%d")
            # BST (UTC+1) for early September
            kick = local.replace(tzinfo=timezone(timedelta(hours=1))).astimezone(UTC)
            date_ymd, kick_utc, kick_hkt = parse_iso_to_utc_hkt(kick.isoformat())
            hs = as_ = None
            status = "SCHEDULED"
            if has_scores and (r.get("FTHG") not in (None, "")):
                try:
                    hs = int(r.get("FTHG"))
                    as_ = int(r.get("FTAG"))
                    status = "FINISHED"
                except (TypeError, ValueError):
                    pass
            m = empty_match()
            m.update(
                {
                    "date": date_ymd or d.isoformat(),
                    "kickoff_utc": kick_utc,
                    "kickoff_hkt": kick_hkt,
                    "league": league,
                    "country": country,
                    "home": r.get("HomeTeam"),
                    "away": r.get("AwayTeam"),
                    "home_score": hs,
                    "away_score": as_,
                    "status": status,
                    "venue": None,
                    "source": "football-data.co.uk",
                    "source_id": f"{div}:{d.isoformat()}:{(r.get('HomeTeam') or '')}:{(r.get('AwayTeam') or '')}",
                    "extra": _fd_uk_extra(r, div),
                }
            )
            matches.append(m)
            n += 1
        return n

    # Upcoming / near-term fixtures dump
    raw = http_get_text("https://www.football-data.co.uk/fixtures.csv")
    if raw:
        rows = list(csv.DictReader(io.StringIO(raw)))
        n = ingest_rows(rows, has_scores=False)
        log(f"  football-data.co.uk fixtures.csv: {n} in range")
        _source_notes.append(f"football-data.co.uk fixtures.csv: {n} in range")
    else:
        _source_notes.append("football-data.co.uk fixtures.csv: unavailable")

    # Results for major leagues (season folder 2526 = 2025-26; also try 2627)
    season_folders = []
    # For Sep 2026 we want 2026-27 season folder if present, else 2025-26 for late results
    y = start.year if start.month >= 7 else start.year - 1
    season_folders.append(f"{str(y)[-2:]}{str(y+1)[-2:]}")  # e.g. 2627
    season_folders.append(f"{str(y-1)[-2:]}{str(y)[-2:]}")  # e.g. 2526
    codes = ["E0", "SP1", "D1", "N1", "SC0"]  # Level-1 only
    total_res = 0
    for folder in season_folders:
        for code in codes:
            url = f"https://www.football-data.co.uk/mmz4281/{folder}/{code}.csv"
            raw = http_get_text(url)
            if not raw or "HomeTeam" not in raw[:200]:
                continue
            rows = list(csv.DictReader(io.StringIO(raw)))
            total_res += ingest_rows(rows, has_scores=True)
    log(f"  football-data.co.uk results CSVs: {total_res} in range")
    _source_notes.append(f"football-data.co.uk results CSVs: {total_res} in range")
    return matches


# ---------------------------------------------------------------------------
# football-data.org (optional)
# ---------------------------------------------------------------------------


def fetch_football_data_org(start: date, end: date) -> List[Dict[str, Any]]:
    token = os.environ.get("FOOTBALL_DATA_API_TOKEN") or os.environ.get("FOOTBALL_DATA_TOKEN")
    url = (
        "https://api.football-data.org/v4/matches?"
        + urlencode({"dateFrom": start.isoformat(), "dateTo": end.isoformat()})
    )
    headers = {}
    if token:
        headers["X-Auth-Token"] = token
    matches: List[Dict[str, Any]] = []
    try:
        data = http_get_json(url, headers=headers or None)
    except Exception as e:
        _source_notes.append(
            f"football-data.org skipped/failed ({e}). Set FOOTBALL_DATA_API_TOKEN for free-tier token."
        )
        return matches
    if not data:
        _source_notes.append("football-data.org returned no data")
        return matches
    raw_matches = data.get("matches") or []
    if not raw_matches:
        _source_notes.append(
            "football-data.org returned 0 matches (free access without token is limited; "
            "register at https://www.football-data.org/client/register for a free token)."
        )
        return matches
    for ev in raw_matches:
        utc_s = ev.get("utcDate")
        date_ymd, kick_utc, kick_hkt = parse_iso_to_utc_hkt(utc_s)
        score = ev.get("score") or {}
        full = score.get("fullTime") or {}
        hs = full.get("home")
        as_ = full.get("away")
        status = norm_status(ev.get("status"))
        comp = ev.get("competition") or {}
        area = ev.get("area") or {}
        m = empty_match()
        m.update(
            {
                "date": date_ymd,
                "kickoff_utc": kick_utc,
                "kickoff_hkt": kick_hkt,
                "league": comp.get("name"),
                "country": area.get("name"),
                "home": (ev.get("homeTeam") or {}).get("name"),
                "away": (ev.get("awayTeam") or {}).get("name"),
                "home_score": hs,
                "away_score": as_,
                "status": status,
                "venue": None,
                "source": "football-data.org",
                "source_id": str(ev.get("id") or ""),
                "extra": {
                    "matchday": ev.get("matchday"),
                    "stage": ev.get("stage"),
                    "competition_code": comp.get("code"),
                },
            }
        )
        matches.append(m)
    return matches


# ---------------------------------------------------------------------------
# Collect / write
# ---------------------------------------------------------------------------

def collect(start: date, end: date) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    global _source_notes
    _source_notes = []
    ensure_dirs()
    days = daterange(start, end)
    log(f"Collecting football matches {start} → {end} ({len(days)} day(s)) [HKT today={today_hkt()}]")

    bucket: Dict[str, Dict[str, Any]] = {}

    # 1) ESPN (primary)
    log("Source: ESPN public scoreboards…")
    espn_count = 0
    for day in days:
        got = fetch_espn_day(day)
        espn_count += len(got)
        merge_matches(bucket, got)
        log(f"  ESPN {day}: {len(got)} events")
    _source_notes.append(f"ESPN: {espn_count} raw events across {len(ESPN_LEAGUES)} league slugs")

    # 2) TheSportsDB
    log("Source: TheSportsDB (free key 3)…")
    tsdb_count = 0
    for day in days:
        got = fetch_thesportsdb_day(day)
        tsdb_count += len(got)
        merge_matches(bucket, got)
        log(f"  TheSportsDB {day}: {len(got)} events")
    _source_notes.append(
        f"TheSportsDB: {tsdb_count} raw events (free key is rate/coverage-limited)"
    )

    # 3) OpenLigaDB
    log("Source: OpenLigaDB (German leagues)…")
    try:
        got = fetch_openligadb_range(start, end)
        merge_matches(bucket, got)
        log(f"  OpenLigaDB: {len(got)} events in range")
        _source_notes.append(f"OpenLigaDB: {len(got)} events in range")
    except Exception as e:
        _source_notes.append(f"OpenLigaDB failed: {e}")
        log(f"  OpenLigaDB failed: {e}")

    # 4) football-data.co.uk free CSVs
    log("Source: football-data.co.uk (CSV)…")
    try:
        got = fetch_football_data_uk(start, end)
        merge_matches(bucket, got)
        log(f"  football-data.co.uk: {len(got)} events")
    except Exception as e:
        _source_notes.append(f"football-data.co.uk failed: {e}")
        log(f"  football-data.co.uk failed: {e}")

    # 5) football-data.org
    log("Source: football-data.org (optional)…")
    got = fetch_football_data_org(start, end)
    merge_matches(bucket, got)
    log(f"  football-data.org: {len(got)} events")

    matches = sorted(
        bucket.values(),
        key=lambda m: (
            m.get("date") or "",
            m.get("kickoff_utc") or "",
            m.get("league") or "",
            m.get("home") or "",
        ),
    )
    meta = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "generated_at_hkt": datetime.now(HKT).isoformat(),
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "match_count": len(matches),
        "sources_notes": list(_source_notes),
        "leagues": sorted({m["league"] for m in matches if m.get("league")}),
    }
    return matches, meta


def write_outputs(matches: List[Dict[str, Any]], meta: Dict[str, Any], out_base: Path) -> None:
    out_base.parent.mkdir(parents=True, exist_ok=True)
    json_path = Path(str(out_base) + ".json")
    csv_path = Path(str(out_base) + ".csv")
    meta_path = Path(str(out_base) + ".meta.json")

    payload = {"meta": meta, "matches": matches}
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    fields = [
        "date",
        "kickoff_utc",
        "kickoff_hkt",
        "league",
        "country",
        "home",
        "away",
        "home_score",
        "away_score",
        "status",
        "venue",
        "source",
        "source_id",
        "extra",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for m in matches:
            row = dict(m)
            row["extra"] = json.dumps(m.get("extra") or {}, ensure_ascii=False)
            w.writerow(row)

    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Wrote {json_path}")
    log(f"Wrote {csv_path}")
    log(f"Wrote {meta_path}")


def build_summary(matches: List[Dict[str, Any]], meta: Dict[str, Any], path: Path) -> None:
    from collections import Counter

    leagues = Counter(m.get("league") or "?" for m in matches)
    statuses = Counter(m.get("status") or "?" for m in matches)
    sources = Counter(m.get("source") or "?" for m in matches)
    by_date = Counter(m.get("date") or "?" for m in matches)

    # Interesting sample: big leagues + any with scores + high-profile names
    priority_leagues = {
        "English Premier League",
        "Spanish LALIGA",
        "Spanish La Liga",
        "German Bundesliga",
        "Italian Serie A",
        "French Ligue 1",
        "MLS",
        "UEFA Champions League",
    }
    interesting = [
        m
        for m in matches
        if (m.get("league") in priority_leagues)
        or (m.get("home_score") is not None)
        or any(
            k in (m.get("home") or "") + (m.get("away") or "")
            for k in ("Madrid", "Barcelona", "United", "Bayern", "Liverpool", "City", "Juventus", "PSG")
        )
    ]
    if len(interesting) < 10:
        interesting = list(matches)
    sample = interesting[:10]

    lines = []
    lines.append("# Football data pull — SUMMARY")
    lines.append("")
    lines.append(f"- Generated (HKT): `{meta.get('generated_at_hkt')}`")
    lines.append(f"- Range: `{meta.get('date_from')}` → `{meta.get('date_to')}`")
    lines.append(f"- **Match count:** {meta.get('match_count')}")
    lines.append(f"- **Leagues covered:** {len(meta.get('leagues') or [])}")
    lines.append("")
    lines.append("## Counts by date")
    for d, n in sorted(by_date.items()):
        lines.append(f"- {d}: {n}")
    lines.append("")
    lines.append("## Status mix")
    for s, n in statuses.most_common():
        lines.append(f"- {s}: {n}")
    lines.append("")
    lines.append("## Source mix (primary tag after merge)")
    for s, n in sources.most_common():
        lines.append(f"- {s}: {n}")
    lines.append("")
    lines.append("## Leagues")
    for lg, n in leagues.most_common():
        lines.append(f"- {lg}: {n}")
    lines.append("")
    lines.append("## Source notes / failures")
    notes = meta.get("sources_notes") or []
    if not notes:
        lines.append("- (none)")
    else:
        for n in notes:
            lines.append(f"- {n}")
    lines.append("")
    lines.append("## Sample of 10 interesting matches")
    for m in sample:
        score = (
            f"{m.get('home_score')}-{m.get('away_score')}"
            if m.get("home_score") is not None
            else "vs"
        )
        lines.append(
            f"- {m.get('date')} | {m.get('league')} | {m.get('home')} {score} {m.get('away')} "
            f"| {m.get('status')} | {m.get('venue') or '—'} | src={m.get('source')}"
        )
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"Wrote {path}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Collect football fixtures/results from free APIs")
    parser.add_argument("--date", help="Single date YYYY-MM-DD")
    parser.add_argument("--from", dest="date_from", help="Start date YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", help="End date YYYY-MM-DD")
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="Output path prefix (writes .json/.csv/.meta.json)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Ignore disk cache (still writes new cache entries)",
    )
    args = parser.parse_args(argv)

    if args.no_cache:
        global CACHE_TTL_SEC
        CACHE_TTL_SEC = 0

    if args.date:
        start = end = parse_ymd(args.date)
    elif args.date_from or args.date_to:
        if not args.date_from or not args.date_to:
            parser.error("Provide both --from and --to")
        start = parse_ymd(args.date_from)
        end = parse_ymd(args.date_to)
    else:
        # Default: today + tomorrow in Asia/Hong_Kong
        start = today_hkt()
        end = start + timedelta(days=1)

    matches, meta = collect(start, end)
    out_base = Path(args.out)
    if not out_base.is_absolute():
        out_base = ROOT / out_base
    write_outputs(matches, meta, out_base)
    print(json.dumps({"match_count": len(matches), "leagues": len(meta["leagues"]), "out": str(out_base)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
