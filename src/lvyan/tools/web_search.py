"""受限的联网权威来源检索。

只在用户主动开启后调用，先对检索词脱敏并截断；仅保留官方域名的结果元数据。
不会抓取结果正文，也不会把联网页面当作已验证的法律法规文本。
"""

from __future__ import annotations

import html
import logging
import os
import re
from urllib.parse import urlsplit
from html.parser import HTMLParser

import httpx

from lvyan.schemas.web import OnlineSource, is_official_source_url
from lvyan.validators.privacy import redact_privacy

__all__ = ["search_official_web"]

_logger = logging.getLogger("lvyan.tools.web_search")
# 直接使用中国区 RSS 端点，避免 www.bing.com 的地区重定向导致空结果。
_BING_RSS_ENDPOINT = "https://cn.bing.com/search"
_MAX_QUERY_LENGTH = 180
_MAX_RESULTS = 5
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _web_search_enabled() -> bool:
    """工具层默认 **禁用** 外网检索（纵深防御，与偏好层默认关闭一致）。

    部署方需显式设置 ``WEB_SEARCH_ENABLED=true`` 才会发起外网请求。
    """
    return os.getenv("WEB_SEARCH_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _prepare_query(query: str) -> str:
    redacted = redact_privacy(str(query or "")).redacted_text
    return _WHITESPACE_RE.sub(" ", redacted).strip()[:_MAX_QUERY_LENGTH]


def _clean_text(value: str | None, limit: int) -> str:
    text = html.unescape(value or "")
    text = _TAG_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text[:limit]


def _source_name(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host == "flk.npc.gov.cn":
        return "国家法律法规数据库"
    if host == "npc.gov.cn" or host.endswith(".npc.gov.cn"):
        return "全国人大网站"
    if host == "spp.gov.cn" or host.endswith(".spp.gov.cn"):
        return "检察机关网站"
    if host == "court.gov.cn" or host.endswith(".court.gov.cn"):
        return "人民法院网站"
    return "政府网站"


class _BingResultParser(HTMLParser):
    """只提取 Bing 标准结果卡中的标题、链接和摘要。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._inside_h2 = False
        self._inside_title = False
        self._inside_snippet = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "li" and "b_algo" in (attributes.get("class") or "").split():
            self._current = {"title": "", "url": "", "snippet": ""}
            return
        if self._current is None:
            return
        if tag == "h2":
            self._inside_h2 = True
        elif tag == "a" and self._inside_h2 and not self._current["url"]:
            self._current["url"] = attributes.get("href") or ""
            self._inside_title = True
        elif tag == "p" and not self._inside_snippet:
            self._inside_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        if tag == "a":
            self._inside_title = False
        elif tag == "h2":
            self._inside_h2 = False
        elif tag == "p":
            self._inside_snippet = False
        elif tag == "li":
            if self._current["url"] and self._current["title"]:
                self.items.append(self._current)
            self._current = None
            self._inside_h2 = False
            self._inside_title = False
            self._inside_snippet = False

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return
        if self._inside_title:
            self._current["title"] += data
        elif self._inside_snippet:
            self._current["snippet"] += data


def _html_items(payload: bytes) -> list[dict[str, str]]:
    parser = _BingResultParser()
    try:
        parser.feed(payload.decode("utf-8", errors="replace"))
        parser.close()
    except (ValueError, UnicodeError):
        return []
    return parser.items


def search_official_web(
    query: str,
    *,
    top_k: int = _MAX_RESULTS,
    timeout_seconds: float = 5.0,
) -> list[OnlineSource]:
    """返回最多五条官方 HTTPS 搜索结果；网络失败时安全降级为空列表。"""
    if not _web_search_enabled():
        return []
    sanitized_query = _prepare_query(query)
    if not sanitized_query:
        return []
    limit = max(1, min(int(top_k), _MAX_RESULTS))
    try:
        with httpx.Client(
            timeout=max(1.0, min(float(timeout_seconds), 8.0)),
            follow_redirects=False,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; lvyan-legal-agent/1.0)",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        ) as client:
            response = client.get(_BING_RSS_ENDPOINT, params={"q": sanitized_query})
            response.raise_for_status()
    except httpx.HTTPError as exc:
        _logger.info("联网权威来源检索不可用：%s", type(exc).__name__)
        return []
    except (TypeError, ValueError) as exc:
        _logger.info("联网权威来源检索参数无效：%s", type(exc).__name__)
        return []

    sources: list[OnlineSource] = []
    seen_urls: set[str] = set()
    for item in _html_items(response.content):
        url = str(item["url"]).strip()
        title = _clean_text(item["title"], 500)
        if not title or not is_official_source_url(url) or url in seen_urls:
            continue
        try:
            sources.append(
                OnlineSource(
                    title=title,
                    url=url,
                    snippet=_clean_text(item["snippet"], 800),
                    source_name=_source_name(url),
                )
            )
        except ValueError:
            continue
        seen_urls.add(url)
        if len(sources) >= limit:
            break
    return sources
