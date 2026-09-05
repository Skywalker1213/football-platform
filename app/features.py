"""Feature pipeline: fixture load, weather, form proxies; stubs for injuries/players."""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import os
from app.collector_core import _norm_team

SKIP_WEATHER = os.environ.get("FOOTBALL_SKIP_WEATHER", "0") == "1"

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache"
HKT = timezone(timedelta(hours=8))
UTC = timezone.utc

# City / club venue approximate coords (lon, lat) for Open-Meteo when geocode fails
VENUE_COORDS: Dict[str, Tuple[float, float]] = {
    # England
    "london": (-0.1276, 51.5074),
    "manchester": (-2.2426, 53.4808),
    "liverpool": (-2.9916, 53.4084),
    "birmingham": (-1.8904, 52.4862),
    "newcastle": (-1.6178, 54.9783),
    "leeds": (-1.5491, 53.8008),
    "sheffield": (-1.4701, 53.3811),
    "nottingham": (-1.1581, 52.9548),
    "brighton": (-0.1372, 50.8225),
    "southampton": (-1.4044, 50.9097),
    "leicester": (-1.1332, 52.6369),
    "wolverhampton": (-2.1288, 52.5869),
    "bristol": (-2.5879, 51.4545),
    "cardiff": (-3.1791, 51.4816),
    "swansea": (-3.9436, 51.6214),
    "norwich": (1.2979, 52.6309),
    "ipswich": (1.1482, 52.0567),
    # Germany
    "munich": (11.5820, 48.1351),
    "berlin": (13.4050, 52.5200),
    "dortmund": (7.4686, 51.5136),
    "frankfurt": (8.6821, 50.1109),
    "hamburg": (9.9937, 53.5511),
    "cologne": (6.9603, 50.9375),
    "köln": (6.9603, 50.9375),
    "leipzig": (12.3731, 51.3397),
    "stuttgart": (9.1829, 48.7758),
    "freiburg": (7.8421, 47.9990),
    "bremen": (8.8017, 53.0793),
    # Spain
    "madrid": (-3.7038, 40.4168),
    "barcelona": (2.1734, 41.3851),
    "sevilla": (-5.9845, 37.3891),
    "valencia": (-0.3763, 39.4699),
    "bilbao": (-2.9350, 43.2630),
    "villarreal": (-0.1014, 39.9370),
    # Netherlands
    "amsterdam": (4.9041, 52.3676),
    "rotterdam": (4.4777, 51.9244),
    "eindhoven": (5.4697, 51.4416),
    "utrecht": (5.1214, 52.0907),
    # Scotland
    "glasgow": (-4.2518, 55.8642),
    "edinburgh": (-3.1883, 55.9533),
    "aberdeen": (-2.0943, 57.1497),
    "dundee": (-2.9707, 56.4620),
    # Switzerland
    "zurich": (8.5417, 47.3769),
    "zürich": (8.5417, 47.3769),
    "bern": (7.4474, 46.9480),
    "basel": (7.5886, 47.5596),
    "geneva": (6.1432, 46.2044),
    "st. gallen": (9.3767, 47.4245),
}

TEAM_CITY: Dict[str, str] = {
    "manchester city": "manchester",
    "manchester united": "manchester",
    "liverpool": "liverpool",
    "chelsea": "london",
    "arsenal": "london",
    "tottenham hotspur": "london",
    "west ham united": "london",
    "crystal palace": "london",
    "fulham": "london",
    "brentford": "london",
    "newcastle united": "newcastle",
    "aston villa": "birmingham",
    "wolverhampton wanderers": "wolverhampton",
    "brighton and hove albion": "brighton",
    "nottingham forest": "nottingham",
    "everton": "liverpool",
    "bournemouth": "southampton",
    "bayern munich": "munich",
    "borussia dortmund": "dortmund",
    "rb leipzig": "leipzig",
    "bayer leverkusen": "cologne",
    "eintracht frankfurt": "frankfurt",
    "real madrid": "madrid",
    "atletico madrid": "madrid",
    "barcelona": "barcelona",
    "sevilla": "sevilla",
    "valencia": "valencia",
    "ajax": "amsterdam",
    "feyenoord": "rotterdam",
    "psv eindhoven": "eindhoven",
    "celtic": "glasgow",
    "rangers": "glasgow",
    "hearts": "edinburgh",
    "hibernian": "edinburgh",
}


def _cache_get(key: str, ttl: int = 86400) -> Optional[Any]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    import hashlib
    h = hashlib.sha256(key.encode()).hexdigest()[:40]
    path = CACHE_DIR / f"feat_{h}.json"
    if path.exists() and time.time() - path.stat().st_mtime < ttl:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return None


def _cache_set(key: str, data: Any) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    import hashlib
    h = hashlib.sha256(key.encode()).hexdigest()[:40]
    path = CACHE_DIR / f"feat_{h}.json"
    path.write_text(json.dumps(data), encoding="utf-8")


def _http_json(url: str, ttl: int = 86400) -> Optional[Any]:
    cached = _cache_get(url, ttl=ttl)
    if cached is not None:
        return cached
    try:
        req = Request(url, headers={"User-Agent": "football-platform/0.1 (educational)", "Accept": "application/json"})
        with urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        _cache_set(url, data)
        return data
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return None


def resolve_coords(venue: Optional[str], home_team: Optional[str], city_hint: Optional[str] = None, allow_network: bool = True) -> Tuple[Optional[float], Optional[float], str]:
    """Return (lon, lat, method)."""
    # 1) known team city
    tn = _norm_team(home_team)
    if tn in TEAM_CITY:
        city = TEAM_CITY[tn]
        if city in VENUE_COORDS:
            lon, lat = VENUE_COORDS[city]
            return lon, lat, f"team_city:{city}"

    # 2) scan venue / city strings against VENUE_COORDS
    blob = " ".join([venue or "", city_hint or "", home_team or ""]).lower()
    for city, (lon, lat) in VENUE_COORDS.items():
        if city in blob:
            return lon, lat, f"venue_lookup:{city}"

    # 3) Open-Meteo geocoding (free)
    if allow_network:
        q = (city_hint or venue or home_team or "").strip()
        if q:
            url = f"https://geocoding-api.open-meteo.com/v1/search?name={quote(q)}&count=1&language=en&format=json"
            data = _http_json(url, ttl=7 * 86400)
            if data and data.get("results"):
                r0 = data["results"][0]
                return float(r0["longitude"]), float(r0["latitude"]), f"geocode:{r0.get('name')}"

    return None, None, "unavailable"


def fetch_weather_at_kickoff(
    lon: float, lat: float, kickoff_utc: Optional[str]
) -> Dict[str, Any]:
    """Open-Meteo hourly weather near kickoff. Free, no key."""
    if not kickoff_utc:
        return {"status": "stub", "reason": "no_kickoff"}
    try:
        s = kickoff_utc.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        dt = dt.astimezone(UTC)
    except ValueError:
        return {"status": "stub", "reason": "bad_kickoff"}

    day = dt.date().isoformat()
    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m,precipitation,weathercode,windspeed_10m"
        f"&start_date={day}&end_date={day}&timezone=UTC"
    )
    # archive for past dates
    if dt.date() < datetime.now(UTC).date() - timedelta(days=1):
        url = (
            f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}"
            f"&hourly=temperature_2m,precipitation,weathercode,windspeed_10m"
            f"&start_date={day}&end_date={day}&timezone=UTC"
        )
    data = _http_json(url, ttl=6 * 3600)
    if not data or "hourly" not in data:
        return {"status": "unavailable", "lon": lon, "lat": lat}

    hours = data["hourly"].get("time") or []
    temps = data["hourly"].get("temperature_2m") or []
    precip = data["hourly"].get("precipitation") or []
    codes = data["hourly"].get("weathercode") or []
    winds = data["hourly"].get("windspeed_10m") or []
    target = dt.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00")
    idx = None
    for i, t in enumerate(hours):
        if t.startswith(target[:13]) or t == target:
            idx = i
            break
    if idx is None and hours:
        # nearest hour
        idx = min(range(len(hours)), key=lambda i: abs(
            (datetime.fromisoformat(hours[i]) - dt.replace(tzinfo=None)).total_seconds()
            if "+" not in hours[i] and "Z" not in hours[i]
            else (datetime.fromisoformat(hours[i].replace("Z", "+00:00")).astimezone(UTC) - dt).total_seconds()
        ))
    if idx is None:
        return {"status": "unavailable", "lon": lon, "lat": lat}

    return {
        "status": "live",
        "source": "open-meteo",
        "lon": lon,
        "lat": lat,
        "hour_utc": hours[idx] if idx < len(hours) else None,
        "temperature_c": temps[idx] if idx < len(temps) else None,
        "precipitation_mm": precip[idx] if idx < len(precip) else None,
        "weathercode": codes[idx] if idx < len(codes) else None,
        "windspeed_kmh": winds[idx] if idx < len(winds) else None,
        "note": "Weather at kickoff hour (UTC). Statistical only — not a betting signal.",
    }


def fixture_load_features(team: str, before_date: str, all_team_matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Rest days, matches in last 7/14 days, midweek flag from collected fixtures."""
    recent = [m for m in all_team_matches if (m.get("date") or "") < before_date]
    recent.sort(key=lambda m: m.get("date") or "", reverse=True)

    rest_days = None
    if recent:
        try:
            last = datetime.strptime(recent[0]["date"], "%Y-%m-%d").date()
            cur = datetime.strptime(before_date, "%Y-%m-%d").date()
            rest_days = (cur - last).days
        except (ValueError, TypeError, KeyError):
            rest_days = None

    def in_last(n_days: int) -> int:
        try:
            cur = datetime.strptime(before_date, "%Y-%m-%d").date()
        except ValueError:
            return 0
        c = 0
        for m in recent:
            try:
                d = datetime.strptime(m["date"], "%Y-%m-%d").date()
            except (ValueError, KeyError, TypeError):
                continue
            if 0 < (cur - d).days <= n_days:
                c += 1
        return c

    midweek = False
    try:
        wd = datetime.strptime(before_date, "%Y-%m-%d").weekday()
        midweek = wd in (1, 2)  # Tue/Wed typical European midweek
    except ValueError:
        pass

    # minutes/load proxy: assume ~90 min per finished match in window
    matches_7 = in_last(7)
    matches_14 = in_last(14)
    return {
        "status": "live",
        "rest_days": rest_days,
        "matches_last_7d": matches_7,
        "matches_last_14d": matches_14,
        "minutes_proxy_14d": matches_14 * 90,
        "midweek_fixture": midweek,
        "note": "Load proxies from fixture calendar only; true GPS/mental state not public.",
    }


def recent_form(team: str, before_date: str, matches: List[Dict[str, Any]], n: int = 5) -> Dict[str, Any]:
    """Best-effort points / GF-GA from finished matches in DB."""
    recent = []
    for m in matches:
        if (m.get("date") or "") >= before_date:
            continue
        if m.get("status") != "FINISHED":
            continue
        if m.get("home_score") is None:
            continue
        if m.get("home") != team and m.get("away") != team:
            continue
        recent.append(m)
    recent.sort(key=lambda m: m.get("date") or "", reverse=True)
    recent = recent[:n]
    if not recent:
        return {
            "status": "sparse",
            "n": 0,
            "points": None,
            "gf": None,
            "ga": None,
            "form_string": "",
            "note": "Insufficient finished matches in local DB for form.",
        }
    pts = gf = ga = 0
    letters = []
    for m in recent:
        hs, aws = int(m["home_score"]), int(m["away_score"])
        if m["home"] == team:
            gf += hs
            ga += aws
            if hs > aws:
                pts += 3
                letters.append("W")
            elif hs == aws:
                pts += 1
                letters.append("D")
            else:
                letters.append("L")
        else:
            gf += aws
            ga += hs
            if aws > hs:
                pts += 3
                letters.append("W")
            elif aws == hs:
                pts += 1
                letters.append("D")
            else:
                letters.append("L")
    return {
        "status": "live",
        "n": len(recent),
        "points": pts,
        "gf": gf,
        "ga": ga,
        "gd": gf - ga,
        "ppg": round(pts / len(recent), 3),
        "form_string": "".join(letters),
        "note": "Derived from collected finished matches only (best-effort).",
    }


def injuries_availability(team: str, source_id: Optional[str] = None) -> Dict[str, Any]:
    """Stub — no free reliable cross-league injury feed in this MVP."""
    return {
        "status": "stub",
        "team": team,
        "injuries": [],
        "unavailable": [],
        "note": (
            "No free reliable injury/availability API for all allowlisted leagues. "
            "Interface ready for TheSportsDB/ESPN enrichment when present; "
            "do not scrape paid injury sites."
        ),
    }


def player_recent_form(team: str) -> Dict[str, Any]:
    """Stub interface for per-player form — sparse on free sources."""
    return {
        "status": "stub",
        "team": team,
        "players": [],
        "note": (
            "Per-player minutes/ratings need richer feeds (often paid). "
            "Team-level form + fixture load used instead."
        ),
    }


def build_features_for_match(match: Dict[str, Any], home_hist: List[Dict[str, Any]], away_hist: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Assemble feature dict for one match."""
    date = match.get("date") or ""
    home = match.get("home") or ""
    away = match.get("away") or ""
    venue = match.get("venue")
    extra = match.get("extra") or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except json.JSONDecodeError:
            extra = {}

    lon, lat, coord_method = resolve_coords(venue, home, extra.get("city") if isinstance(extra, dict) else None, allow_network=not SKIP_WEATHER)
    if SKIP_WEATHER:
        weather = {
            "status": "deferred",
            "coord_method": coord_method,
            "lon": lon,
            "lat": lat,
            "note": "Weather deferred in bulk mode; open match detail or set FOOTBALL_SKIP_WEATHER=0",
        }
    elif lon is not None and lat is not None:
        weather = fetch_weather_at_kickoff(lon, lat, match.get("kickoff_utc"))
        weather["coord_method"] = coord_method
    else:
        weather = {"status": "unavailable", "coord_method": coord_method}

    home_load = fixture_load_features(home, date, home_hist)
    away_load = fixture_load_features(away, date, away_hist)
    home_form = recent_form(home, date, home_hist)
    away_form = recent_form(away, date, away_hist)

    # Weather adjustment hint (mild)
    weather_factor = 0.0
    if weather.get("status") == "live":
        precip = weather.get("precipitation_mm") or 0
        wind = weather.get("windspeed_kmh") or 0
        if precip and precip > 2:
            weather_factor -= 0.05  # slight lower scoring
        if wind and wind > 35:
            weather_factor -= 0.03

    return {
        "weather": weather,
        "home_load": home_load,
        "away_load": away_load,
        "home_form": home_form,
        "away_form": away_form,
        "injuries_home": injuries_availability(home, match.get("source_id")),
        "injuries_away": injuries_availability(away, match.get("source_id")),
        "player_form_home": player_recent_form(home),
        "player_form_away": player_recent_form(away),
        "physical_mental_proxies": {
            "status": "proxy",
            "home_minutes_14d": home_load.get("minutes_proxy_14d"),
            "away_minutes_14d": away_load.get("minutes_proxy_14d"),
            "note": "True mental state is not publicly observable; proxies are fixture density only.",
        },
        "weather_scoring_adj": weather_factor,
        "feature_matrix": {
            "elo_ratings": "live",
            "poisson_dixon_coles": "live",
            "fixture_load": "live",
            "team_form": home_form["status"],
            "weather_open_meteo": weather.get("status"),
            "player_form": "stub",
            "injuries": "stub",
            "mental_state": "documented_unavailable",
        },
    }
