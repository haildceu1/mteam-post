"""Fill the M-Team publishing form from a local prepare package.

This module deliberately stops before the final publish action.  It supports
both a normal Netscape cookie export and the request-header dump produced by
the current M-Team web application.  The latter restores the app's
``localStorage`` authentication values instead of treating a bearer token as a
cookie.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class MTeamSession:
    authorization: str = ""
    did: str = ""
    visitor_id: str = ""
    version: str = ""
    web_version: str = ""
    timestamp: str = ""
    cookies: tuple[dict[str, object], ...] = ()

    @property
    def is_auth_dump(self) -> bool:
        return bool(self.authorization)


_HEADER_KEYS = {
    "authorization": "authorization",
    "did": "did",
    "visitorid": "visitor_id",
    "visitor_id": "visitor_id",
    "version": "version",
    "webversion": "web_version",
    "timestamp": "timestamp",
    "ts": "timestamp",
    ":authority": "authority",
}


def _normalise_header_key(value: str) -> str:
    return re.sub(r"[^a-z0-9_:]", "", value.casefold())


def _parse_header_dump(text: str) -> MTeamSession:
    """Parse the two-line-per-field dump copied from browser DevTools."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    values: dict[str, str] = {}
    for index, line in enumerate(lines):
        key = _HEADER_KEYS.get(_normalise_header_key(line))
        if not key:
            continue
        for candidate in lines[index + 1 :]:
            # The dump has a value on the following line. Stop at the first
            # non-empty value; ordinary header values are never blank here.
            if candidate:
                values.setdefault(key, candidate)
                break
    authorization = values.get("authorization", "")
    if not authorization or not authorization.lower().startswith(("ey", "bearer ")):
        return MTeamSession()
    if authorization.lower().startswith("bearer "):
        authorization = authorization[7:].strip()
    return MTeamSession(
        authorization=authorization,
        did=values.get("did", ""),
        visitor_id=values.get("visitor_id", ""),
        version=values.get("version", ""),
        web_version=values.get("web_version", ""),
        timestamp=values.get("timestamp", ""),
    )


def _parse_netscape(text: str) -> MTeamSession:
    cookies: list[dict[str, object]] = []
    for raw_line in text.splitlines():
        if not raw_line or raw_line.startswith("#") and not raw_line.startswith("#HttpOnly_"):
            continue
        parts = raw_line.split("\t")
        if len(parts) != 7:
            continue
        domain, include_subdomains, path, secure, expiry, name, value = parts
        http_only = domain.startswith("#HttpOnly_")
        if http_only:
            domain = domain[len("#HttpOnly_") :]
        bare_domain = domain.lstrip(".").casefold()
        if not re.search(r"(?:^|\.)m-team\.(?:cc|io|co|net)$", bare_domain):
            continue
        item: dict[str, object] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path or "/",
            "secure": secure.upper() == "TRUE",
        }
        if http_only:
            item["httpOnly"] = True
        try:
            expiry_value = int(expiry)
        except ValueError:
            expiry_value = 0
        if expiry_value > int(time.time()):
            item["expiry"] = expiry_value
        cookies.append(item)
    return MTeamSession(cookies=tuple(cookies))


def load_mteam_session(path: Path) -> MTeamSession:
    text = path.read_text(encoding="utf-8", errors="replace")
    session = _parse_header_dump(text)
    if session.is_auth_dump:
        return session
    session = _parse_netscape(text)
    if not session.cookies:
        raise ValueError(
            "会话文件既不是可用的 M-Team 请求头，也没有包含 m-team.cc 的 Netscape Cookie；"
            "请从当前登录的 M-Team 域名重新导出，或使用 CookieCloud 专用 Chrome 配置。"
        )
    return session


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("M-Team URL 必须是完整的 http(s) 地址")
    return f"{parsed.scheme}://{parsed.netloc}/"


def _set_local_storage(driver, session: MTeamSession) -> None:
    driver.execute_script(
        """
        window.localStorage.setItem('auth', arguments[0]);
        if (arguments[1]) window.localStorage.setItem('did', arguments[1]);
        if (arguments[2]) window.localStorage.setItem('visitorId', arguments[2]);
        if (arguments[3]) window.localStorage.setItem('version', arguments[3]);
        if (arguments[4]) window.localStorage.setItem('webVersion', arguments[4]);
        """,
        session.authorization,
        session.did,
        session.visitor_id,
        session.version,
        session.web_version,
    )


def _has_mteam_auth(driver) -> bool:
    try:
        return bool(driver.execute_script("return window.localStorage.getItem('auth')"))
    except Exception:
        return False


def _wait_for_mteam_auth(driver, timeout_seconds: int) -> bool:
    """Wait for the SPA to persist its token after an interactive login."""
    if _has_mteam_auth(driver):
        return True
    if timeout_seconds <= 0:
        return False
    print(
        "请在已打开的 ChromeDriver 窗口中登录 M-Team；程序会自动检测登录状态，"
        f"最长等待 {timeout_seconds} 秒，无需回到终端按回车。"
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        if _has_mteam_auth(driver):
            print("已检测到 M-Team 登录状态，继续执行。")
            return True
    return False


def _is_login_url(url: str) -> bool:
    return urlsplit(url).path.rstrip("/").casefold().endswith("/login")


def _wait_for_publish_page(driver, target_url: str, timeout_seconds: int) -> bool:
    """Wait for an expired stored token to be replaced by a real login."""
    deadline = time.monotonic() + max(timeout_seconds, 0)
    announced = False
    while True:
        if not _is_login_url(driver.current_url):
            if driver.current_url.rstrip("/") != target_url.rstrip("/"):
                driver.get(target_url)
                time.sleep(2)
            if not _is_login_url(driver.current_url):
                return True
        if time.monotonic() >= deadline:
            return False
        if not announced:
            print(
                "保存的 M-Team 登录状态已过期，请在已打开的 ChromeDriver 窗口中重新登录；"
                f"程序最长等待 {timeout_seconds} 秒，登录成功后会自动打开发布页。"
            )
            announced = True
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def _set_react_value(driver, element, value: str) -> None:
    driver.execute_script(
        """
        const element = arguments[0];
        const value = arguments[1];
        const prototype = element instanceof HTMLTextAreaElement
          ? HTMLTextAreaElement.prototype
          : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(prototype, 'value').set;
        setter.call(element, value);
        element.dispatchEvent(new Event('input', {bubbles: true}));
        element.dispatchEvent(new Event('change', {bubbles: true}));
        element.dispatchEvent(new Event('blur', {bubbles: true}));
        """,
        element,
        value,
    )


def _find_field(driver, hints: tuple[str, ...]):
    """Find an input/textarea by Chinese label, placeholder or aria-label."""
    return driver.execute_script(
        """
        const hints = arguments[0].map(x => x.toLowerCase());
        const aliases = {
          '标题': 'name', 'title': 'name',
          '副标题': 'smallDescr', 'subtitle': 'smallDescr',
          '豆瓣链接': 'douban', 'douban': 'douban', 'douban url': 'douban',
          'mediainfo': 'mediainfo', 'media info': 'mediainfo'
        };
        for (const hint of hints) {
          const id = aliases[hint] || hint.replace(/^#/, '');
          const direct = document.getElementById(id);
          if (direct && (direct.matches('input:not([type=file]), textarea, [contenteditable="true"]'))) {
            return direct;
          }
        }
        const controls = [...document.querySelectorAll('input:not([type=file]), textarea, [contenteditable="true"]')];
        const haystack = el => [
          el.getAttribute('placeholder') || '', el.getAttribute('aria-label') || '',
          el.getAttribute('name') || '', el.getAttribute('id') || ''
        ].join(' ').toLowerCase();
        for (const el of controls) {
          const text = haystack(el);
          if (hints.some(h => text.includes(h))) return el;
        }
        for (const label of [...document.querySelectorAll('label, .ant-form-item, .form-item, .form-group, td, th, div')]) {
          const text = (label.innerText || '').trim().toLowerCase();
          if (!text || text.length > 80 || !hints.some(h => text.includes(h))) continue;
          const container = label.matches('label, .ant-form-item, .form-item, .form-group')
            ? label.closest('.ant-form-item, .form-item, .form-group') || label
            : label.parentElement;
          const control = container && container.querySelector('input:not([type=file]), textarea, [contenteditable="true"]');
          if (control) return control;
        }
        return null;
        """,
        list(hints),
    )


def _fill_field(driver, hints: tuple[str, ...], value: str, label: str) -> bool:
    element = _find_field(driver, hints)
    if element is None:
        print(f"警告：找不到{label}输入框，请手工填写。")
        return False
    if element.get_attribute("contenteditable") == "true":
        driver.execute_script(
            "arguments[0].innerHTML = ''; arguments[0].textContent = arguments[1]; arguments[0].dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:arguments[1]}));",
            element,
            value,
        )
    else:
        _set_react_value(driver, element, value)
    return True


def _click_get_intro(driver) -> bool:
    buttons = driver.execute_script(
        """
        const douban = document.querySelector('#douban');
        const nearby = douban && douban.parentElement
          ? douban.parentElement.querySelector('button') : null;
        if (nearby) return [nearby];
        return [...document.querySelectorAll('button, [role="button"]')]
          .filter(el => /获取简介|取得简介|get introduction/i.test((el.innerText || el.getAttribute('aria-label') || '').trim()));
        """
    )
    if not buttons:
        print("提示：找不到“获取简介”按钮，请手工点击。")
        return False
    buttons[0].click()
    return True


def _wait_for_intro(driver, timeout: float = 20.0) -> None:
    """Wait for the Douban fetch to populate the Lexical editor."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = driver.execute_script(
            """
            const editor = document.querySelector('[contenteditable="true"][data-lexical-editor]');
            if (!editor) return {text: '', children: 0};
            return {text: (editor.innerText || '').trim(), children: editor.children.length};
            """
        )
        if state and (len(state.get("text", "")) > 0 or state.get("children", 0) > 2):
            return
        time.sleep(0.25)


def _move_editor_caret_to_end(driver) -> bool:
    """Place the Lexical editor selection after its final paragraph/node."""
    return bool(
        driver.execute_script(
            """
            const editor = document.querySelector('[contenteditable="true"][data-lexical-editor]');
            if (!editor) return false;
            editor.focus();
            const selection = window.getSelection();
            const range = document.createRange();
            range.selectNodeContents(editor);
            range.collapse(false);
            selection.removeAllRanges();
            selection.addRange(range);
            return true;
            """
        )
    )


def _select_category(driver, category: str) -> bool:
    select = driver.execute_script(
        """
        const wanted = arguments[0].toLowerCase();
        const direct = document.querySelector('#category');
        if (direct) return direct;
        const nodes = [...document.querySelectorAll('.ant-select, [role="combobox"], select')];
        for (const node of nodes) {
          const box = node.closest('.ant-form-item, .form-item, .form-group, td, tr, div');
          const text = (box?.innerText || '').toLowerCase();
          if (/类别|分类|category/.test(text) || (node.getAttribute('aria-label') || '').toLowerCase().includes('category')) return node;
        }
        return nodes[0] || null;
        """,
        category,
    )
    if select is None:
        print("警告：找不到分类控件，请手工选择。")
        return False
    select.click()
    option = None
    for _ in range(20):
        option = driver.execute_script(
            """
            const canon = value => value.trim().toLowerCase().replaceAll(/\\s+/g, '')
              .replaceAll('／', '/').replaceAll('劇', '剧').replaceAll('綜', '综')
              .replaceAll('藝', '艺').replaceAll('電', '电').replaceAll('視', '视')
              .replaceAll('畫', '画').replaceAll('動', '动');
            const wanted = canon(arguments[0]);
            return [...document.querySelectorAll('[role="option"], .ant-select-item-option, li, .ant-cascader-menu-item')]
              .filter(el => el.offsetParent !== null)
              .find(el => {
                const text = canon(el.textContent || '');
                return text === wanted || text.includes(wanted) || wanted.includes(text);
              }) || null;
            """,
            category,
        )
        if option is not None:
            break
        time.sleep(0.25)
    if option is None:
        print(f"警告：分类列表中找不到“{category}”，请手工选择。")
        return False
    # Ant Design renders a clickable child inside the option wrapper; a
    # synthetic DOM click avoids Selenium's coordinate interception when the
    # dropdown is near the viewport edge.
    driver.execute_script("arguments[0].click();", option)
    return True


def _file_inputs(driver):
    return [element for element in driver.find_elements("css selector", "input[type=file]") if element.is_enabled() or element.get_attribute("style") is not None]


def _wait_for_image_uploads(driver, expected_count: int, timeout: float = 120.0) -> bool:
    """Wait until Ant Design has uploaded every selected local image."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = driver.execute_script(
            """
            const dialog = [...document.querySelectorAll('[role="dialog"]')]
              .find(el => el.offsetParent !== null);
            if (!dialog) return {count: 0, uploading: 0, failed: 0};
            const items = [...dialog.querySelectorAll('.ant-upload-list-item')];
            return {
              count: items.length,
              uploading: items.filter(el => el.classList.contains('ant-upload-list-item-uploading')).length,
              failed: items.filter(el => el.classList.contains('ant-upload-list-item-error')).length,
            };
            """
        ) or {}
        if int(state.get("failed", 0)):
            return False
        if int(state.get("count", 0)) >= expected_count and not int(state.get("uploading", 0)):
            return True
        time.sleep(0.5)
    return False


def _wait_for_editor_images(driver, expected_count: int, timeout: float = 30.0) -> bool:
    """Wait until uploaded images have been inserted into the Lexical editor."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        count = driver.execute_script(
            """
            const editor = document.querySelector('[contenteditable="true"][data-lexical-editor]');
            return editor ? editor.querySelectorAll('img').length : 0;
            """
        )
        if int(count or 0) >= expected_count:
            return True
        time.sleep(0.5)
    return False


def _load_selenium():
    try:
        from selenium import webdriver
    except ImportError as exc:
        raise RuntimeError("缺少 Selenium，请执行：python -m pip install selenium") from exc
    return webdriver


def _fill_page(driver, package: dict[str, object], *, upload: bool) -> None:
    _fill_field(driver, ("标题", "title"), str(package.get("title") or package.get("release_name") or ""), "标题")
    _fill_field(driver, ("副标题", "subtitle"), str(package.get("subtitle") or ""), "副标题")
    _fill_field(driver, ("imdb", "imdb url", "IMDb链接"), str(package.get("imdb_url") or ""), "IMDb链接")
    _fill_field(driver, ("豆瓣链接", "douban", "douban url"), str(package.get("douban_url") or ""), "豆瓣链接")
    technical_type = str(package.get("technical_info_type") or "MediaInfo")
    technical_text = str(package.get("technical_info_text") or package.get("mediainfo_text") or "")
    _fill_field(driver, ("mediainfo", "media info", "bdinfo", "bd info"), technical_text, technical_type)
    _select_category(driver, str(package.get("category") or ""))
    if package.get("douban_url"):
        time.sleep(0.5)
        if _click_get_intro(driver):
            _wait_for_intro(driver)

    if not upload:
        print("已完成字段预填；按 --upload 才会上传种子和截图。")
        return
    torrent = str((package.get("torrent") or {}).get("path") or "")
    screenshots = [str(item) for item in (package.get("screenshots") or [])]
    if len(screenshots) > 4:
        print(f"提示：资料包包含 {len(screenshots)} 张截图，按 M-Team 发布要求只上传前 4 张。")
        screenshots = screenshots[:4]
    if not torrent or not Path(torrent).is_file():
        print("警告：找不到资料包中的种子文件，请手工选择。")
    inputs = _file_inputs(driver)
    torrent_inputs = [item for item in inputs if item.get_attribute("id") == "torrent-input"]
    if torrent and Path(torrent).is_file() and torrent_inputs:
        torrent_inputs[0].send_keys(str(Path(torrent).resolve()))
    elif torrent and Path(torrent).is_file() and inputs:
        inputs[0].send_keys(str(Path(torrent).resolve()))
    if screenshots:
        # M-Team opens the editor's image-upload dialog lazily; clicking the
        # image toolbar button creates a hidden multi-file input.
        if not _move_editor_caret_to_end(driver):
            print("提示：找不到简介编辑器，请手工上传截图。")
            return
        opened = driver.execute_script(
            """
            const button = document.querySelector('button[aria-label="插入圖片"], button[aria-label="插入图片"]');
            if (!button) return false;
            button.click();
            return true;
            """
        )
        if not opened:
            print("提示：找不到简介编辑器的图片按钮，请手工上传截图。")
            return
        time.sleep(0.5)
        image_inputs = driver.find_elements("css selector", 'input[type=file][accept*="image"]')
        if not image_inputs:
            print("提示：图片上传控件未出现，请手工上传截图。")
            return
        valid = [str(Path(item).resolve()) for item in screenshots if Path(item).is_file()]
        if valid:
            existing_images = int(
                driver.execute_script(
                    """
                    const editor = document.querySelector('[contenteditable="true"][data-lexical-editor]');
                    return editor ? editor.querySelectorAll('img').length : 0;
                    """
                )
                or 0
            )
            image_inputs[-1].send_keys("\n".join(valid))
            if not _wait_for_image_uploads(driver, len(valid)):
                print("警告：本地截图未能全部上传，请检查图片上传窗口。")
                return
            confirmed = driver.execute_script(
                """
                const dialog = [...document.querySelectorAll('[role="dialog"], .ant-modal-wrap')]
                  .find(el => el.offsetParent !== null);
                const button = dialog && [...dialog.querySelectorAll('button')]
                  .find(el => /確\\s*認|确认/i.test((el.innerText || '').trim()));
                if (!button) return false;
                button.click();
                return true;
                """
            )
            if not confirmed or not _wait_for_editor_images(driver, existing_images + len(valid)):
                print("警告：截图已上传，但未能确认全部插入简介，请检查简介编辑器。")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="使用 ChromeDriver 将 M-Team 发布资料包填入已登录的发布页")
    parser.add_argument("package", type=Path, nargs="?", help="prepare 生成的 mteam-prepare.json；--login-only 时可省略")
    parser.add_argument("--cookie-file", "--session-file", dest="session_file", type=Path, help="M-Team Cookie 导出或请求头复制文件")
    parser.add_argument("--url", default="https://kp.m-team.cc/upload", help="M-Team 发布页地址；默认使用 kp.m-team.cc/upload")
    parser.add_argument("--profile-dir", type=Path, help="专用 Chrome 配置目录；可用于复用 CookieCloud 登录态")
    parser.add_argument("--upload", action="store_true", help="在填表后上传种子和截图；仍不会点击最终发布")
    parser.add_argument("--yes", action="store_true", help="跳过上传前确认；仅建议在你已检查资料包后使用")
    parser.add_argument("--keep-open", action="store_true", help="填表后等待回车再关闭浏览器")
    parser.add_argument("--login-timeout", type=int, default=600, help="等待手工登录的秒数；默认 600")
    parser.add_argument("--login-only", action="store_true", help="只打开专用 Chrome 配置供你手工登录，不读取资料包、不填表")
    parser.add_argument("--inspect-only", action="store_true", help="只读取发布页控件，不填写、不上传")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.login_only or args.inspect_only:
            if not args.profile_dir:
                raise ValueError("--login-only/--inspect-only 必须指定专用 Chrome 配置目录 --profile-dir")
        elif not args.package or not args.package.is_file():
            raise FileNotFoundError(f"找不到资料包：{args.package or '<未指定>'}")
        if not args.login_only and not args.session_file and not args.profile_dir:
            raise ValueError("请提供 --cookie-file，或提供已登录的 CookieCloud Chrome 配置目录 --profile-dir")
        if args.session_file and not args.session_file.is_file():
            raise FileNotFoundError(f"找不到会话文件：{args.session_file}")
        package = json.loads(args.package.read_text(encoding="utf-8")) if args.package else {}
        session = load_mteam_session(args.session_file) if args.session_file else MTeamSession()
        webdriver = _load_selenium()
        options = webdriver.ChromeOptions()
        if args.profile_dir:
            options.add_argument(f"--user-data-dir={args.profile_dir.resolve()}")
        driver = webdriver.Chrome(options=options)
        try:
            origin = _origin(args.url)
            driver.get(origin)
            if args.login_only:
                auth_present = _wait_for_mteam_auth(driver, args.login_timeout)
                if not auth_present:
                    raise ValueError(
                        "等待登录超时，仍未检测到 M-Team auth；请确认浏览器页面能正常联网并已完成登录"
                    )
                print("登录态检查：localStorage auth 已保存。")
                return
            if args.inspect_only:
                # When a request-header dump is supplied, restore it before
                # inspecting the page so a fresh profile can be inspected
                # without performing any form action.
                if session.is_auth_dump:
                    _set_local_storage(driver, session)
                    driver.refresh()
                elif session.cookies:
                    for cookie in session.cookies:
                        try:
                            driver.add_cookie(cookie)
                        except Exception:
                            continue
                    driver.refresh()
                time.sleep(2)
                if args.profile_dir and not _has_mteam_auth(driver):
                    _wait_for_mteam_auth(driver, args.login_timeout)
                if driver.current_url.rstrip("/") != args.url.rstrip("/"):
                    driver.get(args.url)
                    time.sleep(2)
                controls = driver.execute_script(
                    """
                    return [...document.querySelectorAll('input, textarea, select, button, [role="combobox"]')].map((el, index) => ({
                      index,
                      tag: el.tagName.toLowerCase(),
                      type: el.getAttribute('type') || '',
                      name: el.getAttribute('name') || '',
                      placeholder: el.getAttribute('placeholder') || '',
                      aria: el.getAttribute('aria-label') || '',
                      text: (el.innerText || '').trim().slice(0, 80),
                    }));
                    """
                )
                print(f"M-Team 页面标题：{driver.title}")
                print(f"M-Team 页面地址：{driver.current_url}")
                auth_present = _has_mteam_auth(driver)
                print(f"localStorage auth={'已保存' if auth_present else '未发现'}")
                print("页面控件（仅属性，不读取输入值）：")
                for item in controls:
                    print(json.dumps(item, ensure_ascii=False))
                return
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
            # Let the SPA finish its bootstrap.  A profile may have no
            # durable localStorage token (or an old token may be rejected),
            # so fall back to an interactive login in this same driver.
            time.sleep(2)
            auth_present = _has_mteam_auth(driver)
            if args.profile_dir and not auth_present:
                auth_present = _wait_for_mteam_auth(driver, args.login_timeout)
            print(f"M-Team 页面已打开；localStorage auth={'已恢复' if auth_present else '未发现'}。")
            if not auth_present:
                raise ValueError("M-Team 登录态不可用；请重新登录，或提供当前有效的 --cookie-file。")
            # The origin visit above is only used to install auth/cookies.
            # Navigate to the requested publishing route after authentication
            # so field discovery runs against the actual upload form.
            if driver.current_url.rstrip("/") != args.url.rstrip("/"):
                driver.get(args.url)
                time.sleep(2)
            if not _wait_for_publish_page(driver, args.url, args.login_timeout):
                raise ValueError("M-Team 登录超时；请重新运行命令并在 ChromeDriver 窗口中完成登录。")
            if not args.yes:
                technical_type = str(package.get("technical_info_type") or "MediaInfo")
                action = f"标题、副标题、豆瓣链接和 {technical_type}"
                if args.upload:
                    action += "，以及种子和本地截图文件"
                answer = input(
                    f"即将把{action}写入 M-Team 发布页"
                    + ("并上传文件" if args.upload else "")
                    + "，但不会点击最终发布。继续？[y/N] "
                ).strip().casefold()
                if answer not in {"y", "yes"}:
                    print("已取消填表。")
                    return
            _fill_page(driver, package, upload=args.upload)
            print("已停止在最终发布之前；请检查页面内容。")
            if args.keep_open or sys.stdin.isatty():
                input("检查完成后按回车关闭 ChromeDriver 窗口。")
        finally:
            driver.quit()
    except (FileNotFoundError, ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
