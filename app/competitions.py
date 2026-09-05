"""Allowlisted competitions for the MVP scope."""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# (canonical_key, country, display_name, match_patterns for league/country strings)
COMPETITIONS: List[Dict[str, object]] = [
    # England
    {"key": "eng_pl", "country": "England", "name": "Premier League",
     "patterns": ["premier league", "english premier league", "epl"]},
    {"key": "eng_ch", "country": "England", "name": "Championship",
     "patterns": ["championship", "english league championship", "efl championship"]},
    {"key": "eng_l1", "country": "England", "name": "League One",
     "patterns": ["league one", "english league one"]},
    {"key": "eng_l2", "country": "England", "name": "League Two",
     "patterns": ["league two", "english league two"]},
    {"key": "eng_fa", "country": "England", "name": "FA Cup",
     "patterns": ["fa cup", "english fa cup"]},
    {"key": "eng_efl", "country": "England", "name": "EFL Cup",
     "patterns": ["efl cup", "carabao cup", "league cup", "english carabao cup", "english league cup"]},
    # Germany
    {"key": "ger_bl1", "country": "Germany", "name": "Bundesliga",
     "patterns": ["bundesliga", "1. bundesliga", "german bundesliga", "1. fußball-bundesliga", "1. fussball-bundesliga"]},
    {"key": "ger_bl2", "country": "Germany", "name": "2. Bundesliga",
     "patterns": ["2. bundesliga", "2. fußball-bundesliga", "2. fussball-bundesliga", "german 2. bundesliga"]},
    {"key": "ger_dfb", "country": "Germany", "name": "DFB-Pokal",
     "patterns": ["dfb-pokal", "dfb pokal"]},
    # Switzerland
    {"key": "sui_sl", "country": "Switzerland", "name": "Super League",
     "patterns": ["swiss super league", "super league"]},
    {"key": "sui_chl", "country": "Switzerland", "name": "Challenge League",
     "patterns": ["challenge league", "swiss challenge league"]},
    # Scotland
    {"key": "sco_pre", "country": "Scotland", "name": "Premiership",
     "patterns": ["scottish premiership", "premiership", "cinch premiership"]},
    {"key": "sco_ch", "country": "Scotland", "name": "Championship",
     "patterns": ["scottish championship"]},
    {"key": "sco_cup", "country": "Scotland", "name": "Scottish Cup",
     "patterns": ["scottish cup"]},
    # Spain
    {"key": "esp_ll", "country": "Spain", "name": "La Liga",
     "patterns": ["la liga", "laliga", "spanish la liga", "spanish laliga"]},
    {"key": "esp_ll2", "country": "Spain", "name": "La Liga 2",
     "patterns": ["la liga 2", "laliga 2", "laliga2", "segunda", "spanish laliga 2", "spanish la liga 2"]},
    {"key": "esp_copa", "country": "Spain", "name": "Copa del Rey",
     "patterns": ["copa del rey", "copa"]},
    # Netherlands
    {"key": "ned_ere", "country": "Netherlands", "name": "Eredivisie",
     "patterns": ["eredivisie", "dutch eredivisie"]},
    {"key": "ned_eer", "country": "Netherlands", "name": "Eerste Divisie",
     "patterns": ["eerste divisie", "keuken kampioen"]},
    {"key": "ned_knvb", "country": "Netherlands", "name": "KNVB Cup",
     "patterns": ["knvb", "knvb beker"]},
    # Euro cups
    {"key": "uefa_ucl", "country": "Europe", "name": "UEFA Champions League",
     "patterns": ["champions league", "uefa champions"]},
    {"key": "uefa_uel", "country": "Europe", "name": "UEFA Europa League",
     "patterns": ["europa league", "uefa europa"]},
    {"key": "uefa_uecl", "country": "Europe", "name": "UEFA Conference League",
     "patterns": ["conference league", "europa conference"]},
    # Internationals
    {"key": "int_wcq", "country": "International", "name": "World Cup Qualifiers",
     "patterns": ["world cup qualif", "fifa world cup qualif"]},
    {"key": "int_euroq", "country": "International", "name": "Euro Qualifiers",
     "patterns": ["euro qualif", "uefa euro qualif"]},
    {"key": "int_nl", "country": "International", "name": "Nations League",
     "patterns": ["nations league", "uefa nations"]},
    {"key": "int_fr", "country": "International", "name": "International Friendly",
     "patterns": ["friendly", "international friendly"]},
]

# Ambiguous short names need country context
_AMBIGUOUS = {
    "championship": {"England": "eng_ch", "Scotland": "sco_ch"},
    "premiership": {"Scotland": "sco_pre", "England": "eng_pl"},
    "super league": {"Switzerland": "sui_sl"},
    "copa": {"Spain": "esp_copa"},
}


def _norm(s: str) -> str:
    return " ".join((s or "").lower().replace("laliga", "la liga").split())


def match_competition(league: Optional[str], country: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Return allowlisted competition dict or None if out of scope."""
    ln = _norm(league or "")
    cn = (country or "").strip()
    if not ln:
        return None

    # Ambiguous short names first
    for amb, by_country in _AMBIGUOUS.items():
        if ln == amb or ln.endswith(" " + amb):
            for ctry, key in by_country.items():
                if ctry.lower() in cn.lower() or cn.lower() in ctry.lower():
                    for c in COMPETITIONS:
                        if c["key"] == key:
                            return {"key": key, "country": str(c["country"]), "name": str(c["name"])}

    # Longer / more specific patterns first
    ranked: List[Tuple[int, Dict[str, object]]] = []
    for c in COMPETITIONS:
        for p in c["patterns"]:  # type: ignore
            pn = _norm(str(p))
            if pn and (pn in ln or ln in pn):
                # Prefer exact-ish and longer patterns; skip generic "friendly" unless intl-ish
                if pn == "friendly" and "international" not in ln and "friendly" not in ln:
                    continue
                if pn == "copa" and "copa del rey" not in ln and "del rey" not in ln:
                    # too broad alone without Spain
                    if "spain" not in cn.lower() and "spanish" not in ln:
                        continue
                if pn == "super league" and "swiss" not in ln and "switzerland" not in cn.lower():
                    # Swiss Super League often just "Super League" from sources with country
                    if cn and "switzerland" not in cn.lower() and "swiss" not in cn.lower():
                        continue
                ranked.append((len(pn), c))

    if not ranked:
        return None
    ranked.sort(key=lambda x: -x[0])
    best = ranked[0][1]

    # Extra guard: English Championship vs Scottish — if pattern matched "championship"
    # and country is Scotland, prefer sco
    if best["key"] == "eng_ch" and "scotland" in cn.lower():
        for c in COMPETITIONS:
            if c["key"] == "sco_ch":
                best = c
                break
    if best["key"] == "sco_pre" and "england" in cn.lower():
        for c in COMPETITIONS:
            if c["key"] == "eng_pl":
                best = c
                break

    # Exclude German 3. Liga falsely matching bundesliga patterns — already handled by pattern list
    if "3. liga" in ln or "3 liga" in ln:
        return None

    return {"key": str(best["key"]), "country": str(best["country"]), "name": str(best["name"])}


def is_allowed(league: Optional[str], country: Optional[str] = None) -> bool:
    return match_competition(league, country) is not None


def countries() -> List[str]:
    seen = []
    for c in COMPETITIONS:
        if c["country"] not in seen:
            seen.append(str(c["country"]))
    return seen


def competitions_by_country() -> Dict[str, List[Dict[str, str]]]:
    out: Dict[str, List[Dict[str, str]]] = {}
    for c in COMPETITIONS:
        out.setdefault(str(c["country"]), []).append(
            {"key": str(c["key"]), "name": str(c["name"])}
        )
    return out
