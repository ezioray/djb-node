# SPDX-License-Identifier: Apache-2.0
import os
import socket
from datetime import timedelta

import pytest

from djb_node import facts
from djb_node.config import ClientConfig, save_config
from djb_node.daemon import ControlClient, Daemon, control_socket_file
from djb_node.errors import NotInstalledError, PortInUseError
from djb_node.limits import Limiter, Signal, YieldDetector
from djb_node.secure import CredentialStore
from djb_node.store import Store
from djb_node.util import iso, utcnow
from tests.mock_m02 import CLIENT_KEY, MockServer, NODE_ID


class FixedProbe:
    def __init__(self, state="idle", name="fake"):
        self.state = state
        self.name = name

    def read(self, now):
        return Signal(self.name, self.state, "test")


class FakeResources:
    def __init__(self, cpu=1.0, mem=1.0, gpu=None):
        self.values = {"cpu_pct": cpu, "mem_pct": mem, "gpu_pct": gpu}

    def snapshot(self):
        return dict(self.values)


@pytest.fixture
def mock():
    server = MockServer().start()
    yield server
    server.stop()


def _daemon(paths, store, credentials, mock, *, probes=None, clock=None,
            resources=None, port=0, config=None):
    clock = clock or utcnow
    config = config or ClientConfig(gateway_url=mock.base_url, mirror_port=1717)
    save_config(paths, config)
    mock.policy_issued_at = iso(clock())
    detector = YieldDetector(probes if probes is not None else [FixedProbe()])
    limiter = Limiter(store, detector=detector,
                      resources=resources or FakeResources(), now=clock)
    return Daemon(paths, config=config, store=store, credentials=credentials,
                  local_token="t" * 40, port=port, limiter=limiter,
                  yield_detector=detector, now=clock)


def _add_upstream(store, mock, secret=None, model="mock-model"):
    return store.add_upstream(upstream_id="u1", access_type="A2",
                              base_url=mock.upstream_base_url,
                              models=[{"id": model, "enabled": True,
                                       "min_price": "0.5"}],
                              credential_ref="upstream:u1")


def test_start_registers_heartbeats_and_goes_online(paths, windows_control_usable, store,
                                                    credentials, mock, clock):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    status = daemon.start()
    try:
        assert status["state"] == "online"
        assert status["accepting"] is True
        assert status["node"]["node_id"] == NODE_ID
        assert credentials.client_key() == CLIENT_KEY
        assert store.policy().policy_version == "policy-1"
        assert mock.heartbeats
        assert mock.heartbeats[-1]["capacity"][0]["model"] == "mock-model"
        clock.advance(30)
        daemon.tick(clock())
        assert mock.heartbeats[-1]["runtime_state"] == "online"
        # 本机镜像只绑回环
        assert daemon.server.address[0] == "127.0.0.1"
        assert daemon.server.address[1] > 0
        health = daemon.health()
        assert health["status"] == "ok"
        assert health["local_api"]["loopback_only"] is True
        models = daemon.models()
        assert models["data"][0]["id"] == "mock-model"
        assert daemon.admit("mock-model").ok is True
        route = daemon.route("mock-model")
        assert route is not None and route.upstream_id == "u1"
        assert daemon.admit("unknown-model").ok is False
    finally:
        daemon.stop()
    assert daemon.machine.snapshot.state == "stopped"
    assert not os.path.exists(control_socket_file(paths))


def test_start_requires_init(paths, credentials, mock):
    store = Store(paths.db_file)                 # 未 create -> 库不存在
    with pytest.raises(NotInstalledError):
        _daemon(paths, store, credentials, mock).start()


def test_control_socket_commands(windows_control_usable, paths, store, credentials, mock,
                                 clock):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        client = ControlClient(paths)
        assert client.call({"cmd": "ping"})["ok"] is True
        response = client.call({"cmd": "status"})
        assert response.get("ok") is True, response
        status = response["status"]
        assert status["state"] == "online"
        token = client.call({"cmd": "token"})["local_token"]
        assert token == daemon.token
        assert client.call({"cmd": "health"})["health"]["status"] == "ok"
        limits = client.call({"cmd": "limits"})
        assert limits["profile"]["limits"]["concurrent"] >= 1
        paused = client.call({"cmd": "pause"})["status"]
        assert paused["state"] == "paused"
        assert paused["reason"] == "user_paused"
        assert daemon.admit("mock-model").ok is False
        resumed = client.call({"cmd": "resume"})["status"]
        assert resumed["accepting"] is True
        receipt = daemon.factory.build(
            task_id="t-1", access="A2", usage_amount="100", price_amount="5",
            sig_consumer="hmac-sha256:" + "a" * 64, receipt_id="r-1")
        stored = client.call({"cmd": "receipt", "receipt": receipt})
        assert stored["stored"] is True
        assert daemon.buffer.count() == 1
        with pytest.raises(Exception):
            client.call({"cmd": "nope"})
    finally:
        daemon.stop()


def test_yield_pause_and_auto_resume(paths, store, credentials, mock, clock):
    _add_upstream(store, mock)
    probe = FixedProbe("in_use", "load")
    daemon = _daemon(paths, store, credentials, mock, probes=[probe], clock=clock)
    daemon.start()
    try:
        assert daemon.machine.snapshot.state == "paused"
        assert daemon.machine.snapshot.reason == "yield_paused"
        assert daemon.admit("mock-model").ok is False
        admission = daemon.admit("mock-model")
        assert admission.http_status == 503
        probe.state = "idle"
        clock.advance(30)
        daemon.tick(clock())
        assert daemon.machine.snapshot.state == "online"
        assert daemon.admit("mock-model").ok is True
    finally:
        daemon.stop()


def test_detection_error_pauses(paths, store, credentials, mock, clock):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock,
                     probes=[FixedProbe("error", "session")], clock=clock)
    daemon.start()
    try:
        assert daemon.machine.snapshot.reason == "yield_paused"
        assert daemon.machine.snapshot.detail.get("source") == "session"
    finally:
        daemon.stop()


def test_resource_limit_pauses(paths, store, credentials, mock, clock):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock,
                     resources=FakeResources(cpu=99.0))
    daemon.start()
    try:
        assert daemon.machine.snapshot.reason == "resource_limited"
    finally:
        daemon.stop()


def test_policy_expiry_renews_when_server_confirms_same_version(paths, store, credentials,
                                                               mock, clock):
    """D-POLICY-01：本地租约过期但**服务端仍认同一版本**时，必须续租而不是停摆。

    旧行为：只在 304 时续租。而服务端会返回 200 同版本（内容未变），
    于是 `expires_at = issued_at + TTL` 永久落在过去 ⇒ 节点永远 `paused/conservative`，
    heartbeat 却照常 200，云端看起来一切正常 —— 这是最难查的一类停摆。
    """
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        assert daemon.machine.snapshot.state == "online"
        policy = store.policy()
        policy.expires_at = "2026-10-24T00:00:30Z"
        store.save_policy(policy)
        clock.advance(120)
        daemon.tick(clock())
        assert daemon.machine.snapshot.state == "online"      # 续租成功
        assert store.policy().expires_at != "2026-10-24T00:00:30Z"
    finally:
        daemon.stop()


def test_conservative_when_policy_expires_and_cannot_refresh(paths, store, credentials,
                                                            mock, clock):
    """租约过期**且**服务端拿不到策略 ⇒ 仍然保守停单（fail-closed 不能被续租改动破坏）。"""
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        assert daemon.machine.snapshot.state == "online"
        policy = store.policy()
        policy.expires_at = "2026-10-24T00:00:30Z"
        store.save_policy(policy)
        mock.policy_status = 500                              # 服务端拒绝/不可用
        clock.advance(120)
        daemon.tick(clock())
        assert daemon.machine.snapshot.state == "paused"
        assert daemon.machine.snapshot.reason == "conservative"
        assert daemon.admit("mock-model").ok is False
    finally:
        daemon.stop()


def test_heartbeat_failures_go_conservative_then_recover(paths, store, credentials,
                                                         mock, clock):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        mock.fail_heartbeat_count = 5
        for _ in range(4):
            clock.advance(120)
            daemon.tick(clock())
        assert daemon.machine.snapshot.reason == "conservative"
        assert daemon.admit("mock-model").ok is False
        mock.fail_heartbeat_count = 0
        clock.advance(3600)
        mock.policy_version = "policy-2"
        mock.policy_issued_at = iso(clock())
        mock.policy_updated = True
        daemon.tick(clock())
        assert daemon.machine.snapshot.state == "online"
    finally:
        daemon.stop()


def test_heartbeat_forbidden_keeps_frozen(paths, store, credentials, mock, clock):
    _add_upstream(store, mock)
    mock.heartbeat_status = 403
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        assert daemon.machine.snapshot.reason == "account_frozen"
        assert "申诉" in (daemon.machine.snapshot.next_step or "")
        mock.heartbeat_status = 200
        clock.advance(3600)
        daemon.tick(clock())
        assert daemon.machine.snapshot.reason == "account_frozen"
    finally:
        daemon.stop()


def test_heartbeat_forbidden_backs_off_instead_of_busy_loop(paths, store, credentials,
                                                            mock, clock):
    """P0（2026-11-07）：403 分支原来只暂停、不推进下次心跳 → tick 循环 ~110ms 重发。

    实测证据：节点停在 pending 时 ``daemon.heartbeat_forbidden`` 一分钟刷 184 条
    （``reports/DEPLOY-20261107/evidence/client-heartbeat-storm.txt``）。
    """
    _add_upstream(store, mock)
    mock.heartbeat_status = 403
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        sent = len(mock.heartbeats)
        assert sent >= 1
        for _ in range(20):                     # 同一时刻连续 tick 20 次 = 忙等场景
            daemon.tick(clock())
        assert len(mock.heartbeats) == sent, "403 之后不许立刻重发心跳"
        # 退避下限 = facts.HEARTBEAT_MIN_S（服务端 5s 限流，客户端不得抢跑）
        clock.advance(facts.HEARTBEAT_MIN_S - 1)
        daemon.tick(clock())
        assert len(mock.heartbeats) == sent
        clock.advance(1)
        daemon.tick(clock())
        assert len(mock.heartbeats) == sent + 1
    finally:
        daemon.stop()


def test_pending_forbidden_is_pending_not_frozen(paths, store, credentials, mock, clock):
    """P0：pending（等审核）被兜底成 account_frozen，界面显示「被冻结：申诉」——误导客户。"""
    _add_upstream(store, mock)
    mock.heartbeat_status = 403
    mock.heartbeat_error_state = "pending"      # 云端 40305 的 error.state
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        snapshot = daemon.machine.snapshot
        assert snapshot.reason == "account_pending"
        assert "审核" in (snapshot.next_step or "")
        assert "申诉" not in (snapshot.next_step or "")
        assert daemon.admit("mock-model").ok is False       # 等审核期间不接单
    finally:
        daemon.stop()


def test_frozen_forbidden_still_says_frozen(paths, store, credentials, mock, clock):
    """对照：真冻结还得说冻结（新分流不能把冻结也说成审核）。"""
    _add_upstream(store, mock)
    mock.heartbeat_status = 403
    mock.heartbeat_error_state = "frozen"
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        snapshot = daemon.machine.snapshot
        assert snapshot.reason == "account_frozen"
        assert "申诉" in (snapshot.next_step or "")
    finally:
        daemon.stop()


def test_forbidden_reason_prefers_state_over_message(paths, store, credentials, mock, clock):
    """分流只看服务端状态字段；文案里恰好写着别的状态也不许盖过它。"""
    from djb_node.errors import ForbiddenError
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    pending = ForbiddenError("节点状态不允许该动作", code=40305,
                             details={"state": "pending"})
    assert daemon._forbidden_reason(pending) == "account_pending"
    retired = ForbiddenError("frozen", code=40305, details={"state": "retired"})
    assert daemon._forbidden_reason(retired) == "retired"
    # 老服务端不带 state：仍然按文案兜底，不猜
    legacy = ForbiddenError("node is frozen", code=40305)
    assert daemon._forbidden_reason(legacy) == "account_frozen"
    text_only = ForbiddenError("节点当前状态 pending 不允许 heartbeat", code=40305)
    assert daemon._forbidden_reason(text_only) == "account_pending"


def test_policy_refresh_and_directive(paths, store, credentials, mock, clock):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        mock.policy_version = "policy-2"
        mock.policy_updated = True
        clock.advance(30)
        daemon.tick(clock())
        assert store.policy().policy_version == "policy-2"
        # 服务端指令 pause_accepting -> 保守模式（不接新单）
        payload = store.get_kv("capabilities") or {}
        assert payload
        from tests.mock_m02 import policy_payload
        data = policy_payload(policy_version="policy-3")
        data["directives"] = ["pause_accepting"]
        from djb_node import policy as policy_mod
        store.save_policy(policy_mod.parse_policy(data, now=clock()))
        daemon.tick(clock())
        assert daemon.machine.snapshot.reason == "conservative"
    finally:
        daemon.stop()


def test_receipts_flush_success_and_failure(paths, store, credentials, mock, clock):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        receipt = daemon.factory.build(
            task_id="t-1", access="A2", usage_amount="100", price_amount="5",
            sig_consumer="hmac-sha256:" + "b" * 64, receipt_id="r-1")
        daemon.buffer.enqueue(receipt)
        mock.fail_receipts_count = 1
        daemon.tick(clock())
        assert daemon.buffer.count() == 1
        rows = store.receipts()
        assert rows[0].attempts == 1
        assert rows[0].next_attempt_at
        clock.advance(60)
        daemon.tick(clock())
        assert daemon.buffer.count() == 0
        assert len(mock.receipts) >= 1
    finally:
        daemon.stop()


def test_receipt_backlog_stops_accepting(paths, store, credentials, mock, clock):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        receipt = daemon.factory.build(
            task_id="t-1", access="A2", usage_amount="100", price_amount="5",
            sig_consumer="hmac-sha256:" + "c" * 64, receipt_id="r-1")
        daemon.buffer.enqueue(receipt)
        mock.fail_receipts_count = 999
        clock.advance(facts.RECEIPT_BUFFER_MAX_AGE_S + 60)
        daemon.tick(clock())
        # 超 24 h 不丢弃：标 stale + 停接单（Lead 裁决 CR-2）
        assert daemon.buffer.count() == 1
        assert daemon.buffer.stale_count() == 1
        assert daemon.machine.snapshot.reason == "receipt_backlog"
        assert daemon.admit("mock-model").ok is False
        # 网络恢复 + 策略刷新后退避重推成功 -> 自动恢复
        mock.fail_receipts_count = 0
        clock.advance(3600)
        mock.policy_version = "policy-2"
        mock.policy_issued_at = iso(clock())
        mock.policy_updated = True
        daemon.tick(clock())
        assert daemon.buffer.count() == 0
        assert daemon.machine.snapshot.state == "online"
    finally:
        daemon.stop()


def test_port_in_use_refuses_to_start(paths, store, credentials, mock):
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        daemon = _daemon(paths, store, credentials, mock, port=port)
        with pytest.raises(PortInUseError) as info:
            daemon.start()
        assert "--port" in str(info.value)
    finally:
        holder.close()


def test_heartbeat_machine_field_is_additive_and_flag_gated(paths, store, credentials,
                                                           mock, clock):
    """M3：机器画像只在开关打开时以加性字段 `machine` 出现在心跳里。"""
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        assert "machine" not in mock.heartbeats[-1]          # 默认关：字段整体不出现
        assert daemon.status()["machine"]["schema"] == "djb.machine-eval/1"
        assert daemon.status()["machine_reported"] is False
    finally:
        daemon.stop()

    enabled = ClientConfig(gateway_url=mock.base_url, mirror_port=1717,
                           extra={"feature_flags": {facts.MACHINE_PROFILE_FLAG: True}})
    daemon2 = _daemon(paths, store, credentials, mock, clock=clock, config=enabled)
    daemon2.start()
    try:
        payload = mock.heartbeats[-1]
        machine = payload.get("machine")
        assert isinstance(machine, dict)
        assert machine["schema"] == "djb.machine-eval/1"
        # 老服务端只认 capacity/load；加性字段不影响既有键
        assert payload["capacity"][0]["model"] == "mock-model"
        assert "load" in payload
        assert daemon2.status()["machine_reported"] is True
    finally:
        daemon2.stop()
