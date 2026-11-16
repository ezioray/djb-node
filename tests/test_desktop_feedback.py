# SPDX-License-Identifier: Apache-2.0
"""RC14 P0 G1：反馈总闸（白名单 → 黑名单）。

客户报的「点按钮整体没反馈」**不是 8 个分散 bug**，而是一处白名单式分派：
``app.py`` 只在 ``result.effect`` ∈ 六个已知值时才播报，于是 ``effect=""`` 的失败
（模型意向上报 / 跑分失败 / 空节点 ID / 收据未选中 / 控制通道未接通 / 登出无节点 /
日志无行 / 日志路径未知）全部零反馈。

本文件锁住三条口径（都不需要显示器，直接测真方法）：

1. **失败必报**：``views.py`` 里每一个 ``Action`` 的 key，只要动作返回 ``ok=False``，
   就必须播报一次（哪怕 ``effect=""``）；
2. **取消不报**：``effect="cancelled"``（用户自己取消）静默；
3. **刷新不重复**：``effect="refresh"`` 且成功时由 ``_refresh_started`` 播报，
   总闸不再补一刀；失败照报。
"""
from __future__ import annotations

from typing import Any

import pytest

from djb_node.desktop import app as desktop_app
from djb_node.desktop.controller import ActionResult
from djb_node.desktop.datasource import FakeDataSource
from djb_node.desktop.views import build_views


class _Status:
    """假状态栏（``_toast`` 就是 ``self._status.configure(text=…)``）。"""

    def __init__(self) -> None:
        self.text = ""

    def configure(self, **kwargs: Any) -> None:
        self.text = str(kwargs.get("text") or "")


def _app_for(result: ActionResult) -> tuple[desktop_app.DesktopApp, _Status]:
    app = desktop_app.DesktopApp.__new__(desktop_app.DesktopApp)

    class _Controller:
        def set_draft(self, key, value):        # noqa: ANN001 - 桩
            return None

        def action(self, key, confirm=None):    # noqa: ANN001 - 桩
            return result

        def current(self):                      # refresh/access/bench/reload 会重绘当前页
            return build_views(desktop_app.demo_snapshot())["overview"]

    app._controller = _Controller()             # noqa: SLF001 - 装配最小属性
    app._vars = {}                              # noqa: SLF001
    app._status = _Status()                     # noqa: SLF001
    app._rendered = 0                           # noqa: SLF001

    def _render(_view):                         # noqa: ANN001 - 桩
        app._rendered += 1                      # noqa: SLF001

    app._render = _render                       # noqa: SLF001
    app._update_status = lambda: None           # noqa: SLF001
    return app, app._status                      # noqa: SLF001


def _action_keys() -> list[str]:
    """``views.py`` 里全部 Action 的 key（13 个视图逐个构建，不手抄清单）。"""
    snapshot = desktop_app.demo_snapshot()
    keys = [action.key for view in build_views(snapshot).values() for action in view.actions]
    assert len(keys) >= 20, "视图里的动作数量异常少：" + repr(keys)
    return keys


def test_every_failed_action_says_something():
    """G1-0 验收：遍历全部 Action key，``ok=False`` 必须播报一次（含 ``effect=""``）。

    这是「点了没反应」的**总闸**：它不关心是哪一条动作失败，只关心「失败一定有话」。
    """
    for key in _action_keys():
        app, status = _app_for(ActionResult("失败原因：" + key, ok=False))
        app._on_action(key)                      # noqa: SLF001
        assert status.text, "动作 " + key + " 失败后状态栏没有任何提示"
        assert status.text.startswith("!"), "失败不许打勾：" + status.text


def test_successful_action_with_message_is_reported():
    """成功也要说一句（例如「已刷新 GPU 信息」）；旧白名单漏掉的 effect 同样要报。"""
    app, status = _app_for(ActionResult("已刷新 GPU 信息。", effect=""))
    app._on_action("gpu.refresh")                # noqa: SLF001
    assert status.text.startswith("✓")
    assert "GPU" in status.text


def test_cancelled_action_stays_quiet():
    """用户自己取消（登出/绑定/导出里按 Esc）不算失败，不弹「已取消」。"""
    app, status = _app_for(ActionResult("已取消登出（本机凭据未改动）。", ok=False,
                                        effect="cancelled"))
    app._on_action("account.logout")             # noqa: SLF001
    assert status.text == ""


def test_refresh_success_is_not_reported_twice():
    """``_refresh_started`` 已经播过「正在刷新…」；总闸不再补一条盖掉忙态提示。"""
    app, status = _app_for(ActionResult("正在刷新…", effect="refresh"))
    app._on_action("gpu.refresh")                # noqa: SLF001
    assert status.text == ""


def test_refresh_failure_is_reported():
    """但刷新**失败**必须报（这是旧白名单最后一个坑：失败静默）。"""
    app, status = _app_for(ActionResult("刷新失败：界面保留上一次结果。", ok=False,
                                        effect="refresh"))
    app._on_action("gpu.refresh")                # noqa: SLF001
    assert status.text.startswith("!") and "刷新失败" in status.text


def test_self_reporting_effects_do_not_double_toast():
    """``open_url`` / ``open_dir`` / ``payout_submit`` / ``update`` 自己播报，总闸不重复播。"""
    for effect in desktop_app.SELF_REPORTING_EFFECTS:
        app, status = _app_for(ActionResult("网页端：https://tkbank.ai", effect=effect))
        app._open_url = lambda _url: None        # noqa: SLF001 - 不真的开浏览器
        app._open_dir = lambda _path: None       # noqa: SLF001
        app._start_payout_submit = lambda *_a, **_k: None   # noqa: SLF001
        app._apply_update = lambda *_a, **_k: None          # noqa: SLF001
        app._on_action("account.open_web" if effect == "open_url" else "logs.open_dir")
        assert status.text == "", effect + " 被播报了两次（会盖掉「打不开」的差别）"


# ───────────────────────────── G1-2（RC14 P0）：2FA 真入口 + 链接协议校验
def test_2fa_action_is_disabled_with_a_reason_and_has_a_real_entry():
    """禁用态 + 灰字原因 + 旁边一颗**真能点**的按钮（旧实现：可点 → 只回「暂不可用」）。"""
    from djb_node.desktop.views import build_view

    view = build_view("account", desktop_app.demo_snapshot())
    actions = {action.key: action for action in view.actions}
    assert actions["account.enable_2fa"].enabled is False
    assert "网页控制台" in actions["account.enable_2fa"].reason
    entry = actions["account.open_2fa_page"]
    assert entry.enabled is True and "2FA" in entry.label


def test_open_url_refuses_non_http_schemes(monkeypatch):
    """``DJB_WEB_CONSOLE`` 配错时不许静默（``webbrowser`` 对 ``ftp:`` / 空 scheme 可能毫无反应）。"""
    opened = []
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)

    app, status = _app_for(ActionResult("x", effect=""))
    app._open_url("ftp://misconfigured.example/console/security")   # noqa: SLF001
    assert status.text.startswith("!") and "已拒绝" in status.text
    assert opened == [], "非 http(s) 链接被交给浏览器了"

    app._open_url("")                                             # noqa: SLF001
    assert status.text.startswith("!")

    app._open_url("https://tkbank.ai/console/security")            # noqa: SLF001
    assert status.text.startswith("✓") and "已在浏览器打开" in status.text
    assert opened == ["https://tkbank.ai/console/security"]


# ────────────── RC15-039 #1：字段级错误走「字段下方红字 + 聚焦首错」，不落状态栏了事
def _labels_starting_with(widget: Any, prefix: str) -> list[str]:
    """递归收集文本以 ``prefix`` 开头的 Label 文本（真窗口里找红字用）。"""
    found: list[str] = []
    try:
        children = widget.winfo_children()
    except Exception:                                  # pragma: no cover - 已销毁
        return found
    for child in children:
        try:
            text = child.cget("text")
        except Exception:
            text = ""
        if text and str(text).startswith(prefix):
            found.append(str(text))
        found.extend(_labels_starting_with(child, prefix))
    return found


def test_real_tk_inline_error_marks_the_field_and_takes_focus():
    """点「保存并回读」失败后：红字在字段下方、键盘焦点在第一个错误字段、切页不留残留。

    这一条钉的是走查原话「错误只在底部状态栏，不定位字段、跨页面残留」：状态栏仍然说一句
    （失败必报），但**定位置**的活由字段自己干。
    """
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:                                  # pragma: no cover - 无显示
        pytest.skip("没有可用的 Tk 显示")
    from djb_node.desktop import access as ac
    from djb_node.desktop.app import DesktopApp, demo_snapshot
    from djb_node.desktop.controller import DesktopController
    from djb_node.desktop.datasource import FakeDataSource

    sections = dict(demo_snapshot().sections)
    sections["access"] = {"config": {"price_mode": "auto", "node_secret": "••••1234",
                                     "node_id": "nd_old"},
                          "valid": True, "saved": True, "binding": None,
                          "node_id": "nd_old", "node_id_source": "config",
                          "note": "", "path": "~/.djb/desktop/access-config.json"}
    store = ac.MemoryAccessStore(ac.AccessConfig(node_id="nd_old", price_mode="auto"))
    controller = DesktopController(FakeDataSource(sections, source="demo"),
                                   access=ac.AccessService(store, reloader=lambda: ac.RELOAD_OK))
    app = DesktopApp(controller, root=root)
    try:
        app.show_view("access")
        root.update()
        app._advanced_toggle.command()                 # 节点凭证在折叠区里（要改就得先展开）
        root.update()
        app._vars["price_mode"].set("跟随平台建议价")
        app._vars["node_id"].set("n" * 200)
        app._vars["node_secret"].set("abc\ndef")
        app._on_action("access.save")
        root.update()

        assert set(app._field_errors) == {"price_mode", "node_id", "node_secret"}
        assert app._status.cget("text").startswith("!")      # 状态栏仍然报一次（失败必报）
        view = app._last_view
        assert view is not None and view.focus_field == "node_id"
        assert root.focus_get() is app._entries["node_id"]   # 焦点落在**首个**错误字段
        inline = _labels_starting_with(app._body, "！")
        assert inline, "字段下方没有任何红字（错误还只活在状态栏里）"
        assert sum(1 for text in inline if "节点标识" in text) == 1
        # 折叠区里的错误也必须看得见：有错就自动展开
        assert app._advanced_panel.winfo_ismapped() == 1

        app.show_view("logs")                          # 切页：红字与错误态一起清掉
        root.update()
        assert app._field_errors == {}
        assert "！" not in app._last_view.plain_text()
    finally:
        app.stop()
        try:
            root.destroy()
        except Exception:                          # pragma: no cover
            pass


# ─────────────── RC15-039 #5：日志级别筛选是**真控件**（旧版只有页脚一句承诺）
def test_real_tk_log_level_dropdown_filters_the_table():
    """下拉选「error（错误）」→ 表格真的只剩 error 行，状态栏说清「文件未改动」。"""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:                                  # pragma: no cover - 无显示
        pytest.skip("没有可用的 Tk 显示")
    from djb_node.desktop.app import DesktopApp
    from djb_node.desktop.controller import DesktopController
    from djb_node.desktop.datasource import FakeDataSource

    rows = [{"ts": "2026-11-16T10:00:01Z", "level": "info", "event": "daemon.started",
             "fields": {}},
            {"ts": "2026-11-16T10:00:05Z", "level": "error", "event": "upstream.probe_failed",
             "fields": {"reason": "上游返回 401", "next_step": "在接入配置里更新密钥"}}]
    sections = {"status": {"state": "online", "accepting": True, "daemon_running": True},
                "logs": {"rows": rows, "levels": ["debug", "info", "warning", "error"],
                         "total": 2, "note": "", "path": "/tmp/djb-test/logs/desktop.jsonl"}}
    app = DesktopApp(DesktopController(FakeDataSource(sections, source="demo")), root=root)
    try:
        app.show_view("logs")
        root.update()
        assert len(app._tree.get_children()) == 2
        assert app._filter_widgets, "日志页没有任何筛选控件（页脚又变成空承诺了）"
        box = app._filter_widgets[0]
        box.current(list(box.cget("values")).index("error（错误）"))
        box.event_generate("<<ComboboxSelected>>")
        root.update()
        assert len(app._tree.get_children()) == 1
        values = app._tree.item(app._tree.get_children()[0], "values")
        assert "error" in str(values[0]) and "上游返回 401" in str(values[3])
        assert "未改动" in app._status.cget("text")
        # 切页再回来：筛选状态还在（不能悄悄退回「全部」）
        app.show_view("overview")
        root.update()
        app.show_view("logs")
        root.update()
        assert app._last_view.filters[0].current_text() == "error（错误）"
        assert len(app._tree.get_children()) == 1
    finally:
        app.stop()
        try:
            root.destroy()
        except Exception:                          # pragma: no cover
            pass
