# SPDX-License-Identifier: Apache-2.0
"""D2 + D4（2026-11-18 客户报障）：版本如实 + 失败提示不许被刷新吃掉。

真机现象（报障原文）：「点 OK 没反应，客户端版本 0.3.11，绑定页面显示 0.3.0」。

* **D4**：``_toast(ok=False)`` 只写状态条，``_update_status()``（每轮刷新都调）随即用
  ``_status_line()`` 覆写 ⇒ 错误行活不过 10 秒。本文件钉住「失败粘住、成功即清、
  换页/新动作才退场」。
* **D2**：版本卡读 ``update.status()["current_version"]``（= 安装记录），手工解压换包
  不写记录 ⇒ 侧栏 v0.3.11 与卡面 0.3.0 自相矛盾。本文件钉住「卡面写程序版本」+
  「启动自检把记录对齐到程序版本」。
"""
from __future__ import annotations

from typing import Any

import pytest

from djb_node import facts
from djb_node.desktop import app as desktop_app
from djb_node.desktop import controller as desktop_controller
from djb_node.desktop.controller import DesktopController
from djb_node.desktop.views import build_views
from djb_node.desktop import datasource as ds
from djb_node.desktop.datasource import FakeDataSource, ProviderError
from djb_node.update import UpdateManager


class _Label:
    """假状态栏 / 右侧标签（``configure(**kwargs)`` 就是它们唯一会收到的调用）。"""

    def __init__(self) -> None:
        self.text = ""
        self.foreground = ""
        self.calls: list[dict[str, Any]] = []

    def configure(self, **kwargs: Any) -> None:
        self.calls.append(dict(kwargs))
        if "text" in kwargs:
            self.text = str(kwargs.get("text") or "")
        if "foreground" in kwargs:
            self.foreground = str(kwargs.get("foreground") or "")


def _bare_app(status_line: str = "已连接 · 3 上游") -> desktop_app.DesktopApp:
    """只装 ``_toast`` / ``_update_status`` / ``_clear_sticky`` 需要的最小属性。"""
    app = desktop_app.DesktopApp.__new__(desktop_app.DesktopApp)
    app._status = _Label()                       # noqa: SLF001 - 最小装配
    app._status_right = _Label()                 # noqa: SLF001
    app._sticky_error = ""                       # noqa: SLF001
    app._sticky_job = None                       # noqa: SLF001
    app._theme_name = "day"                      # noqa: SLF001 - _status_right_text 要它
    app._controller = type("C", (), {"snapshot": {}})()   # noqa: SLF001
    app._status_line = lambda: status_line       # noqa: SLF001 - 实例属性遮蔽方法
    app._pill_text = lambda: ("RUNNING", "ok")   # noqa: SLF001
    app._update_identity = lambda: None          # noqa: SLF001
    app._update_clock = lambda: None             # noqa: SLF001
    return app


# ─────────────────────────────────────────────────────────── D4：失败提示粘住
def test_failed_toast_survives_status_refresh():
    """失败文案必须活过 :meth:`_update_status`（旧行为：10 秒内被「已连接 · …」盖掉）。"""
    app = _bare_app()
    app._toast("绑定未成功：绑定码已过期，请重新生成", ok=False)
    assert app._status.text.startswith("! ")                      # noqa: SLF001
    assert "绑定码已过期" in app._status.text                      # noqa: SLF001

    app._update_status()                                          # 模拟下一轮刷新
    assert "绑定码已过期" in app._status.text, "失败提示被常规状态行覆盖了"  # noqa: SLF001
    assert app._status.foreground == desktop_app.dual.color("red")  # noqa: SLF001


def test_success_toast_replaces_sticky_error():
    """成功播报即清粘住态（否则旧的失败红字会一直挂着，反而骗人）。"""
    app = _bare_app()
    app._toast("绑定未成功：绑定码已过期", ok=False)
    app._toast("已保存接入配置", ok=True)
    assert app._sticky_error == ""                                # noqa: SLF001
    assert app._status.text.startswith("✓ ")                      # noqa: SLF001
    app._update_status()
    assert app._status.text == "已连接 · 3 上游"                   # noqa: SLF001


def test_clear_sticky_restores_regular_status_line():
    app = _bare_app("已连接 · 2 上游")
    app._toast("绑定未成功：绑定码已过期", ok=False)
    app._clear_sticky()                                           # noqa: SLF001
    assert app._sticky_error == ""                                # noqa: SLF001
    assert app._status.text == "已连接 · 2 上游"                   # noqa: SLF001


def test_new_action_clears_previous_error():
    """按了新动作 = 上一条失败已经看过；新失败会重新粘上。"""
    app = _bare_app()
    app._vars = {}                                                # noqa: SLF001
    app._render = lambda view: None                               # noqa: SLF001
    app._toast("绑定未成功：绑定码已过期", ok=False)

    class _C:
        snapshot: dict[str, Any] = {}

        def set_draft(self, key, value):                          # noqa: ANN001 - 桩
            return None

        def action(self, key, confirm=None):                      # noqa: ANN001 - 桩
            from djb_node.desktop.controller import ActionResult
            return ActionResult("失败原因：" + key, ok=False)

        def current(self):                                        # noqa: ANN001 - 桩
            return build_views(desktop_app.demo_snapshot())["overview"]

    app._controller = _C()                                        # noqa: SLF001
    app._on_action("account.bind")                                # noqa: SLF001
    # 新动作用新的失败文案替换旧的（清 → 立刻重新粘上），绝不留上一条的原文
    assert app._sticky_error.startswith("! 失败原因：account.bind")  # noqa: SLF001


# ─────────────────────────────────────────────────────────── D4：失败文案带下一步
def test_bind_failure_with_expired_code_tells_how_to_fix():
    """服务端 409（``binding_code_expired``）→ 文案必须说「去网页端重新生成」。"""
    controller = DesktopController(
        FakeDataSource({"account": {"profile": {}, "security": {}, "note": ""}}, source="test"),
        cli=_raise(ProviderError("绑定码已过期，请重新生成 [reason=binding_code_expired]")),
        ask_code=lambda: "kyet-expired-code")
    result = controller.action("account.bind")
    assert result.ok is False
    assert "绑定码已过期" in result.message
    assert "重新生成" in result.message, "失败文案没给出下一步（客户只能再点一次 OK）"


def test_bind_failure_with_network_error_has_no_code_hint():
    """网络类失败不许挂「码过期」的引导（会把客户指向错误的原因）。"""
    controller = DesktopController(
        FakeDataSource({"account": {"profile": {}, "security": {}, "note": ""}}, source="test"),
        cli=_raise(ProviderError("本地命令超时（20s）")),
        ask_code=lambda: "any-code")
    result = controller.action("account.bind")
    assert result.ok is False
    assert desktop_controller.BIND_CODE_HINT not in result.message


def _snapshot_with_update(update: dict[str, Any]) -> Any:
    """演示快照 + 指定的 ``account.update``（版本卡的数据源）。"""
    base = desktop_app.demo_snapshot()
    account = dict(base.get("account") or {})
    account["update"] = update
    sections = dict(base.sections)
    sections["account"] = account
    return ds.Snapshot(sections, fetched_at=base.fetched_at, source="test")


def _raise(exc: BaseException):
    def cli(argv):                                            # noqa: ANN001, ARG001 - 桩
        raise exc
    return cli


# ─────────────────────────────────────────────────────────── D2：版本如实
def test_update_status_exposes_package_and_recorded_version(paths, store, clock):
    manager = UpdateManager(paths, store, source="", client_version="0.3.0", now=clock)
    status = manager.status()
    assert status["package_version"] == facts.CLIENT_VERSION
    assert status["install_record_version"] == store.install().version
    assert status["current_version"] == status["install_record_version"]   # 老键语义不变


def test_reconcile_install_record_aligns_stale_record(paths, store, clock):
    """0.3.11 的包 + 0.3.0 的安装记录（手工换包）→ 自检后记录变成 0.3.11。"""
    store.update_install(version="0.3.0")
    manager = UpdateManager(paths, store, source="", client_version=facts.CLIENT_VERSION,
                            now=clock)
    first = manager.reconcile_install_record()
    assert first["ok"] is True and first["changed"] is True
    assert first["from"] == "0.3.0" and first["to"] == facts.CLIENT_VERSION
    assert store.install().version == facts.CLIENT_VERSION

    again = manager.reconcile_install_record()                # 幂等：第二次零写
    assert again == {"ok": True, "changed": False, "version": facts.CLIENT_VERSION}


def test_reconcile_without_install_record_does_nothing(paths, clock, monkeypatch):
    """未 ``djb init`` 的机器：自检不许凭空建库，只回一个理由。"""
    manager = UpdateManager(paths, _NoInstallStore(), source="", now=clock)  # type: ignore[arg-type]
    result = manager.reconcile_install_record()
    assert result["ok"] is False and result["reason"]


class _NoInstallStore:
    def install(self):                                        # noqa: ANN201 - 桩
        from djb_node.store import StoreNotInitializedError
        raise StoreNotInitializedError("安装记录缺失：先执行 djb init")


def test_account_view_shows_package_version_and_flags_stale_record():
    """版本卡：写程序版本；安装记录不一致时提示里点明「已按实际程序显示」。"""
    snapshot = _snapshot_with_update({"channel": "stable", "edition": "windows",
                                      "package_version": "0.3.11",
                                      "install_record_version": "0.3.0",
                                      "verify_state": "up_to_date"})
    view = build_views(snapshot)["account"]
    field = next(item for item in view.fields if item.key == "update_version")
    assert field.value == "0.3.11"
    assert "0.3.0" in field.hint and "已按实际程序显示" in field.hint
    assert field.tone == "amber"


def test_account_view_keeps_plain_hint_when_record_matches():
    snapshot = _snapshot_with_update({"channel": "stable", "edition": "windows",
                                      "package_version": "0.3.11",
                                      "install_record_version": "0.3.11",
                                      "verify_state": "up_to_date"})
    view = build_views(snapshot)["account"]
    field = next(item for item in view.fields if item.key == "update_version")
    assert field.value == "0.3.11"
    assert field.tone == "muted" and "已按实际程序显示" not in field.hint


if __name__ == "__main__":                                    # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


# ─────────────────────────────────────── D5/D6（2026-11-18 第二次报障）
def test_bind_success_says_payout_is_a_separate_action():
    """客户把地址填在同一页 → 绑定成功后以为地址一起提交了（云端仍「尚未提交」）。
    绑定成功的文案必须当场把两件事分开。"""
    controller = DesktopController(
        FakeDataSource({"account": {"profile": {}, "security": {}, "note": ""}}, source="test"),
        cli=lambda argv: {"ok": True}, ask_code=lambda: "code-1")
    result = controller.action("account.bind")
    assert result.ok is True
    assert "USDT" in result.message and "单独" in result.message


def test_account_page_actions_are_grouped_into_two_blocks():
    """D6：账户页的「绑定节点」与「提交 USDT 收款地址」必须是两个独立动作区。"""
    base = desktop_app.demo_snapshot()
    sections = dict(base.sections)
    access = dict(base.get("access") or {})
    access["binding"] = {"bound": False}           # 未绑定 → 绑定入口可见（真实场景）
    sections["access"] = access
    view = build_views(ds.Snapshot(sections, fetched_at=base.fetched_at,
                                   source="test"))["account"]
    from djb_node.desktop.views import GROUP_BIND, GROUP_PAYOUT, action_groups

    groups = action_groups(view.actions)
    titles = [title for title, _ in groups if title]
    assert GROUP_BIND in titles and GROUP_PAYOUT in titles
    bind_keys = [a.key for title, members in groups if title == GROUP_BIND for a in members]
    payout_keys = [a.key for title, members in groups if title == GROUP_PAYOUT for a in members]
    assert "account.bind" in bind_keys
    # 2026-11-24 R2.1 文案/交互变更：收款地址行加了「复制地址」（② 统一复制组件）
    assert payout_keys == ["account.copy_address", "account.submit_payout"]
    assert not (set(bind_keys) & set(payout_keys)), "两块不许共用动作"


def test_action_groups_keeps_legacy_single_bar_when_ungrouped():
    """没写 group 的页面仍是老行为（一条按钮栏，不插标题）。"""
    from djb_node.desktop.views import Action, action_groups

    groups = action_groups([Action("a", "A"), Action("b", "B")])
    assert groups == [("", [groups[0][1][0], groups[0][1][1]])]


def test_payout_refresh_is_deferred_when_refresh_busy():
    """D5：POST 成功后的刷新被「已有刷新在跑」丢弃 → 卡片停在「尚未提交」（真机两次
    重复 POST）。现在必须记下来，等这一轮结束再补。"""
    app = _bare_app()
    app._refresh_deferred = None                                  # noqa: SLF001

    class _Busy:
        busy = True
        last_duration_s = 0.0
        started = []

        def start(self, sections=None):                           # noqa: ANN001 - 桩
            self.started.append(sections)
            return True

    busy = _Busy()

    class _C:
        message = ""

        def submit_payout(self, address):                         # noqa: ANN001 - 桩
            from djb_node.desktop.controller import ActionResult
            return ActionResult("已提交收款地址（待审核）", ok=True)

        def apply_refresh(self, *args, **kwargs):                 # noqa: ANN001 - 桩
            return build_views(desktop_app.demo_snapshot())["account"]

    app._controller = _C()                                        # noqa: SLF001
    app._refresh_worker = busy                                    # noqa: SLF001
    app._ensure_refresh_worker = lambda: busy                     # noqa: SLF001
    app._render = lambda view: None                               # noqa: SLF001
    app._apply_payout(None, None)                                 # 提交失败的兜底路径也会补刷

    assert app._refresh_deferred == ("access",), "忙时被丢弃的刷新必须记下来"  # noqa: SLF001

    busy.busy = False
    app._consume_deferred_refresh()                                # noqa: SLF001
    assert busy.started == [("access",)]
    assert app._refresh_deferred is None                           # noqa: SLF001


# ─────────────────────────────────────── D7（2026-11-18 第三次报障）
def test_submit_message_surfaces_cooldown_window():
    """「更换 USDT 提交要提示冷却时间」：成功回执里带上可再改时刻。"""
    from djb_node.desktop import payout as payout_mod

    future = "2099-01-01T00:00:00Z"
    text = payout_mod.submit_message({"address": "0x" + "a" * 40, "status": "pending",
                                      "cooldown_until": future})
    assert "待审核" in text
    assert "可出款时间" in text and "冷却中" in text, text


def test_cooldown_failure_names_the_deadline_from_last_snapshot():
    """冷却挡住**更换**时，云端 409 只回「24h 冷却未结束」；本机上一轮 GET 已知
    cooldown_until —— 失败文案必须把它写出来（否则客户只能猜「什么时候能改」）。"""
    snapshot = ds.Snapshot({"access": {"payout": {"ok": True, "data": {
        "address": "0x" + "a" * 40, "status": "pending",
        "changed_at": "2099-01-01T00:00:00Z"}}}}, source="test")

    class _Source(ds.FakeDataSource):
        def submit_payout_address(self, address, label=""):   # noqa: ANN001 - 桩
            raise ProviderError("地址变更 24h 冷却未结束（409）",
                                hint="等冷却结束再改")

    controller = DesktopController(_Source({"account": {}}, source="test"),
                                   cli=lambda argv: {"ok": True},
                                   ask_code=lambda: "x")
    controller._snapshot = snapshot                              # noqa: SLF001 - 最小装配
    result = controller.submit_payout("0x" + "a" * 40)
    assert result.ok is False
    assert "可改地址时间" in result.message, result.message


def test_payout_result_is_shown_in_a_dialog():
    """收款地址提交结果要给模态提示（客户不会盯着状态条）。"""
    app = _bare_app()
    shown: list[tuple[str, bool]] = []
    app._show_payout_dialog = lambda text, ok: shown.append((text, ok))  # noqa: SLF001
    app._refresh_worker = None                                   # noqa: SLF001
    app._request_refresh_async = lambda sections=None: None       # noqa: SLF001
    app._controller = type("C", (), {
        "submit_payout": lambda self, address: None,
        "message": "",
    })()                                                         # noqa: SLF001

    from djb_node.desktop.controller import ActionResult
    app._apply_payout(ActionResult("已提交到云端：待审核。", ok=True), None)   # noqa: SLF001
    assert shown and shown[0][1] is True and "待审核" in shown[0][0]

    app._apply_payout(None, RuntimeError("boom"))                 # noqa: SLF001
    assert shown[-1][1] is False and "地址未变更" in shown[-1][0]


# ───────────────────────── D8（2026-11-18：「客户端经常卡顿，是多个刷新卡的吗」）
def test_unchanged_view_is_not_rebuilt():
    """D8：数据没变就不许重建控件。

    ``_render`` 会 ``_clear()`` 销毁本页全部控件（含 Treeview）再重建，并把整棵树推给
    读屏——每 10 秒空转一次就是客户嘴里的「经常卡顿」。同一版视图再来时应直接跳过。
    """
    app = _bare_app()
    app._field_errors = {}                                       # noqa: SLF001
    app._body = object()                                         # noqa: SLF001
    app._rendered_narrow = False                                 # noqa: SLF001
    app._rendered_two_col = True                                 # noqa: SLF001
    app._is_narrow = lambda: False                               # noqa: SLF001
    app._two_col = lambda: True                                  # noqa: SLF001
    view = build_views(desktop_app.demo_snapshot())["overview"]

    assert app._should_skip_render(view) is False, "还没画过 → 必须画"      # noqa: SLF001
    app._last_view = view                                        # noqa: SLF001
    assert app._should_skip_render(view) is True                 # noqa: SLF001

    changed = build_views(desktop_app.demo_snapshot())["account"]
    assert app._should_skip_render(changed) is False             # noqa: SLF001
    app._rendered_narrow = True                                  # 宽度档位变了 → 必须重排
    assert app._should_skip_render(view) is False                # noqa: SLF001


def test_refresh_engine_has_a_single_timer_and_no_pile_up():
    """「多个刷新卡的么」：自动刷新只有**一个** ``after`` 定时器，且上一轮没回来不叠请求。"""
    import inspect

    source = inspect.getsource(desktop_app.DesktopApp._schedule_auto_refresh)
    assert "after(" in source and "_cancel_auto_refresh()" in source, \
        "排下一次前必须取消旧的（单定时器）"
    tick = inspect.getsource(desktop_app.DesktopApp._auto_refresh_tick)
    assert "busy" in tick and "self._schedule_auto_refresh()" in tick, \
        "忙时不发新请求，但无论如何要排下一次（不堆叠、不断链）"


# ─────────────── D9（2026-11-18 14:15：失败的 UI 要提醒 + 成功也要说清 + 自动审批）
def test_bind_again_on_same_account_says_it_was_already_bound():
    """「已经绑定该账户」不是失败：要说「本来就绑着、没有新变化」。"""
    snapshot = ds.Snapshot({"access": {"binding": {"bound": True, "state": "bound",
                                                  "bound_at": "2026-11-11T05:03:56Z"}}},
                           source="test")
    controller = DesktopController(ds.FakeDataSource({"account": {}}, source="test"),
                                   cli=lambda argv: {"ok": True},
                                   ask_code=lambda: "code-1")
    controller._snapshot = snapshot                              # noqa: SLF001
    result = controller.action("account.bind")
    assert result.ok is True
    assert "本来就绑在" in result.message and "2026-11-11T05:03:56Z" in result.message


def test_bind_failure_on_other_account_points_at_unbind_not_at_new_code():
    """409「已绑其它账户」要给解绑路径，不能只让客户「重新生成绑定码」（那会白试）。"""
    from djb_node.desktop.controller import _bind_failure_hint

    hint = _bind_failure_hint("节点已绑定其它账户，请先解绑（INV-08）")
    assert "解绑" in hint and "重新生成" not in hint
    assert "没有等待期" in hint


def test_bind_failure_cooldown_is_forward_compatible_with_server_time():
    """云端将来加「换账户冷却」时，老客户端按 reason 前缀也能显示可再绑时间。"""
    from djb_node.desktop.controller import _bind_failure_hint

    hint = _bind_failure_hint("rebind_cooldown", available_at="2026-11-20T10:00:00Z")
    assert "冷却" in hint and "2026-11-20T10:00:00Z" in hint


def test_auto_approved_payout_is_written_as_such():
    """云端自动审批通过 → 客户端说「系统自动校验通过」，而不是和人工审批同一句话。"""
    from djb_node.desktop import payout as payout_mod

    text = payout_mod.submit_message({"address": "0x" + "a" * 40, "status": "whitelisted",
                                      "auto_approved": True})
    assert "已生效" in text and "自动" in text
    manual = payout_mod.submit_message({"address": "0x" + "a" * 40, "status": "whitelisted"})
    assert "自动" not in manual


# ───────── D11（2026-11-18 14:31 owner 抓到口径混用）：两个时间点不许混为一谈
def test_cooldown_until_is_payout_eligibility_not_change_window():
    """`cooldown_until` = 新地址 72h 准入冷却（**可出款时间**）；
    「可不可以再改地址」= `changed_at + 24h`。混用会让客户以为 3 天内不能改。"""
    from djb_node.desktop import payout as po

    row = {"address": "0x" + "a" * 40, "status": "whitelisted",
           "cooldown_until": "2026-11-21T03:00:33Z",          # UTC = 北京 11:00:33
           "changed_at": "2026-11-18T03:00:33Z"}
    now = po._moment("2026-11-18T06:31:00Z")

    notice = po.cooldown_notice(row, now=now)
    assert "可出款时间" in notice and "可再改" not in notice

    change = po.change_notice(row, now=now)
    assert "可改地址时间" in change
    # changed_at + 24h 而不是 +72h：2026-11-19（不是 11-21）
    assert "2026-11-19" in change and "2026-11-21" not in change


def test_change_notice_says_already_changeable_after_24h():
    from djb_node.desktop import payout as po

    row = {"changed_at": "2026-11-15T00:00:00Z"}
    assert "已可修改" in po.change_notice(row, now=po._moment("2026-11-18T00:00:00Z"))
    assert po.change_notice({}) == "", "缺 changed_at 时不许编造时间"


# ───────── D12（2026-11-18 边界一）：EIP-55 拒绝要有精确文案，不能落回泛泛的「格式不对」
def test_checksum_rejection_has_its_own_actionable_copy():
    from djb_node.desktop.nodecloud import _payout_error

    class _Resp:
        def error_code(self):
            return 40001

        def json(self):
            return {"error": {"code": 40001, "reason": "address_checksum_invalid",
                              "message": "地址校验和不对（EIP-55）"}}

    exc = _payout_error(_Resp(), 400)
    assert "校验和" in exc.reason and "重新完整复制" in exc.hint
