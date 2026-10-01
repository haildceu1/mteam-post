from __future__ import annotations

import hashlib

import pytest

from media_title_renamer.prepare import _bencode
from media_title_renamer.seed_official import (
    _torrent_manifest,
    _validated_link,
    _wait_for_valid_copied_link,
)


def test_official_download_link_is_restricted_to_matching_mteam_id() -> None:
    link = "https://api2.m-team.cc/api/rss/dlv2?tid=1261708&sign=hidden"
    assert _validated_link(link, "1261708") == link
    alternate_official_host = "https://api.m-team.cc/api/rss/dlv2?tid=1261708&sign=hidden"
    assert _validated_link(alternate_official_host, "1261708") == alternate_official_host
    with pytest.raises(RuntimeError, match="官方下载域名"):
        _validated_link("https://attacker.example/api/torrent?tid=1261708", "1261708")
    with pytest.raises(RuntimeError, match="条目 ID"):
        _validated_link("https://api2.m-team.cc/api/download?tid=7&sign=hidden", "1261708")


def test_copy_wait_ignores_stale_clipboard_until_matching_official_link_arrives() -> None:
    stale = "https://api.m-team.cc/api/rss/dlv2?tid=1261708&sign=old"
    copied = iter(
        [
            stale,
            "https://kp.m-team.cc/detail/1261708",
            "https://api2.m-team.cc/api/rss/dlv2?tid=1261708&sign=redacted",
        ]
    )
    now = [0.0]
    result = _wait_for_valid_copied_link(
        lambda: next(copied),
        "1261708",
        previous_text=stale,
        timeout=2,
        interval=0.1,
        clock=lambda: now[0],
        sleep=lambda duration: now.__setitem__(0, now[0] + duration),
    )
    assert "tid=1261708" in result


def test_official_torrent_manifest_requires_exact_single_file() -> None:
    info = {
        b"length": 79_154_150_362,
        b"name": b"Threads.1984.UHD.BluRay.2160p.mkv",
        b"piece length": 16 * 1024 * 1024,
        b"pieces": b"p" * 20,
    }
    manifest = _bencode({b"info": info})
    name, size, info_hash = _torrent_manifest(manifest)
    assert name == "Threads.1984.UHD.BluRay.2160p.mkv"
    assert size == 79_154_150_362
    assert info_hash == hashlib.sha1(_bencode(info)).hexdigest()

    multifile = _bencode(
        {
            b"info": {
                b"files": [{b"length": 1, b"path": [b"part.mkv"]}],
                b"name": b"Threads",
                b"piece length": 1,
                b"pieces": b"p" * 20,
            }
        }
    )
    with pytest.raises(RuntimeError, match="单文件"):
        _torrent_manifest(multifile)
