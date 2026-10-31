# SPDX-License-Identifier: Apache-2.0
"""F16 · 节点控制台只读接口（tasks / receipts / earnings / profile）客户端与数据层契约。

用例覆盖 lead 要求的五类：**有数据、空数据、`unavailable_reason` 降级、翻页到底、
401 重新登录提示**；外加金额口径（micro-USD → 至少 2 位、最多 6 位、去尾零）。

云端四端点尚未上线，因此这里用**契约桩**（注入 transport / fake cloud）；
接口落地后只换 base URL 真联调，桩继续留着当契约回归。
"""
from __future__ import annotations

import pytest

from djb_node.desktop import views as vw
from djb_node.desktop.datasource import LocalDataSource, ProviderError, UNAVAILABLE
from djb_node.desktop.nodecloud import MAX_PAGES, NodeCloudClient, micro_text


class FakeResponse:
    def __init__(self, status: int, payload=None, *, raw: str | None = None):
        self.status = status
        self._payload = payload
        self._raw = raw

    def json(self):
        if self._raw is not None:
            raise ValueError("not json")
        return self._payload


class FakeHttp:
    """记录请求（URL / 头），按队列回包。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, *, headers=None, json_body=None, timeout=None):
        self.calls.append((method, url, dict(headers or {})))
        if not self.responses:
            raise AssertionError("没有预置响应了")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def page(items, cursor=None, **extra):
    payload = {"items": items}
    if cursor:
        payload["page"] = {"next_cursor": cursor}
    payload.update(extra)
    return payload


# ─────────────────────────────────────────────────────────── 金额口径
@pytest.mark.parametrize("minor,text", [
    (None, None), (True, None), ("oops", None),
    (0, "0.00"), (10, "0.00001"), (100_000, "0.10"),
    (1_500_000, "1.50"), (1_234_567, "1.234567"), (123_456_789, "123.456789"),
    (-2_500_000, "-2.50"),
])
def test_micro_text_matches_web_convention(minor, text):
    assert micro_text(minor) == text


# ─────────────────────────────────────────────────────────── 翻页
def test_fetch_follows_next_cursor_to_the_end():
    client = NodeCloudClient("http://cloud.test", "ck", http=FakeHttp(
        FakeResponse(200, page([{"id": "t1"}, {"id": "t2"}], "c2")),
        FakeResponse(200, page([{"id": "t3"}])),
    ))
    result = client.fetch("tasks")
    assert [row["id"] for row in result["items"]] == ["t1", "t2", "t3"]
    assert result["pages"] == 2 and result["next_cursor"] is None
    assert result["truncated"] is False


def test_fetch_stops_at_page_cap_and_says_so():
    client = NodeCloudClient("http://cloud.test", "ck", max_pages=2, http=FakeHttp(
        FakeResponse(200, page([{"id": "a"}], "c2")),
        FakeResponse(200, page([{"id": "b"}], "c3")),
    ))
    result = client.fetch("receipts")
    assert [row["id"] for row in result["items"]] == ["a", "b"]
    assert result["truncated"] is True and result["next_cursor"] == "c3"
    assert result["pages"] == 2


def test_fetch_sends_cursor_and_limit_as_query():
    http = FakeHttp(FakeResponse(200, page([])))
    client = NodeCloudClient("http://cloud.test", "ck", page_limit=20, http=http)
    client.list_tasks(cursor="a b", limit=20)
    _method, url, _headers = http.calls[0]
    assert url.startswith("http://cloud.test/api/v1/node/tasks?")
    assert "limit=20" in url and "cursor=a%20b" in url


# ─────────────────────────────────────────────────────────── 失败口径
def test_401_asks_to_login_again():
    client = NodeCloudClient("http://cloud.test", "ck", http=FakeHttp(FakeResponse(401, {})))
    with pytest.raises(ProviderError) as info:
        client.fetch("tasks")
    assert "登录已过期" in info.value.reason
    assert "重新登录" in info.value.hint


@pytest.mark.parametrize("status,needle", [
    (404, "尚未上线"), (403, "无权"), (500, "暂时不可用"), (429, "被拒绝"),
])
def test_http_errors_become_readable_reasons(status, needle):
    client = NodeCloudClient("http://cloud.test", "ck", http=FakeHttp(FakeResponse(status, {})))
    with pytest.raises(ProviderError) as info:
        client.fetch("tasks")
    assert needle in info.value.reason


def test_network_exception_is_not_leaked_as_url():
    client = NodeCloudClient("http://cloud.test", "ck", http=FakeHttp(OSError("boom")))
    with pytest.raises(ProviderError) as info:
        client.fetch("tasks")
    assert "云端连不上（OSError）" == info.value.reason
    assert "cloud.test" not in info.value.reason


def test_non_json_response_is_reported():
    client = NodeCloudClient("http://cloud.test", "ck", http=FakeHttp(FakeResponse(200, raw="<html>")))
    with pytest.raises(ProviderError) as info:
        client.fetch("tasks")
    assert "不是 JSON" in info.value.reason


def test_unavailable_reason_marks_the_domain_unavailable():
    client = NodeCloudClient("http://cloud.test", "ck", http=FakeHttp(
        FakeResponse(200, {"items": [], "unavailable_reason": "metering 服务维护中"})))
    with pytest.raises(ProviderError) as info:
        client.fetch("receipts")
    assert "服务端已降级" in info.value.reason and "维护中" in info.value.reason


def test_client_sends_client_key_header_and_never_in_url():
    http = FakeHttp(FakeResponse(200, page([])))
    client = NodeCloudClient("http://cloud.test", "secret-key", http=http)
    client.fetch("profile")
    _method, url, headers = http.calls[0]
    assert "secret-key" not in url
    assert "secret-key" in str(headers.values())


# ─────────────────────────────────────────────────────────── from_home
def test_from_home_returns_none_without_credentials(home):
    assert NodeCloudClient.from_home(home) is None


def test_from_home_builds_client_when_logged_in(home, paths):
    from djb_node.config import ClientConfig, save_config
    from djb_node.secure import CredentialStore

    save_config(paths, ClientConfig(gateway_url="http://gw.test"))
    CredentialStore(paths).set_node("node-1", "ck-1")
    client = NodeCloudClient.from_home(home)
    assert client is not None
    assert client.base_url == "http://gw.test"


# ─────────────────────────────────────────────────────────── 数据层 + 视图
class FakeCloud:
    """最小契约桩：`fetch(kind)` + `earnings()` + `profile()`。"""

    def __init__(self, *, tasks=None, receipts=None, earnings=None, profile=None,
                 error: Exception | None = None):
        self._tasks = tasks or {"items": [], "next_cursor": None, "truncated": False}
        self._receipts = receipts or {"items": [], "next_cursor": None, "truncated": False}
        self._earnings = earnings or {}
        self._profile = profile or {}
        self._error = error

    def fetch(self, kind):
        if self._error is not None:
            raise self._error
        return self._tasks if kind == "tasks" else self._receipts

    def earnings(self):
        if self._error is not None:
            raise self._error
        return self._earnings

    def profile(self):
        if self._error is not None:
            raise self._error
        return self._profile


class FakeStore:
    """本地 store 桩：没有收据（真实场景里 daemon 起来才有数据）。"""

    def receipts(self, limit=200):
        return []

    def get_kv(self, key):
        return None


def make_source(cloud) -> LocalDataSource:
    return LocalDataSource(home="/tmp/djb-nodecloud-test", cloud=cloud,
                           runner=lambda argv: {}, facts=lambda: {},
                           which=lambda name: None, bench=lambda **kw: {},
                           store_factory=lambda path: FakeStore())


def text_of(key: str, source: LocalDataSource) -> str:
    return vw.build_view(key, source.snapshot()).plain_text()


def test_tasks_render_cloud_rows_with_origin():
    cloud = FakeCloud(tasks={"items": [{"id": "t-1", "state": "running", "tokens": 120,
                                      "receipt_id": "rc-1"}],
                            "next_cursor": None, "truncated": False})
    source = make_source(cloud)
    rows = source._tasks()["rows"]
    assert rows[0]["id"] == "t-1"
    text = text_of("tasks", source)
    assert "t-1" in text and "来源：cloud" in text


def test_tasks_page_cap_note_says_more_available():
    cloud = FakeCloud(tasks={"items": [{"id": "t-1", "state": "queued"}],
                            "next_cursor": "c9", "truncated": True})
    payload = make_source(cloud)._tasks()
    assert "还有更多" in payload["note"]
    assert payload["next_cursor"] == "c9"


def test_empty_cloud_list_is_honest_empty_not_unavailable():
    source = make_source(FakeCloud())
    receipts = source._receipts()
    assert receipts["rows"] == [] and receipts["note"] == "还没有收据"
    text = text_of("receipts", source)
    assert "还没有收据" in text
    assert "暂不可用" not in text                     # 真空数据 vs 取不到，要分清


def test_unavailable_reason_degrades_view_but_keeps_page():
    cloud = FakeCloud(error=ProviderError("服务端已降级：metering 维护中"))
    source = make_source(cloud)
    text = text_of("tasks", source)
    assert UNAVAILABLE in text and "metering 维护中" in text
    assert "任务队列" in text                          # 页面不隐藏


def test_401_shows_relogin_hint():
    cloud = FakeCloud(error=ProviderError("登录已过期（401）", hint="重新登录节点后重试（djb auth --pair）"))
    source = make_source(cloud)
    text = text_of("account", source)
    assert "登录已过期" in text


def test_earnings_use_micro_usd_text():
    cloud = FakeCloud(earnings={"currency": "USDT", "settled": micro_text(1_500_000),
                                "pending": micro_text(1_234_567), "today": micro_text(250_000),
                                "balance": micro_text(0), "settled_minor": 1_500_000,
                                "pending_minor": 1_234_567, "payouts": []})
    source = make_source(cloud)
    text = text_of("earnings", source)
    assert "0.25 USDT" in text                        # 今日（带币种）
    assert "[已结算] 1.50" in text and "[待结算] 1.234567" in text
    assert "[余额（额度）] 0.00" in text


def test_account_uses_profile_fields():
    cloud = FakeCloud(profile={"email": "node@example.test", "role": "node",
                               "twofa_enabled": True})
    text = text_of("account", make_source(cloud))
    assert "node@example.test" in text and "node" in text and "已开启" in text


def test_no_cloud_means_unavailable_not_zero():
    source = make_source(None)
    text = text_of("tasks", source)
    assert UNAVAILABLE in text and "未登录节点" in text
    assert "0 条" not in text
