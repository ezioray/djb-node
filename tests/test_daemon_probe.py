# SPDX-License-Identifier: Apache-2.0
"""C7 · daemon 侧：启动校验 + 周期复探（注入时钟/间隔，不 sleep）+ 开关门控。"""
from __future__ import annotations

import pytest

from djb_node import facts
from djb_node.config import ClientConfig, feature_flag, flag_env_name
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


def _due(daemon, clock) -> None:
    """把注入时钟推到日计划的「下一轮」之后一格（CHG-20261121-PROBE-DAILY）。

    修前各处直接 ``advance(PROBE_PERIODIC_CAP_S + 1)``；日探针下这个数（450 s）已经不是
    「一个周期」，下一轮由落盘计划给出 —— 测试跟着计划走，才不会把常量抄第二遍。
    """
    clock.advance(max(0.0, (daemon._next_probe_at - clock()).total_seconds()) + 1)


# ─────────────────────────────────────────────────────────── 开关三字段（C-01b）
def _clean_config(mock) -> ClientConfig:
    """干净 home 的配置：**不写** feature_flags，走登记表默认值（真机客户就是这个形态）。"""
    return ClientConfig(gateway_url=mock.base_url, mirror_port=1717)


def test_status_exposes_flag_with_three_fields(paths, store, credentials, mock, clock):
    """C-01b 路径 1/3：**默认开**（没有显式配置、没有环境变量）。"""
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock,
                     config=_clean_config(mock))
    daemon.start()
    try:
        status = daemon.status()
        flag = status["flags"][facts.PROBE_FLAG_PERIODIC]
        assert flag["enabled"] is True                      # 0.2.5 起默认开
        assert flag["owner"] == "client-seat"
        assert flag["expire_at"] == "2027-06-30T00:00:00+08:00"
        assert flag["rollback_rule"]
        assert status["probe"]["periodic_enabled"] is True
        # CHG-20261121-PROBE-DAILY：周期从 half-fresh(450 s) 改成**一天**，
        # 间隔 == cap == 86400，且**永不越过 cap**（显示值与上限仍是同一条不变式）。
        assert facts.PROBE_PERIODIC_CAP_S == facts.PROBE_DAILY_INTERVAL_S
        assert status["probe"]["interval_s"] == facts.PROBE_PERIODIC_CAP_S
        assert status["probe"]["cap_s"] == facts.PROBE_PERIODIC_CAP_S
        assert status["probe"]["next_at"]
        # 日计划可见：固定时刻 + 当天已用轮次（「一天只探一次」可审计）
        daily = status["probe"]["daily"]
        assert 0 <= daily["slot_offset_s"] < 24 * 3600
        assert daily["max_attempts"] == facts.PROBE_DAILY_MAX_ATTEMPTS
    finally:
        daemon.stop()


def test_default_on_reprobes_without_any_flag_setting(paths, store, credentials, mock,
                                                      clock):
    """C-01b 路径 1/3 的行为面：干净 home 下到点就自己复探（不再靠人手动 probe）。"""
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock,
                     config=_clean_config(mock))
    daemon.start()
    try:
        baseline = _chat_count(mock)
        _due(daemon, clock)                                  # 到日计划的下一轮
        daemon.tick(clock())
        # 日探针：**一个链接一天一轮、一轮一个模型** ⇒ 恰好 +1 次推理请求
        assert _chat_count(mock) == baseline + 1
        assert daemon._last_probe["reason"] == "periodic"
    finally:
        daemon.stop()


def test_explicit_config_disable_still_wins(paths, store, credentials, mock, clock):
    """C-01b 路径 2/3：配置层显式关（`djb flags --disable` 写的就是这个值）。"""
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock,
                     config=_flag_config(mock, False))
    daemon.start()
    try:
        baseline = _chat_count(mock)
        clock.advance(facts.PROBE_PERIODIC_CAP_S * 4)
        daemon.tick(clock())
        assert _chat_count(mock) == baseline
        assert daemon.status()["probe"]["periodic_enabled"] is False
    finally:
        daemon.stop()


def test_env_disable_wins_over_default(paths, store, credentials, mock, clock,
                                       monkeypatch):
    """C-01b 路径 2/3（环境变量）：`DJB_FLAG_CLIENT_PROBE_PERIODIC_ENABLED=false`。

    回滚规则写的就是这条 —— 事故现场不必改配置文件、不必发版。
    """
    monkeypatch.setenv(flag_env_name(facts.PROBE_FLAG_PERIODIC), "false")
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock,
                     config=_clean_config(mock))
    daemon.start()
    try:
        baseline = _chat_count(mock)
        clock.advance(facts.PROBE_PERIODIC_CAP_S * 4)
        daemon.tick(clock())
        assert _chat_count(mock) == baseline
        assert daemon.status()["probe"]["periodic_enabled"] is False
    finally:
        daemon.stop()


def test_illegal_flag_value_falls_back_to_safe_side(paths, store, credentials, mock, clock):
    """C-01b 路径 3/3：配置里写错值时不猜、不崩，落到**安全侧**（关）。

    口径沿用 C7 既有语义（`config.feature_flag`）：只有 bool 真值 / `_TRUE_TEXT` 里的文本
    算开，其余文本一律关。默认开之后这条尤其要紧 —— 不能因为配置里手滑写了 `maybe`
    就把周期复探打开，也不能因此让 daemon 抛异常。
    """
    for value, expected in (("maybe", False), ("off", False), ("  ", False),
                            (0, False), (1, True), (None, True)):
        config = ClientConfig()
        config.extra = {} if value is None else {facts.PROBE_FLAG_PERIODIC: value}
        assert feature_flag(config, facts.PROBE_FLAG_PERIODIC) is expected, value
    # daemon 侧只验一条（每 start 一次都要建 mock/端口，别把用例拖成几十秒）：
    # 非法值既不让 daemon 崩，也不会偷偷把周期复探打开。
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock,
                     config=ClientConfig(gateway_url=mock.base_url, mirror_port=1717,
                                         extra={facts.PROBE_FLAG_PERIODIC: "maybe"}))
    daemon.start()
    try:
        assert daemon.status()["probe"]["periodic_enabled"] is False
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
def test_periodic_off_never_reprobes(paths, store, credentials, mock, clock):
    """关掉时行为与改前完全一致：只有启动校验那一次探针。"""
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock,
                     config=_flag_config(mock, False))
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
        _due(daemon, clock)
        daemon.tick(clock())
        assert _chat_count(mock) == baseline + 1
        assert daemon._last_probe["reason"] == "periodic"
        assert daemon._probe_failures == 0
        assert daemon.status()["probe"]["next_at"] is not None
    finally:
        daemon.stop()


def test_failure_retries_once_per_day_then_waits_for_next_day(paths, store, credentials,
                                                             mock, clock):
    """失败**不缩到分钟级**：当日只补 1 次（默认 1 h 后），之后等次日（票面不变量）。

    CHG-20261121-PROBE-DAILY 之前这里断言的是「指数退避但不越过 half-fresh」——
    那正是额度燃烧的另一半：450 s 周期下 1200 / 2400 s 的重试仍落在**同一天**，
    一轮失败当天就能再打十几次推理请求。日探针的失败处理只有「今天补一次、明天见」。
    """
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock,
                     config=_flag_config(mock, True))
    daemon.start()
    try:
        mock.chat_status = 500                          # L1 可达、推理失败
        _due(daemon, clock)
        daemon.tick(clock())
        assert daemon._probe_failures == 1
        plan = daemon.status()["probe"]["daily"]
        assert plan["next_kind"] == "retry" and plan["attempts"] == 1
        # 重试排在 1 h 后（既不是 5 s 空转，也不是下一个分钟级周期），且**仍当天**
        delta = (daemon._next_probe_at - clock()).total_seconds()
        assert abs(delta - facts.PROBE_DAILY_RETRY_DELAY_S) < 2
        assert daemon._next_probe_at.date() == clock().date()
        # 到点重试：当天额度（2）用尽 ⇒ 下一轮直接排到次日同一时刻
        clock.advance(delta + 1)
        daemon.tick(clock())
        assert daemon._probe_failures == 2
        assert daemon.status()["probe"]["daily"]["next_kind"] == "daily"
        assert daemon._next_probe_at.date() > clock().date()
        # 当天不再有第三轮（把时钟再推 3 h 也不会发请求）
        settled = _chat_count(mock)
        clock.advance(3 * 3600)
        daemon.tick(clock())
        assert _chat_count(mock) == settled
        # 次日恢复：失败计数归零（正常轮次）
        mock.chat_status = 200
        _due(daemon, clock)
        daemon.tick(clock())
        assert daemon._probe_failures == 0
        assert daemon.status()["probe"]["daily"]["attempts"] == 1
    finally:
        daemon.stop()


def test_interval_is_one_day_and_one_model_per_round(paths, store, credentials, mock,
                                                     clock):
    """日探针两条不变量：间隔恒为一天；**模型再多，一轮也只有 1 次推理请求**。

    修前这条锁的是「10 个模型 ⇒ 间隔按 PROBE_MODELS_PER_STEP 放大」：模型越多越稀疏，
    但 450 s 基数下仍是分钟级请求量（10 个模型 × 每天 192 轮 ≈ 1920 请求/天），
    正是额度被打空的直接原因。现在稀疏度体现在**跨天轮换**，不体现在日内间隔。
    """
    models = ["mock-model"] + ["m%d" % index for index in range(9)]   # 共 10 个
    store.add_upstream(upstream_id="u1", access_type="A2",
                       base_url=mock.upstream_base_url,
                       models=[{"id": name, "enabled": True} for name in models],
                       credential_ref="upstream:u1")
    daemon = _daemon(paths, store, credentials, mock, clock=clock,
                     config=_flag_config(mock, True))
    daemon.start()
    try:
        assert periodic_interval_s(10, 0) == periodic_interval_s(1, 0) \
            == facts.PROBE_DAILY_INTERVAL_S
        assert periodic_interval_s(10, 3) == facts.PROBE_DAILY_INTERVAL_S  # 失败不再退避
        baseline = _chat_count(mock)
        _due(daemon, clock)
        daemon.tick(clock())
        assert _chat_count(mock) == baseline + 1, "10 个模型 enabled 也只许 1 次推理请求"
        # 另外 9 个模型按日期轮换没探 ⇒ **不是** backlog（记 budget 会 5 s 续跑，
        # 又把额度烧回去）。轮换原因串在 tests/test_probe_daily.py 里对 not_probed 断言。
        assert daemon._last_probe["backlog"] == 0, "轮换不是 backlog（不许 5 s 续跑）"
        assert daemon._last_probe["probed"] == 1
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
        _due(daemon, clock)
        daemon.tick(clock())
        assert store.upstream("u1").state == "rejected"
        assert mock.heartbeats[-1]["capacity"][0]["max_qps"] == 0.0
    finally:
        daemon.stop()


def test_flag_off_keeps_manual_probe_path(paths, store, credentials, mock, clock):
    """回滚规则：开关关闭 → 只保留手动 probe，公告按已存 last_probe_at 判新鲜。"""
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock,
                     config=_flag_config(mock, False))
    daemon.start()
    try:
        assert daemon.status()["probe"]["periodic_enabled"] is False
        # CHG-20261121-PROBE-DAILY：本地新鲜窗口同步放到 25 h（云端同值），
        # 所以「过期」不能再拿 15 min 当界；越过窗口才该掉容量。
        clock.advance(facts.PROBE_FRESH_S + 60)
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


# ───────────────────────────── RC14 §3-B.1：逐模型判定（取代 D-PROBE-03 的 all()）
def test_partially_working_upstream_counts_as_verified(paths, store, credentials, mock,
                                                       clock):
    """一个模型通、一个模型不通 ⇒ 仍计 verified（**这是 owner 2026-11-14 的裁决**）。

    历史：D-PROBE-03（2026-11-04）曾把这里对齐成「任一失败即 failed」，理由是节点与
    云端日志口径要一致。W6 现场（6/7 模型可用、整条上游 rejected、容量归零）证明那条
    对齐会**放大故障**：一个模型下架就能拖死整条上游。RC14 起判定改为
    「至少一个 enabled 模型通过即 ok」，逐模型结论进 ``per_upstream[].per_model``，
    云端侧同步改成按 enabled 集合判定（契约 1.14.0 的 ``verdict_scope=enabled_only``）。
    """
    mock.models = ["probe-model"]                 # 上游只列 probe-model（另一个是下架模型）
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
        assert result["verified"] == 1
        assert result["failed"] == 0
        # 逐模型结论必须留在 last_pass 里（桌面「为什么没通过」的唯一来源之一）
        detail = result["per_upstream"][0]
        assert detail["ok"] is True and detail["passed"] == 1 and detail["total"] == 2
        per_model = {item["model"]: item for item in detail["per_model"]}
        assert per_model["probe-model"]["ok"] is True
        assert per_model["not-served-model"]["ok"] is False
        assert per_model["not-served-model"]["error_class"] == "model_not_listed"
    finally:
        daemon.stop()


def test_no_working_model_still_counts_failed(paths, store, credentials, mock, clock):
    """一个都不通 ⇒ failed（新口径的边界：至少一个通过才算 ok，不许无条件放行）。"""
    store.add_upstream(upstream_id="u1", access_type="A2",
                       base_url=mock.upstream_base_url,
                       models=[{"id": "not-served-a", "enabled": True, "min_price": "0.5"},
                               {"id": "not-served-b", "enabled": True,
                                "min_price": "0.5"}],
                       credential_ref="upstream:u1")
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    daemon.start()
    try:
        result = daemon.probe_now(reason="manual")
        assert result["verified"] == 0 and result["failed"] == 1
        assert result["per_upstream"][0]["passed"] == 0
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
