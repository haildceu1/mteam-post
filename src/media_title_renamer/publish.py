"""One-command M-Team preparation and browser form filling."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .mteam_fill import main as mteam_fill_main
from .prepare import main as prepare_main


def _default_profile_dir() -> Path:
    configured = os.environ.get("MTEAM_PROFILE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    preferred = Path(r"D:\Cinema\mteam")
    if preferred.is_dir():
        return preferred
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "mteam-post" / "chrome-profile"
    return Path.home() / "AppData" / "Local" / "mteam-post" / "chrome-profile"


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
        help="忽略已有资料包，强制重新探测、截图并生成种子（必须同时使用 --apply）",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=_default_profile_dir(),
        help="专用 Chrome 配置目录；默认读取 MTEAM_PROFILE_DIR，本机优先使用 D:\\Cinema\\mteam",
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
    return str(Path(value).resolve()).casefold()


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


def main(argv: list[str] | None = None) -> None:
    """Run ``prepare`` followed by ``mteam-fill`` with one user command."""
    parser = _parser()
    args, prepare_options = parser.parse_known_args(argv)

    package_path = _direct_package(args.input)
    direct_package = package_path is not None
    non_apply_options = [option for option in prepare_options if option != "--apply"]
    if direct_package and args.refresh_prepare:
        parser.error("输入已经是资料包，不能同时使用 --refresh-prepare")
    if args.reuse_prepare and non_apply_options:
        parser.error(
            "要求复用现有资料包时不能再传 prepare 参数：" + " ".join(non_apply_options)
        )

    if package_path is None and not args.refresh_prepare and not non_apply_options:
        try:
            package_path = _find_existing_package(args.input)
        except FileNotFoundError as exc:
            if args.reuse_prepare:
                parser.error(str(exc))

    if package_path is not None:
        unsupported = [option for option in prepare_options if option != "--apply"]
        if unsupported:
            parser.error(
                "复用现有资料包时不能再传 prepare 参数：" + " ".join(unsupported)
            )
        print(f"正在复用现有发布资料包：{package_path}")
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
