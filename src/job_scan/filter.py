"""Cheap pre-LLM filters: title keywords + location."""
from __future__ import annotations

import re

from .config import Filters
from .scrapers.base import Posting


def _matches_any(haystack: str, needles: list[str]) -> bool:
    h = haystack.lower()
    return any(n in h for n in needles)


# --- Remote-location handling -------------------------------------------------
# "remote" alone is too permissive — it lets "Remote Spain", "Remote Canada", and
# state-restricted "Remote - MA, NY, OK" (no NJ) through, none of which the
# candidate can actually take. Precedence in _remote_ok:
#   1. home state (NJ) named -> keep
#   2. nationwide US marker (US / USA / United States) -> keep
#   3. foreign country/region -> reject
#   4. SPECIFIC US states listed but none is the home state -> reject
#      (residency-restricted remote the candidate can't be hired into)
#   5. vague global marker (anywhere/worldwide) -> keep
#   6. bare "Remote" with no geography -> keep
#
# To accept more home states (e.g. if you'd relocate to NY/PA), add them here.
_HOME_STATE_NAMES = r"new jersey"
_HOME_STATE_ABBRS = r"NJ"

# Nationwide US — NOT individual states (those are handled as restrictions).
_BROAD_US = re.compile(
    r"\b(u\.?s\.?|u\.?s\.?a|usa|united states|u\.?s\.? based|america|stateside|nationwide)\b",
    re.I,
)
_HOME = re.compile(rf"\b({_HOME_STATE_NAMES})\b", re.I)
_HOME_ABBR = re.compile(rf"\b({_HOME_STATE_ABBRS})\b")  # case-sensitive: "NJ" not "nj"

_US_STATE_NAME = re.compile(
    r"\b(alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|"
    r"florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|"
    r"maine|maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana|"
    r"nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina|"
    r"north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|"
    r"south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|"
    r"wisconsin|wyoming)\b",
    re.I,
)
# State 2-letter codes — matched case-SENSITIVELY so "NJ"/"NY"/"CA" hit but words
# like "or"/"in"/"me" don't.
_US_STATE_ABBR = re.compile(
    r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|"
    r"MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b"
)
# Vague "includes everywhere" markers — include the US, so acceptable.
_WEAK_US = re.compile(r"\b(anywhere|world\s?wide|global|fully remote)\b", re.I)
# Non-US countries / regions / major foreign cities. Word-boundary so "india" !=
# "indiana", "eu" is exact.
_FOREIGN = re.compile(
    r"\b(canada|canadian|toronto|vancouver|montreal|ottawa|calgary|"
    r"mexico|mexico city|brazil|brasil|sao paulo|são paulo|argentina|buenos aires|"
    r"chile|colombia|peru|latam|latin america|south america|central america|"
    r"uk|united kingdom|england|scotland|wales|ireland|britain|london|dublin|manchester|"
    r"europe|european|emea|eu|germany|berlin|munich|france|paris|spain|madrid|barcelona|"
    r"portugal|lisbon|italy|netherlands|amsterdam|belgium|switzerland|austria|poland|warsaw|"
    r"sweden|norway|denmark|finland|czech|romania|bulgaria|hungary|greece|ukraine|serbia|"
    r"croatia|lithuania|latvia|estonia|apac|asia|india|bangalore|bengaluru|hyderabad|mumbai|"
    r"pune|delhi|china|japan|singapore|hong kong|taiwan|korea|vietnam|thailand|philippines|"
    r"malaysia|indonesia|pakistan|bangladesh|australia|sydney|melbourne|new zealand|oceania|"
    r"africa|nigeria|kenya|south africa|egypt|ghana|morocco|middle east|israel|tel aviv|"
    r"uae|dubai|saudi|qatar|turkey|remote international)\b",
    re.I,
)


def _remote_ok(original_loc: str) -> bool:
    if _HOME.search(original_loc) or _HOME_ABBR.search(original_loc):
        return True
    if _BROAD_US.search(original_loc):
        return True
    if _FOREIGN.search(original_loc):
        return False
    # Specific US states listed, but none is the home state — residency-restricted
    # remote the candidate can't be hired into.
    if _US_STATE_NAME.search(original_loc) or _US_STATE_ABBR.search(original_loc):
        return False
    if _WEAK_US.search(original_loc):
        return True
    return True  # bare "Remote" with no geography — accept


def passes(posting: Posting, f: Filters) -> bool:
    title = posting.title.lower()
    loc = posting.location.lower()

    if f.title_include and not _matches_any(title, f.title_include):
        return False
    if f.title_exclude and _matches_any(title, f.title_exclude):
        return False

    if loc:
        if "remote" in loc:
            # Remote roles use the US-eligibility rules above, not the plain
            # include list (which matches "remote" too broadly).
            if not _remote_ok(posting.location):
                return False
        elif f.location_include and not (
            _matches_any(loc, f.location_include)
            # Enterprise ATSs (Workday etc.) abbreviate: "Princeton - NJ - US",
            # "Jersey City, NJ". The include list only spells out "new jersey",
            # so also accept the home-state patterns used for remote roles.
            or _HOME.search(posting.location)
            or _HOME_ABBR.search(posting.location)
        ):
            return False
    if f.location_exclude and _matches_any(loc, f.location_exclude):
        return False
    return True
