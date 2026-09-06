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
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterator

from .cli import (
    VIDEO_EXTENSIONS,
    FilenameHints,
    MediaInfo,
    _canonical_source,
    _find_mediainfo,
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
class DoubanMatch:
    id: str
    url: str
    title: str
    original_title: str
    year: str
    score: float


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


def _douban_candidates(names: list[str], year: str | None) -> list[DoubanMatch]:
    found: dict[str, DoubanMatch] = {}
    for name in names:
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
                score = _match_score(query, [title, original], year, item_year)
                candidate = DoubanMatch(
                    id=item_id,
                    url=f"https://movie.douban.com/subject/{item_id}/" if item_id else str(item.get("url") or ""),
                    title=title,
                    original_title=original,
                    year=item_year,
                    score=score,
                )
                if item_id and (item_id not in found or score > found[item_id].score):
                    found[item_id] = candidate
    return sorted(found.values(), key=lambda item: item.score, reverse=True)


def _choose_douban(candidates: list[DoubanMatch]) -> DoubanMatch | None:
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
    sd = bool(re.match(r"(?:480|576)[pi]$", resolution, re.I))
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


def create_private_v1_torrent(source: Path, output: Path, logical_name: str) -> int:
    total_size = source.stat().st_size
    piece_length = automatic_piece_length(total_size)
    piece_count = math.ceil(total_size / piece_length) if total_size else 1
    pieces = bytearray()
    print(f"正在生成 V1 私有种子：{piece_count} 个分块，每块 {piece_length // 1024} KiB")
    with source.open("rb") as handle:
        while True:
            chunk = handle.read(piece_length)
            if not chunk:
                break
            pieces.extend(hashlib.sha1(chunk).digest())
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
    total_size = sum(source.stat().st_size for source, _logical in files)
    piece_length = automatic_piece_length(total_size)
    piece_count = math.ceil(total_size / piece_length) if total_size else 0
    print(f"正在生成 V1 私有目录种子：{piece_count} 个分块，每块 {piece_length // 1024} KiB")
    pieces = bytearray()
    pending = bytearray()
    file_entries: list[dict[str, Any]] = []
    for source, logical_path in files:
        try:
            source.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError(f"制种文件不在输入目录内：{source}") from exc
        if logical_path.is_absolute() or ".." in logical_path.parts:
            raise ValueError(f"种子内部路径不安全：{logical_path}")
        file_entries.append({"length": source.stat().st_size, "path": list(logical_path.parts)})
        with source.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                pending.extend(chunk)
                while len(pending) >= piece_length:
                    pieces.extend(hashlib.sha1(pending[:piece_length]).digest())
                    del pending[:piece_length]
    if pending:
        pieces.extend(hashlib.sha1(pending).digest())
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
    raise FileNotFoundError(
        "Blu-ray ISO 必须使用 BDInfo：请先运行 `winget install agentjp.bdinfo-rs`，"
        "或用 --bdinfo-report 指定已由图形版 BDInfo 保存的 Text 报告"
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
    if not selected:
        list_command = [cli, str(disc), "--list"] if kind == "rs" else [cli, "--list", str(disc)]
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
    output_dir.mkdir(parents=True, exist_ok=True)
    if kind == "rs":
        command = [cli, str(disc), str(output_dir), "--mpls", selected]
    else:
        command = [cli, "--mpls", f"{selected}.MPLS", str(disc), str(output_dir)]
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


def _mount_iso(path: Path) -> tuple[Path, bool]:
    if sys.platform != "win32":
        raise RuntimeError("ISO 自动挂载目前只支持 Windows；可用 --screenshot-source 指定已挂载的视频文件")
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
    return Path(data["root"]), bool(data["mountedByUs"])


def _unmount_iso(path: Path) -> None:
    environment = os.environ.copy()
    environment["MEDIA_TITLE_ISO_PATH"] = str(path)
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
    root, mounted_by_us = _mount_iso(path)
    try:
        candidates = list((root / "BDMV" / "STREAM").glob("*.m2ts"))
        candidates += list((root / "VIDEO_TS").glob("VTS_*_[1-9].VOB"))
        if not candidates:
            raise RuntimeError("ISO 中没有找到可用于截图的 M2TS/VOB；请用 --screenshot-source 指定视频文件")
        yield max(candidates, key=lambda item: item.stat().st_size)
    finally:
        if mounted_by_us:
            _unmount_iso(path)


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


def _episode_sort_key(value: str) -> tuple[int, int, str]:
    match = re.match(r"S(\d{1,2})(?:E(\d{1,3}))?", value, re.I)
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


def _douban_for_release(
    args: argparse.Namespace,
    *,
    tmdb: TmdbMatch | None,
    title: str,
    base_title: str,
    year: str | None,
) -> DoubanMatch | None:
    douban = _manual_douban(args.douban_url) if args.douban_url else None
    if not douban and not args.offline:
        search_names = [
            tmdb.chinese_name if tmdb else "",
            tmdb.name if tmdb else "",
            tmdb.original_name if tmdb else "",
            title,
            base_title,
        ]
        douban = _choose_douban(_douban_candidates(search_names, year))
    if not douban and sys.stdin.isatty():
        manual = input("未自动找到可靠豆瓣条目，可粘贴豆瓣链接或直接回车跳过：").strip()
        if manual:
            douban = _manual_douban(manual)
    return douban


def _folder_screenshots(
    plans: list[FolderPlan],
    output: Path,
    count: int,
    override: Path | None,
) -> list[Path]:
    if count <= 0:
        return []
    if override:
        with screenshot_source(plans[0].source_path, override) as source_for_screenshots:
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


def _apply_folder_renames(plans: list[FolderPlan]) -> None:
    completed: list[tuple[Path, Path]] = []
    created_directories: list[Path] = []
    try:
        for plan in plans:
            if plan.source_path == plan.target_path:
                continue
            if not plan.target_path.parent.exists():
                plan.target_path.parent.mkdir(parents=True, exist_ok=True)
                created_directories.append(plan.target_path.parent)
            plan.source_path.rename(plan.target_path)
            completed.append((plan.source_path, plan.target_path))
    except OSError as exc:
        rollback_errors: list[str] = []
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
    paths = _folder_videos(root)
    if not paths:
        raise ValueError("目录中没有找到支持的视频文件")

    probes: list[tuple[Path, FilenameHints]] = []
    missing_episodes: list[Path] = []
    print(f"正在扫描剧集文件名：共 {len(paths)} 个")
    for path in paths:
        hints = filename_hints(path)
        probes.append((path, hints))
        if not hints.episode:
            missing_episodes.append(path)
    if missing_episodes:
        examples = "；".join(str(path.relative_to(root)) for path in missing_episodes[:8])
        extra = f"（另有 {len(missing_episodes) - 8} 个）" if len(missing_episodes) > 8 else ""
        raise ValueError(f"以下文件无法识别 SxxExx 季集号，未改名：{examples}{extra}")
    probes.sort(key=lambda item: (*_episode_sort_key(item[1].episode or ""), str(item[0]).casefold()))

    first_path, _first_hints = probes[0]
    print(f"正在探测代表集 MediaInfo：{first_path.relative_to(root)}（其余 {len(paths) - 1} 集复用）")
    first_media = read_mediainfo(first_path)
    probes[0] = (first_path, filename_hints(first_path, first_media))
    shared_args = argparse.Namespace(**vars(args))
    shared_args.kind = "tv"
    shared_args.episode = None
    base_title, year, common_source, common_group, edition, _episode, common_platform = _resolve_fields(
        shared_args, first_path, first_media
    )
    tmdb = _tmdb_for_release(args, kind="tv", base_title=base_title, year=year)
    title = args.title or (tmdb.name if tmdb and tmdb.name else base_title)
    year = args.year or (tmdb.year if tmdb and tmdb.year else year)

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
    douban = _douban_for_release(args, tmdb=tmdb, title=title, base_title=base_title, year=year)
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
    output_dir.mkdir(parents=True, exist_ok=True)
    media_text = read_mediainfo_text(representative.source_path, representative.target_path.name)
    media_path = output_dir / "mediainfo.txt"
    media_path.write_text(media_text, encoding="utf-8")
    file_records: list[dict[str, Any]] = []
    for index, plan in enumerate(provisional, start=1):
        file_records.append(
            {
                "source_path": str(plan.source_path),
                "prepared_path": str(plan.target_path if args.apply else plan.source_path),
                "filename": plan.target_path.name,
                "relative_path": str(plan.logical_path),
                "episode": plan.episode,
                "source": plan.source,
                "group": plan.group or "",
                "media": asdict(plan.media),
                "mediainfo_path": str(media_path) if index == 1 else "",
                "mediainfo_text": media_text if index == 1 else "",
                "media_inherited_from": str(representative.source_path),
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
        )

    torrent_path = output_dir / f"{pack_title}.torrent"
    piece_length = 0
    if not args.skip_torrent:
        torrent_files = sorted(
            ((plan.source_path, plan.logical_path) for plan in provisional),
            key=lambda item: str(item[1]).casefold(),
        )
        piece_length = create_private_v1_folder_torrent(root, torrent_files, torrent_path, root.name)

    imdb_url = f"https://www.imdb.com/title/{tmdb.imdb_id}/" if tmdb and tmdb.imdb_id else ""
    payload = {
        "schema_version": 1,
        "created_at": int(time.time()),
        "input_path": str(root),
        "prepared_path": str(root),
        "release_name": pack_title,
        "filename": root.name,
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
        "media_probe_path": str(representative.source_path),
        "mediainfo_text": media_text,
        "mediainfo_path": str(media_path),
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
        _apply_folder_renames(provisional)

    print("\nM-Team 整季发布资料已准备完：")
    print(f"  整季标题：{pack_title}")
    rename_status = "已全部改名" if args.apply else "尚未改名"
    print(f"  视频文件：{len(provisional)} 个，{rename_status}")
    print(f"  副标题：{subtitle or '未识别'}")
    print(f"  分类：{category}")
    print(f"  豆瓣：{douban.url if douban else '未找到，请手工补充'}")
    print(f"  MediaInfo：1 份（探测 {representative.source_path.relative_to(root)}）")
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
        initial_media = read_mediainfo(path)
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

        douban: DoubanMatch | None = _manual_douban(args.douban_url) if args.douban_url else None
        if not douban and not args.offline:
            search_names = [
                tmdb.chinese_name if tmdb else "",
                tmdb.name if tmdb else "",
                tmdb.original_name if tmdb else "",
                title,
                base_title,
            ]
            douban = _choose_douban(_douban_candidates(search_names, year))
        if not douban and sys.stdin.isatty():
            manual = input("未自动找到可靠豆瓣条目，可粘贴豆瓣链接或直接回车跳过：").strip()
            if manual:
                douban = _manual_douban(manual)

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
