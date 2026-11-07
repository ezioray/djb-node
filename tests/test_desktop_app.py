# SPDX-License-Identifier: Apache-2.0
"""窗口入口回归（无 Tk 环境）：无头退出码 3、延迟 import tkinter、装配不缺件。

真实 Tk 窗口在 CI 里没有显示器，本文件只测「能不能启动 / 启动不了怎么退出」；
12 视图的真实渲染取证见 ``ops/uta/evidence/wp6/*.png``（Xvfb + import 截的）。
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from djb_node.desktop import app as desktop_app
from djb_node.desktop.datasource import SECTIONS
from djb_node.desktop.views import VIEW_KEYS, build_view


def test_module_import_does_not_touch_tkinter():
    """纯逻辑环境（无 tkinter）也必须能 import 整个包。"""
    code = ("import sys; import djb_node.desktop.app;"
            "print('tkinter' in sys.modules)")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_main_returns_headless_code_without_display():
    """没有 tkinter / 没有 DISPLAY：退出码必须是 3，且不打印 traceback。"""
    assert desktop_app.EXIT_HEADLESS == 3
    assert desktop_app.main(["--demo"]) == desktop_app.EXIT_HEADLESS


def test_headless_hint_is_actionable():
    assert "图形环境" in desktop_app.HEADLESS_HINT


def test_has_display_respects_env(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    if sys.platform in ("win32", "darwin"):
        assert desktop_app._has_display() is True
    else:
        assert desktop_app._has_display() is False
        monkeypatch.setenv("DISPLAY", ":99")
        assert desktop_app._has_display() is True


class BrokenTk:
    """模拟「有 tkinter 但没有可用显示」：Tk() 抛 TclError。"""

    class TclError(RuntimeError):
        pass

    def Tk(self):                                   # noqa: N802 - 模拟 tkinter.Tk
        raise self.TclError("couldn't connect to display")

    def Toplevel(self, *args, **kwargs):             # noqa: N802
        raise self.TclError("no display")


def test_window_creation_failure_becomes_headless_error():
    controller = desktop_app.make_controller(demo=True)
    with pytest.raises(desktop_app.HeadlessError):
        desktop_app.DesktopApp(controller, tk=BrokenTk())


def test_controller_assembly_is_complete():
    controller = desktop_app.make_controller(demo=True)
    assert controller.access is not None
    assert controller.control is not None
    assert controller.bus is not None
    assert set(controller.views) == set(VIEW_KEYS)
    assert controller.status_line()


def test_demo_data_source_is_deterministic():
    first, second = desktop_app.demo_snapshot(), desktop_app.demo_snapshot()
    assert first.sections.keys() == second.sections.keys()
    for key in VIEW_KEYS:
        assert (build_view(key, first).plain_text()
                == build_view(key, second).plain_text())


def test_view_argument_is_validated():
    with pytest.raises(SystemExit):
        desktop_app.main(["--view", "nope"])


def test_demo_snapshot_has_all_sections():
    """演示数据必须 12 个数据域全有值（否则截图会带「暂不可用」）。"""
    snapshot = desktop_app.demo_snapshot()
    assert set(snapshot.sections) == set(SECTIONS)
    for name in SECTIONS:
        assert snapshot.ok(name), name


def test_demo_mode_is_labelled_on_screen(tmp_path):
    """--demo 必须在界面上贴标签，截图不许被误当成真机数据。"""
    controller = desktop_app.make_controller(home=tmp_path, demo=True)
    assert controller.snapshot.source == "demo"
    assert controller.status_line().startswith("【演示数据】")

    real = desktop_app.make_controller(home=tmp_path, demo=False)
    assert real.snapshot.source != "demo"
    assert not real.status_line().startswith("【演示数据】")


# ───────────────────────────────────────────── 窄窗（E10 口径 390）布局
def _window_app(tmp_path, width: int):
    """真窗口 + 指定宽度；没有 DISPLAY 就跳过（CI 里靠 xvfb 跑）。"""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:                              # pragma: no cover - 无显示
        pytest.skip("没有可用的 Tk 显示")
    from djb_node.desktop.app import DesktopApp, _DEMO_SECTIONS
    from djb_node.desktop.controller import DesktopController
    from djb_node.desktop.datasource import FakeDataSource

    # 用真数据形状（state / rejected_code / local_check），演示数据的旧 status 形状测不到拒绝码
    sections = dict(_DEMO_SECTIONS)
    sections["receipts"] = {"origin": "cloud", "note": "", "rows": [
        {"receipt_id": "rcpt_uat_rj_0001", "created_at": "2026-11-01T03:12:00Z", "tokens": None,
         "state": "rejected", "rejected_code": 46001,
         "reject_reason": "provider_signature_mismatch", "local_check": "no_local"},
        {"receipt_id": "rcpt_uat_0001", "created_at": "2026-11-01T03:10:00Z", "tokens": 1500,
         "state": "accepted", "local_check": "no_local"},
    ]}
    controller = DesktopController(FakeDataSource(sections, source="test"))
    app = DesktopApp(controller, root=root)
    root.minsize(1, 1)
    root.geometry(str(width) + "x780")
    app.show_view("receipts")
    root.update_idletasks()
    root.update()
    return app, root


def test_narrow_window_shows_every_receipt_column_without_horizontal_scroll(tmp_path):
    """390 宽：5 列里的「本机校验」不许被挤出可视区（Treeview 没有横向滚动条）。"""
    app, root = _window_app(tmp_path, 390)
    try:
        tree = app._tree                             # noqa: SLF001 - 断言用
        shown = list(tree["displaycolumns"])
        assert "本机校验" in shown
        widths = [int(tree.column(column, "width")) for column in shown]
        assert sum(widths) <= int(tree.winfo_width())
        assert "时间" not in shown and "Tokens" not in shown   # 让位的列在详情抽屉里仍有
    finally:
        root.destroy()


def test_narrow_rows_are_compact_but_detail_keeps_full_text(tmp_path):
    """窄窗状态列只留拒绝码，详情抽屉仍是完整解释——信息不丢，只是换地方。"""
    app, root = _window_app(tmp_path, 390)
    try:
        tree = app._tree                             # noqa: SLF001 - 断言用
        first = tree.get_children()[0]
        compact = tree.item(first, "values")
        assert "46001" in compact[3] and "收据签名不匹配" not in compact[3]
        full = app._row_cells[first]                 # noqa: SLF001 - 断言用
        assert "收据签名不匹配" in full[3]
        tree.selection_set(first)
        root.update()
        texts = [w.cget("text") for frame in app._detail.winfo_children()  # noqa: SLF001
                 for w in frame.winfo_children()]
        assert any("收据签名不匹配" in text for text in texts)
    finally:
        root.destroy()


# ───────────────────────────────────────── 实例属性不许遮蔽方法（真实崩溃回归）
def test_no_instance_attribute_shadows_a_class_method():
    """``self._two_col = <bool>`` 会把同名方法遮蔽，下一次调用就是 TypeError。

    这曾经让「先算过布局、再渲染」的路径直接崩（改窗口大小 / 切第二个视图必现）。
    用 AST 静态扫一遍，比只测一条路径更能兜住同类错误。
    """
    import ast
    import inspect
    from djb_node.desktop.app import DesktopApp

    module = ast.parse(inspect.getsource(DesktopApp))
    for node in ast.walk(module):
        if isinstance(node, ast.ClassDef) and node.name == "DesktopApp":
            cls = node
            break
    else:                                            # pragma: no cover - 源码结构变了
        pytest.fail("没找到 DesktopApp 类")
    methods = {item.name for item in cls.body
               if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assigned = set()
    for node in ast.walk(cls):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for target in targets:
            if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                    and target.value.id == "self"):
                assigned.add(target.attr)
    clash = sorted(assigned & methods)
    assert not clash, "实例属性遮蔽了同类方法：" + ", ".join(clash)


def test_apply_layout_then_render_does_not_explode(tmp_path):
    """先布局（写宽度状态）再渲染，不许抛异常——真实窗口 resize / 切视图都走这条路。"""
    app, root = _window_app(tmp_path, 900)
    try:
        app._apply_layout()                           # noqa: SLF001 - 断言用
        app._render(app._controller.current())        # noqa: SLF001 - 断言用
        root.update()
        assert app._tree is not None                  # noqa: SLF001 - 渲染确实跑完了
    finally:
        root.destroy()


# ───────────────────────────────────────── IA R2：分组 / 滚动 / 快捷键 / 表头（B1–B7）
def test_content_item_width_never_exceeds_canvas():
    """B4：内容列宽恒等于画布宽（负值/0 当 0，绝不把内容撑到画布外）。"""
    assert desktop_app.content_item_width(1232) == 1232
    assert desktop_app.content_item_width("1232") == 1232
    assert desktop_app.content_item_width(0) == 1      # Tk 的 item width 最小 1
    assert desktop_app.content_item_width(-8) == 1
    assert desktop_app.content_item_width(None) == 1


def test_nav_needs_scroll_only_when_content_is_taller():
    """B2：13 项 + 4 分组头 574px —— 900 宽时不滚，620 高时滚。"""
    assert desktop_app.nav_needs_scroll(574, 671) is False
    assert desktop_app.nav_needs_scroll(574, 391) is True
    assert desktop_app.nav_needs_scroll(391, 391) is False
    assert desktop_app.nav_needs_scroll(0, 0) is False
    assert desktop_app.nav_needs_scroll(574, 1) is False     # 还没布局（h=1）不猜
    # 2px 磁滞：差 1~2px 不算溢出（否则 1px 摆动会让滚动条反复开关）
    assert desktop_app.nav_needs_scroll(573, 571) is False
    assert desktop_app.nav_needs_scroll(574, 571) is True


def test_view_shortcuts_cover_every_view_exactly_once():
    """B5：Ctrl+1…9 给前 9 项，Ctrl+Shift+1…4 给后 4 项；13 个视图一个不落。"""
    bindings = desktop_app.view_shortcut_bindings()
    assert [key for _sequence, key in bindings] == list(VIEW_KEYS)
    sequences = [sequence for sequence, _key in bindings]
    assert sequences[:9] == ["<Control-Key-%d>" % number for number in range(1, 10)]
    # Shift+数字在 Tk 里是 exclam/at/numbersign/dollar，绑数字本身不会触发
    assert sequences[9:] == ["<Control-Shift-exclam>", "<Control-Shift-at>",
                             "<Control-Shift-numbersign>", "<Control-Shift-dollar>"]
    assert len(set(sequences)) == len(VIEW_KEYS)


def test_view_shortcuts_accept_a_command_modifier():
    """macOS 走 Command：只换修饰符，键位表不动（xplat 流接这里）。"""
    bindings = desktop_app.view_shortcut_bindings(modifier="Command")
    assert bindings[0][0] == "<Command-Key-1>"
    assert bindings[9][0] == "<Command-Shift-exclam>"


def test_shortcut_hint_mentions_both_key_ranges():
    text = desktop_app.sync_shortcut_hint(
        "就绪：F5 刷新 · Ctrl+1…9 切换视图 · 关闭窗口不会停止 daemon")
    assert "Ctrl+1…9" in text and "Ctrl+Shift+1…4" in text
    assert text.endswith("关闭窗口不会停止 daemon")            # 其它段落原样保留
    assert desktop_app.sync_shortcut_hint("没有旧文案") == "没有旧文案"


def test_page_kickers_come_from_the_ia_table():
    """B3：页头眉标只有一份数据源（views.page_kicker），app 里不手抄第二份。"""
    from djb_node.desktop.views import page_kicker

    assert desktop_app.PAGE_KICKERS == {key: page_kicker(key) for key in VIEW_KEYS}
    assert desktop_app.PAGE_KICKERS["account"] == "SYSTEM / ACCOUNT"
    assert desktop_app.PAGE_KICKERS["receipts"] == "EARN / RECEIPTS"


def test_looks_numeric_knows_money_tokens_and_units():
    assert desktop_app.looks_numeric("1,234")
    assert desktop_app.looks_numeric("¥12.00")
    assert desktop_app.looks_numeric("98%")
    assert desktop_app.looks_numeric("1.5k")
    assert desktop_app.looks_numeric("512 MB")
    assert not desktop_app.looks_numeric("rcpt_uat_0001")
    assert not desktop_app.looks_numeric("2026-11-01 03:12")
    assert not desktop_app.looks_numeric("已接受")
    assert not desktop_app.looks_numeric("—")


def test_numeric_columns_flags_money_and_tokens_not_ids():
    """B7：数字列（Tokens/金额）右对齐，编号/时间/状态保持左对齐。"""
    from types import SimpleNamespace

    columns = ("收据号", "时间", "Tokens", "状态")
    rows = [
        SimpleNamespace(cells=("rcpt_uat_0001", "2026-11-01 03:10", "1,500", "已接受")),
        SimpleNamespace(cells=("rcpt_uat_0002", "2026-11-01 03:12", "¥12.00", "已拒绝")),
    ]
    assert desktop_app.numeric_columns(columns, rows) == {"Tokens"}
    assert desktop_app.numeric_columns(("余额",), [
        SimpleNamespace(cells=("—",)), SimpleNamespace(cells=("¥3.20",))
    ]) == {"余额"}
    assert desktop_app.numeric_columns(("空列",), [SimpleNamespace(cells=("",))]) == set()


# ── 真窗口：分组头 / 13 项 / 滚动条只在溢出时出现 / 内容宽不变量 / 键盘
def test_sidebar_renders_four_groups_and_thirteen_items(tmp_path):
    app, root = _window_app(tmp_path, 1440)
    try:
        from djb_node.desktop.views import NAV_SECTIONS

        assert list(app._nav_section_headers) == [section.key for section in NAV_SECTIONS]
        assert len(app._nav_section_headers) == 4
        assert list(app._nav_buttons) == list(VIEW_KEYS)
        for section in NAV_SECTIONS:
            header = app._nav_section_headers[section.key]
            first = app._nav_buttons[section.keys[0]]
            assert first.winfo_y() > header.winfo_y()
        # 13 项都在同一列里（没有横向溢出）：内层宽 = 画布宽
        assert int(app._nav_inner.winfo_width()) <= int(app._nav_canvas.winfo_width()) + 1
    finally:
        root.destroy()


def test_short_window_scrolls_the_nav_and_keeps_every_item_reachable(tmp_path):
    """B2：900×620 下导航区可滚，最后一项滚到底能看全；1440 高不出现滚动条。"""
    app, root = _window_app(tmp_path, 900)
    try:
        root.geometry("900x620")
        root.update()
        assert app._nav_scrollbar.winfo_ismapped()
        assert int(app._nav_inner.winfo_reqheight()) > int(app._nav_canvas.winfo_height())
        app._nav_canvas.yview_moveto(1.0)
        root.update()
        last = app._nav_buttons[VIEW_KEYS[-1]]
        visible_bottom = app._nav_canvas.canvasy(0) + int(app._nav_canvas.winfo_height())
        assert last.winfo_y() + last.winfo_height() <= visible_bottom + 1
        # 没有横向滚动：xview 永远在 (0, 1)
        assert app._nav_canvas.xview() == (0.0, 1.0)
        # 回到高窗：13 项放得下 → 滚动条收起
        root.geometry("1440x900")
        root.update()
        assert not app._nav_scrollbar.winfo_ismapped()
    finally:
        root.destroy()


def test_content_width_follows_canvas_across_views_and_resizes(tmp_path):
    """B4：换视图 / 改窗口宽之后，内容宽 == 画布宽（右侧不再被裁）。"""
    app, root = _window_app(tmp_path, 1440)
    try:
        assert int(app._content.winfo_width()) == int(app._canvas.winfo_width())
        app.show_view("account")
        root.update()
        assert int(app._content.winfo_width()) == int(app._canvas.winfo_width())
        root.geometry("1100x700")
        root.update()
        assert int(app._content.winfo_width()) == int(app._canvas.winfo_width())
    finally:
        root.destroy()


def test_page_kicker_label_shows_two_level_ascii_for_account(tmp_path):
    app, root = _window_app(tmp_path, 1440)
    try:
        app.show_view("account")
        root.update()
        assert app._page_kicker.cget("text") == "SYSTEM / ACCOUNT"
    finally:
        root.destroy()
