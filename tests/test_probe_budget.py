# SPDX-License-Identifier: Apache-2.0
"""探针判定/预算回归（0.3.8 预算口径 + 0.3.9 判定顺序修复）。

历史一（0.3.8 / RC14 P0-2）：单轮预算 10s 死值，5 个 enabled 模型跑一次 L3 实测
2 分 37 秒 ~ 6 分 06 秒 ⇒ 每轮必有模型 ``not_probed`` ⇒ ``all()`` 恒 false ⇒
``verified=0``、从不上报 ⇒ 云端 ``verified_at`` 冻结。0.3.8 修了预算，但把
「≥1 个 ok 且**没有真实失败**」当成判定 ⇒ 单模型 timeout 判死整条上游。

历史二（0.3.9 / D-RCA）：0.3.8 灰度现场 13:54 qwen3.8-max 一个 ``timeout``、其余 4 个
ok ⇒ ``state=rejected`` ⇒ ``quotes.py`` 的 ``gated`` 跳过该上游**全部**模型 ⇒
``quotes rows=0`` 整条断流（13:55），05:59 命中自动回滚阈值。本文件钉住修后的口径：

1. 预算可配，但**硬上限 = 一个心跳周期**（内联在主循环，超了节点会 stale/offline）；
2. ``not_probed`` 不是失败：上报的 ``model_checks`` 里不出现，只进 ``not_probed[]``；
3. **任一模型 verified ⇒ 整条 verified**（P0-a）；daemon 有 ok 即 ``verified`` 并上报，
   ``real_failures`` 只是诊断字段（P0-b）；
4. **全量故障不许洗白**：全部模型 timeout ⇒ ``rejected``、``quotes rows=0``、不上报（P0-c）；
5. 单模型预算切片（P1-a）+ backlog 优先（P1-b）。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from djb_node import facts
from djb_node import probe as probe_mod
from djb_node.config import ClientConfig
from djb_node.quotes import SKIP_NOT_PROBED, SKIP_UPSTREAM, plan_quotes
from djb_node.upstream import UpstreamManager
from tests.test_daemon import _add_upstream, _daemon


@pytest.fixture
def mock():
    from tests.mock_m02 import MockServer
    server = MockServer().start()
    yield server
    server.stop()


FIVE = ("m1", "m2", "m3", "m4", "qwen3.8-max")
"""D-RCA 现场同构的 5 个 enabled 模型（qwen3.8-max = 2026-11-16 那个 timeout 的模型）。"""


class _Outcome:
    """``probe.ProbeOutcome`` 的最小替身（只带判定要用的四个字段）。"""

    def __init__(self, ok: bool = True, error_class: str | None = None) -> None:
        self.inference_ok = ok
        self.stream_ok = ok
        self.usage_present = ok
        self.error_class = error_class


def _payload(*, ok_models, budget_missed=(), real_failures=()):
    """模拟 ``UpstreamManager.probe()`` 的返回：not_probed 仍在 model_checks 里（ok=False）。"""
    checks = [{"model": name, "ok": True, "error_class": None} for name in ok_models]
    checks += [{"model": name, "ok": False, "error_class": "not_probed"}
               for name in budget_missed]
    checks += [{"model": name, "ok": False, "error_class": "timeout"}
               for name in real_failures]
    return {
        "upstream_id": "u1",
        "reachable": True,
        "latency_ms": 12.0,
        "error_class": None,
        "model_checks": checks,
        "models": [item["model"] for item in checks],
        "not_probed": [{"model": name, "reason": "budget"} for name in budget_missed],
        "checked_at": "2026-11-15T00:00:00Z",
    }


def _daemon_with_payload(paths, store, credentials, mock, clock, payload):
    _add_upstream(store, mock)
    daemon = _daemon(paths, store, credentials, mock, clock=clock,
                     config=ClientConfig(gateway_url=mock.base_url, mirror_port=1717,
                                         extra={facts.PROBE_FLAG_PERIODIC: True}))
    reports: list[dict] = []
    daemon.upstreams.probe = lambda *args, **kwargs: dict(payload)
    daemon._report_probe = lambda record, value, *, reason: reports.append(value)
    return daemon, reports


def test_partial_pass_with_backlog_still_verifies_and_reports_honestly(
        paths, store, credentials, mock, clock):
    """5 模型：3 个 ok + 2 个 not_probed ⇒ verified；上报**如实**带 5 条 model_checks。

    云端（main/745eddc）按 ``models[]``+``not_probed[]`` 判定，not_probed 记 ok=None 不算失败；
    节点侧剔除反而会让 ``models[]`` 从 enabled 集合退化成「探过的集合」。
    """
    payload = _payload(ok_models=["m1", "m2", "m3"], budget_missed=["m4", "m5"])
    daemon, reports = _daemon_with_payload(paths, store, credentials, mock, clock, payload)
    result = daemon._probe_upstreams(clock(), reason="periodic")

    assert result["verified"] == 1
    assert result["failed"] == 0
    assert result["backlog"] == 2
    detail = result["per_upstream"][0]
    assert detail["ok"] is True and detail["passed"] == 3 and detail["total"] == 5
    assert detail["not_probed"] == 2 and detail["real_failures"] == []

    assert len(reports) == 1, "有 ok 模型就必须上报，否则云端 verified_at 不刷新"
    reported = [(item["model"], item["ok"]) for item in reports[0]["model_checks"]]
    assert reported == [("m1", True), ("m2", True), ("m3", True),
                        ("m4", False), ("m5", False)], "如实上报，不剔除 not_probed"
    assert {item["model"]: item["reason"]
            for item in reports[0]["not_probed"]} == {"m4": "budget", "m5": "budget"}

    # backlog 未清空 ⇒ 5s 后续跑，而不是等 half-fresh（450s）
    assert daemon._next_probe_at <= clock() + timedelta(
        seconds=facts.PROBE_BACKLOG_RETRY_S + 1)


def test_partial_pass_with_real_failure_still_verifies_and_reports(
        paths, store, credentials, mock, clock):
    """D-RCA §3.2 用例 2：2 ok + 1 timeout ⇒ **verified 并上报**（0.3.9 / P0-b）。

    0.3.8 曾把这条断言成「rejected 且不上报」，正是它把单模型 timeout 放大成整条上游
    不可用（daemon 连续两轮 ``verified=0`` ⇒ 命中自动回滚阈值）。现在 timeout 只降自己
    那一行：``real_failures`` 仍如实保留（P0-c），但不再是否决票。
    """
    payload = _payload(ok_models=["m1", "m2"], real_failures=["m3"])
    daemon, reports = _daemon_with_payload(paths, store, credentials, mock, clock, payload)
    result = daemon._probe_upstreams(clock(), reason="periodic")

    assert result["verified"] == 1 and result["failed"] == 0
    detail = result["per_upstream"][0]
    assert detail["ok"] is True and detail["passed"] == 2 and detail["total"] == 3
    assert detail["real_failures"] == ["m3"], "real_failures 降级为诊断字段，仍如实保留"
    assert len(reports) == 1, "有 ok 就必须上报，否则云端 verified_at 不刷新"
    assert [(item["model"], item["ok"], item["error_class"])
            for item in reports[0]["model_checks"]] == [
        ("m1", True, None), ("m2", True, None), ("m3", False, "timeout")], \
        "P0-c：timeout 仍如实上报，不被洗掉"


def test_all_real_failures_still_fails_and_does_not_report(paths, store, credentials, mock,
                                                           clock):
    """P0-c 兜底：0 个 ok + 确有失败 ⇒ failed、不上报（全量故障不许洗成可用）。"""
    payload = _payload(ok_models=[], real_failures=["m1", "m2", "m3"])
    daemon, reports = _daemon_with_payload(paths, store, credentials, mock, clock, payload)
    result = daemon._probe_upstreams(clock(), reason="periodic")

    assert result["verified"] == 0 and result["failed"] == 1
    assert result["per_upstream"][0]["ok"] is False
    assert result["per_upstream"][0]["real_failures"] == ["m1", "m2", "m3"]
    assert reports == []


def test_all_budget_missed_is_backlog_not_failure(paths, store, credentials, mock, clock):
    """一个都没探到（全 backlog）⇒ 不算 failed、不触发退避，5s 后续跑。"""
    payload = _payload(ok_models=[], budget_missed=["m1", "m2"])
    daemon, reports = _daemon_with_payload(paths, store, credentials, mock, clock, payload)
    result = daemon._probe_upstreams(clock(), reason="periodic")

    assert result["verified"] == 0 and result["failed"] == 0
    assert result["backlog"] == 2 and reports == []
    assert daemon._probe_failures == 0
    assert daemon._next_probe_at <= clock() + timedelta(
        seconds=facts.PROBE_BACKLOG_RETRY_S + 1)


def test_periodic_probe_uses_l2_and_budget_is_capped():
    """周期探针只跑 L2（L3 留人工）；预算硬上限 = 一个心跳周期。"""
    assert facts.PROBE_PERIODIC_LEVEL == "L2"
    assert facts.PROBE_PASS_BUDGET_MAX_S == float(facts.HEARTBEAT_DEFAULT_S)
    assert facts.probe_pass_budget_s(600) == facts.PROBE_PASS_BUDGET_MAX_S
    assert facts.probe_pass_budget_s(5) == 5.0
    assert facts.probe_pass_budget_s() <= facts.PROBE_PASS_BUDGET_MAX_S


def test_probe_reports_budget_missed_models_honestly(store, credentials, monkeypatch):
    """源头（UpstreamManager.probe）：not_probed 仍进 ``model_checks``，另进 ``not_probed[]``。"""
    store.add_upstream(upstream_id="u1", access_type="A2",
                       base_url="https://upstream.invalid/v1",
                       models=[{"id": name, "enabled": True, "min_price": "0.5"}
                               for name in ("m1", "m2", "m3", "m4", "m5")],
                       credential_ref="upstream:u1")
    credentials.set_upstream_secret("u1", "sk-test-not-a-real-key")

    class _Result:
        reachable = True
        latency_ms = 9.5
        error_class = None
        model_checks = [{"model": name, "ok": True, "error_class": None}
                        for name in ("m1", "m2", "m3", "m4", "m5")]

    class _Outcome:
        inference_ok = True
        stream_ok = True
        usage_present = True
        error_class = None

    monkeypatch.setattr("djb_node.upstream.probe_upstream", lambda *a, **k: _Result())
    monkeypatch.setattr("djb_node.upstream.probe_mod.probe_models",
                        lambda *a, **k: {"m1": _Outcome(), "m2": _Outcome(),
                                         "m3": _Outcome()})

    payload = UpstreamManager(store, credentials).probe("u1")

    assert [item["model"] for item in payload["model_checks"]] == ["m1", "m2", "m3", "m4", "m5"]
    assert payload["models"] == ["m1", "m2", "m3", "m4", "m5"], "models[] = enabled 集合"
    reasons = {item["model"]: item["reason"] for item in payload["not_probed"]}
    assert reasons["m4"] == "budget" and reasons["m5"] == "budget"


def _patch_probe(monkeypatch, *, listed, outcomes):
    """把 L1（``/models``）与 L2（逐模型探针）换成替身；不做任何真实网络请求。"""
    class _Result:
        reachable = True
        latency_ms = 5.0
        error_class = None
        model_checks = [{"model": name, "ok": True, "error_class": None} for name in listed]

    monkeypatch.setattr("djb_node.upstream.probe_upstream", lambda *a, **k: _Result())
    monkeypatch.setattr("djb_node.upstream.probe_mod.probe_models",
                        lambda *a, **k: dict(outcomes))


def _seed_upstream(store, credentials, *, state="verified", models=FIVE):
    store.add_upstream(upstream_id="u1", access_type="A2",
                       base_url="https://upstream.invalid/v1",
                       models=[{"id": name, "enabled": True, "min_price": "0.5"}
                               for name in models],
                       credential_ref="upstream:u1")
    store.update_upstream("u1", state=state)
    credentials.set_upstream_secret("u1", "sk-test-not-a-real-key")


def test_all_not_probed_keeps_previous_state(store, credentials, monkeypatch):
    """全 not_probed（预算截断）= 没证据 ⇒ 本地 state 不动（否则 quotes 会被门掉）。"""
    _seed_upstream(store, credentials, state="verified")
    _patch_probe(monkeypatch, listed=("m1", "m2", "m3", "m4", "m5"), outcomes={})
    UpstreamManager(store, credentials).probe("u1")
    assert store.upstream("u1").state == "verified"


def test_real_failure_sets_rejected(store, credentials, monkeypatch):
    """有明确失败结论 ⇒ 仍然 rejected（无证据≠失败，别把两者混为一谈）。"""
    _seed_upstream(store, credentials, state="verified")

    class _Outcome:
        inference_ok = False
        stream_ok = False
        usage_present = False
        error_class = "timeout"

    _patch_probe(monkeypatch, listed=("m1", "m2"), outcomes={"m1": _Outcome()})
    UpstreamManager(store, credentials).probe("u1")
    assert store.upstream("u1").state == "rejected"


def test_cli_probe_with_no_enabled_models_does_not_report(home, mock_server, capsys):
    """P2-9：全模型停用 ⇒ CLI 不上报（原因码与云端 `no_enabled_models` 对齐）。"""
    import json

    from djb_node.cli import main
    from djb_node.config import Paths
    from djb_node.secure import CredentialStore
    from tests.mock_m02 import CLIENT_KEY

    assert main(["init", "--no-env", "--gateway", mock_server.base_url]) == 0
    capsys.readouterr()
    CredentialStore(Paths(str(home))).set_node("node-mock-1", CLIENT_KEY)
    assert main(["upstream", "add", "--access", "A1", "--base-url",
                 mock_server.upstream_base_url, "--models", "mock-model",
                 "--secret", "secret-value", "--json"]) == 0
    upstream_id = json.loads(capsys.readouterr().out.strip())["upstream"]["id"]
    assert main(["upstream", "disable", "--id", upstream_id,
                 "--model", "mock-model", "--json"]) == 0
    capsys.readouterr()
    assert main(["upstream", "probe", "--id", upstream_id, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["reported"] is False
    assert payload["report_error"] == "no_enabled_models"
    assert payload["model_checks"] == []


# ─────────────── D-RCA §3.2 用例 1 / 2：0.3.8 断流的两个方向（0.3.9 门禁用例）
def _daemon_real_probe(paths, store, credentials, mock, clock):
    """真跑 ``UpstreamManager.probe``（只把 L1/L2 的网络换成 ``_patch_probe`` 的替身）。"""
    daemon = _daemon(paths, store, credentials, mock, clock=clock)
    reports: list[dict] = []
    daemon._report_probe = lambda record, value, *, reason: reports.append(value)
    return daemon, reports


def test_one_timeout_four_ok_keeps_verified_and_four_quote_rows(
        paths, store, credentials, mock, clock, monkeypatch):
    """**D-RCA §3.2 用例 1**：5 个 enabled 模型，qwen timeout + 其余 4 ok。

    0.3.8 现场（2026-11-16 13:54→13:55）：qwen3.8-max 一个 ``timeout`` ⇒
    ``upstream.state=rejected`` ⇒ ``quotes.py`` 的 ``gated`` 跳过该上游**全部**模型 ⇒
    ``quotes rows=0`` 整条断流。修后必须：``state=verified``、报价保留 4 行、
    qwen 只按模型 ``not_probed`` 跳过（不出现 ``upstream_gated``）、daemon 计
    ``verified`` 并上报（``real_failures`` 里仍如实留着 qwen）。
    """
    _seed_upstream(store, credentials, models=FIVE)
    _patch_probe(monkeypatch, listed=FIVE,
                 outcomes={"m1": _Outcome(), "m2": _Outcome(), "m3": _Outcome(),
                           "m4": _Outcome(), "qwen3.8-max": _Outcome(False, "timeout")})
    daemon, reports = _daemon_real_probe(paths, store, credentials, mock, clock)

    result = daemon._probe_upstreams(clock(), reason="periodic")

    assert store.upstream("u1").state == "verified", "4 ok 不能被 1 个 timeout 判死整条上游"
    plan = plan_quotes(daemon.upstreams, now=clock())
    assert sorted(row.model for row in plan.rows) == ["m1", "m2", "m3", "m4"]
    assert SKIP_UPSTREAM not in plan.skipped, "不许再出现 upstream_gated（0.3.8 的断流点）"
    assert plan.skipped.get(SKIP_NOT_PROBED) == 1, "qwen 只按模型跳过"
    assert result["verified"] == 1 and result["failed"] == 0
    assert result["per_upstream"][0]["ok"] is True
    assert result["per_upstream"][0]["real_failures"] == ["qwen3.8-max"]
    assert len(reports) == 1, "有 4 个 ok 模型就必须上报，否则云端 verified_at 会冻结"
    checks = {item["model"]: item for item in reports[0]["model_checks"]}
    assert checks["qwen3.8-max"]["error_class"] == "timeout", "P0-c：timeout 诊断必须保留"


def test_all_timeout_is_rejected_and_quotes_gated(paths, store, credentials, mock, clock,
                                                 monkeypatch):
    """**D-RCA §3.2 用例 2**：5 个模型全 timeout ⇒ rejected、rows=0、不上报。

    这是「不许为了保 4 行把全量故障洗成可用」的那条线：P0-c 之所以拒绝把 ``timeout``
    从 ``real_failures`` 里抹掉，就是为了让这条成立。
    """
    _seed_upstream(store, credentials, models=FIVE)
    _patch_probe(monkeypatch, listed=FIVE,
                 outcomes={name: _Outcome(False, "timeout") for name in FIVE})
    daemon, reports = _daemon_real_probe(paths, store, credentials, mock, clock)

    result = daemon._probe_upstreams(clock(), reason="periodic")

    assert store.upstream("u1").state == "rejected"
    plan = plan_quotes(daemon.upstreams, now=clock())
    assert plan.rows == [] and plan.skipped.get(SKIP_UPSTREAM) == len(FIVE)
    assert result["verified"] == 0 and result["failed"] == 1
    assert reports == []


# ─────────────────────────────────────────── P1-a：单模型预算切片（不许饿死其他模型）
def _fake_slow_probe(clock, seen):
    def fake(base_url, model, *, credential=None, level=None, timeout=None,
             http=None, protocol=None):
        seen.append((str(model), float(timeout)))
        clock.advance(float(timeout))          # 模拟「每个模型都把自己的切片烧满」
        return _Outcome()
    return fake


def test_model_budget_slicing_prevents_slow_model_starvation(clock, monkeypatch):
    """P1-a：慢模型排第一也只吃 ``remaining/remaining_models``，其余 4 个同一轮仍被探到。

    0.3.8 把完整 ``PROBE_TIMEOUT_S``(20s) 交给**每个**模型、只在每个模型开始前看一次
    deadline ⇒ 首模型烧满 20s 后其余全部 ``not_probed``（5 模型最坏 ~100s > 30s 心跳上限）。
    """
    seen: list[tuple[str, float]] = []
    monkeypatch.setattr(probe_mod, "probe_model", _fake_slow_probe(clock, seen))
    deadline = clock() + timedelta(seconds=facts.PROBE_PASS_BUDGET_S)

    outcomes = probe_mod.probe_models("https://upstream.invalid/v1", list(FIVE),
                                      deadline=deadline, clock=clock, level="L2",
                                      timeout=facts.PROBE_TIMEOUT_S)

    assert list(outcomes) == list(FIVE), "5 个模型都必须被探到（旧行为只探得到第一个）"
    assert [timeout for _, timeout in seen] == pytest.approx(
        [facts.PROBE_PASS_BUDGET_S / len(FIVE)] * len(FIVE)), "20s / 5 ≈ 4s，不是每人 20s"
    assert max(timeout for _, timeout in seen) <= facts.PROBE_TIMEOUT_S


def test_model_slice_floors_at_min_model_timeout(clock, monkeypatch):
    """P1-a 下限：预算不够时每个模型仍拿 ``PROBE_MIN_MODEL_TIMEOUT_S``（默认 2s）。"""
    seen: list[tuple[str, float]] = []
    monkeypatch.setattr(probe_mod, "probe_model", _fake_slow_probe(clock, seen))
    deadline = clock() + timedelta(seconds=3)          # 3s / 5 模型 = 0.6s < 2s 下限

    probe_mod.probe_models("https://upstream.invalid/v1", list(FIVE),
                           deadline=deadline, clock=clock, level="L2",
                           timeout=facts.PROBE_TIMEOUT_S)

    assert seen and all(timeout == facts.PROBE_MIN_MODEL_TIMEOUT_S
                        for _, timeout in seen)
    assert len(seen) == 2, "3s 预算 + 2s 下限 ⇒ 只跑得完 2 个，其余进 backlog（不算失败）"


# ─────────────────────── P1-b：backlog 只补缺口，已知 timeout 排最后，不复探刚 ok 的模型
def _patch_rounds(monkeypatch, calls, rounds):
    """L1 恒「5 个模型都在清单里」；L2 按 ``rounds`` 逐轮返回，并记录每轮的探测顺序。"""
    class _Result:
        reachable = True
        latency_ms = 5.0
        error_class = None
        model_checks = [{"model": name, "ok": True, "error_class": None} for name in FIVE]

    def fake_probe_models(base_url, models, **kwargs):
        calls.append(list(models))
        return dict(rounds[len(calls) - 1])

    monkeypatch.setattr("djb_node.upstream.probe_upstream", lambda *a, **k: _Result())
    monkeypatch.setattr("djb_node.upstream.probe_mod.probe_models", fake_probe_models)


def _round(manager, clock, *, budgeted=True):
    deadline = (clock() + timedelta(seconds=facts.PROBE_PASS_BUDGET_S)
                if budgeted else None)
    return manager.probe("u1", level="L2", now=clock(), deadline=deadline, clock=clock)


def test_backlog_round_prefers_unprobed_and_defers_known_timeout(store, credentials, clock,
                                                                monkeypatch):
    """P1-b **排序**：下一轮先探没轮到的、最旧的靠前，**已知 timeout 排最后**。

    0.3.4 的逐模型 stale（``last_probe_at`` 爬到 635s、5 行掉到 1 行）与 0.3.8 的
    「慢模型每轮先吃预算、其余永远 ``not_probed``」都是同一类「没轮到」的累积：
    固定按上游 ``/models`` 顺序探，队尾模型永远等不到。
    把排序改回原顺序（``listed`` 原样）⇒ 本用例红。
    """
    _seed_upstream(store, credentials, models=FIVE)
    rounds = [{"m1": _Outcome(), "m2": _Outcome(),
               "qwen3.8-max": _Outcome(False, "timeout")},        # 第 1 轮：m3/m4 没轮到
              {name: _Outcome() for name in FIVE},
              {name: _Outcome() for name in FIVE}]
    calls: list[list[str]] = []
    _patch_rounds(monkeypatch, calls, rounds)
    manager = UpstreamManager(store, credentials)

    _round(manager, clock)
    assert calls[0] == list(FIVE)
    assert store.upstream("u1").state == "verified"

    clock.advance(facts.PROBE_REUSE_OK_S + 1)      # 复用窗过期，但缺口还在（m3/m4/qwen）
    _round(manager, clock)
    assert calls[1] == ["m3", "m4", "m1", "m2", "qwen3.8-max"], \
        "没轮到的 m3/m4 先探，其次是最旧的 m1/m2，已知 timeout 的 qwen 排最后"


def test_backlog_round_does_not_reprobe_recent_ok(store, credentials, clock, monkeypatch):
    """P1-b **复用**：5s backlog 续跑只补缺口，**不复探几秒前刚 ok 的模型**。

    没有这一条，20s 预算里 4 个刚 ok 的模型会把预算重新吃光，缺口依旧轮不到；
    同时复用**不得刷新** ``last_probe_at``（不伪造新鲜度），缺口补齐后不许再报 backlog
    （否则会 5s 空转）。把复用整段去掉 ⇒ 本用例红。
    """
    _seed_upstream(store, credentials, models=FIVE)
    rounds = [{"m1": _Outcome(), "m2": _Outcome(),
               "qwen3.8-max": _Outcome(False, "timeout")},
              {"m3": _Outcome(), "m4": _Outcome(), "qwen3.8-max": _Outcome()},
              {name: _Outcome() for name in FIVE}]
    calls: list[list[str]] = []
    _patch_rounds(monkeypatch, calls, rounds)
    manager = UpstreamManager(store, credentials)

    _round(manager, clock)
    first_pass = {item["id"]: item.get("last_probe_at")
                  for item in store.upstream("u1").models}

    clock.advance(facts.PROBE_BACKLOG_RETRY_S)                 # 5s backlog 续跑
    payload = _round(manager, clock)
    assert calls[1] == ["m3", "m4", "qwen3.8-max"], \
        "只补缺口 m3/m4 + 已知 timeout 的 qwen；刚 ok 的 m1/m2 这一轮不重探"
    second_pass = {item["id"]: item.get("last_probe_at")
                   for item in store.upstream("u1").models}
    assert second_pass["m1"] == first_pass["m1"] == first_pass["m2"], \
        "复用的模型不刷新 last_probe_at（不伪造新鲜度）"
    assert second_pass["m3"] is not None and second_pass["qwen3.8-max"] is not None
    assert payload["not_probed"] == [], "缺口补齐后不再有 backlog（否则会 5s 空转）"
    assert store.upstream("u1").state == "verified"

    clock.advance(facts.PROBE_REUSE_OK_S + 1)                  # 复用窗过期
    _round(manager, clock)
    assert len(calls[2]) == len(FIVE), "复用窗过期后整轮实探，不靠旧结论混过去"


def test_manual_probe_without_deadline_always_probes_every_model(store, credentials, clock,
                                                                monkeypatch):
    """P1-b 边界：手动 ``djb upstream probe``（``deadline=None``）永远实探，不复用旧结论。"""
    _seed_upstream(store, credentials, models=FIVE)
    calls: list[list[str]] = []

    class _Result:
        reachable = True
        latency_ms = 5.0
        error_class = None
        model_checks = [{"model": name, "ok": True, "error_class": None} for name in FIVE]

    def fake_probe_models(base_url, models, **kwargs):
        calls.append(list(models))
        return {str(name): _Outcome() for name in models}

    monkeypatch.setattr("djb_node.upstream.probe_upstream", lambda *a, **k: _Result())
    monkeypatch.setattr("djb_node.upstream.probe_mod.probe_models", fake_probe_models)
    manager = UpstreamManager(store, credentials)

    manager.probe("u1", level="L2", now=clock(),
                  deadline=clock() + timedelta(seconds=facts.PROBE_PASS_BUDGET_S),
                  clock=clock())                               # 预算轮：全探一遍
    manager.probe("u1", level="L3", now=clock())                # CLI 手动：无 deadline
    assert len(calls[1]) == len(FIVE), "手动探针不得复用上一轮结论"
