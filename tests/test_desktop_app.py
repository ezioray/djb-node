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
