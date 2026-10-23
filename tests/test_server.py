# SPDX-License-Identifier: Apache-2.0
import http.client
import json
import os
import socket
import time

import pytest

from djb_node import facts
from djb_node.server import (Admission, LocalApiServer, RouteTarget, is_loopback_peer,
                             validate_bind_host)
from djb_node.util import new_token
from tests.mock_m02 import MockServer

TOKEN = "t" * 40


class FakeRuntime:
    def __init__(self, upstream_base=None, admission=None):
        self.upstream_base = upstream_base
        self.admission = admission or Admission(True)
        self.observed = []
        self.inflight = 0

    def health(self):
        return {"status": "ok", "daemon": {"state": "online"},
                "policy": {"usable": True}, "loopback_only": True}

    def models(self):
        return {"object": "list", "data": [{"id": "mock-model", "object": "model",
                                            "owned_by": "djb-node"}]}

    def admit(self, model):
        return self.admission

    def route(self, model):
        if model != "mock-model" or not self.upstream_base:
            return None
        return RouteTarget(base_url=self.upstream_base, model=model, access="A2",
                           upstream_id="u1", credential=None)

    def observe(self, **fields):
        self.observed.append(fields)

    def begin_task(self):
        self.inflight += 1

    def end_task(self):
        self.inflight -= 1


def request(port, method, path, *, body=None, token=TOKEN):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {}
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    data = response.read()
    status = response.status
    response_headers = dict(response.getheaders())
    connection.close()
    return status, data, response_headers


def test_bind_host_validation():
    assert validate_bind_host("127.0.0.1") == "127.0.0.1"
    assert validate_bind_host("localhost") == "127.0.0.1"
    with pytest.raises(Exception):
        validate_bind_host("0.0.0.0")
    with pytest.raises(Exception):
        validate_bind_host("")


def test_peer_detection():
    assert is_loopback_peer(("127.0.0.1", 1234))
    assert is_loopback_peer(("127.0.0.2", 1))
    assert is_loopback_peer(("::1", 1, 0, 0))
    assert not is_loopback_peer(("10.0.0.1", 1))
    assert not is_loopback_peer(None)


@pytest.fixture
def upstream():
    server = MockServer().start()
    yield server
    server.stop()


@pytest.fixture
def local(upstream):
    runtime = FakeRuntime(upstream.upstream_base_url)
    server = LocalApiServer(runtime, token=TOKEN, port=0)
    server.bind()
    assert server.address[0] == "127.0.0.1"
    server.start()
    yield server, runtime
    server.shutdown()


def test_health_does_not_require_token(local):
    server, runtime = local
    status, data, headers = request(server.port, "GET", "/v1/health", token=None)
    assert status == 200
    payload = json.loads(data)
    assert payload["status"] == "ok"
    assert payload["loopback_only"] is True
    assert payload["token_fingerprint"]
    assert headers.get("X-Request-Id")


def test_models_require_token(local):
    server, runtime = local
    status, data, headers = request(server.port, "GET", "/v1/models", token=None)
    assert status == 401
    payload = json.loads(data)
    assert payload["error"]["code"] == 40101
    assert payload["error"]["type"] == "authentication_error"
    status, data, _ = request(server.port, "GET", "/v1/models", token="wrong" * 10)
    assert status == 401
    status, data, _ = request(server.port, "GET", "/v1/models")
    assert status == 200
    assert json.loads(data)["data"][0]["id"] == "mock-model"


def _wait_idle(runtime, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runtime.inflight == 0:
            return
        time.sleep(0.01)
    assert runtime.inflight == 0


def test_chat_non_stream_forwarding_and_usage(local):
    server, runtime = local
    status, data, _ = request(server.port, "POST", "/v1/chat/completions",
                              body={"model": "mock-model",
                                    "messages": [{"role": "user", "content": "hi"}]})
    assert status == 200
    payload = json.loads(data)
    assert payload["object"] == "chat.completion"
    assert runtime.observed[0]["total_tokens"] == 12
    assert runtime.observed[0]["prompt_tokens"] == 7
    _wait_idle(runtime)


def test_chat_stream_forwarding(local):
    server, runtime = local
    status, data, headers = request(server.port, "POST", "/v1/chat/completions",
                                    body={"model": "mock-model", "stream": True,
                                          "messages": [{"role": "user",
                                                        "content": "hi"}]})
    assert status == 200
    assert "text/event-stream" in headers.get("Content-Type", "")
    text = data.decode("utf-8")
    assert "data: [DONE]" in text
    assert '"content": "你"' in text or '"content":"你"' in text
    assert runtime.observed[-1]["total_tokens"] == 12
    _wait_idle(runtime)


def test_admission_quota_and_paused_mapping(upstream):
    runtime = FakeRuntime(upstream.upstream_base_url,
                          Admission(False, 402, 40201, "quota_exceeded",
                                    "配额用尽：等待下个周期", retry_after=30))
    server = LocalApiServer(runtime, token=TOKEN, port=0)
    server.bind()
    server.start()
    try:
        status, data, headers = request(server.port, "POST", "/v1/chat/completions",
                                        body={"model": "mock-model",
                                              "messages": [{"role": "user",
                                                            "content": "x"}]})
        assert status == 402
        payload = json.loads(data)
        assert payload["error"]["code"] == 40201
        assert payload["error"]["reason"] == "quota_exceeded"
        assert headers.get("Retry-After") == "30"
    finally:
        server.shutdown()


def test_unknown_model_and_bad_requests(local):
    server, runtime = local
    status, data, _ = request(server.port, "POST", "/v1/chat/completions",
                              body={"model": "other",
                                    "messages": [{"role": "user", "content": "x"}]})
    assert status == 503
    assert json.loads(data)["error"]["code"] == 50901
    status, data, _ = request(server.port, "POST", "/v1/chat/completions",
                              body={"messages": []})
    assert status == 400
    assert json.loads(data)["error"]["code"] == 40001
    status, data, _ = request(server.port, "POST", "/v1/chat/completions",
                              body=None)
    assert status == 400
    status, data, _ = request(server.port, "GET", "/v1/nope")
    assert status == 404
    assert json.loads(data)["error"]["code"] == 40401


def test_upstream_unreachable_maps_to_502():
    runtime = FakeRuntime("http://127.0.0.1:1/v1")
    server = LocalApiServer(runtime, token=TOKEN, port=0)
    server.bind()
    server.start()
    try:
        status, data, _ = request(server.port, "POST", "/v1/chat/completions",
                                  body={"model": "mock-model",
                                        "messages": [{"role": "user", "content": "x"}]})
        assert status == 502
        assert json.loads(data)["error"]["code"] == 50903
    finally:
        server.shutdown()


def test_token_is_one_per_process_and_not_on_disk(paths):
    token_a = new_token()
    token_b = new_token()
    first = LocalApiServer(FakeRuntime(), token=token_a, port=0)
    first.bind()
    first.start()
    try:
        assert request(first.port, "GET", "/v1/models", token=token_a)[0] == 200
    finally:
        first.shutdown()
    second = LocalApiServer(FakeRuntime(), token=token_b, port=0)
    second.bind()
    second.start()
    try:
        # 旧进程的令牌在新进程里无效（进程退出即失效、不落盘）
        assert request(second.port, "GET", "/v1/models", token=token_a)[0] == 401
        assert request(second.port, "GET", "/v1/models", token=token_b)[0] == 200
    finally:
        second.shutdown()
    for root, _, names in os.walk(paths.home):
        for name in names:
            try:
                content = open(os.path.join(root, name), "r",
                               encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            assert token_a not in content
            assert token_b not in content


def test_port_in_use_gives_actionable_error():
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        server = LocalApiServer(FakeRuntime(), token=TOKEN, port=port)
        with pytest.raises(Exception) as info:
            server.bind()
        assert "--port" in str(info.value)
    finally:
        holder.close()
