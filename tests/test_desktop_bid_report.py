# SPDX-License-Identifier: Apache-2.0
"""R8-F6：逐模型底价「一次性上报到云端」——真 POST /api/v1/schedule/quotes。

客户可见的验收口径（R8-FIX-DESIGN §5）：

* 按钮从「假成功/置灰」变成**真上报**：点击 → 预检 → 确认弹窗（模型+底价+TTL+条数）
  → 逐条 POST；一次性上报**不看** `client.upstream.bid_report.enabled`（该 flag 只管
  daemon 自动续报，默认保持 False）；
* 失败分类给可执行下一步；空计划用人话列跳过原因，**绝不说已上报**；
* 结果（running/ok/fail + 文案）常驻 Token Plan 页，按钮上报中置灰。

全部离线：上行端点由 `tests.mock_m02.MockServer` 承接，不联网、不连数据库。
"""
from __future__ import annotations

import pytest

from djb_node import bids as bids_mod
from djb_node import facts, quotes
from djb_node.config import Paths, feature_flag, load_config, save_config
from djb_node.desktop import controller as ctrl_mod
from djb_node.desktop import datasource as ds
from djb_node.desktop import views as vw
from djb_node.secure import CredentialStore
from djb_node.util import iso, utcnow
from tests.mock_m02 import CLIENT_KEY, NODE_ID


# ─────────────────────────────────────────────────────────── 夹具 / 工具
def _seed(store, *, upstream_id: str = "u1", access: str = "B", models=(),
          state: str = "verified") -> None:
    """落库一条「已探针」的上游（与 test_quotes._seed 同口径）。"""
    from datetime import timedelta

    moment = utcnow()
    entries = []
    for item in models:
        entries.append({
            "id": item["id"],
            "enabled": bool(item.get("enabled", True)),
            "min_price": item.get("min_price"),
            "min_price_unit": facts.DEFAULT_MIN_PRICE_UNIT,
            "inference_ok": item.get("inference_ok", True),
            "last_probe_at": iso(moment - timedelta(seconds=float(item.get("age_s", 0)))),
        })
    store.add_upstream(upstream_id=upstream_id, access_type=access,
                       base_url="https://agg.example.com/v1", models=entries,
                       credential_ref=None, state=state)


def _point_at_mock(paths: Paths, mock, extra=None) -> None:
    config = load_config(paths)
    config.gateway_url = mock.base_url
    if extra:
        config.extra.update(extra)
    save_config(paths, config)


def _bound_snapshot(*, bound: bool = True) -> ds.Snapshot:
    return ds.Snapshot({
        "access": {"binding": {"bound": bool(bound)}, "config": {}},
        "account": {"profile": {"node_id": NODE_ID}},
    })


def _page_snapshot(*, bound: bool = True) -> ds.Snapshot:
    """带 token_plan 区的快照（视图断言用；与 test_desktop_bids._plan_snapshot 同形）。"""
    return ds.Snapshot({
        "access": {"binding": {"bound": bool(bound)}, "config": {}},
        "account": {"profile": {"node_id": NODE_ID}},
        "token_plan": {
            "upstreams": [{"id": "u1", "access_type": "B", "state": "verified",
                           "enabled": True, "model_count": 1, "verified_models": 1,
                           "max_qps": 1.0, "last_probe_at": "2026-11-14T10:00:00Z"}],
            "models": [{"model": "gpt-4o-mini", "upstream_id": "u1",
                        "canonical": "gpt-4o-mini", "enabled": True,
                        "announceable": True}],
            "bids": {"rows": {"gpt-4o-mini": {"min_price": "0.35"}},
                     "cloud": {"state": "ok", "source": "cloud"},
                     "report_blocker": "",
                     "preview": {"models": [{"model": "gpt-4o-mini",
                                            "min_price": "0.35"}]}},
            "protocol": facts.DEFAULT_UPSTREAM_PROTOCOL, "base_url": "",
            "secret_seen": "未保存", "discover_note": "",
            "limits": {}, "usage": [], "note": "",
        },
    })


class _Source:
    """最小数据源：本机 home + store + 固定快照（不发任何网络）。"""

    def __init__(self, home, store, *, bound: bool = True, page: bool = False):
        self._home = str(home)
        self._store = lambda: store
        self._snapshot = _page_snapshot(bound=bound) if page else _bound_snapshot(bound=bound)

    def snapshot(self, sections=None):
        return self._snapshot

    def bids_store(self):
        return self._store()


@pytest.fixture
def env(home, paths, store, credentials, mock):
    """已绑定 + 网关指向 mock + 一本账本可写的最小环境。"""
    _point_at_mock(paths, mock)
    credentials.set_node(NODE_ID, CLIENT_KEY)
    return {"home": home, "paths": paths, "store": store, "mock": mock,
            "credentials": credentials}


def _controller(env, *, bound: bool = True, page: bool = False) -> ctrl_mod.DesktopController:
    source = _Source(env["home"], env["store"], bound=bound, page=page)
    return ctrl_mod.DesktopController(source)


def _confirm_yes(_message: str) -> bool:
    return True


# ─────────────────────────────────────────────────────────── 1. 真上报
def test_report_posts_one_quote_per_ledger_priced_model(env):
    store, mock = env["store"], env["mock"]
    _seed(store, models=[{"id": "gpt-4o-mini", "min_price": "0.75"},
                         {"id": "deepseek-chat", "min_price": "0.5"}])
    bids_mod.BidBook(store).set_price("gpt-4o-mini", "0.35")
    bids_mod.BidBook(store).set_price("deepseek-chat", "1.5")
    controller = _controller(env)

    result = controller.action("token_plan.report", confirm=_confirm_yes)
    assert result.effect == "bid_report" and result.ok
    plan = result.payload["plan"]
    assert [row.model for row in plan.rows] == ["deepseek-chat", "gpt-4o-mini"]
    assert plan.ttl_s == 120

    outcome = controller.run_bid_report(plan)
    assert outcome.ok
    assert len(mock.quotes) == 2
    by_model = {body["model"]: body for body in mock.quotes}
    # 底价来自本机账本（不是 ModelEntry 的 0.75/0.5）
    assert by_model["gpt-4o-mini"]["price_min"] == 35
    assert by_model["deepseek-chat"]["price_min"] == 150
    for body in mock.quotes:
        assert body["access"] == "B" and body["unit"] == "token"
        assert body["ttl_s"] == 120 and body["currency"] == "USD"
        assert body["node_id"] == NODE_ID
    assert controller.bid_report["state"] == "ok"
    assert "已上报 2 个模型" in controller.bid_report["text"]


# ─────────────────────────────────────────────────────────── 2. 预检
def test_report_preflight_blocks_unbound_node(env):
    store, mock = env["store"], env["mock"]
    _seed(store, models=[{"id": "gpt-4o-mini", "min_price": "0.75"}])
    bids_mod.BidBook(store).set_price("gpt-4o-mini", "0.35")
    controller = _controller(env, bound=False)

    result = controller.action("token_plan.report", confirm=_confirm_yes)
    assert not result.ok and "绑定" in result.message
    assert result.effect != "bid_report"
    assert mock.quotes == []                       # 一个字节都没上行


def test_report_preflight_blocks_missing_client_key(env):
    store, mock = env["store"], env["mock"]
    env["credentials"].clear_node()                # 已解绑：本机没有节点密钥
    _seed(store, models=[{"id": "gpt-4o-mini", "min_price": "0.75"}])
    bids_mod.BidBook(store).set_price("gpt-4o-mini", "0.35")
    controller = _controller(env)

    result = controller.action("token_plan.report", confirm=_confirm_yes)
    assert not result.ok and "密钥" in result.message and "重新绑定" in result.message
    assert mock.quotes == []


def test_report_with_an_empty_ledger_says_nothing_was_sent(env):
    _seed(env["store"], models=[{"id": "gpt-4o-mini", "min_price": "0.75"}])
    controller = _controller(env)
    asked = []

    result = controller.action("token_plan.report",
                               confirm=lambda message: asked.append(message) or True)
    assert not result.ok and "还没有任何底价" in result.message
    assert "已上报" not in result.message
    assert asked == []                             # 没东西可发就不弹「即将上报」
    assert controller.bid_report["state"] == "fail"
    assert env["mock"].quotes == []


# ─────────────────────────────────────────────────────────── 3. TTL / 周期
def test_report_uses_configured_period_ttl(env):
    store, mock, paths = env["store"], env["mock"], env["paths"]
    _point_at_mock(paths, mock, {"quotes.period_s": 90})
    _seed(store, models=[{"id": "gpt-4o-mini", "min_price": "0.75"}])
    bids_mod.BidBook(store).set_price("gpt-4o-mini", "0.35")
    controller = _controller(env)

    result = controller.action("token_plan.report", confirm=_confirm_yes)
    assert result.effect == "bid_report"
    assert result.payload["plan"].ttl_s == 180
    controller.run_bid_report(result.payload["plan"])
    assert mock.quotes[0]["ttl_s"] == 180


def test_report_fails_closed_on_inconsistent_ttl_config(env):
    store, mock, paths = env["store"], env["mock"], env["paths"]
    _point_at_mock(paths, mock, {"quotes.period_s": 60, "quotes.ttl_s": 300})
    _seed(store, models=[{"id": "gpt-4o-mini", "min_price": "0.75"}])
    bids_mod.BidBook(store).set_price("gpt-4o-mini", "0.35")
    controller = _controller(env)

    result = controller.action("token_plan.report", confirm=_confirm_yes)
    assert not result.ok and "TTL" in result.message and "quotes" in result.message
    assert mock.quotes == []
    assert controller.bid_report["state"] == "fail"


# ─────────────────────────────────────────────────────────── 4. 确认弹窗
def test_confirm_lists_models_prices_ttl_and_skips(env):
    store, mock = env["store"], env["mock"]
    _seed(store, models=[
        {"id": "gpt-4o-mini", "min_price": "0.75"},
        {"id": "stale-model", "min_price": "0.5",
         "age_s": facts.PROBE_FRESH_AFTER_S + 60},
    ])
    bids_mod.BidBook(store).set_price("gpt-4o-mini", "0.35")
    bids_mod.BidBook(store).set_price("stale-model", "0.2")
    controller = _controller(env)
    captured: list[str] = []

    result = controller.action(
        "token_plan.report",
        confirm=lambda message: captured.append(message) or False)
    text = captured[0]
    assert "gpt-4o-mini" in text and "0.35" in text
    assert "TTL 120s" in text and "条报价" in text
    assert "进入撮合" in text and "不会自动续期" in text
    assert "单条失败不影响其它模型" in text
    assert "本次只上报一次" in text and "不会打开自动上报" in text
    # 账本里有价、但探针过期 → 明确写「本次不会上报」
    assert "stale-model" in text and "不会上报" in text
    assert result.effect == "cancelled" and not result.ok
    assert mock.quotes == []                       # 取消 = 零请求


# ─────────────────────────────────────────────────────────── 5. 失败分类 / 复用
def test_report_partial_failure_counts_and_gives_next_step(env):
    store, mock = env["store"], env["mock"]
    _seed(store, models=[{"id": "bad", "min_price": "0.5"},
                         {"id": "good", "min_price": "0.5"}])
    bids_mod.BidBook(store).set_price("bad", "0.35")
    bids_mod.BidBook(store).set_price("good", "0.35")
    mock.quote_fail_models = {"bad"}
    controller = _controller(env)

    result = controller.action("token_plan.report", confirm=_confirm_yes)
    outcome = controller.run_bid_report(result.payload["plan"])
    assert not outcome.ok
    assert "已上报 1 个模型" in outcome.message
    assert "失败 1" in outcome.message
    assert "参数被云端拒绝" in outcome.message       # 400 → invalid 的下一步
    assert len(mock.quotes) == 2                   # 成功的没回滚，失败的不拖整批
    assert controller.bid_report["state"] == "fail"


def test_report_auth_failure_points_to_rebinding(env):
    store, mock = env["store"], env["mock"]
    env["credentials"].set_node(NODE_ID, "ck_wrong")
    _seed(store, models=[{"id": "gpt-4o-mini", "min_price": "0.75"}])
    bids_mod.BidBook(store).set_price("gpt-4o-mini", "0.35")
    controller = _controller(env)

    result = controller.action("token_plan.report", confirm=_confirm_yes)
    outcome = controller.run_bid_report(result.payload["plan"])
    assert not outcome.ok and "失败 1" in outcome.message
    assert "重新绑定" in outcome.message
    assert controller.bid_report["state"] == "fail"


@pytest.mark.parametrize("status,fragment", [
    (403, "节点状态"),
    (429, "限流"),
])
def test_report_http_failures_carry_their_own_next_step(env, status, fragment):
    """失败分类不是一句「失败了」：403 → 节点状态；429 → 限流稍后。"""
    store, mock = env["store"], env["mock"]
    _seed(store, models=[{"id": "gpt-4o-mini", "min_price": "0.75"}])
    bids_mod.BidBook(store).set_price("gpt-4o-mini", "0.35")
    mock.quote_status = status
    controller = _controller(env)

    result = controller.action("token_plan.report", confirm=_confirm_yes)
    outcome = controller.run_bid_report(result.payload["plan"])
    assert not outcome.ok and fragment in outcome.message
    assert controller.bid_report["state"] == "fail"


def test_report_reused_does_not_extend_ttl(env):
    store, mock = env["store"], env["mock"]
    _seed(store, models=[{"id": "gpt-4o-mini", "min_price": "0.75"}])
    bids_mod.BidBook(store).set_price("gpt-4o-mini", "0.35")
    controller = _controller(env)

    first = controller.action("token_plan.report", confirm=_confirm_yes)
    one = controller.run_bid_report(first.payload["plan"])
    assert one.ok and "新建 1" in one.message
    second = controller.action("token_plan.report", confirm=_confirm_yes)
    two = controller.run_bid_report(second.payload["plan"])
    assert "复用 1" in two.message
    assert "不会续期" in two.message and "到期时间不变" in two.message
    assert len(mock.quote_keys) == 1               # 内容指纹相同：云端只留一条


# ─────────────────────────────────────────────────────────── 6. flag / 视图
def test_report_never_flips_the_auto_report_flag(env):
    store, paths = env["store"], env["paths"]
    _seed(store, models=[{"id": "gpt-4o-mini", "min_price": "0.75"}])
    bids_mod.BidBook(store).set_price("gpt-4o-mini", "0.35")
    controller = _controller(env)

    result = controller.action("token_plan.report", confirm=_confirm_yes)
    assert controller.run_bid_report(result.payload["plan"]).ok
    config = load_config(paths)
    assert feature_flag(config, facts.UPSTREAM_BID_FLAG) is False
    assert feature_flag(config, facts.QUOTES_FLAG) is False


def test_report_result_stays_on_the_page_and_disables_the_button(env):
    controller = _controller(env, page=True)
    controller.set_bid_report("running", "正在上报 2 个模型到云端（TTL 120s）…",
                              count=2, ttl_s=120)
    view = controller.show("token_plan")
    assert any("正在上报 2 个模型" in note for note in view.notes)
    action = next(item for item in view.actions if item.key == "token_plan.report")
    assert action.enabled is False and "上报中" in action.label
    assert "正在上报" in action.reason

    controller.set_bid_report("ok", "已上报 2 个模型（新建 2 · 复用 0 · 失败 0），有效期至 12:00")
    view = controller.show("token_plan")
    assert any("已上报 2 个模型" in note for note in view.notes)
    enabled = next(item for item in view.actions if item.key == "token_plan.report")
    assert enabled.enabled is True and enabled.reason == ""


def test_app_dispatches_the_report_to_a_worker_and_applies_the_result(env):
    """Tk 主线程不发 HTTP：effect="bid_report" 交给 RefreshWorker，结果回投页面。"""
    import time

    from djb_node.desktop.app import DesktopApp

    store = env["store"]
    _seed(store, models=[{"id": "gpt-4o-mini", "min_price": "0.75"}])
    bids_mod.BidBook(store).set_price("gpt-4o-mini", "0.35")
    controller = _controller(env)
    result = controller.action("token_plan.report", confirm=_confirm_yes)

    class _Root:
        def __init__(self) -> None:
            self.jobs = []

        def after(self, delay, fn):
            self.jobs.append(fn)
            return len(self.jobs)

        def after_cancel(self, job):
            return None

    class _App(DesktopApp):
        """不建 Tk 的最小壳：只验证派发 / 回投两条边。"""

        def __init__(self, controller, root):
            self._controller = controller
            self._root = root
            self._force_next_render = False
            self.toasts = []
            self.rendered = []

        def _render(self, view):
            self.rendered.append(view)

        def _toast(self, message, *, ok=True):
            self.toasts.append((message, ok))

        def _update_status(self):
            return None

    app = _App(controller, _Root())
    app._start_bid_report(result.payload["plan"], result.message)
    worker = app._bid_report_worker
    assert worker is not None
    deadline = time.time() + 5.0
    while worker.busy and time.time() < deadline:
        time.sleep(0.01)
    assert worker.poll_once()
    assert len(env["mock"].quotes) == 1
    assert controller.bid_report["state"] == "ok"
    assert any("已上报" in text for text, _ok in app.toasts)


def test_report_action_reason_is_concrete_for_an_unbound_node(env):
    controller = _controller(env, bound=False, page=True)
    view = controller.show("token_plan")
    action = next(item for item in view.actions if item.key == "token_plan.report")
    assert action.enabled is False and "绑定" in action.reason
    assert "未开通" not in action.reason

