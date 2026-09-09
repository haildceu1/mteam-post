"""One-command M-Team preparation and browser form filling."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .mteam_fill import main as mteam_fill_main
from .prepare import main as prepare_main


def _default_profile_dir() -> Path:
    configured = os.environ.get("MTEAM_PROFILE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "win32":
        preferred = Path(r"D:\Cinema\mteam")
        if preferred.is_dir():
            return preferred
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data) / "mteam-post" / "chrome-profile"
        return Path.home() / "AppData" / "Local" / "mteam-post" / "chrome-profile"
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    config_home = Path(xdg_config_home).expanduser() if xdg_config_home else Path.home() / ".config"
    return config_home / "mteam-post" / "chrome-profile"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "一条命令完成 M-Team 资料准备并打开发布页填写。"
            "其余参数会原样交给 prepare，例如 --tmdb-id、--category、--screenshots。"
        )
    )
    parser.add_argument(
        "input",
        type=Path,
        help="视频/ISO、剧集目录、mteam-prepare.json，或包含该 JSON 的 .prepare 目录",
    )
    reuse = parser.add_mutually_exclusive_group()
    reuse.add_argument(
        "--reuse-prepare",
        action="store_true",
        help="要求复用匹配的 .prepare 资料包；现在默认会自动复用，保留此参数用于严格检查",
    )
    reuse.add_argument(
        "--refresh-prepare",
        action="store_true",
        help="忽略已有资料包，强制重新探测、截图并生成种子（通常与 --apply 一起使用）",
    )
    parser.add_argument(
        "--reuse-torrent",
        action="store_true",
        help="配合 --refresh-prepare 重新获取发布资料但复用现有种子，不重新哈希",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=_default_profile_dir(),
        help="专用 Chrome 配置目录；默认读取 MTEAM_PROFILE_DIR，否则使用系统用户配置目录",
    )
    parser.add_argument("--cookie-file", type=Path, help="M-Team Cookie 导出或请求头复制文件")
    parser.add_argument("--url", default="https://kp.m-team.cc/upload", help="M-Team 发布页地址")
    uploads = parser.add_mutually_exclusive_group()
    uploads.add_argument("--upload", dest="upload", action="store_true", help="上传种子和 4 张截图（默认）")
    uploads.add_argument("--no-upload", dest="upload", action="store_false", help="只填写字段，不上传文件")
    parser.set_defaults(upload=True)
    parser.add_argument("--yes", action="store_true", help="跳过发布页写入/上传前的确认")
    parser.add_argument("--keep-open", action="store_true", help="填表后保持 ChromeDriver 窗口打开")
    parser.add_argument("--login-timeout", type=int, default=600, help="等待手工登录的秒数；默认 600")
    return parser


def _direct_package(path: Path) -> Path | None:
    """Resolve an explicitly supplied JSON package or prepare directory."""
    resolved = path.resolve()
    if resolved.is_file() and resolved.name.casefold() == "mteam-prepare.json":
        return resolved
    if resolved.is_dir():
        package = resolved / "mteam-prepare.json"
        if package.is_file():
            return package
    return None


def _normalised_path(value: str | Path) -> str:
    # Windows paths are case-insensitive while Linux paths are not.
    return os.path.normcase(str(Path(value).resolve()))


def _find_existing_package(input_path: Path) -> Path:
    """Find the newest adjacent package that identifies the same media input."""
    resolved = input_path.resolve()
    wanted = _normalised_path(resolved)
    parent = resolved.parent
    matches: list[tuple[int, int, Path]] = []
    for candidate in parent.glob("*.prepare/mteam-prepare.json"):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        references = {
            _normalised_path(value)
            for key in ("input_path", "prepared_path")
            if (value := payload.get(key)) and isinstance(value, str)
        }
        if wanted not in references:
            continue
        created_at = int(payload.get("created_at") or 0)
        matches.append((created_at, candidate.stat().st_mtime_ns, candidate.resolve()))
    if not matches:
        raise FileNotFoundError(
            f"没有找到与 {input_path} 匹配的现有 .prepare 资料包；"
            "请直接传入 mteam-prepare.json，或先运行 prepare"
        )
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def _apply_reused_single_file_rename(package_path: Path, input_path: Path) -> bool:
    """Apply a reused package's canonical name to one source file.

    ``prepare`` can be run without ``--apply`` to inspect and build all
    publication assets first.  When that package is later reused by
    ``publish --apply``, the torrent already contains the canonical target
    name, so the source file must be renamed before the browser form is
    filled.  Directory packages deliberately stay untouched here: their
    season/disc rename plan is applied atomically by ``prepare`` itself.
    """
    if not input_path.is_file():
        return False

    try:
        payload = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False

    recorded_input = payload.get("input_path")
    target_filename = payload.get("filename")
    if not isinstance(recorded_input, str) or not isinstance(target_filename, str):
        return False
    if _normalised_path(input_path) != _normalised_path(recorded_input):
        return False

    # ``filename`` is generated by prepare and should be a basename.  Using
    # only the basename also prevents a stale/malformed package from moving a
    # file outside its original directory.
    target_name = Path(target_filename).name
    if not target_name or target_name in {".", ".."}:
        return False
    target = input_path.with_name(target_name)
    if target == input_path:
        return False
    if target.exists():
        raise FileExistsError(f"目标文件已存在：{target}")

    input_path.rename(target)
    payload["prepared_path"] = str(target)
    package_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"已按 M-Team 标题重命名单个文件：{input_path.name} → {target.name}")
    return True


def _load_package(package_path: Path) -> dict[str, object]:
    try:
        payload = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 M-Team 资料包：{package_path}：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"M-Team 资料包格式无效：{package_path}")
    return payload


def _package_layout(payload: dict[str, object]) -> tuple[str, tuple[str, ...]]:
    """Return the names that are part of the torrent's logical layout."""
    filename = payload.get("filename")
    if not isinstance(filename, str) or not filename:
        raise ValueError("资料包缺少 filename，无法安全复用种子")
    kind = str(payload.get("kind") or "")
    if kind != "tv":
        return filename.casefold(), ()
    records = payload.get("files")
    if not isinstance(records, list):
        raise ValueError("电视剧资料包缺少 files，无法安全复用目录种子")
    relative_paths: list[str] = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("relative_path"), str):
            raise ValueError("电视剧资料包的 files 记录无效，无法安全复用目录种子")
        relative_paths.append(Path(str(record["relative_path"])).as_posix().casefold())
    return filename.casefold(), tuple(sorted(relative_paths))


def _package_torrent(payload: dict[str, object], package_path: Path) -> tuple[dict[str, object], Path]:
    torrent = payload.get("torrent")
    if not isinstance(torrent, dict):
        raise ValueError(f"资料包没有 torrent 信息，无法复用种子：{package_path}")
    torrent_value = torrent.get("path")
    if not isinstance(torrent_value, str) or not torrent_value.strip():
        raise FileNotFoundError(f"资料包没有记录现有种子路径：{package_path}")
    torrent_path = Path(torrent_value)
    if not torrent_path.is_file():
        raise FileNotFoundError(f"找不到资料包中的现有种子：{torrent_path}")
    return torrent, torrent_path


def _refresh_with_reused_torrent(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    package_path: Path,
    prepare_options: list[str],
) -> Path:
    """Refresh publication fields while keeping an already hashed torrent.

    The refresh prepare run intentionally omits ``--apply`` and
    ``--skip-torrent`` is passed to the prepare layer.  This lets us compare
    the newly calculated logical names with the old package before changing
    any media path.  A changed logical name would make the old torrent
    incompatible, so it is rejected instead of producing a misleading form.
    """
    if any(option == "--output" or option.startswith("--output=") for option in prepare_options):
        parser.error("--reuse-torrent 不允许同时指定 --output；资料会更新到现有 .prepare 目录")
    if "--skip-torrent" in prepare_options:
        parser.error("--reuse-torrent 已经负责跳过制种，不要重复传 --skip-torrent")

    old_payload = _load_package(package_path)
    old_torrent, _torrent_path = _package_torrent(old_payload, package_path)
    old_layout = _package_layout(old_payload)
    apply_requested = "--apply" in prepare_options
    old_input = Path(str(old_payload.get("input_path") or ""))
    old_prepared = Path(str(old_payload.get("prepared_path") or ""))
    source = args.input.resolve()
    if old_prepared.is_file() or old_prepared.is_dir():
        # An already-applied package's canonical path is the safest source
        # for a refresh.  It also allows callers to pass the former original
        # path after the file/folder has already been renamed.
        if _normalised_path(old_prepared) != _normalised_path(old_input):
            source = old_prepared.resolve()
    if not source.is_file() and not source.is_dir():
        raise FileNotFoundError(f"找不到资料包对应的媒体路径：{source}")

    if apply_requested and str(old_payload.get("kind") or "") == "tv":
        # A folder torrent made by prepare without --apply contains the old
        # root name.  Renaming that root now would make the reused torrent
        # invalid; require a full prepare instead.
        if _normalised_path(old_input) == _normalised_path(old_prepared):
            parser.error(
                "现有电视剧种子是在未改名状态下生成的，不能在复用种子时同时改目录名；"
                "请移除 --reuse-torrent 重新制种"
            )

    generation_options = [option for option in prepare_options if option != "--apply"]
    generation_options.extend(["--skip-torrent", "--output", str(package_path.parent)])
    generated = prepare_main([str(source), *generation_options])
    if generated is None:
        raise RuntimeError("刷新发布资料没有返回资料包")
    generated_path = Path(generated).resolve()
    if not generated_path.is_file():
        raise FileNotFoundError(f"刷新发布资料包不存在：{generated_path}")
    refreshed = _load_package(generated_path)
    if _package_layout(refreshed) != old_layout:
        # prepare has not been run with --apply, so media paths are still
        # untouched. Restore the old package when output was in-place.
        if generated_path == package_path.resolve():
            package_path.write_text(
                json.dumps(old_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        parser.error(
            "刷新后规范文件名或电视剧目录结构发生变化，不能复用旧种子；"
            "请移除 --reuse-torrent 后使用 --refresh-prepare --apply 完整重新制种"
        )

    refreshed["torrent"] = old_torrent
    # Keep the original input reference so later automatic package lookup
    # still works even when the package was refreshed from its prepared path.
    if isinstance(old_payload.get("input_path"), str):
        refreshed["input_path"] = old_payload["input_path"]

    renamed = False
    if apply_requested and source.is_file():
        renamed = _apply_reused_single_file_rename(generated_path, source)
        if renamed:
            refreshed = _load_package(generated_path)
            refreshed["torrent"] = old_torrent
            if isinstance(old_payload.get("input_path"), str):
                refreshed["input_path"] = old_payload["input_path"]
    generated_path.write_text(
        json.dumps(refreshed, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"正在刷新 M-Team 发布资料并复用现有种子：{generated_path}")
    print("已跳过种子哈希；MediaInfo/BDInfo、TMDB/豆瓣、截图和 M-Team 字段已刷新。")
    if renamed:
        print("已按刷新后的 M-Team 标题应用单文件改名。")
    elif apply_requested and source.is_dir():
        print("电视剧目录与种子逻辑名称未变化，已保持现有规范目录结构。")
    return generated_path


def main(argv: list[str] | None = None) -> None:
    """Run ``prepare`` followed by ``mteam-fill`` with one user command."""
    parser = _parser()
    args, prepare_options = parser.parse_known_args(argv)

    package_path = _direct_package(args.input)
    direct_package = package_path is not None
    non_apply_options = [option for option in prepare_options if option != "--apply"]
    if direct_package and args.refresh_prepare:
        parser.error("输入已经是资料包，不能同时使用 --refresh-prepare")
    if args.reuse_torrent and not args.refresh_prepare:
        parser.error("--reuse-torrent 必须与 --refresh-prepare 一起使用")
    if args.reuse_torrent and direct_package:
        parser.error("--reuse-torrent 需要传入原始媒体文件或剧集目录，不能直接传资料包")
    if args.reuse_prepare and non_apply_options:
        parser.error(
            "要求复用现有资料包时不能再传 prepare 参数：" + " ".join(non_apply_options)
        )

    if args.reuse_torrent:
        try:
            package_path = _find_existing_package(args.input)
        except FileNotFoundError as exc:
            parser.error(str(exc))
    elif package_path is None and not args.refresh_prepare and not non_apply_options:
        try:
            package_path = _find_existing_package(args.input)
        except FileNotFoundError as exc:
            if args.reuse_prepare:
                parser.error(str(exc))

    if args.reuse_torrent:
        try:
            package_path = _refresh_with_reused_torrent(parser, args, package_path, prepare_options)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            parser.error(str(exc))
    elif package_path is not None:
        unsupported = [option for option in prepare_options if option != "--apply"]
        if unsupported:
            parser.error(
                "复用现有资料包时不能再传 prepare 参数：" + " ".join(unsupported)
            )
        renamed = False
        if not direct_package and "--apply" in prepare_options:
            try:
                renamed = _apply_reused_single_file_rename(package_path, args.input)
            except (FileExistsError, OSError, ValueError) as exc:
                parser.error(str(exc))
        print(f"正在复用现有发布资料包：{package_path}")
        if renamed:
            print("已跳过媒体探测、截图生成和种子哈希；单个文件已应用资料包中的 M-Team 标题。")
        else:
            print("已跳过媒体探测、截图生成、种子哈希和文件改名。")
    else:
        # A torrent references the canonical names generated by prepare.
        # Requiring --apply prevents a one-step run from creating a torrent
        # that no longer matches the files left on disk.
        if "--apply" not in prepare_options:
            parser.error("publish 为保证种子与文件名一致，必须传入 --apply")
        package_path = prepare_main([str(args.input), *prepare_options])
        if package_path is None:
            raise RuntimeError("prepare 未返回 M-Team 发布资料包")

    fill_args = [str(package_path)]
    fill_args.extend(["--profile-dir", str(args.profile_dir)])
    if args.cookie_file:
        fill_args.extend(["--cookie-file", str(args.cookie_file)])
    fill_args.extend(["--url", args.url])
    if args.upload:
        fill_args.append("--upload")
    if args.yes:
        fill_args.append("--yes")
    if args.keep_open:
        fill_args.append("--keep-open")
    fill_args.extend(["--login-timeout", str(args.login_timeout)])
    mteam_fill_main(fill_args)


if __name__ == "__main__":
    main()
