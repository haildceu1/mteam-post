"""Add recalled official torrents without touching the original TL task."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from .prepare import _bdecode, _bencode
from .recall_official import validate_manifest
from .seed_official import DEFAULT_QB_CONFIG, DEFAULT_QB_URL, _qb_api, _qb_config_key, _qb_task

STOPPED = {"pausedUP", "pausedDL", "stoppedUP", "stoppedDL"}
SEEDING = {"uploading", "stalledUP", "queuedUP", "forcedUP"}
MTEAM_SUFFIXES = ("m-team.cc", "m-team.io")
TL_SUFFIXES = ("torrentleech.org", "torrentleech.me")


def expected_files(content: bytes) -> dict[str, int]:
    info = _bdecode(content)[b"info"]
    root = (info.get(b"name.utf-8") or info[b"name"]).decode()
    if b"files" not in info:
        return {root: info[b"length"]}
    return {root + "/" + "/".join(p.decode() for p in (f.get(b"path.utf-8") or f[b"path"])): f[b"length"]
            for f in info[b"files"]}


def mapped_source(source: Path, container: str) -> Path:
    inspected = subprocess.run(["docker", "inspect", container, "--format", "{{json .Mounts}}"],
                               capture_output=True, text=True, check=True, timeout=15)
    candidates = []
    for mount in json.loads(inspected.stdout):
        host = Path(mount["Source"])
        if mount.get("Type") == "bind" and source.is_relative_to(host):
            candidates.append((len(host.parts), Path(mount["Destination"]) / source.relative_to(host)))
    if not candidates:
        raise ValueError("源资源没有对应的 qB 容器 bind mount，拒绝添加")
    return max(candidates, key=lambda item: item[0])[1]


def snapshot(source: Path) -> dict:
    paths = sorted(source.rglob("*")) if source.is_dir() else [source]
    return {str(p): (p.stat().st_dev, p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns)
            for p in paths if p.is_file()}


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    host = host.casefold().rstrip(".")
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def _mteam_trackers(content: bytes) -> list[str]:
    meta = _bdecode(content)
    if not isinstance(meta, dict):
        raise ValueError("official_torrent_invalid")
    values: list[bytes] = []
    announce = meta.get(b"announce")
    if isinstance(announce, bytes):
        values.append(announce)
    tiers = meta.get(b"announce-list")
    if isinstance(tiers, list):
        for tier in tiers:
            if isinstance(tier, list):
                values.extend(item for item in tier if isinstance(item, bytes))
    urls = []
    for raw in values:
        try:
            value = raw.decode("utf-8")
            parsed = urlsplit(value)
            host = parsed.hostname or ""
            port = parsed.port
            valid = (parsed.scheme in {"http", "https", "udp"}
                     and (port is None or 1 <= port <= 65535)
                     and not parsed.username and not parsed.password
                     and not parsed.fragment and bool(parsed.path)
                     and _host_matches(host, MTEAM_SUFFIXES))
        except (UnicodeDecodeError, ValueError):
            valid = False
        if valid and value not in urls:
            urls.append(value)
    if not urls:
        raise ValueError("official_mteam_tracker_missing")
    return urls


def export_qb_torrent(source: Path, destination: Path, *, container: str = "qbittorrent",
                      url: str = DEFAULT_QB_URL, config: Path = DEFAULT_QB_CONFIG) -> dict:
    """Export a complete TL task, validate it, and strip private tracker URLs.

    Top-level announce fields are not part of the infohash. The sanitized
    upload torrent therefore retains the exact qB payload identity without
    disclosing TorrentLeech credentials to the upload site.
    """
    source = source.absolute()
    source_before = snapshot(source)
    mapped = mapped_source(source, container)
    accepted_content_paths = {mapped.as_posix().rstrip("/")}
    if source.is_dir():
        files = [path for path in source.rglob("*") if path.is_file() and not path.is_symlink()]
        if len(files) == 1:
            accepted_content_paths.add((mapped / files[0].relative_to(source)).as_posix().rstrip("/"))
    base = url.rstrip("/")
    session = _qb_api(base, _qb_config_key(config))
    try:
        response = session.get(base + "/api/v2/torrents/info", timeout=20)
        response.raise_for_status()
        matches = [row for row in response.json()
                   if str(row.get("content_path", "")).rstrip("/") in accepted_content_paths]
        if len(matches) != 1:
            raise ValueError("qb_source_task_not_unique")
        task = matches[0]
        tags = {part.strip().casefold() for part in str(task.get("tags") or "").split(",")}
        if not any("torrentleech" in tag for tag in tags):
            raise ValueError("qb_source_not_torrentleech")
        if float(task.get("progress", 0)) < 1 or int(task.get("amount_left", 0)) > 0:
            raise ValueError("qb_source_incomplete")
        export = session.get(base + "/api/v2/torrents/export",
                             params={"hash": str(task.get("hash") or "")}, timeout=30)
        export.raise_for_status()
        original = export.content
        receipt = validate_manifest(original, source)
        if receipt["info_hash"].casefold() != str(task.get("hash") or "").casefold():
            raise ValueError("qb_export_hash_mismatch")
        if int(task.get("size", receipt["size_bytes"])) != receipt["size_bytes"]:
            raise ValueError("qb_export_size_mismatch")
        if snapshot(source) != source_before:
            raise ValueError("source_changed")
        meta = _bdecode(original)
        for key in (b"announce", b"announce-list", b"url-list"):
            meta.pop(key, None)
        sanitized = _bencode(meta)
        sanitized_receipt = validate_manifest(sanitized, source)
        if sanitized_receipt["info_hash"].casefold() != receipt["info_hash"].casefold():
            raise ValueError("qb_export_hash_changed")
        if snapshot(source) != source_before:
            raise ValueError("source_changed")
        destination = destination.absolute()
        if destination.is_symlink():
            raise ValueError("qb_export_path_unsafe")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any(parent.is_symlink() for parent in destination.parents):
            raise ValueError("qb_export_path_unsafe")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(destination, flags, 0o600)
        except FileExistsError:
            if destination.is_symlink() or not stat.S_ISREG(destination.lstat().st_mode):
                raise ValueError("qb_export_artifact_exists") from None
            existing = destination.read_bytes()
            existing_receipt = validate_manifest(existing, source)
            if existing_receipt["info_hash"] != receipt["info_hash"]:
                raise ValueError("qb_export_artifact_exists") from None
            existing_meta = _bdecode(existing)
            if any(key in existing_meta for key in (b"announce", b"announce-list", b"url-list")):
                raise ValueError("qb_export_artifact_exists") from None
            os.chmod(destination, 0o600)
        else:
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(sanitized)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                try:
                    destination.unlink()
                except OSError:
                    pass
                raise
        return {"path": str(destination), "info_hash": receipt["info_hash"],
                "size_bytes": receipt["size_bytes"], "file_count": receipt["file_count"],
                "manifest_verified": True, "trackers_stripped": True,
                "source_torrentleech_hash": str(task["hash"]).casefold()}
    finally:
        session.close()


def _get_trackers(session, base_url: str, info_hash: str) -> list[dict]:
    response = session.get(base_url + "/api/v2/torrents/trackers",
                           params={"hash": info_hash}, timeout=15)
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise RuntimeError("qB tracker 列表响应无效")
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get("url"), str)]


def _set_mteam_trackers(session, base_url: str, info_hash: str, content: bytes, post) -> None:
    official = _mteam_trackers(content)
    rows = _get_trackers(session, base_url, info_hash)
    present = {row["url"] for row in rows}
    missing = [value for value in official if value not in present]
    if missing:
        post("addTrackers", {"hash": info_hash, "urls": "\n".join(missing)})
        rows = _get_trackers(session, base_url, info_hash)
        present = {row["url"] for row in rows}
        if not all(value in present for value in official):
            raise RuntimeError("qB 未确认 M-Team tracker 已写入；任务保持现状")
    obsolete = []
    for row in rows:
        value = row["url"]
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if _host_matches(host, TL_SUFFIXES) or (
            _host_matches(host, MTEAM_SUFFIXES) and value not in official
        ):
            obsolete.append(value)
    if obsolete:
        post("removeTrackers", {"hash": info_hash, "urls": "|".join(obsolete)})
    rows = _get_trackers(session, base_url, info_hash)
    final = {row["url"] for row in rows}
    if not all(value in final for value in official):
        raise RuntimeError("qB M-Team tracker 最终校验失败；任务未标记完成")
    if any(_host_matches(urlsplit(value).hostname or "", TL_SUFFIXES) for value in final):
        raise RuntimeError("qB 仍保留 TorrentLeech tracker；任务未标记完成")


def seed(torrent: Path, source: Path, *, container: str = "qbittorrent",
         url: str = DEFAULT_QB_URL, config: Path = DEFAULT_QB_CONFIG) -> dict:
    content = torrent.read_bytes()
    receipt = validate_manifest(content, source)
    before = snapshot(source)
    mapped = mapped_source(source.absolute(), container)
    manifest = expected_files(content)
    info = _bdecode(content)[b"info"]
    root = (info.get(b"name.utf-8") or info[b"name"]).decode("utf-8")
    records = info.get(b"files")
    wrapped_single_file = False
    if source.is_file() and isinstance(records, list) and len(records) == 1:
        parts = records[0].get(b"path.utf-8") or records[0].get(b"path")
        wrapped_single_file = (
            root == source.parent.name
            and isinstance(parts, list)
            and len(parts) == 1
            and parts[0].decode("utf-8") == source.name
        )
    if wrapped_single_file:
        # A one-file multifile torrent's save path is above its release root.
        host_base = source.parent.parent
        mapped_base = mapped.parent.parent
    else:
        host_base = source.parent
        mapped_base = mapped.parent
    save_path = mapped_base.as_posix()
    mounted = subprocess.run(["docker", "exec", container, "stat", "-c", "%d:%i:%s", "--",
                              *[str(mapped_base / name) for name in manifest]],
                             capture_output=True, text=True, check=True, timeout=30)
    host_stats = []
    for name in manifest:
        details = (host_base / name).stat()
        host_stats.append(f"{details.st_dev}:{details.st_ino}:{details.st_size}")
    if mounted.stdout.splitlines() != host_stats:
        raise ValueError("qB 容器内文件与宿主机设备号、inode 或大小不一致")
    info_hash = receipt["info_hash"]
    base = url.rstrip("/")
    session = _qb_api(base, _qb_config_key(config))
    try:
        version = session.get(base + "/api/v2/app/version", timeout=10).text.strip()
        modern = version.lstrip("v").split(".")[0].isdigit() and int(version.lstrip("v").split(".")[0]) >= 5
        def post(endpoint, data=None, **kwargs):
            response = session.post(base + "/api/v2/torrents/" + endpoint, data=data, timeout=30, **kwargs)
            # qBittorrent endpoints such as addTrackers may successfully return
            # HTTP 204 No Content. Treat any 2xx response as success.
            if not 200 <= response.status_code < 300 or response.text.strip() == "Fails.":
                raise RuntimeError(f"qB {endpoint} 失败（HTTP {response.status_code}）；保留任务，不删除文件")
        task = _qb_task(session, base, info_hash)
        added = not bool(task)
        if added:
            print("[qB] 官方种子以暂停、跳过校验方式添加…", flush=True)
            post("add", {"savepath": save_path, "paused": "true", "stopped": "true",
                         "skip_checking": "true", "autoTMM": "false", "contentLayout": "Original"},
                 files={"torrents": ("official.torrent", content, "application/x-bittorrent")})
        for _ in range(15):
            task = _qb_task(session, base, info_hash)
            if task:
                break
            time.sleep(1)
        if not task:
            raise RuntimeError("qB 尚未显示官方任务；停止继续操作")
        if added:
            # qB 5 briefly exposes checkingResumeData even when stopped=true.
            # Wait for resume-data loading; this is not a content hash recheck.
            for _ in range(15):
                if task.get("state") in STOPPED:
                    break
                time.sleep(1)
                task = _qb_task(session, base, info_hash) or task
        try:
            if int(task.get("size", task.get("total_size", -1))) != receipt["size_bytes"]:
                raise ValueError("qB 总大小不匹配")
            if str(task.get("save_path", "")).rstrip("/") != save_path:
                raise ValueError("qB 保存目录不匹配")
            if task.get("content_path"):
                expected_content_paths = {mapped.as_posix()}
                if source.is_dir() and len(manifest) == 1:
                    only_path = next(iter(manifest))
                    prefix = root + "/"
                    if root == source.name and only_path.startswith(prefix):
                        # qB reports the single file, not its containing root,
                        # as content_path for a one-file multifile torrent.
                        expected_content_paths.add((mapped / only_path[len(prefix):]).as_posix())
                if task["content_path"].rstrip("/") not in expected_content_paths:
                    raise ValueError("qB 内容路径不匹配")
            response = session.get(base + "/api/v2/torrents/files", params={"hash": info_hash}, timeout=10)
            response.raise_for_status()
            files = response.json()
            observed = {f["name"]: int(f["size"]) for f in files}
            if len(files) != len(manifest) or observed != manifest:
                raise ValueError("qB 文件清单或字节数不匹配")
            if float(task.get("progress", 0)) < 1 or int(task.get("amount_left", 0)) > 0:
                raise ValueError("qB 未确认文件完整，保持暂停")
            if snapshot(source) != before:
                raise ValueError("源文件 inode、大小或修改时间发生变化")
            if added and task.get("state") not in STOPPED:
                raise ValueError("新任务未保持暂停")
        except Exception:
            # Never mutate an existing task which might belong to the user.
            if added:
                post("stop" if modern else "pause", {"hashes": info_hash})
            raise
        _set_mteam_trackers(session, base, info_hash, content, post)
        post("createTags", {"tags": "Mteam"})
        post("addTags", {"hashes": info_hash, "tags": "Mteam"})
        if task.get("state") in STOPPED:
            post("start" if modern else "resume", {"hashes": info_hash})
        for _ in range(15):
            task = _qb_task(session, base, info_hash)
            if task and task.get("state") in SEEDING and "Mteam" in {t.strip() for t in str(task.get("tags", "")).split(",")}:
                print(f"[qB] 已做种：{info_hash}，标签 Mteam，{len(manifest)} 个文件。", flush=True)
                return {"qb_torrent_hash": info_hash, "qb_save_path": save_path,
                        "qb_state": task["state"], "tag": "Mteam", "status": "seeded", "reused": not added}
            time.sleep(1)
        raise RuntimeError("qB 尚未确认做种状态；官方任务已保留，可重新运行核对")
    finally:
        session.close()
