# SPDX-License-Identifier: Apache-2.0
"""W5-CLIENT-036 · USDT 收款地址「客户端直接提交云端」（owner 2026-11-13 方案 B）。

四层各测一层，**不联网**（除了最后一个真 socket 用例，验证出站契约真的对）：

1. ``desktop/payout.py``：状态口径（未提交 / 待审核 / 已生效 / 已拒绝 / 已撤销 / 冷却中
   / 读不到），**任何情况下都不把「不知道」说成「已生效」**；
2. ``desktop/nodecloud.py``：GET/POST 的出站形状（路径、``X-Client-Key``、幂等头、body）
   与错误映射（401/403/40913/40919/42210/42211/40001/5xx）；
3. ``desktop/datasource.py``：``access.payout`` 投影（读不到 vs 尚未提交要分开）；
4. ``desktop/controller.py``：点「提交到云端」时**先本地判**（TRC20 不发请求）、
   草稿优先、失败必带「地址未变更」；提交本身跑在 worker 线程里（主线程不阻塞）。
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

from djb_node.desktop import payout as po
from djb_node.desktop.controller import DesktopController
from djb_node.desktop.datasource import FakeDataSource, LocalDataSource, ProviderError
from djb_node.desktop.nodecloud import NodeCloudClient
from djb_node.httpclient import HttpResponse
from djb_node.protocol import IDEMPOTENCY_HEADER, NODE_KEY_HEADER

EVM = "0x3f7c9a1b2d3e4f5a6b7c8d9e0f1a2b3c4d5e9a2c"
TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


# ─────────────────────────────────────────────────────────── 1) 纯口径（payout.py）
def test_submit_blocker_only_allows_polygon_evm():
    """链闸：TRC20 在本地就被拦下（云端只收 Polygon），空值给下一步。"""
    assert po.submit_blocker(EVM) == ""
    assert po.submit_blocker(EVM.upper().replace("0X", "0x")) == ""
    message = po.submit_blocker(TRC20)
    assert "TRC20 暂不支持" in message and "Polygon" in message
    assert "0x" in message
    assert "先填" in po.submit_blocker("")
    assert "先填" in po.submit_blocker("   ")


def test_short_state_covers_every_cloud_state():
    assert po.short_state({}) == po.UNSUBMITTED                    # data: null / {}
    assert po.short_state({"address": EVM}) == po.UNREADABLE        # 有地址没状态：不编
    assert po.short_state({"address": EVM, "status": "pending"}) == "待审核"
    assert po.short_state({"address": EVM, "status": "whitelisted"}) == "已生效"
    assert po.short_state({"address": EVM, "status": "rejected"}) == "已拒绝"
    assert po.short_state({"address": EVM, "status": "revoked"}) == "已撤销"
    # 没登记的状态词原样带出，不翻译成猜得出来的意思
    assert "something_new" in po.short_state({"address": EVM, "status": "something_new"})
    assert po.short_state({"address": EVM, "status": "pending"},
                          now=po._moment("2026-11-14T00:00:00Z")) == "待审核"


def test_short_state_prefers_cooldown_and_never_says_active():
    """冷却中优先（它解释「为什么改不了」），且**读不到绝不能显示成已生效**。"""
    row = {"address": EVM, "status": "whitelisted",
           "cooldown_until": "2026-11-14T00:00:00Z"}
    assert po.short_state(row, now=po._moment("2026-11-13T00:00:00Z")) == "冷却中"
    assert po.short_state(row, now=po._moment("2026-11-15T00:00:00Z")) == "已生效"
    assert po.short_state(None) == po.UNREADABLE
    assert "已生效" not in po.short_state(None)
    assert "已生效" not in po.summarize(None)
    # 服务端漏时区时按 UTC 读（契约口径），不要因为解析不了就当成「没有冷却」
    assert po.cooldown_active({"cooldown_until": "2026-11-14T00:00:00"},
                              now=po._moment("2026-11-13T00:00:00Z")) is True


def test_summarize_separates_unsubmitted_from_unreadable():
    assert "尚未提交" in po.summarize({})
    assert po.UNREADABLE not in po.summarize({})
    assert "尚未提交" not in po.summarize(None)
    assert po.summarize(None).startswith("云端：")
    # 地址只上掩码，不把完整地址印在界面上
    text = po.summarize({"address": EVM, "status": "whitelisted"})
    assert EVM not in text and "…" in text and "已生效" in text
    assert "24" not in po.summarize({"address": EVM, "status": "pending"})
    cold = po.summarize({"address": EVM, "status": "pending",
                         "cooldown_until": "2026-11-14T00:00:00Z"},
                        now=po._moment("2026-11-13T00:00:00Z"))
    assert "冷却中" in cold and "2026-11-14T00:00:00Z" in cold


def test_submit_message_never_promises_whitelist():
    assert "已生效" in po.submit_message({"status": "whitelisted"})
    assert "待审核" in po.submit_message({"status": "pending"})
    assert "已生效" not in po.submit_message({"status": "pending"})
    assert "已生效" not in po.submit_message({})       # 云端没给状态：不替它判断
    assert "已生效" not in po.submit_message(None)
    assert "已拒绝" in po.submit_message({"status": "rejected"})


# ───────────────────────────────────────────────── 2) 出站形状 + 错误映射（nodecloud）
class FakeHttp:
    """记录每次出站，按脚本返回 ``HttpResponse``（不发真网络）。"""

    def __init__(self, *responses: HttpResponse):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, *, headers=None, json_body=None, body=None,
                timeout=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "json_body": json_body, "timeout": timeout})
        if not self.responses:
            raise AssertionError("多打了一次请求：" + method + " " + url)
        return self.responses.pop(0)


def _response(status: int, payload: dict | None = None) -> HttpResponse:
    return HttpResponse(status=status,
                        body=b"" if payload is None else json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"})


def _client(http: FakeHttp) -> NodeCloudClient:
    return NodeCloudClient("https://gw.example.invalid", "node-secret-value", http=http)


def test_get_payout_address_sends_node_key_and_normalizes_null_to_empty():
    http = FakeHttp(_response(200, {"data": None, "request_id": "r1"}))
    assert _client(http).payout_address() == {}
    call = http.calls[0]
    assert call["method"] == "GET"
    assert call["url"].endswith("/api/v1/node/payout-address")
    assert call["headers"][NODE_KEY_HEADER] == "node-secret-value"
    assert call["json_body"] is None


def test_get_payout_address_returns_row_verbatim():
    row = {"address": EVM, "status": "pending", "cooldown_until": None,
           "updated_at": "2026-11-13T01:02:03Z", "label": "默认"}
    http = FakeHttp(_response(200, {"data": row, "request_id": "r1"}))
    assert _client(http).payout_address() == row


def test_submit_payout_address_posts_body_and_idempotency_key():
    http = FakeHttp(_response(200, {"data": {"address": EVM, "status": "pending"},
                                    "request_id": "r2"}))
    data = _client(http).submit_payout_address(EVM, "默认收款")
    assert data["status"] == "pending"
    call = http.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/api/v1/node/payout-address")
    assert call["json_body"] == {"address": EVM, "label": "默认收款"}
    assert call["headers"][NODE_KEY_HEADER] == "node-secret-value"
    key = call["headers"][IDEMPOTENCY_HEADER]
    assert 16 <= len(key) <= 128                # cloud 侧幂等头的长度约束


def test_submit_payout_address_omits_empty_label():
    http = FakeHttp(_response(200, {"data": {"address": EVM, "status": "pending"}}))
    _client(http).submit_payout_address(EVM, "")
    assert http.calls[0]["json_body"] == {"address": EVM}


@pytest.mark.parametrize("status,payload,needles", [
    (401, {"error": {"code": 40101, "message": "node key invalid"}},
     ["401", "绑定"]),
    (403, {"error": {"code": 40305, "message": "node not bound",
                     "details": {"reason": "node_not_bound"}}},
     ["403", "绑定"]),
    (409, {"error": {"code": 40913, "message": "当前仅支持 Polygon（ERC20）链上 USDT 收款地址"}},
     ["Polygon", "TRC20", "0x"]),
    (409, {"error": {"code": 40919, "message": "cooldown",
                     "details": {"reason": "address_cooldown"}}},
     ["24 小时", "冷却"]),
    (422, {"error": {"code": 42211, "message": "risk"}}, ["风险"]),
    (422, {"error": {"code": 42210, "message": "admission"}}, ["准入"]),
    (400, {"error": {"code": 40001, "field": "address"}}, ["格式", "0x"]),
    (500, None, ["云端暂时不可用"]),
])
def test_submit_error_mapping_is_actionable(status, payload, needles):
    http = FakeHttp(_response(status, payload))
    with pytest.raises(ProviderError) as caught:
        _client(http).submit_payout_address(EVM)
    text = caught.value.reason + "｜" + (caught.value.hint or "")
    for needle in needles:
        assert needle in text, (status, needle, text)


def test_submit_feature_disabled_is_not_a_retryable_bug():
    """404/40401 ``feature_disabled``：说「服务未开放」，不引导重试、不降级成本地保存。"""
    http = FakeHttp(_response(404, {"error": {"code": 40401,
                                             "reason": "feature_disabled",
                                             "message": "endpoint disabled"}}))
    with pytest.raises(ProviderError) as caught:
        _client(http).submit_payout_address(EVM)
    text = caught.value.reason + "｜" + (caught.value.hint or "")
    assert "服务未开放" in caught.value.reason
    assert "未变更" in text
    assert "重试" not in caught.value.hint.replace("不用反复重试", "")


def test_get_feature_disabled_is_marked_not_just_unreadable(tmp_path):
    """GET 也被关时，页面显示「服务未开放」而不是「暂时读不到」（含义不同）。"""
    cloud = CloudStub(error=ProviderError("服务未开放（404）"))
    payout = _source(tmp_path, cloud).snapshot().sections["access"]["payout"]
    assert payout["disabled"] is True
    assert payout["can_submit"] is False


def test_generic_404_without_envelope_still_says_not_open():
    http = FakeHttp(_response(404, None))
    with pytest.raises(ProviderError) as caught:
        _client(http).payout_address()
    assert "服务未开放" in caught.value.reason


def test_idempotency_conflict_is_not_reported_as_cooldown():
    """40919 有歧义：``reason`` 必须优先，别把幂等冲突说成「等 24 小时」。"""
    http = FakeHttp(_response(409, {"error": {"code": 40919,
                                             "reason": "idempotency_conflict"}}))
    with pytest.raises(ProviderError) as caught:
        _client(http).submit_payout_address(EVM)
    text = caught.value.reason + "｜" + (caught.value.hint or "")
    assert "幂等" in text
    assert "24 小时" not in text and "冷却" not in text


def test_cooldown_reason_wins_when_present():
    http = FakeHttp(_response(409, {"error": {"code": 40919,
                                             "reason": "address_cooldown"}}))
    with pytest.raises(ProviderError) as caught:
        _client(http).submit_payout_address(EVM)
    assert "冷却" in caught.value.reason
    assert "24 小时" in caught.value.reason


def test_bad_request_body_field_blames_the_client():
    """``field == "body"`` = 客户端多传了字段（本端只发 address/label）：要说「是 bug」。"""
    http = FakeHttp(_response(400, {"error": {"code": 40001, "field": "body"}}))
    with pytest.raises(ProviderError) as caught:
        _client(http).submit_payout_address(EVM)
    text = caught.value.reason + "｜" + (caught.value.hint or "")
    assert "400" in caught.value.reason
    assert "报给平台" in text or "bug" in text.lower()


def test_bad_request_address_field_points_at_the_address():
    http = FakeHttp(_response(400, {"error": {"code": 40001, "field": "address"}}))
    with pytest.raises(ProviderError) as caught:
        _client(http).submit_payout_address(EVM)
    assert "格式" in caught.value.reason
    assert "0x" in (caught.value.hint or "")


def test_each_submit_uses_a_fresh_idempotency_key():
    """同一键换地址会被云端判 409 幂等冲突 → 每次提交都必须换新键（本端用 uuid4）。"""
    http = FakeHttp(_response(200, {"data": {"status": "pending"}}),
                    _response(200, {"data": {"status": "pending"}}))
    client = _client(http)
    client.submit_payout_address(EVM)
    client.submit_payout_address("0x" + "ab" * 20)
    first = http.calls[0]["headers"][IDEMPOTENCY_HEADER]
    second = http.calls[1]["headers"][IDEMPOTENCY_HEADER]
    assert first != second


def test_submit_error_without_envelope_still_says_status():
    http = FakeHttp(_response(451, None))
    with pytest.raises(ProviderError) as caught:
        _client(http).submit_payout_address(EVM)
    assert "451" in caught.value.reason


def test_network_failure_maps_to_readable_reason():
    class Boom(FakeHttp):
        def request(self, *args, **kwargs):
            raise OSError("connection reset")

    with pytest.raises(ProviderError) as caught:
        _client(Boom()).submit_payout_address(EVM)
    assert "云端连不上" in caught.value.reason


# ─────────────────────────────────────────────────────── 3) 数据层投影（datasource）
class CloudStub:
    """最小云端桩：只实现收款地址两个方法，其余域由 LocalDataSource 当「未接线」处理。"""

    def __init__(self, row=None, error: Exception | None = None):
        self.row = row
        self.error = error
        self.submitted: list[tuple[str, str]] = []

    def payout_address(self):
        if self.error is not None:
            raise self.error
        return dict(self.row or {})

    def submit_payout_address(self, address, label=""):
        self.submitted.append((address, label))
        return {"address": address, "status": "pending", "cooldown_until": None}


def _source(tmp_path, cloud) -> LocalDataSource:
    return LocalDataSource(str(tmp_path), cloud=cloud)


def test_access_section_carries_payout_status(tmp_path):
    source = _source(tmp_path, CloudStub({"address": EVM, "status": "whitelisted"}))
    section = source.snapshot().sections["access"]
    payout = section["payout"]
    assert payout["ok"] is True
    assert payout["data"]["status"] == "whitelisted"
    assert payout["can_submit"] is True            # 有节点凭据（桩里由 cloud 直接给）


def test_access_section_marks_empty_cloud_row_as_unsubmitted(tmp_path):
    source = _source(tmp_path, CloudStub({}))
    payout = source.snapshot().sections["access"]["payout"]
    assert payout["ok"] is True and payout["data"] == {}


def test_access_section_reports_unreadable_cloud_without_faking_status(tmp_path):
    source = _source(tmp_path, CloudStub(error=ProviderError("云端连不上（OSError）")))
    payout = source.snapshot().sections["access"]["payout"]
    assert payout["ok"] is False
    assert "云端连不上" in payout["reason"]


def test_no_cloud_keeps_submit_disabled(tmp_path):
    """没有云端客户端（没配对/没网关）→ can_submit False，按钮置灰并说下一步。"""
    source = LocalDataSource(str(tmp_path), cloud=None)
    payout = source.snapshot().sections["access"]["payout"]
    assert payout["ok"] is False
    assert payout["can_submit"] is False


def test_datasource_submit_goes_through_cloud(tmp_path):
    cloud = CloudStub()
    source = _source(tmp_path, cloud)
    data = source.submit_payout_address(EVM, "默认")
    assert cloud.submitted == [(EVM, "默认")]
    assert data["status"] == "pending"


def test_datasource_submit_without_cloud_raises_readable(tmp_path):
    source = LocalDataSource(str(tmp_path), cloud=None)
    with pytest.raises(ProviderError):
        source.submit_payout_address(EVM)


# ──────────────────────────────────────────────────────── 4) 控制器（先本地判，再出站）
def _controller(tmp_path, cloud, drafts=None) -> DesktopController:
    return DesktopController(source=_source(tmp_path, cloud), drafts=dict(drafts or {}))


def test_action_blocks_trc20_without_touching_the_network(tmp_path):
    """owner 指令：TRC20 直接在本地拦，**不发请求**等云端 409。"""
    cloud = CloudStub()
    controller = _controller(tmp_path, cloud, {"usdt_address": TRC20})
    result = controller.action("access.submit_payout")
    assert result.ok is False
    assert "TRC20 暂不支持" in result.message
    assert "未发送到云端" in result.message
    assert result.effect != "payout_submit"
    assert cloud.submitted == []


def test_action_blocks_bad_format_before_network(tmp_path):
    cloud = CloudStub()
    controller = _controller(tmp_path, cloud, {"usdt_address": "0x1234"})
    result = controller.action("access.submit_payout")
    assert result.ok is False
    assert "格式不对" in result.message
    assert cloud.submitted == []


def test_action_uses_draft_then_saved_config(tmp_path):
    cloud = CloudStub()
    controller = _controller(tmp_path, cloud, {"usdt_address": EVM})
    result = controller.action("access.submit_payout")
    assert result.ok is True and result.effect == "payout_submit"
    assert result.payload == EVM                  # 渲染层据此派发 worker
    assert cloud.submitted == []                  # 动作本身**不**发请求（非阻塞）

    saved = _controller(tmp_path, cloud)
    assert saved.payout_address() == ""           # 没草稿也没配置 → 空
    assert "先填" in saved.action("access.submit_payout").message


def test_submit_reports_failure_without_changing_address(tmp_path):
    class Failing(CloudStub):
        def submit_payout_address(self, address, label=""):
            raise ProviderError("云端暂时不可用（503）", hint="稍后重试")

    controller = _controller(tmp_path, Failing())
    result = controller.submit_payout(EVM)
    assert result.ok is False
    assert "地址未变更" in result.message
    assert "稍后重试" in result.message


def test_submit_success_message_never_claims_effective(tmp_path):
    controller = _controller(tmp_path, CloudStub())
    result = controller.submit_payout(EVM)
    assert result.ok is True
    assert "待审核" in result.message


def test_submit_rechecks_chain_gate_defensively(tmp_path):
    """绕过动作直接调 submit 也要被拦（防御性重判）。"""
    cloud = CloudStub()
    controller = _controller(tmp_path, cloud)
    result = controller.submit_payout(TRC20)
    assert result.ok is False and "TRC20 暂不支持" in result.message
    assert cloud.submitted == []


# ────────────────────────────────────────── 5) 真 socket：出站契约（路径/头/body 真的对）
class PayoutHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list[dict] = []
    reply = {"data": {"address": EVM, "status": "pending", "cooldown_until": None},
             "request_id": "req-1"}

    def log_message(self, *args):
        pass

    def _record(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        PayoutHandler.seen.append({
            "method": self.command, "path": urlparse(self.path).path,
            "key": self.headers.get(NODE_KEY_HEADER),
            "idem": self.headers.get(IDEMPOTENCY_HEADER),
            "body": json.loads(raw.decode("utf-8")) if raw else None})

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                          # noqa: N802 - stdlib 约定
        self._record()
        self._send(200, {"data": None, "request_id": "req-0"})

    def do_POST(self):                         # noqa: N802 - stdlib 约定
        self._record()
        self._send(200, PayoutHandler.reply)


@pytest.fixture()
def payout_server():
    PayoutHandler.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), PayoutHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_socket_contract(payout_server):
    client = NodeCloudClient(payout_server, "node-secret-value", timeout=5.0)
    assert client.payout_address() == {}                    # data: null → {}
    client.submit_payout_address(EVM, "默认")
    get_call, post_call = PayoutHandler.seen
    assert get_call["method"] == "GET"
    assert get_call["path"] == "/api/v1/node/payout-address"
    assert get_call["key"] == "node-secret-value"
    assert post_call["method"] == "POST"
    assert post_call["path"] == "/api/v1/node/payout-address"
    assert post_call["body"] == {"address": EVM, "label": "默认"}
    assert post_call["idem"] and 16 <= len(post_call["idem"]) <= 128


# ──────────────────────────────────────── 6) 非阻塞：提交跑在 worker 线程（复用 W1 模式）
def test_submit_runs_off_the_calling_thread():
    """W1 的 ``RefreshWorker`` 组合：``start()`` 立刻返回，慢提交不占调用线程。"""
    from djb_node.desktop.refresh_worker import RefreshWorker

    started = threading.Event()
    release = threading.Event()

    class Slow(CloudStub):
        def submit_payout_address(self, address, label=""):
            started.set()
            release.wait(5)
            return {"address": address, "status": "pending"}

    class Root:
        """假 Tk root：``after`` 立刻排队，``after_cancel`` 记下来。"""

        def __init__(self):
            self.jobs: list = []

        def after(self, delay, fn):
            self.jobs.append(fn)
            return len(self.jobs)

        def after_cancel(self, job):
            pass

    controller = _controller("/tmp", Slow())
    root = Root()
    applied: list = []
    worker = RefreshWorker(lambda sections=None: controller.submit_payout(EVM),
                           apply=lambda result, error, sections=None: applied.append(
                               (result, error)),
                           schedule=root.after, cancel=root.after_cancel)
    began = time.monotonic()
    assert worker.start() is True
    assert time.monotonic() - began < 1.0        # 立即返回，没等网络
    assert started.wait(5)                       # 提交确实发生在 worker 线程
    release.set()
    deadline = time.monotonic() + 5
    while not applied and time.monotonic() < deadline:
        job = root.jobs.pop(0) if root.jobs else None   # after 轮询：每轮会追加下一次
        if job is None:
            time.sleep(0.01)
            continue
        job()
    assert applied and applied[0][1] is None
    assert applied[0][0].ok is True


# ──────────────────────────────────────── 7) 保存路径：空草稿不许抹掉已有收款地址
def test_empty_draft_never_erases_the_saved_address(tmp_path):
    """``prefill=False`` 的输入框留空 = 「没动它」。空白草稿若照常合并，会把地址清空。"""
    from djb_node.desktop.access import AccessConfig, FileAccessStore

    store = FileAccessStore(str(tmp_path / "access-config.json"))
    store.save(AccessConfig(usdt_address=EVM))
    controller = DesktopController(
        source=LocalDataSource(str(tmp_path), cloud=CloudStub()),
        access=__import__("djb_node.desktop.access", fromlist=["AccessService"])
        .AccessService(store),
        drafts={"usdt_address": "   "})
    merged, warnings = controller._config_for_save()
    assert merged.usdt_address == EVM            # 原值保住
    assert warnings == []

    controller.drafts = {"usdt_address": "0xAAAA9a1b2d3e4f5a6b7c8d9e0f1a2b3c4d5ebbbb"}
    merged, _ = controller._config_for_save()
    assert merged.usdt_address.endswith("bbbb")  # 填了新值就是新值


def test_masked_value_can_no_longer_be_written_back(tmp_path):
    """旧 bug 的回归钉：把掩码当草稿保存必须**失败**（而不是悄悄写坏配置）。"""
    from djb_node.desktop.access import AccessConfig, AccessService, FileAccessStore

    store = FileAccessStore(str(tmp_path / "access-config.json"))
    store.save(AccessConfig(usdt_address=EVM))
    controller = DesktopController(
        source=LocalDataSource(str(tmp_path), cloud=CloudStub()),
        access=AccessService(store), drafts={"usdt_address": "0x3f7c…9a2c"})
    result = controller.action("access.save")
    assert result.ok is False
    assert store.load().usdt_address == EVM      # 配置没被写坏


def test_fit_wrap_follows_shrink_but_steps_growth():
    """换行宽策略：**变窄立刻跟**（不裁字）、**变宽按步进**（吃掉 1px 抖动）。

    这条钉的是一个真回环：逐像素追列宽 → 行数变 → 高度变 → 滚动条出现 → 列宽又变
    → ``update_idletasks`` 永不返回（接入页长出两行长提示时必现；修前真窗口挂死）。
    """
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:                            # pragma: no cover - 无显示
        pytest.skip("没有可用的 Tk 显示")
    from djb_node.desktop.app import DesktopApp, demo_snapshot

    app = DesktopApp(DesktopController(FakeDataSource(demo_snapshot().sections, source="demo")),
                     root=root)
    try:
        # 根窗口已被 app 用 grid 管理，另开一个 Toplevel 放被测 label
        host = tk.Toplevel(root)
        label = tk.Label(host, text="x" * 80)
        label.pack()
        root.update_idletasks()

        label.configure(wraplength=200)
        assert app.WRAP_GROW_STEP >= 8
        # 1px 抖动：不跟（否则就是回环）
        app._fit_wrap(label, width=203)
        assert int(float(label.cget("wraplength"))) == 200
        # 变窄立刻跟（含 1px：宁可多折一行也不裁字）——回环由「变宽按步进」掐断
        app._fit_wrap(label, width=201)
        assert int(float(label.cget("wraplength"))) == 199
        # 变窄：立刻跟（宁可多折一行，也不许裁字）
        app._fit_wrap(label, width=120)
        assert int(float(label.cget("wraplength"))) == 118
        # 明显变宽：跟（步进以内不动，超过步进才动）
        app._fit_wrap(label, width=130)
        assert int(float(label.cget("wraplength"))) == 118
        app._fit_wrap(label, width=200)
        assert int(float(label.cget("wraplength"))) == 198
    finally:
        app.stop()
        try:
            root.destroy()
        except Exception:                        # pragma: no cover
            pass


# ────────────────────────────────────────────── 8) 真窗口：折叠区与不预填（Xvfb 下跑）
def test_real_tk_access_page_folds_advanced_and_never_prefills_mask():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:                            # pragma: no cover - 无显示
        pytest.skip("没有可用的 Tk 显示")
    from djb_node.desktop.app import DesktopApp, demo_snapshot
    from djb_node.desktop.datasource import FakeDataSource

    app = DesktopApp(DesktopController(FakeDataSource(demo_snapshot().sections, source="demo")),
                     root=root)
    try:
        app.show_view("access")
        root.update()
        # 首屏：节点凭证（秘密）**没有**输入框——折叠起来才不占地方
        assert "node_secret" not in app._vars
        assert app._advanced_toggle.text == "高级（排障用）"
        assert app._advanced_panel.winfo_ismapped() == 0
        # 收款地址：输入框留空（不预填掩码），当前值与云端状态都在提示里
        assert app._vars["usdt_address"].get() == ""
        assert app._fields["usdt_address"].prefill is False
        hint = app._fields["usdt_address"].hint
        assert "本机已存：0x3f7c…9a2c" in hint and "Polygon" in hint

        app._advanced_toggle.command()            # 展开
        root.update()
        assert "node_secret" in app._vars
        assert app._advanced_toggle.text == "收起高级"
        assert app._advanced_panel.winfo_ismapped() == 1
        assert app._vars["node_secret"].get() == ""     # 秘密同样不预填

        app._advanced_toggle.command()            # 收回
        root.update()
        assert app._advanced_toggle.text == "高级（排障用）"
    finally:
        app.stop()                               # 关轮询 + 销毁窗口（DesktopApp 的收场口）
        try:
            root.destroy()
        except Exception:                        # pragma: no cover
            pass
