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
        FakeResponse(200, page([{"task_id": "t1"}, {"task_id": "t2"}], "c2")),
        FakeResponse(200, page([{"task_id": "t3"}])),
    ))
    result = client.fetch("tasks")
    assert [row["id"] for row in result["items"]] == ["t1", "t2", "t3"]
    assert result["pages"] == 2 and result["next_cursor"] is None
    assert result["truncated"] is False


def test_fetch_stops_at_page_cap_and_says_so():
    client = NodeCloudClient("http://cloud.test", "ck", max_pages=2, http=FakeHttp(
        FakeResponse(200, page([{"receipt_id": "a"}], "c2")),
        FakeResponse(200, page([{"receipt_id": "b"}], "c3")),
    ))
    result = client.fetch("receipts")
    assert [row["receipt_id"] for row in result["items"]] == ["a", "b"]
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
    (404, "接口未上线或节点不存在"), (403, "无权"), (500, "暂时不可用"), (429, "被拒绝"),
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


def test_earnings_without_any_data_is_unavailable_with_reason():
    client = NodeCloudClient("http://cloud.test", "ck", http=FakeHttp(
        FakeResponse(200, {"settled_minor": None, "pending_minor": None, "rows": [],
                           "unavailable_reason": "no_usage_for_node"})))
    with pytest.raises(ProviderError) as info:
        client.earnings()
    assert "还没有计费记录" in info.value.reason


def test_earnings_with_data_keeps_reason_as_note():
    """有数据但结算排期没配置：照常展示金额，原因降级成 note（不整域判死）。"""
    client = NodeCloudClient("http://cloud.test", "ck", http=FakeHttp(
        FakeResponse(200, {"settled_minor": 1_500_000, "pending_minor": 0, "rows": [],
                           "next_settlement_at": None,
                           "unavailable_reason": "settlement_schedule_not_configured"})))
    data = client.earnings()
    assert data["settled"] == "1.50" and data["pending"] == "0.00"
    assert "结算排期尚未配置" in data["note"]


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
    assert "[余额（可用）]" in text                       # 余额卡：取不到就说暂不可用，不写 0
    assert "余额暂不可用" in text and "0.00" not in text.split("[余额（可用）]")[1].split("\n")[0]


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


# ─────────────────────────────────────────────────────────── 行归一化（对齐 node_console 真实字段）
def test_task_rows_get_tokens_elapsed_and_state_mapping():
    client = NodeCloudClient("http://cloud.test", "ck", http=FakeHttp(FakeResponse(200, page([
        {"task_id": "t-1", "request_id": "req-1", "state": "recorded", "inflight": True,
         "tokens_in": 30, "tokens_out": 12, "started_at": "2026-11-01T10:00:00Z",
         "finished_at": "2026-11-01T10:00:09Z", "amount_micro": 1_250_000},
        {"task_id": None, "request_id": "req-2", "state": "settled", "inflight": False,
         "tokens_in": 5, "tokens_out": 0, "started_at": "2026-11-01T10:00:00Z",
         "finished_at": "2026-11-01T10:01:00Z", "amount_micro": 500_000},
    ]))))
    rows = client.fetch("tasks")["items"]
    # 云端原词保留（recorded/settled），在途与否另由 inflight 表达；两者不再互相顶替
    assert rows[0]["id"] == "t-1" and rows[0]["state"] == "recorded"
    assert rows[0]["state_text"] == "已记录" and rows[0]["inflight"] is True
    assert rows[0]["tokens"] == 42 and rows[0]["elapsed_s"] == 9
    assert rows[0]["amount"] == "1.25"
    assert rows[1]["id"] == "req-2" and rows[1]["state"] == "settled"
    assert rows[1]["state_text"] == "已结算" and rows[1]["inflight"] is False
    assert rows[1]["elapsed_s"] == 60 and rows[1]["amount"] == "0.50"


def test_receipt_rows_keep_state_words_and_do_not_claim_verified():
    client = NodeCloudClient("http://cloud.test", "ck", http=FakeHttp(FakeResponse(200, page([
        {"receipt_id": "r-1", "state": "accepted", "tokens": 40, "revenue_minor": 2_000_000,
         "issued_at": "2026-11-01T10:00:00Z", "hash": "deadbeef…"},
        {"receipt_id": "r-2", "state": "rejected", "tokens": None, "revenue_minor": None,
         "issued_at": "2026-11-01T10:05:00Z", "reject_reason": "bad signature"},
    ]))))
    rows = client.fetch("receipts")["items"]
    # accepted 是「云端受理」，不是「本机校验通过」——原词保留，中文另给
    assert rows[0]["state"] == "accepted" and rows[0]["state_text"] == "已受理"
    assert "status" not in rows[0] or rows[0].get("status") != "verified"
    assert rows[0]["created_at"] == "2026-11-01T10:00:00Z" and rows[0]["amount"] == "2.00"
    assert rows[1]["state"] == "rejected" and rows[1]["amount"] is None


def test_receipt_view_shows_cloud_state_and_local_check_separately():
    """真数据口径：云端 accepted/rejected/expired 原样说，本机有没有副本另说。"""
    cloud = FakeCloud(receipts={"items": [
        {"receipt_id": "r-1", "state": "accepted", "state_text": "已受理",
         "created_at": "2026-11-01T10:00:00Z", "tokens": 40, "local_check": "no_local"},
        {"receipt_id": "r-2", "state": "rejected", "state_text": "已拒绝",
         "rejected_code": 46001, "reject_reason": "provider_signature_mismatch",
         "created_at": "2026-11-01T10:01:00Z", "tokens": None, "local_check": "no_local"},
    ], "next_cursor": None, "truncated": False})
    text = text_of("receipts", make_source(cloud))
    assert "✓ 已受理" in text
    assert "已拒绝（46001 收据签名不匹配" in text
    assert "本机无副本" in text
    assert "已验证" not in text.replace("本机已验证", "").replace("本机无副本", "")


def test_profile_payload_maps_node_identity():
    client = NodeCloudClient("http://cloud.test", "ck", http=FakeHttp(FakeResponse(200, {
        "node_id": "node-7c31", "state": "active", "region": "cn-east", "platform": "linux",
        "upstream_kind": "A1", "capabilities": ["A1:mock-small"], "bound": True,
        "last_seen_at": "2026-11-01T10:00:00Z"})))
    profile = client.profile()
    assert profile["node_id"] == "node-7c31" and profile["bound"] is True
    assert profile["capabilities"] == ["A1:mock-small"]
    assert profile["email"] is None                  # 节点接口没有邮箱，不编


def test_one_network_failure_does_not_cost_four_timeouts():
    """一轮刷新里网络挂了一次，剩下的域直接沿用同一原因（不各等一次 6s 超时）。"""

    class DeadCloud:
        def __init__(self):
            self.calls = 0

        def fetch(self, kind):
            self.calls += 1
            raise ProviderError("云端连不上（OSError）")

        def earnings(self):
            self.calls += 1
            raise ProviderError("云端连不上（OSError）")

        def profile(self):
            self.calls += 1
            raise ProviderError("云端连不上（OSError）")

    cloud = DeadCloud()
    snapshot = make_source(cloud).snapshot()
    assert cloud.calls == 1                      # 只有第一个域真的去撞网络
    for name in ("tasks", "receipts", "earnings"):
        assert not snapshot.ok(name)
        assert "云端连不上" in str(snapshot.sections[name].reason)
    # 账户域即使云端取不到也还能显示本机凭据，但必须把原因写进 note
    account = snapshot.sections["account"]
    assert not snapshot.ok("account") or "云端连不上" in str(account.get("note", ""))


# ─────────────────────────────────────────── 余额（/api/v1/node/balance，节点钥可读，三态）
def test_node_balance_with_amount(monkeypatch):
    source = make_source(FakeCloud())
    source._cloud = type("Cloud", (), {
        "fetch": lambda self, kind: {"items": [], "next_cursor": None, "truncated": False},
        "earnings": lambda self: {"settled": "1.50", "pending": None, "payouts": [],
                                  "currency": "USDT", "note": ""},
        "node_balance": lambda self: {"balance": "0.00", "balance_minor": 0,
                                      "note": "no_balance_record：还没有充值凭证"},
    })()
    section = source._earnings()
    assert section["balance"] == "0.00"                  # 账本说是 0 就是 0
    assert "还没有充值凭证" in section["balance_note"]


def test_node_balance_unbound_is_unavailable_not_zero():
    class Unbound:
        def fetch(self, kind):
            return {"items": [], "next_cursor": None, "truncated": False}

        def earnings(self):
            return {"settled": "1.50", "pending": None, "payouts": [], "currency": "USDT",
                    "note": ""}

        def node_balance(self):
            raise ProviderError("节点未绑定账户，可用余额暂不可用")

    section = make_source(Unbound())._earnings()
    assert section["balance"] is None
    assert "未绑定" in section["balance_note"]


def test_node_balance_failure_is_unavailable_not_zero():
    class Boom:
        def fetch(self, kind):
            return {"items": [], "next_cursor": None, "truncated": False}

        def earnings(self):
            return {"settled": "1.50", "pending": None, "payouts": [], "currency": "USDT",
                    "note": ""}

        def node_balance(self):
            raise ProviderError("云端暂时不可用（502）")

    section = make_source(Boom())._earnings()
    assert section["balance"] is None
    assert "502" in section["balance_note"]


# ───────────────────────────────────────────── 第二页被拒：拿到的照显，但要标 truncated
def test_second_page_rejection_keeps_first_page_and_flags_truncated():
    """真联调：receipts 的 next_cursor 回传云端 400，客户端不能把已拿到的 2 条也丢掉。"""
    answers = [
        {"data": [{"receipt_id": "r1", "state": "accepted"}],
         "page": {"cursor": "", "has_more": True, "next_cursor": "broken"}},
    ]

    class FlakyHttp:
        def __init__(self):
            self.calls = 0

        def request(self, method, url, headers=None, timeout=None):   # noqa: ARG002
            self.calls += 1
            if self.calls == 1:
                return FakeResponse(200, answers[0])
            return FakeResponse(400, {"error": {"code": 40001, "message": "cursor 非法"}})

    client = NodeCloudClient("http://cloud.test", "ck", http=FlakyHttp())
    result = client.fetch("receipts")
    assert [row["receipt_id"] for row in result["items"]] == ["r1"]
    assert result["truncated"] is True
    assert "400" in result["page_error"] and result["pages"] == 1


def test_first_page_failure_still_fails_the_whole_domain():
    class Dead:
        def request(self, method, url, headers=None, timeout=None):   # noqa: ARG002
            return FakeResponse(400, {"error": {"code": 40001}})

    with pytest.raises(ProviderError):
        NodeCloudClient("http://cloud.test", "ck", http=Dead()).fetch("receipts")


def test_truncated_note_names_the_cursor_rejection_not_a_generic_more():
    section = {"rows": [{"receipt_id": "r1"}], "truncated": True,
               "next_cursor": "broken", "page_error": "请求被拒绝（400）"}
    from djb_node.desktop.datasource import _more_note

    note = _more_note(section)
    assert "云端拒绝了翻页游标" in note and "已显示前 1 条" in note


def test_receipt_row_reads_issued_at_and_prefers_revenue_micro():
    """列表行时间字段是 ``issued_at``；金额优先 ``revenue_micro``，缺了回落 ``revenue_minor``。"""
    client = NodeCloudClient("http://cloud.test", "ck", http=FakeHttp(FakeResponse(200, page([
        {"receipt_id": "r-1", "state": "accepted", "issued_at": "2026-11-01T10:00:00Z",
         "revenue_micro": 1_250_000, "revenue_minor": 1_250_000, "tokens": 10},
        {"receipt_id": "r-2", "state": "accepted", "issued_at": "2026-11-01T10:01:00Z",
         "revenue_minor": 500_000, "tokens": 5},                        # 只有 deprecated 字段
        {"receipt_id": "r-3", "state": "accepted", "received_at": "2026-11-01T10:02:00Z",
         "tokens": 1},                                                  # 详情端点才有的名字
    ]))))
    rows = client.fetch("receipts")["items"]
    assert rows[0]["created_at"] == "2026-11-01T10:00:00Z" and rows[0]["amount"] == "1.25"
    assert rows[1]["amount"] == "0.50"                 # 回落 revenue_minor
    assert rows[2]["created_at"] == "2026-11-01T10:02:00Z"   # 兼容详情端点的 received_at
