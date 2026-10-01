"""Add recalled official torrents without touching the original TL task."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .prepare import _bdecode
from .recall_official import validate_manifest
from .seed_official import DEFAULT_QB_CONFIG, DEFAULT_QB_URL, _qb_api, _qb_config_key, _qb_task

STOPPED = {"pausedUP", "pausedDL", "stoppedUP", "stoppedDL"}
SEEDING = {"uploading", "stalledUP", "queuedUP", "forcedUP"}


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


def seed(torrent: Path, source: Path, *, container: str = "qbittorrent",
         url: str = DEFAULT_QB_URL, config: Path = DEFAULT_QB_CONFIG) -> dict:
    content = torrent.read_bytes()
    receipt = validate_manifest(content, source)
    before = snapshot(source)
    mapped = mapped_source(source.absolute(), container)
    save_path = mapped.parent.as_posix()
    manifest = expected_files(content)
    mounted = subprocess.run(["docker", "exec", container, "stat", "-c", "%d:%i:%s", "--",
                              *[str(mapped.parent / name) for name in manifest]],
                             capture_output=True, text=True, check=True, timeout=30)
    host_stats = []
    for name in manifest:
        details = (source.parent / name).stat()
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
            if response.status_code != 200 or response.text.strip() == "Fails.":
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
            if task.get("content_path") and task["content_path"].rstrip("/") != mapped.as_posix():
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
