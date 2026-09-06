from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


VIDEO_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".iso",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ts",
    ".vob",
    ".webm",
    ".wmv",
}

SOURCE_CHOICES = (
    "BluRay",
    "UHD BluRay",
    "BluRay REMUX",
    "UHD BluRay REMUX",
    "BluRay BDRip",
    "UHD BluRay BDRip",
    "WEB-DL",
    "HDTV",
    "DVD5",
    "DVD9",
)

SOURCE_ALIASES = {
    "bluray": "BluRay",
    "blu ray": "BluRay",
    "uhd bluray": "UHD BluRay",
    "uhd blu ray": "UHD BluRay",
    "bluray remux": "BluRay REMUX",
    "blu ray remux": "BluRay REMUX",
    "uhd bluray remux": "UHD BluRay REMUX",
    "uhd blu ray remux": "UHD BluRay REMUX",
    "bluray bdrip": "BluRay BDRip",
    "blu ray bdrip": "BluRay BDRip",
    "uhd bluray bdrip": "UHD BluRay BDRip",
    "uhd blu ray bdrip": "UHD BluRay BDRip",
    "web-dl": "WEB-DL",
    "webdl": "WEB-DL",
    "hdtv": "HDTV",
    "dvd": "DVD",
    "dvd iso": "DVD",
    "dvdiso": "DVD",
    "dvd d5": "DVD5",
    "dvd5": "DVD5",
    "d5": "DVD5",
    "dvd d9": "DVD9",
    "dvd9": "DVD9",
    "d9": "DVD9",
}

PLATFORM_PATTERNS = (
    (r"\bAMZN\b|\bAMAZON\b", "AMZN"),
    (r"\bNETFLIX\b|\bNF\b", "Netflix"),
    (r"\bDSNP\b|\bDISNEY[ +.]?PLUS\b", "Disney+"),
    (r"\bHMAX\b|\bHBOMAX\b|\bMAX\b", "HMAX"),
    (r"\bATVP\b|\bAPPLE[ +.]?TV\b", "ATVP"),
    (r"\bHULU\b", "Hulu"),
    (r"\bITUNES\b", "iTunes"),
    (r"\bCATCHPLAY\b", "CATCHPLAY"),
    (r"\bYOUKU\b", "YOUKU"),
    (r"\bIQIYI\b|\bIQ\b", "iQIYI"),
    (r"\bBAHA\b", "Baha"),
)


@dataclass(frozen=True)
class MediaInfo:
    width: int
    height: int
    resolution: str
    video_format: str
    writing_library: str
    video_codec: str
    hdr: tuple[str, ...]
    hfr: str | None
    audio_codec: str
    audio_channels: str | None
    audio_tracks: int
    audio_bitrate: int
    scan_type: str = ""
    scan_order: str = ""
    audio_language: str = ""


@dataclass(frozen=True)
class FilenameHints:
    title: str | None
    year: str | None
    edition: str | None
    episode: str | None
    source: str | None
    group: str | None
    platform: str | None


def _normalise_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _field(track: dict[str, Any], *names: str) -> str:
    normalised = {_normalise_key(str(key)): value for key, value in track.items()}
    for name in names:
        value = normalised.get(_normalise_key(name))
        if value not in (None, ""):
            return str(value)
    return ""


def _integer(value: str) -> int:
    match = re.search(r"\d+", value.replace("\u202f", "").replace(" ", ""))
    return int(match.group()) if match else 0


def _number(value: str) -> float:
    match = re.search(r"\d+(?:[.,]\d+)?", value.replace(" ", ""))
    return float(match.group().replace(",", ".")) if match else 0.0


def _resolution(width: int, height: int, scan_type: str = "", scan_order: str = "") -> str:
    # Letterboxed films often have a height below their nominal raster; width is
    # therefore the more useful tie-breaker for common HD/UHD releases.
    interlaced = "interlac" in scan_type.lower() or scan_order.upper() in {"TFF", "BFF"}
    suffix = "i" if interlaced else "p"
    if width >= 7000 or height >= 4000:
        return f"4320{suffix}"
    if width >= 3500 or height >= 2000:
        return f"2160{suffix}"
    if width >= 2500 or height >= 1350:
        return f"1440{suffix}"
    if width >= 1800 or height >= 1000:
        return f"1080{suffix}"
    if width >= 1100 or height >= 650:
        return f"720{suffix}"
    if height:
        return f"{height}{suffix}"
    return "未知分辨率"


def _has_any(text: str, *patterns: str) -> bool:
    upper = text.upper()
    return any(pattern.upper() in upper for pattern in patterns)


def _video_codec(video_format: str, writing_library: str, source: str | None) -> str:
    format_upper = video_format.upper()
    library_upper = writing_library.upper()
    disc_or_remux = bool(source and (source.endswith("REMUX") or source in {"BluRay", "UHD BluRay"}))

    if "AVC" in format_upper or "H.264" in format_upper:
        if disc_or_remux:
            return "AVC"
        return "x264" if "X264" in library_upper else "H.264"
    if "HEVC" in format_upper or "H.265" in format_upper:
        if disc_or_remux:
            return "HEVC"
        return "x265" if "X265" in library_upper else "H.265"
    if "MPEG-2" in format_upper or "MPEG 2" in format_upper or ("MPEG VIDEO" in format_upper and "2" in format_upper):
        return "MPEG2"
    if "VC-1" in format_upper or "VC1" in format_upper:
        return "VC-1"
    if "AV1" in format_upper:
        return "AV1"
    if "VP9" in format_upper:
        return "VP9"
    return video_format or "未知视频编码"


def _hdr_tokens(track: dict[str, Any]) -> tuple[str, ...]:
    description = " ".join(
        _field(
            track,
            "HDR_Format",
            "HDR_Format_String",
            "HDR_Format_Compatibility",
            "transfer_characteristics",
            "colour_primaries",
            "ColorPrimaries",
        ).upper()
        for track in (track,)
    )
    is_dovi = _has_any(description, "DOLBY VISION", "DOVI", "DVHE", "DV.HE")
    is_hdr10_plus = _has_any(description, "HDR10+")
    is_hdr10 = _has_any(description, "HDR10", "ST 2084", "SMPTE2084", "PQ")
    is_hlg = "HLG" in description or "ARIB-STD-B67" in description

    result: list[str] = []
    if is_hdr10_plus:
        result.append("HDR10+")
    elif is_hdr10:
        result.append("HDR10")
    elif is_hlg:
        result.append("HLG")
    if is_dovi:
        result.append("DoVi")
    return tuple(result)


def _audio_codec(track: dict[str, Any]) -> str:
    description = " ".join(
        value
        for value in (
            _field(track, "Format"),
            _field(track, "Format_Profile"),
            _field(track, "CommercialName", "Format_Commercial_IfAny"),
            _field(track, "Format_AdditionalFeatures"),
        )
        if value
    ).upper()

    if _has_any(description, "DTS:X", "DTS-X"):
        return "DTS-X"
    if "DTS" in description and _has_any(description, "MASTER", " MA", "MA /", "MA / CORE"):
        return "DTS-HD MA"
    if "DTS-HD" in description and _has_any(description, "HIGH RESOLUTION", "HRA"):
        return "DTS-HD HRA"
    if "TRUEHD" in description or "TRUE HD" in description:
        return "TrueHD Atmos" if "ATMOS" in description else "TrueHD"
    if _has_any(description, "E-AC-3", "EAC3", "DOLBY DIGITAL PLUS"):
        return "DDP Atmos" if "ATMOS" in description else "DDP"
    if _has_any(description, "AC-3", "AC3", "DOLBY DIGITAL"):
        return "DD"
    if "LPCM" in description or "PCM" in description:
        return "LPCM"
    if "FLAC" in description:
        return "FLAC"
    if "OPUS" in description:
        return "Opus"
    if "AAC" in description:
        return "AAC"
    if "DTS" in description:
        return "DTS"
    if "MPEG AUDIO" in description or "MP3" in description:
        return "MP3"
    return _field(track, "Format") or "未知音频编码"


def _audio_channels(track: dict[str, Any]) -> str | None:
    positions = _field(track, "ChannelPositions", "ChannelPositions_String2")
    direct = re.search(r"(\d+)\s*/\s*(\d+)\s*/\s*(\d+)(?:\.(\d+))?", positions)
    if direct:
        main_channels = sum(int(value) for value in direct.group(1, 2, 3))
        lfe = int(direct.group(4) or 0)
        return f"{main_channels}.{lfe}"

    channel_count = _integer(_field(track, "Channel(s)", "Channels"))
    common_layouts = {1: "1.0", 2: "2.0", 3: "2.1", 4: "3.1", 5: "4.1", 6: "5.1", 7: "6.1", 8: "7.1"}
    return common_layouts.get(channel_count) or (f"{channel_count}.0" if channel_count else None)


def _audio_bitrate(track: dict[str, Any]) -> int:
    return _integer(_field(track, "BitRate", "BitRate_Nominal", "BitRate_Maximum"))


def _select_primary_audio(tracks: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not tracks:
        return None
    # The M-Team rule calls for the highest-bitrate audio track when multiple
    # tracks exist. Keep input order as a deterministic tie-breaker.
    return max(tracks, key=_audio_bitrate)


def inspect_media(data: dict[str, Any], source: str | None = None) -> MediaInfo:
    """Turn a MediaInfo JSON object into the fields used in an upload title."""
    tracks = data.get("media", {}).get("track", [])
    if not isinstance(tracks, list):
        raise ValueError("MediaInfo JSON 中找不到轨道列表")
    video = next((track for track in tracks if _field(track, "@type").lower() == "video"), None)
    if not isinstance(video, dict):
        raise ValueError("未在文件中找到视频轨")
    audio_tracks = [track for track in tracks if _field(track, "@type").lower() == "audio"]
    primary_audio = _select_primary_audio(audio_tracks)

    width = _integer(_field(video, "Width"))
    height = _integer(_field(video, "Height"))
    frame_rate = _number(_field(video, "FrameRate"))
    hfr = f"{round(frame_rate):.0f}Fps" if frame_rate >= 50 else None
    video_format = _clean_spaces(" ".join(value for value in (_field(video, "Format"), _field(video, "Format_Version")) if value))
    writing_library = _field(video, "WritingLibrary", "Encoded_Library_Name", "Writing_library")
    scan_type = _field(video, "ScanType", "Scan type")
    scan_order = _field(video, "ScanOrder", "Scan order")

    return MediaInfo(
        width=width,
        height=height,
        resolution=_resolution(width, height, scan_type, scan_order),
        video_format=video_format,
        writing_library=writing_library,
        video_codec=_video_codec(video_format, writing_library, source),
        hdr=_hdr_tokens(video),
        hfr=hfr,
        audio_codec=_audio_codec(primary_audio) if primary_audio else "",
        audio_channels=_audio_channels(primary_audio) if primary_audio else None,
        audio_tracks=len(audio_tracks),
        audio_bitrate=_audio_bitrate(primary_audio) if primary_audio else 0,
        scan_type=scan_type,
        scan_order=scan_order,
        audio_language=_field(primary_audio, "Language") if primary_audio else "",
    )


def _clean_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _clean_title(value: str) -> str:
    # M-Team's main title prohibits ! ; . # $ % : and, except where they are
    # part of the official work title, brackets. We favour a safe upload title.
    value = value.replace("_", " ").replace(".", " ")
    value = re.sub(r"[!;#$%:<>\"/\\|?*]", " ", value)
    value = re.sub(r"[\[\](){}]", " ", value)
    return _clean_spaces(value)


def _clean_component(value: str) -> str:
    value = re.sub(r"[<>:\"/\\|?*]", " ", value)
    return _clean_spaces(value)


def _canonical_source(value: str | None) -> str | None:
    if not value:
        return None
    compact = _clean_spaces(value.replace("_", " ").replace(".", " ")).lower()
    return SOURCE_ALIASES.get(compact, _clean_component(value))


def _detect_platform(stem: str) -> str | None:
    upper = stem.upper().replace("_", " ").replace(".", " ")
    for pattern, platform in PLATFORM_PATTERNS:
        if re.search(pattern, upper, flags=re.IGNORECASE):
            return platform
    return None


def _dvd_disc_label(file_size: int | None) -> str | None:
    if not file_size:
        return None
    # Nominal single- and dual-layer DVD capacities are expressed in decimal
    # bytes. A small tolerance handles authored images with padding/overhead.
    if file_size <= 4_900_000_000:
        return "DVD5"
    if file_size <= 9_000_000_000:
        return "DVD9"
    return None


def _infer_source(
    stem: str,
    extension: str,
    media: MediaInfo | None = None,
    file_size: int | None = None,
) -> str | None:
    tokens = stem.upper().replace("_", " ").replace(".", " ")
    is_uhd = bool(re.search(r"\bUHD\b|\b2160P\b|\b4K\b", tokens))
    is_blu_ray = bool(re.search(r"\bBLU[ -]?RAY\b|\bBDRIP\b", tokens))
    is_bdrip = bool(re.search(r"\bBDRIP\b", tokens))

    if re.search(r"\bWEB[ -]?DL\b", tokens):
        return "WEB-DL"
    if re.search(r"\bHDTV\b", tokens):
        return "HDTV"
    if re.search(r"\bDVD(?:ISO|RIP)?\b", tokens):
        disc_label = _dvd_disc_label(file_size) if extension.lower() == ".iso" else None
        return disc_label or "DVD"
    if re.search(r"\bREMUX\b", tokens):
        return "UHD BluRay REMUX" if is_uhd else "BluRay REMUX"
    if is_bdrip:
        return "UHD BluRay BDRip" if is_uhd else "BluRay BDRip"
    if is_blu_ray:
        # An ISO is a complete authored disc image. Codec-looking filename
        # tags such as x265 describe its HEVC stream and must not turn it into
        # a BDRip encode, otherwise prepare would incorrectly skip BDInfo.
        if extension.lower() == ".iso":
            return "UHD BluRay" if is_uhd else "BluRay"
        # An explicit x264/x265 writing library proves this is an encode rather
        # than a disc/REMUX, even when the source filename omitted BDRip.
        library = (media.writing_library if media else "").upper()
        if "X264" in library or "X265" in library:
            return "UHD BluRay BDRip" if is_uhd else "BluRay BDRip"
        return "UHD BluRay" if is_uhd else "BluRay"
    return None


def _strip_group(stem: str) -> tuple[str, str | None]:
    by_group = re.search(r"(?:^|[ ._-])by[ ._-]+([A-Za-z0-9][A-Za-z0-9._& -]*)$", stem, re.I)
    if by_group:
        return stem[: by_group.start()].rstrip(" ._-"), by_group.group(1).strip()

    technical = list(
        re.finditer(
            r"\b(?:x26[45]|h\.?26[45]|avc|hevc|mpeg[- .]?2|dts(?:[- .]?hd)?(?:[ .]?ma)?|truehd|ddp|dd|aac|flac|lpcm|pcm|opus)\b",
            stem,
            re.I,
        )
    )
    if not technical:
        return stem, None
    # Start at the first separator *after* the final codec/audio tag. This
    # avoids mistaking episode ranges (S01E01-E12) for a group, while retaining
    # legitimate hyphens in names such as VCB-Studio.
    final_tag_end = technical[-1].end()
    separators = [index for index, character in enumerate(stem) if character in "-@" and index >= final_tag_end]
    if not separators:
        return stem, None
    separator = separators[0]
    candidate = stem[separator + 1 :].strip(" ._-")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._&@ -]*", candidate):
        return stem[:separator], candidate
    return stem, None


def _normalise_episode(value: str) -> str:
    value = re.sub(r"[ ._]", "", value.upper())
    value = re.sub(r"^(S\d{1,2})-E", r"\1E", value)
    match = re.fullmatch(r"S(\d{1,2})(.*)", value)
    if not match:
        return value
    season, episode_part = match.groups()
    result = f"S{int(season):02d}"
    if not episode_part:
        return result
    if not re.fullmatch(r"E\d{1,3}(?:(?:-?E?|TOE?)\d{1,3})*", episode_part):
        return value
    episodes = [int(number) for number in re.findall(r"\d{1,3}", episode_part)]
    result += f"E{episodes[0]:02d}"
    if len(episodes) > 1:
        result += f"-E{episodes[-1]:02d}"
    return result


def filename_hints(path: Path, media: MediaInfo | None = None) -> FilenameHints:
    stem, group = _strip_group(path.stem)
    searchable = stem.replace("_", " ").replace(".", " ")
    year_match = re.search(r"\b((?:19|20)\d{2})\b", searchable)
    episode_pattern = r"\bS\d{1,2}(?:[ ._-]*E\d{1,3}(?:[ ._]*(?:(?:-|TO)[ ._]*E?|E)\d{1,3})*)?\b"
    episode_match = re.search(episode_pattern, searchable, re.I)

    technical_pattern = (
        r"\b(?:4320[pi]|2160[pi]|1440[pi]|1080[pi]|720[pi]|576[pi]|480[pi]|4K|UHD|BLU[ .-]?RAY|BDRIP|REMUX|"
        r"WEB[ .-]?DL|HDTV|DVD(?:ISO|RIP)?|AVC|HEVC|X26[45]|H[ .]?26[45]|MPEG[ .-]?2|"
        r"HDR(?:10\+)?|DOVI|DV|DTS|TRUEHD|DDP|DD|AAC|FLAC|LPCM|PCM)\b"
    )

    anchors: list[int] = []
    for pattern in (
        r"\b(?:19|20)\d{2}\b",
        episode_pattern,
        r"\b(?:(?:4320|2160|1440|1080|720|576|480)[pi]|4K)\b",
        technical_pattern,
    ):
        match = re.search(pattern, searchable, re.I)
        if match:
            anchors.append(match.start())
    title_part = searchable[: min(anchors)] if anchors else searchable
    title = _clean_title(title_part) or None

    edition = None
    if year_match and not episode_match:
        following = searchable[year_match.end() :]
        next_technical = re.search(technical_pattern, following, re.I)
        if next_technical:
            candidate = _clean_title(following[: next_technical.start()])
            edition = candidate if candidate and re.search(r"\w", candidate) else None

    try:
        file_size = path.stat().st_size
    except OSError:
        file_size = None

    return FilenameHints(
        title=title,
        year=year_match.group(1) if year_match else None,
        edition=edition,
        episode=_normalise_episode(episode_match.group(0)) if episode_match else None,
        source=_infer_source(stem, path.suffix, media, file_size),
        group=group,
        platform=_detect_platform(stem),
    )


def _filename_resolution(stem: str) -> tuple[int, int, str]:
    if re.search(r"\b4K\b", stem, re.I):
        return 3840, 2160, "2160p"
    match = re.search(r"\b(4320|2160|1440|1080|720|576|480)([pi])\b", stem, re.I)
    if not match:
        return 0, 0, "未知分辨率"
    height = int(match.group(1))
    widths = {4320: 7680, 2160: 3840, 1440: 2560, 1080: 1920, 720: 1280, 576: 720, 480: 720}
    return widths[height], height, f"{height}{match.group(2).lower()}"


def _filename_video_format(stem: str) -> tuple[str, str]:
    patterns = (
        (r"\bX264\b", "AVC", "x264"),
        (r"\bX265\b", "HEVC", "x265"),
        (r"\b(?:AVC|H[ .]?264)\b", "AVC", ""),
        (r"\b(?:HEVC|H[ .]?265)\b", "HEVC", ""),
        (r"\bMPEG[ .-]?2\b", "MPEG-2", ""),
        (r"\bVC[ .-]?1\b", "VC-1", ""),
        (r"\bAV1\b", "AV1", ""),
    )
    for pattern, video_format, library in patterns:
        if re.search(pattern, stem, re.I):
            return video_format, library
    return "", ""


def _filename_audio(stem: str) -> tuple[str, str | None]:
    patterns = (
        (r"\bDTS[ .-]?X\b", "DTS-X"),
        (r"\bDTS[ .-]?HD[ .-]?MA\b", "DTS-HD MA"),
        (r"\bTRUEHD(?:[ .-]?ATMOS)?\b", "TrueHD"),
        (r"\bDDP(?:[ .-]?ATMOS)?\b", "DDP"),
        (r"\bLPCM\b|\bPCM\b", "LPCM"),
        (r"\bFLAC\b", "FLAC"),
        (r"\bAAC\b", "AAC"),
        (r"\bOPUS\b", "Opus"),
        (r"\bDD\b|\bAC[ .-]?3\b", "DD"),
        (r"\bDTS\b", "DTS"),
    )
    codec = ""
    matched_end = 0
    for pattern, candidate in patterns:
        match = re.search(pattern, stem, re.I)
        if match:
            codec = candidate
            matched_end = match.end()
            suffix = stem[match.start() : match.end()].upper()
            if "ATMOS" in suffix:
                codec += " Atmos"
            break
    channel_match = re.search(r"\b([1-9])\.(\d)\b", stem[matched_end:]) if matched_end else None
    channels = f"{channel_match.group(1)}.{channel_match.group(2)}" if channel_match else None
    return codec, channels


def inspect_media_from_filename(path: Path, source: str | None = None) -> MediaInfo:
    """Fallback for disc images where MediaInfo exposes no elementary tracks."""
    # Keep dots here because they carry channel layouts such as LPCM 2.0 and
    # are also valid separators in codec tags such as DTS-HD.MA.
    stem = path.stem.replace("_", " ")
    width, height, resolution = _filename_resolution(stem)
    video_format, writing_library = _filename_video_format(stem)
    audio_codec, audio_channels = _filename_audio(stem)
    if resolution == "未知分辨率" or not video_format:
        raise ValueError("ISO 中未读到视频轨，文件名也缺少分辨率或视频编码")
    upper = stem.upper()
    hdr: list[str] = []
    if "HDR10+" in upper:
        hdr.append("HDR10+")
    elif "HDR10" in upper:
        hdr.append("HDR10")
    elif re.search(r"\bHDR\b", upper):
        hdr.append("HDR")
    if re.search(r"\b(?:DOVI|DV)\b", upper):
        hdr.append("DoVi")
    fps_match = re.search(r"\b(\d{2,3})(?:FPS|FPS)\b", upper)
    hfr = f"{int(fps_match.group(1))}Fps" if fps_match and int(fps_match.group(1)) >= 50 else None
    return MediaInfo(
        width=width,
        height=height,
        resolution=resolution,
        video_format=video_format,
        writing_library=writing_library,
        video_codec=_video_codec(video_format, writing_library, source),
        hdr=tuple(hdr),
        hfr=hfr,
        audio_codec=audio_codec,
        audio_channels=audio_channels,
        audio_tracks=1 if audio_codec else 0,
        audio_bitrate=0,
        scan_type="Interlaced" if resolution.endswith("i") else "Progressive",
    )


def _source_with_platform(source: str, platform: str | None) -> str:
    if platform and source == "WEB-DL":
        return f"{platform} WEB-DL"
    return source


def build_title(
    *,
    title: str,
    year: str | None,
    source: str,
    media: MediaInfo,
    group: str | None = None,
    edition: str | None = None,
    episode: str | None = None,
    platform: str | None = None,
    include_audio_count: bool = False,
) -> str:
    """Build one conservative M-Team-style movie or television title."""
    clean_title = _clean_title(title)
    if not clean_title:
        raise ValueError("片名不能为空")
    if year and not re.fullmatch(r"(?:19|20)\d{2}", str(year)):
        raise ValueError("年份必须是四位数字，例如 2024")
    if not year and not episode:
        raise ValueError("电影标题必须提供四位年份")
    canonical_source = _canonical_source(source)
    if not canonical_source:
        raise ValueError("必须提供来源，例如 BluRay REMUX 或 WEB-DL")

    # Recalculate after the final source is known: BluRay/REMUX must use AVC,
    # HEVC or MPEG2; compressed releases use x264/H.264/x265/H.265.
    codec = _video_codec(media.video_format, media.writing_library, canonical_source)
    parts = [clean_title]
    if year:
        parts.append(str(year))
    clean_edition = _clean_title(edition or "")
    if clean_edition:
        if canonical_source not in {"BluRay", "UHD BluRay"}:
            raise ValueError("地区/版本标注仅用于 BluRay/UHD BluRay 原盘")
        parts.append(clean_edition)
    if episode:
        parts.append(_normalise_episode(episode))
    source_part = _source_with_platform(canonical_source, platform)
    # The M-Team movie template puts source before resolution (as in the
    # user's BluRay 1080p example), while its TV examples use the opposite
    # order after the episode field.
    if episode:
        parts.extend([media.resolution, source_part])
    else:
        parts.extend([source_part, media.resolution])
    if media.hfr:
        parts.append(media.hfr)
    parts.extend(media.hdr)
    parts.append(codec)
    if media.audio_codec:
        # M-Team title style joins the channel layout directly to the audio
        # codec: DD5.1, DDP5.1, DTS-HD MA5.1, LPCM2.0.
        audio = media.audio_codec + (media.audio_channels or "")
        parts.append(audio)
        if include_audio_count and media.audio_tracks > 1:
            parts.append(f"{media.audio_tracks}Audio")

    result = _clean_spaces(" ".join(part for part in parts if part))
    clean_group = _clean_component(group or "")
    return f"{result}-{clean_group}" if clean_group else result


def _find_mediainfo() -> str:
    found = shutil.which("MediaInfo") or shutil.which("mediainfo")
    if found:
        return found
    local_app_data = Path.home() / "AppData" / "Local"
    installed = local_app_data / "Programs" / "MediaInfo-CLI" / "MediaInfo.exe"
    if installed.is_file():
        return str(installed)
    raise RuntimeError(
        "找不到 MediaInfo CLI。请安装它，或将 MediaInfo.exe 所在目录加入 PATH。"
    )


def read_mediainfo(path: Path, source: str | None = None) -> MediaInfo:
    executable = _find_mediainfo()
    result = subprocess.run(
        [executable, "--Output=JSON", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "MediaInfo 没有返回详情"
        raise RuntimeError(f"读取媒体信息失败：{detail}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("MediaInfo 返回的 JSON 无法解析") from exc
    try:
        return inspect_media(data, source=source)
    except ValueError:
        if path.suffix.lower() != ".iso":
            raise
        # MediaInfo can inspect DVD-Video images directly, but some UDF
        # Blu-ray ISOs expose only a General track. Their tagged filename is
        # still sufficient for a safe, explicit title fallback.
        fallback_source = source or _infer_source(
            path.stem,
            path.suffix,
            file_size=path.stat().st_size,
        )
        return inspect_media_from_filename(path, source=fallback_source)


def _prompt(label: str, default: str | None, *, required: bool = False) -> str | None:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            answer = input(f"{label}{suffix}: ").strip()
        except EOFError as exc:
            if default is not None:
                return default
            if required:
                raise ValueError(f"缺少 {label}；请使用相应命令参数提供，或在交互终端运行。") from exc
            return None
        if answer:
            return answer
        if default is not None:
            return default
        if not required:
            return None
        print("此项不能为空。")


def _prompt_source(default: str | None) -> str:
    if default:
        answer = _prompt("来源（可直接确认或改填）", default, required=True)
        return _canonical_source(answer) or default
    print("来源无法由 MediaInfo 单独判断，请选择：")
    for index, choice in enumerate(SOURCE_CHOICES, start=1):
        print(f"  {index}. {choice}")
    while True:
        answer = _prompt("来源编号或名称", None, required=True)
        if answer and answer.isdigit() and 1 <= int(answer) <= len(SOURCE_CHOICES):
            return SOURCE_CHOICES[int(answer) - 1]
        canonical = _canonical_source(answer)
        if canonical:
            return canonical
        print("请输入列表中的编号或来源名称。")


def _is_interactive() -> bool:
    return sys.stdin.isatty()


def _resolve_fields(
    args: argparse.Namespace,
    path: Path,
    media: MediaInfo,
) -> tuple[str, str | None, str, str | None, str | None, str | None, str | None]:
    hints = filename_hints(path, media)
    title = args.title or hints.title
    year = str(args.year) if args.year else hints.year
    edition = args.edition if args.edition is not None else hints.edition
    source = None if args.source == "auto" else _canonical_source(args.source)
    source = source or hints.source
    group = args.group if args.group is not None else hints.group
    episode = args.episode or hints.episode
    platform = args.platform if args.platform is not None else hints.platform
    kind = args.kind if args.kind != "auto" else ("tv" if episode else "movie")

    if _is_interactive():
        # A fully tagged release should stay a one-command workflow. Ask only
        # for facts that cannot be recovered from its original filename.
        if not title:
            title = _prompt("英文片名", None, required=True)
        if kind == "movie" and not year:
            year = _prompt("年份", None, required=True)
        if not source:
            source = _prompt_source(None)
        if kind == "tv" and not episode:
            episode = _prompt("季/集（如 S01E01 或 S01）", None, required=True)
    else:
        missing = []
        if not title:
            missing.append("--title")
        if not source:
            missing.append("--source")
        if kind == "movie" and not year:
            missing.append("--year")
        if kind == "tv" and not episode:
            missing.append("--episode")
        if missing:
            raise ValueError("无法从文件名补全 " + "、".join(missing) + "；请传入这些参数，或在终端交互运行。")

    return title or "", year, source or "", group, edition, episode, platform


def _title_for_path(args: argparse.Namespace, path: Path) -> tuple[MediaInfo, FilenameHints, str]:
    initial_media = read_mediainfo(path)
    title, year, source, group, edition, episode, platform = _resolve_fields(args, path, initial_media)
    # Source controls codec vocabulary; retain the technical probe, then derive
    # the title using the final source selection.
    media = MediaInfo(
        **{**initial_media.__dict__, "video_codec": _video_codec(initial_media.video_format, initial_media.writing_library, source)}
    )
    hints = filename_hints(path, media)
    kind = args.kind
    if kind == "auto":
        kind = "tv" if episode else "movie"
    if kind == "movie":
        episode = None
    title_text = build_title(
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
    return media, hints, title_text


def _video_paths(input_path: Path, recursive: bool = False) -> list[Path]:
    if input_path.is_file():
        if input_path.suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError(f"不是支持的视频文件：{input_path}")
        return [input_path]
    if not input_path.is_dir():
        raise FileNotFoundError(f"找不到输入路径：{input_path}")
    # Directory input means "all videos below this folder". Recursion is
    # automatic; keep the parameter only so older command lines remain valid.
    iterator = input_path.rglob("*")
    return sorted((path for path in iterator if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS), key=lambda path: str(path).lower())


def _describe_media(media: MediaInfo) -> str:
    audio = media.audio_codec
    if media.audio_channels:
        audio += media.audio_channels
    if media.audio_tracks > 1:
        audio += f"（共 {media.audio_tracks} 条音轨，已取最高码率）"
    video = f"{media.width}×{media.height} {media.video_format}"
    if media.writing_library:
        video += f" / {media.writing_library}"
    return f"视频：{video}\n音频：{audio or '未找到音频轨'}"


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1].lower() == "prepare":
        from .prepare import main as prepare_main

        prepare_main(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1].lower() in {"mteam-fill", "mteam-autofill"}:
        from .mteam_fill import main as mteam_fill_main

        mteam_fill_main(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1].lower() in {"publish", "mteam-publish"}:
        from .publish import main as publish_main

        publish_main(sys.argv[2:])
        return

    parser = argparse.ArgumentParser(
        description="读取 MediaInfo，生成符合 M-Team 影片标题规则的名称；默认只预览，不改文件。"
    )
    parser.add_argument("input", type=Path, help="视频文件，或视频所在目录")
    parser.add_argument("--apply", action="store_true", help="确认执行重命名；未指定时仅预览")
    parser.add_argument("--recursive", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--title", help="英文片名；省略时从原文件名识别或交互填写")
    parser.add_argument("--year", help="四位年份；省略时从原文件名识别")
    parser.add_argument("--source", default="auto", help="来源，如 BluRay REMUX、UHD BluRay、WEB-DL；默认自动识别")
    parser.add_argument("--group", help="发布组；传 --group \"\" 可明确省略")
    parser.add_argument("--edition", help="蓝光原盘的地区/版本/发行标签，如 MOC、US Cut")
    parser.add_argument("--platform", help="WEB-DL 平台，如 Netflix、AMZN；默认从文件名识别")
    parser.add_argument("--kind", choices=("auto", "movie", "tv"), default="auto", help="资源类型，默认自动")
    parser.add_argument("--episode", help="电视剧季/集，例如 S01E01-E12 或 S01")
    parser.add_argument("--audio-count", action="store_true", help="多音轨时追加 2Audio、3Audio 等标记；默认不追加")
    parser.add_argument("--no-audio-count", action="store_false", dest="audio_count", help=argparse.SUPPRESS)
    parser.set_defaults(audio_count=False)
    args = parser.parse_args()

    try:
        paths = _video_paths(args.input, args.recursive)
        if not paths:
            raise ValueError("目录中没有找到支持的视频文件")
        planned: list[tuple[Path, Path, MediaInfo]] = []
        for path in paths:
            media, _hints, title = _title_for_path(args, path)
            target = path.with_name(title + path.suffix)
            planned.append((path, target, media))

        targets = [target for _path, target, _media in planned]
        duplicate_targets = {target for target in targets if targets.count(target) > 1}
        conflicts = [target for path, target, _media in planned if target.exists() and target != path]
        if duplicate_targets:
            raise ValueError("生成了重复目标名称：" + "；".join(str(path) for path in sorted(duplicate_targets)))
        if conflicts:
            raise FileExistsError("目标文件已存在，未执行任何改名：" + "；".join(str(path) for path in conflicts))

        for path, target, media in planned:
            print(f"\n原文件：{path.name}")
            print(_describe_media(media))
            print(f"新名称：{target.name}")
        if not args.apply:
            print("\n以上为预览，文件尚未改名。确认无误后，在命令末尾加 --apply。")
            return

        for path, target, _media in planned:
            if target != path:
                path.rename(target)
                print(f"已改名：{target}")
            else:
                print(f"名称已符合：{path}")
    except (FileNotFoundError, FileExistsError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
