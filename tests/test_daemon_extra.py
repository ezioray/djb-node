# SPDX-License-Identifier: Apache-2.0
"""daemon 降级 / 恢复 / 边界路径（离线、注册失败、策略 304/非法、生命周期）。"""
from __future__ import annotations

import pytest

from djb_node import facts, policy as policy_mod
from djb_node.config import ClientConfig, save_config
from djb_node.daemon import Daemon, main as daemon_main
from djb_node.errors import UsageError
from djb_node.limits import Limiter, Signal, YieldDetector
from djb_node.store import Store
from djb_node.util import iso
from tests.mock_m02 import MockServer
from tests.test_daemon import FakeResources, FixedProbe, _add_upstream, _daemon


def test_capabilities_failure_still_online(paths, store, credentials, mock, clock):
    _add_upstream(store, mock)
    mock.capabilities_status = 500
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        assert daemon.machine.snapshot.state == "online"
        assert store.get_kv("capabilities") is None
    finally:
        daemon.stop()


def test_protocol_incompatible_refuses(paths, store, credentials, mock, clock):
    _add_upstream(store, mock)
    mock.capabilities_override = {"protocol_version": "2.0.0",
                                  "min_supported_version": "2.0.0",
                                  "capabilities": [], "server_time": "2026-10-24T00:00:00Z"}
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        assert daemon.machine.snapshot.state == "error"
        assert "升级客户端" in (daemon.machine.snapshot.next_step or "")
    finally:
        daemon.stop()


def test_register_without_key_requires_manual_key(paths, store, credentials, mock, clock):
    _add_upstream(store, mock)
    mock.register_returns_key = False
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        assert daemon.machine.snapshot.reason == "credential_invalid"
        assert "--set-key" in (daemon.machine.snapshot.next_step or "")
    finally:
        daemon.stop()


def test_register_fingerprint_conflict_gives_actionable_guidance(paths, store, credentials,
                                                                 mock, clock):
    """P5（CR-20261106-OFFICIAL-SUPPLY-01）：同指纹已注册（云侧 40903）→ 可读指引 + 不重试。

    一机一节点的第一现场就在这里：冲突必须变成「怎么办」，而不是一个抽象 409；而且
    这是永久错误，自动重试只会把日志刷成噪声（也不能靠重试拿到别人的身份）。
    """
    _add_upstream(store, mock)
    mock.register_conflict_code = facts.REGISTER_CONFLICT_CODE
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        snapshot = daemon.machine.snapshot
        assert snapshot.state == "error"
        assert snapshot.detail.get("register_conflict") is True
        assert snapshot.detail.get("code") == facts.REGISTER_CONFLICT_CODE
        assert snapshot.detail.get("reason_code") == "fingerprint_bound"
        next_step = snapshot.next_step or ""
        # 指引必须可执行：复用密钥 / 绑定归属 / 加机器，三选一都在
        for token in ("硬件指纹", "一台机器只跑一个节点", "--set-key", "--bind", "加机器"):
            assert token in next_step, token
        # 服务端原话保留（云侧 INV-01 的「原账户解绑」不能说丢就丢）
        assert "已绑定账户" in next_step
        # 冲突时绝不落密钥：否则等于凭指纹接管别人的节点
        assert not credentials.client_key()
        # fail-closed：同一冲突只发生 1 次注册请求，推进时钟也不再来一遍
        def registrations() -> int:
            return len([item for item in mock.requests
                        if item["path"] == "/api/v1/node/register"])
        assert registrations() == 1
        clock.advance(600)
        daemon.tick(clock())
        # 再推进一个心跳周期也不会重打注册（永久冲突不靠重试解决）
        assert registrations() == 1
        # 没有密钥就不可能接单：无论停在 error 还是后续的 paused，都不能是 online
        assert daemon.machine.snapshot.state != "online"
        assert not credentials.client_key()
    finally:
        daemon.stop()


def test_register_conflict_without_fingerprint_code_still_guides(paths, store, credentials,
                                                                 mock, clock):
    """非 40903 的 409（其它状态冲突）也走同一条可读指引，但不冒充指纹冲突。"""
    _add_upstream(store, mock)
    mock.register_conflict_code = 40919
    mock.register_conflict_message = "节点状态不允许该动作"
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        snapshot = daemon.machine.snapshot
        assert snapshot.state == "error"
        assert snapshot.detail.get("register_conflict") is True
        assert snapshot.detail.get("code") == 40919
        assert "reason_code" not in snapshot.detail
        assert "节点状态不允许该动作" in (snapshot.next_step or "")
        assert "一台机器只跑一个节点" in (snapshot.next_step or "")
    finally:
        daemon.stop()


def test_offline_bootstrap_then_recover(paths, store, credentials, mock, clock):
    _add_upstream(store, mock)
    config = ClientConfig(gateway_url="http://127.0.0.1:1", mirror_port=1717)
    save_config(paths, config)
    mock.policy_issued_at = iso(clock())
    detector = YieldDetector([FixedProbe()])
    limiter = Limiter(store, detector=detector, resources=FakeResources(), now=clock)
    daemon = Daemon(paths, config=config, store=store, credentials=credentials,
                    local_token="t" * 40, port=0, limiter=limiter,
                    yield_detector=detector, now=clock)
    daemon.start()
    try:
        assert daemon.machine.snapshot.state == "paused"
        assert daemon.machine.snapshot.reason == "conservative"
        # 网络恢复：改配置后 tick 会自动重试注册 + 心跳 + 策略
        daemon.config.gateway_url = mock.base_url
        daemon.api = None
        clock.advance(5)
        daemon.tick(clock())
        assert daemon.credentials.client_key()
        assert daemon.machine.snapshot.state == "online"
    finally:
        daemon.stop()


def test_register_retry_after_transient_failure(paths, store, credentials, mock, clock):
    _add_upstream(store, mock)
    mock.fail_register_count = 999
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        assert daemon.machine.snapshot.reason == "conservative"
        mock.fail_register_count = 0
        clock.advance(5)
        daemon.tick(clock())
        assert daemon.credentials.client_key()
        assert daemon.machine.snapshot.state == "online"
    finally:
        daemon.stop()


def test_policy_304_lease_renewal(paths, store, credentials, mock, clock):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        policy = store.policy()
        policy.expires_at = iso(clock())
        store.save_policy(policy)
        mock.policy_updated = True
        clock.advance(30)
        daemon.tick(clock())
        assert store.policy().expires_at > iso(clock())
        assert daemon.machine.snapshot.state == "online"
    finally:
        daemon.stop()


def test_invalid_policy_goes_conservative(paths, store, credentials, mock, clock):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        mock.policy_version = "bad-2"
        mock.policy_override = {"policy_version": "bad-2", "protocol_version": "1.0.0",
                                "issued_at": iso(clock()), "digest": "not-a-digest"}
        mock.policy_updated = True
        clock.advance(30)
        daemon.tick(clock())
        assert daemon.machine.snapshot.reason == "conservative"
        assert daemon.admit("mock-model").ok is False
    finally:
        daemon.stop()


def test_start_twice_and_control_receipt_validation(windows_control_usable, paths, store,
                                                   credentials, mock, clock):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        again = daemon.start()
        assert again["state"] == "online"
        with pytest.raises(UsageError):
            daemon.control({"cmd": "receipt", "receipt": {}})
        with pytest.raises(UsageError):
            daemon.control({"cmd": "receipt", "receipt": "x"})
    finally:
        daemon.stop()


def test_run_max_cycles_and_main_entry(paths, store, credentials, mock, clock):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    assert daemon.run(max_cycles=1) == 0
    assert daemon.machine.snapshot.state == "stopped"
    # 进程入口（--max-cycles 便于测试；不阻塞）
    code = daemon_main(["--home", str(paths.home), "--port", "0", "--max-cycles", "1"])
    assert code == 0
    store.close()
