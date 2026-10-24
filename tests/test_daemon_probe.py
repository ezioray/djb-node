# SPDX-License-Identifier: Apache-2.0
"""C7 · daemon 侧：启动校验 + 周期复探（注入时钟/间隔，不 sleep）+ 开关门控。"""
from __future__ import annotations

import pytest

from djb_node import facts
from djb_node.config import ClientConfig
from djb_node.probe import periodic_interval_s
from tests.test_daemon import _add_upstream, _daemon


@pytest.fixture
def mock():
    from tests.mock_m02 import MockServer
    server = MockServer().start()
    yield server
    server.stop()


def _flag_config(mock, enabled: bool) -> ClientConfig:
    return ClientConfig(gateway_url=mock.base_url, mirror_port=1717,
                        extra={facts.PROBE_FLAG_PERIODIC: enabled})


def _chat_count(mock) -> int:
    return len([item for item in mock.requests
                if item.get("path") == "/v1/chat/completions"])


# ─────────────────────────────────────────────────────────── 开关三字段
def test_status_exposes_flag_with_three_fields(paths, store, credentials, mock, clock):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        status = daemon.status()
        flag = status["flags"][facts.PROBE_FLAG_PERIODIC]
        assert flag["enabled"] is False                    # 默认关闭
        assert flag["owner"] == "client-seat"
        assert flag["expire_at"] == "2026-12-31"
        assert flag["rollback_rule"]
        assert status["probe"]["periodic_enabled"] is False
        assert status["probe"]["interval_s"] == facts.PROBE_PERIODIC_DEFAULT_S
        assert status["probe"]["next_at"]
    finally:
        daemon.stop()


# ─────────────────────────────────────────────────────────── 启动校验
def test_startup_probe_verifies_models_before_announcing(paths, store, credentials,
                                                         mock, clock):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        record = store.upstream("u1")
        assert record.state == "verified"
        assert record.models[0]["inference_ok"] is True
        assert record.models[0]["probe_version"] == facts.PROBE_VERSION
        assert record.last_probe_at == record.models[0]["last_probe_at"]
        # 首次心跳就带正容量（不需要人工 probe）
        assert mock.heartbeats[-1]["capacity"][0]["model"] == "mock-model"
        assert mock.heartbeats[-1]["capacity"][0]["max_qps"] > 0
        assert daemon._last_probe["reason"] == "startup"
    finally:
        daemon.stop()


def test_startup_probe_with_unreachable_upstream_still_boots(paths, store, credentials,
                                                             mock, clock):
    store.add_upstream(upstream_id="u-bad", access_type="A1",
                       base_url="http://127.0.0.1:1/v1",
                       models=[{"id": "m1", "enabled": True}], credential_ref=None)
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        assert daemon.machine.snapshot.state == "online"      # 探针失败不拖垮启动
        assert store.upstream("u-bad").state == "rejected"
        assert daemon.health()["upstream"]["announced"] == 0
        assert {item["model"]: item["max_qps"]
                for item in daemon.upstreams.announce_capacity(store.profile(),
                                                               now=clock())} == {
            "m1": 0.0}
    finally:
        daemon.stop()


# ─────────────────────────────────────────────────────────── 周期开关
def test_periodic_off_by_default_never_reprobes(paths, store, credentials, mock, clock):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        baseline = _chat_count(mock)                     # 启动校验的那一次
        assert baseline >= 1
        clock.advance(30 * 60)
        daemon.tick(clock())
        assert _chat_count(mock) == baseline             # 开关关闭：一次都不复探
        assert daemon._last_probe["reason"] == "startup"
    finally:
        daemon.stop()


def test_periodic_on_reprobes_when_due(paths, store, credentials, mock, clock):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock,
                     config=_flag_config(mock, True))
    daemon.start()
    try:
        baseline = _chat_count(mock)
        clock.advance(facts.PROBE_PERIODIC_DEFAULT_S + 1)
        daemon.tick(clock())
        assert _chat_count(mock) > baseline
        assert daemon._last_probe["reason"] == "periodic"
        assert daemon._probe_failures == 0
        assert daemon.status()["probe"]["next_at"] is not None
    finally:
        daemon.stop()


def test_periodic_failure_backs_off_exponentially(paths, store, credentials, mock, clock):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock,
                     config=_flag_config(mock, True))
    daemon.start()
    try:
        mock.chat_status = 500                          # L1 可达、推理失败
        clock.advance(facts.PROBE_PERIODIC_DEFAULT_S + 1)
        daemon.tick(clock())
        assert daemon._probe_failures == 1
        assert daemon._probe_interval_s == 2 * facts.PROBE_PERIODIC_DEFAULT_S
        # 下一轮：失败继续翻倍
        clock.advance(daemon._probe_interval_s + 1)
        daemon.tick(clock())
        assert daemon._probe_failures == 2
        assert daemon._probe_interval_s == 4 * facts.PROBE_PERIODIC_DEFAULT_S
        # 上游恢复后失败计数归零
        mock.chat_status = 200
        clock.advance(daemon._probe_interval_s + 1)
        daemon.tick(clock())
        assert daemon._probe_failures == 0
        assert daemon._probe_interval_s == facts.PROBE_PERIODIC_DEFAULT_S
    finally:
        daemon.stop()


def test_periodic_interval_scales_with_model_count(paths, store, credentials, mock,
                                                   clock):
    models = ["mock-model"] + ["m%d" % index for index in range(9)]   # 共 10 个
    store.add_upstream(upstream_id="u1", access_type="A2",
                       base_url=mock.upstream_base_url,
                       models=[{"id": name, "enabled": True} for name in models],
                       credential_ref="upstream:u1")
    daemon = _daemon(paths, store, credentials, mock, clock=clock,
                     config=_flag_config(mock, True))
    daemon.start()
    try:
        clock.advance(facts.PROBE_PERIODIC_DEFAULT_S * 2 + 1)
        daemon.tick(clock())
        expected = periodic_interval_s(10, daemon._probe_failures)
        assert daemon._probe_interval_s == expected
        assert expected >= 2 * facts.PROBE_PERIODIC_DEFAULT_S
    finally:
        daemon.stop()


def test_failed_reprobe_zeroes_announced_capacity(paths, store, credentials, mock, clock):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock,
                     config=_flag_config(mock, True))
    daemon.start()
    try:
        assert mock.heartbeats[-1]["capacity"][0]["max_qps"] > 0
        mock.chat_status = 500
        clock.advance(facts.PROBE_PERIODIC_DEFAULT_S + 1)
        daemon.tick(clock())
        assert store.upstream("u1").state == "rejected"
        assert mock.heartbeats[-1]["capacity"][0]["max_qps"] == 0.0
    finally:
        daemon.stop()


def test_flag_off_keeps_manual_probe_path(paths, store, credentials, mock, clock):
    """回滚规则：开关关闭 → 只保留手动 probe，公告按已存 last_probe_at 判新鲜。"""
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        assert daemon.status()["probe"]["periodic_enabled"] is False
        clock.advance(16 * 60)                           # 超过 15 min 新鲜窗口
        daemon.tick(clock())                             # 心跳刷新容量：已过期
        assert mock.heartbeats[-1]["capacity"][0]["max_qps"] == 0.0
        result = daemon.probe_now(reason="manual")       # 手动 probe 仍可用
        assert result["verified"] == 1
        clock.advance(31)                                # 等下一次心跳
        daemon.tick(clock())
        assert mock.heartbeats[-1]["capacity"][0]["max_qps"] > 0
    finally:
        daemon.stop()


# ─────────────────────────────────────────────────────────── 隐私 / 可见性
def test_probe_never_logs_prompt_or_upstream_content(paths, store, credentials, mock,
                                                     clock):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        clock.advance(30)
        daemon.tick(clock())
        with open(paths.log_file, "r", encoding="utf-8") as handle:
            log = handle.read()
        assert facts.PROBE_PROMPT not in log            # 不落合成 prompt
        assert "你好" not in log                        # 不落上游响应正文
        assert "Bearer" not in log                      # 不落凭据
    finally:
        daemon.stop()


def test_health_and_status_report_probe_visibility(paths, store, credentials, mock,
                                                   clock):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        health = daemon.health()
        assert health["upstream"]["verified_models"] == 1
        assert health["upstream"]["announced"] == 1
        assert health["upstream"]["stale_models"] == 0
        clock.advance(facts.PROBE_FRESH_S + 1)
        health = daemon.health()
        assert health["upstream"]["stale_models"] == 1
        assert health["upstream"]["announced"] == 0
        details = daemon.status()["probe"]["models"]
        assert details[0]["model"] == "mock-model"
        assert details[0]["inference_ok"] is True
    finally:
        daemon.stop()
