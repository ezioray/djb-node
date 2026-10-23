# SPDX-License-Identifier: Apache-2.0
import pytest

from djb_node import protocol
from djb_node.api import NodeApi, raise_for_response
from djb_node.errors import (AuthError, ConflictError, ForbiddenError, NetworkError,
                             RateLimitError, UsageError, VerifyError)
from djb_node.httpclient import HttpResponse
from tests.mock_m02 import CLIENT_KEY, MockServer, NODE_ID


@pytest.fixture
def server():
    item = MockServer().start()
    yield item
    item.stop()


def test_capabilities_and_deprecations(server):
    api = NodeApi(server.base_url)
    view = api.capabilities()
    assert view.protocol_version == "1.0.0"
    assert view.supports("chat_completions")
    assert api.deprecations() == []


def test_register_returns_node_and_once_key(server):
    api = NodeApi(server.base_url)
    result = api.register(hardware_fingerprint="a" * 64)
    assert result["node_id"] == NODE_ID
    assert result["client_key"] == CLIENT_KEY
    request = [item for item in server.requests
               if item["path"] == "/api/v1/node/register"][-1]
    assert request["body"]["hardware_fingerprint"] == "a" * 64
    assert "client_key" not in request["body"]


def test_heartbeat_requires_key(server):
    api = NodeApi(server.base_url)
    with pytest.raises(AuthError):
        api.heartbeat({"node_id": NODE_ID, "protocol_version": "1.0.0",
                       "policy_version": "p"})
    api = NodeApi(server.base_url, client_key=CLIENT_KEY)
    result = api.heartbeat({"node_id": NODE_ID, "protocol_version": "1.0.0",
                            "policy_version": "p"})
    assert result["next_heartbeat_seconds"] == 30
    assert server.heartbeats[-1]["node_id"] == NODE_ID


def test_heartbeat_forbidden_and_rate_limited(server):
    api = NodeApi(server.base_url, client_key=CLIENT_KEY)
    server.heartbeat_status = 403
    with pytest.raises(ForbiddenError):
        api.heartbeat({"node_id": NODE_ID, "protocol_version": "1.0.0",
                       "policy_version": "p"})
    server.heartbeat_status = 429
    server.extra_headers = {"Retry-After": "7"}
    with pytest.raises(RateLimitError) as info:
        api.heartbeat({"node_id": NODE_ID, "protocol_version": "1.0.0",
                       "policy_version": "p"})
    assert info.value.retry_after == 7


def test_policy_200_then_304(server):
    api = NodeApi(server.base_url, client_key=CLIENT_KEY)
    first = api.policy()
    assert first.not_modified is False
    assert first.payload["policy_version"] == "policy-1"
    second = api.policy(if_none_match="policy-1")
    assert second.not_modified is True
    assert second.etag == "policy-1"


def test_upstream_declare_and_probe(server):
    api = NodeApi(server.base_url, client_key=CLIENT_KEY)
    declared = api.declare_upstream({"access_type": "A2",
                                     "base_url": "http://127.0.0.1:11434/v1",
                                     "models": ["m1"],
                                     "credential_handle": "upstream:u1"})
    assert declared["state"] == "declared"
    ack = api.report_probe({"upstream_id": "up-1", "reachable": True,
                            "latency_ms": 12})
    assert ack["state"] == "verified"


def test_receipts_need_idempotency_and_key(server):
    api = NodeApi(server.base_url, client_key=CLIENT_KEY)
    receipts = [{"receipt_id": "r1"}]
    result = api.submit_receipts(receipts, idempotency_key="djb-receipts-" + "a" * 32)
    assert result["accepted"] == ["r1"]
    with pytest.raises(AuthError):
        NodeApi(server.base_url).submit_receipts(receipts, idempotency_key="k" * 16)


def test_pair_bind_unbind(server):
    api = NodeApi(server.base_url, client_key=CLIENT_KEY)
    paired = api.pair_key(NODE_ID, rotate=True, overlap_seconds=3600)
    assert paired["client_key"]
    binding = api.binding(NODE_ID)
    assert binding["bound"] is True
    assert api.bind(NODE_ID, "code-1")["bound"] is True
    assert api.unbind(NODE_ID)["bound"] is False


def test_raise_for_response_mapping():
    def response(status, code=None, reason=None, retry_after=None):
        body = {"error": {"type": "x", "code": code or 0, "message": "m",
                          "request_id": "r"}}
        if reason:
            body["error"]["reason"] = reason
        headers = {"retry-after": str(retry_after)} if retry_after else {}
        import json
        return HttpResponse(status=status, headers=headers,
                            body=json.dumps(body).encode())

    assert isinstance(raise_for_response(response(401, 40101), what="x"), AuthError)
    assert isinstance(raise_for_response(response(403, 40305), what="x"), ForbiddenError)
    assert isinstance(raise_for_response(response(409, 40919), what="x"), ConflictError)
    assert isinstance(raise_for_response(response(426, 42601), what="x"), VerifyError)
    assert isinstance(raise_for_response(response(429, 42901), what="x"), RateLimitError)
    assert isinstance(raise_for_response(response(500, 50001), what="x"), NetworkError)
    assert isinstance(raise_for_response(response(400, 40001), what="x"), UsageError)
    generic = raise_for_response(response(418), what="x")
    assert generic.code == 50001


def test_protocol_compatibility():
    assert protocol.is_protocol_compatible("1.0.0")
    assert not protocol.is_protocol_compatible("2.0.0")
    assert not protocol.is_protocol_compatible(None)
    assert not protocol.is_protocol_compatible("1.0.0", min_supported="1.1.0")
    assert protocol.version_tuple("1.2.3") == (1, 2, 3)
    assert protocol.version_tuple("x") is None
