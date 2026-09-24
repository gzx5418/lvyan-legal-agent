"""联网权威来源检索的安全边界测试。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lvyan.schemas.web import OnlineSource
from lvyan.tools import web_search


_HTML = """<!doctype html><ol>
  <li class="b_algo"><h2><a href="https://flk.npc.gov.cn/detail2.html">国家法律法规数据库</a></h2><p><b>官方法条</b></p></li>
  <li class="b_algo"><h2><a href="https://www.court.gov.cn/zixun-xiangqing.html">人民法院公告</a></h2><p>司法公开信息</p></li>
  <li class="b_algo"><h2><a href="https://example.invalid/phishing">不可信页面</a></h2><p>应被过滤</p></li>
  <li class="b_algo"><h2><a href="http://www.gov.cn/test">非 HTTPS 官方页</a></h2><p>应被过滤</p></li>
</ol>""".encode()


class _Response:
    content = _HTML
    status_code = 200
    headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None


class _Client:
    def __init__(self, *args, **kwargs) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def get(self, url: str, *, params: dict) -> _Response:
        self.calls.append((url, params))
        return _Response()


def test_search_official_web_filters_sources_and_redacts_query(monkeypatch: pytest.MonkeyPatch):
    # 工具层默认禁用外网检索（纵深防御），本测试关注过滤/脱敏逻辑，显式开启
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    clients: list[_Client] = []

    def make_client(*args, **kwargs):
        client = _Client(*args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(web_search.httpx, "Client", make_client)
    results = web_search.search_official_web("手机号13800138000的个人信息保护", top_k=5)

    assert [item.url for item in results] == [
        "https://flk.npc.gov.cn/detail2.html",
        "https://www.court.gov.cn/zixun-xiangqing.html",
    ]
    assert results[0].snippet == "官方法条"
    assert "13800138000" not in clients[0].calls[0][1]["q"]
    assert "手机号已脱敏" in clients[0].calls[0][1]["q"]


def test_search_official_web_can_be_disabled_by_deployment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "false")
    with patch.object(web_search.httpx, "Client") as client:
        assert web_search.search_official_web("个人信息保护") == []
    client.assert_not_called()


def test_online_source_rejects_non_official_or_non_https_urls():
    with pytest.raises(ValueError):
        OnlineSource(title="x", url="https://example.com/x")
    with pytest.raises(ValueError):
        OnlineSource(title="x", url="http://www.gov.cn/x")
