#!/usr/bin/env python3
"""Serially prepare, publish, recall, and seed the explicitly listed qB tasks."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import queue
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from media_title_renamer.autonomous import files_snapshot
from media_title_renamer.seed_official import (
    DEFAULT_QB_CONFIG, DEFAULT_QB_URL, _qb_api, _qb_config_key,
)
from media_title_renamer.auto_publish import previous_task


TARGETS = [
    ("9834448afce8a69954666c22b1ffb94de8138840", "Survivor S1-S38", "tv"),
    ("4f9c176f16d3a209b3d4802f309c5e36afbd1b13",
     "The.Twilight.Zone.1959-1963.S01-S05.1080p.BluRay.REMUX.AVC.FLAC.2.0-NOGRP", "tv"),
    ("137b0bdc2658b620427d940212330148df068aac",
     "La.camera.explore.le.temps.INTEGRALE.VFF.480p.WEBRip.AAC.2.0.x264-KIRK", "tv"),
    ("079c3162f5b1f29b4690ebe3d7214478c828fcfc",
     "The.Leftovers.S02.1080p.DTS-HD.MA.5.1.AVC.REMUX-FraMeSToR", "tv"),
    ("4b2a7623c6a0ca35f9814552e81196283f415490",
     "The.Leftovers.S01.1080p.DTS-HD.MA.5.1.AVC.REMUX-FraMeSToR", "tv"),
    ("deec97979d0bcc061885e94e5268e65a4ca066da",
     "Widows.Bay.2026.S01.2160p.ATVP.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-DWTP", "tv"),
    ("22a5d0ee82d40a41a1bef90a6a746f03f4561cb2",
     "Westworld.S04.2160p.BluRay.TrueHD.Atmos.7.1.DV.HDR10.x265-Stelks", "tv"),
    ("bf66743ff5ae02ab91bab44df96b089065d334df",
     "The.Leftovers.S03.1080p.DTS-HD.MA.5.1.AVC.REMUX-FraMeSToR", "tv"),
    ("69ca59ddcf3ca40a58e68aa1996d888278974414",
     "She Rides Shotgun 2025 br remux avc-d3g", "movie"),
    ("65a5e7a3d18adce5aaf61981a496c99a68fc357f",
     "She Rides Shotgun 2025 2160p UHD br remux hdr hevc-d3g", "movie"),
    ("b532ace9523b8b372c4d2707be7693ed4b600d91",
     "Survivor (2000) S26 (1080p AMZN WEB-DL H264 SDR DDP 2.0 English - HONE)", "tv"),
    ("1e25eac99a9e7b551b4324290523c6a59225eba5",
     "Survivor (2000) S25 (1080p AMZN WEB-DL H264 SDR DDP 2.0 English - HONE)", "tv"),
    ("e20a66da753c1149138da34f231ce98198f29b64",
     "The.Counsellor.1973.2160p.UHD.BluRay.x265-LWRTD", "movie"),
    ("fe49c4e7c68d81522634f83d83013f5394755fe8",
     "[SCY] Death Parade (BD 1080p Hi10 FLAC) [Dual-Audio]", "tv"),
]

DOWNLOADS = Path("/data/Zhyw/media-stack/downloads")
DEFAULT_OUTPUT = DOWNLOADS / ".mteam-transfer/qb-targeted/20260930"
ARCHIVES = re.compile(
    r"\.(?:rar|r\d{2,3}|zip|7z|tar|tgz|tbz2?|txz|gz|bz2?|xz|zst|lz|lz4|lzma|lzo|cab|ace|arj|\d{3})$",
    re.I,
)
REFRESH_HASHES = {
    # Add a hash only when the current task's cached package is actually stale.
}


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".writing")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def safe_line(line: str) -> str:
    if re.search(r"(?:cookie|authorization|x-api-key|passkey)\s*[:=]", line, re.I):
        return "[认证信息已隐藏]"
    return re.sub(
        r"https?://[^\s<>'\"]+",
        lambda m: (lambda u: f"{u.scheme}://{u.hostname}{u.path}" + ("[参数已隐藏]" if u.query else ""))(
            urlsplit(m.group(0))
        ),
        line,
    )


def output_key(name: str, torrent_hash: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")[:72].lower() or "resource"
    return f"{slug}-{torrent_hash[:12]}"


def host_source(content_path: str) -> Path:
    virtual = Path(content_path)
    if not virtual.is_absolute() or ".." in virtual.parts or not virtual.is_relative_to(Path("/downloads")):
        raise ValueError("source_path_not_under_downloads")
    source = DOWNLOADS / virtual.relative_to("/downloads")
    if not source.exists():
        raise ValueError("source_missing")
    return source


def stage_qb_files(task: dict, qb_files: list[dict], task_output: Path) -> tuple[Path, Path, list[dict]]:
    """Hard-link only the exact qB torrent manifest, excluding unrelated files in save_path."""
    source = host_source(str(task.get("content_path") or ""))
    if source.is_symlink() or not source.is_dir():
        raise ValueError("source_not_safe_directory")
    stage_parent = task_output / "input"
    stage = stage_parent / str(task.get("name") or "source")
    stage_parent.mkdir(parents=True, exist_ok=True)
    stage.mkdir(exist_ok=True)
    rows = []
    seen = set()
    for item in qb_files:
        rel = PurePosixPath(str(item.get("name") or ""))
        if rel.is_absolute() or not rel.parts or ".." in rel.parts:
            raise ValueError("unsafe_qb_file_path")
        # qB content_path already points at the torrent root; the API manifest
        # includes the root directory name as its first path component.
        if rel.parts[0] != str(task.get("name") or ""):
            raise ValueError("qb_manifest_root_mismatch")
        rel = PurePosixPath(*rel.parts[1:])
        if not rel.parts or rel.as_posix() in seen:
            raise ValueError("invalid_qb_manifest_path")
        seen.add(rel.as_posix())
        src = source.joinpath(*rel.parts)
        if src.is_symlink() or not src.is_file():
            raise ValueError("qb_manifest_file_missing_or_unsafe")
        st = src.stat()
        if st.st_size != int(item.get("size", -1)):
            raise ValueError("qb_manifest_size_mismatch")
        dst = stage.joinpath(*rel.parts)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() or dst.is_symlink():
            dst_stat = dst.stat()
            if (dst.is_symlink() or not dst.is_file() or dst_stat.st_dev != st.st_dev
                    or dst_stat.st_ino != st.st_ino or dst_stat.st_size != st.st_size):
                raise ValueError("staging_conflict")
        else:
            if dst.parent.stat().st_dev != st.st_dev:
                raise ValueError("hardlink_cross_device")
            os.link(src, dst)
        rows.append({"name": rel.as_posix(), "bytes": st.st_size,
                     "device": st.st_dev, "inode": st.st_ino, "mtime_ns": st.st_mtime_ns})
    if not rows or sum(row["bytes"] for row in rows) != int(task.get("size", -1)):
        raise ValueError("qb_manifest_total_size_mismatch")
    staged = files_snapshot(stage)
    if {(x["name"], x["bytes"], x["device"], x["inode"]) for x in staged} != {
            (x["name"], x["bytes"], x["device"], x["inode"]) for x in rows}:
        raise ValueError("staging_manifest_mismatch")
    if any(ARCHIVES.search(row["name"]) for row in rows):
        raise ValueError("compressed_resource")
    if any(Path(row["name"]).suffix.casefold() in {".exe", ".dll", ".pak", ".msi", ".apk", ".obb"}
           for row in rows):
        raise ValueError("game_or_software_payload")
    return source, stage, rows


def source_manifest_unchanged(source: Path, rows: list[dict]) -> bool:
    for row in rows:
        path = source / row["name"]
        try:
            st = path.stat()
        except OSError:
            return False
        if path.is_symlink() or not path.is_file() or (st.st_size, st.st_dev, st.st_ino, st.st_mtime_ns) != (
                row["bytes"], row["device"], row["inode"], row["mtime_ns"]):
            return False
    return True


def write_summary(output: Path, state: dict) -> None:
    rows = state["resources"]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    text = ["# qB 指定资源转种", "", f"更新时间：{state['updated_at']}", "",
            f"总数：{len(rows)}；状态统计：" + "、".join(f"{k} {v}" for k, v in sorted(counts.items())), "",
            "| 资源 | 状态 | M-Team ID | 说明 |", "|---|---|---:|---|"]
    for row in rows:
        name = str(row.get("name", "")).replace("|", "\\|")
        reason = str(row.get("reason") or "").replace("|", "\\|")
        text.append(f"| {name} | {row['status']} | {row.get('mteam_torrent_id', '')} | {reason} |")
    (output / "summary.md").write_text("\n".join(text) + "\n")
    save_json(output / "batch.json", state)


def apply_explicit_user_facts(torrent_hash: str, task_output: Path) -> None:
    """Apply explicit user facts and preserve earlier verified season matches."""
    known = {
        "137b0bdc2658b620427d940212330148df068aac",
        "b532ace9523b8b372c4d2707be7693ed4b600d91",
        "1e25eac99a9e7b551b4324290523c6a59225eba5",
        "fe49c4e7c68d81522634f83d83013f5394755fe8",
        "9834448afce8a69954666c22b1ffb94de8138840",
        "4f9c176f16d3a209b3d4802f309c5e36afbd1b13",
    }
    if torrent_hash not in known:
        return
    package_path = task_output / "mteam-prepare.json"
    review_path = task_output / "review.json"
    if not package_path.is_file() or not review_path.is_file():
        return
    package = json.loads(package_path.read_text())
    if torrent_hash in {
        "9834448afce8a69954666c22b1ffb94de8138840",
        "4f9c176f16d3a209b3d4802f309c5e36afbd1b13",
    }:
        series = {
            "9834448afce8a69954666c22b1ffb94de8138840": {
                "url": "https://movie.douban.com/subject/6118634/",
                "id": "6118634", "title": "幸存者：婆罗洲 第一季",
                "original_title": "Survivor: Borneo Season 1", "year": "2000",
                "subtitle": "幸存者 / Survivor [英语]",
            },
            "4f9c176f16d3a209b3d4802f309c5e36afbd1b13": {
                "url": "https://movie.douban.com/subject/1950484/",
                "id": "1950484", "title": "迷离时空(原版) 第一季",
                "original_title": "The Twilight Zone", "year": "1959",
                "subtitle": "迷离时空(原版) / The Twilight Zone [英语]",
            },
        }[torrent_hash]
        candidate = {"id": series["id"], "title": series["title"],
                     "original_title": series["original_title"], "year": series["year"],
                     "season_number": 1, "site": "douban", "source": "user_confirmed_series_anchor"}
        package["douban_url"] = series["url"]
        package["douban_match"] = {**candidate, "url": series["url"], "selection_confidence": 1.0}
        package["subtitle"] = series["subtitle"]
        facts = package.setdefault("user_confirmed_facts", [])
        anchor_fact = {
            "field": "douban_series_anchor", "value": series["url"],
            "source": "user_instruction",
            "note": "用户确认豆瓣第一季条目可代表该多季合集；发布标题仍标明实际包含的季范围",
        }
        if anchor_fact not in facts:
            facts.append(anchor_fact)
        subtitle_fact = {
            "field": "subtitle", "value": series["subtitle"], "source": "user_instruction",
        }
        if subtitle_fact not in facts:
            facts.append(subtitle_fact)
        package.setdefault("identification_evidence", {})["user_confirmed_douban_series_anchor"] = candidate
        ai_input_path = task_output / "ai-input.json"
        ai_output_path = task_output / "ai-output.json"
        if ai_input_path.is_file():
            ai_input = json.loads(ai_input_path.read_text())
            candidates = ai_input.setdefault("candidates", {}).setdefault("douban", [])
            if not any(str(item.get("id")) == series["id"] for item in candidates):
                candidates.append(candidate)
            save_json(ai_input_path, ai_input)
        if ai_output_path.is_file():
            ai_result = json.loads(ai_output_path.read_text())
            ai_result["douban_id"] = series["id"]
            ai_result["status"] = "ready"
            ai_result["issues"] = [
                issue for issue in ai_result.get("issues", [])
                if "豆瓣条目对应第1季" not in str(issue)
                and "豆瓣条目对应第 1 季" not in str(issue)
                and not (torrent_hash == "9834448afce8a69954666c22b1ffb94de8138840"
                         and ("季播出年份跨越多个年份" in str(issue)
                              or "没有可确认对应《Survivor》" in str(issue)))
            ]
            ai_result.pop("user_confirmed_selection", None)
            note = "用户确认采用第一季豆瓣条目作为多季合集代表"
            if note not in ai_result.setdefault("reasons", []):
                ai_result["reasons"].append(note)
            save_json(ai_output_path, ai_result)
    elif torrent_hash == "137b0bdc2658b620427d940212330148df068aac":
        package["remain_mode"] = True
        package.setdefault("user_confirmed_facts", []).append({
            "field": "episode_tree", "value": "preserve all existing files and season folders",
            "source": "user_instruction"
        })
    elif torrent_hash == "fe49c4e7c68d81522634f83d83013f5394755fe8":
        url = "https://bgm.tv/subject/116742"
        parsed = urlsplit(url)
        if parsed.hostname != "bgm.tv" or parsed.path != "/subject/116742":
            raise ValueError("invalid_user_bangumi_url")
        # BD-marked anime encode: animation category + BDRip, not WEB-DL/BDMV.
        package.update(
            source="BDRip", category="动画",
            # M-Team's release-title vocabulary requires BluRay for BDRip encodes.
            title="Death Parade 2015 BluRay 1080p x264 FLAC 5.1-SCY",
            bangumi_url=url + "/",
        )
        package.setdefault("user_confirmed_facts", []).append({
            "field": "bangumi_url", "value": url + "/", "source": "user_instruction"
        })
        package.setdefault("identification_evidence", {})["source_override"] = {
            "value": "BDRip", "evidence": "原始发布名标注 BD；实测为 x264 压制而非 BDMV/ISO"
        }
    else:
        package["source"] = "WEB-DL"
        package["title"] = re.sub(r"DDP\s*5\.1", "DDP", str(package.get("title") or ""), flags=re.I)
        package.setdefault("identification_evidence", {})["audio_channels_override"] = {
            "value": "omitted_from_release_title",
            "evidence": "全季各集声道数不一致，仅标注共同音频编码 DDP"
        }
        if torrent_hash == "b532ace9523b8b372c4d2707be7693ed4b600d91":
            old_path = Path("/data/Zhyw/media-stack/downloads/.mteam-transfer/qb-targeted/20260930/tasks/survivor-2000-s26-1080p-amzn-web-dl-h264-sdr-ddp-2-0-english-hone-b532ace9523b/mteam-prepare.json")
            if old_path.is_file():
                old = json.loads(old_path.read_text())
                if (old.get("douban_match", {}).get("id") == "20471026"
                        and old.get("douban_match", {}).get("season_number") == 26):
                    for key in ("douban_url", "douban_match"):
                        package[key] = old[key]
                    package.setdefault("identification_evidence", {})["douban_reused"] = {
                        "id": "20471026", "evidence": "此前高置信度匹配 Survivor: Caramoan Season 26"
                    }
        package["media"] = {**(package.get("media") or {}), "audio_channels": ""}
    save_json(package_path, package)
    nested_package = task_output / "prepare" / "mteam-prepare.json"
    if nested_package.is_file():
        save_json(nested_package, package)

    review = json.loads(review_path.read_text())
    old_blockers = list(review.get("blockers") or [])
    review["blockers"] = [
        item for item in old_blockers
        if item != "identity_not_high_confidence"
        and "identity.source" not in str(item)
        and "标注BD" not in str(item)
        and "声道" not in str(item)
        and "audio channel" not in str(item).casefold()
        and not (torrent_hash == "137b0bdc2658b620427d940212330148df068aac"
                 and ("整季范围" in str(item) or "每季内容完整" in str(item)))
        and not (torrent_hash == "b532ace9523b8b372c4d2707be7693ed4b600d91"
                 and ("douban" in str(item).casefold()
                      or "豆瓣" in str(item)
                      or "identity_not_high_confidence" in str(item)))
        and not (torrent_hash in {
            "9834448afce8a69954666c22b1ffb94de8138840",
            "4f9c176f16d3a209b3d4802f309c5e36afbd1b13",
        } and (str(item) in {"douban_unconfirmed", "identity_not_high_confidence"}
               or "豆瓣条目对应第1季" in str(item)
               or "豆瓣条目对应第 1 季" in str(item)))
        and not (torrent_hash == "9834448afce8a69954666c22b1ffb94de8138840"
                 and ("季播出年份跨越多个年份" in str(item)
                      or "没有可确认对应《Survivor》" in str(item)))
        and not (torrent_hash in {
            "9834448afce8a69954666c22b1ffb94de8138840",
            "4f9c176f16d3a209b3d4802f309c5e36afbd1b13",
        } and str(item) in {"mteam_search_unavailable", "publication_or_recall_incomplete"})
    ]
    manual_confirmation = (
        "用户确认保持 La caméra 原始季目录和所有现存文件，不因包内重复/完整性疑虑阻塞"
        if torrent_hash == "137b0bdc2658b620427d940212330148df068aac"
        else ("依据用户提供 Bangumi 链接与原始文件标记修正 Death Parade 来源为动画 BDRip"
              if torrent_hash == "fe49c4e7c68d81522634f83d83013f5394755fe8"
        else ("用户确认豆瓣第一季条目可代表该多季合集，标题按实际季范围标注"
              if torrent_hash in {
                  "9834448afce8a69954666c22b1ffb94de8138840",
                  "4f9c176f16d3a209b3d4802f309c5e36afbd1b13",
              } else "复用可追溯豆瓣季条目并省略全季不一致的声道数"))
    )
    confirmations = review.setdefault("manual_confirmations", [])
    if manual_confirmation not in confirmations:
        confirmations.append(manual_confirmation)
    if not review["blockers"] and float(review.get("confidence") or 0) >= .90:
        review["status"] = "ready"
    save_json(review_path, review)


def run_child(command: list[str], *, env: dict, task_output: Path, timeout: int) -> int:
    log_path = task_output / "batch.log"
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n[{datetime.now(timezone.utc).isoformat()}] START {command[4]}\n")
        process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1], env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, bufsize=1)
        lines: queue.Queue = queue.Queue()

        def drain_output():
            try:
                for item in process.stdout:
                    lines.put(item)
            finally:
                lines.put(None)

        reader = threading.Thread(target=drain_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        eof = False
        while not eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                line = lines.get(timeout=min(1.0, remaining))
            except queue.Empty:
                continue
            if line is None:
                eof = True
                continue
            clean = safe_line(line.rstrip("\n"))
            print(clean, flush=True)
            log.write(clean + "\n")
            log.flush()
        return process.wait(timeout=10)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="串行处理固定 qB 任务：资料、发布、官方召回、qB 做种")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--only", action="append", default=[], help="仅跑指定 qB hash，可重复传入")
    parser.add_argument("--qb-url", default=DEFAULT_QB_URL)
    parser.add_argument("--qb-config", type=Path, default=DEFAULT_QB_CONFIG)
    parser.add_argument("--qb-container", default="qbittorrent")
    parser.add_argument("--moviepilot-container", default="moviepilot")
    parser.add_argument("--site-id", type=int, default=1)
    parser.add_argument("--proxy", default="http://127.0.0.1:7890")
    parser.add_argument("--douban-interval", type=int, default=30)
    parser.add_argument("--max-gib", type=float, default=0, help="可选单资源大小上限 GiB；默认 0 表示不限制")
    parser.add_argument("--timeout", type=int, default=7200)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output.is_relative_to(Path("/tmp")) or output.is_relative_to(DOWNLOADS / "TL"):
        parser.error("输出路径不允许位于 /tmp 或 TL 源目录")
    if args.max_gib < 0 or args.douban_interval < 0:
        parser.error("max-gib 不得为负，douban-interval 不得为负")
    selected = [t for t in TARGETS if not args.only or t[0] in set(args.only)]
    unknown = set(args.only) - {t[0] for t in TARGETS}
    if unknown:
        parser.error("未知 hash：" + ",".join(sorted(unknown)))
    output.mkdir(parents=True, exist_ok=True)
    (output / "cache").mkdir(exist_ok=True)
    state_file = output / "batch.json"
    previous = json.loads(state_file.read_text()) if state_file.is_file() else {}
    by_hash = {r.get("hash"): r for r in previous.get("resources", [])}
    state = {"mode": "submit_recall_seed", "started_at": previous.get("started_at") or datetime.now(timezone.utc).isoformat(),
             "updated_at": datetime.now(timezone.utc).isoformat(), "resources": []}
    env = os.environ.copy()
    env["TMPDIR"] = str(output / "cache")
    if args.proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[key] = args.proxy
    env["DOUBAN_REQUEST_INTERVAL"] = str(args.douban_interval)
    env["DOUBAN_RATE_STATE"] = str(output / "cache/douban-request-rate.txt")
    env.setdefault("CLIPROXY_WEB_TIMEOUT", "30")

    api = _qb_api(args.qb_url, _qb_config_key(args.qb_config))
    try:
        response = api.get(args.qb_url.rstrip("/") + "/api/v2/torrents/info", timeout=25)
        response.raise_for_status()
        qbtasks = {str(row.get("hash", "")).lower(): row for row in response.json()}
        for index, (torrent_hash, expected_name, kind) in enumerate(selected, 1):
            old = by_hash.get(torrent_hash, {})
            row = {"hash": torrent_hash, "expected_name": expected_name, "kind": kind,
                   "name": old.get("name", expected_name), "status": "pending"}
            state["resources"].append(row)
            task = qbtasks.get(torrent_hash)
            if task is None:
                row.update(status="missing_from_qb", reason="qB 中找不到此 hash")
                write_summary(output, state)
                continue
            row["name"] = str(task.get("name") or expected_name)
            row["size_bytes"] = int(task.get("size", 0))
            row["output"] = str(output / "tasks" / output_key(row["name"], torrent_hash))
            task_output = Path(row["output"])
            receipt_path = task_output / "publish-result.json"
            existing_receipt = json.loads(receipt_path.read_text()) if receipt_path.is_file() else {}
            if existing_receipt.get("status") == "seeded" and existing_receipt.get("mteam_torrent_id"):
                row.update(status="seeded", mteam_torrent_id=str(existing_receipt["mteam_torrent_id"]),
                           qb_torrent_hash=existing_receipt.get("qb_torrent_hash"), reason="此前流程已完成")
                write_summary(output, state)
                continue
            if not task.get("progress") == 1 or int(task.get("amount_left", 0)) != 0:
                row.update(status="incomplete", reason="qB 源任务尚未完成")
                write_summary(output, state)
                continue
            if args.max_gib > 0 and int(task.get("size", 0)) > args.max_gib * 1024**3:
                row.update(status="over_size_limit", reason=f"超过 {args.max_gib:g} GiB 安全上限")
                write_summary(output, state)
                continue
            current_tags = {part.strip().casefold() for part in str(task.get("tags") or "").split(",")}
            if "mteam" in current_tags and task.get("state") in {"uploading", "stalledUP", "queuedUP", "forcedUP"}:
                row.update(status="seeded", reason="qB 已存在 Mteam 标签且处于做种状态")
                write_summary(output, state)
                continue
            try:
                source = host_source(str(task.get("content_path") or ""))
                files_response = api.get(args.qb_url.rstrip("/") + "/api/v2/torrents/files",
                                         params={"hash": torrent_hash}, timeout=25)
                files_response.raise_for_status()
                original_source, staging_source, before = stage_qb_files(task, files_response.json(), task_output)
                if source.resolve() != original_source.resolve():
                    raise ValueError("source_path_changed")
                row["source"] = str(original_source)
                row["staging"] = str(staging_source)
            except (OSError, ValueError) as exc:
                row.update(status="source_invalid", reason=str(exc) if re.fullmatch(r"[a-z0-9_]+", str(exc)) else type(exc).__name__)
                write_summary(output, state)
                continue
            prior = previous_task(original_source)
            if prior:
                prior_receipt_path = Path(str(prior.get("receipt") or ""))
                if prior_receipt_path.is_file():
                    prior_receipt = json.loads(prior_receipt_path.read_text())
                    prior_qb_hash = str(prior_receipt.get("qb_torrent_hash") or "").lower()
                    seeded_task = qbtasks.get(prior_qb_hash)
                    seeded_tags = {part.strip().casefold() for part in str((seeded_task or {}).get("tags") or "").split(",")}
                    if (prior_receipt.get("status") == "seeded" and prior_receipt.get("mteam_torrent_id")
                            and seeded_task and "mteam" in seeded_tags
                            and seeded_task.get("state") in {"uploading", "stalledUP", "queuedUP", "forcedUP"}):
                        row.update(status="seeded", mteam_torrent_id=str(prior_receipt["mteam_torrent_id"]),
                                   qb_torrent_hash=prior_qb_hash, reason="已验证既有发布回执及 qB Mteam 做种任务")
                        write_summary(output, state)
                        continue
            row.update(status="running", reason="資料識別 → 查重 → 發布 → 官方召回 → qB 校验")
            write_summary(output, state)
            print(f"[{index}/{len(selected)}] {row['name']} ({row['size_bytes'] / 1024**3:.2f} GiB)", flush=True)
            task_output.mkdir(parents=True, exist_ok=True)
            # Reconcile user-confirmed series anchors before choosing whether a
            # completed preview can be submitted without re-hashing hundreds of GiB.
            apply_explicit_user_facts(torrent_hash, task_output)
            package_path = task_output / "mteam-prepare.json"
            review_path = task_output / "review.json"
            cached_review = json.loads(review_path.read_text()) if review_path.is_file() else {}
            reuse_ready = (
                package_path.is_file()
                and cached_review.get("status") == "ready"
                and not cached_review.get("blockers")
                and (task_output / "ai-input.json").is_file()
                and (task_output / "ai-output.json").is_file()
            )
            command = [sys.executable, "-u", "-c", "from media_title_renamer.cli import main;main()",
                       "auto", str(package_path if reuse_ready else staging_source), "--gpt",
                       "--submit" if reuse_ready else "--preview", "--web-search", "--kind", kind,
                       "--output", str(task_output), "--qb-url", args.qb_url, "--qb-config", str(args.qb_config),
                       "--qb-container", args.qb_container, "--moviepilot-container", args.moviepilot_container,
                       "--site-id", str(args.site_id), "--max-gib", str(args.max_gib),
                       "--prepare-timeout", str(args.timeout)]
            if torrent_hash in REFRESH_HASHES:
                command.append("--refresh")
            douban_cookie = Path("/data/Zhyw/media-stack/moviepilot-maintenance/douban.xlsx")
            if douban_cookie.is_file():
                command.extend(["--douban-cookie-file", str(douban_cookie)])
            started = time.monotonic()
            try:
                returncode = run_child(command, env=env, task_output=task_output, timeout=args.timeout)
                apply_explicit_user_facts(torrent_hash, task_output)
                review_path = task_output / "review.json"
                review = json.loads(review_path.read_text()) if review_path.is_file() else {}
                receipt = json.loads(receipt_path.read_text()) if receipt_path.is_file() else {}
                if not receipt.get("mteam_torrent_id") and review.get("status") == "ready":
                    package_path = task_output / "mteam-prepare.json"
                    draft = json.loads(package_path.read_text()) if package_path.is_file() else {}
                    release = str(task.get("name") or "")
                    is_disc = (source.suffix.casefold() == ".iso"
                               or any(path.name.casefold() == "bdmv" for path in source.rglob("*")))
                    has_remux = bool(re.search(r"\bREMUX\b", release, re.I))
                    is_anime_bdrip = (draft.get("category") in {"動畫", "动画"}
                                      and draft.get("source") in {"BluRay", "BDRip"})
                    if not is_disc and not has_remux and not is_anime_bdrip and draft.get("source") != "WEB-DL":
                        row.update(status="needs_attention", reason="source_policy_conflict")
                        write_summary(output, state)
                        continue
                    submit_command = list(command)
                    if "--preview" in submit_command:
                        submit_command[submit_command.index("--preview")] = "--submit"
                        submit_command[submit_command.index(str(staging_source))] = str(package_path)
                    print("[提交] 预览字段与来源规则通过，复用资料包开始发布召回…", flush=True)
                    returncode = run_child(submit_command, env=env, task_output=task_output, timeout=args.timeout)
                    review = json.loads(review_path.read_text()) if review_path.is_file() else {}
                    receipt = json.loads(receipt_path.read_text()) if receipt_path.is_file() else {}
                if receipt.get("status") == "seeded" or review.get("status") == "seeded":
                    final_receipt = receipt
                    if not final_receipt.get("mteam_torrent_id"):
                        prior = previous_task(original_source)
                        if prior and Path(str(prior.get("receipt") or "")).is_file():
                            final_receipt = json.loads(Path(prior["receipt"]).read_text())
                    row.update(status="seeded", mteam_torrent_id=str(receipt.get("mteam_torrent_id") or ""),
                               qb_torrent_hash=receipt.get("qb_torrent_hash"), reason="发布、官方召回及 qB 做种验证完成")
                    row["mteam_torrent_id"] = str(final_receipt.get("mteam_torrent_id") or review.get("mteam_torrent_id") or "")
                    row["qb_torrent_hash"] = final_receipt.get("qb_torrent_hash") or review.get("qb_torrent_hash")
                elif receipt.get("mteam_torrent_id"):
                    row.update(status="published_recall_pending", mteam_torrent_id=str(receipt["mteam_torrent_id"]),
                               reason=str(receipt.get("status") or "待续接召回"))
                elif receipt.get("submit_attempted_at"):
                    row.update(status="publish_result_ambiguous", reason="发布请求已提交但缺少成功回执，查看任务日志后续接")
                else:
                    blockers = review.get("blockers") or []
                    row.update(status="needs_attention" if blockers else "failed",
                               reason="、".join(map(str, blockers)) or f"CLI 退出码 {returncode}")
                row["elapsed_seconds"] = round(time.monotonic() - started, 1)
                if not source_manifest_unchanged(original_source, before):
                    row.update(status="source_changed", reason="源文件指纹发生变化，暂停后续自动处理")
            except subprocess.TimeoutExpired:
                if "process" in locals() and process.poll() is None:
                    process.terminate()
                    try: process.wait(timeout=10)
                    except subprocess.TimeoutExpired: process.kill()
                row.update(status="timed_out", reason="已超时；检查 receipt 后再重试，避免重复发布")
            except KeyboardInterrupt:
                if "process" in locals() and process.poll() is None:
                    process.terminate()
                    try: process.wait(timeout=10)
                    except subprocess.TimeoutExpired: process.kill()
                row.update(status="interrupted", reason="人工中断；可原目录续跑")
                write_summary(output, state)
                return 130
            write_summary(output, state)
            print(f"[状态] {row['status']}：{row.get('mteam_torrent_id') or row.get('reason', '')}", flush=True)
    finally:
        api.close()
    print(f"[完成] 报告：{output / 'summary.md'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
