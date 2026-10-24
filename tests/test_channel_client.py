# SPDX-License-Identifier: Apache-2.0
"""C10c · 出站长轮询通道：开关门控、204 空转、job 执行与字段映射、错误映射、退避、
停止语义、幂等回传、日志脱敏、daemon 线程起停。

全部本机回环 / 注入替身，不联网、不 sleep（退避用注入 sleep 记录器）。
"""
from __future__ import annotations

import json
import logging
import threading
import time

import pytest

from djb_node import facts, logging_setup
from djb_node.api import NodeApi
from djb_node.channel import ChannelClient, channel_enabled, parse_job
from djb_node.config import ClientConfig
from djb_node.daemon import Daemon
from djb_node.errors import AuthError, ForbiddenError, NetworkError
from djb_node.httpclient import HttpClient, HttpResponse
from djb_node.util import utcnow
from tests.mock_channel import DEFAULT_COMPLETION, MockChannel

LOCAL_TOKEN = "t" * 40
SECRET_TEXT = "机密正文ZZ1234"          # 断言：正文与令牌都不进日志
MESSAGES = [{"role": "user", "content": SECRET_TEXT}]
JOB = {"job_id": "job-1", "node_id": "node-1", "timeout_s": 30,
       "requests": {"model": "mock-model", "messages": MESSAGES,
                    "max_tokens": 16, "temperature": 0.3, "stop": ["\n\n"]}}

# spec 1.2.9 NodeChannelResultRequest：客户端只发这些键（多余会被丢弃/40001）
WIRE_COMPLETED = {"job_id", "status", "content", "finish_reason", "tokens_in",
                  "tokens_out", "usage_source", "latency_ms"}
WIRE_FAILED = {"job_id", "status", "error_code", "error_class", "latency_ms"}
LEGACY_FIELDS = {"succeeded", "node_id", "usage", "duration_ms", "completed_at"}


# ─────────────────────────────────────────────────────────── 夹具 / 替身
class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        fields = getattr(record, "fields", None) or {}
        merged = {"event": getattr(record, "event", record.getMessage())}
        merged.update(fields)
        self.items.append(merged)


@pytest.fixture
def events():
    logger = logging_setup.get_logger()
    handler = Capture()
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield handler.items
    logger.removeHandler(handler)
    logger.setLevel(previous)


class FakeApi:
    """平台 API 替身：脚本化 poll（None=204 / dict=job / 异常），记录 submit。"""

    def __init__(self, polls=None, *, submit_fail_times: int = 0,
                 duplicate: bool = False) -> None:
        self.polls = list(polls or [])
        self.poll_calls: list[int] = []
        self.submits: list[dict] = []
        self.submit_fail_times = submit_fail_times
        self.duplicate = duplicate
        self.stop_event: threading.Event | None = None
        self.stop_after: int | None = None

    def poll_channel(self, wait_s: int = facts.CHANNEL_POLL_WAIT_S):
        self.poll_calls.append(wait_s)
        if (self.stop_event is not None and self.stop_after is not None
                and len(self.poll_calls) >= self.stop_after):
            self.stop_event.set()
        if not self.polls:
            return None
        item = self.polls.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def submit_channel_result(self, payload):
        self.submits.append(payload)
        if self.submit_fail_times > 0:
            self.submit_fail_times -= 1
            raise NetworkError("mock submit failure", code=50903)
        return {"accepted": [] if self.duplicate else [payload["job_id"]],
                "duplicate": bool(self.duplicate)}


class FakeChat:
    """本机镜像调用替身：记录 (body, timeout)，返回可配 HttpResponse。"""

    def __init__(self, payload=None, *, status: int = 200, error=None) -> None:
        self.payload = payload if payload is not None else dict(DEFAULT_COMPLETION)
        self.status = status
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, body, timeout):
        self.calls.append({"body": body, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return HttpResponse(status=self.status, headers={},
                            body=json.dumps(self.payload, ensure_ascii=False).encode(),
                            url="local", elapsed_ms=3)


@pytest.fixture
def channel_mock():
    server = MockChannel().start()
    yield server
    server.stop()


def _client(paths, credentials, *, api=None, chat_call=None, config=None,
            port: int = 18999, sleep=None, clock=None) -> ChannelClient:
    credentials.set_node("node-1", "ck_" + "test_channel_key_0123456789abcdef")
    config = config or ClientConfig(gateway_url="http://127.0.0.1:1",
                                    mirror_port=port,
                                    extra={facts.CHANNEL_FLAG: True})
    return ChannelClient(paths, config, credentials, port=port, local_token=LOCAL_TOKEN,
                         api=api, chat_call=chat_call or FakeChat(),
                         now=clock, sleep=sleep)


def _run(client, api, *, stop_after: int, timeout: float = 5.0) -> threading.Event:
    """跑 run() 并在第 stop_after 次 poll 时置停止位（不真 sleep）。"""
    stop = threading.Event()
    api.stop_event = stop
    api.stop_after = stop_after
    thread = threading.Thread(target=client.run, args=(stop,), daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "通道 run() 未在超时内退出"
    return stop


# ─────────────────────────────────────────────────────────── 开关
def test_flag_default_off_with_three_fields():
    for config in (None, ClientConfig()):
        assert channel_enabled(config) is False
    spec = facts.FEATURE_FLAGS[facts.CHANNEL_FLAG]
    assert spec["default"] is False
    assert spec["owner"] == "client-seat"
    assert spec["expire_at"] == "2026-12-31"
    assert "停长轮询" in str(spec["rollback_rule"])
    assert spec.get("description")
    assert "desc" not in spec
    enabled = ClientConfig(extra={"feature_flags": {facts.CHANNEL_FLAG: True}})
    assert channel_enabled(enabled) is True


# ─────────────────────────────────────────────────────────── poll 循环
def test_204_keeps_polling_without_submit(paths, credentials):
    api = FakeApi([None, None])
    client = _client(paths, credentials, api=api)
    _run(client, api, stop_after=2)
    assert api.poll_calls == [facts.CHANNEL_POLL_WAIT_S] * 2
    assert api.submits == []


def test_stop_event_already_set_exits_without_polling(paths, credentials):
    api = FakeApi()
    client = _client(paths, credentials, api=api)
    stop = threading.Event()
    stop.set()
    client.run(stop)
    assert api.poll_calls == []


def test_poll_401_stops_channel(paths, credentials, events):
    api = FakeApi([AuthError("凭据失效", code=40101)])
    client = _client(paths, credentials, api=api)
    _run(client, api, stop_after=5)          # 只要退出得比 5 次早即可
    assert len(api.poll_calls) == 1          # 401 不再 poll
    assert [item["event"] for item in events].count("channel.auth_failed") == 1
    assert [item["event"] for item in events].count("channel.poll_failed") == 0


def test_poll_403_stops_channel(paths, credentials, events):
    api = FakeApi([ForbiddenError("被冻结", code=40305)])
    client = _client(paths, credentials, api=api)
    _run(client, api, stop_after=5)
    assert len(api.poll_calls) == 1
    failed = [item for item in events if item["event"] == "channel.auth_failed"]
    assert failed and failed[0]["code"] == 40305


def test_poll_errors_backoff_1_5_30(paths, credentials, events):
    delays: list[float] = []
    api = FakeApi([NetworkError("net", code=50903)] * 4)
    client = _client(paths, credentials, api=api, sleep=delays.append)
    _run(client, api, stop_after=4)
    assert delays == [1.0, 5.0, 30.0, 30.0]
    assert len(api.poll_calls) == 4
    assert [item["event"] for item in events].count("channel.poll_failed") == 4


# ─────────────────────────────────────────────────────────── job 执行 / 映射
def test_job_success_maps_contract_fields(paths, credentials, clock, events):
    api = FakeApi([JOB])
    chat = FakeChat()
    client = _client(paths, credentials, api=api, chat_call=chat, clock=clock)
    _run(client, api, stop_after=1)
    assert len(chat.calls) == 1
    body = chat.calls[0]["body"]
    assert body["model"] == "mock-model"
    assert body["messages"] == MESSAGES
    assert body["max_tokens"] == 16 and body["temperature"] == 0.3
    assert body["stop"] == ["\n\n"] and body["stream"] is False
    assert len(api.submits) == 1
    result = api.submits[0]
    # 契约口径：status=completed + tokens_in/out + usage_source，无 succeeded/node_id/usage 嵌套
    assert result["job_id"] == "job-1"
    assert result["status"] == "completed"
    assert result["content"] == "你好"
    assert result["finish_reason"] == "stop"
    assert result["tokens_in"] == 7 and result["tokens_out"] == 2
    assert result["usage_source"] == "actual"
    assert isinstance(result["latency_ms"], int) and result["latency_ms"] >= 0
    assert set(result) == WIRE_COMPLETED
    assert not (set(result) & LEGACY_FIELDS)
    assert client.snapshot()["jobs_done"] == 1

def test_job_body_only_whitelisted_fields(paths, credentials):
    job = json.loads(json.dumps(JOB))
    job["requests"].update({"foo": "bar", "stream": True, "extra_hint": {"x": 1}})
    job["requests"]["messages"] = MESSAGES
    api = FakeApi([job])
    chat = FakeChat()
    client = _client(paths, credentials, api=api, chat_call=chat)
    _run(client, api, stop_after=1)
    body = chat.calls[0]["body"]
    assert "foo" not in body and "extra_hint" not in body
    assert body["stream"] is False
    assert set(body) <= {"model", "messages", "max_tokens", "temperature", "stop", "stream"}


def test_local_4xx_maps_to_failed_with_error_code(paths, credentials):
    api = FakeApi([JOB])
    chat = FakeChat(status=402)
    client = _client(paths, credentials, api=api, chat_call=chat)
    _run(client, api, stop_after=1)
    result = api.submits[0]
    assert result["status"] == "failed"
    assert result["error_code"] == "40201"          # 字符串，不是整数
    assert result["error_class"] == "quota_exceeded"
    assert set(result) == WIRE_FAILED
    assert not (set(result) & LEGACY_FIELDS)
    assert "content" not in result

def test_local_5xx_maps_to_upstream_error(paths, credentials):
    api = FakeApi([JOB])
    client = _client(paths, credentials, api=api, chat_call=FakeChat(status=500))
    _run(client, api, stop_after=1)
    result = api.submits[0]
    assert result["status"] == "failed"
    assert result["error_code"] == "50903"
    assert result["error_class"] == "upstream_error"

def test_local_timeout_maps_to_504(paths, credentials):
    api = FakeApi([JOB])
    error = NetworkError("local timeout", code=50902, reason="timeout")
    client = _client(paths, credentials, api=api,
                     chat_call=FakeChat(error=error))
    _run(client, api, stop_after=1)
    result = api.submits[0]
    assert result["status"] == "failed"
    assert result["error_code"] == "50902"
    assert result["error_class"] == "upstream_timeout"

def test_invalid_job_submits_failed_without_chat(paths, credentials):
    api = FakeApi([{"job_id": "job-bad"}])          # 缺 request/requests
    chat = FakeChat()
    client = _client(paths, credentials, api=api, chat_call=chat)
    _run(client, api, stop_after=1)
    assert chat.calls == []
    assert api.submits[0]["job_id"] == "job-bad"
    assert api.submits[0]["status"] == "failed"
    assert api.submits[0]["error_code"] == "40001"
    assert api.submits[0]["error_class"] == "invalid_job"

def test_job_timeout_clamped_to_upper_and_lower_bounds(paths, credentials):
    huge = json.loads(json.dumps(JOB))
    huge["job_id"] = "job-huge"
    huge["timeout_s"] = 99999
    tiny = json.loads(json.dumps(JOB))
    tiny["job_id"] = "job-tiny"
    tiny["timeout_s"] = 0.2
    api = FakeApi([huge, tiny])
    chat = FakeChat()
    client = _client(paths, credentials, api=api, chat_call=chat)
    _run(client, api, stop_after=2)
    assert chat.calls[0]["timeout"] == facts.CHANNEL_JOB_TIMEOUT_MAX_S
    assert chat.calls[1]["timeout"] == facts.CHANNEL_JOB_TIMEOUT_MIN_S
    assert parse_job(huge).timeout_s == facts.CHANNEL_JOB_TIMEOUT_MAX_S


# ─────────────────────────────────────────────────────────── 回传（幂等）
def test_submit_retries_once_with_same_idempotency(paths, credentials, events):
    api = FakeApi([JOB], submit_fail_times=1)
    client = _client(paths, credentials, api=api, sleep=lambda seconds: None)
    _run(client, api, stop_after=1)
    assert len(api.submits) == 2
    assert {item["job_id"] for item in api.submits} == {"job-1"}
    submitted = [item for item in events if item["event"] == "channel.result_submitted"]
    assert len(submitted) == 1 and submitted[0]["duplicate"] is False


def test_submit_duplicate_is_reported(paths, credentials, events):
    api = FakeApi([JOB], duplicate=True)
    client = _client(paths, credentials, api=api)
    _run(client, api, stop_after=1)
    submitted = [item for item in events if item["event"] == "channel.result_submitted"]
    assert submitted and submitted[0]["duplicate"] is True


def test_submit_gives_up_after_attempts(paths, credentials, events):
    api = FakeApi([JOB], submit_fail_times=99)
    client = _client(paths, credentials, api=api, sleep=lambda seconds: None)
    _run(client, api, stop_after=1)
    assert len(api.submits) == facts.CHANNEL_RESULT_ATTEMPTS
    failed = [item for item in events if item["event"] == "channel.result_failed"]
    assert failed and failed[0]["attempts"] == facts.CHANNEL_RESULT_ATTEMPTS


# ─────────────────────────────────────────────────────────── 日志脱敏
def test_events_never_log_body_or_token(paths, credentials, events):
    api = FakeApi([JOB, JOB])
    client = _client(paths, credentials, api=api)
    _run(client, api, stop_after=2)
    dumped = json.dumps(events, ensure_ascii=False)
    assert SECRET_TEXT not in dumped
    assert LOCAL_TOKEN not in dumped
    keys = {key for item in events for key in item}
    assert not keys & {"messages", "content", "prompt", "token", "local_token"}
    received = [item for item in events if item["event"] == "channel.job_received"]
    assert received and received[0]["job_id"] == "job-1"
    assert received[0]["model"] == "mock-model"


# ─────────────────────────────────────────────────────────── api.py 层
class FakeHttp:
    """HttpClient 替身：记录 method/url/headers/body/timeout，返回脚本化响应。"""

    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    def request(self, method, url, *, headers=None, json_body=None, body=None,
                timeout=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "json_body": json_body, "timeout": timeout})
        return self.response


def test_api_poll_channel_204_returns_none_and_sends_key():
    http = FakeHttp(HttpResponse(status=204, headers={}, body=b"", url="", elapsed_ms=1))
    api = NodeApi("https://gateway.test", client_key="ck_test_0123456789abcdef",
                  http=http)
    assert api.poll_channel(7) is None
    call = http.calls[0]
    assert call["url"].endswith(facts.CHANNEL_POLL_PATH)
    assert call["json_body"] == {"wait_s": 7}
    assert call["timeout"] == facts.CHANNEL_POLL_TIMEOUT_S
    from djb_node import protocol
    assert call["headers"][protocol.NODE_KEY_HEADER] == "ck_test_0123456789abcdef"
    assert call["headers"][protocol.PROTOCOL_HEADER] == facts.PROTOCOL_VERSION


def test_api_poll_channel_raises_on_401():
    body = json.dumps({"error": {"type": "authentication_error", "code": 40101,
                                 "message": "bad key", "request_id": "r"}}).encode()
    http = FakeHttp(HttpResponse(status=401, headers={}, body=body, url="", elapsed_ms=1))
    api = NodeApi("https://gateway.test", client_key="ck_test", http=http)
    with pytest.raises(AuthError):
        api.poll_channel(1)


def test_api_submit_channel_result_uses_job_id_as_idempotency_key():
    body = json.dumps({"accepted": ["job-1"], "duplicate": False}).encode()
    http = FakeHttp(HttpResponse(status=200, headers={}, body=body, url="", elapsed_ms=1))
    api = NodeApi("https://gateway.test", client_key="ck_test", http=http)
    result = api.submit_channel_result({"job_id": "job-1", "status": "completed"})
    assert result["accepted"] == ["job-1"]
    from djb_node import protocol
    call = http.calls[0]
    assert call["url"].endswith(facts.CHANNEL_RESULT_PATH)
    assert call["headers"][protocol.IDEMPOTENCY_HEADER] == "job-1"
    assert call["timeout"] == facts.CHANNEL_RESULT_TIMEOUT_S


# ─────────────────────────────────────────────────────────── 真实回环集成
def test_integration_poll_job_local_chat_and_submit(paths, credentials, channel_mock):
    credentials.set_node("node-1", "ck_" + "test_channel_key_0123456789abcdef")
    channel_mock.poll_script = [JOB]
    channel_mock.poll_default = None
    config = ClientConfig(gateway_url=channel_mock.base_url,
                          mirror_port=channel_mock.port,
                          extra={facts.CHANNEL_FLAG: True})
    client = ChannelClient(paths, config, credentials, port=channel_mock.port,
                           local_token=LOCAL_TOKEN,
                           http=HttpClient(timeout=5.0), now=utcnow, sleep=None)
    stop = threading.Event()
    thread = threading.Thread(target=client.run, args=(stop,), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not channel_mock.results:
        time.sleep(0.02)
    stop.set()
    thread.join(5)
    assert channel_mock.results, "结果没有回传到平台端点"
    result = channel_mock.results[0]
    assert result["job_id"] == "job-1" and result["status"] == "completed"
    assert result["content"] == "你好"
    assert result["tokens_in"] == 7 and result["tokens_out"] == 2
    assert result["usage_source"] == "actual"
    assert set(result) <= WIRE_COMPLETED
    # 本机镜像调用带 Bearer 一次性令牌，body 里没有平台注入字段
    assert channel_mock.chat_auth[0] == "Bearer " + LOCAL_TOKEN
    assert channel_mock.chat_requests[0]["stream"] is False
    # 平台侧收到的头里带客户端密钥 + 幂等键
    from djb_node import protocol
    assert channel_mock.result_headers[0][protocol.IDEMPOTENCY_HEADER.lower()] == "job-1"
    assert channel_mock.poll_headers[0][protocol.NODE_KEY_HEADER.lower()].startswith("ck_")


# ─────────────────────────────────────────────────────────── daemon 线程起停
class StubChannel:
    """daemon 接线用替身：只等停止位，不真 poll。"""

    instances: list["StubChannel"] = []

    def __init__(self, paths, config, credentials, **kwargs) -> None:
        self.kwargs = kwargs
        self.stop_event: threading.Event | None = None
        StubChannel.instances.append(self)

    def snapshot(self):
        return {"state": "polling", "jobs_done": 0, "jobs_failed": 0,
                "last_job_id": None, "last_error": None}

    def run(self, stop_event):
        self.stop_event = stop_event
        stop_event.wait(30)


@pytest.fixture
def daemon_env(paths, store, credentials):
    from tests.test_daemon import _daemon
    from tests.mock_m02 import MockServer

    server = MockServer().start()
    yield paths, store, credentials, server, _daemon
    server.stop()


def test_daemon_starts_channel_thread_when_flag_enabled(daemon_env, monkeypatch):
    paths, store, credentials, mock, make_daemon = daemon_env
    from djb_node import daemon as daemon_module

    StubChannel.instances = []
    monkeypatch.setattr(daemon_module, "ChannelClient", StubChannel)
    config = ClientConfig(gateway_url=mock.base_url, mirror_port=1717,
                          extra={facts.CHANNEL_FLAG: True})
    daemon = make_daemon(paths, store, credentials, mock, config=config)
    daemon.start()
    thread = daemon._channel_thread
    try:
        assert len(StubChannel.instances) == 1
        stub = StubChannel.instances[0]
        assert stub.kwargs["local_token"] == "t" * 40
        assert stub.kwargs["port"] == daemon.server.port
        assert thread is not None and thread.is_alive()
        status = daemon.status()
        assert status["channel"]["enabled"] is True
        assert status["channel"]["running"] is True
        assert status["channel"]["port"] == daemon.server.port
        assert status["flags"][facts.CHANNEL_FLAG]["enabled"] is True
    finally:
        daemon.stop()
    assert thread is not None and not thread.is_alive()
    assert daemon._channel_thread is None
    assert daemon.status()["channel"]["running"] is False


def test_daemon_has_no_channel_by_default(daemon_env, monkeypatch):
    paths, store, credentials, mock, make_daemon = daemon_env
    from djb_node import daemon as daemon_module

    StubChannel.instances = []
    monkeypatch.setattr(daemon_module, "ChannelClient", StubChannel)
    daemon = make_daemon(paths, store, credentials, mock)
    daemon.start()
    try:
        assert StubChannel.instances == []
        assert daemon._channel is None and daemon._channel_thread is None
        status = daemon.status()
        assert status["channel"]["enabled"] is False
        assert status["channel"]["running"] is False
        assert status["channel"]["jobs_done"] == 0
        assert status["flags"][facts.CHANNEL_FLAG]["enabled"] is False
    finally:
        daemon.stop()


def test_daemon_status_exposes_channel_wait_and_state(daemon_env, monkeypatch):
    paths, store, credentials, mock, make_daemon = daemon_env
    from djb_node import daemon as daemon_module

    StubChannel.instances = []
    monkeypatch.setattr(daemon_module, "ChannelClient", StubChannel)
    config = ClientConfig(gateway_url=mock.base_url, mirror_port=1717,
                          extra={facts.CHANNEL_FLAG: True})
    daemon = make_daemon(paths, store, credentials, mock, config=config)
    daemon.start()
    try:
        channel = daemon.status()["channel"]
        assert channel["wait_s"] == facts.CHANNEL_POLL_WAIT_S
        assert channel["state"] in facts.CHANNEL_STATUS_KINDS
        assert channel["jobs_failed"] == 0
        assert channel["last_job_id"] is None
    finally:
        daemon.stop()


# ─────────────────────────────────────────────────────────── wire 契约（C10a 1.2.9）
def _completed_result(paths, credentials, chat) -> dict:
    api = FakeApi([JOB])
    client = _client(paths, credentials, api=api, chat_call=chat)
    _run(client, api, stop_after=1)
    return api.submits[0]


def test_wire_completed_payload_is_json_and_matches_contract(paths, credentials):
    result = _completed_result(paths, credentials, FakeChat())
    wire = json.loads(json.dumps(result, ensure_ascii=False))   # 真发出去的样子
    assert set(wire) == WIRE_COMPLETED
    assert wire["status"] == "completed"
    assert wire["usage_source"] in ("actual", "estimated")
    assert isinstance(wire["tokens_in"], int) and isinstance(wire["tokens_out"], int)
    assert not (set(wire) & LEGACY_FIELDS)


def test_wire_failed_payload_matches_contract(paths, credentials):
    api = FakeApi([JOB])
    client = _client(paths, credentials, api=api, chat_call=FakeChat(status=429))
    _run(client, api, stop_after=1)
    wire = json.loads(json.dumps(api.submits[0], ensure_ascii=False))
    assert set(wire) == WIRE_FAILED
    assert wire["status"] == "failed"
    assert wire["error_code"] == "42901" and isinstance(wire["error_code"], str)
    assert not (set(wire) & LEGACY_FIELDS)
    assert "content" not in wire


def test_usage_absent_marks_estimated_without_null_tokens(paths, credentials):
    chat = FakeChat({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
    result = _completed_result(paths, credentials, chat)
    assert result["usage_source"] == "estimated"
    # spec 里 tokens_in/out 是 integer：缺失时**不发**（发 null 会破坏类型），平台取到空值
    assert "tokens_in" not in result and "tokens_out" not in result
    assert set(result) == WIRE_COMPLETED - {"tokens_in", "tokens_out"}


def test_parse_job_accepts_envelope_request_and_flat_shapes():
    request_job = {"job": {"job_id": "j1", "kind": "chat.completions",
                           "request_id": "req-1", "lease_expires_at": "2026-10-24T00:05:00Z",
                           "request": {"model": "m1", "messages": MESSAGES,
                                       "max_tokens": 8, "timeout_s": 42}},
                   "server_time": "2026-10-24T00:00:00Z"}
    parsed = parse_job(request_job)
    assert parsed.job_id == "j1" and parsed.model == "m1"
    assert parsed.messages == MESSAGES and parsed.max_tokens == 8
    assert parsed.timeout_s == 42                        # request 内的 timeout_s 也认
    legacy = parse_job({"job_id": "j2", "requests": {"model": "m2", "messages": MESSAGES}})
    assert legacy.job_id == "j2" and legacy.model == "m2"
    flat = parse_job({"job_id": "j3", "kind": "chat.completions", "model": "m3",
                      "messages": MESSAGES, "timeout_s": 5,
                      "lease_expires_at": "2026-10-24T00:05:00Z"})
    assert flat is not None and flat.job_id == "j3" and flat.model == "m3"
    assert flat.timeout_s == 5
    assert not hasattr(flat, "lease_expires_at")          # 租约只属平台侧，客户端忽略
