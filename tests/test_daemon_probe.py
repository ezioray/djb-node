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
        # P6：周期复探间隔一律钳在 half-fresh（默认 600 → 450），不再是裸的默认值
        assert status["probe"]["interval_s"] == facts.PROBE_PERIODIC_CAP_S
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


def test_periodic_failure_backoff_never_crosses_half_fresh(paths, store, credentials,
                                                            mock, clock):
    """失败仍然计数（退避语义在），但**间隔不得越过新鲜度的一半**（P6 / E2E-12）。

    修前这里断言 1200 / 2400 s：那正是「每周期掉 5 分钟容量 → 永久 0 容量」的根因。
    """
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock,
                     config=_flag_config(mock, True))
    cap = facts.PROBE_FRESH_S // 2
    daemon.start()
    try:
        mock.chat_status = 500                          # L1 可达、推理失败
        clock.advance(facts.PROBE_PERIODIC_DEFAULT_S + 1)
        daemon.tick(clock())
        assert daemon._probe_failures == 1
        assert daemon._probe_interval_s <= cap
        # 下一轮：失败继续计数，但间隔仍被钳住（不再翻倍到 1200/2400 s）
        clock.advance(daemon._probe_interval_s + 1)
        daemon.tick(clock())
        assert daemon._probe_failures == 2
        assert daemon._probe_interval_s <= cap
        # 上游恢复后失败计数归零，间隔仍在 half-fresh 之内
        mock.chat_status = 200
        clock.advance(daemon._probe_interval_s + 1)
        daemon.tick(clock())
        assert daemon._probe_failures == 0
        assert daemon._probe_interval_s <= cap
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
        # P6：模型数缩放与退避一起被钳在 half-fresh 之内（修前这里是 ≥ 2×默认 600）
        assert expected <= facts.PROBE_FRESH_S // 2
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


# ──────────────────────────────────────────── D-PROBE-03：周期探针必须上报
def test_startup_probe_reports_to_cloud(paths, store, credentials, mock, clock):
    """daemon 的探针结论必须上行 `POST /node/upstream/probe`。

    D-PROBE-03 现场：`report_probe` 全仓只有 `cli.py` 一个调用点，daemon 零处。
    于是本地 `last_probe_at` 新鲜（节点算得出正 QPS），云端 `verified_at` 却不动，
    `_gate_unverified(now-900s)` 15 分钟后把容量打回 0，**且只有人工 CLI 能刷新**。
    这条用例锁的是"自动恢复路径存在"这件事，不是"某个函数被调用了"。
    """
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        reports = [item for item in mock.requests
                   if item.get("path") == "/api/v1/node/upstream/probe"]
        assert reports, "启动探针没有上报到云端（D-PROBE-03 回归）"
        body = reports[-1].get("body") or {}
        # 必须带服务端 id 字段（本地 id 与服务端 id 不同，缺了会 404 NodeNotFound）
        assert body.get("upstream_id")
        # 结论字段要和本地探针同形
        assert "reachable" in body or "model_checks" in body
    finally:
        daemon.stop()


def test_periodic_probe_reports_each_round(paths, store, credentials, mock, clock):
    """周期复探打开后，**每一轮**都要上报，而不是只报启动那一次。"""
    _add_upstream(store, mock)
    config = _flag_config(mock, True)
    daemon = _daemon(paths, store, credentials, mock, clock=clock, config=config)
    daemon.start()
    try:
        before = len([item for item in mock.requests
                      if item.get("path") == "/api/v1/node/upstream/probe"])
        daemon.probe_now(reason="periodic")
        after = len([item for item in mock.requests
                     if item.get("path") == "/api/v1/node/upstream/probe"])
        assert after == before + 1, "周期探针没有上报（云端 verified_at 会过期）"
    finally:
        daemon.stop()


# ───────────────────────────────────────── D-PROBE-03：节点/云端判决口径对齐
def test_partially_working_upstream_counts_as_failed(paths, store, credentials, mock,
                                                     clock):
    """一个模型通、一个模型不通 ⇒ 必须计 failed（与云端 `任一失败即拒` 一致）。

    修前用 `any(ok)`：节点自称 verified=1、云端回 rejected（reason=第一条失败原因），
    两边日志口径不一致。本用例锁住对齐后的语义。
    """
    store.add_upstream(upstream_id="u1", access_type="A2",
                       base_url=mock.upstream_base_url,
                       models=[{"id": "probe-model", "enabled": True, "min_price": "0.5"},
                               {"id": "not-served-model", "enabled": True,
                                "min_price": "0.5"}],
                       credential_ref="upstream:u1")
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        result = daemon.probe_now(reason="manual")
        assert result["models"] == 2
        assert result["verified"] == 0
        assert result["failed"] == 1
    finally:
        daemon.stop()


def test_all_models_ok_still_counts_verified(paths, store, credentials, mock, clock):
    """全通仍计 verified（防止把 all() 写反成"永远 failed"）。"""
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        result = daemon.probe_now(reason="manual")
        assert result["verified"] == 1 and result["failed"] == 0
    finally:
        daemon.stop()
