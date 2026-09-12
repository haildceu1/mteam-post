from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterator

from .cli import (
    VIDEO_EXTENSIONS,
    FilenameHints,
    MediaInfo,
    _canonical_source,
    _find_mediainfo,
    _is_release_prefix_label,
    _resolve_fields,
    _video_codec,
    build_title,
    filename_hints,
    read_mediainfo,
)


MTEAM_CATEGORIES = (
    "电影/SD",
    "电影/HD",
    "电影/DVDiSo",
    "电影/BluRay",
    "电影/Remux",
    "影剧/综艺/SD",
    "影剧/综艺/HD",
    "影剧/综艺/BluRay",
    "影剧/综艺/DVDiSo",
    "动画",
    "动画/Bluray",
)

LANGUAGE_NAMES = {
    "ar": "阿拉伯语",
    "cn": "粤语",
    "cs": "捷克语",
    "da": "丹麦语",
    "de": "德语",
    "en": "英语",
    "es": "西班牙语",
    "fi": "芬兰语",
    "fr": "法语",
    "hi": "印地语",
    "hu": "匈牙利语",
    "id": "印尼语",
    "it": "意大利语",
    "ja": "日语",
    "ko": "韩语",
    "nl": "荷兰语",
    "no": "挪威语",
    "pl": "波兰语",
    "pt": "葡萄牙语",
    "ro": "罗马尼亚语",
    "ru": "俄语",
    "sv": "瑞典语",
    "th": "泰语",
    "tr": "土耳其语",
    "uk": "乌克兰语",
    "vi": "越南语",
    "zh": "汉语",
}


@dataclass(frozen=True)
class TmdbMatch:
    id: int
    media_type: str
    name: str
    chinese_name: str
    original_name: str
    original_language: str
    year: str
    imdb_id: str
    genre_ids: tuple[int, ...]
    score: float


@dataclass(frozen=True)
class TmdbSeason:
    id: int
    season_number: int
    name: str
    chinese_name: str
    year: str


@dataclass(frozen=True)
class DoubanMatch:
    id: str
    url: str
    title: str
    original_title: str
    year: str
    score: float
    season_number: int | None = None


@dataclass(frozen=True)
class IsoMount:
    root: Path
    cleanup_method: str = ""
    cleanup_target: Path | str | None = None


def _normalise_name(value: str) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", value.casefold())


def _name_variants(value: str) -> list[str]:
    values = [value]
    if re.search(r"\d[ ._-]+\d", value):
        values.extend(
            [
                re.sub(r"(?<=\d)[ ._-]+(?=\d)", "/", value),
                re.sub(r"(?<=\d)[ ._/-]+(?=\d)", " ", value),
            ]
        )
    result: list[str] = []
    for item in values:
        item = re.sub(r"\s+", " ", item).strip()
        if item and item.casefold() not in {existing.casefold() for existing in result}:
            result.append(item)
    return result


def _year_from_date(value: str) -> str:
    match = re.match(r"((?:19|20)\d{2})", value or "")
    return match.group(1) if match else ""


def _match_score(query: str, names: list[str], expected_year: str | None, actual_year: str) -> float:
    query_key = _normalise_name(query)
    name_keys = [_normalise_name(name) for name in names if name]
    if not query_key or not name_keys:
        return 0.0
    ratios = [SequenceMatcher(None, query_key, candidate).ratio() for candidate in name_keys]
    score = max(ratios) * 70
    if query_key in name_keys:
        score += 20
    if expected_year and actual_year:
        score += 10 if expected_year == actual_year else -15
    return round(score, 2)


def _get_json(url: str, headers: dict[str, str] | None = None, timeout: int = 12) -> Any:
    request_headers = {"Accept": "application/json", "User-Agent": "media-title-rename/0.3"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_text(url: str, headers: dict[str, str] | None = None, timeout: int = 12) -> str:
    request_headers = {"Accept": "text/html,application/xhtml+xml", "User-Agent": "Mozilla/5.0"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


class TmdbClient:
    def __init__(self, read_token: str = "", api_key: str = "") -> None:
        self.read_token = read_token.strip()
        self.api_key = api_key.strip()

    @property
    def available(self) -> bool:
        return bool(self.read_token or self.api_key)

    def _get(self, path: str, **params: str) -> Any:
        if self.api_key:
            params["api_key"] = self.api_key
        query = urllib.parse.urlencode(params)
        headers = {"Authorization": f"Bearer {self.read_token}"} if self.read_token else None
        return _get_json(f"https://api.themoviedb.org/3{path}?{query}", headers=headers)

    def _details(self, media_type: str, item_id: int, score: float) -> TmdbMatch:
        endpoint = "tv" if media_type == "tv" else "movie"
        zh = self._get(
            f"/{endpoint}/{item_id}",
            language="zh-CN",
            append_to_response="external_ids,alternative_titles",
        )
        en = self._get(f"/{endpoint}/{item_id}", language="en-US")
        name_key = "name" if media_type == "tv" else "title"
        original_key = "original_name" if media_type == "tv" else "original_title"
        date_key = "first_air_date" if media_type == "tv" else "release_date"
        genres = tuple(int(item["id"]) for item in zh.get("genres", []) if "id" in item)
        external_ids = zh.get("external_ids", {})
        return TmdbMatch(
            id=int(item_id),
            media_type=media_type,
            name=str(en.get(name_key) or zh.get(name_key) or zh.get(original_key) or ""),
            chinese_name=str(zh.get(name_key) or ""),
            original_name=str(zh.get(original_key) or en.get(original_key) or ""),
            original_language=str(zh.get("original_language") or ""),
            year=_year_from_date(str(zh.get(date_key) or en.get(date_key) or "")),
            imdb_id=str(external_ids.get("imdb_id") or zh.get("imdb_id") or ""),
            genre_ids=genres,
            score=score,
        )

    def by_id(self, media_type: str, item_id: int) -> TmdbMatch:
        return self._details(media_type, item_id, 100.0)

    def season(self, item_id: int, season_number: int) -> TmdbSeason:
        """Return the season's localized and original names for a TV title."""
        zh = self._get(f"/tv/{int(item_id)}/season/{int(season_number)}", language="zh-CN")
        en = self._get(f"/tv/{int(item_id)}/season/{int(season_number)}", language="en-US")
        actual_number = int(zh.get("season_number") or en.get("season_number") or season_number)
        return TmdbSeason(
            id=int(zh.get("id") or en.get("id") or 0),
            season_number=actual_number,
            name=str(en.get("name") or zh.get("name") or ""),
            chinese_name=str(zh.get("name") or ""),
            year=_year_from_date(str(zh.get("air_date") or en.get("air_date") or "")),
        )

    def search(self, media_type: str, title: str, year: str | None) -> list[TmdbMatch]:
        endpoint = "tv" if media_type == "tv" else "movie"
        name_key = "name" if media_type == "tv" else "title"
        original_key = "original_name" if media_type == "tv" else "original_title"
        date_key = "first_air_date" if media_type == "tv" else "release_date"
        found: dict[int, tuple[float, dict[str, Any]]] = {}
        for query in _name_variants(title):
            params = {"query": query, "language": "zh-CN", "include_adult": "false"}
            if year:
                params["year"] = year
            data = self._get(f"/search/{endpoint}", **params)
            for item in data.get("results", [])[:10]:
                item_year = _year_from_date(str(item.get(date_key) or ""))
                score = _match_score(
                    query,
                    [str(item.get(name_key) or ""), str(item.get(original_key) or "")],
                    year,
                    item_year,
                )
                item_id = int(item["id"])
                if item_id not in found or score > found[item_id][0]:
                    found[item_id] = (score, item)
        ranked = sorted(found.items(), key=lambda entry: entry[1][0], reverse=True)
        matches: list[TmdbMatch] = []
        for item_id, (score, _item) in ranked[:5]:
            matches.append(self._details(media_type, item_id, score))
        return matches


def _choose_tmdb(candidates: list[TmdbMatch]) -> TmdbMatch | None:
    if not candidates:
        return None
    top = candidates[0]
    ambiguous = len(candidates) > 1 and top.score - candidates[1].score < 8
    if top.score >= 85 and not ambiguous:
        return top
    if not sys.stdin.isatty():
        return top if top.score >= 75 else None
    print("\nTMDB 找到多个可能结果：")
    for index, item in enumerate(candidates, start=1):
        print(f"  {index}. {item.name} / {item.original_name} ({item.year or '未知年份'})，匹配度 {item.score:.0f}")
    answer = input("选择编号；直接回车使用第 1 项；输入 0 跳过：").strip()
    if answer == "0":
        return None
    if answer.isdigit() and 1 <= int(answer) <= len(candidates):
        return candidates[int(answer) - 1]
    return top


def _douban_title_parts(
    raw_title: str,
    original_title: str,
    abstract: str,
    item_year: str,
) -> tuple[str, str, str, int | None]:
    """Split Douban's combined search title and infer its season number."""
    clean = re.sub(r"[\u200e\u200f\ufeff]", "", raw_title or "").strip()
    year_match = re.search(r"(?:19|20)\d{2}", clean)
    year = item_year or (year_match.group(0) if year_match else "")
    clean = re.sub(r"\s*[（(](?:19|20)\d{2}[）)]\s*$", "", clean).strip()
    original = (original_title or "").strip()
    title = clean
    if not original:
        # The HTML search page often combines a Chinese title and its original
        # title in one field, e.g. ``幸存者：珍珠岛 第七季 Survivor: Pearl
        # Islands Season 7 (2003)``.  Split only before a Latin word so the
        # Chinese season suffix remains part of the title.
        boundary = re.search(r"\s+(?=[A-Za-z][A-Za-z0-9])", clean)
        if boundary and _contains_cjk(clean[: boundary.start()]):
            title = clean[: boundary.start()].strip()
            original = clean[boundary.end() :].strip()
    season = _season_number(" ".join(value for value in (raw_title, original, abstract) if value))
    return title, original, year, season


def _douban_search_page_candidates(
    query: str,
    year: str | None,
) -> list[DoubanMatch]:
    """Search Douban's regular result page, which includes TV-season entries."""
    url = "https://search.douban.com/movie/subject_search?search_text=" + urllib.parse.quote(query)
    text = _get_text(url, headers={"User-Agent": "Mozilla/5.0"})
    marker = "window.__DATA__"
    start = text.find(marker)
    if start < 0:
        return []
    payload_start = text.find("{", start)
    if payload_start < 0:
        return []
    data, _end = json.JSONDecoder().raw_decode(text[payload_start:])
    found: list[DoubanMatch] = []
    for item in data.get("items", [])[:20]:
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        raw_title = str(item.get("title") or "")
        abstract = str(item.get("abstract") or "")
        title, original, item_year, season = _douban_title_parts(
            raw_title,
            "",
            abstract,
            str(item.get("year") or ""),
        )
        score = _match_score(query, [title, original, raw_title, abstract], year, item_year)
        found.append(
            DoubanMatch(
                id=item_id,
                url=str(item.get("url") or f"https://movie.douban.com/subject/{item_id}/"),
                title=title,
                original_title=original,
                year=item_year,
                score=score,
                season_number=season,
            )
        )
    return found


def _douban_candidates(
    names: list[str],
    year: str | None,
    expected_season: int | None = None,
) -> list[DoubanMatch]:
    found: dict[str, DoubanMatch] = {}

    def add(candidate: DoubanMatch) -> None:
        if candidate.id and (candidate.id not in found or candidate.score > found[candidate.id].score):
            found[candidate.id] = candidate

    unique_names: list[str] = []
    seen_names: set[str] = set()
    for name in names:
        key = name.casefold().strip()
        if not key or key in seen_names:
            continue
        seen_names.add(key)
        unique_names.append(name.strip())
    # Keep automatic lookups bounded; the first names are deliberately ordered
    # by _douban_for_release from the most specific to the broadest fallback.
    for name in unique_names[:8]:
        if not name:
            continue
        for query in _name_variants(name):
            url = "https://movie.douban.com/j/subject_suggest?q=" + urllib.parse.quote(query)
            try:
                data = _get_json(url, headers={"User-Agent": "Mozilla/5.0"})
            except (OSError, urllib.error.URLError, json.JSONDecodeError):
                continue
            for item in data[:10]:
                item_id = str(item.get("id") or "")
                item_year = str(item.get("year") or "")
                title = str(item.get("title") or "")
                original = str(item.get("sub_title") or "")
                season = _season_number(" ".join(value for value in (title, original) if value))
                score = _match_score(query, [title, original], year, item_year)
                add(
                    DoubanMatch(
                        id=item_id,
                        url=f"https://movie.douban.com/subject/{item_id}/" if item_id else str(item.get("url") or ""),
                        title=title,
                        original_title=original,
                        year=item_year,
                        score=score,
                        season_number=season,
                    )
                )

    if expected_season is not None:
        # The suggest endpoint frequently returns only the newest season.  The
        # regular HTML search has all season-specific entries, so query it with
        # an explicit season suffix and merge its results with the suggest API.
        page_queries: list[str] = []
        for name in unique_names[:8]:
            if not name:
                continue
            page_queries.append(f"{name} Season {expected_season}")
            if _contains_cjk(name):
                page_queries.append(f"{name} 第{expected_season}季")
        seen_queries: set[str] = set()
        for query in page_queries[:6]:
            if query.casefold() in seen_queries:
                continue
            seen_queries.add(query.casefold())
            try:
                for candidate in _douban_search_page_candidates(query, year):
                    add(candidate)
            except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError):
                continue
    return sorted(found.values(), key=lambda item: item.score, reverse=True)


def _choose_douban(
    candidates: list[DoubanMatch],
    expected_season: int | None = None,
) -> DoubanMatch | None:
    if expected_season is not None:
        candidates = [item for item in candidates if item.season_number == expected_season]
        if not candidates:
            return None
    if not candidates:
        return None
    top = candidates[0]
    ambiguous = len(candidates) > 1 and top.score - candidates[1].score < 8
    if top.score >= 85 and not ambiguous:
        return top
    if not sys.stdin.isatty():
        return top if top.score >= 75 else None
    print("\n豆瓣找到多个可能结果：")
    for index, item in enumerate(candidates[:5], start=1):
        print(f"  {index}. {item.title} / {item.original_title} ({item.year or '未知年份'})")
    answer = input("选择编号；直接回车使用第 1 项；输入 0 后手工提供：").strip()
    if answer == "0":
        return None
    if answer.isdigit() and 1 <= int(answer) <= min(5, len(candidates)):
        return candidates[int(answer) - 1]
    return top


def _contains_cjk(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", value))


def build_subtitle(
    *,
    douban: DoubanMatch | None,
    tmdb: TmdbMatch | None,
    fallback_title: str,
    language_code: str,
) -> str:
    chinese = ""
    if douban and _contains_cjk(douban.title):
        chinese = douban.title
    elif tmdb and _contains_cjk(tmdb.chinese_name):
        chinese = tmdb.chinese_name
    original = ""
    if tmdb:
        original = tmdb.original_name or tmdb.name
    if not original and douban:
        original = douban.original_title or douban.title
    original = original or fallback_title
    names: list[str] = []
    for value in (chinese, original):
        if value and _normalise_name(value) not in {_normalise_name(item) for item in names}:
            names.append(value)
    language = LANGUAGE_NAMES.get(language_code.lower(), language_code) if language_code else ""
    result = " / ".join(names)
    return f"{result} [{language}]" if language else result


def infer_mteam_category(*, kind: str, source: str, resolution: str, animation: bool) -> str:
    source_upper = source.upper()
    disc_bluray = source in {"BluRay", "UHD BluRay"}
    any_bluray = "BLURAY" in source_upper
    dvd_iso = source in {"DVD", "DVD5", "DVD9"}
    resolution_match = re.match(r"(\d+)[pi]$", resolution or "", re.I)
    # M-Team treats sub-720p releases such as 480p/576p/540p/544p as SD.
    # The Survivor S07 files are 544p and must therefore use the SD category.
    sd = bool(resolution_match and int(resolution_match.group(1)) < 720)
    if animation:
        return "动画/Bluray" if any_bluray else "动画"
    if kind == "tv":
        if dvd_iso:
            return "影剧/综艺/DVDiSo"
        if disc_bluray:
            return "影剧/综艺/BluRay"
        return "影剧/综艺/SD" if sd else "影剧/综艺/HD"
    if dvd_iso:
        return "电影/DVDiSo"
    if "REMUX" in source_upper:
        return "电影/Remux"
    if disc_bluray:
        return "电影/BluRay"
    return "电影/SD" if sd else "电影/HD"


def _bencode(value: Any) -> bytes:
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii") + b"e"
    if isinstance(value, str):
        return _bencode(value.encode("utf-8"))
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    if isinstance(value, (list, tuple)):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        pairs: list[tuple[bytes, Any]] = []
        for key, item in value.items():
            encoded_key = key if isinstance(key, bytes) else str(key).encode("utf-8")
            pairs.append((encoded_key, item))
        return b"d" + b"".join(_bencode(key) + _bencode(item) for key, item in sorted(pairs)) + b"e"
    raise TypeError(f"不支持 bencode 类型：{type(value).__name__}")


def automatic_piece_length(total_size: int) -> int:
    piece_length = 64 * 1024
    while math.ceil(max(total_size, 1) / piece_length) > 2000 and piece_length < 16 * 1024 * 1024:
        piece_length *= 2
    return piece_length


def _format_transfer_size(value: float) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = max(float(value), 0.0)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}" if unit != "B" else f"{amount:.0f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


def _format_remaining_time(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "--:--"
    rounded = int(seconds)
    hours, remainder = divmod(rounded, 3600)
    minutes, seconds_part = divmod(remainder, 60)
    return f"{hours:d}:{minutes:02d}:{seconds_part:02d}" if hours else f"{minutes:02d}:{seconds_part:02d}"


def _console_safe_text(value: str) -> str:
    """Avoid a progress line aborting work in legacy Windows code pages."""
    encoding = getattr(sys.stdout, "encoding", None)
    if not encoding:
        return value
    try:
        return value.encode(encoding, errors="replace").decode(encoding)
    except LookupError:
        return value


class TorrentProgress:
    """A bounded, terminal-friendly progress display for local torrent hashing."""

    def __init__(self, total_bytes: int, file_count: int, initial_bytes: int = 0) -> None:
        self.total_bytes = max(total_bytes, 0)
        self.file_count = max(file_count, 1)
        self.completed_bytes = min(max(initial_bytes, 0), self.total_bytes)
        self.bytes_this_run = 0
        self.current_file_index = 0
        self.current_file_name = ""
        self.started_at = time.monotonic()
        self.last_render_at = 0.0
        self.last_bucket = -1
        self.interactive = bool(getattr(sys.stdout, "isatty", lambda: False)())
        self.line_active = False
        self.disabled = False

    def start_file(self, index: int, path: Path) -> None:
        self.current_file_index = index
        self.current_file_name = path.name
        # In an interactive console this makes a long multi-file hash visibly
        # move to the next episode without emitting 96 separate log lines.
        self._render(force=self.interactive or index == 1)

    def advance(self, size: int) -> None:
        added = min(max(size, 0), self.total_bytes - self.completed_bytes)
        self.completed_bytes += added
        self.bytes_this_run += added
        self._render()

    def finish(self) -> None:
        self.completed_bytes = self.total_bytes
        if self.interactive or self.last_bucket != 20:
            self._render(force=True)
        try:
            if self.interactive and self.line_active and not self.disabled:
                sys.stdout.write("\n")
                sys.stdout.flush()
            elif not self.interactive and not self.disabled:
                print("制种完成。")
        except (OSError, UnicodeError):
            self.disabled = True

    def interrupt(self) -> None:
        if self.interactive and self.line_active and not self.disabled:
            try:
                sys.stdout.write("\n")
                sys.stdout.flush()
            except OSError:
                self.disabled = True
            self.line_active = False

    def _render(self, *, force: bool = False) -> None:
        if self.disabled:
            return
        now = time.monotonic()
        total = self.total_bytes
        percentage = 100.0 if total == 0 else self.completed_bytes * 100 / total
        bucket = min(20, int(percentage // 5))
        if not force:
            if self.interactive and now - self.last_render_at < 1.0 and self.completed_bytes < total:
                return
            if not self.interactive and bucket == self.last_bucket and self.completed_bytes < total:
                return
        elapsed = max(now - self.started_at, 0.001)
        speed = self.bytes_this_run / elapsed
        remaining = (total - self.completed_bytes) / speed if speed > 0 else None
        width = 24
        filled = min(width, int(width * percentage / 100))
        # Keep the bar ASCII-only so legacy Windows GBK terminals cannot make
        # the actual torrent hash fail while merely rendering progress.
        bar = "#" * filled + "-" * (width - filled)
        file_name = self.current_file_name
        if len(file_name) > 46:
            file_name = file_name[:43] + "..."
        file_part = (
            f" | 文件 {self.current_file_index}/{self.file_count}: {file_name}"
            if self.current_file_index
            else ""
        )
        line = (
            f"制种进度 [{bar}] {percentage:5.1f}%  "
            f"{_format_transfer_size(self.completed_bytes)}/{_format_transfer_size(total)}  "
            f"{_format_transfer_size(speed)}/s  剩余 {_format_remaining_time(remaining)}{file_part}"
        )
        try:
            if self.interactive:
                sys.stdout.write("\r" + _console_safe_text(line))
                sys.stdout.flush()
                self.line_active = True
            else:
                print(_console_safe_text(line))
        except (OSError, UnicodeError):
            # Progress rendering must never interrupt a multi-hour hash if a
            # terminal/redirected stdout handle disappears.
            self.disabled = True
            return
        self.last_render_at = now
        self.last_bucket = bucket


_TORRENT_RESUME_SCHEMA = 1
_TORRENT_PIECE_HASH_SIZE = hashlib.sha1().digest_size


def _resume_paths(output: Path) -> tuple[Path, Path]:
    return (
        output.with_suffix(output.suffix + ".resume.json"),
        output.with_suffix(output.suffix + ".resume.pieces"),
    )


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _torrent_resume_identity(
    *,
    kind: str,
    total_size: int,
    piece_length: int,
    logical_root_name: str,
    files: list[tuple[Path, Path, int, int]],
) -> dict[str, Any]:
    return {
        "schema": _TORRENT_RESUME_SCHEMA,
        "kind": kind,
        "total_size": total_size,
        "piece_length": piece_length,
        "logical_root_name": logical_root_name,
        "files": [
            {
                "source_path": str(source.resolve()),
                "logical_path": list(logical_path.parts),
                "size": size,
                "mtime_ns": mtime_ns,
            }
            for source, logical_path, size, mtime_ns in files
        ],
    }


class TorrentHashCheckpoint:
    """Append-only SHA-1 piece checkpoint used to resume an interrupted hash."""

    def __init__(
        self,
        *,
        metadata_path: Path,
        pieces_path: Path,
        total_size: int,
        piece_length: int,
        completed_pieces: int,
    ) -> None:
        self.metadata_path = metadata_path
        self.pieces_path = pieces_path
        self.total_size = total_size
        self.piece_length = piece_length
        self.completed_pieces = completed_pieces
        self._handle = pieces_path.open("ab")

    @property
    def completed_bytes(self) -> int:
        return min(self.total_size, self.completed_pieces * self.piece_length)

    @classmethod
    def open_or_create(
        cls,
        output: Path,
        identity: dict[str, Any],
    ) -> "TorrentHashCheckpoint":
        metadata_path, pieces_path = _resume_paths(output)
        metadata_exists = metadata_path.is_file()
        pieces_exists = pieces_path.is_file()
        if metadata_exists or pieces_exists:
            if not (metadata_exists and pieces_exists):
                raise RuntimeError(
                    "检测到不完整的种子续传检查点；请删除以下两个自动生成文件后重新制种："
                    f"{metadata_path}；{pieces_path}"
                )
            try:
                saved = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"无法读取种子续传检查点：{metadata_path}：{exc}") from exc
            if saved != identity:
                raise RuntimeError(
                    "已有种子续传检查点与当前文件列表、大小或修改时间不一致；"
                    "为避免制作出错误种子，未继续。请保留原文件不变，"
                    f"或删除检查点后重新开始：{metadata_path}"
                )
            print(f"发现未完成的种子哈希，将从检查点继续：{metadata_path.name}")
        else:
            _atomic_json_write(metadata_path, identity)
            pieces_path.parent.mkdir(parents=True, exist_ok=True)
            pieces_path.write_bytes(b"")

        pieces_size = pieces_path.stat().st_size
        valid_size = pieces_size - pieces_size % _TORRENT_PIECE_HASH_SIZE
        if valid_size != pieces_size:
            # A process may have stopped midway through a 20-byte hash write.
            # This is an internal temporary file, so discarding only that
            # incomplete hash is safe; the corresponding 16 MiB is re-read.
            with pieces_path.open("r+b") as handle:
                handle.truncate(valid_size)
            pieces_size = valid_size
        completed_pieces = pieces_size // _TORRENT_PIECE_HASH_SIZE
        maximum_pieces = math.ceil(identity["total_size"] / identity["piece_length"]) if identity["total_size"] else 0
        if completed_pieces > maximum_pieces:
            raise RuntimeError(
                "种子续传检查点的分块数超过当前文件总大小，未继续："
                f"{pieces_path}"
            )
        return cls(
            metadata_path=metadata_path,
            pieces_path=pieces_path,
            total_size=int(identity["total_size"]),
            piece_length=int(identity["piece_length"]),
            completed_pieces=completed_pieces,
        )

    def append_hash(self, payload: bytes) -> None:
        self.append_digest(hashlib.sha1(payload).digest())

    def append_digest(self, digest: bytes) -> None:
        if len(digest) != _TORRENT_PIECE_HASH_SIZE:
            raise ValueError("torrent piece digest must be 20 bytes")
        self._handle.write(digest)
        self.completed_pieces += 1
        # Sync about once per GiB for 16 MiB pieces.  Normal exceptions also
        # close the handle, preserving all hashes written so far.
        if self.completed_pieces % 64 == 0:
            self._handle.flush()
            os.fsync(self._handle.fileno())

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.flush()
            self._handle.close()

    def read_hashes(self) -> bytes:
        self.close()
        return self.pieces_path.read_bytes()

    def cleanup(self) -> None:
        self.close()
        self.metadata_path.unlink(missing_ok=True)
        self.pieces_path.unlink(missing_ok=True)


class TorrentPieceHasher:
    """Stream bytes into fixed-size SHA-1 pieces with bounded memory."""

    def __init__(self, checkpoint: TorrentHashCheckpoint, piece_length: int) -> None:
        self.checkpoint = checkpoint
        self.piece_length = piece_length
        self._hasher = hashlib.sha1()
        self._piece_bytes = 0

    def update(self, payload: bytes) -> None:
        view = memoryview(payload)
        offset = 0
        try:
            while offset < len(view):
                remaining = self.piece_length - self._piece_bytes
                take = min(remaining, len(view) - offset)
                self._hasher.update(view[offset : offset + take])
                offset += take
                self._piece_bytes += take
                if self._piece_bytes == self.piece_length:
                    self.checkpoint.append_digest(self._hasher.digest())
                    self._hasher = hashlib.sha1()
                    self._piece_bytes = 0
        finally:
            view.release()

    def finish(self) -> None:
        if self._piece_bytes:
            self.checkpoint.append_digest(self._hasher.digest())
            self._hasher = hashlib.sha1()
            self._piece_bytes = 0


def _hash_file_from_offset(
    handle: Any,
    start_offset: int,
    file_size: int,
    piece_hasher: TorrentPieceHasher,
    progress: TorrentProgress,
    source: Path,
) -> int:
    """Read a file through one reusable buffer and feed the piece hasher.

    ``read`` allocates a new bytes object for every call.  On very long
    multi-file hashes that can eventually fragment the process address space,
    even though each individual read is small.  ``readinto`` keeps one buffer
    alive for the entire file and bounds both the allocation count and memory
    usage.
    """
    file_offset = start_offset
    read_buffer = bytearray(1024 * 1024)
    buffer_view = memoryview(read_buffer)
    try:
        while file_offset < file_size:
            request_size = min(len(buffer_view), file_size - file_offset)
            count = handle.readinto(buffer_view[:request_size])
            if not count:
                raise RuntimeError(
                    f"制种读取失败：{source} 在文件内偏移 {file_offset:,}/{file_size:,} 字节提前结束"
                )
            piece_hasher.update(buffer_view[:count])
            file_offset += count
            progress.advance(count)
    finally:
        buffer_view.release()
    return file_offset


def _torrent_file_specs(files: list[tuple[Path, Path]]) -> list[tuple[Path, Path, int, int]]:
    return [
        (source, logical_path, source.stat().st_size, source.stat().st_mtime_ns)
        for source, logical_path in files
    ]


def create_private_v1_torrent(source: Path, output: Path, logical_name: str) -> int:
    file_specs = _torrent_file_specs([(source, Path(logical_name))])
    _source, _logical_path, total_size, _mtime_ns = file_specs[0]
    piece_length = automatic_piece_length(total_size)
    piece_count = math.ceil(total_size / piece_length) if total_size else 1
    print(f"正在生成 V1 私有种子：{piece_count} 个分块，每块 {piece_length // 1024} KiB")
    identity = _torrent_resume_identity(
        kind="single",
        total_size=total_size,
        piece_length=piece_length,
        logical_root_name=logical_name,
        files=file_specs,
    )
    checkpoint = TorrentHashCheckpoint.open_or_create(output, identity)
    progress = TorrentProgress(total_size, 1, checkpoint.completed_bytes)
    progress.start_file(1, source)
    piece_hasher = TorrentPieceHasher(checkpoint, piece_length)
    try:
        file_offset = checkpoint.completed_bytes
        with source.open("rb") as handle:
            handle.seek(file_offset)
            file_offset = _hash_file_from_offset(
                handle,
                file_offset,
                total_size,
                piece_hasher,
                progress,
                source,
            )
        piece_hasher.finish()
        pieces = checkpoint.read_hashes()
    except MemoryError as exc:
        progress.interrupt()
        checkpoint.close()
        raise RuntimeError(
            f"制种内存不足：{source}（全局进度 {progress.completed_bytes:,}/{total_size:,} 字节）；"
            "已保存的续传检查点仍可继续"
        ) from exc
    except OSError as exc:
        progress.interrupt()
        checkpoint.close()
        raise RuntimeError(
            f"制种读取失败：{source}（文件内偏移 {file_offset:,}/{total_size:,} 字节，"
            f"全局进度 {progress.completed_bytes:,}/{total_size:,} 字节）：{exc}"
        ) from exc
    except BaseException:
        progress.interrupt()
        checkpoint.close()
        raise
    progress.finish()
    info = {
        "length": total_size,
        "name": logical_name,
        "piece length": piece_length,
        "pieces": bytes(pieces),
        "private": 1,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_bytes(_bencode({"info": info}))
    temporary.replace(output)
    checkpoint.cleanup()
    return piece_length


def create_private_v1_folder_torrent(
    root: Path,
    files: list[tuple[Path, Path]],
    output: Path,
    logical_root_name: str,
) -> int:
    """Create one BEP 3 multi-file torrent.

    ``files`` contains (physical source, logical relative path) pairs. This lets
    prepare hash the current files before --apply while writing their future
    normalized names into the torrent.
    """
    if not files:
        raise ValueError("文件夹中没有可制种的视频文件")
    file_specs = _torrent_file_specs(files)
    total_size = sum(size for _source, _logical, size, _mtime_ns in file_specs)
    piece_length = automatic_piece_length(total_size)
    piece_count = math.ceil(total_size / piece_length) if total_size else 0
    print(f"正在生成 V1 私有目录种子：{piece_count} 个分块，每块 {piece_length // 1024} KiB")
    identity = _torrent_resume_identity(
        kind="folder",
        total_size=total_size,
        piece_length=piece_length,
        logical_root_name=logical_root_name,
        files=file_specs,
    )
    checkpoint = TorrentHashCheckpoint.open_or_create(output, identity)
    piece_hasher = TorrentPieceHasher(checkpoint, piece_length)
    file_entries: list[dict[str, Any]] = []
    progress = TorrentProgress(total_size, len(file_specs), checkpoint.completed_bytes)
    remaining_skip = checkpoint.completed_bytes
    try:
        for index, (source, logical_path, file_size, _mtime_ns) in enumerate(file_specs, start=1):
            try:
                source.resolve().relative_to(root.resolve())
            except ValueError as exc:
                raise ValueError(f"制种文件不在输入目录内：{source}") from exc
            if logical_path.is_absolute() or ".." in logical_path.parts:
                raise ValueError(f"种子内部路径不安全：{logical_path}")
            file_entries.append({"length": file_size, "path": list(logical_path.parts)})
            progress.start_file(index, source)
            file_offset = min(remaining_skip, file_size)
            remaining_skip = max(remaining_skip - file_size, 0)
            if file_offset == file_size:
                continue
            with source.open("rb") as handle:
                handle.seek(file_offset)
                file_offset = _hash_file_from_offset(
                    handle,
                    file_offset,
                    file_size,
                    piece_hasher,
                    progress,
                    source,
                )
        piece_hasher.finish()
        pieces = checkpoint.read_hashes()
    except MemoryError as exc:
        progress.interrupt()
        checkpoint.close()
        current_path = source if "source" in locals() else root
        raise RuntimeError(
            f"制种内存不足：{current_path}（全局进度 {progress.completed_bytes:,}/{total_size:,} 字节）；"
            "已保存的续传检查点仍可继续"
        ) from exc
    except OSError as exc:
        progress.interrupt()
        checkpoint.close()
        current_path = source if "source" in locals() else root
        current_size = file_size if "file_size" in locals() else 0
        current_offset = file_offset if "file_offset" in locals() else 0
        raise RuntimeError(
            f"制种读取失败：{current_path}（文件内偏移 {current_offset:,}/{current_size:,} 字节，"
            f"全局进度 {progress.completed_bytes:,}/{total_size:,} 字节）：{exc}"
        ) from exc
    except BaseException:
        progress.interrupt()
        checkpoint.close()
        raise
    progress.finish()
    info = {
        "files": file_entries,
        "name": logical_root_name,
        "piece length": piece_length,
        "pieces": bytes(pieces),
        "private": 1,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_bytes(_bencode({"info": info}))
    temporary.replace(output)
    checkpoint.cleanup()
    return piece_length


def read_mediainfo_text(path: Path, display_name: str) -> str:
    result = subprocess.run(
        [_find_mediainfo(), str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0 or not result.stdout.strip():
        detail = result.stderr.strip() or "MediaInfo 没有返回 Text 信息"
        raise RuntimeError(f"读取 MediaInfo Text 失败：{detail}")
    text = re.sub(r"^(Complete name\s*:).*$", rf"\1 {display_name}", result.stdout, flags=re.I | re.M)
    return text.strip() + "\n"


def read_bdinfo_report(path: Path) -> str:
    """Read and validate a classic BDInfo text report."""
    if not path.is_file():
        raise FileNotFoundError(f"找不到 BDInfo 报告：{path}")
    payload = path.read_bytes()
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = payload.decode("cp1252", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    required = ("DISC INFO:", "PLAYLIST REPORT:", "VIDEO:", "AUDIO:")
    missing = [label for label in required if label not in text.upper()]
    if missing:
        raise ValueError("不是完整的 BDInfo Text 报告，缺少：" + "、".join(missing))
    return text.strip() + "\n"


def _normalise_bdinfo_playlist(value: str) -> str:
    match = re.fullmatch(r"\s*(\d{1,5})(?:\.MPLS)?\s*", value, re.I)
    if not match:
        raise ValueError("BDInfo 播放列表应类似 00005 或 00005.MPLS")
    return f"{int(match.group(1)):05d}"


def select_longest_bdinfo_playlist(output: str) -> str:
    """Return the longest playlist name from ``bdinfo-rs --list`` output."""
    candidates: list[tuple[int, str]] = []
    pattern = re.compile(
        r"^\s*\d+\s+\d+\s+(\d{5})\.MPLS\s+(\d{1,3}):(\d{2}):(\d{2})(?:\.\d+)?",
        re.I | re.M,
    )
    for match in pattern.finditer(output):
        hours, minutes, seconds = (int(value) for value in match.groups()[1:])
        candidates.append((hours * 3600 + minutes * 60 + seconds, match.group(1)))
    if not candidates:
        raise RuntimeError(
            "无法从 BDInfo CLI 列表中自动判断主播放列表；请添加 --bdinfo-playlist 00000"
        )
    return max(candidates)[1]


def _find_bdinfo_cli(explicit: str | None = None) -> str:
    requested = explicit or os.environ.get("BDINFO_PATH", "")
    if requested:
        candidate = Path(requested).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        found = shutil.which(requested)
        if found:
            return found
        raise FileNotFoundError(f"找不到 BDInfo CLI：{requested}")
    found = shutil.which("bdinfo-rs") or shutil.which("bdinfo-rs.exe")
    if found:
        return found
    install_hint = (
        "请使用 bdinfo-rs 官方安装脚本安装，或设置 BDINFO_PATH；"
        if sys.platform.startswith("linux")
        else "请先运行 `winget install agentjp.bdinfo-rs`，"
    )
    raise FileNotFoundError(
        "Blu-ray ISO 必须使用 BDInfo：" + install_hint
        + "或用 --bdinfo-report 指定已由图形版 BDInfo 保存的 Text 报告"
    )


def _bdinfo_cli_kind(executable: str) -> str:
    name = Path(executable).stem.casefold()
    if "bdinfo-rs" in name:
        return "rs"
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "指定的 BDInfo.exe 看起来是图形版，不能静默生成报告；"
            "请改用 bdinfo-rs，或通过 --bdinfo-report 传入图形版保存的报告"
        ) from exc
    version_text = (result.stdout + result.stderr).casefold()
    if "bdinfo-rs" in version_text:
        return "rs"
    return "classic"


def _bdinfo_list_command(cli: str, kind: str, disc: Path, output_dir: Path) -> list[str]:
    if kind == "rs":
        # bdinfo-rs 4.x requires REPORT_DEST even for --list when BD_PATH is
        # an ISO image. Supplying it for folders too keeps one stable command.
        return [cli, "--list", str(disc), str(output_dir)]
    return [cli, "--list", str(disc)]


def _bdinfo_scan_command(
    cli: str,
    kind: str,
    disc: Path,
    output_dir: Path,
    selected: str,
) -> list[str]:
    if kind == "rs":
        return [cli, "--mpls", selected, str(disc), str(output_dir)]
    return [cli, "--mpls", f"{selected}.MPLS", str(disc), str(output_dir)]


def generate_bdinfo_report(
    disc: Path,
    output_dir: Path,
    *,
    executable: str | None = None,
    playlist: str | None = None,
) -> tuple[str, Path, str]:
    """Generate a BDInfo report for one Blu-ray ISO and return text/path/MPLS."""
    cli = _find_bdinfo_cli(executable)
    kind = _bdinfo_cli_kind(cli)
    selected = _normalise_bdinfo_playlist(playlist) if playlist else ""
    output_dir.mkdir(parents=True, exist_ok=True)
    if not selected:
        list_command = _bdinfo_list_command(cli, kind, disc, output_dir)
        listing = subprocess.run(
            list_command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if listing.returncode != 0:
            detail = listing.stderr.strip() or listing.stdout.strip() or f"退出码 {listing.returncode}"
            raise RuntimeError(f"BDInfo 播放列表扫描失败：{detail}")
        selected = select_longest_bdinfo_playlist(listing.stdout)
    command = _bdinfo_scan_command(cli, kind, disc, output_dir, selected)
    print(f"正在生成 BDInfo：主播放列表 {selected}.MPLS；完整扫描可能需要较长时间……")
    result = subprocess.run(command)
    if result.returncode not in {0, 3}:
        raise RuntimeError(f"BDInfo 扫描失败，退出码 {result.returncode}")
    reports = sorted(
        (
            item
            for item in output_dir.glob("*.txt")
            if item.is_file() and item.name.casefold().startswith("bdinfo")
        ),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    if not reports:
        raise RuntimeError(f"BDInfo CLI 已结束，但输出目录中没有找到报告：{output_dir}")
    report_path = reports[0]
    return read_bdinfo_report(report_path), report_path, selected


def prepare_technical_info(
    path: Path,
    display_name: str,
    source: str,
    output_dir: Path,
    *,
    bdinfo_report: Path | None = None,
    bdinfo_exe: str | None = None,
    bdinfo_playlist: str | None = None,
) -> tuple[str, str, Path, str]:
    """Choose MediaInfo for files/DVD and BDInfo for Blu-ray ISO."""
    output_dir.mkdir(parents=True, exist_ok=True)
    bluray_iso = path.suffix.casefold() == ".iso" and "BLURAY" in source.replace(" ", "").upper()
    if not bluray_iso:
        if bdinfo_report or bdinfo_exe or bdinfo_playlist:
            raise ValueError("--bdinfo-* 参数只适用于 Blu-ray/UHD ISO")
        media_text_path = output_dir / "mediainfo.txt"
        media_text = read_mediainfo_text(path, display_name)
        media_text_path.write_text(media_text, encoding="utf-8")
        return "MediaInfo", media_text, media_text_path, ""

    if bdinfo_report:
        bdinfo_text = read_bdinfo_report(bdinfo_report.resolve())
        bdinfo_path = output_dir / "bdinfo.txt"
        bdinfo_path.write_text(bdinfo_text, encoding="utf-8")
        playlist_match = re.search(r"(?:PLAYLIST:|Name:)\s*(\d{5})\.MPLS", bdinfo_text, re.I)
        return "BDInfo", bdinfo_text, bdinfo_path, playlist_match.group(1) if playlist_match else ""

    bdinfo_text, bdinfo_path, selected = generate_bdinfo_report(
        path,
        output_dir,
        executable=bdinfo_exe,
        playlist=bdinfo_playlist,
    )
    return "BDInfo", bdinfo_text, bdinfo_path, selected


def _read_initial_iso_media(
    args: argparse.Namespace,
    path: Path,
) -> tuple[MediaInfo, tuple[str, str] | None]:
    """Read a Blu-ray ISO through BDInfo when MediaInfo has no video track.

    Some authored Blu-ray images expose only a General track to MediaInfo and
    their release names do not include a codec token.  Do not guess AVC/HEVC
    from the filename; use the actual BDInfo report and retain it for the
    later publication-info step so the disc is scanned only once.
    """
    hints = filename_hints(path)
    source_hint = None if args.source == "auto" else _canonical_source(args.source)
    source_hint = _canonical_source(source_hint or hints.source) or ""
    if "BLURAY" not in source_hint.replace(" ", "").upper():
        raise ValueError("ISO 中未读到视频轨，文件名也缺少分辨率或视频编码")

    if args.bdinfo_report:
        bdinfo_text = read_bdinfo_report(args.bdinfo_report.resolve())
        playlist_match = re.search(r"(?:PLAYLIST:|Name:)\s*(\d{5})\.MPLS", bdinfo_text, re.I)
        selected = playlist_match.group(1) if playlist_match else ""
    else:
        with tempfile.TemporaryDirectory(prefix="mteam-post-bdinfo-") as temporary_directory:
            bdinfo_text, _report_path, selected = generate_bdinfo_report(
                path,
                Path(temporary_directory),
                executable=args.bdinfo_exe,
                playlist=args.bdinfo_playlist,
            )
    return _media_from_bdinfo(bdinfo_text, source_hint), (bdinfo_text, selected)


def _extract_screenshots(video: Path, output: Path, count: int) -> list[Path]:
    from random_video_screenshots.cli import extract_screenshots

    before = {
        item.resolve(): (item.stat().st_mtime_ns, item.stat().st_size)
        for item in output.glob("*")
        if item.is_file()
    }
    extract_screenshots(video, output, count=count)
    generated: list[Path] = []
    for item in output.glob("*"):
        if not item.is_file():
            continue
        signature = (item.stat().st_mtime_ns, item.stat().st_size)
        if before.get(item.resolve()) != signature:
            generated.append(item)
    return sorted(generated)


def _mount_iso(path: Path) -> IsoMount:
    if sys.platform.startswith("linux"):
        errors: list[str] = []
        udisksctl = shutil.which("udisksctl")
        if udisksctl:
            environment = os.environ.copy()
            environment["LC_ALL"] = "C"
            loop_result = subprocess.run(
                [
                    udisksctl,
                    "loop-setup",
                    "--read-only",
                    "--no-user-interaction",
                    "--file",
                    str(path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
            )
            loop_output = loop_result.stdout + loop_result.stderr
            device_match = re.search(r"/dev/loop\d+", loop_output)
            if loop_result.returncode == 0 and device_match:
                device = device_match.group(0)
                mount_result = subprocess.run(
                    [
                        udisksctl,
                        "mount",
                        "--no-user-interaction",
                        "--block-device",
                        str(device),
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=environment,
                )
                if mount_result.returncode == 0:
                    mount_root = ""
                    findmnt = shutil.which("findmnt")
                    if findmnt:
                        find_result = subprocess.run(
                            [findmnt, "--noheadings", "--raw", "--source", str(device), "--output", "TARGET"],
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            env=environment,
                        )
                        if find_result.returncode == 0:
                            mount_root = find_result.stdout.strip().splitlines()[0] if find_result.stdout.strip() else ""
                    if not mount_root:
                        root_match = re.search(r"\bat (.+?)\.?\s*$", mount_result.stdout, re.M)
                        mount_root = root_match.group(1) if root_match else ""
                    if mount_root and Path(mount_root).is_dir():
                        return IsoMount(Path(mount_root), "udisks", device)
                    errors.append("udisksctl 已挂载镜像，但无法确定挂载目录")
                    subprocess.run(
                        [udisksctl, "unmount", "--no-user-interaction", "--block-device", str(device)],
                        capture_output=True,
                        env=environment,
                    )
                else:
                    errors.append(
                        "udisksctl mount: "
                        + (mount_result.stderr.strip() or mount_result.stdout.strip() or f"退出码 {mount_result.returncode}")
                    )
                subprocess.run(
                    [udisksctl, "loop-delete", "--no-user-interaction", "--block-device", str(device)],
                    capture_output=True,
                    env=environment,
                )
            else:
                errors.append(
                    "udisksctl loop-setup: "
                    + (loop_result.stderr.strip() or loop_result.stdout.strip() or f"退出码 {loop_result.returncode}")
                )

        sudo = shutil.which("sudo")
        mount_executable = shutil.which("mount")
        if sudo and mount_executable:
            mount_root = Path(tempfile.mkdtemp(prefix="mteam-post-iso-"))
            result = subprocess.run(
                [
                    sudo,
                    "-n",
                    mount_executable,
                    "-o",
                    "loop,ro,nosuid,nodev,noexec",
                    str(path),
                    str(mount_root),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode == 0:
                return IsoMount(mount_root, "sudo", mount_root)
            shutil.rmtree(mount_root, ignore_errors=True)
            errors.append(
                "sudo mount: "
                + (result.stderr.strip() or result.stdout.strip() or f"退出码 {result.returncode}")
            )
        fuseiso = shutil.which("fuseiso")
        if fuseiso:
            mount_root = Path(tempfile.mkdtemp(prefix="mteam-post-iso-"))
            result = subprocess.run(
                [fuseiso, str(path), str(mount_root)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode == 0:
                return IsoMount(mount_root, "fuseiso", mount_root)
            shutil.rmtree(mount_root, ignore_errors=True)
            errors.append(
                "fuseiso: "
                + (result.stderr.strip() or result.stdout.strip() or f"退出码 {result.returncode}")
            )
        detail = "；".join(errors)
        raise RuntimeError(
            "Ubuntu 无法自动挂载 ISO。请安装 udisks2（推荐，支持 Blu-ray UDF）"
            "或 fuseiso；纯 SSH 会话推荐配置免密码 sudo，"
            "也可用 --screenshot-source 指定已挂载的视频文件"
            + (f"。详情：{detail}" if detail else "")
        )
    if sys.platform != "win32":
        raise RuntimeError(
            "ISO 自动挂载目前支持 Windows 和 Linux；"
            "可用 --screenshot-source 指定已挂载的视频文件"
        )
    script = r"""
$p=$env:MEDIA_TITLE_ISO_PATH
$image=Get-DiskImage -ImagePath $p -ErrorAction SilentlyContinue
$already=[bool]($image -and $image.Attached)
if(-not $already){$image=Mount-DiskImage -ImagePath $p -PassThru -ErrorAction Stop}
$volume=$image | Get-Volume | Where-Object DriveLetter | Select-Object -First 1
if(-not $volume){throw '挂载成功但没有找到盘符'}
[pscustomobject]@{root=($volume.DriveLetter + ':\'); mountedByUs=(-not $already)} | ConvertTo-Json -Compress
"""
    environment = os.environ.copy()
    environment["MEDIA_TITLE_ISO_PATH"] = str(path)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
    )
    if result.returncode != 0:
        raise RuntimeError("无法挂载 ISO：" + (result.stderr.strip() or result.stdout.strip()))
    data = json.loads(result.stdout)
    return IsoMount(
        Path(data["root"]),
        "windows" if bool(data["mountedByUs"]) else "",
        path if bool(data["mountedByUs"]) else None,
    )


def _unmount_iso(mount: IsoMount) -> None:
    if mount.cleanup_method == "windows" and mount.cleanup_target:
        environment = os.environ.copy()
        environment["MEDIA_TITLE_ISO_PATH"] = str(mount.cleanup_target)
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Dismount-DiskImage -ImagePath $env:MEDIA_TITLE_ISO_PATH -ErrorAction SilentlyContinue",
            ],
            capture_output=True,
            env=environment,
        )
        return
    if mount.cleanup_method == "fuseiso" and mount.cleanup_target:
        unmounter = shutil.which("fusermount3") or shutil.which("fusermount")
        if not unmounter:
            raise RuntimeError(
                f"找不到 fusermount，无法卸载临时 ISO：{mount.cleanup_target}"
            )
        result = subprocess.run(
            [unmounter, "-u", str(mount.cleanup_target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise RuntimeError(
                "无法卸载临时 ISO："
                + (result.stderr.strip() or result.stdout.strip() or f"退出码 {result.returncode}")
            )
        shutil.rmtree(mount.cleanup_target, ignore_errors=True)
        return
    if mount.cleanup_method == "udisks" and mount.cleanup_target:
        udisksctl = shutil.which("udisksctl")
        if not udisksctl:
            raise RuntimeError(
                f"找不到 udisksctl，无法卸载临时 ISO：{mount.cleanup_target}"
            )
        environment = os.environ.copy()
        environment["LC_ALL"] = "C"
        unmount_result = subprocess.run(
            [
                udisksctl,
                "unmount",
                "--no-user-interaction",
                "--block-device",
                str(mount.cleanup_target),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
        delete_result = subprocess.run(
            [
                udisksctl,
                "loop-delete",
                "--no-user-interaction",
                "--block-device",
                str(mount.cleanup_target),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
        if unmount_result.returncode != 0 or delete_result.returncode != 0:
            detail = (
                unmount_result.stderr.strip()
                or delete_result.stderr.strip()
                or unmount_result.stdout.strip()
                or delete_result.stdout.strip()
            )
            raise RuntimeError("无法卸载临时 ISO：" + (detail or "udisksctl 执行失败"))
        return
    if mount.cleanup_method == "sudo" and mount.cleanup_target:
        sudo = shutil.which("sudo")
        umount = shutil.which("umount")
        if not sudo or not umount:
            raise RuntimeError(
                f"找不到 sudo/umount，无法卸载临时 ISO：{mount.cleanup_target}"
            )
        result = subprocess.run(
            [sudo, "-n", umount, str(mount.cleanup_target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise RuntimeError(
                "无法卸载临时 ISO："
                + (result.stderr.strip() or result.stdout.strip() or f"退出码 {result.returncode}")
            )
        shutil.rmtree(mount.cleanup_target, ignore_errors=True)


@contextmanager
def screenshot_source(path: Path, override: Path | None = None) -> Iterator[Path]:
    if override:
        if not override.is_file():
            raise FileNotFoundError(f"找不到截图源：{override}")
        yield override
        return
    if path.suffix.lower() != ".iso":
        yield path
        return
    mount = _mount_iso(path)
    try:
        candidates = list((mount.root / "BDMV" / "STREAM").glob("*.m2ts"))
        candidates += list((mount.root / "VIDEO_TS").glob("VTS_*_[1-9].VOB"))
        if not candidates:
            raise RuntimeError("ISO 中没有找到可用于截图的 M2TS/VOB；请用 --screenshot-source 指定视频文件")
        yield max(candidates, key=lambda item: item.stat().st_size)
    finally:
        if mount.cleanup_method:
            _unmount_iso(mount)


def _manual_douban(url: str) -> DoubanMatch:
    match = re.search(r"/subject/(\d+)", url)
    item_id = match.group(1) if match else ""
    canonical = f"https://movie.douban.com/subject/{item_id}/" if item_id else url
    return DoubanMatch(id=item_id, url=canonical, title="", original_title="", year="", score=100.0)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成 M-Team 发布资料包、V1 私有种子、MediaInfo/BDInfo Text 和 4 张截图")
    parser.add_argument("input", type=Path, help="单个视频/ISO，或剧集所在文件夹")
    parser.add_argument("--apply", action="store_true", help="资料准备成功后执行规范重命名")
    parser.add_argument("--recursive", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path, help="资料包输出目录；默认在媒体旁创建 .prepare 文件夹")
    parser.add_argument("--title", help="手工指定主标题，并优先于 TMDB")
    parser.add_argument("--year", help="四位年份")
    parser.add_argument("--source", default="auto", help="来源，如 HDTV、WEB-DL、BluRay、BluRay REMUX")
    parser.add_argument("--group", help="发布组")
    parser.add_argument("--edition", help="原盘版本，如 MOC")
    parser.add_argument("--platform", help="WEB-DL 平台")
    parser.add_argument("--kind", choices=("auto", "movie", "tv"), default="auto", help="电影或剧集")
    parser.add_argument("--episode", help="单文件的季集，例如 S01E01；文件夹模式从每个文件名识别")
    parser.add_argument("--audio-count", action="store_true", help="在标题添加 2Audio、3Audio；默认不添加")
    parser.add_argument("--tmdb-id", type=int, help="手工指定 TMDB ID")
    parser.add_argument("--douban-url", help="手工指定豆瓣链接，并跳过自动查找")
    parser.add_argument("--offline", action="store_true", help="不访问 TMDB 和豆瓣；通常需同时给 --douban-url")
    parser.add_argument("--bdinfo-report", type=Path, help="Blu-ray ISO 已有的 BDInfo Text 报告")
    parser.add_argument("--bdinfo-exe", help="BDInfo CLI 路径；默认查找 bdinfo-rs 或读取 BDINFO_PATH")
    parser.add_argument("--bdinfo-playlist", help="Blu-ray 主播放列表，如 00005；默认自动选择最长项")
    parser.add_argument("--category", choices=MTEAM_CATEGORIES, help="覆盖自动推断的 M-Team 分类")
    parser.add_argument("--animation", action="store_true", help="按动画分类；有 TMDB 时也会自动识别动画类型")
    parser.add_argument("--screenshots", type=int, default=4, help="本地截图数量，默认 4")
    parser.add_argument("--screenshot-source", type=Path, help="ISO 截图时手工指定已挂载的 M2TS/VOB")
    parser.add_argument("--skip-screenshots", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--skip-torrent", action="store_true", help=argparse.SUPPRESS)
    return parser


@dataclass(frozen=True)
class FolderPlan:
    source_path: Path
    target_path: Path
    logical_path: Path
    episode: str
    source: str
    group: str | None
    platform: str | None
    media: MediaInfo


_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "兩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def _number_token(value: str) -> int | None:
    value = value.strip()
    if value.isdigit():
        return int(value)
    if value == "十":
        return 10
    if "十" in value:
        tens, ones = value.split("十", 1)
        tens_value = _CHINESE_DIGITS.get(tens, 1) if tens else 1
        ones_value = _CHINESE_DIGITS.get(ones, 0) if ones else 0
        return tens_value * 10 + ones_value
    if value and all(character in _CHINESE_DIGITS for character in value):
        result = 0
        for character in value:
            result = result * 10 + _CHINESE_DIGITS[character]
        return result
    return None


def _season_number(value: str) -> int | None:
    chinese = re.search(r"第\s*([0-9零〇一二两兩三四五六七八九十]+)\s*季", value, re.I)
    if chinese:
        return _number_token(chinese.group(1))
    western = re.search(r"(?:^|[^A-Z0-9])(?:SEASON\s*|S)0*(\d{1,2})(?!\d)", value, re.I)
    return int(western.group(1)) if western else None


def _disc_number(value: str) -> int | None:
    normalized = re.search(r"\bS\d{1,2}D0*(\d{1,2})(?!\d)", value, re.I)
    if normalized:
        return int(normalized.group(1))
    chinese = re.search(
        r"第\s*([0-9零〇一二两兩三四五六七八九十]+)\s*(?:碟|盘|盤|张|張)",
        value,
        re.I,
    )
    if chinese:
        return _number_token(chinese.group(1))
    western = re.search(
        r"(?:^|[^A-Z0-9])(?:DISC|DISK|VOL(?:UME)?|D)[ ._-]*0*(\d{1,2})(?!\d)",
        value,
        re.I,
    )
    return int(western.group(1)) if western else None


def _disc_episode(path: Path, root: Path) -> str | None:
    if path.suffix.casefold() != ".iso":
        return None
    disc = _disc_number(path.stem)
    if disc is None:
        return None
    try:
        relative = path.relative_to(root)
        context = " ".join((root.name, *relative.parts[:-1], path.stem))
    except ValueError:
        context = f"{root.name} {path.stem}"
    season = _season_number(context)
    if season is None:
        return None
    return f"S{season:02d}D{disc:02d}"


def _bracket_release_group(path: Path) -> str | None:
    """Return a likely release group such as ``TTG`` from ``[TTG]``."""
    for value in reversed(re.findall(r"\[([^\[\]]+)\]", path.stem)):
        candidate = value.strip()
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9._@-]{1,30}", candidate):
            if not re.fullmatch(r"(?:DISC|DISK|SEASON|S|D)\d*", candidate, re.I):
                if _is_release_prefix_label(candidate) and re.match(
                    rf"^\s*\[{re.escape(candidate)}\]", path.stem, re.I
                ):
                    continue
                return candidate
    return None


def _embedded_tmdb_id(path: Path) -> int | None:
    for part in reversed(path.parts):
        match = re.search(r"(?:\[|\{|\()?\s*tmdb\s*=\s*(\d+)", part, re.I)
        if match:
            return int(match.group(1))
    return None


def _resume_tmdb_id_for_folder(root: Path, source_paths: list[Path]) -> int | None:
    """Recover a prior TMDB id when a long resume follows a network outage."""
    wanted = {os.path.normcase(str(path.resolve())) for path in source_paths}
    if not wanted:
        return None
    for candidate in root.parent.glob("*.prepare/*.torrent.resume.json"):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        entries = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            continue
        recorded = {
            os.path.normcase(str(Path(item["source_path"]).resolve()))
            for item in entries
            if isinstance(item, dict) and isinstance(item.get("source_path"), str)
        }
        if recorded != wanted:
            continue
        logical_root = payload.get("logical_root_name")
        if not isinstance(logical_root, str):
            continue
        match = re.search(r"-\[tmdb=(\d+)\]$", logical_root, re.I)
        if match:
            return int(match.group(1))
    return None


def _media_from_bdinfo(text: str, source: str) -> MediaInfo:
    """Extract title fields from a classic BDInfo report for a disc set."""
    video_match = re.search(r"\b(4320|2160|1080|720|576|480)([pi])\b", text, re.I)
    if not video_match:
        raise ValueError("BDInfo 报告中没有找到视频分辨率")
    height = int(video_match.group(1))
    scan = video_match.group(2).lower()
    widths = {4320: 7680, 2160: 3840, 1080: 1920, 720: 1280, 576: 720, 480: 720}

    video_section_match = re.search(r"\bVIDEO:\s*(.*?)(?:\n\s*AUDIO:|\Z)", text, re.I | re.S)
    video_section = video_section_match.group(1) if video_section_match else text
    upper_video = video_section.upper()
    if "HEVC" in upper_video or "H.265" in upper_video:
        video_format = "HEVC"
    elif "AVC" in upper_video or "H.264" in upper_video:
        video_format = "AVC"
    elif "VC-1" in upper_video or "VC1" in upper_video:
        video_format = "VC-1"
    elif "MPEG-2" in upper_video or "MPEG2" in upper_video:
        video_format = "MPEG-2"
    else:
        raise ValueError("BDInfo 报告中没有找到受支持的视频编码")

    hdr: list[str] = []
    if "HDR10+" in upper_video:
        hdr.append("HDR10+")
    elif "HDR10" in upper_video or "PQ" in upper_video:
        hdr.append("HDR10")
    if "DOLBY VISION" in upper_video or "DOVI" in upper_video:
        hdr.append("DoVi")

    fps_match = re.search(r"(\d+(?:\.\d+)?)\s*fps", video_section, re.I)
    frame_rate = float(fps_match.group(1)) if fps_match else 0.0

    audio_section_match = re.search(
        r"\bAUDIO:\s*(.*?)(?:\n\s*(?:SUBTITLES|FILES|CHAPTERS|STREAM DIAGNOSTICS):|\Z)",
        text,
        re.I | re.S,
    )
    audio_section = audio_section_match.group(1) if audio_section_match else ""
    audio_rows: list[tuple[int, str, str | None, str]] = []
    codec_patterns = (
        (r"DTS-HD\s+Master|DTS-HD\s+MA", "DTS-HD MA"),
        (r"DTS-HD\s+High\s+Resolution|DTS-HD\s+HRA", "DTS-HD HRA"),
        (r"DTS:X|DTS-X", "DTS-X"),
        (r"Dolby\s+TrueHD(?:/Atmos)?", "TrueHD Atmos"),
        (r"Dolby\s+Digital\s+Plus(?:/Atmos)?", "DDP Atmos"),
        (r"Dolby\s+Digital", "DD"),
        (r"(?:Linear\s+PCM|LPCM)", "LPCM"),
        (r"\bDTS\b", "DTS"),
    )
    for line in audio_section.splitlines():
        codec = next((label for pattern, label in codec_patterns if re.search(pattern, line, re.I)), None)
        if not codec:
            continue
        if "ATMOS" not in line.upper():
            codec = codec.replace(" Atmos", "")
        bitrate_match = re.search(r"([\d, ]+)\s*kbps", line, re.I)
        bitrate = int(re.sub(r"\D", "", bitrate_match.group(1))) * 1000 if bitrate_match else 0
        channels_match = re.search(r"\b(\d{1,2}\.\d)\b", line)
        language_match = re.search(
            r"\b(English|Chinese|Mandarin|Cantonese|French|German|Italian|Japanese|Korean|Spanish|Russian)\b",
            line,
            re.I,
        )
        audio_rows.append(
            (
                bitrate,
                codec,
                channels_match.group(1) if channels_match else None,
                language_match.group(1).lower() if language_match else "",
            )
        )
    primary = max(audio_rows, key=lambda item: item[0]) if audio_rows else (0, "", None, "")

    return MediaInfo(
        width=widths[height],
        height=height,
        resolution=f"{height}{scan}",
        video_format=video_format,
        writing_library="",
        video_codec=_video_codec(video_format, "", source),
        hdr=tuple(hdr),
        hfr=f"{round(frame_rate):.0f}Fps" if frame_rate >= 50 else None,
        audio_codec=primary[1],
        audio_channels=primary[2],
        audio_tracks=len(audio_rows),
        audio_bitrate=primary[0],
        scan_type="Interlaced" if scan == "i" else "Progressive",
        scan_order="",
        audio_language=primary[3],
    )


def _episode_sort_key(value: str) -> tuple[int, int, str]:
    match = re.match(r"S(\d{1,2})(?:[ED](\d{1,3}))?", value, re.I)
    if not match:
        return (999, 9999, value.casefold())
    return (int(match.group(1)), int(match.group(2) or 0), value.casefold())


def _season_label(episodes: list[str]) -> str:
    seasons = sorted(
        {int(match.group(1)) for value in episodes if (match := re.match(r"S(\d{1,2})", value, re.I))}
    )
    if not seasons:
        raise ValueError("无法从文件名识别季数")
    if len(seasons) == 1:
        return f"S{seasons[0]:02d}"
    return f"S{seasons[0]:02d}-S{seasons[-1]:02d}"


def _season_folder(episode: str) -> str:
    match = re.match(r"S(\d{1,2})", episode, re.I)
    if not match:
        raise ValueError(f"无法从季集号识别分季目录：{episode}")
    return f"Season {int(match.group(1)):02d}"


def _series_folder_name(title: str, year: str | None, tmdb_id: int | None) -> str:
    """Return the M-Team TV root-folder name from the site naming template."""
    component = re.sub(r"[<>:\"/\\|?*]", " ", title or "")
    component = re.sub(r"\s+", " ", component).strip(" .")
    if not component:
        raise ValueError("无法按剧集文件夹规则生成目录名：缺少剧名；请传 --title")
    clean_year = str(year or "").strip()
    if not re.fullmatch(r"(?:19|20)\d{2}", clean_year):
        raise ValueError("无法按剧集文件夹规则生成目录名：缺少四位年份；请传 --year")
    result = f"{component}-{clean_year}"
    if tmdb_id:
        result += f"-[tmdb={int(tmdb_id)}]"
    return result


def _folder_videos(root: Path) -> list[Path]:
    iterator = root.rglob("*")
    return sorted(
        (item for item in iterator if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS),
        key=lambda item: str(item).casefold(),
    )


def _tmdb_for_release(
    args: argparse.Namespace,
    *,
    kind: str,
    base_title: str,
    year: str | None,
) -> TmdbMatch | None:
    tmdb_client = TmdbClient(
        read_token=os.environ.get("TMDB_READ_ACCESS_TOKEN", ""),
        api_key=os.environ.get("TMDB_API_KEY", ""),
    )
    if args.offline:
        return None
    if not tmdb_client.available:
        print("提示：未配置 TMDB_READ_ACCESS_TOKEN，暂用文件名和豆瓣识别；配置后可提高片名准确率。")
        return None
    try:
        if args.tmdb_id:
            return tmdb_client.by_id(kind, args.tmdb_id)
        return _choose_tmdb(tmdb_client.search(kind, base_title, year))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, KeyError, ValueError) as exc:
        print(f"警告：TMDB 查询失败，将使用文件名结果：{exc}")
        return None


def _season_number_from_episode(value: str | None) -> int | None:
    if not value:
        return None
    match = re.match(r"S(\d{1,2})(?:E|D)", value, re.I)
    return int(match.group(1)) if match else None


def _tmdb_season_for_release(
    args: argparse.Namespace,
    tmdb: TmdbMatch | None,
    season_number: int | None,
) -> TmdbSeason | None:
    if args.offline or not tmdb or tmdb.media_type != "tv" or season_number is None:
        return None
    client = TmdbClient(
        read_token=os.environ.get("TMDB_READ_ACCESS_TOKEN", ""),
        api_key=os.environ.get("TMDB_API_KEY", ""),
    )
    if not client.available:
        return None
    try:
        return client.season(tmdb.id, season_number)
    except (OSError, urllib.error.URLError, json.JSONDecodeError, KeyError, ValueError) as exc:
        print(f"警告：TMDB 第 {season_number} 季查询失败，将使用通用季数检索：{exc}")
        return None


def _douban_for_release(
    args: argparse.Namespace,
    *,
    tmdb: TmdbMatch | None,
    title: str,
    base_title: str,
    year: str | None,
    season_number: int | None = None,
) -> DoubanMatch | None:
    douban = _manual_douban(args.douban_url) if args.douban_url else None
    if not douban and not args.offline:
        season = _tmdb_season_for_release(args, tmdb, season_number)
        search_year = (season.year if season and season.year else year)
        search_names = [
            tmdb.original_name if tmdb else "",
            tmdb.name if tmdb else "",
            title,
            tmdb.chinese_name if tmdb else "",
            base_title,
        ]
        if season_number is not None:
            # Prefer the TMDB season's subtitle (e.g. ``Pearl Islands``),
            # while retaining generic ``Season N``/``第N季`` fallbacks.
            season_names: list[str] = []
            for name in search_names:
                if not name:
                    continue
                if season and season.name:
                    season_names.append(f"{name} {season.name}")
                season_names.append(f"{name} Season {season_number}")
                if _contains_cjk(name):
                    season_names.append(f"{name} 第{season_number}季")
            search_names = season_names + search_names
        deduplicated_names: list[str] = []
        seen_names: set[str] = set()
        for name in search_names:
            key = name.casefold().strip()
            if key and key not in seen_names:
                seen_names.add(key)
                deduplicated_names.append(name.strip())
        search_names = deduplicated_names
        douban = _choose_douban(
            _douban_candidates(search_names, search_year, expected_season=season_number),
            expected_season=season_number,
        )
        if season_number is not None and not douban:
            print(f"提示：未找到与第 {season_number} 季精确匹配的豆瓣条目，已留空以避免误填其他季。")
    if not douban and not args.offline and sys.stdin.isatty():
        try:
            manual = input("未自动找到可靠豆瓣条目，可粘贴豆瓣链接或直接回车跳过：").strip()
        except EOFError:
            manual = ""
        if manual:
            douban = _manual_douban(manual)
    return douban


def _folder_screenshots(
    plans: list[FolderPlan],
    output: Path,
    count: int,
    override: Path | None,
    *,
    first_only: bool = False,
) -> list[Path]:
    if count <= 0:
        return []
    if override:
        with screenshot_source(plans[0].source_path, override) as source_for_screenshots:
            return _extract_screenshots(source_for_screenshots, output, count=count)
    if first_only:
        with screenshot_source(plans[0].source_path) as source_for_screenshots:
            return _extract_screenshots(source_for_screenshots, output, count=count)

    selected_count = min(count, len(plans))
    if selected_count == 1:
        selected = [plans[0]]
    else:
        indexes = [round(index * (len(plans) - 1) / (selected_count - 1)) for index in range(selected_count)]
        selected = [plans[index] for index in indexes]
    base_count, remainder = divmod(count, len(selected))
    generated: list[Path] = []
    for index, plan in enumerate(selected):
        item_count = base_count + (1 if index < remainder else 0)
        with screenshot_source(plan.source_path) as source_for_screenshots:
            generated.extend(_extract_screenshots(source_for_screenshots, output, count=item_count))
    return sorted(generated)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _apply_folder_renames(
    plans: list[FolderPlan],
    *,
    root: Path | None = None,
    target_root: Path | None = None,
) -> None:
    """Move episode files and, last, the TV root folder as one rollback unit."""
    completed: list[tuple[Path, Path]] = []
    created_directories: list[Path] = []
    root_renamed = False
    try:
        for plan in plans:
            if plan.source_path == plan.target_path:
                continue
            if not plan.target_path.parent.exists():
                plan.target_path.parent.mkdir(parents=True, exist_ok=True)
                created_directories.append(plan.target_path.parent)
            plan.source_path.rename(plan.target_path)
            completed.append((plan.source_path, plan.target_path))
        if root is not None and target_root is not None and not _same_path(root, target_root):
            root.rename(target_root)
            root_renamed = True
    except OSError as exc:
        rollback_errors: list[str] = []
        if root_renamed and root is not None and target_root is not None:
            try:
                if target_root.exists() and not root.exists():
                    target_root.rename(root)
            except OSError as rollback_exc:
                rollback_errors.append(f"{target_root}: {rollback_exc}")
        for source, target in reversed(completed):
            try:
                if target.exists() and not source.exists():
                    target.rename(source)
            except OSError as rollback_exc:
                rollback_errors.append(f"{target}: {rollback_exc}")
        for directory in reversed(created_directories):
            try:
                directory.rmdir()
            except OSError:
                pass
        detail = f"；回滚也遇到问题：{' | '.join(rollback_errors)}" if rollback_errors else "；已回滚先前的改名"
        raise RuntimeError(f"批量改名失败：{exc}{detail}") from exc


def _prepare_folder(args: argparse.Namespace, root: Path) -> Path:
    if args.kind == "movie":
        raise ValueError("文件夹模式用于剧集，--kind 不能设为 movie")
    if args.episode:
        raise ValueError("文件夹模式会从每个文件名识别季集，请不要传 --episode")
    embedded_tmdb_id = _embedded_tmdb_id(root)
    if args.tmdb_id is None and embedded_tmdb_id is not None:
        args = argparse.Namespace(**vars(args))
        args.tmdb_id = embedded_tmdb_id
        print(f"已从目录名识别 TMDB ID：{embedded_tmdb_id}")
    paths = _folder_videos(root)
    if not paths:
        raise ValueError("目录中没有找到支持的视频文件")

    probes: list[tuple[Path, FilenameHints]] = []
    missing_episodes: list[Path] = []
    print(f"正在扫描剧集文件名：共 {len(paths)} 个")
    for path in paths:
        hints = filename_hints(path)
        disc_episode = _disc_episode(path, root)
        if disc_episode and not hints.episode:
            hints = replace(
                hints,
                episode=disc_episode,
                group=hints.group or _bracket_release_group(path),
            )
        probes.append((path, hints))
        if not hints.episode:
            missing_episodes.append(path)
    if missing_episodes:
        examples = "；".join(str(path.relative_to(root)) for path in missing_episodes[:8])
        extra = f"（另有 {len(missing_episodes) - 8} 个）" if len(missing_episodes) > 8 else ""
        raise ValueError(
            "以下文件无法识别 SxxExx 季集号，也无法识别 SxxDxx/第N季第N碟，"
            f"未改名：{examples}{extra}"
        )
    probes.sort(key=lambda item: (*_episode_sort_key(item[1].episode or ""), str(item[0]).casefold()))

    disc_collection = all(
        path.suffix.casefold() == ".iso" and re.fullmatch(r"S\d{2}D\d{2}", hints.episode or "", re.I)
        for path, hints in probes
    )
    first_path, first_hints = probes[0]
    cached_technical: tuple[str, str, str] | None = None
    if disc_collection:
        source_hint = None if args.source == "auto" else _canonical_source(args.source)
        source_hint = _canonical_source(source_hint or first_hints.source) or ""
        if source_hint not in {"BluRay", "UHD BluRay"}:
            raise ValueError("剧集 ISO 光盘无法判断为 Blu-ray；请传 --source BluRay 或 --source 'UHD BluRay'")
        print(
            f"正在探测代表光盘 BDInfo：{first_path.relative_to(root)}"
            f"（其余 {len(paths) - 1} 张光盘复用）"
        )
        with tempfile.TemporaryDirectory(prefix="mteam-post-bdinfo-") as temporary_directory:
            technical_type, technical_text, _temporary_path, selected_playlist = prepare_technical_info(
                first_path,
                first_path.name,
                source_hint,
                Path(temporary_directory),
                bdinfo_report=args.bdinfo_report,
                bdinfo_exe=args.bdinfo_exe,
                bdinfo_playlist=args.bdinfo_playlist,
            )
        first_media = _media_from_bdinfo(technical_text, source_hint)
        cached_technical = (technical_type, technical_text, selected_playlist)
    else:
        print(f"正在探测代表集 MediaInfo：{first_path.relative_to(root)}（其余 {len(paths) - 1} 集复用）")
        first_media = read_mediainfo(first_path)

    refreshed_hints = filename_hints(first_path, first_media)
    first_hints = replace(
        refreshed_hints,
        episode=first_hints.episode,
        group=first_hints.group or refreshed_hints.group,
    )
    probes[0] = (first_path, first_hints)
    shared_args = argparse.Namespace(**vars(args))
    shared_args.kind = "tv"
    shared_args.episode = first_hints.episode
    if shared_args.group is None:
        shared_args.group = first_hints.group
    base_title, year, common_source, common_group, edition, _episode, common_platform = _resolve_fields(
        shared_args, first_path, first_media
    )
    if not year:
        # The first sorted episode often has a scene-style folder name without
        # a year, while later episodes carry the year token.  Use the most
        # frequent four-digit year across the complete episode set so a
        # transient TMDB timeout does not make the folder rename impossible.
        years = [hints.year for _path, hints in probes if hints.year]
        if years:
            year = max(set(years), key=lambda value: (years.count(value), value))
    if args.tmdb_id is None:
        resume_tmdb_id = _resume_tmdb_id_for_folder(root, [path for path, _hints in probes])
        if resume_tmdb_id is not None:
            args = argparse.Namespace(**vars(args))
            args.tmdb_id = resume_tmdb_id
            print(f"已从现有制种检查点恢复 TMDB ID：{resume_tmdb_id}")
    tmdb = _tmdb_for_release(args, kind="tv", base_title=base_title, year=year)
    title = args.title or (tmdb.name if tmdb and tmdb.name else base_title)
    year = args.year or (tmdb.year if tmdb and tmdb.year else year)
    tmdb_id = tmdb.id if tmdb else args.tmdb_id
    series_folder_name = _series_folder_name(title, year, tmdb_id)
    target_root = root.with_name(series_folder_name)
    if not _same_path(root, target_root) and target_root.exists():
        raise FileExistsError(f"目标剧集目录已存在，未执行任何改名：{target_root}")
    if not _same_path(root, target_root):
        print(f"剧集目录预览：{root.name} → {target_root.name}")
    if not tmdb_id:
        print("提示：未获得 TMDB ID，剧集目录名将省略 [tmdb=...]；可传 --tmdb-id 补全。")

    provisional: list[FolderPlan] = []
    for path, hints in probes:
        source = None if args.source == "auto" else _canonical_source(args.source)
        source = _canonical_source(source or hints.source or common_source) or ""
        if not source:
            raise ValueError(f"无法识别来源：{path.name}；请传 --source")
        group = args.group if args.group is not None else (hints.group or common_group)
        platform = args.platform if args.platform is not None else (hints.platform or common_platform)
        media = MediaInfo(
            **{
                **first_media.__dict__,
                "video_codec": _video_codec(first_media.video_format, first_media.writing_library, source),
            }
        )
        episode = hints.episode or ""
        release_title = build_title(
            title=title,
            year=year,
            source=source,
            media=media,
            group=group,
            edition=edition,
            episode=episode,
            platform=platform,
            include_audio_count=args.audio_count,
        )
        season_directory = root / _season_folder(episode)
        target = season_directory / (release_title + path.suffix.lower())
        logical = target.relative_to(root)
        provisional.append(
            FolderPlan(
                source_path=path,
                target_path=target,
                logical_path=logical,
                episode=episode,
                source=source,
                group=group,
                platform=platform,
                media=media,
            )
        )

    target_keys: dict[str, list[Path]] = {}
    for plan in provisional:
        target_keys.setdefault(str(plan.target_path).casefold(), []).append(plan.target_path)
    duplicates = [items[0] for items in target_keys.values() if len(items) > 1]
    if duplicates:
        raise ValueError("生成了重复目标名称，未执行任何改名：" + "；".join(str(path) for path in duplicates))
    conflicts = [
        plan.target_path
        for plan in provisional
        if plan.target_path.exists() and plan.target_path != plan.source_path
    ]
    if conflicts:
        raise FileExistsError("目标文件已存在，未执行任何改名：" + "；".join(str(path) for path in conflicts))

    representative = provisional[0]
    season = _season_label([plan.episode for plan in provisional])
    pack_title = build_title(
        title=title,
        year=year,
        source=representative.source,
        media=representative.media,
        group=representative.group,
        edition=edition,
        episode=season,
        platform=representative.platform,
        include_audio_count=args.audio_count,
    )
    douban_seasons = {
        season_number
        for plan in provisional
        if (season_number := _season_number_from_episode(plan.episode)) is not None
    }
    douban_season_number = next(iter(douban_seasons)) if len(douban_seasons) == 1 else None
    douban = _douban_for_release(
        args,
        tmdb=tmdb,
        title=title,
        base_title=base_title,
        year=year,
        season_number=douban_season_number,
    )
    language_code = (tmdb.original_language if tmdb else "") or representative.media.audio_language
    subtitle = build_subtitle(
        douban=douban,
        tmdb=tmdb,
        fallback_title=title,
        language_code=language_code,
    )
    animation = args.animation or bool(tmdb and 16 in tmdb.genre_ids)
    category = args.category or infer_mteam_category(
        kind="tv",
        source=representative.source,
        resolution=representative.media.resolution,
        animation=animation,
    )

    output_dir = (args.output or root.parent / f"{pack_title}.prepare").resolve()
    if args.apply:
        try:
            output_dir.relative_to(root.resolve())
        except ValueError:
            pass
        else:
            raise ValueError("重命名剧集根目录时，--output 不能位于输入目录内")
    output_dir.mkdir(parents=True, exist_ok=True)
    if cached_technical:
        technical_info_type, media_text, bdinfo_playlist = cached_technical
        media_path = output_dir / "bdinfo.txt"
        media_path.write_text(media_text, encoding="utf-8")
    else:
        technical_info_type, media_text, media_path, bdinfo_playlist = prepare_technical_info(
            representative.source_path,
            representative.target_path.name,
            representative.source,
            output_dir,
            bdinfo_report=args.bdinfo_report,
            bdinfo_exe=args.bdinfo_exe,
            bdinfo_playlist=args.bdinfo_playlist,
        )
    file_records: list[dict[str, Any]] = []
    for index, plan in enumerate(provisional, start=1):
        final_prepared_path = (
            target_root / plan.target_path.relative_to(root)
            if args.apply
            else plan.source_path
        )
        final_representative_path = (
            target_root / representative.target_path.relative_to(root)
            if args.apply
            else representative.source_path
        )
        file_records.append(
            {
                "source_path": str(plan.source_path),
                "prepared_path": str(final_prepared_path),
                "filename": plan.target_path.name,
                "relative_path": str(plan.logical_path),
                "episode": plan.episode,
                "source": plan.source,
                "group": plan.group or "",
                "media": asdict(plan.media),
                "mediainfo_path": str(media_path) if index == 1 else "",
                "mediainfo_text": media_text if index == 1 else "",
                "technical_info_type": technical_info_type if index == 1 else "",
                "technical_info_path": str(media_path) if index == 1 else "",
                "technical_info_text": media_text if index == 1 else "",
                "media_inherited_from": str(final_representative_path),
            }
        )

    screenshot_paths: list[Path] = []
    if not args.skip_screenshots:
        if args.screenshots != 4:
            print(f"提示：当前要求为 4 张截图，本次按参数生成 {args.screenshots} 张。")
        screenshot_paths = _folder_screenshots(
            provisional,
            output_dir / "screenshots",
            args.screenshots,
            args.screenshot_source,
            first_only=disc_collection,
        )

    torrent_path = output_dir / f"{pack_title}.torrent"
    piece_length = 0
    if not args.skip_torrent:
        torrent_files = sorted(
            ((plan.source_path, plan.logical_path) for plan in provisional),
            key=lambda item: str(item[1]).casefold(),
        )
        torrent_root_name = target_root.name if args.apply else root.name
        piece_length = create_private_v1_folder_torrent(root, torrent_files, torrent_path, torrent_root_name)

    imdb_url = f"https://www.imdb.com/title/{tmdb.imdb_id}/" if tmdb and tmdb.imdb_id else ""
    payload = {
        "schema_version": 1,
        "created_at": int(time.time()),
        "input_path": str(root),
        "prepared_path": str(target_root if args.apply else root),
        "release_name": pack_title,
        "filename": target_root.name if args.apply else root.name,
        "kind": "tv",
        "episode": season,
        "year": year or "",
        "source": representative.source,
        "group": representative.group or "",
        "category": category,
        "title": pack_title,
        "subtitle": subtitle,
        "tmdb": asdict(tmdb) if tmdb else None,
        "douban_url": douban.url if douban else "",
        "imdb_url": imdb_url,
        "source_language": LANGUAGE_NAMES.get(language_code.lower(), language_code),
        "media": asdict(representative.media),
        "media_probe_path": str(
            target_root / representative.target_path.relative_to(root)
            if args.apply
            else representative.source_path
        ),
        "mediainfo_text": media_text,
        "mediainfo_path": str(media_path),
        "technical_info_type": technical_info_type,
        "technical_info_text": media_text,
        "technical_info_path": str(media_path),
        "bdinfo_playlist": bdinfo_playlist,
        "files": file_records,
        "screenshots": [str(item) for item in screenshot_paths],
        "torrent": {
            "path": str(torrent_path) if not args.skip_torrent else "",
            "format": "v1",
            "private": True,
            "piece_length": piece_length,
            "tracker": "",
            "web_seed": "",
            "comment": "",
            "source": "",
        },
    }
    package_path = output_dir / "mteam-prepare.json"
    package_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n剧集文件名预检完成：")
    for plan in provisional:
        relative_source = plan.source_path.relative_to(root)
        relative_target = plan.target_path.relative_to(root)
        marker = "=" if relative_source == relative_target else "→"
        print(f"  {relative_source} {marker} {relative_target}")
    if args.apply:
        _apply_folder_renames(provisional, root=root, target_root=target_root)

    print("\nM-Team 整季发布资料已准备完：")
    print(f"  整季标题：{pack_title}")
    rename_status = "已全部改名" if args.apply else "尚未改名"
    print(f"  视频文件：{len(provisional)} 个，{rename_status}")
    if args.apply and not _same_path(root, target_root):
        print(f"  剧集目录：{target_root}")
    elif not _same_path(root, target_root):
        print(f"  剧集目录预览：{root.name} → {target_root.name}")
    print(f"  副标题：{subtitle or '未识别'}")
    print(f"  分类：{category}")
    print(f"  豆瓣：{douban.url if douban else '未找到，请手工补充'}")
    print(
        f"  {technical_info_type}：1 份"
        f"（探测 {representative.source_path.relative_to(root)}，其余光盘复用）"
        if disc_collection
        else f"  {technical_info_type}：1 份（探测 {representative.source_path.relative_to(root)}）"
    )
    print(f"  截图：{len(screenshot_paths)} 张")
    if not args.skip_torrent:
        print(f"  V1 私有多文件种子：{torrent_path}")
    print(f"  发布资料包：{package_path}")
    if not args.apply:
        print("  注意：源文件尚未改名；确认后重新执行并添加 --apply。")
    return package_path


def main(argv: list[str] | None = None) -> Path | None:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        path = args.input.resolve()
        if path.is_dir():
            return _prepare_folder(args, path)
        if not path.is_file():
            raise FileNotFoundError(f"找不到媒体文件或文件夹：{path}")
        precomputed_bdinfo: tuple[str, str] | None = None
        try:
            initial_media = read_mediainfo(path)
        except (RuntimeError, ValueError):
            if path.suffix.casefold() != ".iso":
                raise
            initial_media, precomputed_bdinfo = _read_initial_iso_media(args, path)
        base_title, year, source, group, edition, episode, platform = _resolve_fields(args, path, initial_media)
        kind = args.kind if args.kind != "auto" else ("tv" if episode else "movie")

        tmdb: TmdbMatch | None = None
        tmdb_client = TmdbClient(
            read_token=os.environ.get("TMDB_READ_ACCESS_TOKEN", ""),
            api_key=os.environ.get("TMDB_API_KEY", ""),
        )
        if not args.offline and tmdb_client.available:
            try:
                if args.tmdb_id:
                    tmdb = tmdb_client.by_id(kind, args.tmdb_id)
                else:
                    tmdb = _choose_tmdb(tmdb_client.search(kind, base_title, year))
            except (OSError, urllib.error.URLError, json.JSONDecodeError, KeyError, ValueError) as exc:
                print(f"警告：TMDB 查询失败，将使用文件名结果：{exc}")
        elif not args.offline:
            print("提示：未配置 TMDB_READ_ACCESS_TOKEN，暂用文件名和豆瓣识别；配置后可提高片名准确率。")

        title = args.title or (tmdb.name if tmdb and tmdb.name else base_title)
        year = args.year or (tmdb.year if tmdb and tmdb.year else year)
        source = _canonical_source(source) or source
        media = MediaInfo(
            **{
                **initial_media.__dict__,
                "video_codec": _video_codec(initial_media.video_format, initial_media.writing_library, source),
            }
        )
        release_title = build_title(
            title=title,
            year=year,
            source=source,
            media=media,
            group=group,
            edition=edition,
            episode=episode,
            platform=platform,
            include_audio_count=args.audio_count,
        )
        target = path.with_name(release_title + path.suffix.lower())
        if args.apply and target.exists() and target != path:
            raise FileExistsError(f"目标文件已存在：{target}")

        douban = _douban_for_release(
            args,
            tmdb=tmdb,
            title=title,
            base_title=base_title,
            year=year,
            season_number=_season_number_from_episode(episode),
        )

        language_code = (tmdb.original_language if tmdb else "") or media.audio_language
        subtitle = build_subtitle(
            douban=douban,
            tmdb=tmdb,
            fallback_title=title,
            language_code=language_code,
        )
        animation = args.animation or bool(tmdb and 16 in tmdb.genre_ids)
        category = args.category or infer_mteam_category(
            kind=kind,
            source=source,
            resolution=media.resolution,
            animation=animation,
        )

        output_dir = (args.output or target.with_suffix(".prepare")).resolve()
        screenshots_dir = output_dir / "screenshots"
        output_dir.mkdir(parents=True, exist_ok=True)
        if precomputed_bdinfo is not None:
            media_text, bdinfo_playlist = precomputed_bdinfo
            technical_info_type = "BDInfo"
            media_text_path = output_dir / "bdinfo.txt"
            media_text_path.write_text(media_text, encoding="utf-8")
        else:
            technical_info_type, media_text, media_text_path, bdinfo_playlist = prepare_technical_info(
                path,
                target.name,
                source,
                output_dir,
                bdinfo_report=args.bdinfo_report,
                bdinfo_exe=args.bdinfo_exe,
                bdinfo_playlist=args.bdinfo_playlist,
            )

        screenshot_paths: list[Path] = []
        if not args.skip_screenshots:
            if args.screenshots != 4:
                print(f"提示：当前要求为 4 张截图，本次按参数生成 {args.screenshots} 张。")
            with screenshot_source(path, args.screenshot_source) as source_for_screenshots:
                screenshot_paths = _extract_screenshots(
                    source_for_screenshots,
                    screenshots_dir,
                    count=args.screenshots,
                )

        torrent_path = output_dir / f"{target.stem}.torrent"
        piece_length = 0
        if not args.skip_torrent:
            piece_length = create_private_v1_torrent(path, torrent_path, target.name)

        prepared_path = path
        if args.apply and target != path:
            path.rename(target)
            prepared_path = target

        imdb_url = f"https://www.imdb.com/title/{tmdb.imdb_id}/" if tmdb and tmdb.imdb_id else ""
        payload = {
            "schema_version": 1,
            "created_at": int(time.time()),
            "input_path": str(path),
            "prepared_path": str(prepared_path),
            "release_name": release_title,
            "filename": target.name,
            "kind": kind,
            "episode": episode or "",
            "year": year or "",
            "source": source,
            "group": group or "",
            "category": category,
            "title": release_title,
            "subtitle": subtitle,
            "tmdb": asdict(tmdb) if tmdb else None,
            "douban_url": douban.url if douban else "",
            "imdb_url": imdb_url,
            "source_language": LANGUAGE_NAMES.get(language_code.lower(), language_code),
            "media": asdict(media),
            "technical_info_type": technical_info_type,
            "technical_info_text": media_text,
            "technical_info_path": str(media_text_path),
            "bdinfo_playlist": bdinfo_playlist,
            "mediainfo_text": media_text,
            "mediainfo_path": str(media_text_path),
            "screenshots": [str(item) for item in screenshot_paths],
            "torrent": {
                "path": str(torrent_path) if not args.skip_torrent else "",
                "format": "v1",
                "private": True,
                "piece_length": piece_length,
                "tracker": "",
                "web_seed": "",
                "comment": "",
                "source": "",
            },
        }
        package_path = output_dir / "mteam-prepare.json"
        package_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        print("\nM-Team 发布资料已准备完成：")
        print(f"  标题：{release_title}")
        print(f"  副标题：{subtitle or '未识别'}")
        print(f"  分类：{category}")
        print(f"  豆瓣：{douban.url if douban else '未找到，请手工补充'}")
        print(f"  {technical_info_type}：{media_text_path}")
        print(f"  截图：{len(screenshot_paths)} 张")
        if not args.skip_torrent:
            print(f"  V1 私有种子：{torrent_path}")
        print(f"  发布资料包：{package_path}")
        if not args.apply and target != path:
            print(f"  注意：源文件尚未改名；确认后可重新执行并添加 --apply，目标名为 {target.name}")
        return package_path
    except (FileNotFoundError, FileExistsError, RuntimeError, ValueError, OSError) as exc:
        parser.error(str(exc))
