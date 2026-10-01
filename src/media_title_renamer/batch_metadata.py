"""Read-only batch metadata previews for TorrentLeech media resources."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cli import VIDEO_EXTENSIONS, filename_hints
from .episode_mapping import episode_number_hint, sample_or_extra, season_number_hint, sequential_episode_map


DEFAULT_ROOT = Path("/data/Zhyw/media-stack/downloads/TL")
DEFAULT_OUTPUT_ROOT = Path("/data/Zhyw/media-stack/downloads/.mteam-transfer/batch-metadata")
MAX_RESOURCE_BYTES = 200 * 1024**3
REAL_VIDEO_EXTENSIONS = VIDEO_EXTENSIONS - {".iso"}
AUDIO_EXTENSIONS = {
    ".aac", ".aif", ".aiff", ".ape", ".flac", ".m4a", ".m4b", ".mp3",
    ".oga", ".ogg", ".opus", ".wav", ".wma",
}
ARCHIVE_EXTENSIONS = {
    ".7z", ".001", ".ace", ".arj", ".bz", ".bz2", ".cab", ".gz",
    ".iso", ".lz", ".lz4", ".lzma", ".lzo", ".rar", ".r00", ".r01",
    ".tar", ".tgz", ".xz", ".z", ".zip", ".zst",
}
SPLIT_ARCHIVES = {
    ".001", ".ace", ".arj", ".rar", ".r00", ".r01", ".7z", ".zip",
}
GAME_NAME = re.compile(
    r"(?:\b(?:gog|rune|codex|fitgirl|steamrip|repack|pc[ ._-]*game|game[ ._-]*of[ ._-]*the[ ._-]*year|switch|xbox|playstation|ps[345])\b)",
    re.I,
)
SPORTS_EVENT_NAME = re.compile(
    r"(?:\b(?:AMA\s+Supercross|Supercross|SuperMoto(?:cross)?|Motocross|MotoGP|Formula\s*1|F1|NASCAR|IndyCar|WRC|UFC|WWE|NBA|NFL|MLB|NHL|NCAA|Olympics|Grand\s+Prix)\b|"
    r"\b(?:Rd|Round)\s*\d+\b)",
    re.I,
)
TV_MARKER = re.compile(
    r"(?:\bS\d{1,2}(?:[ ._-]*E\d{1,3}(?:[ ._-]*(?:-|E)\d{1,3})*)?\b|"
    r"\bSeason[ ._-]*\d{1,2}\b|第[0-9零〇一二两三四五六七八九十]+季|\bS\d{1,2}D\d{1,2}\b)",
    re.I,
)
MEDIA_ISO_HINT = re.compile(
    r"(?:\b(?:19|20)\d{2}\b|\b(?:2160|1080|720|540|480|360)p\b|"
    r"blu[ ._-]?ray|uhd|dvd|remux|web[ ._-]?dl|hdtv|bdrip)",
    re.I,
)
RATE_LIMIT = re.compile(r"(?<!非 )HTTP\s*(?:403|429)|搜索访问太频繁|访问频率过高|验证码|安全验证", re.I)
REQUIRED_FIELDS = (
    "title", "subtitle", "category", "douban_url", "resolution",
    "video_codec", "audio_codec", "group", "technical_info_text",
)
ALLOWED_RESOLUTIONS = {"360p", "480p", "540p", "720p", "1080p", "2160p"}


@dataclass
class Resource:
    resource_id: str
    name: str
    relative_path: str
    path: str
    size_bytes: int
    file_count: int
    files: list[dict[str, Any]] = field(default_factory=list)
    symlinks: list[str] = field(default_factory=list)
    scan_errors: list[str] = field(default_factory=list)
    classification: str = "unknown"
    media_kind: str = ""
    input_path: str = ""
    notes: list[str] = field(default_factory=list)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".writing")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _iter_resource_files(root: Path) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    files: list[dict[str, Any]] = []
    symlinks: list[str] = []
    errors: list[str] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda item: item.name.casefold(), reverse=True)
        except OSError as exc:
            errors.append(f"{current.relative_to(root) or '.'}: {type(exc).__name__}")
            continue
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            try:
                if entry.is_symlink():
                    symlinks.append(relative)
                elif entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                elif entry.is_file(follow_symlinks=False):
                    stat = entry.stat(follow_symlinks=False)
                    files.append(
                        {
                            "relative_path": relative,
                            "size_bytes": stat.st_size,
                            "suffix": path.suffix.casefold(),
                            "device": stat.st_dev,
                            "inode": stat.st_ino,
                            "mtime_ns": stat.st_mtime_ns,
                        }
                    )
                else:
                    errors.append(f"{relative}: 非普通文件或目录")
            except OSError as exc:
                errors.append(f"{relative}: {type(exc).__name__}")
    files.sort(key=lambda item: item["relative_path"].casefold())
    return files, sorted(symlinks), errors


def _is_media_iso(resource_name: str, file_path: Path) -> bool:
    name = f"{resource_name} {file_path.name}"
    return bool(MEDIA_ISO_HINT.search(name)) and not GAME_NAME.search(name)


def _has_complete_episode_sequence(resource: Resource, video_files: list[Path]) -> bool:
    resource_root = Path(resource.path)
    season_values = {
        season
        for path in video_files
        if (season := season_number_hint(path.relative_to(resource_root).parent.as_posix())) is not None
    }
    if not season_values:
        season_values = {
            season
            for path in video_files
            if (season := season_number_hint(path.name)) is not None
        }
    # A top-level explicit season marker applies to otherwise bare numbered
    # episode files (e.g. Mayday.S14/14x01.avi).
    if not season_values:
        root_season = season_number_hint(resource.name)
        if root_season is not None:
            season_values = {root_season}
    if len(season_values) > 1:
        by_season: dict[int, list[Path]] = {}
        for path in video_files:
            season = season_number_hint(path.relative_to(resource_root).parent.as_posix()) or season_number_hint(path.name)
            if season is None:
                season = season_number_hint(resource.name)
            if season is None:
                return False
            by_season.setdefault(season, []).append(path)
        return all(sequential_episode_map(paths, season=season) is not None for season, paths in by_season.items())
    season = next(iter(season_values), None)
    return sequential_episode_map(video_files, season=season) is not None


def _archive_dominates(resource: Resource, video_files: list[Path]) -> bool:
    """Reject obvious split-archive releases and tiny embedded samples."""
    video_bytes = sum(path.stat().st_size for path in video_files)
    archive_bytes = sum(
        int(row["size_bytes"])
        for row in resource.files
        if Path(row["relative_path"]).suffix.casefold() in SPLIT_ARCHIVES
    )
    if not archive_bytes:
        return False
    if video_files and all(sample_or_extra(path.relative_to(Path(resource.path))) for path in video_files):
        return True
    return archive_bytes >= video_bytes or max((path.stat().st_size for path in video_files), default=0) < 80 * 1024**2


def _classify_resource(resource: Resource, root: Path) -> None:
    if resource.size_bytes > MAX_RESOURCE_BYTES:
        resource.classification = "oversize"
        resource.notes.append("超过 200 GiB，只盘点、不执行媒体探测")
        return
    if resource.scan_errors:
        resource.classification = "needs_review"
        resource.notes.append("目录扫描存在权限或 I/O 错误，不安全地跳过处理")
        return
    if resource.symlinks:
        resource.classification = "needs_review"
        resource.notes.append("资源内部包含符号链接；为避免越界读取，未跟随链接")
        return
    if SPORTS_EVENT_NAME.search(resource.name):
        resource.classification = "unsupported_type"
        resource.notes.append("体育赛事录像不属于本轮电影/电视剧发布资料范围")
        return

    resource_root = Path(resource.path)
    files = [
        resource_root / row["relative_path"] if resource_root.is_dir() else resource_root
        for row in resource.files
    ]
    regular_videos = [path for path in files if path.suffix.casefold() in REAL_VIDEO_EXTENSIONS]
    iso_files = [path for path in files if path.suffix.casefold() == ".iso"]
    bdmv_roots = [path for path in files if path.name.casefold() == "index.bdmv"]
    dvd_roots = [path for path in files if path.name.casefold() == "video_ts.ifo"]
    media_isos = [path for path in iso_files if _is_media_iso(resource.name, path)]

    if bdmv_roots or dvd_roots:
        resource.classification = "needs_review"
        resource.notes.append("检测到 BDMV/VIDEO_TS 目录；当前批处理不把光盘目录误当剧集目录")
        return

    video_files = regular_videos + media_isos
    if video_files:
        if _archive_dominates(resource, video_files):
            resource.classification = "unsupported_type"
            resource.notes.append("视频只是压缩包发行物中的样片/残片；按用户要求不处理分卷 RAR/ZIP/7z 影视资源")
            return
        relevant_videos = [path for path in video_files if not sample_or_extra(path.relative_to(resource_root))]
        if not relevant_videos:
            resource.classification = "unsupported_type"
            resource.notes.append("只发现 Sample/Proof 样片文件，不作为影视正片处理")
            return
        video_files = relevant_videos
        root_has_season = bool(TV_MARKER.search(resource.name))
        explicit_episode = any(
            re.search(
                r"\bS\d{1,2}[ ._-]*E\d{1,3}\b|\b\d{1,2}\s*[xX]\s*\d{1,3}\b|\b(?:EP|Episode)\s*\d{1,3}\b",
                path.name,
                re.I,
            )
            for path in video_files
        )
        numbered_paths = [path for path in video_files if episode_number_hint(path) is not None]
        numbered_set_likely = len(numbered_paths) >= 3 and len(numbered_paths) / len(video_files) >= 0.75
        is_tv = (
            root_has_season
            or explicit_episode
            or _has_complete_episode_sequence(resource, video_files)
            or numbered_set_likely
        )
        if len(video_files) > 1 and not is_tv:
            sizes = sorted((path.stat().st_size for path in video_files), reverse=True)
            if len(sizes) > 1 and sizes[0] >= max(1, sizes[1]) * 12:
                resource.notes.append("存在多个视频文件，最大文件远大于其余文件；需人工确认附属视频是否应纳入发布")
            else:
                resource.classification = "needs_review"
                resource.notes.append("同一顶层资源有多个视频文件，但季集关系不明确；不猜测电影/剧集边界")
                return
        resource.media_kind = "tv" if is_tv else "movie"
        resource.classification = resource.media_kind
        if is_tv and len(video_files) > 1:
            resource.input_path = str(resource_root)
        elif len(video_files) == 1:
            resource.input_path = str(video_files[0])
        elif len(video_files) == 1 or (len(video_files) > 1 and is_tv):
            resource.input_path = str(resource_root)
        elif len(video_files) > 1:
            largest = max(video_files, key=lambda path: path.stat().st_size)
            resource.input_path = str(largest)
        return

    suffixes = {Path(row["relative_path"]).suffix.casefold() for row in resource.files}
    if suffixes & AUDIO_EXTENSIONS:
        resource.classification = "unsupported_type"
        resource.notes.append("纯音频资源，不属于本次电影/电视剧资料流程")
    elif iso_files:
        resource.classification = "unsupported_type"
        resource.notes.append("ISO 未含可确认的影视发行标记，按游戏/软件/未知光盘处理，未猜测影视类型")
    elif GAME_NAME.search(resource.name) or suffixes & {".exe", ".dll", ".pak", ".obb", ".apk", ".msi"}:
        resource.classification = "unsupported_type"
        resource.notes.append("疑似游戏或软件资源，不走影视发布资料流程")
    elif suffixes & ARCHIVE_EXTENSIONS:
        resource.classification = "unsupported_type"
        resource.notes.append("只有压缩包/分卷文件，没有可直接探测的视频文件")
    else:
        resource.classification = "needs_review"
        resource.notes.append("没有发现可确认的影视视频文件，资源类型不明")


def scan_resources(root: Path) -> list[Resource]:
    root = root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"TL 根路径不是目录：{root}")
    resources: list[Resource] = []
    for entry in sorted(os.scandir(root), key=lambda item: item.name.casefold()):
        path = Path(entry.path)
        if entry.is_symlink():
            resource_id = hashlib.sha256(entry.name.encode("utf-8", "replace")).hexdigest()[:12]
            resource = Resource(
                resource_id, entry.name, entry.name, str(path), 0, 0,
                symlinks=[entry.name], classification="needs_review",
                notes=["TL 顶层条目是符号链接，未跟随"]
            )
        elif entry.is_dir(follow_symlinks=False):
            file_rows, symlinks, errors = _iter_resource_files(path)
            relative = path.relative_to(root).as_posix()
            resource_id = hashlib.sha256(relative.encode("utf-8", "replace")).hexdigest()[:12]
            resource = Resource(
                resource_id=resource_id,
                name=entry.name,
                relative_path=relative,
                path=str(path),
                size_bytes=sum(row["size_bytes"] for row in file_rows),
                file_count=len(file_rows),
                files=file_rows,
                symlinks=symlinks,
                scan_errors=errors,
            )
        elif entry.is_file(follow_symlinks=False):
            stat = entry.stat(follow_symlinks=False)
            relative = path.relative_to(root).as_posix()
            resource_id = hashlib.sha256(relative.encode("utf-8", "replace")).hexdigest()[:12]
            resource = Resource(
                resource_id=resource_id,
                name=entry.name,
                relative_path=relative,
                path=str(path),
                size_bytes=stat.st_size,
                file_count=1,
                files=[{
                    "relative_path": entry.name,
                    "size_bytes": stat.st_size,
                    "suffix": path.suffix.casefold(),
                    "device": stat.st_dev,
                    "inode": stat.st_ino,
                    "mtime_ns": stat.st_mtime_ns,
                }],
            )
        else:
            continue
        _classify_resource(resource, root)
        resources.append(resource)
    return resources


def _source_snapshot(resource: Resource) -> list[tuple[int, int, int, int]]:
    snapshot: list[tuple[int, int, int, int]] = []
    root = Path(resource.path)
    for row in resource.files:
        path = (root / row["relative_path"]) if root.is_dir() else root
        stat = path.stat(follow_symlinks=False)
        snapshot.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
    return snapshot


def _resource_name_hints(resource: Resource):
    resource_path = Path(resource.path)
    # pathlib treats the last dot-token in a directory name as a suffix
    # (e.g. ``.x264-GROUP``); append a harmless extension so the complete
    # torrent label reaches the existing filename parser intact.
    name = resource.name if resource_path.is_file() else resource.name + ".mkv"
    return filename_hints(Path(name))


def _batch_prepare_hints(resource: Resource) -> tuple[str | None, str | None, str | None]:
    """Prefer the complete torrent label over lossy inner-file labels.

    Some releases use generic media filenames (for example ``rem-1080p.mkv``),
    so their source/edition/platform can only be recovered from the torrent
    directory.  An explicit x264/x265 tag also distinguishes a BluRay encode
    from a disc/remux and prevents the disc codec mapping from emitting AVC or
    HEVC for a compressed release.
    """
    hints = _resource_name_hints(resource)
    if Path(resource.path).is_file():
        return hints.source, hints.edition, hints.platform

    label = resource.name.replace("_", " ").replace(".", " ")
    source = hints.source
    encoded_bluray = bool(
        source in {"BluRay", "UHD BluRay"}
        and re.search(r"\b(?:X264|X265|H[ .]?264|H[ .]?265)\b", label, re.I)
    )
    if encoded_bluray:
        source = "UHD BluRay BDRip" if source == "UHD BluRay" else "BluRay BDRip"

    # Empty CLI overrides intentionally suppress misleading tokens parsed
    # from a generic inner filename, while preserving root-level values.
    edition = hints.edition if source in {"BluRay", "UHD BluRay"} else ""
    return source, edition, hints.platform or ""


def _selected_group(resource: Resource) -> tuple[str, str]:
    root_group = _resource_name_hints(resource).group
    if root_group:
        return root_group, "torrent_name"
    source_files = [
        (
            Path(resource.path) / row["relative_path"]
            if Path(resource.path).is_dir()
            else Path(resource.path)
        )
        for row in resource.files
        if Path(row["relative_path"]).suffix.casefold() in REAL_VIDEO_EXTENSIONS
    ]
    if not source_files:
        source_files = [Path(resource.input_path)]
    groups = [filename_hints(path).group for path in source_files]
    counts = Counter(group for group in groups if group)
    if not counts:
        return "NOGRP", "default"
    group, count = counts.most_common(1)[0]
    if count / len(groups) >= 0.6:
        return group, "filename"
    return "", "conflicting_groups"


def _missing_fields(package: dict[str, Any], *, expected_year: str | None = None) -> list[str]:
    missing: list[str] = []
    for field_name in ("title", "subtitle", "category", "douban_url", "group", "technical_info_text"):
        if not str(package.get(field_name) or "").strip():
            missing.append(field_name)
    media = package.get("media") if isinstance(package.get("media"), dict) else {}
    resolution = str(media.get("resolution") or "").strip()
    if resolution not in ALLOWED_RESOLUTIONS:
        missing.append("resolution (需为 360/480/540/720/1080/2160p 之一)")
    for field_name in ("video_codec", "audio_codec"):
        if not str(media.get(field_name) or "").strip():
            missing.append(field_name)
    if not package.get("tmdb"):
        # TMDB is useful provenance, but absence alone is not a hard blocker
        # when the Douban identity and media data are otherwise verified.
        package.setdefault("quality_notes", []).append("未获得 TMDB 条目")
    elif expected_year:
        tmdb = package.get("tmdb") if isinstance(package.get("tmdb"), dict) else {}
        tmdb_year = str(tmdb.get("year") or "")
        tmdb_year_match = re.search(r"\b((?:19|20)\d{2})\b", tmdb_year)
        if tmdb_year_match and abs(int(tmdb_year_match.group(1)) - int(expected_year)) > 1:
            missing.append(f"TMDB 年份冲突（资源名 {expected_year}，TMDB {tmdb_year_match.group(1)}）")
    release_name = str(package.get("release_name") or package.get("title") or "")
    title_year = re.search(r"\b((?:19|20)\d{2})\b", release_name)
    if expected_year and title_year and abs(int(title_year.group(1)) - int(expected_year)) > 1:
        missing.append(f"发布标题年份冲突（资源名 {expected_year}，标题 {title_year.group(1)}）")
    evidence = package.get("identification_evidence") if isinstance(package.get("identification_evidence"), dict) else {}
    identity = evidence.get("identity") if isinstance(evidence.get("identity"), dict) else {}
    try:
        confidence = float(identity.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if not identity or confidence < 0.70:
        missing.append("可验证的大模型作品身份（置信度至少 70%）")
    douban = package.get("douban_match") if isinstance(package.get("douban_match"), dict) else {}
    if not douban.get("id") or not douban.get("year") or not (douban.get("title") or douban.get("original_title")):
        missing.append("豆瓣候选的标题/年份/条目 ID 证据")
    elif douban.get("source") not in {"suggest", "html_search"}:
        missing.append("可追溯的自动豆瓣候选来源")
    if package.get("kind") == "tv":
        tmdb = package.get("tmdb") or {}
        if douban.get("season_number") is None and str(douban.get("year", "")).isdigit() and str(tmdb.get("year", "")).isdigit():
            if abs(int(douban["year"]) - int(tmdb["year"])) > 1:
                missing.append("豆瓣系列年份与 TMDB 不符，不能使用旧版同名条目")
        source_name = Path(str(package.get("input_path") or "")).name
        declared = season_number_hint(source_name)
        prepared = season_number_hint(str(package.get("episode") or ""))
        if declared and prepared and declared != prepared:
            missing.append("源目录声明季号与资料包季号不一致")
        mapping = package.get("episode_mapping") or {}
        if re.search(r"\b(?:the\s+)?final\b", source_name, re.I) and mapping.get("source") == "inferred_single_season":
            missing.append("Final 季号尚未确认，不能默认按 S01 发布")
    return missing


def _douban_issue(item: dict[str, Any]) -> str:
    package = item.get("package") if isinstance(item.get("package"), dict) else {}
    if package.get("douban_url"):
        return "matched"
    diagnostics = " ".join(str(value) for value in item.get("douban_diagnostics") or [])
    error = str(item.get("error") or "")
    text = diagnostics + " " + error
    if re.search(r"HTTP\s*429|访问频率过高|搜索访问太频繁|验证码|安全验证", text, re.I):
        return "rate_limited_or_challenge"
    if re.search(r"(?<!非 )HTTP\s*403|Cookie.*(?:失效|无效|未能加载)|没有有效的 douban\.com Cookie", text, re.I):
        return "blocked_or_cookie"
    if re.search(r"超时|timed out|timeout", text, re.I):
        return "network_timeout"
    if "网页搜索正常返回 0 个候选" in text:
        return "search_completed_zero_candidates"
    if "HTTP 200 返回空结果" in text:
        return "suggest_empty_uncertain"
    if item.get("douban_candidate_count", 0):
        return "candidate_identity_unconfirmed"
    return "no_reliable_candidate_or_search_diagnostic"


def _redact_log(text: str) -> str:
    text = re.sub(r"(?im)^(cookie|set-cookie|authorization|x-api-key)\s*[:=].*$", r"\1: [REDACTED]", text)
    text = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(passkey|api[_-]?key|token)(\s*[=:]\s*)[^\s,;]+", r"\1\2[REDACTED]", text)
    text = re.sub(
        r"https?://[^\s<>\"']+",
        lambda match: re.sub(r"[?#].*$", "?[REDACTED_QUERY]", match.group(0))
        if re.search(r"(?i)(passkey|token|auth|sign|key|download)", match.group(0))
        else match.group(0),
        text,
    )
    return text


def _item_output_path(items_dir: Path, resource: Resource) -> Path:
    slug = re.sub(r"[^\w.-]+", "_", resource.name, flags=re.UNICODE).strip("_.")[:90] or "resource"
    return items_dir / f"{slug}--{resource.resource_id}"


def _run_one(
    resource: Resource,
    *,
    output_dir: Path,
    repository: Path,
    cookie_file: Path | None,
    timeout: int,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "resource_id": resource.resource_id,
        "name": resource.name,
        "relative_path": resource.relative_path,
        "source_path": resource.path,
        "size_bytes": resource.size_bytes,
        "file_count": resource.file_count,
        "files": resource.files,
        "symlinks": resource.symlinks,
        "classification": resource.classification,
        "media_kind": resource.media_kind,
        "notes": list(resource.notes),
        "status": "pending",
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "artifacts": {},
    }
    if resource.classification not in {"movie", "tv"}:
        record["status"] = resource.classification
        return record

    item_dir = _item_output_path(output_dir / "items", resource)
    item_dir.mkdir(parents=True, exist_ok=True)
    record["artifacts"]["directory"] = str(item_dir)
    record["artifacts"]["result"] = str(item_dir / "result.json")
    if resource.classification == "needs_review":
        record["status"] = "needs_review"
        return record
    if not resource.input_path:
        record["status"] = "needs_review"
        record["error"] = "没有安全、明确的单文件或剧集目录输入"
        return record
    try:
        before = _source_snapshot(resource)
    except OSError as exc:
        record["status"] = "failed"
        record["error"] = f"源文件 stat 失败：{type(exc).__name__}"
        return record

    group, group_source = _selected_group(resource)
    if group_source == "conflicting_groups":
        record["status"] = "needs_review"
        record["error"] = "剧集文件名包含多个相互冲突的发布组，未自动选择"
        return record

    args = [
        sys.executable,
        "-c",
        "from media_title_renamer.prepare import main; import sys; main(sys.argv[1:])",
        resource.input_path,
        "--gpt",
        "--web-search",
        "--remain",
        "--allow-inferred-episodes",
        "--exclude-sample-content",
        "--skip-screenshots",
        "--skip-torrent",
        "--kind",
        resource.media_kind,
        "--group",
        group,
        "--output",
        str(item_dir),
    ]
    source_hint, edition_hint, platform_hint = _batch_prepare_hints(resource)
    if source_hint:
        args.extend(["--source", source_hint])
    if edition_hint is not None:
        args.extend(["--edition", edition_hint])
    if platform_hint is not None:
        args.extend(["--platform", platform_hint])
    env = os.environ.copy()
    src_dir = str(repository / "src")
    env["PYTHONPATH"] = src_dir + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    temp_dir = output_dir / "worker-tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = str(temp_dir)
    env["TMP"] = str(temp_dir)
    env["TEMP"] = str(temp_dir)
    if cookie_file:
        env["DOUBAN_COOKIE_FILE"] = str(cookie_file)
    try:
        completed = subprocess.run(
            args,
            cwd=repository,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        stdout = _redact_log(completed.stdout or "")
        stderr = _redact_log(completed.stderr or "")
        (item_dir / "prepare.stdout.log").write_text(stdout, encoding="utf-8")
        (item_dir / "prepare.stderr.log").write_text(stderr, encoding="utf-8")
        record["artifacts"]["stdout_log"] = str(item_dir / "prepare.stdout.log")
        record["artifacts"]["stderr_log"] = str(item_dir / "prepare.stderr.log")
        record["exit_code"] = completed.returncode
        package_path = item_dir / "mteam-prepare.json"
        if package_path.is_file():
            package = json.loads(package_path.read_text(encoding="utf-8"))
            record["package"] = package
            record["artifacts"]["package"] = str(package_path)
            missing = _missing_fields(package, expected_year=_resource_name_hints(resource).year)
            record["group_source"] = group_source
            evidence = package.get("identification_evidence") or {}
            record["douban_cookie_used"] = bool(evidence.get("douban_cookie_used"))
            record["douban_candidate_count"] = len(evidence.get("douban_candidates") or [])
            record["douban_diagnostics"] = evidence.get("douban_diagnostics") or []
            record["ai_identity"] = evidence.get("identity")
            identity = evidence.get("identity") if isinstance(evidence.get("identity"), dict) else {}
            if identity.get("kind") and identity.get("kind") != resource.media_kind:
                missing.append(f"大模型类型冲突：模型={identity.get('kind')}，资源={resource.media_kind}")
            record["missing_fields"] = missing
            record["status"] = "success" if completed.returncode == 0 and not missing else "needs_review"
            douban_match = package.get("douban_match") if isinstance(package.get("douban_match"), dict) else {}
            record["field_evidence"] = {
                "title": "CLIProxyAPI 身份识别 + 文件名规则" if identity else "文件名规则",
                "subtitle": "TMDB 原始语言/豆瓣条目 + 本地音轨语言",
                "douban_url": f"豆瓣 {douban_match.get('source') or '候选'}",
                "category": "media-title-rename 分类规则 + MediaInfo 分辨率/来源",
                "resolution": "MediaInfo 实测画面尺寸",
                "video_codec": "MediaInfo 实测视频轨",
                "audio_codec": "MediaInfo 实测音频轨",
                "group": f"{group_source}；原名没有明确制作组时采用 NOGRP",
                "year_and_kind": "CLIProxyAPI 身份识别 + TMDB/豆瓣候选核对",
            }
            record["preview_scope"] = "metadata_only"
            record["not_generated"] = [
                "发布截图（本轮为减少重型视频解码而跳过）",
                "发布种子（本轮明确禁止生成或上传）",
            ]
            if missing:
                record["error"] = "资料尚未满足校验字段：" + ", ".join(missing)
        else:
            record["status"] = "failed"
            record["error"] = (stderr.strip() or stdout.strip() or f"prepare 退出码 {completed.returncode}")[-2000:]
            diagnostic = re.search(r"豆瓣查询诊断[：:]\s*(.+)", stdout + "\n" + stderr)
            if diagnostic:
                record["douban_diagnostics"] = [diagnostic.group(1).strip()[:1600]]
                record["douban_candidate_count"] = 0
        if RATE_LIMIT.search(stdout + "\n" + stderr):
            record["rate_limited"] = True
        record["douban_failure_type"] = _douban_issue(record)
    except subprocess.TimeoutExpired as exc:
        record["status"] = "failed"
        record["error"] = f"prepare 超时（>{timeout}s）；保留已生成产物，可检查 worker 日志后重试"
        partial_out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        partial_err = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        (item_dir / "prepare.stdout.log").write_text(_redact_log(partial_out), encoding="utf-8")
        (item_dir / "prepare.stderr.log").write_text(_redact_log(partial_err), encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"

    try:
        after = _source_snapshot(resource)
        if before != after:
            record["status"] = "failed"
            record["error"] = "安全校验失败：处理期间源文件的设备/inode/大小/mtime 发生变化"
    except OSError as exc:
        record["status"] = "failed"
        record["error"] = f"处理后源文件无法 stat：{type(exc).__name__}"
    record["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write_json(item_dir / "result.json", record)
    return record


def _load_results(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    result: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("resource_id"):
                result[str(item["resource_id"])] = item
    return result


def _write_reports(
    run_dir: Path,
    resources: list[Resource],
    results: dict[str, dict[str, Any]],
    stop_reason: str = "",
    selected_ids: set[str] | None = None,
) -> None:
    ordered = [results[item.resource_id] for item in resources if item.resource_id in results]
    jsonl = run_dir / "results.jsonl"
    temporary = jsonl.with_name(jsonl.name + ".writing")
    with temporary.open("w", encoding="utf-8") as stream:
        for result in ordered:
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
    temporary.replace(jsonl)
    counts = Counter(item.get("status", "unknown") for item in ordered)
    eligible = [item for item in resources if item.classification in {"movie", "tv"}]
    selected_ids = selected_ids if selected_ids is not None else {item.resource_id for item in eligible}
    selected = [item for item in eligible if item.resource_id in selected_ids]
    supported = [
        item
        for item in ordered
        if item.get("classification") in {"movie", "tv"}
        and item.get("resource_id") in selected_ids
        and item.get("status") not in {"movie", "tv", "not_processed"}
    ]
    successes = sum(item.get("status") == "success" for item in supported)
    success_pct = successes / len(selected) * 100 if selected else 0.0
    kind_counts = Counter(item.media_kind for item in selected)
    inventory_counts = Counter(item.classification for item in resources)
    summary_lines = [
        "# TL 影视发布资料批处理摘要",
        "",
        f"- 生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- 扫描资源根条目：{len(resources)}",
        f"- 电影/电视剧资源：{len(eligible)}（电影 {sum(item.media_kind == 'movie' for item in eligible)}，电视剧 {sum(item.media_kind == 'tv' for item in eligible)}）",
        f"- 本次选择处理：{len(selected)}；已完成尝试：{len(supported)}（选择中电影 {kind_counts.get('movie', 0)}，电视剧 {kind_counts.get('tv', 0)}）",
        f"- 本次选择范围内资料验证通过：{successes}/{len(selected)}（{success_pct:.1f}%）",
        f"- 待确认：{counts.get('needs_review', 0)}；失败：{counts.get('failed', 0)}；超 200 GiB：{counts.get('oversize', 0)}；不支持：{counts.get('unsupported_type', 0)}",
        f"- 分类盘点：{', '.join(f'{key} {value}' for key, value in sorted(inventory_counts.items()))}",
        f"- 豆瓣 Cookie 使用次数：{sum(bool(item.get('douban_cookie_used')) for item in supported)}",
        f"- 大模型识别记录数：{sum(bool(item.get('ai_identity')) for item in supported)}",
        f"- 豆瓣未匹配原因：{dict(Counter(item.get('douban_failure_type', _douban_issue(item)) for item in supported if item.get('douban_failure_type', _douban_issue(item)) != 'matched'))}",
        "- 发布：未执行；qB：未访问/未添加任务；TL 源：仅做设备、inode、大小、mtime 前后核对。",
    ]
    if stop_reason:
        summary_lines.extend(["", f"- 停止原因：{stop_reason}"])
    (run_dir / "summary.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    unsupported = [item for item in ordered if item.get("status") in {"unsupported_type", "oversize"}]
    review_or_failed = [item for item in ordered if item.get("status") in {"needs_review", "failed"}]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in review_or_failed:
        reason = str(item.get("error") or "; ".join(item.get("notes") or []) or item.get("status"))
        if item.get("douban_diagnostics") and not item.get("package", {}).get("douban_url"):
            reason += "；豆瓣诊断：" + "；".join(item["douban_diagnostics"][:3])
        grouped.setdefault(reason, []).append(item)
    failure_lines = ["# 待确认、失败及不支持样本", ""]
    if not grouped:
        failure_lines.append("没有待确认或失败的影视样本。")
    for reason, items in sorted(grouped.items()):
        failure_lines.extend([f"## {reason}", ""])
        failure_lines.extend(f"- `{item['name']}` — `{item['source_path']}`" for item in items)
        failure_lines.append("")
    douban_failures = [
        item for item in ordered
        if item.get("classification") in {"movie", "tv"}
        and item.get("resource_id") in selected_ids
        and _douban_issue(item) != "matched"
    ]
    if douban_failures:
        failure_lines.extend(["## 豆瓣搜索与匹配诊断分类", ""])
        by_type: dict[str, list[dict[str, Any]]] = {}
        for item in douban_failures:
            by_type.setdefault(item.get("douban_failure_type") or _douban_issue(item), []).append(item)
        for failure_type, items in sorted(by_type.items()):
            failure_lines.extend([f"### {failure_type}（{len(items)}）", ""])
            for item in items:
                evidence = "；".join(item.get("douban_diagnostics") or []) or item.get("error") or "无候选详情"
                failure_lines.append(f"- `{item['name']}`：{evidence[:500]}")
            failure_lines.append("")
    if unsupported:
        failure_lines.extend(["## 非影视/仅盘点", ""])
        failure_lines.extend(
            f"- `{item['name']}` — {item.get('classification')}: {'；'.join(item.get('notes') or [])}"
            for item in unsupported
        )
    (run_dir / "failures.md").write_text("\n".join(failure_lines) + "\n", encoding="utf-8")

    changes_lines = [
        "# 本轮代码迭代与验证记录",
        "",
        "- 豆瓣：登录 Cookie 从指定 XLSX/Netscape 文件在内存加载；建议接口无可靠同年份候选时，限量回退豆瓣网页搜索。",
        "- 消歧：大模型只能从真实豆瓣候选中选择；电影候选受标题及年份约束，证据保存候选来源与选择。",
        "- 电视剧：增加保守的连续集数映射，只有模型以至少 80% 置信度确认 TV，且文件序列唯一连续时才推断季集号。",
        "- 附加样片：Sample/Proof/Trailer 等目录不送入主剧集包；压缩包主导且含样片的视频资源明确过滤，不解压或处理。",
        "- 安全：该批处理只调用 prepare 的 --remain/--skip-screenshots/--skip-torrent；不发布、不访问 qB、不改动 TL 源。",
        "- 自动化测试：完整 pytest 结果由本轮控制台记录；未创建 Git commit。",
        "",
    ]
    (run_dir / "changes.md").write_text("\n".join(changes_lines), encoding="utf-8")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只读扫描 TL 目录并批量生成 M-Team 影视资料预览")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output-dir", type=Path, help="本次运行目录；必须位于 batch-metadata 下")
    parser.add_argument("--cookie-file", type=Path, help="豆瓣 Netscape/XLSX Cookie；默认使用已配置的环境变量或本机 douban.xlsx")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个电影/电视剧，用于小批量验证；0 表示全量")
    parser.add_argument("--only", action="append", default=[], help="只处理名称/相对路径包含该字符串的样本；可重复传入")
    parser.add_argument("--delay", type=float, default=60.0, help="两个资源之间的最小间隔秒数（默认 60 秒）")
    parser.add_argument("--timeout", type=int, default=240, help="单资源 prepare 超时秒数")
    parser.add_argument("--resume", action="store_true", help="续跑 output-dir 中尚未有结果的条目")
    parser.add_argument("--retry-failed", action="store_true", help="与 --resume 一起重试失败/待确认的影视条目")
    parser.add_argument("--retry-status", action="append", default=[], help="与 --resume 一起重跑指定状态，可重复传入")
    parser.add_argument("--stop-on-rate-limit", action="store_true", default=True, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    root = args.root.expanduser().resolve(strict=True)
    repository = Path(__file__).resolve().parents[2]
    if args.output_dir:
        run_dir = args.output_dir.expanduser().resolve()
        try:
            run_dir.relative_to(DEFAULT_OUTPUT_ROOT.resolve())
        except ValueError as exc:
            raise SystemExit(f"output-dir 必须位于 {DEFAULT_OUTPUT_ROOT}") from exc
    else:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = DEFAULT_OUTPUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    cookie_file = args.cookie_file.expanduser().resolve() if args.cookie_file else None
    default_cookie = Path("/data/Zhyw/media-stack/moviepilot-maintenance/douban.xlsx")
    if cookie_file is None and os.environ.get("DOUBAN_COOKIE_FILE", "").strip():
        cookie_file = Path(os.environ["DOUBAN_COOKIE_FILE"]).expanduser().resolve()
    elif cookie_file is None and default_cookie.is_file():
        cookie_file = default_cookie.resolve()

    temp_dir = run_dir / "worker-tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(temp_dir)
    os.environ["TMP"] = str(temp_dir)
    os.environ["TEMP"] = str(temp_dir)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

    print(f"[批量资料] TL 根目录：{root}")
    print(f"[批量资料] 输出目录：{run_dir}")
    print("[批量资料] qB、M-Team 发布接口均不会调用；源文件只读。")
    resources = scan_resources(root)
    inventory_path = run_dir / "inventory.jsonl"
    with inventory_path.open("w", encoding="utf-8") as stream:
        for resource in resources:
            stream.write(json.dumps(asdict(resource), ensure_ascii=False) + "\n")
    print(f"[批量资料] 顶层资源 {len(resources)} 条；分类：{dict(Counter(r.classification for r in resources))}")

    targets = [item for item in resources if item.classification in {"movie", "tv"}]
    if args.only:
        needles = [value.casefold() for value in args.only]
        targets = [item for item in targets if any(term in (item.name + " " + item.relative_path).casefold() for term in needles)]
    if args.limit > 0:
        targets = targets[:args.limit]
    results_path = run_dir / "results.jsonl"
    results = _load_results(results_path) if args.resume else {}
    retries = set(args.retry_status)
    if args.retry_failed:
        retries.update({"failed", "needs_review"})
    pending = []
    for item in resources:
        if item not in targets:
            if item.resource_id not in results:
                results[item.resource_id] = {
                    "resource_id": item.resource_id,
                    "name": item.name,
                    "relative_path": item.relative_path,
                    "source_path": item.path,
                    "size_bytes": item.size_bytes,
                    "file_count": item.file_count,
                    "files": item.files,
                    "classification": item.classification,
                    "media_kind": item.media_kind,
                    "notes": item.notes,
                    "status": "not_processed" if item.classification in {"movie", "tv"} else item.classification,
                    "artifacts": {},
                }
            continue
        old = results.get(item.resource_id)
        if old and args.resume and old.get("status") not in retries:
            continue
        pending.append(item)

    print(f"[批量资料] 本轮处理影视样本：{len(pending)}")
    stop_reason = ""
    for index, resource in enumerate(pending, start=1):
        print(f"[{index}/{len(pending)}] {resource.name} ({resource.media_kind}, {resource.size_bytes / 1024**3:.2f} GiB)", flush=True)
        result = _run_one(
            resource,
            output_dir=run_dir,
            repository=repository,
            cookie_file=cookie_file,
            timeout=args.timeout,
        )
        results[resource.resource_id] = result
        _write_reports(
            run_dir,
            resources,
            results,
            selected_ids={item.resource_id for item in targets},
        )
        print(f"  -> {result['status']}: {result.get('error') or result.get('package', {}).get('douban_url') or '资料字段已记录'}", flush=True)
        if result.get("rate_limited") and args.stop_on_rate_limit:
            stop_reason = "豆瓣/搜索站返回明确限流或安全验证信号；已停止后续网络查询，可冷却后用 --resume 继续。"
            print(f"[批量资料] {stop_reason}")
            break
        if args.delay > 0 and index < len(pending):
            time.sleep(args.delay)
    _write_reports(
        run_dir,
        resources,
        results,
        stop_reason=stop_reason,
        selected_ids={item.resource_id for item in targets},
    )
    print(f"[批量资料] 完成。摘要：{run_dir / 'summary.md'}")
    print(f"[批量资料] 结果：{run_dir / 'results.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
