"""Load a Douban browser cookie without ever logging its contents."""

from __future__ import annotations

import csv
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Any


class DoubanCookieError(RuntimeError):
    """The configured browser-cookie export could not be safely parsed."""


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _valid_expiry(value: Any, now: int) -> bool:
    if value in (None, ""):
        return True
    try:
        expiry = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return True
    return expiry <= 0 or expiry >= now


def _domain_matches(domain: str) -> bool:
    return domain.lstrip(".").casefold() == "douban.com" or domain.casefold().endswith(".douban.com")


def _from_xlsx(path: Path, now: int) -> list[tuple[str, str]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise DoubanCookieError("读取 XLSX Cookie 需要 openpyxl") from exc

    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            sheet = workbook.active
            rows = sheet.iter_rows(values_only=True)
            first = next(rows, None)
            if first is None:
                return []
            headers = {_text(value).casefold(): index for index, value in enumerate(first) if _text(value)}
            required = {"domain", "name", "value"}
            if not required.issubset(headers):
                raise DoubanCookieError("XLSX Cookie 表缺少 Domain/Name/Value 列")
            result: list[tuple[str, str]] = []
            for row in rows:
                domain = _text(row[headers["domain"]]) if len(row) > headers["domain"] else ""
                name = _text(row[headers["name"]]) if len(row) > headers["name"] else ""
                value = _text(row[headers["value"]]) if len(row) > headers["value"] else ""
                expiry_index = headers.get("expiry")
                expiry = row[expiry_index] if expiry_index is not None and len(row) > expiry_index else None
                if domain and name and value and _domain_matches(domain) and _valid_expiry(expiry, now):
                    result.append((name, value))
            return result
        finally:
            workbook.close()
    except DoubanCookieError:
        raise
    except Exception as exc:
        # Do not include parser errors or cell contents in diagnostics.
        raise DoubanCookieError(f"XLSX Cookie 无法读取（{type(exc).__name__}）") from exc


def _from_netscape(path: Path, now: int) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as stream:
            for row in csv.reader(stream, delimiter="\t"):
                if len(row) != 7:
                    continue
                domain, _include_subdomains, _cookie_path, _secure, expiry, name, value = row
                if domain.startswith("#HttpOnly_"):
                    domain = domain[len("#HttpOnly_"):]
                if (
                    _domain_matches(domain)
                    and name.strip()
                    and value
                    and _valid_expiry(expiry, now)
                ):
                    result.append((name.strip(), value.strip()))
    except OSError as exc:
        raise DoubanCookieError(f"Cookie 文件无法读取（{type(exc).__name__}）") from exc
    return result


@lru_cache(maxsize=4)
def _cached_cookie_header(path_text: str, mtime_ns: int, size: int) -> str:
    del mtime_ns, size  # They are cache invalidation keys; the file is re-read below.
    path = Path(path_text)
    now = int(time.time())
    if path.suffix.casefold() in {".xlsx", ".xlsm"}:
        pairs = _from_xlsx(path, now)
    else:
        pairs = _from_netscape(path, now)

    # Browser exports can contain duplicate names for distinct subdomains.
    # Keep the last live value, matching the effective cookie sent by a browser.
    cookies: dict[str, str] = {}
    for name, value in pairs:
        cookies[name] = value
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def load_douban_cookie_header(path: str | Path | None = None) -> str:
    """Return live cookies for Douban domains from an explicit file or env var.

    Supported inputs are Cookie-Editor XLSX exports and Netscape cookie files.
    Cookie values exist only in the returned in-memory string; this function
    intentionally has no logging or diagnostic representation of the values.
    """
    configured = str(path or os.environ.get("DOUBAN_COOKIE_FILE", "")).strip()
    if not configured:
        return ""
    candidate = Path(configured).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
        stat = resolved.stat()
    except OSError as exc:
        raise DoubanCookieError(f"Cookie 文件不可用（{type(exc).__name__}）") from exc
    if not resolved.is_file():
        raise DoubanCookieError("Cookie 路径不是普通文件")
    return _cached_cookie_header(str(resolved), stat.st_mtime_ns, stat.st_size)
