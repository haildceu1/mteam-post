"""Fill the M-Team publishing form from a local prepare package.

This module stops before the final publish action unless --submit is supplied. It supports
both a normal Netscape cookie export and the request-header dump produced by
the current M-Team web application.  The latter restores the app's
``localStorage`` authentication values instead of treating a bearer token as a
cookie.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .sports import is_sports_resource


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


def _parse_cookiecloud_json(text: str) -> MTeamSession:
    """Load MoviePilot CookieCloud's in-memory domain-to-cookie mapping."""
    try:
        domains = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return MTeamSession()
    if not isinstance(domains, dict):
        return MTeamSession()

    cookies: list[dict[str, object]] = []
    for raw_domain, header in domains.items():
        if not isinstance(raw_domain, str) or not isinstance(header, str):
            continue
        domain = raw_domain.strip().lstrip(".").casefold()
        if not re.search(r"(?:^|\.)m-team\.(?:cc|io|co|net)$", domain):
            continue
        for part in header.split(";"):
            name, separator, value = part.strip().partition("=")
            if not separator or not name:
                continue
            cookies.append({
                "name": name,
                "value": value,
                "domain": f".{domain}",
                "path": "/",
                "secure": True,
            })
    return MTeamSession(cookies=tuple(cookies))


def load_mteam_session_text(text: str) -> MTeamSession:
    """Parse a request-header dump, Netscape export, or CookieCloud JSON."""
    session = _parse_header_dump(text)
    if session.is_auth_dump:
        return session
    session = _parse_netscape(text)
    if session.cookies:
        return session
    session = _parse_cookiecloud_json(text)
    if session.cookies:
        return session
    raise ValueError(
        "会话输入不含可用的 M-Team 请求头、Cookie 或 CookieCloud 域名数据"
    )


def load_mteam_session(path: Path) -> MTeamSession:
    text = path.read_text(encoding="utf-8", errors="replace")
    return load_mteam_session_text(text)


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


def _read_mteam_credentials(path: Path) -> tuple[str, str]:
    """Read the two-line username/password file without exposing either value."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except Exception as exc:
        raise ValueError("无法读取 M-Team 账密文件") from exc
    if os.name != "nt" and mode & 0o077:
        raise ValueError("账密文件权限过宽，请先设置为 chmod 600")
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except Exception as exc:
        raise ValueError("无法读取 M-Team 账密文件") from exc
    if len(lines) != 2 or not lines[0].strip() or not lines[1].strip():
        raise ValueError("M-Team 账密文件必须恰好包含两行非空内容")
    return lines[0], lines[1]


def _login_with_credentials(driver, credentials_file: Path, target_url: str, timeout_seconds: int = 90) -> bool:
    """Sign in through the current M-Team username/password form, without logging inputs."""
    username, password = _read_mteam_credentials(credentials_file)
    driver.get(_origin(target_url) + "login")
    deadline = time.monotonic() + min(max(timeout_seconds, 10), 90)
    username_fields = []
    password_fields = []
    while time.monotonic() < deadline:
        username_fields = driver.find_elements("css selector", "#username")
        password_fields = driver.find_elements("css selector", "#password")
        if len(username_fields) == 1 and len(password_fields) == 1:
            break
        time.sleep(0.5)
    if len(username_fields) != 1 or len(password_fields) != 1:
        raise ValueError("M-Team 登录表单未出现预期的用户名和密码字段，未提交账密")

    # The login page proves any stored auth is rejected; remove only that stale
    # token so success detection waits for the newly issued value.
    driver.execute_script("window.localStorage.removeItem('auth')")
    username_fields[0].clear()
    username_fields[0].send_keys(username)
    password_fields[0].clear()
    password_fields[0].send_keys(password)
    submit_buttons = driver.find_elements("css selector", "button[type='submit']")
    if len(submit_buttons) != 1:
        raise ValueError("M-Team 登录页没有唯一的提交按钮，未提交账密")
    submit_buttons[0].click()

    deadline = time.monotonic() + min(max(timeout_seconds, 10), 90)
    while time.monotonic() < deadline:
        if _has_mteam_auth(driver):
            driver.get(target_url)
            time.sleep(2)
            return not _is_login_url(driver.current_url)
        time.sleep(0.5)
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


def _wait_for_form_controls(driver, target_url: str, timeout_seconds: int = 30) -> bool:
    """An SPA route is ready only when the actual upload form is mounted."""
    deadline = time.monotonic() + max(timeout_seconds, 0)
    retried = False
    while True:
        present = driver.execute_script("""
            return !!(document.getElementById('name')
              && document.getElementById('smallDescr')
              && document.getElementById('category')
              && document.getElementById('mediainfo'));
        """)
        if present:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if not retried and remaining <= timeout_seconds / 2:
            driver.get(target_url)
            retried = True
        time.sleep(min(1.0, remaining))


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


def _click_get_intro(driver, source: str = "douban") -> bool:
    buttons = driver.execute_script(
        """
        const field = document.getElementById(arguments[0]);
        const isIntro = el => /获取简介|獲取簡介|取得簡介|get introduction/i.test(
          (el.innerText || el.getAttribute('aria-label') || '').trim());
        const container = field && (field.closest('.ant-space-compact') || field.closest('.ant-form-item') || field.parentElement);
        const nearby = container && [...container.querySelectorAll('button, [role="button"]')].find(isIntro);
        return nearby ? [nearby] : [];
        """,
        source,
    )
    if not buttons:
        print("警告：找不到“获取简介”按钮；停止上传和发布。")
        return False
    deadline = time.monotonic() + 10
    while hasattr(buttons[0], "is_enabled") and not buttons[0].is_enabled():
        if time.monotonic() >= deadline:
            print("警告：链接验证后“获取简介”按钮仍不可用；停止上传和发布。")
            return False
        time.sleep(.25)
    buttons[0].click()
    return True


def _wait_for_intro(driver, timeout: float = 60.0, previous_text: str = "") -> bool:
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
        text = str((state or {}).get("text") or "")
        if len(text) >= 50 and text != previous_text:
            return True
        time.sleep(0.25)
    return False


def _fill_description(driver, description: str) -> bool:
    """Fill an explicitly sourced synopsis when the site's Douban fetch is empty."""
    value = str(description or "").strip()
    if len(value) < 50:
        return False
    try:
        editor = driver.find_element(
            "css selector", '[contenteditable="true"][data-lexical-editor]'
        )
    except Exception:
        return False
    editor.click()
    editor.send_keys("\ue009a")  # CTRL+A: update Lexical through real key events.
    editor.send_keys(value)
    return _wait_for_intro(driver, timeout=5.0)


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


def _append_editor_image_spacing(driver, count: int = 2) -> bool:
    """Move to the end of the introduction and press Enter before images."""
    if not _move_editor_caret_to_end(driver):
        return False
    try:
        editor = driver.find_element(
            "css selector", '[contenteditable="true"][data-lexical-editor]'
        )
        # Selenium's WebDriver Enter key. Sending the special key instead of
        # mutating innerHTML lets Lexical update its internal editor state.
        editor.send_keys("\ue007" * count)
    except Exception:
        return False
    return True


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
            const candidates = [...document.querySelectorAll(
              '[role="option"], .ant-select-item-option, li, .ant-cascader-menu-item'
            )].filter(el => {
                const visible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                return visible;
              });
            // Prefer an exact canonical label.  In particular, do not let
            // the shorter "动画" option satisfy a request for
            // "动画/Bluray" merely because it is a substring of the target;
            // the dropdown normally contains both entries in that order.
            const exact = candidates.find(el => canon(el.textContent || '') === wanted);
            const match = exact;
            if (match) return match;

            // Ant Design may virtualize the option list.  In that case an
            // off-screen option is not present in the DOM until the popup's
            // internal scroller is advanced.  Scroll every visible list
            // holder a page at a time and retry after the browser renders it.
            const holders = [...document.querySelectorAll(
              '.ant-select-dropdown .rc-virtual-list-holder, '
              + '.ant-select-dropdown .ant-select-virtual-list-holder, '
              + '.ant-select-dropdown .ant-select-dropdown-menu, '
              + '.ant-select-dropdown, [role="listbox"]'
            )].filter(el => el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            for (const holder of holders) {
              const before = holder.scrollTop;
              const step = Math.max(holder.clientHeight * 0.8, 120);
              holder.scrollTop = Math.min(holder.scrollHeight, before + step);
              if (holder.scrollTop !== before) return null;
            }
            // The page itself may be the scroll container when the dropdown
            // is rendered at the viewport edge rather than in a virtual list.
            window.scrollBy(0, Math.max(window.innerHeight * 0.7, 240));
            return null;
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


def _wait_for_image_uploads(driver, expected_count: int, timeout: float = 45.0) -> bool:
    """Wait until Ant Design has uploaded every selected local image."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = driver.execute_script(
            """
            const visible = [...document.querySelectorAll(
              '[role="dialog"], .ant-modal-wrap, .ant-modal, [data-state="open"]')]
              .filter(el => el.offsetParent !== null);
            const roots = visible.filter(el => el.querySelector('.ant-upload-list-item'));
            const dialog = roots.sort((a, b) =>
              b.querySelectorAll('.ant-upload-list-item').length - a.querySelectorAll('.ant-upload-list-item').length)[0]
              || visible[0];
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


def _remove_failed_or_stalled_image(driver, filename: str, timeout: float = 10.0) -> bool:
    """Cancel one visibly failed/stalled image and verify its row disappears."""
    clicked = driver.execute_script(
        """
        const visible = [...document.querySelectorAll(
          '[role="dialog"], .ant-modal-wrap, .ant-modal, [data-state="open"]')]
          .filter(e => e.offsetParent !== null);
        const root = visible.find(e => e.querySelector('.ant-upload-list-item')) || visible[0];
        const item = [...(root?.querySelectorAll('.ant-upload-list-item') || [])]
          .find(e => (e.innerText || '').includes(arguments[0]));
        if (!item || !(
          item.classList.contains('ant-upload-list-item-error') ||
          item.classList.contains('ant-upload-list-item-uploading'))) return false;
        const buttons = [...item.querySelectorAll('button, [role="button"]')];
        const remove = buttons.find(b => {
          const label = [b.getAttribute('aria-label'), b.getAttribute('title'), b.innerText]
            .filter(Boolean).join(' ');
          return /delete|remove|删除|刪除/i.test(label) ||
            !!b.querySelector('svg[data-icon="delete"], svg[data-icon*="trash"]');
        }) || (buttons.length === 1 ? buttons[0] : null);
        if (!remove) return false;
        remove.click();
        return true;
        """,
        filename,
    )
    if not clicked:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remains = driver.execute_script(
            """
            const visible = [...document.querySelectorAll(
              '[role="dialog"], .ant-modal-wrap, .ant-modal, [data-state="open"]')]
              .filter(e => e.offsetParent !== null);
            const root = visible.find(e => e.querySelector('.ant-upload-list-item')) || visible[0];
            return [...(root?.querySelectorAll('.ant-upload-list-item') || [])]
              .some(e => (e.innerText || '').includes(arguments[0]));
            """,
            filename,
        )
        if not remains:
            return True
        time.sleep(0.25)
    return False


def _wait_for_editor_images(driver, expected_count: int, timeout: float = 30.0) -> bool:
    """Wait until uploaded screenshot images render as real, non-placeholder pixels."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = driver.execute_script(
            """
            const editor = document.querySelector('[contenteditable="true"][data-lexical-editor]');
            if (!editor) return {count: 0, loaded_count: 0};
            const images = [...editor.querySelectorAll('img')];
            return {
              count: images.length,
              loaded_count: images.filter(img => img.complete &&
                img.naturalWidth >= 64 && img.naturalHeight >= 64 &&
                Boolean(img.currentSrc || img.src)).length,
            };
            """
        ) or {}
        if (int(state.get("count", 0)) >= expected_count
                and int(state.get("loaded_count", 0)) >= expected_count):
            return True
        time.sleep(0.5)
    return False


def _install_upload_diagnostics(driver) -> None:
    """Record only safe status metadata for image-upload XHR failures."""
    driver.execute_script(
        r"""
        window.__mteamUploadDiagnostics = [];
        const endpoint = value => {
          try { const u = new URL(value, location.href); return {host: u.host, path: u.pathname}; }
          catch (_) { return {host: '', path: ''}; }
        };
        const safe = value => String(value || '')
          .replace(/https?:\/\/[^\s"']+/ig, '[url]')
          .replace(/(cookie|passkey|token|authorization|api.?key|password)\s*[:=]\s*[^\s,;"']+/ig, '$1=[redacted]')
          .slice(0, 240);
        const originalOpen = XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open = function(method, url, ...rest) {
          const target = endpoint(url);
          if (/upload|image|picture|screenshot|media/i.test(target.path + target.host)) {
            const xhr = this;
            const event = {method: String(method), ...target, status: 0, progress: 0, result: ''};
            window.__mteamUploadDiagnostics.push(event);
            xhr.upload?.addEventListener('progress', e => {
              if (e.lengthComputable) event.progress = Math.round(e.loaded * 100 / e.total);
            });
            xhr.addEventListener('loadend', () => {
              event.status = xhr.status || 0;
              try {
                const body = JSON.parse(xhr.responseText || '{}');
                event.code = body.code ?? body.status ?? '';
                event.result = safe(body.message || body.msg || body.error || '');
              } catch (_) { event.result = safe(xhr.statusText || ''); }
            });
            xhr.addEventListener('error', () => { event.result = 'network_error'; });
            xhr.addEventListener('timeout', () => { event.result = 'request_timeout'; });
            xhr.addEventListener('abort', () => { event.result = 'request_aborted'; });
          }
          return originalOpen.call(this, method, url, ...rest);
        };
        """
    )


def _upload_screenshot(driver, filename: str, expected_count: int) -> bool:
    """Retry failed/stalled uploads only after their UI row is cancelled."""
    for attempt in range(3):
        inputs = driver.find_elements('css selector', 'input[type=file][accept*="image"]')
        if not inputs:
            return False
        inputs[-1].send_keys(filename)
        if _wait_for_image_uploads(driver, expected_count):
            return True
        try:
            state = driver.execute_script(
                """
                const visible = [...document.querySelectorAll(
                  '[role="dialog"], .ant-modal-wrap, .ant-modal, [data-state="open"]')]
                  .filter(el => el.offsetParent !== null);
                const root = visible.find(el => el.querySelector('.ant-upload-list-item')) || visible[0];
                const safeText = value => String(value || '').replace(
                  /https?:\\/\\/[^\\s]+|(?:cookie|passkey|token|authorization|api.?key|password)[^\\s]*/ig,
                  '[redacted]'
                ).slice(0, 240);
                const items = [...(root?.querySelectorAll('.ant-upload-list-item') || [])];
                return {
                  attempt: arguments[0],
                  expected_count: arguments[1],
                  items: items.map(item => ({
                    name: safeText(item.querySelector('.ant-upload-list-item-name')?.textContent),
                    status: item.className,
                    title: safeText(item.getAttribute('title')),
                    aria: safeText(item.getAttribute('aria-label')),
                    details: [...item.querySelectorAll('[title], [aria-label], [role="alert"]')]
                      .map(el => ({
                        title: safeText(el.getAttribute('title')),
                        aria: safeText(el.getAttribute('aria-label')),
                        text: safeText(el.textContent)
                      })).slice(0, 8),
                    progress: item.querySelector('[role="progressbar"]')?.getAttribute('aria-valuenow') || ''
                  })),
                  alerts: [...(root?.querySelectorAll('[role="alert"], .ant-upload-list-item-error .ant-upload-list-item-name') || [])]
                    .map(el => safeText(el.textContent)),
                  requests: (window.__mteamUploadDiagnostics || []).slice(-8)
                };
                """,
                attempt + 1,
                expected_count,
            ) or {}
            print('[截图上传诊断] ' + json.dumps(state, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(f'[截图上传诊断] 无法读取网页错误行：{type(exc).__name__}', flush=True)
        if attempt == 2:
            return False
        removed = _remove_failed_or_stalled_image(driver, Path(filename).name)
        if not removed:
            return False
        print(f'截图上传失败或卡住；已确认取消该文件后重试（{attempt + 1}/2）', flush=True)
        time.sleep(3 * (attempt + 1))
    return False


def _load_selenium():
    try:
        from selenium import webdriver
    except ImportError as exc:
        raise RuntimeError("缺少 Selenium，请执行：python -m pip install selenium") from exc
    return webdriver


def _configure_chrome_options(options):
    """Make ChromeDriver usable on Linux servers and under root."""
    pac_url = os.environ.get("MTEAM_CHROME_PAC_URL", "").strip()
    proxy = os.environ.get("MTEAM_CHROME_PROXY", "").strip()
    if pac_url:
        options.add_argument(f"--proxy-pac-url={pac_url}")
    elif proxy:
        options.add_argument(f"--proxy-server={proxy}")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        options.add_argument("--no-sandbox")
    # Small /dev/shm partitions can make Chrome exit before creating a session.
    options.add_argument("--disable-dev-shm-usage")
    # SSH/container sessions often have no X/Wayland display. Keep desktop
    # sessions visible, but use the modern headless backend when none exists.
    if os.name != "nt" and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
    return options


_MTEAM_AUDIO_CHOICES = {
    "DD": "AC3(DD)", "DDP": "E-AC3(DDP)",
    "DDP Atmos": "E-AC3 Atmos(DDP Atmos)", "DTS-HD MA": "DTS-HD MA",
    "DTS-HD HRA": "DTS-HD HRA", "DTS-X": "DTS:X",
    "TrueHD": "TrueHD", "TrueHD Atmos": "TrueHD Atmos",
    "LPCM": "LPCM/PCM", "FLAC": "FLAC", "AAC": "AAC", "DTS": "DTS",
    "MP3": "MP2/3", "MP2": "MP2/3", "Opus": "Other",
}

_MTEAM_RESOLUTION_CHOICES = {
    "4320p": "8K", "2160p": "4K", "4K": "4K",
    "1080p": "1080p", "1080i": "1080i", "720p": "720p",
    "576p": "SD", "576i": "SD", "480p": "SD", "480i": "SD", "SD": "SD",
}


def _fill_resolution_choice(driver, package: dict) -> bool:
    """Correct the site's width-based guess using the measured resolution.

    An anamorphic 1440x1080 source is still 1080p; M-Team can initially
    classify it as SD even after parsing the supplied MediaInfo.
    """
    label = _MTEAM_RESOLUTION_CHOICES.get(str((package.get('media') or {}).get('resolution') or ''))
    if not label:
        return False
    current = driver.execute_script("const n=document.getElementById('standard');return n?.closest('.ant-select')?.querySelector('.ant-select-content,.ant-select-selection-item')?.innerText || ''; ")
    if str(current or '').strip().casefold() == label.casefold():
        return True
    element = driver.find_element('css selector', '#standard')
    element.click()
    for _ in range(20):
        option = driver.execute_script(r"""
            const wanted=arguments[0].toLowerCase();
            const options=[...document.querySelectorAll('.ant-select-dropdown:not(.ant-select-dropdown-hidden) [role="option"], .ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option')];
            const match=options.find(e=>e.innerText.trim().toLowerCase()===wanted);
            if(match)return match;
            for(const h of document.querySelectorAll('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .rc-virtual-list-holder, .ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-virtual-list-holder'))h.scrollTop+=Math.max(120,h.clientHeight*.8);
            return null;
        """, label)
        if option is not None:
            driver.execute_script('arguments[0].click()', option)
            return True
        time.sleep(.2)
    element.send_keys('\ue00c')
    return False


def _fill_missing_audio_choice(driver, package: dict) -> bool:
    """Make the site choice agree with the measured primary audio track."""
    codec = str((package.get('media') or {}).get('audio_codec') or '').strip()
    label = _MTEAM_AUDIO_CHOICES.get(codec)
    if not label:
        return False
    current = driver.execute_script("const n=document.getElementById('audioCodec');return n?.closest('.ant-select')?.querySelector('.ant-select-content,.ant-select-selection-item')?.innerText || ''; ")
    canon = lambda value: re.sub(r'\s+', '', str(value or '')).casefold()
    if canon(current) == canon(label):
        return True
    if current and not re.search(r'请.*选择|請.*選擇', current):
        title = canon(package.get('title') or package.get('release_name') or '')
        # Do not override the site's own MediaInfo parse merely because a
        # package mapping differs. Correct a selection only when the prepared
        # title independently corroborates the measured codec (for example a
        # multi-track disc where the site picked LPCM but the measured/main
        # title track is explicitly TrueHD Atmos).
        if canon(codec) not in title and canon(label) not in title:
            return True
    element = driver.find_element('css selector', '#audioCodec')
    element.click()
    for _ in range(20):
        option = driver.execute_script(r"""
            const canon=s=>s.replace(/\s+/g,'').toLowerCase();
            const wanted=canon(arguments[0]);
            const options=[...document.querySelectorAll('.ant-select-dropdown:not(.ant-select-dropdown-hidden) [role="option"], .ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option')];
            const match=options.find(e=>canon(e.innerText)===wanted);
            if(match)return match;
            for(const h of document.querySelectorAll('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .rc-virtual-list-holder, .ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-virtual-list-holder'))h.scrollTop+=Math.max(120,h.clientHeight*.8);
            return null;
        """, label)
        if option is not None:
            driver.execute_script('arguments[0].click()', option)
            return True
        time.sleep(.2)
    element.send_keys('\ue00c')
    return False


def _fill_missing_video_choice(driver, package: dict) -> bool:
    """Resolve an empty M-Team codec choice from measured MediaInfo only.

    Keep the site's own MediaInfo parsing whenever it already selected a
    value.  This fallback is intentionally a closed alias map so filenames or
    release-group text cannot silently determine the publishing codec.
    """
    media = package.get('media') or {}
    codec = str(media.get('video_codec') or media.get('video_format') or '').strip()
    label = {
        'H.264': 'H.264(x264/AVC)', 'AVC': 'H.264(x264/AVC)', 'x264': 'H.264(x264/AVC)',
        'H.265': 'H.265(x265/HEVC)', 'HEVC': 'H.265(x265/HEVC)', 'x265': 'H.265(x265/HEVC)',
        'VC-1': 'VC-1', 'MPEG-2': 'MPEG-2', 'Xvid': 'Xvid',
        'MPEG-4 Visual': 'Xvid', 'AV1': 'AV1', 'VP8': 'VP8/9', 'VP9': 'VP8/9', 'AVS': 'AVS',
    }.get(codec)
    if not label:
        return False
    current = driver.execute_script(
        "const n=document.getElementById('videoCodec');return n?.closest('.ant-select')?.querySelector('.ant-select-content,.ant-select-selection-item')?.innerText || '';"
    )
    if current and not re.search(r'请.*选择|請.*選擇', current):
        return True
    element = driver.find_element('css selector', '#videoCodec')
    element.click()
    for _ in range(20):
        option = driver.execute_script(r"""
            const canon=s=>s.replace(/\s+/g,'').toLowerCase();
            const wanted=canon(arguments[0]);
            const options=[...document.querySelectorAll('.ant-select-dropdown:not(.ant-select-dropdown-hidden) [role="option"], .ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option')];
            const match=options.find(e=>canon(e.innerText)===wanted);
            if(match)return match;
            for(const h of document.querySelectorAll('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .rc-virtual-list-holder, .ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-virtual-list-holder'))h.scrollTop+=Math.max(120,h.clientHeight*.8);
            return null;
        """, label)
        if option is not None:
            driver.execute_script('arguments[0].click()', option)
            return True
        time.sleep(.2)
    element.send_keys('\ue00c')
    return False


def _requires_bangumi(package: dict) -> bool:
    # M-Team's animation category requires a Bangumi subject link regardless
    # of the work's original language.  Bangumi coverage may be sparse, but
    # missing coverage must be surfaced as a blocker rather than silently
    # exempting English-language animation.
    category = str(package.get('category') or '').replace('動', '动').replace('畫', '画')
    genres = (package.get('tmdb') or {}).get('genre_ids') or []
    return '动画' in category or 16 in genres


def _validated_bangumi_url(package: dict) -> str:
    value = str(package.get('bangumi_url') or '').strip()
    if value and not re.fullmatch(r'https://(?:bgm\.tv|bangumi\.tv|chii\.in)/subject/[1-9]\d*/?', value):
        raise ValueError('Bangumi 链接必须是有效的作品 subject 链接')
    if _requires_bangumi(package) and not value:
        raise ValueError('动画发布必须提供 bangumi_url，停止最终发布')
    return value


def _movie_filename_identity_error(package: dict) -> bool:
    """Catch a single-file movie torrent whose filename identifies no movie."""
    if str(package.get("kind") or "").casefold() != "movie":
        return False

    torrent = package.get("torrent") or {}
    torrent_path = Path(str(torrent.get("path") or ""))
    filename = str(package.get("filename") or "").strip()
    if torrent_path.is_file():
        try:
            from .prepare import _bdecode

            info = _bdecode(torrent_path.read_bytes()).get(b"info", {})
            if b"files" in info:
                return False
            raw_name = info.get(b"name.utf-8", info.get(b"name", b""))
            if isinstance(raw_name, bytes):
                filename = raw_name.decode("utf-8", errors="replace")
        except (OSError, ValueError, TypeError):
            pass
    if not filename:
        filename = Path(str(package.get("prepared_path") or "")).name
    if not filename:
        return False

    def words(value: str) -> list[str]:
        return re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", value.casefold())

    tmdb = package.get("tmdb") or {}
    title_words = {
        word
        for value in (
            tmdb.get("original_name"), tmdb.get("name"), tmdb.get("chinese_name"),
            package.get("title"), package.get("release_name"),
        )
        for word in words(str(value or ""))
        if word not in {"the", "a", "an"}
    }
    filename_words = words(Path(filename).stem)
    group_words = set(words(str(package.get("group") or "")))
    technical_words = {
        "480p", "576p", "720p", "1080p", "1440p", "2160p", "4320p",
        "bluray", "brrip", "bdrip", "webrip", "webdl", "dvdrip", "remux",
        "uhd", "hdr", "hdr10", "hdr10plus", "dovi", "dv", "atmos",
        "dd", "ddp", "dts", "truehd", "aac", "ac3", "flac", "opus",
        "x264", "x265", "h264", "h265", "avc", "hevc", "vc1", "10bit",
    }
    core_words = [
        word for word in filename_words
        if word not in group_words and word not in technical_words and not word.isdigit()
    ]
    if any(word in title_words for word in core_words):
        return False

    generic_words = {"video", "movie", "film", "file", "main", "output", "sample", "untitled", "unknown", "track"}
    # A title-less core made only of generic labels or one/two-letter initials
    # is not a useful single-movie filename (for example te.1080p-Group.mkv).
    return not core_words or all(word in generic_words or len(word) <= 2 for word in core_words)


def _validate_release_package_metadata(package: dict) -> None:
    """Block mismatched categories, codecs, and ambiguous single-movie filenames."""
    from .prepare import _package_has_bluray_disc_structure, _validate_bluray_category

    disc_structure = _package_has_bluray_disc_structure(package)
    failures = []
    try:
        _validate_bluray_category(
            str(package.get("category") or ""),
            disc_structure,
            str(package.get("source") or ""),
        )
    except ValueError as exc:
        failures.append(str(exc))

    title = str(package.get("title") or package.get("release_name") or "")
    from .autonomous import _normalize_title_video_codec
    expected_title = _normalize_title_video_codec(package)
    if expected_title != title:
        failures.append("标题视频编码与 REMUX/原盘/压制规则或实测 MediaInfo 不符")
    if _movie_filename_identity_error(package):
        failures.append("单文件电影种子文件名未能识别作品名；请在独立 staging 路径创建含片名的命名硬链接并重新制种")
    if failures:
        raise ValueError("资料包发布前校验失败：" + "；".join(failures) + "。请从源文件重新准备资料包。")


def _fill_page(driver, package: dict[str, object], *, upload: bool) -> None:
    # Older cached packages may still contain a numeric FPS spelling. Normalize
    # at the last boundary before any title is entered into the publish form.
    from .cli import _canonicalize_hfr_title

    title = _canonicalize_hfr_title(str(package.get("title") or package.get("release_name") or ""))
    _validate_release_package_metadata(package)
    if title:
        package["title"] = title
        if isinstance(package.get("media"), dict) and package["media"].get("hfr"):
            package["media"]["hfr"] = "HFR"
    bangumi_url = _validated_bangumi_url(package)
    _fill_field(driver, ("标题", "title"), title, "标题")
    _fill_field(driver, ("副标题", "subtitle"), str(package.get("subtitle") or ""), "副标题")
    _fill_field(driver, ("imdb", "imdb url", "IMDb链接"), str(package.get("imdb_url") or ""), "IMDb链接")
    _fill_field(driver, ("豆瓣链接", "douban", "douban url"), str(package.get("douban_url") or ""), "豆瓣链接")
    technical_type = str(package.get("technical_info_type") or "MediaInfo")
    technical_text = str(package.get("technical_info_text") or package.get("mediainfo_text") or "")
    _fill_field(driver, ("mediainfo", "media info", "bdinfo", "bd info"), technical_text, technical_type)
    _select_category(driver, str(package.get("category") or ""))
    if bangumi_url:
        _fill_field(driver, ("bangumi", "Bangumi 链接"), bangumi_url, "Bangumi 链接")
    sports_release = is_sports_resource(package)
    # M-Team's sports category uses screenshots as the description body; do
    # not insert a Douban synopsis or generated prose ahead of them.
    description = "" if sports_release else str(package.get("description") or "").strip()
    intro_source = package.get("_intro_source", "douban")
    if intro_source not in {"douban", "imdb"}:
        raise ValueError("不支持的简介来源")
    if package.get(f"{intro_source}_url") and not sports_release:
        before_intro = driver.execute_script(
            "const e=document.querySelector('[contenteditable=\"true\"][data-lexical-editor]');return (e?.innerText||'').trim();"
        ) or ""
        if not _click_get_intro(driver, source=intro_source):
            raise RuntimeError(f"{intro_source} 简介按钮不可用；停止上传和发布。")
        if not _wait_for_intro(driver, previous_text=str(before_intro)):
            try:
                diagnostic=driver.execute_script(r"""
                    const editor=document.querySelector('[contenteditable="true"][data-lexical-editor]');
                    return {url:location.origin+location.pathname,
                      douban:document.getElementById('douban')?.value||'',
                      editor_text_length:(editor?.innerText||'').trim().length,
                      buttons:[...document.querySelectorAll('button')].filter(b=>/获取简介|獲取簡介|取得簡介/i.test(b.innerText||''))
                        .map(b=>({text:b.innerText,disabled:b.disabled,visible:!!b.offsetParent})),
                      alerts:[...document.querySelectorAll('.ant-form-item-explain-error,.ant-popconfirm-message,.ant-message-notice-content,.ant-modal-content')].map(e=>e.innerText.slice(0,240)),
                      requests:(window.__mteamUploadDiagnostics||[]).slice(-8)};
                """)
                output=Path(str((package.get('torrent') or {}).get('path') or '')).parent
                if output.is_absolute():
                    _save_publish_result(output/'description-fetch-failed.json',diagnostic)
                    _capture_full_page(driver,output/'description-fetch-failed.png')
            except Exception:
                pass
            raise RuntimeError(f"{intro_source} 简介未通过按钮获取或尚未载入；停止上传和发布。")
        package["_description_source"] = f"{intro_source}_fetch_button"
    elif len(description) >= 50:
        if not _fill_description(driver, description):
            raise RuntimeError("资料包简介未能写入 M-Team 编辑器；停止上传和发布。")
    _fill_missing_audio_choice(driver, package)
    _fill_missing_video_choice(driver, package)
    _fill_resolution_choice(driver, package)

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
        if not _append_editor_image_spacing(driver, count=2):
            print("提示：无法在简介末尾插入两个空行，请手工回车两次后上传截图。")
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
            # Submit images serially; concurrent requests intermittently fail
            # at the image host and leave otherwise complete forms unusable.
            for index, filename in enumerate(valid, 1):
                image_inputs = driver.find_elements("css selector", 'input[type=file][accept*="image"]')
                if not image_inputs:
                    print("警告：截图上传控件已消失；停止最终发布。")
                    return
                if not _upload_screenshot(driver, filename, index):
                    print("警告：本地截图未能全部上传，请检查图片上传窗口。")
                    try:
                        torrent_path = Path(str((package.get("torrent") or {}).get("path") or ""))
                        evidence_path = torrent_path.parent / "screenshot-upload-failed.png"
                        _capture_full_page(driver, evidence_path)
                        state = driver.execute_script("""
                            const visible = [...document.querySelectorAll(
                              '[role="dialog"], .ant-modal-wrap, .ant-modal, [data-state="open"]')]
                              .filter(e => e.offsetParent !== null);
                            const root = visible.find(e => e.querySelector('.ant-upload-list-item')) || visible[0];
                            const items = [...(root?.querySelectorAll('.ant-upload-list-item') || [])];
                            return {modal_count: visible.length, item_count: items.length,
                              uploading: items.filter(e => e.classList.contains('ant-upload-list-item-uploading')).length,
                              failed: items.filter(e => e.classList.contains('ant-upload-list-item-error')).length,
                              has_image_input: !!document.querySelector('input[type=file][accept*="image"]')};
                        """) or {}
                        diagnostic_path = torrent_path.parent / "screenshot-upload-failed.json"
                        diagnostic_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                        print(f"截图上传证据已保存在：{evidence_path}；诊断：{diagnostic_path}", flush=True)
                    except Exception as exc:
                        print(f"保存截图上传证据失败：{type(exc).__name__}", flush=True)
                    return
                time.sleep(2)
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
    session_source = parser.add_mutually_exclusive_group()
    session_source.add_argument("--cookie-file", "--session-file", dest="session_file", type=Path, help="M-Team Cookie 导出或请求头复制文件")
    session_source.add_argument("--cookie-stdin", action="store_true", help="从 stdin 内存读取 CookieCloud JSON；不写入会话文件")
    parser.add_argument("--url", default="https://kp.m-team.cc/upload", help="M-Team 发布页地址；默认使用 kp.m-team.cc/upload")
    parser.add_argument("--profile-dir", type=Path, help="专用 Chrome 配置目录；可用于复用 CookieCloud 登录态")
    parser.add_argument("--credentials-file", type=Path, help="两行账密文件；仅在网页未登录时通过 ChromeDriver 登录，文件须为 chmod 600")
    parser.add_argument("--upload", action="store_true", help="在填表后上传种子和截图")
    parser.add_argument("--submit", action="store_true", help="校验完成后点击最终发布一次；默认仅预览")
    parser.add_argument("--intro-source", choices=("douban", "imdb"), default="douban", help="简介获取按钮来源；仅用户明确允许该条例外时选择 imdb")
    parser.add_argument("--retry-ambiguous", action="store_true", help="用户明确要求重发、且已核对站内与 qB 后，允许对历史含糊回执重试一次并保留旧记录")
    parser.add_argument("--result-json", type=Path, help="发布结果与一次性提交记录；默认资料包旁 publish-result.json")
    parser.add_argument("--recall-official", action="store_true", help="发布后通过 MoviePilot 配置按详情页 ID 下载官方种子")
    parser.add_argument("--moviepilot-container", default="moviepilot")
    parser.add_argument("--site-id", type=int, default=1)
    parser.add_argument("--yes", action="store_true", help="跳过上传前确认；仅建议在你已检查资料包后使用")
    parser.add_argument("--keep-open", action="store_true", help="填表后等待回车再关闭浏览器")
    parser.add_argument("--login-timeout", type=int, default=600, help="等待手工登录的秒数；默认 600")
    parser.add_argument("--login-only", action="store_true", help="只打开专用 Chrome 配置供你手工登录，不读取资料包、不填表")
    parser.add_argument("--inspect-only", action="store_true", help="只读取发布页控件，不填写、不上传")
    return parser


def _save_publish_result(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".writing")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _capture_full_page(driver, path: Path) -> None:
    metrics = driver.execute_cdp_cmd("Page.getLayoutMetrics", {})
    size = metrics.get("cssContentSize") or metrics["contentSize"]
    screenshot = driver.execute_cdp_cmd("Page.captureScreenshot", {
        "format": "png", "captureBeyondViewport": True, "fromSurface": True,
        "clip": {"x":0, "y":0, "width":size["width"], "height":size["height"], "scale":1},
    })
    path.write_bytes(base64.b64decode(screenshot["data"]))


def _publish_preflight(driver, package: dict) -> dict:
    """Read the filled form, including technical choices parsed by M-Team."""
    _validate_release_package_metadata(package)
    bangumi_url = _validated_bangumi_url(package)
    values = driver.execute_script(r"""
        const val = id => document.getElementById(id)?.value || '';
        const selection = id => {
            const input = document.getElementById(id);
            const box = input?.closest('.ant-select') || input?.parentElement;
            return (box?.querySelector('.ant-select-content')?.innerText
              || box?.querySelector('.ant-select-selection-item')?.innerText || '').trim();
        };
        const editor = document.querySelector('[contenteditable="true"][data-lexical-editor]');
        const editorImages = [...(editor?.querySelectorAll('img') || [])];
        return { title:val('name'), subtitle:val('smallDescr'), douban_url:val('douban'), imdb_url:val('imdb'), bangumi_url:val('bangumi') || selection('bangumi'),
            category:selection('category'), resolution:selection('standard'),
            video_codec:selection('videoCodec'), audio_codec:selection('audioCodec'),
            mediainfo_length:val('mediainfo').length, description_length:(editor?.innerText || '').trim().length,
            screenshot_count:editorImages.length,
            screenshot_loaded_count:editorImages.filter(img => img.complete &&
              img.naturalWidth >= 64 && img.naturalHeight >= 64 &&
              Boolean(img.currentSrc || img.src)).length,
            screenshot_dimensions:editorImages.map(img => ({
              width:img.naturalWidth || 0, height:img.naturalHeight || 0,
              complete:Boolean(img.complete), has_src:Boolean(img.currentSrc || img.src),
            })),
            torrent_file_count:document.querySelector('#torrent-input')?.files?.length || 0,
            invalid_controls:[...document.querySelectorAll('input,textarea,select')].filter(e=>!e.checkValidity()).length,
            invalid_control_labels:[...document.querySelectorAll('input,textarea,select')]
              .filter(e=>!e.checkValidity())
              .map(e=>e.closest('.ant-form-item, .form-item, .form-group')?.querySelector('label')?.innerText?.trim())
              .filter(Boolean).map(s=>s.slice(0,80)),
            form_error_labels:[...document.querySelectorAll('.ant-form-item-explain-error')]
              .map(e=>e.closest('.ant-form-item, .form-item, .form-group')?.querySelector('label')?.innerText?.trim())
              .filter(Boolean).map(s=>s.slice(0,80)),
            errors:[...document.querySelectorAll('.ant-form-item-explain-error')].map(e=>e.innerText.trim()).filter(Boolean)
        };
    """)
    if package.get("_description_source"):
        values["description_source"] = package["_description_source"]
    failures = []
    if bangumi_url and values.get('bangumi_url') != bangumi_url:
        failures.append('bangumi_url')
    for field in ("title", "subtitle", "douban_url", "imdb_url"):
        wanted = str(package.get(field) or "")
        if wanted and values.get(field) != wanted:
            failures.append(field)
    actual_title = str(values.get("title") or "")
    if re.search(r"(?<![A-Z0-9])\d{2,3}(?:\.\d+)?\s*FPS(?![A-Z0-9])", actual_title, re.I):
        failures.append("title_hfr_spelling (使用 HFR，不得写数字 FPS)")
    media = package.get("media")
    if isinstance(media, dict) and media.get("hfr") and not re.search(r"\bHFR\b", actual_title, re.I):
        failures.append("title_hfr_missing")
    canon = lambda s: re.sub(r"\s+", "", str(s)).replace("電", "电").replace("劇", "剧").replace("綜", "综").replace("藝", "艺").replace("動", "动").replace("畫", "画").replace("／", "/")
    if canon(values.get("category")) != canon(package.get("category")):
        failures.append("category")
    media = package.get("media") if isinstance(package.get("media"), dict) else {}
    expected_audio = _MTEAM_AUDIO_CHOICES.get(str(media.get("audio_codec") or "").strip())
    if expected_audio and canon(values.get("audio_codec")) != canon(expected_audio):
        failures.append("audio_codec_mismatch (expected " + expected_audio + ")")
    expected_resolution = _MTEAM_RESOLUTION_CHOICES.get(str(media.get("resolution") or ""))
    if expected_resolution and canon(values.get("resolution")) != canon(expected_resolution):
        failures.append("resolution_mismatch (expected " + expected_resolution + ")")
    # The site parses these choices from MediaInfo; only check that it did so.
    for field in ("resolution", "video_codec", "audio_codec"):
        if not values.get(field) or re.search(r"请.*选择|請.*選擇", values[field]):
            failures.append(field + " (MediaInfo 未自动识别)")
    if values.get("screenshot_count", 0) < 4:
        failures.append("screenshots>=4")
    if values.get("screenshot_loaded_count", 0) < 4:
        failures.append("screenshots_loaded>=4")
    if values.get("torrent_file_count") != 1 or values.get("mediainfo_length", 0) < 100:
        failures.append("torrent/MediaInfo")
    if not is_sports_resource(package) and values.get("description_length", 0) < 50:
        failures.append("description")
    intro_source = package.get("_intro_source", "douban")
    if not is_sports_resource(package):
        if intro_source not in {"douban", "imdb"}:
            failures.append("invalid_intro_source")
        elif (package.get("douban_url") or intro_source == "imdb") and package.get("_description_source") != f"{intro_source}_fetch_button":
            failures.append(f"{intro_source}_description_not_fetched")
    if values.get("invalid_controls") or values.get("errors"):
        invalid_labels = [str(label).strip() for label in values.get("invalid_control_labels") or []]
        form_labels = [str(label).strip() for label in values.get("form_error_labels") or []]
        intro_label = lambda label: re.sub(r"\s+", "", label).casefold() in {"简介", "簡介", "description"}
        sports_images_only = (
            is_sports_resource(package)
            and values.get("description_length", 0) == 0
            and values.get("screenshot_count", 0) >= 4
            and values.get("screenshot_loaded_count", 0) >= 4
            and (bool(invalid_labels) or bool(form_labels))
            and all(intro_label(label) for label in [*invalid_labels, *form_labels])
            and values.get("invalid_controls", 0) <= len(invalid_labels)
        )
        if not sports_images_only:
            labels = list(dict.fromkeys([*invalid_labels, *form_labels]))
            suffix = ":" + "|".join(label[:80] for label in labels[:8]) if labels else ""
            failures.append("required_controls" + suffix)
        else:
            values["sports_screenshot_only_description"] = True
    if failures:
        raise ValueError("最终发布前校验未通过：" + ", ".join(failures))
    return values


def _dismiss_known_server_switch_notice(driver) -> bool:
    """Dismiss only the informational proxy/server-switch notice that blocks submit."""
    dialogs = driver.find_elements("css selector", ".ant-modal-wrap")
    for dialog in dialogs:
        if not dialog.is_displayed():
            continue
        text = re.sub(r"\s+", " ", str(dialog.text or ""))
        has_connectable = any(value in text for value in ("可连接服务器", "可連接伺服器"))
        has_switch = any(value in text for value in ("切换可用服务器", "切換可用伺服器"))
        has_reload = any(value in text for value in ("重新载入网页", "重新載入網頁", "重新载入", "重新載入"))
        if not (has_connectable and has_switch and has_reload):
            continue
        buttons = dialog.find_elements("css selector", "button")
        acknowledgements = [button for button in buttons
                            if re.sub(r"\s+", "", str(button.text or "")) in {"知道了", "我知道了"}]
        if len(acknowledgements) != 1:
            raise ValueError("已识别服务器切换提示，但未找到唯一的确认按钮")
        acknowledgements[0].click()
        from selenium.webdriver.support.ui import WebDriverWait
        try:
            WebDriverWait(driver, 5).until(lambda _driver: not dialog.is_displayed())
        except Exception:
            raise ValueError("服务器切换提示未能关闭；尚未尝试最终发布") from None
        return True
    return False


def _visible_publish_modal_diagnostics(driver) -> list[dict[str, object]]:
    """Capture visible modal copy only; never persist page storage or request headers."""
    def safe(value: object) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        text = re.sub(r"https?://\S+", "[url]", text, flags=re.I)
        text = re.sub(
            r"(cookie|passkey|token|authorization|api.?key|password)\s*[:=]\s*\S+",
            r"\1=[redacted]", text, flags=re.I,
        )
        return text[:500]

    diagnostics = []
    for dialog in driver.find_elements("css selector", ".ant-modal-wrap"):
        try:
            if not dialog.is_displayed():
                continue
            buttons = dialog.find_elements("css selector", "button")
            diagnostics.append({
                "text": safe(dialog.text),
                "buttons": [safe(button.text) for button in buttons[:12]],
            })
        except Exception:
            continue
    return diagnostics


def _publish_submit_buttons(driver):
    buttons = driver.find_elements("css selector", 'button[type="submit"]')
    return [b for b in buttons if b.is_displayed() and b.is_enabled()
            and re.sub(r"\s+", "", str(b.text or "")) in {"发布", "發布", "發佈"}]


def _retryable_pre_dispatch_receipt(receipt: dict) -> bool:
    from .autonomous import operation_decision
    return operation_decision(receipt) == "retry_pre_dispatch"


def _user_authorized_ambiguous_retry(receipt: dict, args) -> bool:
    return bool(
        getattr(args, "retry_ambiguous", False)
        and receipt.get("status") == "publish_ambiguous"
        and receipt.get("submit_attempted_at")
        and not receipt.get("mteam_torrent_id")
        and not receipt.get("ambiguous_retry_used")
        and not receipt.get("prior_ambiguous_attempts")
    )


def _recall_published(package: dict, result: dict, args, result_path: Path) -> dict:
    from .recall_official import recall
    from .prepare import _bdecode
    if result.get("title") != package.get("title"):
        raise ValueError("已有发布记录与当前资料包标题不一致，拒绝召回其它资源")
    source = Path(str(package.get("prepared_path") or package.get("input_path") or ""))
    output = result_path.parent / f"official-{result['mteam_torrent_id']}.torrent"
    print(f"[召回] MoviePilot 按详情页 ID {result['mteam_torrent_id']} 获取官方种子…", flush=True)
    try:
        receipt = recall(str(result["mteam_torrent_id"]), source=source, output=output,
                         container=args.moviepilot_container, site_id=args.site_id)
        uploaded_path = Path(str((package.get("torrent") or {}).get("path") or ""))
        uploaded_info = _bdecode(uploaded_path.read_bytes())[b"info"]
        official_info = _bdecode(output.read_bytes())[b"info"]
        if any(uploaded_info.get(k) != official_info.get(k) for k in (b"pieces", b"piece length", b"files", b"length", b"name")):
            raise ValueError("官方种子与上传种子的路径、大小或分片哈希不一致；文件已保留，停止继续操作")
    except (ValueError, RuntimeError, OSError) as exc:
        result.update(status="recall_failed", recall_error=str(exc))
        _save_publish_result(result_path, result)
        raise
    result.update(status="official_downloaded", official_torrent=receipt)
    result.pop('recall_error',None)
    _save_publish_result(result_path, result)
    print(f"[召回完成] {receipt['file_count']} 个文件，{receipt['size_bytes']} 字节，完整清单验证通过：{output}", flush=True)
    return result


def _submit_publish(driver, package: dict, args, result_path: Path) -> dict:
    prior_attempts = []
    prior_ambiguous_attempts = []
    ambiguous_backup = None
    if getattr(args, "retry_ambiguous", False) and not result_path.is_file():
        raise ValueError("含糊发布重试必须复用已有回执路径")
    if result_path.exists():
        previous = json.loads(result_path.read_text(encoding="utf-8"))
        authorized_retry = _user_authorized_ambiguous_retry(previous, args)
        if previous.get("mteam_torrent_id"):
            raise ValueError("已有已确认的发布 ID，拒绝重复点击；请召回已有种子")
        if getattr(args, "retry_ambiguous", False) and not authorized_retry:
            raise ValueError("该回执不允许再次重试含糊发布，拒绝重复点击")
        if previous.get("submit_attempted_at") and not (_retryable_pre_dispatch_receipt(previous) or authorized_retry):
            raise ValueError("已有最终发布提交记录，拒绝重复点击；请使用已有详情页 ID 召回")
        if _retryable_pre_dispatch_receipt(previous):
            prior_attempts = list(previous.get("prior_non_dispatched_attempts") or [])
            prior_attempts.append({key: previous.get(key) for key in (
                "status", "submit_attempted_at", "click_intercepted",
                "create_request_started", "intercepted_screenshot", "error")})
        if authorized_retry:
            ambiguous_backup = result_path.with_name(
                f"{result_path.stem}.ambiguous-{previous['submit_attempted_at']}{result_path.suffix}"
            )
            if ambiguous_backup.exists():
                if json.loads(ambiguous_backup.read_text(encoding="utf-8")) != previous:
                    raise ValueError("历史含糊回执备份已存在且内容不同，停止重试")
            else:
                _save_publish_result(ambiguous_backup, previous)
            prior_ambiguous_attempts.append({key: previous.get(key) for key in (
                "status", "submit_attempted_at", "title", "pre_submit_screenshot", "error")})
    values = _publish_preflight(driver, package)
    if _dismiss_known_server_switch_notice(driver):
        # Re-run every form check after the overlay is gone, before recording
        # an attempted submission or clicking the final button.
        values = _publish_preflight(driver, package)
    if hasattr(driver, 'execute_script'):
        driver.execute_script(r"""
            window.__mteamPublishResponse = null;
            window.__mteamPublishRequestStarted = false;
            const observe = text => {
                try {
                    const p = JSON.parse(text);
                    const d = p.data;
                    window.__mteamPublishResponse = {code:p.code,
                        message:typeof p.message === 'string' ? p.message : '',
                        id: typeof d === 'string' || typeof d === 'number' ? d : d?.id};
                } catch (_) {}
            };
            const open = XMLHttpRequest.prototype.open;
            XMLHttpRequest.prototype.open = function(method, url, ...rest) {
                if(String(url).includes('/torrent/createOredit')) {
                    window.__mteamPublishRequestStarted = true;
                    this.addEventListener('load', () => observe(this.responseText));
                }
                return open.call(this, method, url, ...rest);
            };
            const fetchOriginal = window.fetch;
            window.fetch = async function(...args) {
                if(String(args[0]?.url || args[0]).includes('/torrent/createOredit'))
                    window.__mteamPublishRequestStarted = true;
                const response = await fetchOriginal.apply(this,args);
                if(String(args[0]?.url || args[0]).includes('/torrent/createOredit'))
                    response.clone().text().then(observe);
                return response;
            };
        """)
    screenshot = result_path.parent / "publish-before-submit.png"
    _capture_full_page(driver, screenshot)
    buttons = _publish_submit_buttons(driver)
    if len(buttons) != 1:
        raise ValueError("未找到唯一可用的最终发布按钮")
    result = {"status":"submitting", "title":package.get("title"), "form":values,
              "pre_submit_screenshot":str(screenshot), "submit_attempted_at":int(time.time())}
    if prior_attempts:
        result["prior_non_dispatched_attempts"] = prior_attempts
    if prior_ambiguous_attempts:
        result.update(prior_ambiguous_attempts=prior_ambiguous_attempts,
                      ambiguous_retry_used=True,
                      retry_authorization="explicit_user_request_after_site_and_qb_review",
                      prior_ambiguous_receipt=str(ambiguous_backup))
    _save_publish_result(result_path, result)
    print("[发布] 最终校验通过，提交一次并等待详情页…", flush=True)
    try:
        try:
            buttons[0].click()
        except Exception as click_error:
            if type(click_error).__name__ != "ElementClickInterceptedException":
                raise
            request_started = None
            if hasattr(driver, "execute_script"):
                try:
                    request_started = driver.execute_script(
                        "return window.__mteamPublishRequestStarted === true"
                    )
                except Exception:
                    request_started = None
            if request_started is not False:
                result.update(status="publish_ambiguous",
                              error="发布点击异常且无法证明请求未发出；禁止自动重试",
                              click_intercepted=True,
                              create_request_started=request_started)
                _capture_full_page(driver, result_path.parent / "publish-ambiguous.png")
                _save_publish_result(result_path, result)
                raise RuntimeError(result["error"]) from None

            intercept_screenshot = result_path.parent / "publish-click-intercepted.png"
            _capture_full_page(driver, intercept_screenshot)
            result.update(click_intercepted=True, create_request_started=False,
                          intercepted_screenshot=str(intercept_screenshot),
                          intercepted_modal=_visible_publish_modal_diagnostics(driver))
            if not _dismiss_known_server_switch_notice(driver):
                result.update(status="submission_not_dispatched",
                              error="按钮被未知弹窗遮挡；已确认最终发布请求未启动，停止重试")
                _save_publish_result(result_path, result)
                raise RuntimeError(result["error"]) from None

            # A late-arriving, exact server-switch notice is safe to close. Re-run
            # every form check, then make at most one fresh click in this run.
            values = _publish_preflight(driver, package)
            retry_buttons = _publish_submit_buttons(driver)
            if len(retry_buttons) != 1:
                result.update(status="submission_not_dispatched",
                              error="已关闭服务器提示，但重新校验后没有唯一发布按钮；停止重试",
                              form=values)
                _save_publish_result(result_path, result)
                raise RuntimeError(result["error"]) from None
            result.update(form=values, late_server_notice_dismissed=True,
                          retry_after_preflight=True)
            _save_publish_result(result_path, result)
            print("[发布] 已关闭延迟出现的服务器提示并重新校验；本次仅重试一次。", flush=True)
            try:
                retry_buttons[0].click()
            except Exception as retry_error:
                if type(retry_error).__name__ != "ElementClickInterceptedException":
                    raise
                request_started = None
                try:
                    request_started = driver.execute_script(
                        "return window.__mteamPublishRequestStarted === true"
                    )
                except Exception:
                    pass
                result.update(click_intercepted=True,
                              create_request_started=request_started)
                _capture_full_page(driver, result_path.parent / "publish-click-intercepted-retry.png")
                if request_started is False:
                    result.update(status="submission_not_dispatched",
                                  error="唯一一次安全重试仍被弹窗拦截；发布请求未启动，停止")
                else:
                    result.update(status="publish_ambiguous",
                                  error="重试点击后无法证明请求未发出；禁止再次提交")
                _save_publish_result(result_path, result)
                raise RuntimeError(result["error"]) from None
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            response = driver.execute_script('return window.__mteamPublishResponse') if hasattr(driver, 'execute_script') else None
            if response and response.get('code') is not None and str(response['code']) != '0':
                # Store only a safe code; server messages may embed user data.
                message=str(response.get('message') or '')[:300]
                if re.search(r'https?://|credential|cookie|passkey|authorization|api.?key|token|password',message,re.I):
                    message='[redacted server message]'
                result.update(status='publish_rejected', api_code=str(response['code']),
                              api_message=message,
                              error='M-Team 明确拒绝本次发布；保留页面证据，不自动重新提交')
                _capture_full_page(driver, result_path.parent / 'publish-rejected.png')
                _save_publish_result(result_path, result)
                raise RuntimeError(result['error'] + ' code=' + str(response['code']))
            parsed = urlsplit(driver.current_url)
            match = re.fullmatch(r"/detail/([1-9]\d*)/?", parsed.path)
            if match and parsed.hostname == "kp.m-team.cc":
                result.update(status="published", mteam_torrent_id=match[1],
                              mteam_detail_url=f"https://kp.m-team.cc/detail/{match[1]}")
                _save_publish_result(result_path, result)
                from selenium.webdriver.support.ui import WebDriverWait

                # The SPA redirects before loading the candidate detail data.
                # Download does not depend on this render, but its screenshot
                # should show the published resource rather than a skeleton.
                try:
                    WebDriverWait(driver, 15).until(lambda page: str(package.get("title") or "")
                        in page.find_element("tag name", "body").text)
                except Exception:
                    pass
                _capture_full_page(driver, result_path.parent / "published-detail.png")
                print(f"[发布成功] {result['mteam_detail_url']}", flush=True)
                if args.recall_official:
                    return _recall_published(package, result, args, result_path)
                return result
            time.sleep(1)
        result.update(status="publish_ambiguous", error="提交后未返回可确认的详情页；停止自动重试")
        _capture_full_page(driver, result_path.parent / "publish-ambiguous.png")
        _save_publish_result(result_path, result)
        raise RuntimeError(result["error"])
    except Exception:
        if result.get("status") == "submitting":
            result.update(status="publish_ambiguous", error="点击后结果未确认；禁止自动重复提交")
            _save_publish_result(result_path, result)
        raise


def main(argv: list[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.submit and (not args.upload or args.login_only or args.inspect_only):
            raise ValueError("--submit 需要 --upload 和资料包，不能与仅登录/仅检查组合")
        if args.retry_ambiguous and not args.submit:
            raise ValueError("--retry-ambiguous 必须与 --submit 一起使用")
        if args.recall_official and not args.submit:
            raise ValueError("--recall-official 需要 --submit；已有详情页请使用 recall-official 子命令")
        if args.login_only or args.inspect_only:
            if not args.profile_dir:
                raise ValueError("--login-only/--inspect-only 必须指定专用 Chrome 配置目录 --profile-dir")
        elif not args.package or not args.package.is_file():
            raise FileNotFoundError(f"找不到资料包：{args.package or '<未指定>'}")
        if not args.login_only and not args.session_file and not args.cookie_stdin and not args.profile_dir:
            raise ValueError("请提供 --cookie-file，或提供已登录的 CookieCloud Chrome 配置目录 --profile-dir")
        if args.session_file and not args.session_file.is_file():
            raise FileNotFoundError(f"找不到会话文件：{args.session_file}")
        if args.credentials_file and not args.credentials_file.is_file():
            raise FileNotFoundError("找不到 M-Team 账密文件")
        package = json.loads(args.package.read_text(encoding="utf-8")) if args.package else {}
        package.pop("_description_source", None)
        package["_intro_source"] = args.intro_source
        if args.intro_source == "imdb" and not package.get("imdb_url"):
            raise ValueError("IMDb 简介例外需要有效 IMDb 链接")
        if args.package and not (args.recall_official or args.inspect_only):
            _validate_release_package_metadata(package)
        result_path = args.result_json or (args.package.parent / "publish-result.json" if args.package else None)
        if args.retry_ambiguous and (not result_path or not result_path.is_file()):
            raise ValueError("含糊发布重试必须复用已有回执路径")
        if args.submit and result_path.exists():
            previous = json.loads(result_path.read_text(encoding="utf-8"))
            if previous.get("mteam_torrent_id"):
                if args.recall_official:
                    _recall_published(package, previous, args, result_path)
                else:
                    print(f"已发布：{previous['mteam_detail_url']}；未重复提交。")
                return
            if args.retry_ambiguous and not _user_authorized_ambiguous_retry(previous, args):
                raise ValueError("该回执不允许再次重试含糊发布")
            if previous.get("submit_attempted_at") and not (_retryable_pre_dispatch_receipt(previous) or _user_authorized_ambiguous_retry(previous, args)):
                raise ValueError("已有发布提交但详情页 ID 尚未确认，拒绝重复发布")
        if args.cookie_stdin:
            session = load_mteam_session_text(sys.stdin.read())
        else:
            session = load_mteam_session(args.session_file) if args.session_file else MTeamSession()
        webdriver = _load_selenium()
        options = _configure_chrome_options(webdriver.ChromeOptions())
        # M-Team pages keep some subresource requests open. Return at
        # DOMContentLoaded and let the explicit form/auth waits below decide
        # when the page is usable instead of timing out on network idle.
        options.page_load_strategy = "eager"
        if args.cookie_stdin:
            # Keep CookieCloud-backed browsing data in a disposable session.
            options.add_argument("--incognito")
        if args.credentials_file:
            options.add_argument("--disable-save-password-bubble")
            options.add_experimental_option("prefs", {
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False,
            })
        if args.profile_dir:
            options.add_argument(f"--user-data-dir={args.profile_dir.resolve()}")
        driver_path = os.environ.get("CHROMEDRIVER", "").strip()
        if driver_path:
            from selenium.webdriver.chrome.service import Service

            driver = webdriver.Chrome(service=Service(driver_path), options=options)
        else:
            driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(45)
        try:
            origin = _origin(args.url)
            driver.get(args.url if args.login_only or args.inspect_only else origin)
            if args.login_only:
                auth_present = _has_mteam_auth(driver) and not _is_login_url(driver.current_url)
                if not auth_present and args.credentials_file:
                    auth_present = _login_with_credentials(
                        driver, args.credentials_file, args.url, args.login_timeout
                    )
                elif not auth_present:
                    auth_present = _wait_for_mteam_auth(driver, args.login_timeout)
                    if auth_present:
                        driver.get(args.url)
                        time.sleep(2)
                        auth_present = _has_mteam_auth(driver) and not _is_login_url(driver.current_url)
                if not auth_present:
                    raise ValueError(
                        "未建立有效 M-Team 登录态；请检查站点网络、账密或所需的验证码/二次验证"
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
            if not auth_present and args.credentials_file:
                auth_present = _login_with_credentials(
                    driver, args.credentials_file, args.url, args.login_timeout
                )
            elif args.profile_dir and not auth_present:
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
            if args.credentials_file and _is_login_url(driver.current_url):
                if not _login_with_credentials(
                    driver, args.credentials_file, args.url, args.login_timeout
                ):
                    raise ValueError("账密登录未建立有效 M-Team 会话；可能需要验证码或二次验证")
                if driver.current_url.rstrip("/") != args.url.rstrip("/"):
                    driver.get(args.url)
                    time.sleep(2)
            if not _wait_for_publish_page(driver, args.url, args.login_timeout):
                raise ValueError("M-Team 登录超时；请重新运行命令并在 ChromeDriver 窗口中完成登录。")
            if not _wait_for_form_controls(driver, args.url):
                try:
                    _capture_full_page(driver, result_path.parent / 'publish-form-unavailable.png')
                except Exception:
                    pass
                raise ValueError('M-Team 发布页控件未加载；未填写、未提交，已保留页面截图')
            if args.upload:
                try:
                    _install_upload_diagnostics(driver)
                except Exception as exc:
                    print(f"提示：无法启用截图网络诊断（{type(exc).__name__}）；正常继续。", flush=True)
            if not args.yes:
                technical_type = str(package.get("technical_info_type") or "MediaInfo")
                action = f"标题、副标题、豆瓣链接和 {technical_type}"
                if args.upload:
                    action += "，以及种子和本地截图文件"
                answer = input(
                    f"即将把{action}写入 M-Team 发布页"
                    + ("并上传文件" if args.upload else "")
                    + ("，并将点击最终发布。继续？[y/N] " if args.submit else "，但不会点击最终发布。继续？[y/N] ")
                ).strip().casefold()
                if answer not in {"y", "yes"}:
                    print("已取消填表。")
                    return
            _fill_page(driver, package, upload=args.upload)
            if args.submit:
                _submit_publish(driver, package, args, result_path)
                return
            print("已停止在最终发布之前；请检查页面内容。")
            if args.keep_open or sys.stdin.isatty():
                input("检查完成后按回车关闭 ChromeDriver 窗口。")
        finally:
            driver.quit()
    except (FileNotFoundError, ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
