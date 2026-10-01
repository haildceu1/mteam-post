"""Conservative episode-number inference for metadata-only folder previews."""

from __future__ import annotations

import re
from pathlib import Path


def episode_number_hint(path: str | Path) -> int | None:
    """Extract a clear episode number without interpreting years/quality tags."""
    name = Path(path).stem
    name = re.sub(r"\s*\[[0-9a-f]{8}\]\s*$", "", name, flags=re.I)
    patterns = (
        r"\bS\d{1,2}[ ._-]*E\s*0*(\d{1,3})\b",
        r"\b\d{1,2}\s*[xX]\s*0*(\d{1,3})\b",
        r"\b(?:EP|Episode|E)\s*0*(\d{1,3})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, name, re.I)
        if match:
            number = int(match.group(1))
            return number if 1 <= number <= 300 else None

    # Common TV encode naming: "Series - 01 [BD ...]" or "01 - Episode title".
    # Read the first standalone episode-like number, then reject technical
    # tags such as resolution/year rather than mistaking them for episodes.
    match = re.search(r"(?:^|[ ._-])0*(\d{1,3})(?=\s*[\[(]|\s*[-_.]|$)", name)
    if match:
        number = int(match.group(1))
        if 1 <= number <= 300 and not 1900 <= number <= 2099 and number not in {360, 480, 540, 720, 1080, 2160}:
            return number

    # Multipart mini-series commonly uses a compact basename such as brides1.avi.
    match = re.search(r"(?<!\d)0*(\d{1,2})$", name)
    if match:
        number = int(match.group(1))
        if 1 <= number <= 99:
            return number
    return None


def season_number_hint(value: str) -> int | None:
    patterns = (
        r"\bS0*(\d{1,2})(?=\b|[-_.]|[ED]\d)",
        r"\b0*(\d{1,2})(?:st|nd|rd|th)\s+Season\b",
        r"\bSeason\s*0*(\d{1,2})\b",
        r"\bSaison\s*0*(\d{1,2})\b",
        r"第\s*(\d{1,2})\s*季",
    )
    for pattern in patterns:
        match = re.search(pattern, value, re.I)
        if match:
            number = int(match.group(1))
            if 0 <= number <= 99:
                return number
    return None


def sequential_episode_map(paths: list[Path], *, season: int | None = None) -> tuple[dict[Path, str], str] | None:
    """Map files with a complete, unique numeric sequence to SxxExx tokens.

    A missing season defaults to S01 only for a sequence beginning at episode
    one; callers must additionally require a high-confidence TV identity.
    """
    if len(paths) < 2:
        return None
    numbers = [episode_number_hint(path) for path in paths]
    if any(number is None for number in numbers):
        return None
    concrete = [int(number) for number in numbers if number is not None]
    if len(set(concrete)) != len(concrete):
        return None
    if sorted(concrete) != list(range(min(concrete), max(concrete) + 1)):
        return None
    source = "explicit_season" if season is not None else "inferred_single_season"
    selected_season = season if season is not None else (1 if min(concrete) == 1 else None)
    if selected_season is None:
        return None
    mapping = {
        path: f"S{selected_season:02d}E{number:02d}"
        for path, number in zip(paths, concrete)
    }
    return mapping, source


def episode_mapping_issues(paths: list[Path]) -> str:
    """Explain ambiguous numbering without assigning invented episodes."""
    numbers: dict[int, list[Path]] = {}
    missing = []
    for path in paths:
        number = episode_number_hint(path)
        if number is None:
            missing.append(path.name)
        else:
            numbers.setdefault(number, []).append(path)
    details = []
    if missing:
        details.append("无集号文件：" + "、".join(missing[:4]))
    duplicates = [f"{number:02d}（{len(items)} 个文件）" for number, items in sorted(numbers.items()) if len(items) > 1]
    if duplicates:
        details.append("重复集号：" + "、".join(duplicates[:8]))
    if numbers:
        gaps = sorted(set(range(min(numbers), max(numbers) + 1)) - set(numbers))
        if gaps:
            details.append("编号缺口：" + "、".join(f"{number:02d}" for number in gaps[:8]))
    return "；".join(details) or "集数无法组成唯一连续序列"


def season_folder_map(paths: list[Path], root: Path) -> dict[Path, str] | None:
    """Identify explicit season folders without inventing episode numbers.

    Callers must only use season-only tokens for metadata in remain mode.
    """
    result: dict[Path, str] = {}
    root_season = None if re.search(r"S\d{1,2}\s*[-–]\s*S\d{1,2}", root.name, re.I) else season_number_hint(root.name)
    for path in paths:
        try:
            parent_parts = path.relative_to(root).parts[:-1]
        except ValueError:
            return None
        seasons = [season_number_hint(part) for part in parent_parts]
        seasons = [season for season in seasons if season is not None]
        if len(seasons) > 1 or (not seasons and root_season is None):
            return None
        selected = seasons[0] if seasons else root_season
        result[path] = f"S{selected:02d}"
    return result or None


def sample_or_extra(path: str | Path) -> bool:
    """Whether a video is clearly auxiliary material rather than an episode."""
    auxiliary_names = {
        "sample",
        "samples",
        "proof",
        "trailer",
        "trailers",
        "featurette",
        "featurettes",
        "extra",
        "extras",
        "behind the scenes",
        "deleted scenes",
        "special features",
        "bonus",
        "bonus feature",
        "bonus features",
        "blooper",
        "bloopers",
        "outtake",
        "outtakes",
        "interview",
        "interviews",
        "making of",
    }
    parts = Path(path).parts
    normalized_parts = [
        " ".join(re.sub(r"[._-]+", " ", part).casefold().split())
        for part in parts
    ]
    return any(part in auxiliary_names for part in normalized_parts[:-1]) or bool(
        re.search(r"\bCreditless\s+(?:OP|ED)\b|\bNC(?:OP|ED)\b", Path(path).stem, re.I)
    )
