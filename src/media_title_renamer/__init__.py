"""Generate M-Team-style video titles from MediaInfo and rename local files."""

from .cli import build_title, inspect_media

__all__ = ["build_title", "inspect_media"]
