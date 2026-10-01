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


def _wait_for_intro(driver, timeout: float = 60.0) -> bool:
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
        if state and len(state.get("text", "")) >= 50:
            return True
        time.sleep(0.25)
    return False


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


def _wait_for_image_uploads(driver, expected_count: int, timeout: float = 120.0) -> bool:
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


def _upload_screenshot(driver, filename: str, expected_count: int) -> bool:
    """Retry only explicitly failed uploads, never uncertain in-flight requests."""
    for attempt in range(3):
        inputs = driver.find_elements('css selector', 'input[type=file][accept*="image"]')
        if not inputs:
            return False
        inputs[-1].send_keys(filename)
        if _wait_for_image_uploads(driver, expected_count):
            return True
        if attempt == 2:
            return False
        removed = driver.execute_script("""
            const visible = [...document.querySelectorAll(
                '[role="dialog"], .ant-modal-wrap, .ant-modal, [data-state="open"]')]
                .filter(e => e.offsetParent !== null);
            const dialog = visible.find(e => e.querySelector('.ant-upload-list-item-error'))
                || visible[0];
            const item = [...(dialog?.querySelectorAll('.ant-upload-list-item-error') || [])]
                .find(e => e.innerText.includes(arguments[0]));
            const button = item?.querySelector('svg[data-icon="delete"]')?.closest('button')
                || item?.querySelector('button[title="Remove file"]');
            if (!button) return false;
            button.click(); return true;
        """, Path(filename).name)
        if not removed:
            return False
        print(f'截图上传明确失败，移除失败项后重试（{attempt + 1}/2）', flush=True)
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


def _fill_missing_audio_choice(driver, package: dict) -> bool:
    """Keep MediaInfo's selection; resolve an empty control from measured audio."""
    current = driver.execute_script("const n=document.getElementById('audioCodec');return n?.closest('.ant-select')?.querySelector('.ant-select-content,.ant-select-selection-item')?.innerText || ''; ")
    if current and not re.search(r'请.*选择|請.*選擇', current):
        return True
    codec = str((package.get('media') or {}).get('audio_codec') or '')
    label = {'DD':'AC3(DD)', 'DDP':'E-AC3(DDP)', 'DDP Atmos':'E-AC3 Atmos(DDP Atmos)',
             'DTS-HD MA':'DTS-HD MA', 'TrueHD Atmos':'TrueHD Atmos',
             'LPCM':'LPCM/PCM', 'MP3':'MP2/3', 'MP2':'MP2/3', 'Opus':'Other'}.get(codec, codec)
    if not label:
        return False
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


def _requires_bangumi(package: dict) -> bool:
    animation = '动画' in str(package.get('category') or '').replace('動', '动').replace('畫', '画')
    return animation and (package.get('tmdb') or {}).get('original_language') != 'en'


def _validated_bangumi_url(package: dict) -> str:
    value = str(package.get('bangumi_url') or '').strip()
    if value and not re.fullmatch(r'https://(?:bgm\.tv|bangumi\.tv|chii\.in)/subject/[1-9]\d*/?', value):
        raise ValueError('Bangumi 链接必须是有效的作品 subject 链接')
    if _requires_bangumi(package) and not value:
        raise ValueError('动画发布必须提供 bangumi_url，停止最终发布')
    return value


def _fill_page(driver, package: dict[str, object], *, upload: bool) -> None:
    bangumi_url = _validated_bangumi_url(package)
    _fill_field(driver, ("标题", "title"), str(package.get("title") or package.get("release_name") or ""), "标题")
    _fill_field(driver, ("副标题", "subtitle"), str(package.get("subtitle") or ""), "副标题")
    _fill_field(driver, ("imdb", "imdb url", "IMDb链接"), str(package.get("imdb_url") or ""), "IMDb链接")
    _fill_field(driver, ("豆瓣链接", "douban", "douban url"), str(package.get("douban_url") or ""), "豆瓣链接")
    technical_type = str(package.get("technical_info_type") or "MediaInfo")
    technical_text = str(package.get("technical_info_text") or package.get("mediainfo_text") or "")
    _fill_field(driver, ("mediainfo", "media info", "bdinfo", "bd info"), technical_text, technical_type)
    _select_category(driver, str(package.get("category") or ""))
    if bangumi_url:
        _fill_field(driver, ("bangumi", "Bangumi 链接"), bangumi_url, "Bangumi 链接")
    if package.get("douban_url"):
        time.sleep(0.5)
        if _click_get_intro(driver):
            if not _wait_for_intro(driver):
                print("警告：豆瓣简介尚未获取完成；停止上传及最终发布，避免迟到的简介覆盖截图。")
                return
    _fill_missing_audio_choice(driver, package)

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
    parser.add_argument("--cookie-file", "--session-file", dest="session_file", type=Path, help="M-Team Cookie 导出或请求头复制文件")
    parser.add_argument("--url", default="https://kp.m-team.cc/upload", help="M-Team 发布页地址；默认使用 kp.m-team.cc/upload")
    parser.add_argument("--profile-dir", type=Path, help="专用 Chrome 配置目录；可用于复用 CookieCloud 登录态")
    parser.add_argument("--upload", action="store_true", help="在填表后上传种子和截图")
    parser.add_argument("--submit", action="store_true", help="校验完成后点击最终发布一次；默认仅预览")
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
        return { title:val('name'), subtitle:val('smallDescr'), douban_url:val('douban'), imdb_url:val('imdb'), bangumi_url:val('bangumi') || selection('bangumi'),
            category:selection('category'), resolution:selection('standard'),
            video_codec:selection('videoCodec'), audio_codec:selection('audioCodec'),
            mediainfo_length:val('mediainfo').length, description_length:(editor?.innerText || '').trim().length,
            screenshot_count:editor?.querySelectorAll('img').length || 0,
            torrent_file_count:document.querySelector('#torrent-input')?.files?.length || 0,
            invalid_controls:[...document.querySelectorAll('input,textarea,select')].filter(e=>!e.checkValidity()).length,
            errors:[...document.querySelectorAll('.ant-form-item-explain-error')].map(e=>e.innerText.trim()).filter(Boolean)
        };
    """)
    failures = []
    if bangumi_url and values.get('bangumi_url') != bangumi_url:
        failures.append('bangumi_url')
    for field in ("title", "subtitle", "douban_url", "imdb_url"):
        wanted = str(package.get(field) or "")
        if wanted and values.get(field) != wanted:
            failures.append(field)
    canon = lambda s: re.sub(r"\s+", "", str(s)).replace("電", "电").replace("劇", "剧").replace("綜", "综").replace("藝", "艺").replace("動", "动").replace("畫", "画").replace("／", "/")
    if canon(values.get("category")) != canon(package.get("category")):
        failures.append("category")
    # The site parses these choices from MediaInfo; only check that it did so.
    for field in ("resolution", "video_codec", "audio_codec"):
        if not values.get(field) or re.search(r"请.*选择|請.*選擇", values[field]):
            failures.append(field + " (MediaInfo 未自动识别)")
    if values.get("screenshot_count", 0) < 4:
        failures.append("screenshots>=4")
    if values.get("torrent_file_count") != 1 or values.get("mediainfo_length", 0) < 100:
        failures.append("torrent/MediaInfo")
    if values.get("description_length", 0) < 50:
        failures.append("description")
    if values.get("invalid_controls") or values.get("errors"):
        failures.append("required_controls")
    if failures:
        raise ValueError("最终发布前校验未通过：" + ", ".join(failures))
    return values


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
    if result_path.exists():
        previous = json.loads(result_path.read_text(encoding="utf-8"))
        if previous.get("submit_attempted_at"):
            raise ValueError("已有最终发布提交记录，拒绝重复点击；请使用已有详情页 ID 召回")
    values = _publish_preflight(driver, package)
    if hasattr(driver, 'execute_script'):
        driver.execute_script(r"""
            window.__mteamPublishResponse = null;
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
                if(String(url).includes('/torrent/createOredit'))
                    this.addEventListener('load', () => observe(this.responseText));
                return open.call(this, method, url, ...rest);
            };
            const fetchOriginal = window.fetch;
            window.fetch = async function(...args) {
                const response = await fetchOriginal.apply(this,args);
                if(String(args[0]?.url || args[0]).includes('/torrent/createOredit'))
                    response.clone().text().then(observe);
                return response;
            };
        """)
    screenshot = result_path.parent / "publish-before-submit.png"
    _capture_full_page(driver, screenshot)
    buttons = driver.find_elements("css selector", 'button[type="submit"]')
    buttons = [b for b in buttons if b.is_displayed() and b.is_enabled()
               and re.sub(r"\s+", "", b.text) in {"发布", "發佈", "發布"}]
    if len(buttons) != 1:
        raise ValueError("未找到唯一可用的最终发布按钮")
    result = {"status":"submitting", "title":package.get("title"), "form":values,
              "pre_submit_screenshot":str(screenshot), "submit_attempted_at":int(time.time())}
    _save_publish_result(result_path, result)
    print("[发布] 最终校验通过，提交一次并等待详情页…", flush=True)
    try:
        buttons[0].click()
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
        if args.recall_official and not args.submit:
            raise ValueError("--recall-official 需要 --submit；已有详情页请使用 recall-official 子命令")
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
        result_path = args.result_json or (args.package.parent / "publish-result.json" if args.package else None)
        if args.submit and result_path.exists():
            previous = json.loads(result_path.read_text(encoding="utf-8"))
            if previous.get("mteam_torrent_id"):
                if args.recall_official:
                    _recall_published(package, previous, args, result_path)
                else:
                    print(f"已发布：{previous['mteam_detail_url']}；未重复提交。")
                return
            if previous.get("submit_attempted_at"):
                raise ValueError("已有发布提交但详情页 ID 尚未确认，拒绝重复发布")
        session = load_mteam_session(args.session_file) if args.session_file else MTeamSession()
        webdriver = _load_selenium()
        options = _configure_chrome_options(webdriver.ChromeOptions())
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
            if not _wait_for_form_controls(driver, args.url):
                try:
                    _capture_full_page(driver, result_path.parent / 'publish-form-unavailable.png')
                except Exception:
                    pass
                raise ValueError('M-Team 发布页控件未加载；未填写、未提交，已保留页面截图')
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
