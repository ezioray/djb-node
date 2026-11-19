# SPDX-License-Identifier: Apache-2.0
"""RC14 P0 G4：桌面「检查更新」（九状态 + 不阻塞主线程 + no_artifact 给下载页）。

客户报的问题有两半：

1. **GUI 里根本没有「检查更新」**（``grep update views.py`` = 0），只有 CLI ``djb update``；
2. Windows 客户「永远到不了新版」时看到的应该是「本平台暂无安装包 → 去下载页」，
   而不是一句「暂无更新」——后者会让客户以为「我已经是最新的」。

口径（``update.STATE_GUIDANCE`` 是权威表）：

* 每个状态都有**下一步**，绝不出现「只有状态码的 toast」；
* ``no_artifact`` 必须带下载页链接（``facts.WEB_CONSOLE_URL + /download``）；
* ``failed_verify`` / ``invalid`` 要明确「不要安装 / 已拒装」；
* ``channel_mismatch`` 显示两边渠道且**不提供一键切渠道**。

本文件不需要显示器、不需要网络：假 UpdateManager 直接返回九种 payload。
"""
from __future__ import annotations

import time

import pytest

from djb_node import facts, update
from djb_node.desktop import app as desktop_app
from djb_node.desktop.controller import ActionResult, DesktopController
from djb_node.desktop.datasource import (FakeDataSource, LocalDataSource, Snapshot)
from djb_node.desktop.views import build_view
from tests.tk_support import needs_tk

ALL_STATES = ("offline", "invalid", "no_source", "failed_verify", "channel_mismatch",
              "up_to_date", "not_eligible", "no_artifact", "eligible")


def test_every_update_state_has_a_next_step():
    """九状态全覆盖：``title`` / ``next`` / ``action`` 都不许空，``ok`` 明确。"""
    assert set(update.STATE_GUIDANCE) == set(ALL_STATES), "guidance 表没覆盖全部九状态"
    for state in ALL_STATES:
        info = update.guidance({"state": state, "current_version": "0.3.6"})
        assert info["title"], state
        assert info["next"], state
        assert info["action"] in ("retry", "apply", "download", "contact", "wait", "none")
        assert isinstance(info["ok"], bool)
        # 任何状态都要说清「当前版本」，否则客户不知道自己在哪一版
        assert "0.3.6" in info["detail"]


def test_no_artifact_points_at_the_download_page(monkeypatch):
    """Windows 现状：``no_artifact`` 必须给**能点**的下载页（只说「暂无安装包」＝误导）。"""
    monkeypatch.setattr(facts, "WEB_CONSOLE_URL", "https://tkbank.ai")
    info = update.guidance({"state": "no_artifact", "current_version": "0.3.5"})
    assert info["action"] == "download"
    assert info["url"] == "https://tkbank.ai" + facts.UPDATE_DOWNLOAD_PATH
    assert "下载页" in info["next"]


def test_unknown_state_still_says_something():
    """未知/未来的状态也不许静默：退回「找客服」而不是空白。"""
    info = update.guidance({"state": "brand_new_state"})
    assert "brand_new_state" in info["title"]
    assert info["next"] and info["action"] == "contact"
    assert info["ok"] is False


def test_failed_verify_and_invalid_tell_the_customer_not_to_install():
    for state in ("failed_verify", "invalid"):
        info = update.guidance({"state": state, "message": "签名不匹配"})
        assert "不要安装" in info["title"] or "拒装" in info["title"], state
        assert info["action"] == "contact"


def test_channel_mismatch_shows_both_channels_and_no_one_click_switch():
    info = update.guidance({"state": "channel_mismatch",
                            "message": "清单频道为 beta，本机为 stable"})
    assert "beta" in info["detail"] and "stable" in info["detail"]
    assert info["action"] == "contact"          # 不提供「一键切渠道」


# ─────────────────────────────────────── controller：worker / 同步回落 / 结果翻译
class FakeManager:
    def __init__(self, result: dict, *, delay_s: float = 0.0) -> None:
        self.result = result
        self.delay_s = delay_s
        self.checks = 0
        self.applies = 0

    def check(self) -> dict:
        self.checks += 1
        if self.delay_s:
            time.sleep(self.delay_s)       # 模拟真机的网络 + 签名校验
        return dict(self.result)

    def apply(self) -> dict:
        self.applies += 1
        return {"installed": True, "version": "0.3.7"}


def _controller(result: dict) -> tuple[DesktopController, FakeManager]:
    manager = FakeManager(result)
    controller = DesktopController(FakeDataSource({"status": {"state": "online"}}), cli=None,
                                   update_factory=lambda: manager)
    return controller, manager


def test_check_goes_through_the_worker_channel():
    """点了立刻返回（≤200ms 有反馈），真正检查在 worker 里 —— 主线程不许等网络。"""
    controller, manager = _controller({"state": "up_to_date", "current_version": "0.3.6"})
    started: list[str] = []
    controller.update_via = lambda kind: bool(started.append(kind)) or True

    result = controller.action("update.check")
    assert started == ["check"], "update.check 没有走异步通道"
    assert result.effect == "update"
    assert result.payload == {"state": "checking"}
    assert "正在检查" in result.message
    assert manager.checks == 0, "动作里同步跑了网络检查"


def test_check_falls_back_to_sync_without_an_event_loop():
    """截图脚本/单测没有事件循环：同步跑一次并给出真结论（不许空转）。"""
    controller, manager = _controller({"state": "eligible", "current_version": "0.3.6",
                                       "manifest_version": "0.3.7",
                                       "artifact": {"url": "u", "size": 2 * 1048576}})
    result = controller.action("update.check")
    assert manager.checks == 1
    assert result.ok is True and result.effect == "update"
    assert result.payload["state"] == "eligible"
    assert "下一步" in result.message and "安装" in result.message
    assert controller.last_update["state"] == "eligible"


def test_check_reports_the_nine_states_as_actionable_sentences():
    """每个状态的动作结果都得是「一句话 + 下一步」，且 ``ok`` 与状态匹配。"""
    for state in ALL_STATES:
        controller, _ = _controller({"state": state, "current_version": "0.3.6",
                                    "message": "工程细节"})
        result = controller.action("update.check")
        assert result.effect == "update" and result.payload["state"] == state
        assert "下一步：" in result.message, state
        assert result.ok is (state in update.GUIDANCE_OK_STATES), state


def test_check_is_honest_when_the_manager_cannot_be_built():
    """本机未初始化（没有库）⇒ 如实说「不可用 + 原因」，不许假装「已是最新」。"""
    def boom():
        raise RuntimeError("本机未初始化：先执行 djb init")

    controller = DesktopController(FakeDataSource({"status": {}}), cli=None,
                                   update_factory=boom)
    result = controller.action("update.check")
    assert result.ok is False
    assert "RuntimeError" in result.message and "不可用" in result.message


def test_apply_requires_confirmation_and_never_runs_without_it():
    controller, manager = _controller({"state": "eligible"})
    result = controller.action("update.apply", confirm=lambda _m: False)
    assert manager.applies == 0
    assert result.effect == "cancelled" and result.ok is False
    # 确认后走 worker
    started: list[str] = []
    controller.update_via = lambda kind: bool(started.append(kind)) or True
    result = controller.action("update.apply", confirm=lambda _m: True)
    assert started == ["apply"] and result.payload == {"state": "applying"}


def test_apply_success_and_failure_copy():
    controller, _ = _controller({"state": "eligible"})
    ok = controller.run_update_apply()
    # K1（CR-20261117-AUDIT-FOLLOWUP #26）：S1 骨架只下载校验、不换二进制，
    # 文案不得再宣称「已安装 … 重启后生效」。
    assert ok.ok is True and "已下载待安装 0.3.7" in ok.message and "换装" in ok.message

    controller, manager = _controller({"state": "eligible"})
    manager.apply = lambda: {"installed": False, "state": "failed_verify",
                             "message": "签名不匹配"}
    bad = controller.run_update_apply()
    assert bad.ok is False and "未安装" in bad.message and "签名不匹配" in bad.message


def test_download_action_opens_the_download_page(monkeypatch):
    monkeypatch.setattr(facts, "WEB_CONSOLE_URL", "https://tkbank.ai/")
    controller = DesktopController(FakeDataSource({"status": {}}), cli=None)
    result = controller.action("update.download")
    assert result.effect == "open_url"
    assert result.payload == "https://tkbank.ai" + facts.UPDATE_DOWNLOAD_PATH


# ─────────────────────────────────────── views / datasource：入口与降级
def test_account_page_has_the_update_entry():
    view = build_view("account", desktop_app.demo_snapshot())
    keys = {action.key for action in view.actions}
    assert "update.check" in keys
    labels = {field.label for field in view.fields}
    assert {"当前版本", "上次检查", "上次结果"} <= labels


def test_account_page_offers_download_only_for_no_artifact():
    """只在 ``no_artifact`` 时出现「去下载页」；``eligible`` 时出现「安装更新」。"""
    base = dict(desktop_app.demo_snapshot().sections)
    for state, expected in (("no_artifact", "update.download"), ("eligible", "update.apply")):
        sections = dict(base)
        sections["account"] = dict(sections.get("account") or {})
        sections["account"]["update"] = {"current_version": "0.3.6", "channel": "stable",
                                        "verify_state": state, "checked_at": "2026-11-14T10:00",
                                        "bucket": 10, "note": ""}
        keys = {a.key for a in build_view("account", Snapshot(sections=sections)).actions}
        assert expected in keys, state
        other = "update.apply" if expected == "update.download" else "update.download"
        assert other not in keys, state


def test_update_status_degrades_locally_without_killing_the_account_page(tmp_path):
    """更新状态读不到 ⇒ 只有「更新」这一块显示不可用，账户信息照常显示。"""
    def boom():
        raise RuntimeError("库坏了")

    source = LocalDataSource(home=tmp_path, runner=lambda args: {"state": "online"},
                             cloud=None, cloud_factory=lambda home: None,
                             update_status=boom, boot={})
    account = source._account()                          # noqa: SLF001 - 直接测数据域
    assert "库坏了" in account["update"]["unavailable"]
    assert account["update"]["current_version"] is None   # 绝不写 0 / 假装已知


def test_update_status_never_touches_the_network():
    """``_account()`` 只读本地记录：注入的 getter 被调用一次，且不建 HttpClient。"""
    calls: list[int] = []

    def status():
        calls.append(1)
        return {"current_version": "0.3.6", "channel": "stable", "verify_state": "up_to_date",
                "checked_at": "2026-11-14T10:00:00+08:00", "bucket": 10, "edition": "consumer"}

    source = LocalDataSource(home="/tmp/djb-test-home", runner=lambda args: {"state": "online"},
                             cloud=None, cloud_factory=lambda home: None,
                             update_status=status, boot={})
    detail = source._update_detail()                      # noqa: SLF001
    assert calls == [1]
    assert detail["verify_state"] == "up_to_date" and detail["current_version"] == "0.3.6"


@pytest.mark.parametrize("state", ALL_STATES)
def test_update_state_text_covers_all_nine_states(state):
    """界面短标签也必须覆盖九状态（否则表格里会露出英文状态码）。"""
    from djb_node.desktop.views import UPDATE_STATE_TEXT
    assert state in UPDATE_STATE_TEXT


# ─────────────────────────────────────── 真窗口：入口接上 worker、点一下不冻窗口
@needs_tk
def test_window_wires_the_update_worker_before_the_first_click(tmp_path):
    """窗口一起来 ``update_via`` 就必须接上（否则第一次点击在主线程跑网络检查）。

    这是 G4 的**核心验收**（「点下立即返回」）：动作返回时 ``manager.checks`` 仍为 0，
    说明真正检查还在 worker 里。
    """
    manager = FakeManager({"state": "up_to_date", "current_version": "0.3.6"}, delay_s=0.4)
    controller = DesktopController(FakeDataSource(dict(desktop_app.demo_snapshot().sections),
                                                 source="local"),
                                   cli=None, update_factory=lambda: manager)
    app = desktop_app.DesktopApp(controller)
    try:
        assert controller.update_via is not None, "窗口没接上更新异步通道"
        began = time.monotonic()
        result = controller.action("update.check")
        elapsed = time.monotonic() - began
        assert result.payload == {"state": "checking"}
        # 核心验收：真机检查要 0.4 s，动作必须**立刻**返回（旧实现会等满 0.4 s 冻窗口）
        assert elapsed < 0.2, "动作被检查阻塞了 %.2f s（窗口会卡）" % elapsed
        # 让 worker 跑完并把结果回投主线程
        for _ in range(600):
            app._root.update()
            if not app._update_worker.busy:
                break
            time.sleep(0.01)
        assert manager.checks == 1
        assert controller.last_update["state"] == "up_to_date"
    finally:
        app.stop()


@needs_tk
def test_demo_mode_update_check_stays_offline_and_honest():
    """演示模式不联网、也不读客户真机目录：如实说「演示数据」。"""
    controller = desktop_app.make_controller(home=None, demo=True)
    app = desktop_app.DesktopApp(controller)
    try:
        result = controller.action("update.check")
        assert result.ok is False
        assert "演示数据" in result.message
        assert result.payload["state"] == "no_source"
    finally:
        app.stop()
