# SPDX-License-Identifier: Apache-2.0
"""多 plan 组：库 v4 迁移 / 组与成员 / 组级共享预算 / server 自动切换。

覆盖任务单要求的四类证据：

1. **迁移 v3→v4**：老库数据一条不少、可重复打开（幂等）、旧客户端被拒（版本只前进）；
2. **选择与切换**：``plan_router.pick_target`` 真的按库里的组/成员工作，预算耗尽跳过；
3. **记账**：金额只走 minor 整数，跨天 / 跨月归零，拿不到单价只记 token、没有 usage 只计数；
4. **开关门控**：``plan.groups`` 关闭时 ``pick_target`` 一次都不被调用（spy），
   请求行为与接入前一致；打开后 503/401 会换下一个上游，且日志里**没有凭据**。
"""
from __future__ import annotations

import http.client
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from djb_node import facts, plan_router
from djb_node.config import ClientConfig, Paths
from djb_node.errors import StoreDowngradeError, UsageError
from djb_node.logging_setup import setup_logging
from djb_node.server import Admission, LocalApiServer, RouteTarget
from djb_node.store import SCHEMA_VERSION, Store
from tests.mock_m02 import MockServer

TOKEN = "t" * 40
SECRET = "sk-plan-groups-must-not-leak-0123456789"


# ─────────────────────────────────────────────────────── 工具
def _stop(path: sqlite3.Connection) -> None:
    path.commit()


def _v3_db_with_old_data(db_file: str) -> None:
    """把新库降级成 v3 形状：删掉 v4 的两张新表 + user_version 写回 3。"""
    connection = sqlite3.connect(db_file)
    try:
        connection.execute("DROP TABLE IF EXISTS plan_group_members")
        connection.execute("DROP TABLE IF EXISTS plan_groups")
        connection.execute("PRAGMA user_version = 3")
        _stop(connection)
    finally:
        connection.close()


def _upstream(store: Store, upstream_id: str, models=("m1",), price="2.00") -> None:
    store.add_upstream(upstream_id=upstream_id, access_type="A1",
                       base_url="http://127.0.0.1:9/v1", credential_ref=None,
                       models=[{"id": model, "min_price": price} for model in models])


def _wait_idle(runtime, timeout: float = 2.0) -> None:
    """等 handler 线程收工：响应是先写给客户端的，记账 / 事件在之后才落。

    不 wait 就会偶发「客户端已经拿到 200，但 plan_charge 还没 append」——这是记账口径
    （**响应先发、账后记**，同一次请求内同步完成）的必然结果，不是产品 bug。
    """
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if runtime.inflight == 0:
            return
        _time.sleep(0.01)
    raise AssertionError("handler 没有在超时内收工")


def _lines(log_file: str) -> list[dict]:
    with open(log_file, encoding="utf-8") as handle:
        return [json.loads(item) for item in handle if item.strip()]


def _request(port, body):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    connection.request("POST", "/v1/chat/completions", body=payload,
                       headers={"Content-Type": "application/json",
                                "Authorization": "Bearer " + TOKEN})
    response = connection.getresponse()
    data = response.read()
    status = response.status
    connection.close()
    return status, data


class PlanRuntime:
    """假运行时：``route()`` 走老路径，``plan_*`` 走组模式（都能断言调用次数）。"""

    def __init__(self, *, legacy_base=None, members=None, models=("mock-model",)) -> None:
        self.legacy_base = legacy_base
        self.admission = Admission(True)
        self.observed = []
        self.inflight = 0
        self.members = list(members or [])
        self.models = list(models)
        self.charged = []
        self.route_calls = 0

    # 老路径
    def health(self):
        return {"status": "ok", "daemon": {"state": "online"}, "policy": {"usable": True},
                "loopback_only": True}

    def admit(self, model):
        return self.admission

    def route(self, model):
        self.route_calls += 1
        if not self.legacy_base or model not in self.models:
            return None
        return RouteTarget(base_url=self.legacy_base, model=model, access="A2",
                           upstream_id="legacy", credential=SECRET)

    def observe(self, **fields):
        self.observed.append(fields)

    def begin_task(self):
        self.inflight += 1

    def end_task(self):
        self.inflight -= 1

    # 组路径
    def plan_groups(self):
        return self.members

    def plan_route(self, upstream_id, model):
        for member in self.members:
            for item in member["members"]:
                if item["upstream_id"] == upstream_id:
                    if model not in self.models:
                        return None
                    return RouteTarget(base_url=member["base_url"], model=model, access="A2",
                                       upstream_id=upstream_id, credential=SECRET)
        return None

    def plan_charge(self, target, model, usage):
        self.charged.append({"group": target.group, "model": model, "usage": usage})
        return {"group": target.group, "cost_minor": None, "tokens": None,
                "price_known": False, "spent_usd_minor_day": 0,
                "spent_usd_minor_month": 0, "unknown_usage_count": 1}


def _member(upstream_id: str, base_url: str, *, group: str = "main",
            priority: int = 0) -> dict:
    return {"name": group, "enabled": True, "priority": 0,
            "budget_usd_minor_day": None, "budget_usd_minor_month": None,
            "spent_usd_minor_day": 0, "spent_usd_minor_month": 0,
            "base_url": base_url,
            "members": [{"upstream_id": upstream_id, "models": "mock-model",
                         "priority": priority, "weight": 1, "enabled": True}]}


@pytest.fixture
def two_upstreams():
    first, second = MockServer().start(), MockServer().start()
    yield first, second
    first.stop()
    second.stop()


def _local(runtime, *, flag: bool):
    settings = {"feature_flags": {facts.PLAN_GROUPS_FLAG: True}} if flag else {}
    server = LocalApiServer(runtime, token=TOKEN, port=0, settings=settings)
    server.bind()
    server.start()
    return server


# ─────────────────────────────────────────────────────── 1. 迁移
def test_schema_version_is_4():
    assert SCHEMA_VERSION == 4


def test_v3_db_migrates_to_v4_and_keeps_old_data(tmp_path):
    db_file = str(tmp_path / "state.db")
    store = Store.create(db_file)
    _upstream(store, "u-old")
    before = store.upstream("u-old").to_dict()
    store.close()

    _v3_db_with_old_data(db_file)
    connection = sqlite3.connect(db_file)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "plan_groups" not in tables and "plan_group_members" not in tables
    finally:
        connection.close()

    migrated = Store(db_file)                      # 打开即迁移
    try:
        assert migrated.upstream("u-old").to_dict() == before      # 老数据一条不少
        assert migrated.plan_groups() == []                        # 新表是空的，不回填
    finally:
        migrated.close()

    connection = sqlite3.connect(db_file)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        columns = {row[1] for row in connection.execute("PRAGMA table_info(plan_groups)")}
        assert {"budget_usd_minor_day", "spent_usd_minor_day", "day_key",
                "spent_tokens_day", "unknown_usage_count"} <= columns
        member_columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(plan_group_members)")}
        assert {"group_id", "upstream_id", "priority", "weight", "enabled"} <= member_columns
    finally:
        connection.close()


def test_migration_is_idempotent(tmp_path):
    db_file = str(tmp_path / "state.db")
    store = Store.create(db_file)
    _upstream(store, "u-old")
    store.close()
    _v3_db_with_old_data(db_file)
    for _ in range(3):                             # 反复打开不报错、不丢数据
        item = Store(db_file)
        try:
            assert item.upstream("u-old").id == "u-old"
            assert item.plan_groups() == []
        finally:
            item.close()


def test_future_schema_version_is_rejected(tmp_path):
    db_file = str(tmp_path / "state.db")
    Store.create(db_file).close()
    connection = sqlite3.connect(db_file)
    try:
        connection.execute("PRAGMA user_version = 99")
        _stop(connection)
    finally:
        connection.close()
    with pytest.raises(StoreDowngradeError):
        Store(db_file).connect()


def test_plan_tables_created_on_fresh_db(store):
    assert store.plan_groups() == []
    with pytest.raises(UsageError):
        store.plan_group("nope")


# ─────────────────────────────────────────────────────── 2. 组与成员
def test_group_crud_and_member_upsert(store):
    _upstream(store, "u1", models=("m1", "m2"))
    _upstream(store, "u2")
    group = store.add_plan_group(name="main", budget_day_minor=100, budget_month_minor=3000,
                                 priority=10)
    assert group["name"] == "main" and group["budget_usd_minor_day"] == 100
    with pytest.raises(UsageError):
        store.add_plan_group(name="main")            # 名字唯一

    store.add_plan_group_member("main", upstream_id="u1", priority=5, weight=3)
    store.add_plan_group_member("main", upstream_id="u1", priority=1, weight=2)  # 幂等 upsert
    store.add_plan_group_member("main", upstream_id="u2")
    current = store.plan_group("main")
    assert [(item["upstream_id"], item["priority"], item["weight"])
            for item in current["members"]] == [("u1", 1, 2), ("u2", 100, 1)]
    assert current["members"][0]["models"] == "m1,m2"    # 模型来自上游清单
    assert store.plan_group(group["id"])["name"] == "main"   # id 也认

    assert store.remove_plan_group_member("main", "u1") is True
    assert store.remove_plan_group_member("main", "u1") is False
    assert [item["upstream_id"] for item in store.plan_group("main")["members"]] == ["u2"]
    assert store.remove_plan_group("main") is True
    assert store.plan_groups() == []
    assert store.upstream("u1").id == "u1"                   # 删组不动上游


def test_member_requires_existing_upstream(store):
    store.add_plan_group(name="main")
    with pytest.raises(UsageError):
        store.add_plan_group_member("main", upstream_id="ghost")


def test_budget_validation_rejects_bad_money(store):
    store.add_plan_group(name="main", budget_day_minor=100)
    for bad in (-1, 1.5, "1.00", True):
        with pytest.raises(UsageError):
            store.add_plan_group(name="x" + str(bad), budget_day_minor=bad)
    with pytest.raises(UsageError):
        store.add_plan_group_member("main", upstream_id="u1", weight=0)


def test_money_helpers_are_minor_integers():
    assert plan_router.parse_usd_minor("1.23") == 123
    assert plan_router.parse_usd_minor("0") == 0
    assert plan_router.format_usd_minor(123) == "1.23"
    assert plan_router.format_usd_minor(None) == "不限"
    for bad in ("", "-1", "1.234", "abc", 1.5):
        with pytest.raises(ValueError):
            plan_router.parse_usd_minor(bad)
    # 只认「每百万 token 美元」；单位不认识就不算钱
    assert plan_router.estimate_cost_minor(tokens=1_000_000, price="2.00") == 200
    assert plan_router.estimate_cost_minor(tokens=1_000, price="2.00") == 0   # 保守取整
    assert plan_router.estimate_cost_minor(tokens=1_000, price="2.00",
                                           unit="usd_per_1k_tokens") is None
    assert plan_router.estimate_cost_minor(tokens=1_000, price=None) is None
    assert plan_router.estimate_cost_minor(tokens=None, price="2.00") is None


def test_pick_target_uses_store_shapes(store):
    _upstream(store, "u1", models=("m1",))
    _upstream(store, "u2", models=("m2",))
    store.add_plan_group(name="second", priority=20)
    store.add_plan_group(name="first", priority=10)
    store.add_plan_group_member("second", upstream_id="u2")
    store.add_plan_group_member("first", upstream_id="u1")
    target = plan_router.pick_target(store.plan_groups(), model="m1",
                                     state=plan_router.GroupState())
    assert (target.group, target.upstream_id, target.model) == ("first", "u1", "m1")
    assert plan_router.pick_target(store.plan_groups(), model="m9") is None   # 不支持就 None


# ─────────────────────────────────────────────────────── 3. 预算记账
def test_spend_accumulates_and_rolls_over(store):
    _upstream(store, "u1")
    store.add_plan_group(name="main", budget_day_minor=1000, budget_month_minor=5000)
    day1 = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)

    store.add_plan_spend("main", tokens=1000, cost_minor=50, now=day1)
    store.add_plan_spend("main", tokens=2000, cost_minor=70, now=day1)
    group = store.plan_group("main")
    assert (group["spent_usd_minor_day"], group["spent_usd_minor_month"]) == (120, 120)
    assert (group["spent_tokens_day"], group["spent_tokens_month"]) == (3000, 3000)

    # 跨天：日窗口归零、月窗口继续累
    day2 = day1 + timedelta(days=1)
    store.add_plan_spend("main", tokens=100, cost_minor=7, now=day2)
    group = store.plan_group("main")
    assert (group["spent_usd_minor_day"], group["spent_usd_minor_month"]) == (7, 127)
    assert (group["spent_tokens_day"], group["spent_tokens_month"]) == (100, 3100)

    # 跨月：两个窗口都归零
    next_month = datetime(2026, 12, 1, 0, 5, tzinfo=timezone.utc)
    store.add_plan_spend("main", tokens=10, cost_minor=1, now=next_month)
    group = store.plan_group("main")
    assert (group["spent_usd_minor_day"], group["spent_usd_minor_month"]) == (1, 1)
    assert (group["spent_tokens_day"], group["spent_tokens_month"]) == (10, 10)


def test_unknown_usage_counts_but_records_no_money(store):
    _upstream(store, "u1")
    store.add_plan_group(name="main", budget_day_minor=1000)
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    store.add_plan_spend("main", tokens=None, now=now)
    store.add_plan_spend("main", tokens=None, now=now)
    group = store.plan_group("main")
    assert group["unknown_usage_count"] == 2
    assert (group["spent_usd_minor_day"], group["spent_tokens_day"]) == (0, 0)


def test_no_price_records_tokens_only(store):
    _upstream(store, "u1")
    store.add_plan_group(name="main", budget_day_minor=1000)
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    store.add_plan_spend("main", tokens=1500, cost_minor=None, now=now)
    group = store.plan_group("main")
    assert group["spent_tokens_day"] == 1500
    assert group["spent_usd_minor_day"] == 0


def test_exhausted_group_is_skipped(store):
    _upstream(store, "u1", models=("m1",))
    _upstream(store, "u2", models=("m1",))
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    store.add_plan_group(name="broke", priority=0, budget_day_minor=10)
    store.add_plan_group(name="rich", priority=10)
    store.add_plan_group_member("broke", upstream_id="u1")
    store.add_plan_group_member("rich", upstream_id="u2")
    store.add_plan_spend("broke", tokens=100, cost_minor=10, now=now)

    state = plan_router.GroupState()
    groups = store.plan_groups()
    assert plan_router.budget_view(groups[0], now)["budget_exhausted"] == "day"
    target = plan_router.pick_target(groups, model="m1", state=state, now=now)
    assert (target.group, target.upstream_id) == ("rich", "u2")
    assert state.stats["budget_exhausted_total"] == 1        # 跳过是可观测的


def test_effective_budget_left_is_normalised_by_window(store):
    _upstream(store, "u1")
    store.add_plan_group(name="main", budget_day_minor=100)
    day1 = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    store.add_plan_spend("main", tokens=10, cost_minor=100, now=day1)
    group = store.plan_group("main")
    assert plan_router.effective_budget_left(group, "m1", now=day1) == 0
    # 第二天读同一个库：日窗口按 0 算（写入侧还没跑，但选择侧不能误判为耗尽）
    assert plan_router.effective_budget_left(group, "m1", now=day1 + timedelta(days=1)) == 100


# ─────────────────────────────────────────────────────── 4. 开关门控
def test_flag_off_never_calls_pick_target(store, monkeypatch):
    calls = []

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return None

    monkeypatch.setattr(plan_router, "pick_target", _spy)
    upstream = MockServer().start()
    try:
        runtime = PlanRuntime(legacy_base=upstream.upstream_base_url,
                              members=[_member("u1", upstream.upstream_base_url)])
        server = _local(runtime, flag=False)
        try:
            status, data = _request(server.port, {"model": "mock-model",
                                                 "messages": [{"role": "user", "content": "hi"}]})
            assert status == 200
            payload = json.loads(data)
            assert payload["object"] == "chat.completion"
            assert runtime.route_calls == 1          # 还是老路径
            assert calls == []                       # spy：pick_target 一次都没被调
            assert runtime.charged == []
            assert runtime.observed[0]["total_tokens"] == 12
        finally:
            server.shutdown()
    finally:
        upstream.stop()


def test_flag_on_failover_to_next_plan(two_upstreams, tmp_path):
    first, second = two_upstreams
    first.chat_status = 503
    runtime = PlanRuntime(members=[_member("u1", first.upstream_base_url),
                                   _member("u2", second.upstream_base_url,
                                             priority=1)])
    log_file = str(tmp_path / "djb.log")
    setup_logging(log_file)
    server = _local(runtime, flag=True)
    try:
        status, data = _request(server.port, {"model": "mock-model",
                                             "messages": [{"role": "user", "content": "hi"}]})
        assert status == 200
        assert json.loads(data)["object"] == "chat.completion"
        assert len(first.requests) == 1 and len(second.requests) == 1   # 切了一次
        assert runtime.route_calls == 0                                 # 没走老路径
        _wait_idle(runtime)
        # 失败时把 plan.* 事件带出来（含 plan.charge_failed），免得只看到一句空断言
        assert runtime.charged, [item for item in _lines(log_file)
                                 if str(item.get("event", "")).startswith("plan.")]
        assert runtime.charged[0]["usage"][2] == 12
    finally:
        server.shutdown()

    lines = _lines(log_file)
    names = [item.get("event") for item in lines]
    assert "plan.failover" in names
    failover = next(item for item in lines if item.get("event") == "plan.failover")
    assert failover["reason"] == "upstream_error"
    assert failover["status"] == 503
    assert failover["upstream_id"] == "u1"
    assert "plan.chat" in names
    assert SECRET not in open(log_file, encoding="utf-8").read()       # 凭据不进日志


def test_flag_on_401_cools_the_upstream_down(two_upstreams):
    first, second = two_upstreams
    first.chat_status = 401
    runtime = PlanRuntime(members=[_member("u1", first.upstream_base_url),
                                   _member("u2", second.upstream_base_url,
                                             priority=1)])
    server = _local(runtime, flag=True)
    try:
        body = {"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]}
        assert _request(server.port, body)[0] == 200
        assert len(first.requests) == 1 and len(second.requests) == 1
        _wait_idle(runtime)
        # 冷却期内第二个请求直接跳过 u1（10 秒 << 300 秒）
        assert _request(server.port, body)[0] == 200
        assert len(first.requests) == 1                  # 没再打 401 那家
        assert len(second.requests) == 2
        _wait_idle(runtime)
        assert server.plan_stats()["failover_auth"] == 1
    finally:
        server.shutdown()


def test_flag_on_all_plans_fail_returns_503(two_upstreams):
    first, second = two_upstreams
    first.chat_status = 503
    second.chat_status = 503
    runtime = PlanRuntime(members=[_member("u1", first.upstream_base_url),
                                   _member("u2", second.upstream_base_url,
                                             priority=1)])
    server = _local(runtime, flag=True)
    try:
        status, data = _request(server.port, {"model": "mock-model",
                                             "messages": [{"role": "user", "content": "hi"}]})
        assert status == 503
        assert json.loads(data)["error"]["code"] == facts.LOCAL_ERROR_CODES["no_upstream"]
        assert len(first.requests) == 1 and len(second.requests) == 1   # 两个都试过
    finally:
        server.shutdown()


def test_flag_on_empty_groups_falls_back_to_legacy_route(two_upstreams, tmp_path):
    first, _ = two_upstreams
    runtime = PlanRuntime(legacy_base=first.upstream_base_url, members=[])
    log_file = str(tmp_path / "djb.log")
    setup_logging(log_file)
    server = _local(runtime, flag=True)
    try:
        status, _data = _request(server.port, {"model": "mock-model",
                                             "messages": [{"role": "user", "content": "hi"}]})
        assert status == 200
        assert runtime.route_calls == 1        # 没组 -> 退回单上游路径（不静默）
    finally:
        server.shutdown()
    text = open(log_file, encoding="utf-8").read()
    assert "plan.no_groups" in text


def test_flag_on_attempts_are_capped(monkeypatch, two_upstreams):
    """同一上游不会被反复重试：MAX_ATTEMPTS 次之后停手（且换了目标）。"""
    first, second = two_upstreams
    first.chat_status = 503
    second.chat_status = 503
    runtime = PlanRuntime(members=[_member("u1", first.upstream_base_url),
                                   _member("u2", second.upstream_base_url,
                                             priority=1)])
    server = _local(runtime, flag=True)
    try:
        _request(server.port, {"model": "mock-model",
                              "messages": [{"role": "user", "content": "hi"}]})
        total = len(first.requests) + len(second.requests)
        assert total <= plan_router.MAX_ATTEMPTS
        assert len(first.requests) == 1        # u1 只被打一次（exclude 生效）
    finally:
        server.shutdown()


# ─────────────────────────────────────────────────────── 5. 运行时记账（daemon 侧）
def _daemon(paths, store, credentials, *, price="2.00", budget_day=10_000):
    from djb_node.config import save_config
    from djb_node.daemon import Daemon

    config = ClientConfig(gateway_url="http://127.0.0.1:1", mirror_port=1717)
    save_config(paths, config)
    models = [{"id": "mock-model", "enabled": True}]
    if price is not None:
        models[0]["min_price"] = price
    store.add_upstream(upstream_id="u1", access_type="A2",
                       base_url="http://127.0.0.1:9/v1", models=models,
                       credential_ref=None)
    store.add_plan_group(name="main", budget_day_minor=budget_day)
    store.add_plan_group_member("main", upstream_id="u1")
    return Daemon(paths, config=config, store=store, credentials=credentials,
                  local_token="t" * 40, port=0)


def test_daemon_charge_uses_price_in_minor(paths, store, credentials):
    daemon = _daemon(paths, store, credentials)
    target = plan_router.PlanTarget("main", "u1", "mock-model")
    charged = daemon.plan_charge(target, "mock-model", (500_000, 500_000, 1_000_000))
    assert charged["price_known"] is True
    assert charged["cost_minor"] == 200                     # 1M token × $2/1M = $2.00
    assert charged["spent_usd_minor_day"] == 200
    group = store.plan_group("main")
    assert (group["spent_usd_minor_day"], group["spent_tokens_day"]) == (200, 1_000_000)


def test_daemon_charge_without_price_records_tokens_only(paths, store, credentials):
    daemon = _daemon(paths, store, credentials, price=None)
    target = plan_router.PlanTarget("main", "u1", "mock-model")
    charged = daemon.plan_charge(target, "mock-model", (7, 5, 12))
    assert charged["price_known"] is False and charged["cost_minor"] is None
    group = store.plan_group("main")
    assert (group["spent_usd_minor_day"], group["spent_tokens_day"]) == (0, 12)


def test_daemon_charge_without_usage_counts_unknown(paths, store, credentials):
    daemon = _daemon(paths, store, credentials)
    target = plan_router.PlanTarget("main", "u1", "mock-model")
    charged = daemon.plan_charge(target, "mock-model", None)
    assert charged["tokens"] is None and charged["cost_minor"] is None
    group = store.plan_group("main")
    assert group["unknown_usage_count"] == 1
    assert (group["spent_usd_minor_day"], group["spent_tokens_day"]) == (0, 0)


def test_daemon_plan_route_skips_unknown_model_and_disabled(paths, store, credentials):
    daemon = _daemon(paths, store, credentials)
    target = daemon.plan_route("u1", "mock-model")
    assert target is not None and target.upstream_id == "u1"
    assert target.credential is None            # 没存凭据就不带
    assert daemon.plan_route("u1", "ghost-model") is None
    assert daemon.plan_route("ghost", "mock-model") is None
    store.update_upstream("u1", enabled=False)
    assert daemon.plan_route("u1", "mock-model") is None


# ─────────────────────────────────────────────────────── 6. CLI
def test_cli_plan_group_and_budget_roundtrip(paths, store, capsys):
    from djb_node.cli import main

    _upstream(store, "u1", models=("m1",))
    assert main(["plan", "group", "add", "--name", "main", "--day", "1.00",
                 "--month", "30.00", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["group"]["budget_day_minor"] == 100
    assert payload["group"]["budget_month_minor"] == 3000
    assert payload["flag"]["enabled"] is False              # 默认关闭，且要报出来

    assert main(["plan", "group", "add-upstream", "--group", "main", "--id", "u1",
                 "--priority", "5", "--weight", "3"]) == 0
    capsys.readouterr()
    assert main(["plan", "group", "list", "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["count"] == 1
    assert listing["groups"][0]["members"][0]["upstream_id"] == "u1"
    assert listing["groups"][0]["members"][0]["models"] == "m1"

    assert main(["plan", "budget", "set", "--group", "main", "--day", "2.50"]) == 0
    capsys.readouterr()
    assert main(["plan", "budget", "show", "--json"]) == 0
    view = json.loads(capsys.readouterr().out)["groups"][0]
    assert view["budget_day_minor"] == 250 and view["budget_exhausted"] is None
    assert view["remaining_day_usd"] == "2.50"

    assert main(["plan", "budget", "set", "--group", "main", "--no-month"]) == 0
    capsys.readouterr()
    assert store.plan_group("main")["budget_usd_minor_month"] is None

    assert main(["plan", "group", "remove-upstream", "--group", "main", "--id", "u1"]) == 0
    capsys.readouterr()
    assert main(["plan", "group", "remove", "--group", "main"]) == 0
    capsys.readouterr()
    assert store.plan_groups() == []


def test_cli_plan_help_and_flag_note(paths, store, capsys):
    from djb_node.cli import main

    with pytest.raises(SystemExit) as info:              # argparse 的 --help 走 SystemExit
        main(["plan", "--help"])
    assert info.value.code == 0
    assert "budget" in capsys.readouterr().out

    assert main(["plan", "budget", "set", "--group", "ghost", "--day", "1.00"]) == 2
    assert "不存在" in capsys.readouterr().err

    store.add_plan_group(name="main")
    assert main(["plan", "budget", "show"]) == 0
    out = capsys.readouterr().out
    assert "当前不生效" in out            # 开关关着也必须说清楚


def test_flag_on_streaming_failover(two_upstreams):
    """流式也要能切换：503 的上游还没吐数据就换下一个（最坏情况最多 3 次）。"""
    first, second = two_upstreams
    first.chat_status = 503
    runtime = PlanRuntime(members=[_member("u1", first.upstream_base_url),
                                   _member("u2", second.upstream_base_url, priority=1)])
    server = _local(runtime, flag=True)
    try:
        status, data = _request(server.port, {"model": "mock-model", "stream": True,
                                             "messages": [{"role": "user", "content": "hi"}]})
        assert status == 200
        assert b"data:" in data                    # 真的把 SSE 透传下来了
        assert len(first.requests) == 1 and len(second.requests) == 1
        _wait_idle(runtime)
        assert runtime.charged and runtime.charged[0]["usage"][2] == 12
    finally:
        server.shutdown()
