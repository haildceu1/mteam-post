from __future__ import annotations

import hashlib

import pytest

from media_title_renamer.prepare import _bencode
from media_title_renamer.seed_official import (
    _qb_torrent_control,
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


@pytest.mark.parametrize(
    ("action", "preferred"),
    [("start", "start"), ("stop", "stop")],
)
def test_qb_control_uses_qb_5_route_first(action: str, preferred: str) -> None:
    class Response:
        status_code = 200

    class Session:
        calls: list[str] = []

        def post(self, url: str, **kwargs: object) -> Response:
            self.calls.append(url.rsplit("/", 1)[-1])
            return Response()

    session = Session()
    route = _qb_torrent_control(session, "http://qb", action, "abc")
    assert route == preferred
    assert session.calls == [preferred]


@pytest.mark.parametrize(
    ("action", "preferred", "fallback"),
    [("start", "start", "resume"), ("stop", "stop", "pause")],
)
def test_qb_control_falls_back_to_legacy_route_on_missing_endpoint(
    action: str, preferred: str, fallback: str
) -> None:
    class Response:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    class Session:
        calls: list[str] = []

        def post(self, url: str, **kwargs: object) -> Response:
            route = url.rsplit("/", 1)[-1]
            self.calls.append(route)
            return Response(404 if route == preferred else 200)

    session = Session()
    route = _qb_torrent_control(session, "http://qb", action, "abc")
    assert route == fallback
    assert session.calls == [preferred, fallback]
