"""Seed an existing TL file from the official M-Team detail-page copy action."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import sys
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import requests

from .prepare import _bdecode, _bencode


DEFAULT_TL_ROOT = Path("/data/Zhyw/media-stack/downloads/TL")
DEFAULT_DOWNLOADS_ROOT = Path("/data/Zhyw/media-stack/downloads")
DEFAULT_QB_CONFIG = Path("/data/Zhyw/media-stack/qbittorrent-config/qBittorrent/qBittorrent.conf")
DEFAULT_QB_URL = "http://127.0.0.1:8081"
OFFICIAL_DOWNLOAD_HOST = "api2.m-team.cc"
OFFICIAL_DOWNLOAD_HOSTS = frozenset({"api.m-team.cc", OFFICIAL_DOWNLOAD_HOST})
MAX_TORRENT_BYTES = 16 * 1024 * 1024
_COPY_CAPTURE_SCRIPT = r"""
  window.__mteamCopyPayload = '';
  window.__mteamCopySequence = 0;
  const recordCopy = value => {
    window.__mteamCopyPayload = String(value || '');
    window.__mteamCopySequence += 1;
  };
  document.addEventListener('copy', event => {
    try { recordCopy(event.clipboardData.getData('text/plain')); } catch (_) {}
  }, true);
  try {
    const clipboard = navigator.clipboard;
    const originalWriteText = clipboard.writeText.bind(clipboard);
    Object.defineProperty(clipboard, 'writeText', {
      configurable: true,
      value: function (value) {
        recordCopy(value);
        return originalWriteText(value);
      }
    });
  } catch (_) {}
"""


def _validated_link(url: str, torrent_id: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme != "https":
        raise RuntimeError("复制链接不是 HTTPS")
    if (parsed.hostname or "").lower() not in OFFICIAL_DOWNLOAD_HOSTS:
        raise RuntimeError("复制链接不是 M-Team 官方下载域名")
    try:
        port_allowed = parsed.port in (None, 443)
    except ValueError:
        port_allowed = False
    if not port_allowed or parsed.username or parsed.password or parsed.fragment:
        raise RuntimeError("复制链接包含不允许的端口、用户信息或片段")
    if not parsed.path.startswith("/api/"):
        raise RuntimeError("复制链接不是 M-Team 官方 API 路径")
    values = parse_qs(parsed.query, keep_blank_values=True)
    tids = values.get("tid") or values.get("torrentid") or values.get("torrent_id")
    if not tids:
        raise RuntimeError("复制链接缺少条目 ID 参数")
    if any(item != str(torrent_id) for item in tids):
        raise RuntimeError("官方复制链接中的条目 ID 与指定 M-Team 条目不一致")
    return url.strip()


def _wait_for_valid_copied_link(
    read_text: Callable[[], str | list[str]],
    torrent_id: str,
    *,
    previous_text: str = "",
    timeout: float = 8,
    interval: float = 0.2,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Ignore stale clipboard contents until the copy action yields this torrent's link."""
    deadline = clock() + timeout
    last_validation_error: RuntimeError | None = None
    previous_digest = hashlib.sha256(previous_text.strip().encode("utf-8")).digest() if previous_text else None
    while clock() < deadline:
        copied_values = read_text()
        candidates = [copied_values] if isinstance(copied_values, str) else copied_values
        for copied in candidates:
            if not copied:
                continue
            if previous_digest and hashlib.sha256(copied.strip().encode("utf-8")).digest() == previous_digest:
                continue
            try:
                return _validated_link(copied, torrent_id)
            except RuntimeError as exc:
                last_validation_error = exc
        sleep(interval)
    if last_validation_error:
        raise last_validation_error
    raise RuntimeError("复制按钮没有产生可读取的链接")


def _copy_official_link(torrent_id: str, profile_dir: Path, expected_name: str) -> tuple[str, str]:
    """Click the unique detail-page copy icon and capture clipboard text in memory."""
    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        from .mteam_fill import _configure_chrome_options, _has_mteam_auth
    except ImportError as exc:
        raise RuntimeError("需要 Selenium 才能从 M-Team 详情页复制官方链接") from exc

    options = _configure_chrome_options(webdriver.ChromeOptions())
    chrome_binary = os.environ.get("CHROME_BINARY", "").strip()
    if chrome_binary:
        options.binary_location = chrome_binary
    options.add_argument(f"--user-data-dir={profile_dir.expanduser().resolve()}")
    options.add_argument("--disable-notifications")
    driver = None
    user_agent = ""
    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(45)
        driver.execute_cdp_cmd(
            "Browser.grantPermissions",
            {
                "origin": "https://kp.m-team.cc",
                "permissions": ["clipboardReadWrite", "clipboardSanitizedWrite"],
            },
        )
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": _COPY_CAPTURE_SCRIPT},
        )
        driver.get(f"https://kp.m-team.cc/detail/{torrent_id}")
        WebDriverWait(driver, 45).until(
            lambda current: current.execute_script("return document.readyState") == "complete"
        )
        WebDriverWait(driver, 30).until(
            lambda current: expected_name.casefold() in current.title.casefold()
        )
        if not _has_mteam_auth(driver):
            raise RuntimeError("当前 Chrome 配置未登录 M-Team")
        user_agent = str(driver.execute_script("return navigator.userAgent") or "")

        # Invoke the same non-mutating copy action a user would click. The URL
        # is only read into this process and is never printed or written to disk.
        buttons = driver.find_elements(By.CSS_SELECTOR, 'button svg[data-icon="copy"]')
        if len(buttons) != 1:
            raise RuntimeError("详情页未找到唯一的复制链接按钮")
        driver.execute_script(
            "window.__mteamCopyPayload = ''; window.__mteamCopySequence = 0;"
        )
        previous_clipboard = str(driver.execute_async_script(
            "const done=arguments[0]; navigator.clipboard.readText().then(done).catch(()=>done(''));"
        ) or "")
        buttons[0].find_element(By.XPATH, "..").click()

        def read_copied_text() -> str | list[str]:
            captured_text = str(driver.execute_script(
                "return window.__mteamCopySequence > 0 ? window.__mteamCopyPayload : '';"
            ) or "")
            # Some M-Team frontend builds copy via the browser/OS clipboard
            # without calling the wrapped Clipboard API or firing `copy`.
            clipboard_text = str(driver.execute_async_script(
                "const done=arguments[0]; navigator.clipboard.readText().then(done).catch(()=>done(''));"
            ) or "")
            return [captured_text, clipboard_text]

        copied_link = _wait_for_valid_copied_link(
            read_copied_text,
            torrent_id,
            previous_text=previous_clipboard,
        )
        return copied_link, user_agent
    finally:
        if driver is not None:
            driver.quit()


def _download_torrent_bytes(url: str, torrent_id: str, user_agent: str) -> bytes:
    """Fetch the signed official link in memory, with same-host redirects only."""
    last_error: Exception | None = None
    request_failures: list[str] = []
    proxy_configs = [None, {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}]
    for route, proxies in zip(("直连", "Clash 7890"), proxy_configs):
        session = requests.Session()
        session.trust_env = False
        current = url
        try:
            for _ in range(6):
                response = session.get(
                    current,
                    headers={"User-Agent": user_agent, "Referer": f"https://kp.m-team.cc/detail/{torrent_id}"},
                    allow_redirects=False,
                    timeout=(10, 35),
                    proxies=proxies,
                )
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise RuntimeError("M-Team 下载接口返回无 Location 的重定向")
                    current = _validated_link(urljoin(current, location), torrent_id)
                    continue
                if response.status_code != 200:
                    raise RuntimeError(f"M-Team 官方下载接口返回 HTTP {response.status_code}")
                content = bytes(response.content or b"")
                if not content or len(content) > MAX_TORRENT_BYTES:
                    raise RuntimeError("M-Team 官方接口返回空内容或异常大的种子文件")
                return content
            raise RuntimeError("M-Team 官方下载重定向次数过多")
        except (requests.RequestException, RuntimeError) as exc:
            last_error = exc
            if isinstance(exc, requests.RequestException):
                request_failures.append(f"{route}:{type(exc).__name__}")
        finally:
            session.close()
    # Never stringify the network exception: request exceptions can contain the signed URL.
    if isinstance(last_error, RuntimeError):
        raise last_error
    details = "、".join(request_failures) or "无可用连接"
    raise RuntimeError(f"无法连接 M-Team 官方下载接口（{details}；签名内容已隐藏）")


def _torrent_manifest(content: bytes) -> tuple[str, int, str]:
    """Decode the official .torrent without writing it to disk."""
    try:
        metainfo = _bdecode(content)
        info = metainfo[b"info"]
        if b"files" in info:
            raise RuntimeError("当前只接受单文件 M-Team 种子")
        raw_name = info.get(b"name.utf-8") or info[b"name"]
        name = raw_name.decode("utf-8", errors="strict") if isinstance(raw_name, bytes) else str(raw_name)
        size = int(info[b"length"])
        info_hash = hashlib.sha1(_bencode(info)).hexdigest()
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("M-Team 返回内容不是可解析的单文件 BitTorrent 种子") from None
    if not re.fullmatch(r"[0-9a-f]{40}", info_hash):
        raise RuntimeError("无法解析 M-Team 官方种子 info hash")
    return name, size, info_hash


def _source_identity(source: Path, expected_size: int) -> tuple[Path, os.stat_result]:
    root = DEFAULT_TL_ROOT.resolve(strict=True)
    candidate = source.expanduser()
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise RuntimeError("源文件必须是 TL 根目录内的绝对路径")
    if not candidate.is_relative_to(DEFAULT_TL_ROOT):
        raise RuntimeError(f"源文件必须位于 {DEFAULT_TL_ROOT}")
    current = DEFAULT_TL_ROOT
    for part in candidate.relative_to(DEFAULT_TL_ROOT).parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError:
            raise RuntimeError("TL 源文件不存在") from None
        if current.is_symlink():
            raise RuntimeError("源路径包含符号链接，拒绝使用")
        if current != candidate and not current.is_dir():
            raise RuntimeError("源文件路径中间项不是目录")
        if current == candidate and not stat.S_ISREG(mode):
            raise RuntimeError("TL 源文件不是普通文件")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise RuntimeError("源文件解析后越过 TL 根目录")
    details = resolved.stat()
    if details.st_size != expected_size:
        raise RuntimeError(f"TL 源文件大小是 {details.st_size} 字节，不等于预期 {expected_size} 字节")
    return resolved, details


def _qb_config_key(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("WebUI\\APIKey="):
                value = line.split("=", 1)[1].strip()
                if value:
                    return value
    except OSError:
        pass
    raise RuntimeError("未能从 qBittorrent 配置中读取 WebUI API key")


def _qb_api(base_url: str, api_key: str) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({"Authorization": f"Bearer {api_key}"})
    try:
        response = session.get(f"{base_url}/api/v2/app/version", timeout=8)
    except requests.RequestException:
        session.close()
        raise RuntimeError("无法连接 qBittorrent WebUI API") from None
    if response.status_code != 200:
        session.close()
        raise RuntimeError(f"qBittorrent API 认证失败或服务不可用（HTTP {response.status_code}）")
    return session


def _qb_task(session: requests.Session, base_url: str, info_hash: str) -> dict | None:
    response = session.get(
        f"{base_url}/api/v2/torrents/info", params={"hashes": info_hash}, timeout=10
    )
    if response.status_code != 200:
        raise RuntimeError(f"读取 qBittorrent 任务失败（HTTP {response.status_code}）")
    rows = response.json()
    return rows[0] if isinstance(rows, list) and rows else None


def _assert_task_matches(
    session: requests.Session,
    base_url: str,
    info_hash: str,
    source: Path,
    qb_save_path: Path,
    expected_size: int,
) -> dict:
    task = None
    for attempt in range(12):
        task = _qb_task(session, base_url, info_hash)
        if task:
            break
        if attempt < 11:
            time.sleep(1)
    if not task:
        raise RuntimeError("qB 尚未显示新任务；任务未加标签、未启动")

    observed_size = int(task.get("size", task.get("total_size", -1)))
    if observed_size != expected_size:
        raise RuntimeError("qB 任务总大小与 M-Team 官方种子不一致；保持暂停")
    if Path(str(task.get("save_path") or "")).as_posix().rstrip("/") != qb_save_path.as_posix().rstrip("/"):
        raise RuntimeError("qB 保存路径与原 TL 文件目录不一致；保持暂停")
    content_path = str(task.get("content_path") or "")
    if content_path and Path(content_path).as_posix() != (qb_save_path / source.name).as_posix():
        raise RuntimeError("qB content_path 与 TL 源文件路径不一致；保持暂停")

    response = session.get(
        f"{base_url}/api/v2/torrents/files", params={"hash": info_hash}, timeout=10
    )
    if response.status_code != 200:
        raise RuntimeError("无法读取 qB 文件清单；保持暂停")
    files = response.json()
    manifest = [
        (str(item.get("name") or "").replace("\\", "/"), int(item.get("size", -1)))
        for item in files
        if isinstance(item, dict)
    ]
    if manifest != [(source.name, expected_size)]:
        raise RuntimeError("qB 文件清单与 M-Team 官方单文件种子不一致；保持暂停")
    if task.get("state") not in ("pausedUP", "pausedDL", "stoppedUP", "stoppedDL"):
        pause = session.post(
            f"{base_url}/api/v2/torrents/pause", data={"hashes": info_hash}, timeout=10
        )
        if pause.status_code != 200:
            raise RuntimeError("新任务未暂停且补充暂停失败；请立即在 qB 中检查")
        raise RuntimeError("qB 新任务未保持暂停；已再次暂停，未加标签")
    progress = float(task.get("progress", 0) or 0)
    amount_left = int(task.get("amount_left", 0) or 0)
    if progress < 1 or amount_left > 0:
        raise RuntimeError("qB 未将跳过校验的完整文件识别为完成；保持暂停，未加标签、未启动")
    return task


def _run(args: argparse.Namespace) -> int:
    source, source_before = _source_identity(args.source_path, args.expected_size)
    profile_dir = args.profile_dir.expanduser()
    print("[1/6] 已验证 TL 源文件位于允许目录，名字、大小与 inode 将全程复核。", flush=True)
    official_url, user_agent = _copy_official_link(
        args.mteam_torrent_id, profile_dir, args.expected_page_title
    )
    print("[2/6] 已从 M-Team 详情页复制按钮捕获官方链接（签名未输出、未保存）。", flush=True)
    content = _download_torrent_bytes(official_url, args.mteam_torrent_id, user_agent)
    official_url = ""
    file_name, torrent_size, info_hash = _torrent_manifest(content)
    if file_name != source.name or torrent_size != args.expected_size:
        raise RuntimeError(
            "官方种子单文件名或精确字节数与 TL 源不一致；qB 未添加任务"
        )
    print(f"[3/6] 官方种子核验通过：{file_name}，{torrent_size:,} bytes。", flush=True)

    relative = source.relative_to(DEFAULT_DOWNLOADS_ROOT.resolve(strict=True))
    qb_root = Path(args.qb_downloads_root)
    qb_save_path = qb_root / relative.parent
    if not qb_save_path.is_absolute():
        raise RuntimeError("qB 下载根目录必须是绝对路径")

    api_key = _qb_config_key(args.qb_config)
    session = _qb_api(args.qb_url.rstrip("/"), api_key)
    del api_key
    base_url = args.qb_url.rstrip("/")
    try:
        if _qb_task(session, base_url, info_hash):
            raise RuntimeError("qB 已存在该官方 info hash；为保护现有任务，没有修改它")
        print("[4/6] qB 直连接口认证通过，未发现同 hash 任务。", flush=True)
        response = session.post(
            f"{base_url}/api/v2/torrents/add",
            data={
                "savepath": qb_save_path.as_posix(),
                "paused": "true",
                "skip_checking": "true",
                "autoTMM": "false",
                "root_folder": "false",
            },
            files={"torrents": ("official-mteam.torrent", content, "application/x-bittorrent")},
            timeout=30,
        )
        if response.status_code != 200:
            raise RuntimeError(f"qB 拒绝添加官方种子（HTTP {response.status_code}）")
        task = _assert_task_matches(
            session, base_url, info_hash, source, qb_save_path, args.expected_size
        )
        source_after = source.stat()
        if any(
            getattr(source_after, key) != getattr(source_before, key)
            for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        ):
            raise RuntimeError("TL 源文件在验证期间发生变化；新 qB 任务仍保持暂停")
        print("[5/6] qB 保存路径、文件清单、字节数均精确匹配；TL 源 inode 未变化。", flush=True)

        tags_response = session.get(f"{base_url}/api/v2/torrents/tags", timeout=10)
        if tags_response.status_code != 200:
            raise RuntimeError("无法读取 qB 标签；任务保持暂停")
        if "Mteam" not in tags_response.json():
            create = session.post(
                f"{base_url}/api/v2/torrents/createTags", data={"tags": "Mteam"}, timeout=10
            )
            if create.status_code != 200:
                raise RuntimeError("创建 qB Mteam 标签失败；任务保持暂停")
        add_tag = session.post(
            f"{base_url}/api/v2/torrents/addTags",
            data={"hashes": info_hash, "tags": "Mteam"},
            timeout=10,
        )
        if add_tag.status_code != 200:
            raise RuntimeError("qB 设置 Mteam 标签失败；任务保持暂停")
        task = _qb_task(session, base_url, info_hash)
        task_tags = {item.strip() for item in str((task or {}).get("tags") or "").split(",")}
        if "Mteam" not in task_tags:
            raise RuntimeError("qB 尚未确认 Mteam 标签；任务保持暂停")

        resume = session.post(
            f"{base_url}/api/v2/torrents/resume", data={"hashes": info_hash}, timeout=10
        )
        if resume.status_code != 200:
            raise RuntimeError("qB 启动做种失败；任务已加 Mteam 标签并保留")
        task = None
        for attempt in range(8):
            task = _qb_task(session, base_url, info_hash)
            if task and task.get("state") not in {"pausedUP", "pausedDL", "stoppedUP", "stoppedDL"}:
                break
            if attempt < 7:
                time.sleep(1)
        if not task or task.get("state") in {"pausedUP", "pausedDL", "stoppedUP", "stoppedDL"}:
            raise RuntimeError("已添加 Mteam 标签，但 qB 暂未确认已启动；任务保留待检查")
        print("[6/6] 已加 Mteam 标签并启动。", flush=True)
        print(f"M-Team ID：{args.mteam_torrent_id}")
        print(f"qB hash：{info_hash}")
        print(f"qB 保存路径：{qb_save_path}")
        print(f"文件：{file_name} ({torrent_size:,} bytes)")
        print(f"qB 状态：{task.get('state')}")
        print("hash 校验：按要求跳过；未声称校验了文件内容。")
        print("TL 源：路径、inode、大小、修改时间均未改变。")
        return 0
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="media-title-rename seed-official",
        description=(
            "从 M-Team 详情页复制按钮获取官方种子，在内存核验后直接经 qBittorrent WebUI API 添加；"
            "不调用 MoviePilot API、不保存签名链接。"
        ),
    )
    parser.add_argument("mteam_torrent_id", help="M-Team 详情页数字 ID")
    parser.add_argument("source_path", type=Path, help="宿主机 TL 中的单个源文件路径")
    parser.add_argument("--expected-size", type=int, required=True, help="源文件精确字节数")
    parser.add_argument("--expected-page-title", default="Threads 1984", help="用于确认详情页身份的标题片段")
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=Path(os.environ.get("MTEAM_PROFILE_DIR", "~/.config/mteam-post/chrome-profile")).expanduser(),
        help="已登录 M-Team 的 Chrome 配置目录",
    )
    parser.add_argument(
        "--qb-url",
        default=os.environ.get("QBITTORRENT_URL", DEFAULT_QB_URL),
        help="qB WebUI API 根地址",
    )
    parser.add_argument(
        "--qb-config",
        type=Path,
        default=Path(os.environ.get("QBITTORRENT_CONFIG", str(DEFAULT_QB_CONFIG))),
        help="只在内存读取 WebUI API key 的 qBittorrent.conf 路径",
    )
    parser.add_argument(
        "--qb-downloads-root",
        type=Path,
        default=Path(os.environ.get("QBITTORRENT_DOWNLOADS_ROOT", "/downloads")),
        help="qB 容器内共享下载目录根路径",
    )
    args = parser.parse_args(argv)
    if args.expected_size <= 0:
        parser.error("--expected-size 必须是正整数")
    try:
        return _run(args)
    except Exception as exc:
        # Exception strings from HTTP clients may contain signed URLs. Only print
        # sanitized RuntimeError messages from our explicit validation branches.
        if isinstance(exc, RuntimeError):
            message = str(exc)
        else:
            message = f"{type(exc).__name__}（详细异常已抑制，防止泄露签名链接）"
        print(f"[失败] {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
