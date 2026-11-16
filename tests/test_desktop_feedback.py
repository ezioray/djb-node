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


# ────── RC15-039 #4：自动刷新（可见 10s / 最小化或空闲 60s / 不叠请求 / 能在设置里关）
def _auto_app(root, tmp_path, source=None):
    from djb_node.desktop.app import DesktopApp, demo_snapshot
    from djb_node.desktop.controller import DesktopController
    from djb_node.desktop.datasource import FakeDataSource

    class _Counting(FakeDataSource):
        def __init__(self, sections, **kwargs):
            super().__init__(sections, **kwargs)
            self.rounds: list[tuple[str, ...]] = []

        def snapshot(self, *, sections=None):       # noqa: ANN001 - 与基类同形
            self.rounds.append(tuple(sections or ()))
            return super().snapshot(sections=sections)

    data = source or _Counting(dict(demo_snapshot().sections), source="demo")
    controller = DesktopController(data)
    return DesktopApp(controller, root=root, prefs_home=tmp_path), controller, data


def test_real_tk_auto_refresh_interval_follows_visibility():
    """可见 10 秒、最小化 60 秒、空闲 60 秒后降到 60 秒（不做 0 秒轮询）。"""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:                                  # pragma: no cover - 无显示
        pytest.skip("没有可用的 Tk 显示")
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as home:
        app, _controller, _data = _auto_app(root, Path(home))
        try:
            root.update()
            assert app._auto_interval_s() == desktop_app.AUTO_REFRESH_VISIBLE_S == 10
            app._last_input -= desktop_app.AUTO_IDLE_S + 1      # 模拟长时间没操作
            assert app._auto_interval_s() == desktop_app.AUTO_REFRESH_HIDDEN_S == 60
            app._note_activity()                        # 一按键就回到 10 秒
            assert app._auto_interval_s() == 10
            # 最小化（iconic）= 不可见 → 60 秒
            app._root.state = lambda: "iconic"          # type: ignore[assignment]
            assert app._auto_visible() is False
            assert app._auto_interval_s() == 60
        finally:
            app.stop()


def test_real_tk_auto_refresh_refreshes_only_the_current_page_and_never_stacks():
    """一次 tick 只取本页那一域；上一次还没回来就**跳过**（不派第二个请求、不刷提示）。"""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:                                  # pragma: no cover - 无显示
        pytest.skip("没有可用的 Tk 显示")
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as home:
        app, _controller, data = _auto_app(root, Path(home))
        try:
            app.show_view("logs")
            root.update()
            before = len(data.rounds)
            app._auto_refresh_tick()
            root.update()
            assert data.rounds[before:] == [("logs",)]   # 只取 logs，不整库重拉
            assert app._auto_timer is not None           # 排了下一次
            # 忙：跳过，且状态栏不许刷「已忽略这次点击」
            # 用真 worker 造忙态（不换替身：换掉会让真 worker 的 after 轮询漏收尾）
            app._status.configure(text="")
            app._refresh_worker.start(("logs",))
            assert app._refresh_worker.busy is True
            before = len(data.rounds)
            app._auto_refresh_tick()
            assert len(data.rounds) == before              # 没有派第二个请求
            assert app._status.cget("text") == ""          # 也没刷「已忽略这次点击」
            # 收尾：让这次刷新正常跑完（**不**用 poll_once：它会绕过已排的 after 轮询，
            # 留下一个没人取消的任务，销毁窗口时 Tk 会打一行「invalid command name」）
            import time
            for _ in range(100):
                root.update()
                if not app._refresh_worker.busy:
                    break
                time.sleep(0.02)               # 轮询间隔 50ms：得让 after 真的到点
            assert app._refresh_worker.busy is False
        finally:
            app.stop()


def test_real_tk_auto_refresh_off_leaves_no_timer_and_persists():
    """设置里关掉 → 定时器清空、立刻停刷；偏好写进 desktop-prefs.json。"""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:                                  # pragma: no cover - 无显示
        pytest.skip("没有可用的 Tk 显示")
    import json
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as home:
        app, controller, data = _auto_app(root, Path(home))
        try:
            app.show_view("access")
            root.update()
            assert app._switch_widgets["auto_refresh"].on is True
            app._on_toggle(app._last_view.switches[0], False)
            root.update()
            assert controller.auto_refresh is False
            assert app._auto_timer is None                   # 一个定时器都不留
            assert app._switch_widgets["auto_refresh"].on is False
            before = len(data.rounds)
            app._auto_refresh_tick()                         # 关掉后即使被叫到也不刷
            root.update()
            assert len(data.rounds) == before
            prefs = json.loads((Path(home) / "desktop-prefs.json").read_text(encoding="utf-8"))
            assert prefs["auto_refresh"] is False
        finally:
            app.stop()


def test_real_tk_auto_refresh_reads_the_saved_preference_at_startup():
    """默认是「开」，所以这条真的在读文件（否则从关到开不会发生）。"""
    tk = pytest.importorskip("tkinter")
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as home:
        from djb_node.desktop import theme as dual
        dual.save_flag("auto_refresh", False, home)
        try:
            root = tk.Tk()
        except Exception:                              # pragma: no cover - 无显示
            pytest.skip("没有可用的 Tk 显示")
        app, controller, _data = _auto_app(root, Path(home))
        try:
            app.show_view("access")
            root.update()
            assert controller.auto_refresh is False
            assert app._auto_timer is None
            assert app._switch_widgets["auto_refresh"].on is False
        finally:
            app.stop()


def test_real_tk_auto_refresh_yields_while_the_form_has_unsaved_edits():
    """客户正在填表：自动刷新让路（重画会把没保存的输入冲掉）。"""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:                                  # pragma: no cover - 无显示
        pytest.skip("没有可用的 Tk 显示")
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as home:
        app, _controller, data = _auto_app(root, Path(home))
        try:
            app.show_view("access")
            root.update()
            assert app._has_unsaved_edits() is False
            app._vars["price_mode"].set("manual")        # 改了但没点保存
            root.update()
            assert app._has_unsaved_edits() is True
            before = len(data.rounds)
            app._auto_refresh_tick()
            root.update()
            assert len(data.rounds) == before
            app._vars["price_mode"].set("auto")          # 改回去 → 恢复自动刷新
            root.update()
            assert app._has_unsaved_edits() is False
        finally:
            app.stop()


# ────── RC15-039 #17：让路页三档能切（回车/双击 → 二次确认 → 写 → 回读）
def test_real_tk_enter_on_a_tier_row_switches_after_confirmation():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:                                  # pragma: no cover - 无显示
        pytest.skip("没有可用的 Tk 显示")
    import tempfile
    from pathlib import Path

    from djb_node.desktop.app import DesktopApp, demo_snapshot
    from djb_node.desktop.controller import DesktopController
    from djb_node.desktop.datasource import FakeDataSource

    sections = dict(demo_snapshot().sections)
    sections["yield"] = {"state": "online", "reason": None, "reason_label": None,
                         "accepting": True, "mode": "balanced", "preset": "均衡",
                         "yield_enabled": True}

    class Cli:
        def __init__(self) -> None:
            self.preset = "均衡"
            self.calls: list[tuple[str, ...]] = []

        def __call__(self, argv):
            argv = tuple(argv)
            self.calls.append(argv)
            if argv[:1] == ("limits",) and "--json" in argv:      # 只读：读本机真原档
                return {"profile": {"preset": self.preset}}
            if argv[:1] == ("limits",) and "--preset" in argv:
                self.preset = argv[argv.index("--preset") + 1]
            return {"profile": {"preset": self.preset}, "state": "online"}

    with tempfile.TemporaryDirectory() as home:
        controller = DesktopController(FakeDataSource(sections, source="demo"))
        cli = Cli()
        controller.cli = cli
        app = DesktopApp(controller, root=root, prefs_home=Path(home))
        asked: list[str] = []
        app._confirm = lambda message: asked.append(message) or True   # 二次确认走假弹窗
        try:
            app.show_view("yield")
            root.update()
            assert app._tree is not None
            assert "Enter" in str(app._root.nametowidget(app._tree.winfo_parent())
                                  .winfo_children()[-1].cget("text"))
            app._tree.focus_set()
            app._tree.selection_set("saver")                # 选中「省心」这一行
            root.update()
            app._tree.event_generate("<Return>")            # 回车 = 切到它
            root.update()
            assert asked and "省心" in asked[0]             # 先问了一次
            assert ("limits", "--preset", "省心") in cli.calls
            assert ("status",) in cli.calls                 # 回读
            assert "已切到「省心」" in app._status.cget("text")
        finally:
            app.stop()


def test_real_tk_enter_on_a_non_action_table_opens_the_detail_instead():
    """收据/日志这类表行不是动作：回车只打开详情，不许误触发任何动作。"""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:                                  # pragma: no cover - 无显示
        pytest.skip("没有可用的 Tk 显示")
    import tempfile
    from pathlib import Path

    from djb_node.desktop.app import DesktopApp, demo_snapshot
    from djb_node.desktop.controller import DesktopController
    from djb_node.desktop.datasource import FakeDataSource

    with tempfile.TemporaryDirectory() as home:
        controller = DesktopController(FakeDataSource(dict(demo_snapshot().sections),
                                                     source="demo"))
        app = DesktopApp(controller, root=root, prefs_home=Path(home))
        try:
            app.show_view("receipts")
            root.update()
            first = app._tree.get_children()[0]
            root.focus_force()                            # Xvfb 没有窗口管理器：先给窗口焦点
            app._tree.selection_set(first)
            root.update()
            before = str(controller.message)
            app._tree.focus_set()                          # 键盘事件先要落在表上
            root.update()
            app._tree.event_generate("<Return>")
            root.update()
            assert controller.message == before            # 没派动作
            # 读屏靠「焦点控件的名字」：详情区拿到焦点时名字里就有这一行的值
            assert app._detail.focus_get() is app._detail
            assert "详情：" in getattr(app._detail, app.A11Y_NAME_ATTR)
        finally:
            app.stop()


# ────── RC15-039 #31/#32：无障碍最小切片（名字 / Tab 视觉顺序 / 表格键盘 / 文字导出）
def _close(app, root):
    """关窗口：``app.stop()`` 已经销毁了 root，再 destroy 一次会抛 TclError。"""
    app.stop()
    try:
        root.destroy()
    except Exception:
        pass


def _tk_root_or_skip():
    tk = pytest.importorskip("tkinter")
    try:
        return tk, tk.Tk()
    except Exception:                                  # pragma: no cover - 无显示
        pytest.skip("没有可用的 Tk 显示")


def test_real_tk_every_focusable_control_has_an_accessible_name():
    """#31①：Tab 能停的每个控件都要有名字——读屏念不出名字的控件等于不存在。"""
    import tempfile
    from pathlib import Path

    from djb_node.desktop.views import VIEW_KEYS

    tk, root = _tk_root_or_skip()
    with tempfile.TemporaryDirectory() as home:
        app, _controller, _data = _auto_app(root, Path(home))
        try:
            root.update()
            missing = []
            for key in VIEW_KEYS:
                app.show_view(key)
                root.update()
                for widget in app._tab_order():
                    if not getattr(widget, app.A11Y_NAME_ATTR, ""):
                        missing.append((key, widget.winfo_class(), str(widget)))
            assert missing == []
        finally:
            _close(app, root)


def test_real_tk_tab_order_follows_visual_order():
    """#31②：顶部按钮（左→右）→ 侧栏（上→下）→ 内容区（上→下、左→右）。"""
    import tempfile
    from pathlib import Path

    tk, root = _tk_root_or_skip()
    with tempfile.TemporaryDirectory() as home:
        app, _controller, _data = _auto_app(root, Path(home))
        try:
            app.show_view("access")
            root.update()
            order = app._tab_order()
            assert order, "Tab 链空 = 键盘用户进不了界面"

            def place(widget):
                return (widget.winfo_rooty(), widget.winfo_rootx())

            # 三个区：顶部一条 → 左侧栏（一整列）→ 内容区；区之间不许穿插
            header_end = app._px(app.HEADER_H)
            header = [w for w in order if w.winfo_rooty() <= header_end]
            nav_set = set(app._nav_buttons.values())
            nav = [w for w in order if w in nav_set]
            content = [w for w in order if w not in header and w not in nav_set]
            assert header and nav and content
            assert order == header + nav + content
            # 区**内部**按视觉顺序（上→下、左→右）
            assert sorted(header, key=place) == header
            assert sorted(nav, key=place) == nav
            assert sorted(content, key=place) == content
            # 区**之间**也是视觉顺序：顶部在最上，侧栏从更上面起（一整列），内容在它们的下方
            assert max(w.winfo_rooty() for w in header) < min(w.winfo_rooty() for w in nav)
            assert min(w.winfo_rooty() for w in nav) < min(w.winfo_rooty() for w in content)
            # 顶部第一格是最左边那个（不是最右的「刷新」）
            assert place(header[0])[1] < place(header[-1])[1]
        finally:
            _close(app, root)


def test_real_tk_tab_and_shift_tab_walk_the_same_order():
    """#31②：Tab 往后、Shift-Tab 往前，走的是同一条链（不是 Tk 的创建顺序）。"""
    import tempfile
    from pathlib import Path

    tk, root = _tk_root_or_skip()
    with tempfile.TemporaryDirectory() as home:
        app, _controller, _data = _auto_app(root, Path(home))
        try:
            app.show_view("logs")
            root.update()
            order = app._tab_order()
            # 焦点不在链上（比如刚启动）时，按 Tab 从**第一格**开始（不是跳到第二格）
            assert app._focus_next(1) == order[0]
            root.focus_force()
            order[0].focus_set()
            root.update()
            assert app._focus_next(1) == order[1]
            assert app._focus_next(-1) == order[-1]        # 回绕到最后一个
        finally:
            _close(app, root)


def test_real_tk_table_keys_move_selection_and_home_end_reach_the_edges():
    """#31③/#32：↑↓ 逐行、Home/End 到首尾——提示语这么写就必须这么动。"""
    import tempfile
    from pathlib import Path

    tk, root = _tk_root_or_skip()
    with tempfile.TemporaryDirectory() as home:
        app, _controller, _data = _auto_app(root, Path(home))
        try:
            app.show_view("receipts")
            root.update()
            rows = list(app._tree.get_children())
            assert len(rows) >= 3
            app._tree.focus_set()
            root.update()
            app._tree.selection_set(rows[0])
            root.update()
            app._tree.event_generate("<Down>", when="now")
            root.update()
            assert app._tree.selection() == (rows[1],)
            app._tree.event_generate("<End>", when="now")
            root.update()
            assert app._tree.selection() == (rows[-1],)
            app._tree.event_generate("<Up>", when="now")
            root.update()
            assert app._tree.selection() == (rows[-2],)
            app._tree.event_generate("<Home>", when="now")
            root.update()
            assert app._tree.selection() == (rows[0],)
            # 到顶再按 ↑ 停在原地（不绕回，否则「最后一条」永远按不到）
            app._tree.event_generate("<Up>", when="now")
            root.update()
            assert app._tree.selection() == (rows[0],)
        finally:
            _close(app, root)


def test_real_tk_export_text_prints_and_copies_the_current_page():
    """#31④：``--export-text`` 是读屏兜底——整页文字既能打印也能进剪贴板。"""
    import tempfile
    from pathlib import Path

    tk, root = _tk_root_or_skip()
    with tempfile.TemporaryDirectory() as home:
        app, controller, _data = _auto_app(root, Path(home))
        try:
            app.show_view("logs")
            root.update()
            code = desktop_app._export_text(app, "logs")
            assert code == desktop_app.EXIT_OK
            text = controller.show("logs").plain_text()
            assert text.strip()
            assert root.clipboard_get() == text        # 剪贴板拿到的是**整页**文字
        finally:
            _close(app, root)


def test_real_tk_switch_focus_ring_uses_the_focus_token():
    """#31②：开关聚焦时要有看得见的环（1px 原生 highlight，颜色跟主题 focus 走）。"""
    import tempfile
    from pathlib import Path

    from djb_node.desktop import theme as desktop_theme

    tk, root = _tk_root_or_skip()
    with tempfile.TemporaryDirectory() as home:
        app, _controller, _data = _auto_app(root, Path(home))
        try:
            app.show_view("access")
            root.update()
            switch = app._switch_widgets["auto_refresh"]
            assert int(str(switch.cget("highlightthickness"))) == 1
            assert str(switch.cget("highlightcolor")) == desktop_theme.color("focus")
            assert int(str(switch.cget("takefocus"))) == 1
        finally:
            _close(app, root)


def test_real_tk_auto_refresh_landing_keeps_input_typed_while_the_request_was_in_flight():
    """复核 M1：派发时没改动、落地前开始输入 —— 落地**不许**重画，输入必须保住。

    旧实现的坑：只在派发前判一次，落地无条件 ``_render``，连 ``_entry_initial`` 一起刷，
    于是"有改动"的证据被抹掉，下一轮继续冲。这里直接驱动落地路径复现。
    """
    import time

    tk, root = _tk_root_or_skip()
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as home:
        app, controller, _data = _auto_app(root, Path(home))
        try:
            app.show_view("access")
            root.update()
            initial = app._entries["price_mode"].get()
            assert app._has_unsaved_edits() is False

            # 派发：此刻没有改动 → 允许刷新
            app._start_quiet_refresh(("access",))
            # 飞在路上的这几百毫秒里，客户开始打字
            app._vars["price_mode"].set("manual")
            root.update()
            assert app._has_unsaved_edits() is True

            # 落地
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                root.update()
                if not app._refresh_worker.busy:
                    break
                time.sleep(0.02)
            root.update()

            assert app._entries["price_mode"].get() == "manual"      # 输入没被回滚
            assert app._has_unsaved_edits() is True                  # 「有改动」的证据还在
            assert "跳过自动刷新" in app._status.cget("text")         # 有话说，不是静默

            # 下一轮 tick 仍然让路（不覆盖、不叠请求）
            rounds = len(_data.rounds)
            app._auto_refresh_tick()
            root.update()
            assert len(_data.rounds) == rounds
            assert app._entries["price_mode"].get() == "manual"

            # 改回去 → 恢复自动刷新（不是永久停摆）
            app._vars["price_mode"].set(initial)
            root.update()
            assert app._has_unsaved_edits() is False
        finally:
            _close(app, root)
