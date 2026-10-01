"""Optional CLIProxyAPI assistance for media identity and Douban choices.

The normal media-title-rename flow never calls this module.  It is enabled
only by ``--gpt`` and sends release names plus a compact MediaInfo summary to
the local OpenAI-compatible CLIProxyAPI endpoint.  Credentials are read from
the environment or a local key file and are never included in diagnostics.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class CliproxyError(RuntimeError):
    """The optional local AI service could not return a usable answer."""


@dataclass(frozen=True)
class AIIdentity:
    search_title: str = ""
    title: str = ""
    year: str = ""
    kind: str = ""
    episode: str = ""
    source: str = ""
    group: str = ""
    edition: str = ""
    platform: str = ""
    confidence: float = 0.0
    search_aliases: tuple[str, ...] = ()


def _config_value(text: str, key: str) -> str:
    match = re.search(
        rf"^\s*{re.escape(key)}\s*:\s*['\"]?([^'\"#\s]+)",
        text,
        flags=re.MULTILINE,
    )
    return match.group(1).strip() if match else ""


def _default_config_path() -> Path:
    configured = os.environ.get("CLIPROXY_CONFIG", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path("/data/Zhyw/qwen_mods/CLIProxyAPI-runtime/config.yaml")


def _read_config() -> tuple[str, int]:
    host = os.environ.get("CLIPROXY_HOST", "").strip()
    port = os.environ.get("CLIPROXY_PORT", "").strip()
    path = _default_config_path()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    host = host or _config_value(text, "host") or "127.0.0.1"
    port = port or _config_value(text, "port") or "8317"
    try:
        parsed_port = int(port)
    except ValueError:
        parsed_port = 8317
    return host, parsed_port


def _read_api_key() -> str:
    value = os.environ.get("CLIPROXY_API_KEY", "").strip()
    if value:
        return value
    configured = os.environ.get("CLIPROXY_API_KEY_FILE", "").strip()
    candidates = [Path(configured).expanduser()] if configured else []
    candidates.append(_default_config_path().with_name("api-key"))
    for path in candidates:
        try:
            value = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if value:
            return value
    return ""


def _extract_json(value: str) -> Any:
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character not in "[{":
            continue
        try:
            parsed, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        return parsed
    raise CliproxyError("CLIProxyAPI 返回的内容不是有效 JSON")


def _string(value: Any, maximum: int = 180) -> str:
    text = str(value or "").strip()
    return text[:maximum]


def _year(value: Any) -> str:
    match = re.fullmatch(r"(?:19|20)\d{2}", str(value or "").strip())
    return match.group(0) if match else ""


class CliproxyAssistant:
    """Small OpenAI-compatible client for a local CLIProxyAPI instance."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
        web_search: bool = False,
    ) -> None:
        host, port = _read_config()
        configured_base = os.environ.get("CLIPROXY_BASE_URL", "").strip()
        self.base_url = (base_url or configured_base or f"http://{host}:{port}/v1").rstrip("/")
        self.api_key = api_key if api_key is not None else _read_api_key()
        self.model = model or os.environ.get("CLIPROXY_MODEL", "gpt-6-luna").strip()
        self.reasoning_effort = os.environ.get(
            "CLIPROXY_REASONING_EFFORT", "medium"
        ).strip()
        try:
            self.timeout = int(timeout or os.environ.get("CLIPROXY_TIMEOUT", "45"))
        except ValueError:
            self.timeout = 45
        self.web_search = web_search
        self.last_web_search_used = False
        self.last_web_search_error = ""
        self.last_douban_choice_confidence: float | None = None
        self.last_douban_candidates: list[Mapping[str, Any]] = []
        self.last_douban_selection: Mapping[str, Any] | None = None
        self.last_douban_diagnostics: list[str] = []
        self.last_douban_cookie_used = False

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def _open(self, request: urllib.request.Request, *, timeout: int | None = None) -> Mapping[str, Any]:
        try:
            parsed_url = urllib.parse.urlsplit(request.full_url)
            effective_timeout = timeout or self.timeout
            if parsed_url.hostname in {"127.0.0.1", "localhost", "::1"}:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                response_context = opener.open(request, timeout=effective_timeout)
            else:
                response_context = urllib.request.urlopen(request, timeout=effective_timeout)
            with response_context as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            # Do not include the response body: some upstreams echo request
            # metadata and it may contain credentials or signed URLs.
            raise CliproxyError(f"CLIProxyAPI HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise CliproxyError(f"CLIProxyAPI 请求失败：{exc}") from exc

    @staticmethod
    def _response_text(response_payload: Mapping[str, Any]) -> tuple[str, bool]:
        """Extract final JSON text and whether Responses emitted web search."""
        output_text = response_payload.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            output = response_payload.get("output")
            used_search = isinstance(output, list) and any(
                isinstance(item, Mapping)
                and str(item.get("type") or "").casefold().startswith("web_search")
                for item in output
            )
            return output_text, used_search

        output = response_payload.get("output")
        if isinstance(output, list):
            chunks: list[str] = []
            used_search = False
            for item in output:
                if not isinstance(item, Mapping):
                    continue
                item_type = str(item.get("type") or "").casefold()
                if item_type.startswith("web_search"):
                    used_search = True
                content = item.get("content")
                if isinstance(content, str):
                    chunks.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, Mapping):
                            continue
                        block_type = str(block.get("type") or "").casefold()
                        if block_type in {"output_text", "text"}:
                            text = block.get("text")
                            if isinstance(text, str):
                                chunks.append(text)
            if chunks:
                return "".join(chunks), used_search

        # A few OpenAI-compatible gateways translate Responses back into a
        # Chat Completions-shaped response. Keep this fallback for them.
        choices = response_payload.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
            content = message.get("content") if isinstance(message, Mapping) else None
            if isinstance(content, list):
                content = "".join(
                    str(item.get("text") or "")
                    for item in content
                    if isinstance(item, Mapping)
                )
            if isinstance(content, str) and content.strip():
                return content, False
        raise CliproxyError("CLIProxyAPI 返回中缺少可解析的 Responses 文本")

    def _complete_chat(self, system: str, user: str) -> Mapping[str, Any]:
        if not self.available:
            raise CliproxyError(
                "CLIProxyAPI 未配置 API key；请检查 CLIPROXY_API_KEY_FILE 或 CLIProxyAPI-runtime/api-key"
            )
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "media-title-rename/0.10",
            },
            method="POST",
        )
        response_payload = self._open(request)
        try:
            content = response_payload["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(
                    item.get("text", "") for item in content if isinstance(item, Mapping)
                )
            result = _extract_json(str(content))
        except (KeyError, IndexError, TypeError, CliproxyError) as exc:
            if isinstance(exc, CliproxyError):
                raise
            raise CliproxyError("CLIProxyAPI 返回中缺少 choices.message.content") from exc
        if not isinstance(result, Mapping):
            raise CliproxyError("CLIProxyAPI 返回的 JSON 不是对象")
        return result

    def _complete_responses(self, system: str, user: str) -> Mapping[str, Any]:
        """Ask CLIProxyAPI to use its hosted web search tool before answering."""
        payload: dict[str, Any] = {
            "model": self.model,
            "store": False,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": system}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": user}],
                },
            ],
            "tools": [{"type": "web_search"}],
            # ``--web-search`` is an explicit user choice.  Requiring a
            # hosted tool call prevents a confident model answer from being
            # mistaken for a web-verified answer.  Unsupported providers are
            # handled by the normal warning + Chat Completions fallback.
            "tool_choice": "required",
        }
        if self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        request = urllib.request.Request(
            f"{self.base_url}/responses",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "media-title-rename/0.10",
            },
            method="POST",
        )
        web_timeout = int(os.environ.get('CLIPROXY_WEB_TIMEOUT', str(self.timeout)))
        response_payload = self._open(request, timeout=web_timeout)
        content, used_search = self._response_text(response_payload)
        self.last_web_search_used = used_search
        result = _extract_json(content)
        if not isinstance(result, Mapping):
            raise CliproxyError("CLIProxyAPI 返回的 JSON 不是对象")
        return result

    def _complete_json(self, system: str, user: str) -> Mapping[str, Any]:
        if self.web_search:
            self.last_web_search_error = ""
            try:
                return self._complete_responses(system, user)
            except CliproxyError as exc:
                self.last_web_search_error = str(exc)
                self.last_web_search_used = False
                # Web search is an accuracy enhancement, not a reason to lose
                # the normal local identification flow when a model/provider
                # does not expose the hosted tool.
                print(f"警告：CLIProxyAPI web_search 不可用，回退普通 GPT：{exc}")
        return self._complete_chat(system, user)

    def identify(
        self,
        *,
        filename: str,
        parent_name: str,
        hints: Mapping[str, Any],
        media: Mapping[str, Any],
    ) -> AIIdentity:
        system = (
            "你是媒体发布资料识别助手。只返回 JSON 对象，不要 Markdown，不要解释。"
            "从发布文件名中提取作品主标题，用于 TMDB/豆瓣搜索；去除年份、季集、分辨率、来源、"
            "编码、音频、HDR、平台和发布组。不要猜测不存在的年份或季集。"
            "允许的 kind 只有 movie 或 tv；episode 没有把握时返回空字符串。"
            "search_aliases 给出至多三个可靠的原名/别名搜索词，例如数字标题的英文拼写；不要含URL或ID。"
        )
        user = json.dumps(
            {
                "filename": filename,
                "parent_name": parent_name,
                "rule_hints": dict(hints),
                "mediainfo_summary": {
                    key: media.get(key)
                    for key in (
                        "width",
                        "height",
                        "resolution",
                        "video_format",
                        "video_codec",
                        "audio_codec",
                        "audio_channels",
                        "audio_language",
                    )
                    if key in media
                },
                "return_schema": {
                    "search_title": "string",
                    "title": "string",
                    "year": "YYYY or empty",
                    "kind": "movie|tv|empty",
                    "episode": "Sxx or SxxExx or empty",
                    "source": "string or empty",
                    "group": "string or empty",
                    "edition": "string or empty",
                    "platform": "string or empty",
                    "confidence": "number 0..1",
                    "search_aliases": "array of up to 3 reliable title aliases",
                },
            },
            ensure_ascii=False,
        )
        result = self._complete_json(system, user)
        search_title = _string(result.get("search_title") or result.get("title"))
        if not search_title:
            raise CliproxyError("CLIProxyAPI 未返回可用的主标题")
        kind = _string(result.get("kind"), 10).casefold()
        if kind not in {"", "movie", "tv"}:
            kind = ""
        try:
            confidence = float(result.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        return AIIdentity(
            search_title=search_title,
            title=_string(result.get("title") or search_title),
            year=_year(result.get("year")),
            kind=kind,
            episode=_string(result.get("episode"), 30),
            source=_string(result.get("source"), 80),
            group=_string(result.get("group"), 80),
            edition=_string(result.get("edition"), 80),
            platform=_string(result.get("platform"), 40),
            confidence=max(0.0, min(confidence, 1.0)),
            search_aliases=tuple(dict.fromkeys(
                _string(value) for value in (result.get('search_aliases') or [])[:3]
                if isinstance(value, str) and not re.search(r'https?://|cookie|api.?key|token|password', value, re.I)
            )) if isinstance(result.get('search_aliases'), list) else (),
        )

    def choose_douban(self, query: str, candidates: list[Mapping[str, Any]]) -> int | None:
        if len(candidates) < 2:
            return None
        self.last_douban_choice_confidence = None
        self.last_douban_candidates = list(candidates[:10])
        self.last_douban_selection = None
        system = (
            "你是豆瓣条目消歧助手。只返回 JSON 对象。"
            "根据作品标题、原文名、年份、季数和搜索关键词选择最匹配的候选。"
            "不能因为候选看起来相似就猜测；没有可靠匹配时返回 candidate=0。"
        )
        user = json.dumps(
            {
                "query": query,
                "candidates": [
                    {
                        "index": index,
                        "id": item.get("id", ""),
                        "title": item.get("title", ""),
                        "original_title": item.get("original_title", ""),
                        "year": item.get("year", ""),
                        "season_number": item.get("season_number"),
                    }
                    for index, item in enumerate(candidates, start=1)
                ],
                "return_schema": {"candidate": "integer index or 0", "confidence": "number 0..1"},
            },
            ensure_ascii=False,
        )
        result = self._complete_json(system, user)
        try:
            index = int(result.get("candidate") or result.get("index") or 0)
        except (TypeError, ValueError):
            return None
        try:
            confidence = float(result.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(confidence, 1.0))
        self.last_douban_choice_confidence = confidence
        if not 1 <= index <= len(candidates) or confidence < 0.70:
            return None
        self.last_douban_selection = {
            "index": index,
            "id": str(candidates[index - 1].get("id") or ""),
            "year": str(candidates[index - 1].get("year") or ""),
            "confidence": confidence,
        }
        return index
