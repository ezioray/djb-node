# SPDX-License-Identifier: Apache-2.0
"""CHG-20261121-PROBE-DAILY：日探针（每链接每天 1 次 × 1 个代表模型）+ 链接级覆盖门控。

口径来源：`changes/CHG-20261121-PROBE-DAILY.md`（owner 2026-11-21 裁决「纯日探针 +
云端新鲜度放到 25h」）、`changes/CHG-20261121-PROBE-QUOTA-BURN.md`（额度打空的根因）。

本文件锁六件事（其余既有用例仍在 test_daemon_probe / test_probe_budget / test_probe_l2）：

1. **时刻**：一天一轮、跨天同一时刻；首启时刻 + ≤30 min 稳定抖动；两轮间隔 < 25 h 新鲜窗；
2. **跨重启**：计划落盘 ⇒ 重启**不重复探**、**不换时刻**（修前每次 start 都无条件探一轮）；
3. **每轮 1 个模型**：3 个 enabled 模型也只发 1 次推理请求；没轮到的记 ``rotation``；
4. **失败/backlog**：当日只补 1 次（1 h），backlog 续跑也只 1 次（不许 5 s 空转）；
5. **手动不受限**：`probe_now()` 随时可用，且**不动**日计划；
6. **覆盖门控**：24 h 内仍算容量、>25 h 归零、模型自己 >7 天不再借上游健康续命。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from djb_node import facts
from djb_node import probe as probe_mod
from djb_node.config import ClientConfig
from djb_node.quotes import SKIP_NOT_PROBED, SKIP_STALE, plan_quotes
from djb_node.upstream import ModelEntry, UpstreamManager, covered
from tests.test_daemon import _add_upstream, _daemon


@pytest.fixture
def mock():
    from tests.mock_m02 import MockServer
    server = MockServer().start()
    yield server
    server.stop()


def _chat_count(mock) -> int:
    return len([item for item in mock.requests
                if item.get("path") == "/v1/chat/completions"])


def _chatted_models(mock) -> list[str]:
    """真正被打过 ``/chat/completions`` 的模型（**请求级**证据，不是推理出来的计数）。"""
    return [str((item.get("body") or {}).get("model")) for item in mock.requests
            if item.get("path") == "/v1/chat/completions"]


def _config(mock, periodic: bool = True) -> ClientConfig:
    return ClientConfig(gateway_url=mock.base_url, mirror_port=1717,
                        extra={facts.PROBE_FLAG_PERIODIC: periodic})


def _due(daemon, clock) -> None:
    """把注入时钟推到日计划的下一轮之后一格。"""
    clock.advance(max(0.0, (daemon._next_probe_at - clock()).total_seconds()) + 1)


# ─────────────────────────────────────────────────────── 1) 时刻：固定、可预测、在窗内
def test_daily_slot_is_stable_and_two_rounds_fit_in_fresh_window():
    """槽位稳定（同一份材料两次算出同一个时刻）+ 相邻两轮 < 25 h。"""
    moment = datetime(2026, 11, 21, 9, 15, tzinfo=timezone.utc)
    offset = probe_mod.daily_slot_offset(moment, "u1|u2")
    assert 0 <= offset < 24 * 3600
    assert offset == probe_mod.daily_slot_offset(moment, "u1|u2"), \
        "时刻必须可复现（sha256 派生，不是每轮重抽）"
    first = probe_mod.next_daily_at(moment, offset)
    second = probe_mod.next_daily_at(first, offset)
    gap = (first - moment).total_seconds()
    # 首启那次已经探过，所以第一个**计划轮**在次日同一时刻 + 抖动
    assert 24 * 3600 <= gap < 24 * 3600 + facts.PROBE_DAILY_JITTER_S
    assert gap < facts.PROBE_FRESH_S, "间隔必须落在新鲜窗内，否则一轮失败就永久掉容量"
    assert (second - first).total_seconds() == 24 * 3600, "跨天同一时刻"
    assert probe_mod.seconds_of_day(second) == offset


def test_daily_slot_never_schedules_a_second_round_same_day():
    """首启在深夜（槽位回绕到凌晨）也**不会**当天再探一轮（最小间隔 20 h 兜底）。"""
    moment = datetime(2026, 11, 21, 23, 50, tzinfo=timezone.utc)
    for material in ("u1", "u2", "u3", "u4", "u5", "u6"):
        offset = probe_mod.daily_slot_offset(moment, material)
        nxt = probe_mod.next_daily_at(moment, offset)
        assert (nxt - moment).total_seconds() >= facts.PROBE_DAILY_MIN_GAP_S
        assert (nxt - moment).total_seconds() < facts.PROBE_FRESH_S
        assert nxt.date() > moment.date()


def test_daily_pick_rotates_by_day_and_is_stable_within_a_day():
    models = ["b-model", "a-model", "c-model"]
    picks = [probe_mod.pick_daily_model(models, day) for day in range(6)]
    assert picks == ["a-model", "b-model", "c-model", "a-model", "b-model", "c-model"]
    # 同一天内重复调用（重试/重启）必须选同一个代表模型
    assert probe_mod.pick_daily_model(models, 7) == probe_mod.pick_daily_model(
        list(reversed(models)), 7)
    assert probe_mod.pick_daily_model([], 3) is None


# ─────────────────────────────────────── 2) 每轮只探 1 个模型 + rotation 原因
def test_round_probes_exactly_one_model_and_marks_the_rest_rotation(
        paths, store, credentials, mock, clock):
    """验收口径：**请求数 = 链接数**；没轮到的模型记 ``rotation``（不是 ``budget``）。"""
    mock.models = ["a-model", "b-model", "c-model"]
    store.add_upstream(upstream_id="u1", access_type="A2",
                       base_url=mock.upstream_base_url,
                       models=[{"id": name, "enabled": True, "min_price": "0.5"}
                               for name in mock.models],
                       credential_ref="upstream:u1")
    manager = UpstreamManager(store, credentials)
    day = probe_mod.day_index(clock())
    payload = manager.probe("u1", now=clock(), level="L2", daily_day=day)

    picked = probe_mod.pick_daily_model(mock.models, day)
    assert _chatted_models(mock) == [picked], "只许打代表模型那一次推理"
    assert _chat_count(mock) == 1, "3 个 enabled 模型也只许 1 次推理请求"
    # L1 仍覆盖全部 enabled 模型（云端按 models[] 判「至少一个通过」）
    assert sorted(payload["models"]) == sorted(mock.models)
    reasons = {item["model"]: item["reason"] for item in payload["not_probed"]}
    assert reasons == {model: "rotation" for model in mock.models if model != picked}
    assert store.upstream("u1").state == "verified"

    # 次日换一个代表模型（同一天重试不换：上面的 day 传同一个值两次结果一致）
    other = manager.probe("u1", now=clock() + timedelta(days=1), level="L2",
                          daily_day=day + 1)
    assert other["model_checks"], "次日仍只探 1 个代表模型"
    assert _chatted_models(mock)[1] == probe_mod.pick_daily_model(mock.models, day + 1)
    assert _chatted_models(mock)[0] != _chatted_models(mock)[1], "跨天必须换代表模型"


def test_manual_probe_without_daily_day_still_covers_every_model(paths, store, credentials,
                                                                mock, clock):
    """``djb upstream probe``（daily_day=None）保持旧行为：全模型实探、随时可用。"""
    mock.models = ["a-model", "b-model", "c-model"]
    store.add_upstream(upstream_id="u1", access_type="A2",
                       base_url=mock.upstream_base_url,
                       models=[{"id": name, "enabled": True} for name in mock.models],
                       credential_ref="upstream:u1")
    manager = UpstreamManager(store, credentials)
    payload = manager.probe("u1", now=clock(), level="L2")
    assert sorted(_chatted_models(mock)) == sorted(mock.models), "手动路径仍全模型实探"
    assert payload["not_probed"] == []


# ─────────────────────────────────────────────────── 3) 跨重启：不重复探、不换时刻
def test_restart_keeps_the_plan_and_does_not_reprobe(paths, store, credentials, mock,
                                                     clock):
    """票面不变量：同一天内重启**不换时刻、不再探**（修前每次 start 都探一轮）。"""
    _add_upstream(store, mock)
    first = _daemon(paths, store, credentials, mock, clock=clock, config=_config(mock))
    first.start()
    try:
        plan = dict(first._probe_plan)
        next_at = first._next_probe_at
        baseline = _chat_count(mock)
        assert baseline >= 1, "首次安装没有计划 ⇒ 立刻做启动校验（容量不留空窗）"
    finally:
        first.stop()

    clock.advance(10 * 60)                                   # 同一天内重启
    second = _daemon(paths, store, credentials, mock, clock=clock, config=_config(mock))
    second.start()
    try:
        assert _chat_count(mock) == baseline, "重启不许再探一轮（额度燃烧的进程侧根因）"
        assert second._next_probe_at == next_at, "重启不许换时刻"
        assert second._probe_plan["slot_offset_s"] == plan["slot_offset_s"]
        assert second._probe_plan["day"] == plan["day"]
        assert second._probe_plan["attempts"] == plan["attempts"]
        assert second.status()["probe"]["daily"]["slot_offset_s"] == plan["slot_offset_s"]
    finally:
        second.stop()


def test_restart_after_due_time_probes_immediately(paths, store, credentials, mock, clock):
    """到了下一轮的点才因为重启补探（补探也算当天那一轮，不额外加轮）。"""
    _add_upstream(store, mock)
    first = _daemon(paths, store, credentials, mock, clock=clock, config=_config(mock))
    first.start()
    try:
        baseline = _chat_count(mock)
        plan = dict(first._probe_plan)
    finally:
        first.stop()

    clock.advance((first._next_probe_at - clock()).total_seconds() + 1)
    second = _daemon(paths, store, credentials, mock, clock=clock, config=_config(mock))
    second.start()
    try:
        assert _chat_count(mock) == baseline + 1
        assert second._last_probe["reason"] == "startup"
        assert second._probe_plan["day"] != plan["day"]
        assert second._probe_plan["attempts"] == 1, "补探就是当天那一轮，不叠加"
    finally:
        second.stop()


# ────────────────────────────────────────────────────── 4) 手动：随时可用、不动计划
def test_manual_probe_is_always_available_and_never_moves_the_plan(
        paths, store, credentials, mock, clock):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock, config=_config(mock))
    daemon.start()
    try:
        next_at, plan = daemon._next_probe_at, dict(daemon._probe_plan)
        baseline = _chat_count(mock)
        clock.advance(60)                                    # 远没到日计划
        result = daemon.probe_now()                          # 手动补探：立刻可用
        assert result["verified"] == 1 and result["scheduled"] is False
        assert _chat_count(mock) == baseline + 1
        assert daemon._next_probe_at == next_at, "手动不许把日计划提前/推后"
        assert daemon._probe_plan["attempts"] == plan["attempts"], "手动不吃当日重试额度"
        assert daemon._probe_plan["next_kind"] == plan["next_kind"]
    finally:
        daemon.stop()


# ───────────────────────────────────────────── 5) 覆盖门控：24 h 在、25 h 归零、7 天上限
def _seed_verified(store, mock, *, models=("mock-model",), last_probe_at=None,
                   record_state="verified", record_last_probe_at=None):
    stored = []
    for name in models:
        item = {"id": name, "enabled": True, "min_price": "0.5",
                "inference_ok": True, "stream_ok": True, "usage_ok": True,
                "probe_version": "L2+stream"}
        if last_probe_at is not None:
            item["last_probe_at"] = last_probe_at.isoformat()
        stored.append(item)
    record = store.add_upstream(upstream_id="u1", access_type="A2",
                                base_url=mock.upstream_base_url, models=stored,
                                credential_ref="upstream:u1", state=record_state)
    stamp = record_last_probe_at or last_probe_at
    return store.update_upstream("u1", last_probe_at=stamp.isoformat() if stamp else None)


def test_capacity_survives_24h_then_drops_after_25h(paths, store, credentials, mock,
                                                   clock):
    """日探针的核心：**24 h 后容量还在**（>25 h 才归零），且报价行同样在。"""
    moment = clock()
    _seed_verified(store, mock, models=("a-model", "b-model"), last_probe_at=moment)
    manager = UpstreamManager(store, credentials)

    later = moment + timedelta(hours=24)
    assert [entry.id for _, entry in manager.announceable_models(later)] == \
        ["a-model", "b-model"]
    plan = plan_quotes(manager, now=later)
    assert plan.rows and SKIP_STALE not in plan.skipped

    expired = moment + timedelta(seconds=facts.PROBE_FRESH_S + 60)
    assert manager.announceable_models(expired) == []
    plan = plan_quotes(manager, now=expired)
    assert plan.rows == []
    assert plan.skipped == {SKIP_STALE: 2}


def test_model_older_than_seven_days_is_not_carried_by_upstream_health(paths, store,
                                                                      credentials, mock,
                                                                      clock):
    """lead 2026-11-21 复核补充：模型自己 >7 天没被实探 ⇒ 上游再健康也不公告/报价。"""
    moment = clock()
    stale_model = moment - timedelta(seconds=facts.PROBE_MODEL_MAX_AGE_S + 3600)
    record = _seed_verified(store, mock, models=("fresh-model", "stale-model"),
                            last_probe_at=moment,
                            record_last_probe_at=moment)
    # 一个模型刚探过，另一个停在 7 天前（链接本轮通过）
    models = [dict(item) for item in record.models]
    for item in models:
        if item["id"] == "stale-model":
            item["last_probe_at"] = stale_model.isoformat()
    store.update_upstream("u1", models=models)

    entry_stale = ModelEntry.from_dict(models[1])
    entry_fresh = ModelEntry.from_dict(models[0])
    assert covered(store.upstream("u1"), entry_fresh, moment) is True
    assert covered(store.upstream("u1"), entry_stale, moment) is False, \
        "超过 7 天的模型结论不许被「上游健康」永久带住"
    ids = [entry.id for _, entry in UpstreamManager(store, credentials)
           .announceable_models(moment)]
    assert ids == ["fresh-model"]


def test_rejected_upstream_carries_nothing_even_with_fresh_models(paths, store,
                                                                  credentials, mock, clock):
    """``state=rejected`` 仍是**一票否决**（覆盖口径不放松失败的上游）。"""
    moment = clock()
    _seed_verified(store, mock, models=("a-model",), last_probe_at=moment,
                   record_state="rejected")
    manager = UpstreamManager(store, credentials)
    assert manager.announceable_models(moment) == []


# ───────────────────────────── 5b) 回归闸：刚探过但**没通过**的模型不得被覆盖放行
@pytest.mark.parametrize("inference_ok", [False, None])
def test_fresh_but_unverified_model_is_never_covered(paths, store, credentials, mock,
                                                     clock, inference_ok):
    """``fresh`` 只代表「最近探过」，**不代表通过**（lead 2026-11-21 复核阻断）。

    ``covered()`` 第一版只判 ``entry.fresh()``：L2 失败（``inference_ok=False``）或
    未知（``None``）同样会写 ``last_probe_at`` ⇒ 这些模型被当成「有新鲜证据」而进入
    公告/报价，比 CHG 之前的 ``ModelEntry.announceable()``（要求 verified+fresh）还松。
    这条闸钉住「两条分支都必须以 L2 通过为前提」。
    """
    moment = clock()
    record = _seed_verified(store, mock, models=("just-probed",), last_probe_at=moment,
                            record_last_probe_at=moment)
    models = [dict(item) for item in record.models]
    models[0]["inference_ok"] = inference_ok
    store.update_upstream("u1", models=models)
    record = store.upstream("u1")
    entry = ModelEntry.from_dict(record.models[0])

    assert entry.fresh(moment) is True, "前提：这份结论确实刚探过（fresh=True）"
    assert entry.verified() is False
    assert covered(record, entry, moment) is False
    manager = UpstreamManager(store, credentials)
    assert manager.announceable_models(moment) == []
    plan = plan_quotes(manager, now=moment)
    assert plan.rows == []
    # 报价侧还有更靠前的一关（3. `inference_ok is not True` → not_probed）：没通过 L2 的模型
    # 在这里就被拦下，不会走到新鲜度那一关 —— 两关都拒绝，顺序不影响结论。
    assert plan.skipped == {SKIP_NOT_PROBED: 1}


def test_fresh_and_verified_model_is_still_covered(paths, store, credentials, mock, clock):
    """防修反：``fresh + inference_ok=True`` 必须仍然 covered（否则日探针全掉容量）。"""
    moment = clock()
    record = _seed_verified(store, mock, models=("ok-model",), last_probe_at=moment,
                            record_last_probe_at=moment)
    entry = ModelEntry.from_dict(record.models[0])

    assert entry.fresh(moment) is True and entry.verified() is True
    assert covered(record, entry, moment) is True
    manager = UpstreamManager(store, credentials)
    assert [item.id for _, item in manager.announceable_models(moment)] == ["ok-model"]
    assert plan_quotes(manager, now=moment).rows


# ───────────────────────────────────────── 6) backlog 续跑只一次（不许 5 s 空转）
def test_backlog_continuation_is_capped_at_one_per_day(paths, store, credentials, mock,
                                                       clock, monkeypatch):
    """预算没探到 ⇒ 5 s 续跑一次；**再没探到就等次日**（修前是每 5 s 无限续跑）。"""
    _add_upstream(store, mock)
    payload = {"upstream_id": "u1", "reachable": True, "latency_ms": 1.0,
               "error_class": None,
               "model_checks": [{"model": "mock-model", "ok": False,
                                 "error_class": facts.PROBE_NOT_PROBED}],
               "models": ["mock-model"], "probed": [],
               "not_probed": [{"model": "mock-model", "reason": "budget"}],
               "checked_at": clock().isoformat()}
    daemon = _daemon(paths, store, credentials, mock, clock=clock, config=_config(mock))
    daemon.start()
    try:
        daemon.upstreams.probe = lambda *args, **kwargs: dict(payload)
        _due(daemon, clock)
        first = daemon._probe_upstreams(clock(), reason="periodic")
        assert first["backlog"] == 1
        assert daemon.status()["probe"]["daily"]["next_kind"] == "backlog"
        gap = (daemon._next_probe_at - clock()).total_seconds()
        assert abs(gap - facts.PROBE_BACKLOG_RETRY_S) < 2

        # 续跑一次仍没探到 ⇒ 当日额度用尽 ⇒ 下一轮排到次日（不再 5 s 空转）
        clock.advance(gap + 1)
        second = daemon._probe_upstreams(clock(), reason="periodic")
        assert second["backlog"] == 1
        assert daemon.status()["probe"]["daily"]["next_kind"] == "daily"
        assert daemon._next_probe_at.date() > clock().date()
        settled = daemon._next_probe_at
        clock.advance(120)
        daemon.tick(clock())
        assert daemon._next_probe_at == settled, "当日不许再有续跑"
    finally:
        daemon.stop()
