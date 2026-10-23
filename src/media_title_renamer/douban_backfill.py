"""Resolve missing Douban subject links from trusted TMDB/IMDb metadata.

Searches use the user's existing Douban browser cookie in memory only. A link is
written only when the candidate's title/year/season evidence is strong, or its
Douban page exposes the exact IMDb ID. All other cases remain reviewable.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .cli import filename_hints
from .douban_cookie import DoubanCookieError, load_douban_cookie_header
from .prepare import (
    DEFAULT_DOUBAN_COOKIE_FILE,
    DoubanMatch,
    TmdbClient,
    _contains_cjk,
    _douban_request_headers,
    _douban_search_page_candidates,
    _douban_title_matches,
    _get_json,
    _get_text,
    _match_score,
    _normalise_name,
    _season_number,
    _strip_season_suffix,
)


DEFAULT_OUTPUT_ROOT = Path("/data/Zhyw/media-stack/downloads/.mteam-transfer/douban-backfill")
DEFAULT_RATE_STATE = DEFAULT_OUTPUT_ROOT / "cache" / "douban-request-rate.txt"
_IMDB_ID = re.compile(r"\btt\d{7,10}\b", re.I)
_DOUBAN_SUBJECT = re.compile(r"^/subject/(\d+)/?$")


@dataclass(frozen=True)
class WorkIdentity:
    titles: tuple[str, ...]
    year: str
    media_type: str
    tmdb_id: str
    imdb_id: str
    season_number: int | None


@dataclass
class BackfillResult:
    package_paths: list[str]
    title: str
    tmdb_id: str
    imdb_id: str
    status: str
    douban_url: str = ""
    confidence: float | None = None
    matched_by: str = ""
    query: str = ""
    chosen: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] | None = None
    diagnostics: list[str] | None = None
    error: str = ""
    web_search_used: bool = False
    identity_override_fields: list[str] | None = None


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _first(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _text(mapping.get(key))
        if value:
            return value
    return ""


def _unique(values: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = re.sub(r"\s+", " ", value).strip(" /|\t\r\n")
        key = clean.casefold()
        if clean and key not in seen:
            result.append(clean)
            seen.add(key)
    return tuple(result)


def _subtitle_titles(value: str) -> list[str]:
    clean = re.sub(r"\s*\[[^]]+\]\s*$", "", value).strip()
    return [part.strip() for part in re.split(r"\s*/\s*", clean) if part.strip()]


def _fallback_title(value: str) -> str:
    clean = re.sub(r"\s+", " ", value).strip()
    if not clean:
        return ""
    try:
        hints = filename_hints(Path(clean if Path(clean).suffix else clean + ".mkv"))
        if hints.title:
            return hints.title
    except (OSError, ValueError):
        pass
    # If filename parsing cannot separate the technical suffix, retain only a
    # conservative leading title portion instead of searching the whole encode.
    clean = re.sub(r"\s+(?:19|20)\d{2}\b.*$", "", clean)
    return clean.strip(" ._-|")


def _media_type(package: dict[str, Any], tmdb: dict[str, Any]) -> str:
    value = _first(tmdb, "media_type") or _first(package, "kind")
    if value.casefold() in {"tv", "series", "television"}:
        return "tv"
    if value.casefold() in {"movie", "film"}:
        return "movie"
    category = _first(package, "category").casefold()
    return "tv" if "影剧" in category or "电视剧" in category else "movie"


def _season(package: dict[str, Any]) -> int | None:
    values = [
        _first(package, "episode"),
        _first(package, "title"),
        _first(package, "release_name"),
        _first(package, "filename"),
    ]
    for value in values:
        if re.search(r"\bS\d{1,2}\s*[-–]\s*S?\d{1,2}\b", value, re.I):
            return None
        season = _season_number(value)
        if season is not None:
            return season
    return None


def _identity(package: dict[str, Any]) -> WorkIdentity:
    tmdb = package.get("tmdb") if isinstance(package.get("tmdb"), dict) else {}
    names = [
        _first(tmdb, "original_name", "original_title"),
        _first(tmdb, "name", "title"),
        _first(tmdb, "chinese_name"),
    ]
    names.extend(_subtitle_titles(_first(package, "subtitle")))
    fallback = _fallback_title(_first(package, "release_name", "title", "filename"))
    names.append(fallback)
    year = _first(tmdb, "year") or _first(package, "year")
    if not year:
        for value in (*names, _first(package, "title", "release_name")):
            match = re.search(r"\b((?:19|20)\d{2})\b", value)
            if match:
                year = match.group(1)
                break
    imdb_url = _first(package, "imdb_url")
    imdb_id = _first(tmdb, "imdb_id")
    if not imdb_id:
        match = _IMDB_ID.search(imdb_url)
        imdb_id = match.group(0).lower() if match else ""
    else:
        imdb_id = imdb_id.lower()
    season = _season(package) if _media_type(package, tmdb) == "tv" else None
    return WorkIdentity(
        titles=_unique(names),
        year=year if re.fullmatch(r"(?:19|20)\d{2}", year) else "",
        media_type=_media_type(package, tmdb),
        tmdb_id=_first(tmdb, "id") or _first(package, "tmdb_id"),
        imdb_id=imdb_id,
        season_number=season,
    )


_IDENTITY_OVERRIDE_FIELDS = {
    "titles", "year", "media_type", "tmdb_id", "imdb_id", "season_number", "candidate_urls"
}


def _validate_identity_override(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("identity override entry must be a JSON object")
    unknown = set(value) - _IDENTITY_OVERRIDE_FIELDS
    if unknown:
        raise ValueError("identity override contains unsupported fields: " + ", ".join(sorted(unknown)))
    normalized: dict[str, Any] = {}
    titles = value.get("titles", [])
    if isinstance(titles, str):
        titles = [titles]
    if not isinstance(titles, list) or any(not isinstance(item, str) for item in titles):
        raise ValueError("identity override titles must be a string array")
    clean_titles = _unique(titles)
    if clean_titles:
        normalized["titles"] = list(clean_titles)
    year = _text(value.get("year"))
    if year:
        if not re.fullmatch(r"(?:19|20)\d{2}", year):
            raise ValueError("identity override year must be a four-digit year")
        normalized["year"] = year
    media_type = _text(value.get("media_type")).casefold()
    if media_type:
        if media_type not in {"movie", "tv"}:
            raise ValueError("identity override media_type must be movie or tv")
        normalized["media_type"] = media_type
    tmdb_id = _text(value.get("tmdb_id"))
    if tmdb_id:
        if not tmdb_id.isdigit():
            raise ValueError("identity override tmdb_id must contain digits only")
        normalized["tmdb_id"] = tmdb_id
    imdb_id = _text(value.get("imdb_id")).lower()
    if imdb_id:
        if not re.fullmatch(r"tt\d{7,10}", imdb_id):
            raise ValueError("identity override imdb_id must be a valid tt identifier")
        normalized["imdb_id"] = imdb_id
    season = value.get("season_number")
    if season is not None:
        if isinstance(season, bool) or not str(season).isdigit():
            raise ValueError("identity override season_number must be a non-negative integer")
        normalized["season_number"] = int(season)
    candidate_urls = value.get("candidate_urls", [])
    if isinstance(candidate_urls, str):
        candidate_urls = [candidate_urls]
    if not isinstance(candidate_urls, list) or any(not isinstance(item, str) for item in candidate_urls):
        raise ValueError("identity override candidate_urls must be a string array")
    if candidate_urls:
        canonical_urls = [_canonical_douban_url(item) for item in candidate_urls]
        if any(not item for item in canonical_urls):
            raise ValueError("identity override candidate_urls must be canonical Douban subject URLs")
        normalized["candidate_urls"] = list(dict.fromkeys(canonical_urls))
    if not normalized:
        raise ValueError("identity override entry is empty")
    return normalized


def _merge_identity_override(
    identity: WorkIdentity, override: dict[str, Any]
) -> tuple[WorkIdentity, list[str]]:
    """Add verified identity hints, never replacing conflicting package metadata."""
    override = _validate_identity_override(override)
    for field_name in ("year", "media_type", "tmdb_id", "imdb_id", "season_number"):
        incoming = override.get(field_name)
        existing = getattr(identity, field_name)
        if incoming not in (None, "") and existing not in (None, "") and str(incoming).casefold() != str(existing).casefold():
            raise ValueError(f"identity override conflicts with existing {field_name}")
    merged = WorkIdentity(
        titles=_unique([*identity.titles, *override.get("titles", [])]),
        year=identity.year or override.get("year", ""),
        media_type=identity.media_type or override.get("media_type", ""),
        tmdb_id=identity.tmdb_id or override.get("tmdb_id", ""),
        imdb_id=identity.imdb_id or override.get("imdb_id", ""),
        season_number=(identity.season_number if identity.season_number is not None
                       else override.get("season_number")),
    )
    return merged, sorted(override)


def _load_identity_overrides(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read identity override JSON ({type(exc).__name__})") from exc
    if not isinstance(payload, dict) or not payload:
        raise ValueError("identity override file must map exact package paths to objects")
    result: dict[str, dict[str, Any]] = {}
    for raw_path, entry in payload.items():
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("identity override keys must be non-empty package paths")
        canonical_path = str(Path(raw_path).expanduser().resolve(strict=False))
        if canonical_path in result:
            raise ValueError("duplicate canonical package path in identity overrides")
        result[canonical_path] = _validate_identity_override(entry)
    return result


def _identity_override_for_paths(
    package_paths: list[Path], overrides: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    matched = [overrides[str(path.resolve(strict=False))] for path in package_paths
               if str(path.resolve(strict=False)) in overrides]
    if not matched:
        return None
    canonical = json.dumps(matched[0], sort_keys=True, ensure_ascii=False)
    if any(json.dumps(item, sort_keys=True, ensure_ascii=False) != canonical for item in matched[1:]):
        raise ValueError("conflicting identity overrides matched the same resource group")
    return matched[0]


def _enrich_tmdb_identity(
    package: dict[str, Any], identity: WorkIdentity, client: TmdbClient, diagnostics: list[str]
) -> WorkIdentity:
    if not identity.tmdb_id or not client.available or identity.imdb_id:
        return identity
    if identity.media_type not in {"movie", "tv"}:
        return identity
    try:
        details = client.by_id(identity.media_type, int(identity.tmdb_id))
    except (ValueError, urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as exc:
        diagnostics.append(f"TMDB external-ID lookup failed ({type(exc).__name__})")
        return identity
    except Exception as exc:
        # The error may contain a credential-bearing URL, so keep only its type.
        diagnostics.append(f"TMDB external-ID lookup failed ({type(exc).__name__})")
        return identity
    updated_names = _unique([
        *identity.titles,
        _text(details.original_name),
        _text(details.name),
        _text(details.chinese_name),
    ])
    return WorkIdentity(
        titles=updated_names,
        year=identity.year or _text(details.year),
        media_type=identity.media_type,
        tmdb_id=identity.tmdb_id,
        imdb_id=_text(details.imdb_id).lower(),
        season_number=identity.season_number,
    )


def _query_plan(identity: WorkIdentity, limit: int) -> list[tuple[str, str | None]]:
    queries: list[tuple[str, str | None]] = []
    title_limit = max(1, limit - 1) if identity.imdb_id and limit > 1 else limit
    for title in identity.titles:
        query = title
        year_filter: str | None = identity.year or None
        if identity.media_type == "tv" and identity.season_number is not None:
            season_phrase = (
                f"第{identity.season_number}季" if _contains_cjk(title)
                else f"Season {identity.season_number}"
            )
            if not re.search(rf"\bS0?{identity.season_number}\b|第\s*{identity.season_number}\s*季|Season\s+{identity.season_number}\b", title, re.I):
                query = f"{title} {season_phrase}"
            # Season air dates differ from a series' first-air year. Do not use
            # the series year as a hard search-page filter for later seasons.
            if identity.season_number > 1:
                year_filter = None
        elif identity.year and identity.year not in query:
            query = f"{query} {identity.year}"
        entry = (query.strip(), year_filter)
        if entry[0] and entry not in queries:
            queries.append(entry)
        if len(queries) >= title_limit:
            break
    if identity.imdb_id:
        imdb_query = (identity.imdb_id, None)
        if imdb_query not in queries and len(queries) < limit:
            queries.append(imdb_query)
    return queries


def _suggest_candidates(query: str, identity: WorkIdentity, diagnostics: list[str]) -> list[DoubanMatch]:
    url = "https://movie.douban.com/j/subject_suggest?q=" + urllib.parse.quote(query)
    try:
        data = _get_json(url, headers=_douban_request_headers(diagnostics))
    except urllib.error.HTTPError as exc:
        diagnostics.append(f"Douban suggestion HTTP {exc.code}")
        return []
    except urllib.error.URLError as exc:
        reason = str(exc.reason or "")
        diagnostics.append("Douban suggestion timeout" if "timed out" in reason.casefold() else "Douban suggestion network error")
        return []
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        diagnostics.append(f"Douban suggestion failed ({type(exc).__name__})")
        return []
    if not isinstance(data, list):
        diagnostics.append("Douban suggestion returned a non-list response")
        return []
    if not data:
        diagnostics.append("Douban suggestion HTTP 200 returned no candidates")
        return []
    result: list[DoubanMatch] = []
    for item in data[:10]:
        if not isinstance(item, dict):
            continue
        item_id = _text(item.get("id"))
        if not item_id.isdigit():
            continue
        title = _text(item.get("title"))
        original = _text(item.get("sub_title"))
        year = _text(item.get("year"))
        raw_url = _text(item.get("url"))
        result.append(
            DoubanMatch(
                id=item_id,
                url=raw_url or f"https://movie.douban.com/subject/{item_id}/",
                title=title,
                original_title=original,
                year=year,
                score=_match_score(query, [title, original], identity.year or None, year),
                season_number=_season_number(" ".join((title, original))),
                source="suggest",
            )
        )
    return result


def _canonical_subject_url(candidate: DoubanMatch) -> str:
    if not candidate.id.isdigit():
        return ""
    parsed = urlsplit(candidate.url)
    if parsed.netloc and not (parsed.hostname or "").casefold().endswith("douban.com"):
        return ""
    if parsed.path and not _DOUBAN_SUBJECT.fullmatch(parsed.path):
        return ""
    return f"https://movie.douban.com/subject/{candidate.id}/"


def _page_evidence(candidate: DoubanMatch, diagnostics: list[str]) -> dict[str, str]:
    url = _canonical_subject_url(candidate)
    if not url:
        return {"error": "invalid_subject_url"}
    try:
        page = _get_text(url, headers=_douban_request_headers(diagnostics), timeout=12)
    except urllib.error.HTTPError as exc:
        return {"error": f"subject_http_{exc.code}"}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"error": f"subject_fetch_{type(exc).__name__}"}
    escaped = html.unescape(page)
    imdb_matches = {
        match.group(1).lower()
        for match in re.finditer(r"imdb\.com/title/(tt\d{7,10})", escaped, re.I)
    }
    text = re.sub(r"<[^>]+>", " ", escaped)
    labeled = re.search(r"\bIMDb\s*[:：]?\s*(tt\d{7,10})\b", text, re.I)
    if labeled:
        imdb_matches.add(labeled.group(1).lower())
    media_type = ""
    for raw_type in re.findall(r'"@type"\s*:\s*(\[[^]]+\]|"[^"]+")', escaped, re.I):
        types = re.findall(r"[A-Za-z]+", raw_type)
        lowered = {value.casefold() for value in types}
        if "movie" in lowered:
            media_type = "movie"
            break
        if lowered & {"tvseries", "tvepisode", "tvseason"}:
            media_type = "tv"
            break
    return {"imdb_id": sorted(imdb_matches)[0] if imdb_matches else "", "media_type": media_type}


def _exact_title(candidate: DoubanMatch, titles: tuple[str, ...]) -> bool:
    candidate_keys = {
        _normalise_name(_strip_season_suffix(value))
        for value in (candidate.title, candidate.original_title)
        if _normalise_name(_strip_season_suffix(value))
    }
    expected_keys = {
        _normalise_name(_strip_season_suffix(value))
        for value in titles
        if _normalise_name(_strip_season_suffix(value))
    }
    return bool(candidate_keys & expected_keys)


def _year_compatible(candidate: DoubanMatch, identity: WorkIdentity) -> bool:
    if not identity.year or not candidate.year.isdigit():
        return not identity.year
    if identity.media_type == "tv" and candidate.season_number == identity.season_number:
        return True
    try:
        return abs(int(candidate.year) - int(identity.year)) <= 1
    except ValueError:
        return False


def _type_compatible(found_type: str, expected_type: str) -> bool:
    return not found_type or found_type == expected_type


def _candidate_rows(
    candidates: list[DoubanMatch],
    page_evidence: dict[str, dict[str, str]],
    identity: WorkIdentity,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        evidence = page_evidence.get(candidate.id, {})
        rows.append(
            {
                **asdict(candidate),
                "url": _canonical_subject_url(candidate),
                "imdb_id_on_douban_page": evidence.get("imdb_id", ""),
                "douban_media_type": evidence.get("media_type", ""),
                "imdb_id_match": bool(identity.imdb_id and evidence.get("imdb_id") == identity.imdb_id),
                "detail_error": evidence.get("error", ""),
            }
        )
    return rows


def _choose_candidate(
    candidates: list[DoubanMatch],
    page_evidence: dict[str, dict[str, str]],
    identity: WorkIdentity,
) -> tuple[DoubanMatch | None, float | None, str, str]:
    eligible: list[tuple[DoubanMatch, bool, bool, bool, bool, bool]] = []
    for candidate in candidates:
        if not _canonical_subject_url(candidate):
            continue
        evidence = page_evidence.get(candidate.id, {})
        imdb_match = bool(identity.imdb_id and evidence.get("imdb_id") == identity.imdb_id)
        title_match = _douban_title_matches(candidate, list(identity.titles))
        exact_title_match = _exact_title(candidate, identity.titles)
        # A Douban page carrying the exact IMDb ID is stronger identity
        # evidence than localized title text. Still enforce media type and
        # season compatibility below to prevent movie/series cross-matches.
        if not title_match and not imdb_match:
            continue
        type_mismatch = not _type_compatible(evidence.get("media_type", ""), identity.media_type)
        # A small number of Douban records mislabel a TV work as a movie. Permit
        # this only when IMDb and exact title both match and the page is a
        # series-level entry (it does not claim to be a numbered season).
        exact_imdb_type_override = bool(
            type_mismatch and imdb_match and exact_title_match and candidate.season_number is None
        )
        if type_mismatch and not exact_imdb_type_override:
            continue
        first_season_series_fallback = bool(
            imdb_match
            and title_match
            and identity.media_type == "tv"
            and identity.season_number is not None
            and identity.season_number > 1
            and candidate.season_number == 1
            and _year_compatible(candidate, identity)
        )
        page_imdb_id = evidence.get("imdb_id", "").casefold()
        first_season_title_year_fallback = bool(
            not imdb_match
            and identity.media_type == "tv"
            and identity.season_number is not None
            and identity.season_number > 1
            and candidate.season_number == 1
            and exact_title_match
            and identity.year
            and candidate.year == identity.year
            and _type_compatible(evidence.get("media_type", ""), "tv")
            and not (identity.imdb_id and page_imdb_id and page_imdb_id != identity.imdb_id.casefold())
            and not evidence.get("error")
        )
        if (
            identity.season_number is not None
            and candidate.season_number not in (None, identity.season_number)
            and not first_season_series_fallback
            and not first_season_title_year_fallback
        ):
            continue
        if not imdb_match and not _year_compatible(candidate, identity):
            continue
        eligible.append((
            candidate, imdb_match, exact_title_match, first_season_series_fallback,
            exact_imdb_type_override, first_season_title_year_fallback,
        ))

    if not eligible:
        return None, None, "", ""
    imdb_hits = [item for item in eligible if item[1]]
    if imdb_hits:
        imdb_hits.sort(key=lambda item: (item[2], item[0].score), reverse=True)
        best, _imdb_match, _exact_title_match, first_season_fallback, type_override, _title_year_fallback = imdb_hits[0]
        if type_override:
            return best, 0.99, "exact_imdb_id_type_override", ""
        if first_season_fallback:
            return best, 0.98, "exact_imdb_id_series_first_season", ""
        return best, 1.0, "exact_imdb_id", ""

    first_season_title_year_hits = [item for item in eligible if item[5]]
    if first_season_title_year_hits:
        first_season_title_year_hits.sort(key=lambda item: item[0].score, reverse=True)
        if (
            len(first_season_title_year_hits) > 1
            and first_season_title_year_hits[0][0].id != first_season_title_year_hits[1][0].id
            and abs(first_season_title_year_hits[0][0].score - first_season_title_year_hits[1][0].score) < 8
        ):
            return None, None, "", "多个第一季豆瓣候选同样匹配精确剧名和首播年，无法安全自动选择"
        best = first_season_title_year_hits[0][0]
        return best, 0.94, "exact_title_year_series_first_season", ""

    season_hits = [item for item in eligible if identity.season_number is not None and item[0].season_number == identity.season_number]
    if season_hits:
        eligible = season_hits

    eligible.sort(
        key=lambda item: (
            item[2],
            bool(identity.year and item[0].year == identity.year),
            item[0].score,
        ),
        reverse=True,
    )
    top = eligible[0]
    candidate = top[0]
    if candidate.score < 85 or not identity.year or not candidate.year:
        return None, None, "", "候选标题或年份证据不足，保留待确认"
    if len(eligible) > 1:
        next_candidate = eligible[1][0]
        if top[2] == eligible[1][2] and abs(candidate.score - next_candidate.score) < 8:
            return None, None, "", "多个豆瓣候选同样匹配，无法安全自动选择"
    if candidate.year != identity.year and not (
        identity.media_type == "tv" and candidate.season_number == identity.season_number
    ):
        return None, None, "", "候选年份与 TMDB 年份不一致，保留待确认"
    confidence = 0.95 if top[2] else 0.88
    return candidate, confidence, "title_year_season", ""


def _canonical_douban_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return ""
    if parsed.scheme != "https" or parsed.hostname != "movie.douban.com":
        return ""
    if parsed.port is not None or parsed.username or parsed.password:
        return ""
    match = _DOUBAN_SUBJECT.fullmatch(parsed.path)
    return f"https://movie.douban.com/subject/{match[1]}/" if match else ""


def _web_search_urls(identity: WorkIdentity, diagnostics: list[str]) -> list[str]:
    """Ask CLIProxyAPI web_search for URL proposals; never trust them unverified."""
    from .ai import CliproxyAssistant, CliproxyError

    assistant = CliproxyAssistant(web_search=True, timeout=75)
    if not assistant.available:
        diagnostics.append("CLIProxyAPI web_search 未配置或不可用")
        return []
    title_query = identity.titles[0] if identity.titles else ""
    if title_query:
        if identity.year and identity.year not in title_query:
            title_query += " " + identity.year
        if identity.media_type == "tv" and identity.season_number is not None:
            title_query += f" Season {identity.season_number}"
        title_query += " 豆瓣"
    search_queries = [query for query in (title_query, f"{identity.imdb_id} 豆瓣" if identity.imdb_id else "") if query]
    search_queries = list(dict.fromkeys(search_queries))[:2]
    system = (
        "你负责联网查找豆瓣作品页面。必须实际调用 web_search 搜索输入中的唯一 search_query，"
        "不得自行改成只搜索 IMDb 或忽略作品名。只根据该次搜索结果中真实出现的豆瓣 subject 页面返回 URL，"
        "不能凭记忆生成 ID，不能返回搜索页或其他网站链接。作品类型、年份和季数信息用于后续核对。"
        '只返回 JSON：{"urls":[最多5个真实 https://movie.douban.com/subject/数字/ 链接]}；找不到则空数组。'
    )
    urls: list[str] = []
    for query in search_queries:
        request_data = {
            "search_query": query,
            "titles": list(identity.titles[:8]),
            "year": identity.year,
            "media_type": identity.media_type,
            "season_number": identity.season_number,
            "tmdb_id": identity.tmdb_id,
            "imdb_id": identity.imdb_id,
        }
        try:
            response = assistant._complete_responses(system, json.dumps(request_data, ensure_ascii=False))
        except CliproxyError:
            diagnostics.append(f"CLIProxyAPI web_search 请求失败（query={query}）")
            break
        if not assistant.last_web_search_used:
            diagnostics.append("CLIProxyAPI 未实际调用 web_search，拒绝使用模型答案")
            break
        diagnostics.append("CLIProxyAPI web_search 已实际调用")
        executed_queries = getattr(assistant, "last_web_search_queries", [])
        diagnostics.append(
            "CLIProxyAPI 实际搜索词：" + " | ".join(executed_queries[:5])
            if executed_queries else f"CLIProxyAPI 请求搜索词：{query}"
        )
        values = response.get("urls") if isinstance(response, dict) else None
        if not isinstance(values, list):
            diagnostics.append("CLIProxyAPI web_search 返回结构不符合 urls 数组")
            continue
        urls.extend(filter(None, (_canonical_douban_url(value) for value in values)))
    urls = list(dict.fromkeys(urls))[:5]
    diagnostics.append(f"CLIProxyAPI web_search 规范化豆瓣候选 URL 数：{len(urls)}")
    return urls


def _web_page_candidate(
    url: str,
    identity: WorkIdentity,
    diagnostics: list[str],
) -> tuple[DoubanMatch | None, dict[str, str]]:
    """Fetch a proposed Douban subject page and extract independently checkable evidence."""
    from .web_candidates import open_search

    canonical = _canonical_douban_url(url)
    if not canonical:
        return None, {"error": "invalid_subject_url"}
    subject_id = _DOUBAN_SUBJECT.fullmatch(urlsplit(canonical).path).group(1)
    request = urllib.request.Request(
        canonical,
        headers=_douban_request_headers(diagnostics),
    )
    try:
        with open_search(request) as response:
            final = urlsplit(response.geturl())
            final_match = _DOUBAN_SUBJECT.fullmatch(final.path)
            if (final.scheme != "https" or final.hostname != "movie.douban.com"
                    or not final_match or final_match.group(1) != subject_id):
                diagnostics.append(f"豆瓣候选 {subject_id} 跳转到非同一作品页面")
                return None, {"error": "redirected"}
            body = response.read(2 * 1024 * 1024).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return None, {"error": f"subject_http_{exc.code}"}
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        return None, {"error": f"subject_fetch_{type(exc).__name__}"}

    escaped = html.unescape(body)
    heading = re.search(
        r'<span[^>]*property=["\']v:itemreviewed["\'][^>]*>(.*?)</span>', escaped, re.S | re.I
    )
    year_match = re.search(r'<span[^>]*class=["\'][^"\']*year[^"\']*["\'][^>]*>\s*\((\d{4})\)', escaped, re.S | re.I)
    if not heading or not year_match:
        blocked = bool(re.search(r"访问太频繁|验证码|安全验证|人机验证", escaped, re.I))
        diagnostics.append(f"豆瓣候选 {subject_id} 页面不可验证" + ("（访问受限）" if blocked else "（缺少标题或年份）"))
        return None, {"error": "rate_limited" if blocked else "unverifiable"}

    title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", heading.group(1))).strip()
    info_match = re.search(r'<div[^>]*id=["\']info["\'][^>]*>(.*?)</div>', escaped, re.S | re.I)
    info_text = ""
    if info_match:
        info_text = re.sub(r"<br\s*/?>", "\n", info_match.group(1), flags=re.I)
        info_text = html.unescape(re.sub(r"<[^>]+>", " ", info_text))
    aliases: list[str] = []
    for line in info_text.splitlines():
        match = re.search(r"(?:又名|原名|英文名|外文名)\s*[:：]\s*(.+)", line)
        if match:
            aliases.extend(re.split(r"\s*/\s*|\s*;\s*|\s*；\s*", match.group(1)))
    aliases = list(dict.fromkeys(re.sub(r"\s+", " ", item).strip() for item in aliases if item.strip()))
    expected_aliases = [
        alias for alias in aliases
        if _normalise_name(_strip_season_suffix(alias)) in {
            _normalise_name(_strip_season_suffix(expected)) for expected in identity.titles
        }
    ]
    original_title = expected_aliases[0] if expected_aliases else ""
    if not original_title and aliases:
        best_alias = max(aliases, key=lambda alias: max(
            (_match_score(expected, [alias], identity.year or None, year_match.group(1)) for expected in identity.titles),
            default=0,
        ))
        if max((_match_score(expected, [best_alias], identity.year or None, year_match.group(1))
                for expected in identity.titles), default=0) >= 85:
            original_title = best_alias

    page_text = re.sub(r"<[^>]+>", " ", escaped)
    imdb_ids = {item.lower() for item in re.findall(r"imdb\.com/title/(tt\d{7,10})", page_text, re.I)}
    labeled_imdb = re.search(r"\bIMDb\s*[:：]?\s*(tt\d{7,10})\b", page_text, re.I)
    if labeled_imdb:
        imdb_ids.add(labeled_imdb.group(1).lower())
    media_type = ""
    type_values = re.findall(r'"@type"\s*:\s*(\[[^]]+\]|"[^\"]+")', escaped, re.I)
    for raw_type in type_values:
        lowered = {value.casefold() for value in re.findall(r"[A-Za-z]+", raw_type)}
        if "movie" in lowered:
            media_type = "movie"
            break
        if lowered & {"tvseries", "tvepisode", "tvseason"}:
            media_type = "tv"
            break
    if not media_type:
        og_type = re.search(r'<meta[^>]+property=["\']og:type["\'][^>]+content=["\']([^"\']+)', escaped, re.I)
        value = og_type.group(1).casefold() if og_type else ""
        media_type = "movie" if "movie" in value else "tv" if "tv" in value else ""

    # Some Douban headings combine the localized title and original title in
    # one span and omit a separate "又名" field. Preserve an exact identity
    # title when it appears as a complete segment in that heading.
    normalized_heading = _normalise_name(title)
    heading_title = next(
        (expected for expected in identity.titles
         if _normalise_name(expected) and _normalise_name(expected) in normalized_heading),
        "",
    )
    if heading_title and not original_title:
        original_title = heading_title

    names = [title, *aliases]
    score = max(
        (_match_score(expected, names, identity.year or None, year_match.group(1)) for expected in identity.titles),
        default=0,
    )
    season_number = _season_number(" ".join((title, *aliases)))
    candidate = DoubanMatch(
        id=subject_id,
        url=canonical,
        title=title,
        original_title=original_title,
        year=year_match.group(1),
        score=score,
        season_number=season_number,
        source="cliproxy_web_search_verified_subject",
    )
    diagnostics.append(f"豆瓣候选 {subject_id} 页面已核验标题、年份及可用的 IMDb/类型信息")
    return candidate, {
        "imdb_id": sorted(imdb_ids)[0] if imdb_ids else "",
        "media_type": media_type,
    }


def _web_search_candidates(
    identity: WorkIdentity,
    diagnostics: list[str],
    *,
    max_details: int = 3,
) -> tuple[list[DoubanMatch], dict[str, dict[str, str]], bool]:
    urls = _web_search_urls(identity, diagnostics)
    candidates: list[DoubanMatch] = []
    evidence: dict[str, dict[str, str]] = {}
    for url in urls[:max(0, max_details)]:
        candidate, page_evidence = _web_page_candidate(url, identity, diagnostics)
        if candidate is None:
            continue
        candidates.append(candidate)
        evidence[candidate.id] = page_evidence
    return candidates, evidence, any("web_search 已实际调用" in item for item in diagnostics)


def _strong_candidate_exists(candidates: list[DoubanMatch], identity: WorkIdentity) -> bool:
    for candidate in candidates:
        if not _douban_title_matches(candidate, list(identity.titles)):
            continue
        if identity.season_number is not None and candidate.season_number not in (None, identity.season_number):
            continue
        if candidate.score >= 90 and _year_compatible(candidate, identity):
            return True
    return False


def _search_package(
    package: dict[str, Any],
    *,
    max_queries: int,
    max_details: int,
    tmdb_client: TmdbClient,
    identity_override: dict[str, Any] | None = None,
) -> tuple[WorkIdentity, list[DoubanMatch], dict[str, dict[str, str]], list[str], list[str]]:
    diagnostics: list[str] = []
    identity = _enrich_tmdb_identity(package, _identity(package), tmdb_client, diagnostics)
    if identity_override:
        identity, override_fields = _merge_identity_override(identity, identity_override)
        diagnostics.append("应用显式身份补充字段：" + ", ".join(override_fields))
    if not identity.titles and not identity.imdb_id:
        return identity, [], {}, [], diagnostics
    plan = _query_plan(identity, max_queries)
    candidates_by_id: dict[str, DoubanMatch] = {}
    queries: list[str] = []
    for query, year_filter in plan:
        queries.append(query)
        try:
            matches = _douban_search_page_candidates(query, year_filter, diagnostics=diagnostics)
        except urllib.error.HTTPError as exc:
            diagnostics.append(f"Douban subject search HTTP {exc.code}")
            if exc.code in {403, 429}:
                break
            matches = []
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
            diagnostics.append(f"Douban subject search failed ({type(exc).__name__})")
            matches = []
        for candidate in matches:
            previous = candidates_by_id.get(candidate.id)
            if previous is None or candidate.score > previous.score:
                candidates_by_id[candidate.id] = candidate
        current = list(candidates_by_id.values())
        if _strong_candidate_exists(current, identity):
            break
        if diagnostics and any(re.search(r"HTTP\s*(?:403|429)|频率过高|访问太频繁|验证码|安全验证", item, re.I) for item in diagnostics):
            break

    # The suggestion endpoint has a different result surface from the regular
    # search page and can find older titles hidden by the latter. Use it once,
    # only if the primary searches gave no candidates at all.
    if not candidates_by_id and identity.titles and not any(
        re.search(r"HTTP\s*(?:403|429)|频率过高|访问太频繁|验证码|安全验证", item, re.I)
        for item in diagnostics
    ):
        query = identity.titles[0]
        if identity.media_type == "tv" and identity.season_number is not None:
            query += f" Season {identity.season_number}"
        queries.append(query + " [suggest]")
        for candidate in _suggest_candidates(query, identity, diagnostics):
            candidates_by_id.setdefault(candidate.id, candidate)

    candidates = sorted(candidates_by_id.values(), key=lambda item: item.score, reverse=True)
    detail_candidates = [
        candidate for candidate in candidates
        if _douban_title_matches(candidate, list(identity.titles))
        and _canonical_subject_url(candidate)
    ][:max_details]
    page_evidence: dict[str, dict[str, str]] = {}
    for candidate in detail_candidates:
        page_evidence[candidate.id] = _page_evidence(candidate, diagnostics)
    for url in (identity_override or {}).get("candidate_urls", []):
        candidate, evidence = _web_page_candidate(url, identity, diagnostics)
        if candidate is None:
            diagnostics.append(f"显式候选 URL 未能独立核验：{evidence.get('error', 'unverifiable')}")
            continue
        previous = candidates_by_id.get(candidate.id)
        if previous is None or candidate.score >= previous.score:
            candidates_by_id[candidate.id] = candidate
        page_evidence[candidate.id] = evidence
        diagnostics.append(f"显式候选 {candidate.id} 已从豆瓣页面重新核验；URL 本身不作为匹配结论")
    candidates = sorted(candidates_by_id.values(), key=lambda item: item.score, reverse=True)
    return identity, candidates, page_evidence, queries, diagnostics


def _package_group_key(package: dict[str, Any], path: Path) -> str:
    torrent = package.get("torrent") if isinstance(package.get("torrent"), dict) else {}
    value = _first(torrent, "info_hash", "hash") or _first(package, "hash")
    if value:
        return "torrent:" + value.casefold()
    input_path = _first(package, "input_path")
    if input_path:
        return "input:" + str(Path(input_path).resolve(strict=False))
    file_rows = package.get("files")
    if isinstance(file_rows, list) and file_rows:
        fingerprints = [
            (_first(item, "filename", "path"), str(item.get("size_bytes") or item.get("size") or ""))
            for item in file_rows if isinstance(item, dict)
        ]
        if fingerprints:
            digest = hashlib.sha256(json.dumps(fingerprints, sort_keys=True).encode()).hexdigest()
            return "files:" + digest
    return "path:" + str(path.resolve(strict=False))


def _canonical_package_path(path: Path) -> Path:
    if path.parent.name == "prepare" and path.name == "mteam-prepare.json":
        peer = path.parent.parent / path.name
        if peer.is_file():
            return peer
    return path


def _discover_paths(sources: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for source in sources:
        if source.is_symlink():
            continue
        if source.is_file():
            if source.name == "mteam-prepare.json":
                paths.append(_canonical_package_path(source))
            elif source.name == "batch.json":
                try:
                    manifest = json.loads(source.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                for item in manifest.get("resources", []):
                    if not isinstance(item, dict):
                        continue
                    if item.get("status") not in {"pending", "candidate", "metadata_ready"}:
                        continue
                    resource_hash = _text(item.get("hash"))
                    if re.fullmatch(r"[0-9a-fA-F]{20,64}", resource_hash):
                        package_path = source.parent / "tasks" / resource_hash / "mteam-prepare.json"
                        if package_path.is_file() and not package_path.is_symlink():
                            paths.append(package_path)
        elif source.is_dir():
            paths.extend(
                _canonical_package_path(path)
                for path in source.rglob("mteam-prepare.json")
                if path.is_file() and not path.is_symlink() and path.parent.name != "prepare"
            )
    return sorted(
        {path.resolve(strict=True) for path in paths if path.is_file()},
        key=lambda path: str(path).casefold(),
    )


def _load_package_groups(paths: list[Path]) -> tuple[list[tuple[dict[str, Any], list[Path]]], list[dict[str, str]]]:
    groups: dict[str, tuple[dict[str, Any], list[Path]]] = {}
    errors: list[dict[str, str]] = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append({"package_path": str(path), "status": "package_read_error", "error": type(exc).__name__})
            continue
        if not isinstance(payload, dict):
            errors.append({"package_path": str(path), "status": "invalid_package", "error": "root_not_object"})
            continue
        key = _package_group_key(payload, path)
        if key not in groups:
            groups[key] = (payload, [path])
        else:
            groups[key][1].append(path)
    return list(groups.values()), errors


def _consensus_identity_key(package: dict[str, Any]) -> tuple[str, str, str, int | None] | None:
    identity = _identity(package)
    # Requiring both independent catalogue IDs avoids propagating a title-only
    # guess. TV links are season-scoped because Douban may have separate pages.
    if not identity.tmdb_id or not identity.imdb_id or identity.media_type not in {"movie", "tv"}:
        return None
    if identity.media_type == "tv" and identity.season_number is None:
        return None
    return identity.tmdb_id, identity.imdb_id, identity.media_type, identity.season_number


def _existing_link_consensus(
    groups: list[tuple[dict[str, Any], list[Path]]],
) -> dict[tuple[str, str, str, int | None], dict[str, dict[str, Any]]]:
    """Index existing URLs only when separate resource packages corroborate them."""
    indexed: dict[tuple[str, str, str, int | None], dict[str, dict[str, Any]]] = defaultdict(dict)
    for package, paths in groups:
        url = _canonical_douban_url(package.get("douban_url"))
        key = _consensus_identity_key(package)
        if not url or key is None:
            continue
        evidence = package.get("douban_match") if isinstance(package.get("douban_match"), dict) else {}
        anchor = str(paths[0]) if paths else _first(package, "input_path", "title")
        row = indexed[key].setdefault(url, {"groups": set(), "candidate": {}})
        row["groups"].add(anchor)
        candidate = row["candidate"]
        if not candidate or _text(evidence.get("title")):
            subject_id = _DOUBAN_SUBJECT.fullmatch(urlsplit(url).path).group(1)
            row["candidate"] = {
                "id": subject_id,
                "url": url,
                "title": _text(evidence.get("title")),
                "original_title": _text(evidence.get("original_title")),
                "year": _text(evidence.get("year")),
                "score": evidence.get("score", 0),
                "source": "existing_package_identity_consensus",
            }
    return indexed


def _seed_rate_state(rate_state: Path) -> None:
    """Carry forward the latest timestamp from per-run state files on upgrade."""
    if rate_state.exists() or not DEFAULT_OUTPUT_ROOT.exists():
        return
    latest = 0.0
    for previous in DEFAULT_OUTPUT_ROOT.rglob("douban-request-rate.txt"):
        if previous.is_symlink() or previous.resolve(strict=False) == rate_state.resolve(strict=False):
            continue
        try:
            latest = max(latest, float(previous.read_text(encoding="ascii").strip() or "0"))
        except (OSError, ValueError, UnicodeError):
            continue
    if latest <= 0:
        return
    rate_state.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(rate_state, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        stream.write(str(latest))
        stream.flush()
        os.fsync(stream.fileno())


def _consensus_result(
    package: dict[str, Any],
    package_paths: list[Path],
    index: dict[tuple[str, str, str, int | None], dict[str, dict[str, Any]]],
) -> BackfillResult | None:
    key = _consensus_identity_key(package)
    if key is None:
        return None
    urls = index.get(key, {})
    if len(urls) != 1:
        return None
    url, evidence = next(iter(urls.items()))
    corroborating_paths = sorted(evidence["groups"])
    if len(corroborating_paths) < 2:
        return None
    candidate = {
        **evidence["candidate"],
        "corroborating_package_paths": corroborating_paths,
        "tmdb_id": key[0],
        "imdb_id": key[1],
        "media_type": key[2],
        "season_number": key[3],
    }
    return BackfillResult(
        package_paths=[str(path) for path in package_paths],
        title=_first(package, "title", "release_name", "filename"),
        tmdb_id=key[0],
        imdb_id=key[1],
        status="mapped",
        douban_url=url,
        confidence=0.99,
        matched_by="same_tmdb_imdb_season_consensus",
        query=f"TMDB {key[0]} + IMDb {key[1]} + {key[2]} season {key[3]}",
        chosen=candidate,
        candidates=[candidate],
        diagnostics=[
            f"同一 TMDB/IMDb/作品类型/季数在 {len(corroborating_paths)} 个独立资源包中已有一致的豆瓣链接"
        ],
    )


def _backup_path(output_dir: Path, source: Path) -> Path:
    digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:20]
    return output_dir / "backups" / f"{source.name}.{digest}.bak"


def _write_package(
    path: Path,
    result: BackfillResult,
    *,
    output_dir: Path,
) -> str:
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"package reread failed ({type(exc).__name__})"
    if _text(current.get("douban_url")):
        return "not overwritten: package already has a Douban URL"
    candidate = result.chosen or {}
    url = _text(candidate.get("url"))
    if not _DOUBAN_SUBJECT.fullmatch(urlsplit(url).path) or (urlsplit(url).hostname or "").casefold() != "movie.douban.com":
        return "refused: invalid canonical Douban subject URL"
    backup = _backup_path(output_dir, path)
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        shutil.copy2(path, backup)
    current["douban_url"] = url
    current["douban_match"] = {
        "id": candidate.get("id", ""),
        "url": url,
        "title": candidate.get("title", ""),
        "original_title": candidate.get("original_title", ""),
        "year": candidate.get("year", ""),
        "score": candidate.get("score", 0),
        "source": candidate.get("source", ""),
    }
    current["douban_candidates"] = result.candidates or []
    current["douban_selection"] = {
        "id": candidate.get("id", ""),
        "url": url,
        "matched_by": result.matched_by,
        "confidence": result.confidence,
        "tmdb_id": result.tmdb_id,
        "imdb_id": result.imdb_id,
    }
    current["douban_backfill"] = {
        "tool": "media_title_renamer.douban_backfill",
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "query": result.query,
        "matched_by": result.matched_by,
        "confidence": result.confidence,
    }
    current["douban_diagnostics"] = result.diagnostics or []
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.writing")
    try:
        temp_path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(path)
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return f"package write failed ({type(exc).__name__})"
    return "updated"


def _apply_result(result: BackfillResult, *, output_dir: Path) -> str:
    messages: list[str] = []
    for package_path_text in result.package_paths:
        package_path = Path(package_path_text)
        messages.append(f"{package_path}: {_write_package(package_path, result, output_dir=output_dir)}")
    for package_path_text in result.package_paths:
        package_path = Path(package_path_text)
        sibling = package_path.parent / "prepare" / "mteam-prepare.json"
        if sibling.is_file() and not sibling.is_symlink() and str(sibling) not in result.package_paths:
            messages.append(f"{sibling}: {_write_package(sibling, result, output_dir=output_dir)}")
    return "; ".join(messages)


def _run_one(
    package: dict[str, Any],
    package_paths: list[Path],
    *,
    max_queries: int,
    max_details: int,
    tmdb_client: TmdbClient,
    use_web_search: bool = False,
    web_search_only: bool = False,
    identity_override: dict[str, Any] | None = None,
) -> BackfillResult:
    package_title = _first(package, "title", "release_name", "filename")
    preliminary = _identity(package)
    override_fields: list[str] = []
    try:
        if identity_override:
            preliminary, override_fields = _merge_identity_override(preliminary, identity_override)
    except ValueError as exc:
        return BackfillResult(
            package_paths=[str(path) for path in package_paths],
            title=package_title,
            tmdb_id=preliminary.tmdb_id,
            imdb_id=preliminary.imdb_id,
            status="identity_override_conflict",
            error=str(exc),
            diagnostics=["身份补充与资料包现有身份冲突；未执行搜索或写入"],
        )
    result = BackfillResult(
        package_paths=[str(path) for path in package_paths],
        title=package_title,
        tmdb_id=preliminary.tmdb_id,
        imdb_id=preliminary.imdb_id,
        status="pending",
        candidates=[],
        diagnostics=[],
        identity_override_fields=override_fields,
    )
    if _text(package.get("douban_url")):
        result.status = "already_present"
        result.douban_url = _text(package.get("douban_url"))
        return result
    if web_search_only:
        diagnostics: list[str] = []
        identity = _enrich_tmdb_identity(package, preliminary, tmdb_client, diagnostics)
        if identity_override:
            identity, override_fields = _merge_identity_override(identity, identity_override)
        candidates: list[DoubanMatch] = []
        page_evidence: dict[str, dict[str, str]] = {}
        queries: list[str] = []
    else:
        identity, candidates, page_evidence, queries, diagnostics = _search_package(
            package,
            max_queries=max_queries,
            max_details=max_details,
            tmdb_client=tmdb_client,
            identity_override=identity_override,
        )
    result.tmdb_id = identity.tmdb_id
    result.imdb_id = identity.imdb_id
    result.query = " | ".join(queries)
    result.diagnostics = diagnostics
    rows = _candidate_rows(candidates, page_evidence, identity)
    result.candidates = rows
    selected, confidence, matched_by, refusal = _choose_candidate(candidates, page_evidence, identity)
    if not selected and (use_web_search or web_search_only) and (identity.titles or identity.imdb_id):
        web_diagnostics: list[str] = []
        web_candidates, web_evidence, web_used = _web_search_candidates(
            identity, web_diagnostics, max_details=max_details
        )
        diagnostics.extend(web_diagnostics)
        result.web_search_used = web_used
        result.query = (result.query + " | CLIProxyAPI web_search").strip(" |")
        by_id = {candidate.id: candidate for candidate in candidates}
        for candidate in web_candidates:
            existing = by_id.get(candidate.id)
            if existing is None or candidate.score > existing.score or candidate.source.startswith("cliproxy_"):
                by_id[candidate.id] = candidate
        candidates = sorted(by_id.values(), key=lambda item: item.score, reverse=True)
        page_evidence.update(web_evidence)
        rows = _candidate_rows(candidates, page_evidence, identity)
        result.candidates = rows
        selected, confidence, matched_by, refusal = _choose_candidate(candidates, page_evidence, identity)
        result.diagnostics = diagnostics
    if selected:
        chosen_row = next((row for row in rows if row.get("id") == selected.id), None)
        if chosen_row is None:
            result.status = "invalid_candidate"
            result.error = "selected candidate was not serialized"
            return result
        result.status = "mapped"
        result.douban_url = _canonical_subject_url(selected)
        result.confidence = confidence
        result.matched_by = matched_by
        result.chosen = chosen_row
        if matched_by == "exact_imdb_id_type_override":
            diagnostics.append(
                "豆瓣页面类型标注与 TMDB 类型冲突；仅因 IMDb ID 和精确标题一致、且候选为系列页而接受此覆盖"
            )
            result.diagnostics = diagnostics
        return result
    result.error = refusal
    if not identity.titles and not identity.imdb_id:
        result.status = "missing_identity"
    elif not candidates:
        if any(re.search(r"HTTP\s*(?:403|429)|频率过高|访问太频繁|验证码|安全验证", item, re.I) for item in diagnostics):
            result.status = "search_blocked_or_rate_limited"
        elif diagnostics and all(
            re.search(r"正常返回 0 个候选|HTTP 200 returned no candidates", item, re.I)
            for item in diagnostics
        ):
            result.status = "no_candidate"
        elif diagnostics:
            result.status = "search_failed_or_empty"
        else:
            result.status = "no_candidate"
    else:
        result.status = "needs_review"
    return result


def _markdown_summary(results: list[BackfillResult], apply_messages: dict[str, str]) -> str:
    counts = Counter(result.status for result in results)
    lines = [
        "# Douban link backfill",
        "",
        f"Generated: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        "Cookie: authenticated Douban cookie loaded in memory; values are not recorded.",
        "CLIProxyAPI web_search: " + str(sum(result.web_search_used for result in results)) + " resource(s) returned a response after an actual web_search call.",
        "",
        "Counts: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())),
        "",
        "| Status | Title | TMDB | IMDb | Douban URL | Confidence | Apply |",
        "| --- | --- | --- | --- | --- | ---: | --- |",
    ]
    for result in results:
        apply_status = apply_messages.get(result.package_paths[0], "") if result.package_paths else ""
        lines.append(
            "| {status} | {title} | {tmdb} | {imdb} | {url} | {confidence} | {apply} |".format(
                status=result.status,
                title=result.title.replace("|", "\\|"),
                tmdb=result.tmdb_id,
                imdb=result.imdb_id,
                url=result.douban_url,
                confidence="" if result.confidence is None else f"{result.confidence:.2f}",
                apply=apply_status,
            )
        )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="media-douban-backfill",
        description="从 TMDB/IMDb 身份与登录态豆瓣搜索中安全补全发布资料包的 Douban URL。",
    )
    parser.add_argument("sources", nargs="+", type=Path, help="mteam-prepare.json、批次目录或 batch.json")
    parser.add_argument("--apply", action="store_true", help="将高置信映射写入资料包；默认仅预览")
    parser.add_argument("--output", type=Path, help="报告目录；默认写入 .mteam-transfer/douban-backfill/<时间戳>")
    parser.add_argument("--cookie-file", type=Path, help="Douban XLSX/Netscape Cookie 文件；默认使用已配置的 Cookie")
    parser.add_argument("--interval", type=float, default=30.0, help="两次豆瓣搜索请求的最小间隔秒数，默认 30")
    parser.add_argument(
        "--rate-state",
        type=Path,
        default=DEFAULT_RATE_STATE,
        help="跨批次共享的豆瓣搜索限速状态文件；默认位于 .mteam-transfer/douban-backfill/cache/",
    )
    parser.add_argument("--max-queries", type=int, default=3, help="每个条目的有界标题查询数，默认 3")
    parser.add_argument("--max-details", type=int, default=3, help="验证 IMDb/类型时读取的豆瓣候选详情页数，默认 3")
    parser.add_argument("--web-search", action="store_true", help="对常规豆瓣搜索未能确定的条目调用 CLIProxyAPI web_search；候选仍须独立核验")
    parser.add_argument(
        "--web-search-only",
        action="store_true",
        help="跳过豆瓣搜索接口，仅用 CLIProxyAPI 搜索标题与 IMDb；返回链接仍须读取豆瓣页面验证",
    )
    parser.add_argument(
        "--consensus-only",
        action="store_true",
        help="仅使用多个独立资料包中 TMDB/IMDb/类型/季数完全一致的现有豆瓣链接；不访问豆瓣或 CLIProxyAPI",
    )
    parser.add_argument(
        "--identity-overrides",
        type=Path,
        help=(
            "JSON 映射：以资料包绝对路径为键，补充已核实的标题别名、年份、类型或 TMDB/IMDb ID；"
            "只填补缺项，身份冲突时拒绝搜索和写入"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.interval < 0 or args.max_queries < 1 or args.max_details < 0:
        print("错误：interval 必须非负，max-queries 必须 >=1，max-details 必须非负。", file=sys.stderr)
        return 2
    if args.consensus_only and args.identity_overrides:
        print("错误：--identity-overrides 需要执行搜索，不能与 --consensus-only 一起使用。", file=sys.stderr)
        return 2
    try:
        identity_overrides = (
            _load_identity_overrides(args.identity_overrides.expanduser().resolve(strict=False))
            if args.identity_overrides else {}
        )
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    if args.cookie_file:
        os.environ["DOUBAN_COOKIE_FILE"] = str(args.cookie_file.expanduser().resolve(strict=False))
    elif not os.environ.get("DOUBAN_COOKIE_FILE") and DEFAULT_DOUBAN_COOKIE_FILE.is_file():
        os.environ["DOUBAN_COOKIE_FILE"] = str(DEFAULT_DOUBAN_COOKIE_FILE)

    output_dir = args.output or DEFAULT_OUTPUT_ROOT / datetime.now().strftime("%Y%m%dT%H%M%S%f")
    output_dir = output_dir.expanduser().resolve(strict=False)
    if output_dir.is_relative_to(Path("/tmp")):
        print("错误：报告和节流状态不能写入 /tmp。", file=sys.stderr)
        return 2
    if args.output and any((output_dir / name).exists() for name in ("summary.md", "results.jsonl")):
        print(f"错误：报告目录已有结果文件，拒绝覆盖：{output_dir}", file=sys.stderr)
        return 2
    output_dir.mkdir(parents=True, exist_ok=True)
    rate_state = args.rate_state.expanduser().resolve(strict=False)
    if rate_state.is_relative_to(Path("/tmp")) or rate_state.is_symlink():
        print("错误：豆瓣限速状态文件不能位于 /tmp 或使用符号链接。", file=sys.stderr)
        return 2
    if not args.consensus_only:
        _seed_rate_state(rate_state)
    os.environ["DOUBAN_REQUEST_INTERVAL"] = str(args.interval)
    os.environ["DOUBAN_RATE_STATE"] = str(rate_state)

    if not args.consensus_only:
        try:
            cookie_header = load_douban_cookie_header()
        except DoubanCookieError as exc:
            print(f"错误：无法读取 Douban Cookie：{exc}", file=sys.stderr)
            return 2
        if not cookie_header:
            print("错误：没有发现可用的 douban.com Cookie；未发送任何搜索请求。", file=sys.stderr)
            return 2
        # Do not emit, serialize, or retain the header after confirming it exists.
        cookie_header = ""
        print("[Cookie] 已从配置文件加载 Douban 登录 Cookie（凭据内容不输出、不写入报告）。", flush=True)
    else:
        print("[模式] 仅检查现有资料包之间的身份共识；不会联网搜索。", flush=True)

    package_paths = _discover_paths(args.sources)
    if not package_paths:
        print("没有找到 mteam-prepare.json 资料包。", file=sys.stderr)
        return 2
    groups, load_errors = _load_package_groups(package_paths)
    link_consensus = _existing_link_consensus(groups)
    tmdb_client = TmdbClient(
        read_token=os.environ.get("TMDB_READ_ACCESS_TOKEN", ""),
        api_key=os.environ.get("TMDB_API_KEY", ""),
    )
    results: list[BackfillResult] = []
    apply_messages: dict[str, str] = {}
    for index, (package, paths) in enumerate(groups, start=1):
        try:
            identity_override = _identity_override_for_paths(paths, identity_overrides)
        except ValueError as exc:
            identity = _identity(package)
            result = BackfillResult(
                package_paths=[str(path) for path in paths],
                title=_first(package, "title", "release_name", "filename"),
                tmdb_id=identity.tmdb_id,
                imdb_id=identity.imdb_id,
                status="identity_override_conflict",
                error=str(exc),
                diagnostics=["多个身份补充项冲突；未执行搜索或写入"],
            )
            results.append(result)
            print(f"[{index}/{len(groups)}] 身份补充冲突：{result.title}", flush=True)
            continue
        if _text(package.get("douban_url")):
            if args.consensus_only:
                identity = _identity(package)
                result = BackfillResult(
                    package_paths=[str(path) for path in paths],
                    title=_first(package, "title", "release_name", "filename"),
                    tmdb_id=identity.tmdb_id,
                    imdb_id=identity.imdb_id,
                    status="already_present",
                    douban_url=_canonical_douban_url(package.get("douban_url")),
                )
            else:
                result = _run_one(
                    package, paths, max_queries=args.max_queries, max_details=args.max_details,
                    tmdb_client=tmdb_client, use_web_search=args.web_search or args.web_search_only,
                    web_search_only=args.web_search_only,
                )
            results.append(result)
            print(f"[{index}/{len(groups)}] 已有豆瓣链接：{result.title}", flush=True)
            continue
        result = None if identity_override else _consensus_result(package, paths, link_consensus)
        if result is not None:
            if args.apply:
                apply_messages[result.package_paths[0]] = _apply_result(result, output_dir=output_dir)
            results.append(result)
            print(
                f"[{index}/{len(groups)}] 同身份资料包共识映射：{result.title} -> {result.douban_url}",
                flush=True,
            )
            continue
        if args.consensus_only:
            identity = _identity(package)
            result = BackfillResult(
                package_paths=[str(path) for path in paths],
                title=_first(package, "title", "release_name", "filename"),
                tmdb_id=identity.tmdb_id,
                imdb_id=identity.imdb_id,
                status="consensus_unavailable",
                candidates=[],
                diagnostics=["没有多个独立资料包对同一 TMDB/IMDb/作品类型/季数给出唯一一致的现有豆瓣链接"],
            )
            results.append(result)
            print(f"[{index}/{len(groups)}] 无可用身份共识：{result.title}", flush=True)
            continue
        print(f"[{index}/{len(groups)}] 搜索豆瓣：{_first(package, 'title', 'release_name', 'filename')}", flush=True)
        try:
            result = _run_one(
                package, paths, max_queries=args.max_queries, max_details=args.max_details,
                tmdb_client=tmdb_client, use_web_search=args.web_search or args.web_search_only,
                web_search_only=args.web_search_only,
                identity_override=identity_override,
            )
        except Exception as exc:
            identity = _identity(package)
            result = BackfillResult(
                package_paths=[str(path) for path in paths],
                title=_first(package, "title", "release_name", "filename"),
                tmdb_id=identity.tmdb_id,
                imdb_id=identity.imdb_id,
                status="search_failed",
                candidates=[],
                diagnostics=[f"Unhandled backfill error ({type(exc).__name__})"],
                error="unexpected_search_error",
            )
        results.append(result)
        if args.apply and result.status == "mapped":
            apply_messages[result.package_paths[0]] = _apply_result(result, output_dir=output_dir)
        print(
            f"  {result.status}"
            + (f" -> {result.douban_url} ({result.matched_by})" if result.douban_url else "")
            + (f"；候选 {len(result.candidates or [])}" if result.candidates else "；候选 0")
            + (f"；{result.error}" if result.error else ""),
            flush=True,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "results.jsonl").open("w", encoding="utf-8") as stream:
        for result in results:
            stream.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")
        for error in load_errors:
            stream.write(json.dumps(error, ensure_ascii=False) + "\n")
    (output_dir / "summary.md").write_text(_markdown_summary(results, apply_messages), encoding="utf-8")
    print(f"报告：{output_dir / 'summary.md'}")
    print(f"结果：{output_dir / 'results.jsonl'}")
    if args.apply:
        print("仅对高置信候选执行了本地资料包更新；没有发布、修改媒体源文件或操作 qB。")
    else:
        print("预览模式：未修改任何资料包。使用 --apply 写入高置信映射。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
