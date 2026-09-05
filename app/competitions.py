"""Allowlisted competitions — Level-1 domestic + internationals (+ UCL)."""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# Only top-flight leagues for selected countries, UEFA Champions League, and internationals.
COMPETITIONS: List[Dict[str, object]] = [
    # England — Level 1
    {"key": "eng_pl", "country": "England", "name": "Premier League",
     "patterns": ["premier league", "english premier league", "epl"]},
    # Germany — Level 1
    {"key": "ger_bl1", "country": "Germany", "name": "Bundesliga",
     "patterns": ["bundesliga", "1. bundesliga", "german bundesliga", "1. fußball-bundesliga", "1. fussball-bundesliga"]},
    # Switzerland — Level 1
    {"key": "sui_sl", "country": "Switzerland", "name": "Super League",
     "patterns": ["swiss super league", "super league"]},
    # Scotland — Level 1
    {"key": "sco_pre", "country": "Scotland", "name": "Premiership",
     "patterns": ["scottish premiership", "premiership", "cinch premiership"]},
    # Spain — Level 1
    {"key": "esp_ll", "country": "Spain", "name": "La Liga",
     "patterns": ["la liga", "laliga", "spanish la liga", "spanish laliga"]},
    # Netherlands — Level 1
    {"key": "ned_ere", "country": "Netherlands", "name": "Eredivisie",
     "patterns": ["eredivisie", "dutch eredivisie"]},
    # Europe — elite club (kept as Level-1 continental)
    {"key": "uefa_ucl", "country": "Europe", "name": "UEFA Champions League",
     "patterns": ["champions league", "uefa champions"]},
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
    "premiership": {"Scotland": "sco_pre", "England": "eng_pl"},
    "super league": {"Switzerland": "sui_sl"},
}


def _norm(s: str) -> str:
    return " ".join((s or "").lower().replace("laliga", "la liga").split())


def match_competition(league: Optional[str], country: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Return allowlisted competition dict or None if out of scope."""
    ln = _norm(league or "")
    cn = (country or "").strip()
    if not ln:
        return None

    # Reject known lower-tier / cup labels early even if they partially match
    reject = [
        "championship", "league one", "league two", "2. bundesliga", "3. liga",
        "la liga 2", "laliga 2", "segunda", "challenge league", "eerste",
        "fa cup", "efl cup", "carabao", "dfb-pokal", "dfb pokal", "copa del rey",
        "scottish cup", "knvb", "europa league", "conference league",
    ]
    # Don't reject "premier league" containing nothing from reject incorrectly
    for r in reject:
        if r in ln:
            # bundesliga alone is L1; "2. bundesliga" is rejected above
            if r == "championship" and "scottish premiership" in ln:
                continue
            return None

    for amb, by_country in _AMBIGUOUS.items():
        if ln == amb or ln.endswith(" " + amb):
            for ctry, key in by_country.items():
                if ctry.lower() in cn.lower() or cn.lower() in ctry.lower():
                    for c in COMPETITIONS:
                        if c["key"] == key:
                            return {"key": key, "country": str(c["country"]), "name": str(c["name"])}

    ranked: List[Tuple[int, Dict[str, object]]] = []
    for c in COMPETITIONS:
        for p in c["patterns"]:  # type: ignore
            pn = _norm(str(p))
            if pn and (pn in ln or ln in pn):
                if pn == "friendly" and "international" not in ln and "friendly" not in ln:
                    continue
                if pn == "super league" and "swiss" not in ln and "switzerland" not in cn.lower():
                    if cn and "switzerland" not in cn.lower() and "swiss" not in cn.lower():
                        continue
                if pn == "bundesliga" and ("2." in ln or "2 " in ln or "zweite" in ln):
                    continue
                if pn == "premiership" and "scottish" not in ln and "scotland" not in cn.lower():
                    if "premier league" in ln:
                        continue
                ranked.append((len(pn), c))

    if not ranked:
        return None
    ranked.sort(key=lambda x: -x[0])
    c = ranked[0][1]
    return {"key": str(c["key"]), "country": str(c["country"]), "name": str(c["name"])}


def competitions_by_country() -> Dict[str, List[Dict[str, str]]]:
    out: Dict[str, List[Dict[str, str]]] = {}
    for c in COMPETITIONS:
        out.setdefault(str(c["country"]), []).append(
            {"key": str(c["key"]), "name": str(c["name"])}
        )
    return out


def countries() -> List[str]:
    return sorted({str(c["country"]) for c in COMPETITIONS})


def all_keys() -> List[str]:
    return [str(c["key"]) for c in COMPETITIONS]
