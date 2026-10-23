"""Small, conservative helpers for M-Team sports releases."""

from __future__ import annotations

import re


_SPORT_CATEGORIES = {"运动", "運動", "体育", "體育", "sports"}
_SPORT_TERMS = re.compile(
    r"\b(?:asian games|olympics?|paralympics?|volleyball|tennis|china open|atp|wta|"
    r"football|soccer|basketball|fifa|world cup|nba|wnba|"
    r"nfl|nhl|mlb|nascar|uefa|epl|premier league|formula\s*1|motogp|"
    r"rugby|cricket|badminton|table tennis|golf|snooker|boxing|ufc|mma|"
    r"athletics|marathon|swimming|cycling|wrestling|surfing)\b"
    r"|亚运会|奥运会|冬奥会|残奥会|排球|网球|篮球|足球|冰球|赛车|田径|游泳|羽毛球|乒乓球|高尔夫|拳击|斯诺克|橄榄球|板球",
    re.IGNORECASE,
)
_EVENT_CONTEXT = re.compile(
    r"\b(?:match|final|semi[- ]?final|quarter[- ]?final|highlights|"
    r"tournament|championship|preseason|season\s*\d*|s\d{1,2}|"
    r"match\s*day|singles|doubles|medal\s*match|vs\.?|versus)\b"
    r"|\b20\d{2}(?:[-./]\d{1,2}){0,2}\b",
    re.IGNORECASE,
)
_LIVE_EVENT_CUE = re.compile(
    r"\b(?:match|final|semi[- ]?final|quarter[- ]?final|highlights|"
    r"tournament|match\s*day|singles|doubles|medal\s*match|vs\.?|versus)\b"
    r"|决赛|半决赛|四分之一决赛|比赛|赛事|集锦|对阵|對陣|vs",
    re.IGNORECASE,
)


def is_sports_category(value: object) -> bool:
    """Whether a category is M-Team's sports category or a common alias."""
    text = re.sub(r"\s+", "", str(value or "")).casefold()
    return text in {item.casefold() for item in _SPORT_CATEGORIES}


def is_sports_resource(package: dict, rows: list[dict] | None = None) -> bool:
    """Identify explicit sports categories or clearly labelled sports events.

    Heuristic classification requires both a sport and an event/season cue to
    avoid moving ordinary sports-themed films into the sports upload category.
    """
    if is_sports_category(package.get("category")):
        return True
    identity_values = [package.get(key) for key in ("title", "release_name", "subtitle", "input_path")]
    identity_text = " ".join(str(value or "") for value in identity_values)
    kind = str(package.get("kind") or "").casefold()
    # A series episode title may mention a sport (for example, Friends' “The
    # One with the Football”).  Never let episode-level filenames override an
    # otherwise non-sports TV identity.  Clearly labelled event packs still
    # match from their torrent/release-level name above.
    if _SPORT_TERMS.search(identity_text) and _EVENT_CONTEXT.search(identity_text):
        return kind in {"tv", "unknown", ""} or bool(_LIVE_EVENT_CUE.search(identity_text))
    episode_rows = rows or []
    episodic = any(re.search(r"(?i)(?:\bS\d{1,2}E\d{1,2}\b|\b\d{1,2}x\d{1,2}\b)", str(row.get("name") or ""))
                   for row in episode_rows)
    if kind == "tv" and episodic:
        return False
    text = identity_text + " " + " ".join(str(row.get("name") or "") for row in episode_rows)
    if not (_SPORT_TERMS.search(text) and _EVENT_CONTEXT.search(text)):
        return False
    # The model/filename classifier may label a single match as a movie. Keep
    # that obvious event on M-Team's sports category, while avoiding a sports
    # film that only mentions a sport or a year.
    return kind in {"tv", "unknown", ""} or bool(_LIVE_EVENT_CUE.search(text))
