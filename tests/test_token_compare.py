# SPDX-License-Identifier: Apache-2.0
"""C8 · 真实请求路径的 token 差异检测：开关默认关闭、>1% 才记、不改 usage、流式同样生效。

全部走本机回环 HTTP（``127.0.0.1:<随机端口>``）+ tests/mock_m02 假上游，不联网、不落正文。
"""
from __future__ import annotations

import http.client
import json
import logging

import pytest

from djb_node import facts, logging_setup, tokens
from djb_node.server import Admission, LocalApiServer, RouteTarget
from tests.mock_m02 import MockServer

TOKEN = "t" * 40
SECRET_TEXT = "机密正文ZZ1234"          # 断言：正文永不出现在日志事件里
MESSAGES = [{"role": "user", "content": SECRET_TEXT}]
NESTED_ENABLED = {"client": {"token": {"compare": {"enabled": True}}}}


# ─────────────────────────────────────────────────────────── 夹具 / 工具
class FakeRuntime:
    def __init__(self, upstream_base=None):
        self.upstream_base = upstream_base
        self.observed: list[dict] = []

    def health(self):
        return {"status": "ok"}

    def models(self):
        return {"object": "list", "data": []}

    def admit(self, model):
        return Admission(True)

    def route(self, model):
        if not self.upstream_base:
            return None
        return RouteTarget(base_url=self.upstream_base, model=model, access="A2",
                           upstream_id="u1")

    def observe(self, **fields):
        self.observed.append(fields)


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
    """直接挂在 djb logger 上抓事件（不依赖 propagate / pytest caplog 配置）。"""
    logger = logging_setup.get_logger()
    handler = Capture()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield handler.items
    logger.removeHandler(handler)
    logger.setLevel(previous_level)


@pytest.fixture
def upstream():
    server = MockServer().start()
    yield server
    server.stop()


def start_server(upstream, *, settings=None):
    runtime = FakeRuntime(upstream.upstream_base_url)
    server = LocalApiServer(runtime, token=TOKEN, port=0, settings=settings)
    server.bind()
    server.start()
    return server, runtime


@pytest.fixture
def serve(upstream, monkeypatch):
    """起一个本机镜像端点；``flag=True/False`` 时注入 config.feature_flag 的返回值。"""
    from djb_node import config as config_module

    started: list[LocalApiServer] = []

    def factory(*, flag=None, settings=None):
        if flag is not None:
            monkeypatch.setattr(config_module, "feature_flag",
                                lambda config, name: flag, raising=False)
        server, runtime = start_server(upstream, settings=settings)
        started.append(server)
        return server, runtime

    yield factory
    for server in started:
        server.shutdown()


def call(server, *, stream=False, messages=None, tools=None):
    payload = {"model": "mock-model", "messages": messages or MESSAGES}
    if stream:
        payload["stream"] = True
    if tools is not None:
        payload["tools"] = tools
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    connection.request("POST", "/v1/chat/completions",
                       body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       headers={"Content-Type": "application/json",
                                "Authorization": "Bearer " + TOKEN})
    response = connection.getresponse()
    body = response.read()
    status = response.status
    connection.close()
    return status, body


def mismatch_events(events):
    return [item for item in events if item["event"] == "token_mismatch"]


# ─────────────────────────────────────────────────────────── 开关口径（委托 config.feature_flag）
def test_flag_default_is_off():
    # 未注入 resolver（或 resolver 缺省）→ 按 facts 默认关闭
    assert tokens.compare_enabled(None) is False
    assert tokens.compare_enabled({}) is False
    assert tokens.compare_enabled(NESTED_ENABLED) is False
    assert facts.FEATURE_FLAGS[facts.TOKEN_COMPARE_FLAG]["default"] is False


def test_flag_delegates_to_config_feature_flag(monkeypatch):
    """唯一求值实现是 config.feature_flag；tokens 只做委托 + 形状归一。"""
    from djb_node import config as config_module

    calls: list[tuple] = []

    def fake_feature_flag(config, name):
        calls.append((config, name))
        return True

    monkeypatch.setattr(config_module, "feature_flag", fake_feature_flag, raising=False)
    assert tokens.compare_enabled(NESTED_ENABLED) is True
    assert tokens.flag_enabled("client.token.compare.enabled", None) is True
    assert [item[1] for item in calls] == [facts.TOKEN_COMPARE_FLAG] * 2
    # 入参形状：dict 归一成 ClientConfig（供 resolver 读 .extra），None 归一成默认配置
    assert all(isinstance(item[0], config_module.ClientConfig) for item in calls)
    assert calls[0][0].extra.get("client") == NESTED_ENABLED["client"]


def test_flag_honours_resolver_false(monkeypatch):
    from djb_node import config as config_module

    monkeypatch.setattr(config_module, "feature_flag", lambda config, name: False,
                        raising=False)
    assert tokens.compare_enabled(NESTED_ENABLED) is False


def test_flag_without_resolver_is_off_not_crashing(monkeypatch):
    """C7 的 resolver 尚未合入时：不抛、按默认关闭（不维护第二套解析）。"""
    from djb_node import config as config_module

    monkeypatch.delattr(config_module, "feature_flag", raising=False)
    assert tokens.compare_enabled(NESTED_ENABLED) is False
    assert tokens.flag_enabled("unknown.flag") is False


def test_feature_flag_carries_owner_expire_and_rollback():
    for name, meta in facts.FEATURE_FLAGS.items():
        assert meta["default"] is False, name
        for field in ("owner", "expire_at", "rollback_rule", "description"):
            assert meta.get(field), (name, field)
    flag = facts.FEATURE_FLAGS[facts.TOKEN_COMPARE_FLAG]
    assert flag["owner"] == "client-seat"
    assert flag["expire_at"] == "2026-12-31"
    assert "只记录上游 usage" in str(flag["rollback_rule"])
    assert "desc" not in flag                     # 形状统一：description
    assert facts.TOKEN_COMPARE_FLAG_META is flag   # 元数据只定义一次


def test_compare_counts_threshold_is_strictly_greater_than_one_percent():
    assert tokens.compare_counts(100, 100) is None
    assert tokens.compare_counts(101, 100) is None          # 恰好 1% → 不记
    assert tokens.compare_counts(99, 100) is None           # 恰好 -1% → 不记
    hit = tokens.compare_counts(102, 100)
    assert hit is not None and hit.delta_tokens == 2 and hit.delta_pct == 2.0
    low = tokens.compare_counts(50, 200)
    assert low is not None and low.delta_tokens == -150
    assert tokens.compare_counts(5, 0) is None              # 上游 0：没有基准，不误报
    assert tokens.compare_counts(0, 0) is None


# ─────────────────────────────────────────────────────────── 非流式路径
def test_no_event_when_flag_is_off(serve, events, upstream):
    upstream.chat_usage = {"prompt_tokens": 500, "completion_tokens": 5, "total_tokens": 505}
    server, _ = serve()                                     # settings 缺省 = 关闭
    status, _ = call(server)
    assert status == 200
    assert mismatch_events(events) == []


def test_mismatch_recorded_when_enabled(serve, events, upstream):
    upstream.chat_usage = {"prompt_tokens": 500, "completion_tokens": 5, "total_tokens": 505}
    server, _ = serve(flag=True)
    status, _ = call(server)
    assert status == 200
    hits = mismatch_events(events)
    assert len(hits) == 1
    hit = hits[0]
    local = tokens.count_messages("mock-model", MESSAGES).tokens
    assert hit["model"] == "mock-model"
    assert hit["source"] == "json"
    assert hit["local_tokens"] == local
    assert hit["upstream_tokens"] == 500
    assert hit["delta_tokens"] == local - 500
    assert hit["delta_pct"] == round(abs(local - 500) / 5.0, 4)
    assert hit["tokenizer_id"] == facts.TOKENIZER_HEURISTIC_ID
    assert hit["estimated"] is True
    assert hit["request_id"]


def test_no_event_when_counts_agree(serve, events, upstream):
    local = tokens.count_messages("mock-model", MESSAGES).tokens
    upstream.chat_usage = {"prompt_tokens": local, "completion_tokens": 5,
                           "total_tokens": local + 5}
    server, _ = serve(flag=True)
    assert call(server)[0] == 200
    assert mismatch_events(events) == []


def test_usage_is_not_rewritten_and_observe_unchanged(serve, events, upstream):
    upstream.chat_usage = {"prompt_tokens": 500, "completion_tokens": 5, "total_tokens": 505}
    server, runtime = serve(flag=True)
    status, body = call(server)
    payload = json.loads(body)
    assert status == 200
    # 返回给客户端的就是上游 body（usage 原样，未被本地数字覆盖）
    assert payload["usage"] == {"prompt_tokens": 500, "completion_tokens": 5,
                                "total_tokens": 505}
    # observe() 收到的仍是上游数（账本/收据口径不变）
    assert runtime.observed == [{"model": "mock-model", "prompt_tokens": 500,
                                 "completion_tokens": 5, "total_tokens": 505,
                                 "request_id": runtime.observed[0]["request_id"]}]


def test_events_contain_no_prompt_text(serve, events, upstream):
    upstream.chat_usage = {"prompt_tokens": 500, "completion_tokens": 5, "total_tokens": 505}
    server, _ = serve(flag=True)
    call(server)
    dumped = json.dumps(events, ensure_ascii=False)
    assert mismatch_events(events)
    assert SECRET_TEXT not in dumped
    assert "messages" not in mismatch_events(events)[0]


def test_tools_are_part_of_local_estimate(serve, events, upstream):
    tools = [{"type": "function",
              "function": {"name": "get_price", "description": "查价",
                           "parameters": {"type": "object", "properties": {}}}}]
    local = tokens.count_messages("mock-model", MESSAGES, tools=tools).tokens
    upstream.chat_usage = {"prompt_tokens": 1000, "completion_tokens": 5,
                           "total_tokens": 1005}
    server, _ = serve(flag=True)
    call(server, tools=tools)
    assert mismatch_events(events)[0]["local_tokens"] == local


def test_no_event_without_upstream_usage(serve, events, upstream):
    upstream.chat_usage = {}                                # 上游不回 usage
    server, runtime = serve(flag=True)
    status, _ = call(server)
    assert status == 200
    assert runtime.observed == []
    assert mismatch_events(events) == []


def test_zero_upstream_prompt_tokens_is_skipped(serve, events, upstream):
    upstream.chat_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    server, _ = serve(flag=True)
    assert call(server)[0] == 200
    assert mismatch_events(events) == []


def test_upstream_error_status_does_not_compare(serve, events, upstream):
    upstream.chat_status = 502
    server, runtime = serve(flag=True)
    assert call(server)[0] == 502
    assert runtime.observed == []
    assert mismatch_events(events) == []


# ─────────────────────────────────────────────────────────── 流式路径
def test_stream_mismatch_records_stream_source(serve, events, upstream):
    upstream.chat_usage = {"prompt_tokens": 400, "completion_tokens": 2, "total_tokens": 402}
    server, runtime = serve(flag=True)
    status, body = call(server, stream=True)
    assert status == 200
    assert b"[DONE]" in body
    local = tokens.count_messages("mock-model", MESSAGES).tokens
    hits = mismatch_events(events)
    assert len(hits) == 1
    assert hits[0]["source"] == "stream"
    assert hits[0]["upstream_tokens"] == 400
    assert hits[0]["local_tokens"] == local
    assert runtime.observed[-1]["prompt_tokens"] == 400


def test_stream_with_agreeing_usage_records_nothing(serve, events, upstream):
    local = tokens.count_messages("mock-model", MESSAGES).tokens
    upstream.chat_usage = {"prompt_tokens": local, "completion_tokens": 2,
                           "total_tokens": local + 2}
    server, _ = serve(flag=True)
    call(server, stream=True)
    assert mismatch_events(events) == []


def test_stream_flag_off_records_nothing(serve, events, upstream):
    upstream.chat_usage = {"prompt_tokens": 400, "completion_tokens": 2, "total_tokens": 402}
    server, _ = serve()
    call(server, stream=True)
    assert mismatch_events(events) == []


# ─────────────────────────────────────────────────────────── 失败隔离
def test_counter_failure_does_not_break_request(serve, events, upstream, monkeypatch):
    upstream.chat_usage = {"prompt_tokens": 500, "completion_tokens": 5, "total_tokens": 505}

    def boom(*args, **kwargs):
        raise RuntimeError("counter exploded")

    from djb_node import server as server_module
    monkeypatch.setattr(server_module.tokens, "count_messages", boom)
    server, runtime = serve(flag=True)
    status, body = call(server)
    assert status == 200
    assert json.loads(body)["usage"]["prompt_tokens"] == 500
    assert runtime.observed[0]["prompt_tokens"] == 500       # 主路径照常
    assert mismatch_events(events) == []
    failed = [item for item in events if item["event"] == "local_api.token_compare_failed"]
    assert len(failed) == 1 and failed[0]["error"] == "RuntimeError"
