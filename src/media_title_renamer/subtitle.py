"""Prepare and fill M-Team subtitle uploads.

Series subtitles are packed into a ZIP as required by M-Team.  Browser
automation fills every requested upload row but does not submit by default.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import zipfile
from pathlib import Path

from .mteam_fill import (
    MTeamSession,
    _has_mteam_auth,
    _load_selenium,
    _origin,
    _set_local_storage,
    _wait_for_mteam_auth,
    _wait_for_publish_page,
    load_mteam_session,
)


SUBTITLE_URL = "https://kp.m-team.cc/subtitle"
RAW_SUBTITLE_EXTENSIONS = {".srt", ".ass"}
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z"}
UPLOAD_EXTENSIONS = RAW_SUBTITLE_EXTENSIONS | ARCHIVE_EXTENSIONS
SIMPLIFIED_CHINESE_LABELS = ("简体中文", "簡體中文")


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


def subtitle_filename(series: str, season: int, episode: int) -> str:
    clean_series = re.sub(r"\s+", ".", series.strip())
    clean_series = re.sub(r"[^\w.-]+", ".", clean_series, flags=re.UNICODE).strip(".")
    if not clean_series:
        raise ValueError("剧名不能为空")
    if season < 0 or episode < 1:
        raise ValueError("季数必须不小于 0，集数必须大于 0")
    return f"{clean_series}.S{season:02d}E{episode:02d}.chs.srt"


def generate_empty_series_subtitles(
    output: Path,
    *,
    series: str,
    season: int,
    episode_count: int,
    overwrite: bool = False,
) -> tuple[list[Path], Path]:
    """Create empty SRT fixtures and a compliant series ZIP for UI testing."""
    if episode_count < 1:
        raise ValueError("集数必须大于 0")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    paths = [output / subtitle_filename(series, season, episode) for episode in range(1, episode_count + 1)]
    existing = [path for path in paths if path.exists()]
    archive_name = re.sub(r"\s+", ".", series.strip()).strip(".") + f".S{season:02d}.chs.zip"
    archive = output / archive_name
    if not overwrite and (existing or archive.exists()):
        conflicts = [*existing, *([archive] if archive.exists() else [])]
        raise FileExistsError("以下测试文件已经存在；如需覆盖请添加 --overwrite：" + "；".join(map(str, conflicts)))
    for path in paths:
        path.write_bytes(b"")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in paths:
            bundle.write(path, arcname=path.name)
    return paths, archive


def _collect_inputs(values: list[Path]) -> list[Path]:
    collected: list[Path] = []
    for value in values:
        resolved = value.resolve()
        if resolved.is_dir():
            collected.extend(
                sorted(
                    (
                        path
                        for path in resolved.rglob("*")
                        if path.is_file() and path.suffix.casefold() in UPLOAD_EXTENSIONS
                    ),
                    key=lambda path: str(path).casefold(),
                )
            )
        elif resolved.is_file() and resolved.suffix.casefold() in UPLOAD_EXTENSIONS:
            collected.append(resolved)
        elif resolved.exists():
            raise ValueError(f"不支持的字幕文件类型：{resolved}")
        else:
            raise FileNotFoundError(f"找不到字幕文件或目录：{resolved}")
    unique: list[Path] = []
    seen: set[str] = set()
    for path in collected:
        key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    if not unique:
        raise ValueError("没有找到可上传的 .srt、.ass、.zip、.rar 或 .7z 文件")
    return unique


def _validate_chs_name(path: Path) -> None:
    suffix = path.suffix.casefold()
    if suffix not in UPLOAD_EXTENSIONS:
        raise ValueError(f"不支持的字幕文件类型：{path}")
    if not path.stem.casefold().endswith(".chs"):
        raise ValueError(
            f"简体中文字幕名必须在扩展名前包含 .chs：{path.name}；"
            "例如 Video.Name.S01E01.chs.srt"
        )


def _series_archive_name(paths: list[Path]) -> str:
    first = paths[0]
    match = re.match(r"(.+?)\.S(\d{1,2})E\d{1,3}\.chs$", first.stem, re.I)
    if match and all(
        re.match(rf"{re.escape(match.group(1))}\.S{int(match.group(2)):02d}E\d{{1,3}}\.chs$", path.stem, re.I)
        for path in paths
    ):
        return f"{match.group(1)}.S{int(match.group(2)):02d}.chs.zip"
    return "Series.Subtitles.chs.zip"


def _pack_raw_subtitles(paths: list[Path]) -> Path:
    for path in paths:
        _validate_chs_name(path)
    archive = paths[0].parent / _series_archive_name(paths)
    if archive.exists():
        original = archive
        base = original.stem[:-4] if original.stem.casefold().endswith(".chs") else original.stem
        archive = original.with_name(f"{base}.mteam-upload.chs{original.suffix}")
        counter = 2
        while archive.exists():
            archive = original.with_name(
                f"{base}.mteam-upload-{counter}.chs{original.suffix}"
            )
            counter += 1
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in paths:
            bundle.write(path, arcname=path.name)
    print(f"已按剧集字幕规则打包：{archive}（{len(paths)} 个字幕）")
    return archive


def prepare_upload_files(values: list[Path]) -> list[Path]:
    """Resolve inputs and pack multiple raw series subtitles into one ZIP."""
    collected = _collect_inputs(values)
    raw = [path for path in collected if path.suffix.casefold() in RAW_SUBTITLE_EXTENSIONS]
    archives = [path for path in collected if path.suffix.casefold() in ARCHIVE_EXTENSIONS]
    for archive in archives:
        _validate_chs_name(archive)
    if len(raw) > 1:
        expected_archive = raw[0].parent / _series_archive_name(raw)
        existing = next(
            (archive for archive in archives if archive.resolve() == expected_archive.resolve()),
            None,
        )
        if existing:
            print(f"发现同名剧集字幕包，直接使用且不覆盖：{existing}")
        else:
            archives.insert(0, _pack_raw_subtitles(raw))
    elif raw:
        _validate_chs_name(raw[0])
        archives.insert(0, raw[0])
    result: list[Path] = []
    seen: set[str] = set()
    for path in archives:
        key = str(path.resolve()).casefold()
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def ensure_nonempty_for_submission(paths: list[Path]) -> None:
    """Refuse the generated empty fixtures (including empty entries in ZIPs)."""
    for path in paths:
        if path.stat().st_size <= 0:
            raise ValueError(f"拒绝提交空字幕文件：{path}")
        if path.suffix.casefold() != ".zip":
            continue
        try:
            with zipfile.ZipFile(path) as bundle:
                subtitle_entries = [
                    item
                    for item in bundle.infolist()
                    if not item.is_dir() and Path(item.filename).suffix.casefold() in RAW_SUBTITLE_EXTENSIONS
                ]
                if not subtitle_entries:
                    raise ValueError(f"ZIP 中没有找到字幕：{path}")
                empty = [item.filename for item in subtitle_entries if item.file_size <= 0]
                if empty:
                    raise ValueError(
                        f"拒绝提交含空字幕的测试 ZIP：{path}；空文件：{'、'.join(empty[:5])}"
                    )
        except zipfile.BadZipFile as exc:
            raise ValueError(f"无法读取 ZIP：{path}") from exc


def _wait_for_subtitle_form(driver, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready = driver.execute_script(
            """
            const files = document.querySelectorAll('input[type=file][accept*=".srt"]');
            const submit = document.querySelector('button[type=submit]');
            return files.length > 0 && Boolean(submit);
            """
        )
        if ready:
            return
        time.sleep(0.5)
    raise RuntimeError("字幕上传表单加载超时")


def _set_react_value(driver, element, value: str) -> None:
    driver.execute_script(
        """
        const element = arguments[0];
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        setter.call(element, arguments[1]);
        element.dispatchEvent(new Event('input', {bubbles: true}));
        element.dispatchEvent(new Event('change', {bubbles: true}));
        element.dispatchEvent(new Event('blur', {bubbles: true}));
        """,
        element,
        value,
    )


def _torrent_id_input(driver):
    return driver.execute_script(
        """
        const items = [...document.querySelectorAll('.ant-form-item')];
        const item = items.find(el => /种子\\s*ID|種子\\s*ID|torrent\\s*id/i.test(el.innerText || ''));
        return item ? item.querySelector('input:not([type=file])') : null;
        """
    )


def _add_upload_row(driver) -> None:
    clicked = driver.execute_script(
        """
        const button = [...document.querySelectorAll('button')].find(el =>
          /添加更多字幕|新增更多字幕|add more subtitles/i.test((el.innerText || '').trim())
        );
        if (!button) return false;
        button.click();
        return true;
        """
    )
    if not clicked:
        raise RuntimeError("找不到“添加更多字幕”按钮")


def _upload_rows(driver):
    return driver.find_elements("css selector", 'input[type=file][accept*=".srt"]')


def _select_simplified_chinese(driver, row_index: int) -> None:
    selector = driver.execute_script(
        """
        const inputs = [...document.querySelectorAll('input[type=file][accept*=".srt"]')];
        const input = inputs[arguments[0]];
        if (!input) return null;
        const row = input.closest('.ant-row');
        return row && row.querySelector('.ant-select');
        """,
        row_index,
    )
    if not selector:
        raise RuntimeError("找不到字幕语言选择框")
    selector.click()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        selected = driver.execute_script(
            """
            const labels = arguments[0];
            const options = [...document.querySelectorAll('[role=option], .ant-select-item-option')]
              .filter(el => el.offsetParent !== null);
            const option = options.find(el => labels.some(label =>
              (el.innerText || '').replace(/\\s+/g, '').includes(label.replace(/\\s+/g, ''))
            ));
            if (!option) return false;
            option.click();
            return true;
            """,
            list(SIMPLIFIED_CHINESE_LABELS),
        )
        if selected:
            return
        time.sleep(0.25)
    raise RuntimeError("字幕语言列表中没有找到“简体中文”")


def _set_row_title(driver, row_index: int, title: str) -> None:
    title_input = driver.execute_script(
        """
        const inputs = [...document.querySelectorAll('input[type=file][accept*=".srt"]')];
        const input = inputs[arguments[0]];
        const row = input && input.closest('.ant-row');
        if (!row) return null;
        return [...row.querySelectorAll('input:not([type=file])')]
          .find(el => el.getAttribute('role') !== 'combobox' &&
            !el.classList.contains('ant-select-selection-search-input')) || null;
        """,
        row_index,
    )
    if not title_input:
        raise RuntimeError("找不到字幕标题输入框")
    _set_react_value(driver, title_input, title)


def fill_subtitle_form(driver, torrent_id: int, paths: list[Path]) -> None:
    _wait_for_subtitle_form(driver)
    torrent_input = _torrent_id_input(driver)
    if not torrent_input:
        raise RuntimeError("找不到种子 ID 输入框")
    _set_react_value(driver, torrent_input, str(torrent_id))

    for index, path in enumerate(paths):
        rows = _upload_rows(driver)
        while len(rows) <= index:
            _add_upload_row(driver)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and len(rows) <= index:
                time.sleep(0.2)
                rows = _upload_rows(driver)
            if len(rows) <= index:
                raise RuntimeError("新增字幕上传行超时")
        file_input = rows[index]
        file_input.send_keys(str(path.resolve()))
        _select_simplified_chinese(driver, index)
        _set_row_title(driver, index, path.stem)


def _set_anonymous(driver) -> None:
    changed = driver.execute_script(
        """
        const items = [...document.querySelectorAll('.ant-form-item')];
        const item = items.find(el => /匿名上传|匿名上傳|隐藏用户名|隱藏用戶名/i.test(el.innerText || ''));
        const checkbox = item && item.querySelector('input[type=checkbox]');
        if (!checkbox) return false;
        if (!checkbox.checked) checkbox.click();
        return true;
        """
    )
    if not changed:
        raise RuntimeError("找不到匿名上传选项")


def _click_submit(driver) -> None:
    button = driver.find_element("css selector", "button[type=submit]")
    button.click()


def _wait_for_submission(driver, timeout: float = 120.0) -> bool:
    """Keep Chrome alive until the SPA reports success or failure."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = driver.execute_script(
            """
            const messages = [...document.querySelectorAll(
              '.ant-message-notice-content, .ant-notification-notice-message, .ant-notification-notice-description'
            )].map(el => (el.innerText || '').trim()).join('\n');
            if (/上传完毕|上傳完畢|上传完成|上傳完成/i.test(messages)) return 'success';
            if (/上传失败|上傳失敗|部分上传失败|部分上傳失敗/i.test(messages)) return 'failure';
            return '';
            """
        )
        if state == "success":
            return True
        if state == "failure":
            return False
        time.sleep(0.25)
    return False


def _subtitle_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="批量准备并填写 M-Team 字幕上传页；默认停在最终提交之前"
    )
    parser.add_argument("inputs", type=Path, nargs="+", help="字幕文件、压缩包或目录；多个裸字幕会自动打包为 ZIP")
    parser.add_argument("--torrent-id", type=int, required=True, help="M-Team 种子 ID，只填写数字")
    parser.add_argument("--profile-dir", type=Path, default=_default_profile_dir(), help="专用 Chrome 登录配置目录")
    parser.add_argument("--cookie-file", type=Path, help="M-Team Cookie 或请求头导出文件")
    parser.add_argument("--url", default=SUBTITLE_URL, help="M-Team 字幕页地址")
    parser.add_argument("--anonymous", action="store_true", help="勾选匿名上传")
    parser.add_argument("--submit", action="store_true", help="实际点击提交并上传；空字幕和空 ZIP 会被拒绝")
    parser.add_argument("--yes", action="store_true", help="与 --submit 同用时跳过最终确认")
    parser.add_argument("--keep-open", action="store_true", help="完成后等待回车再关闭浏览器")
    parser.add_argument("--login-timeout", type=int, default=600, help="等待手工登录的秒数；默认 600")
    return parser


def subtitle_main(argv: list[str] | None = None) -> None:
    parser = _subtitle_parser()
    args = parser.parse_args(argv)
    driver = None
    try:
        if args.torrent_id <= 0:
            raise ValueError("种子 ID 必须是大于 0 的整数")
        if args.cookie_file and not args.cookie_file.is_file():
            raise FileNotFoundError(f"找不到会话文件：{args.cookie_file}")
        paths = prepare_upload_files(args.inputs)
        if args.submit:
            ensure_nonempty_for_submission(paths)
            if not args.yes:
                answer = input(
                    f"即将向种子 ID {args.torrent_id} 实际上传 {len(paths)} 个字幕项目；"
                    "提交后会立即写入 M-Team。继续？[y/N] "
                ).strip().casefold()
                if answer not in {"y", "yes"}:
                    print("已取消上传。")
                    return

        session = load_mteam_session(args.cookie_file) if args.cookie_file else MTeamSession()
        webdriver = _load_selenium()
        options = webdriver.ChromeOptions()
        if args.profile_dir:
            options.add_argument(f"--user-data-dir={args.profile_dir.resolve()}")
        driver = webdriver.Chrome(options=options)
        driver.get(_origin(args.url))
        if session.is_auth_dump:
            _set_local_storage(driver, session)
            driver.refresh()
        else:
            for cookie in session.cookies:
                try:
                    driver.add_cookie(cookie)
                except Exception as exc:
                    print(f"提示：跳过一条不兼容 Cookie（{type(exc).__name__}）。")
            driver.refresh()
        time.sleep(2)
        auth_present = _has_mteam_auth(driver)
        if args.profile_dir and not auth_present:
            auth_present = _wait_for_mteam_auth(driver, args.login_timeout)
        if not auth_present:
            raise ValueError("M-Team 登录态不可用；请重新登录，或提供当前有效的 --cookie-file")
        if driver.current_url.rstrip("/") != args.url.rstrip("/"):
            driver.get(args.url)
            time.sleep(2)
        if not _wait_for_publish_page(driver, args.url, args.login_timeout):
            raise ValueError("M-Team 登录超时；请重新运行并在 ChromeDriver 窗口完成登录")

        fill_subtitle_form(driver, args.torrent_id, paths)
        if args.anonymous:
            _set_anonymous(driver)
        if args.submit:
            _click_submit(driver)
            if not _wait_for_submission(driver):
                raise RuntimeError("字幕提交未在 120 秒内确认成功，或页面报告部分上传失败；请检查浏览器页面")
            print(f"已向种子 ID {args.torrent_id} 成功上传 {len(paths)} 个字幕项目。")
        else:
            print(
                f"已为种子 ID {args.torrent_id} 填入 {len(paths)} 个字幕项目，语言均为简体中文；"
                "已停在最终提交之前。"
            )
        if args.keep_open or sys.stdin.isatty():
            try:
                input("检查完成后按回车关闭 ChromeDriver 窗口。")
            except EOFError:
                pass
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError, OSError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
    finally:
        if driver is not None:
            driver.quit()


def _generate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成一整季空 SRT 和 ZIP，仅用于 M-Team 字幕页界面测试")
    parser.add_argument("output", type=Path, help="测试字幕输出目录")
    parser.add_argument("--series", required=True, help="剧名，例如 Survivor")
    parser.add_argument("--season", type=int, required=True, help="季数，例如 7")
    parser.add_argument("--episode-count", type=int, required=True, help="总集数，例如 15")
    parser.add_argument("--overwrite", action="store_true", help="覆盖同名测试文件")
    return parser


def generate_main(argv: list[str] | None = None) -> None:
    parser = _generate_parser()
    args = parser.parse_args(argv)
    try:
        paths, archive = generate_empty_series_subtitles(
            args.output,
            series=args.series,
            season=args.season,
            episode_count=args.episode_count,
            overwrite=args.overwrite,
        )
        print(f"已生成 {len(paths)} 个空 SRT 测试文件：{paths[0].parent}")
        print(f"已生成剧集字幕测试包：{archive}")
        print("这些文件没有字幕内容，只能测试页面选择和填写；程序会拒绝使用 --submit 上传。")
    except (FileExistsError, ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    subtitle_main()
