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


class BrokenTk:
    """模拟「有 tkinter 但没有可用显示」：Tk() 抛 TclError。"""

    class TclError(RuntimeError):
        pass

    def Tk(self):                                   # noqa: N802 - 模拟 tkinter.Tk
        raise self.TclError("couldn't connect to display")

    def Toplevel(self, *args, **kwargs):             # noqa: N802
        raise self.TclError("no display")


def test_main_returns_headless_code_without_display(monkeypatch, capsys):
    """没有可用显示（``Tk()`` 抛 TclError）：退出码必须是 3，且给可行动提示、不抛 traceback。

    修前这条用例**假定运行环境本身没有 DISPLAY**：在 Xvfb / 真桌面上跑，``main()`` 会真的
    建出窗口并进 ``mainloop`` ⇒ 整个 tk 套件挂死（ROUND2 只能靠 ``--deselect`` 绕开，
    等于这条门禁在 CI 里是瞎的）。现在把「无显示」做成**注入**：清掉
    ``DISPLAY``/``WAYLAND_DISPLAY``，再把 ``_import_tk`` 换成连不上显示的桩 ——
    于是无论有没有显示器，验的都是同一条产品判据（建窗口失败 ⇒ 3 + 提示）。
    """
    assert desktop_app.EXIT_HEADLESS == 3
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(desktop_app, "_import_tk", lambda: BrokenTk())
    assert desktop_app.main(["--demo"]) == desktop_app.EXIT_HEADLESS
    captured = capsys.readouterr()
    assert desktop_app.HEADLESS_HINT in captured.out
    assert "Traceback" not in captured.out + captured.err


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


def _label_offenders(root) -> list[str]:
    """收集「文字放不下」的 Label：reqwidth > 实际 width + 2（差 2px 是 Tk 的边框内距）。"""
    bad, stack = [], [root]
    while stack:
        widget = stack.pop()
        stack.extend(widget.winfo_children())
        try:
            if not widget.winfo_ismapped() or widget.winfo_class() not in ("Label", "TLabel"):
                continue
            text = widget.cget("text")
        except Exception:
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        if widget.winfo_reqwidth() > widget.winfo_width() + 2:
            bad.append("%r 需要 %d px，只有 %d px"
                       % (text[:28], widget.winfo_reqwidth(), widget.winfo_width()))
    return bad


@pytest.mark.parametrize("view", ["access", "agent"])
def test_form_hints_wrap_instead_of_clipping_at_min_width(tmp_path, view):
    """900×620（产品最小窗）下表单一列的说明/值文字必须换行，不能把尾巴切掉。

    真缺陷（ROUND3 UI 审计 before 截图 + audit.json）：表单行第二列只分到 ~170 px，而
    hint label 的 wraplength 是按**窗口**估的 ~620 px ⇒ grid 把它压回列宽、文字被硬切：
    '掩码显示；粘贴新值才会覆盖' 需要 238 px 只有 170 px、'TRC20（T 开头 34 位）或 EVM（0x + 40 位）'
    需要 205 px 只有 171 px、'http://127.0.0.1:1717/agent' 需要 193 px 只有 178 px。
    现在改成 ``_wrap_to_column``：wraplength 跟 label 实际分到的列宽走。
    """
    app, root = _window_app(tmp_path, 900)
    try:
        root.geometry("900x620")
        root.update()
        app.show_view(view)
        root.update_idletasks()
        root.update()
        offenders = _label_offenders(root)
        assert not offenders, "文字被裁：" + "；".join(offenders)
    finally:
        root.destroy()


@pytest.mark.parametrize("ui_scale", [None, "1.25"])
def test_nav_item_height_does_not_drift_across_windows(tmp_path, monkeypatch, ui_scale):
    """同进程连开 5 个窗口：导航项高度与侧栏总高**不得**随窗口数漂移（ROUND3 真缺陷）。

    修前 `app._build_shell` 写的是 ``kit.NavItem.HEIGHT = self._px(kit.NavItem.HEIGHT)`` ——
    对**类属性**原地乘 DPI 比例，于是每新建一个根窗口导航项就再高一点：
    Xvfb(99.93 dpi, scale≈1.0413) 实测 34→35→36→37→41 px，``_nav_inner`` 的 reqheight
    595→608→621→673。后果不止是「变丑」：13 项在 900 高下会「放不下」，侧栏凭空冒出滚动条，
    于是整文件跑到该用例时滚动条合法地留着、断言失败（ROUND2 把它当成用例顺序问题）。
    100% 与 125% 两种缩放下都必须稳，所以这里用 `DJB_UI_SCALE` 注入倍率。
    """
    if ui_scale is None:
        monkeypatch.delenv("DJB_UI_SCALE", raising=False)
    else:
        monkeypatch.setenv("DJB_UI_SCALE", ui_scale)
    heights, needs, mapped, scales = [], [], [], []
    for _index in range(5):
        app, root = _window_app(tmp_path, 1440)
        try:
            first = app._nav_buttons[VIEW_KEYS[0]]
            heights.append(int(first.winfo_reqheight()))
            needs.append(int(app._nav_inner.winfo_reqheight()))
            mapped.append(bool(app._nav_scrollbar.winfo_ismapped()))
            scales.append(round(float(app._scale), 4))
            assert int(first.winfo_height()) == int(first.winfo_reqheight())
        finally:
            root.destroy()
    assert len(set(heights)) == 1, "导航项高度随窗口数漂移：" + repr(heights)
    assert len(set(needs)) == 1, "侧栏内容高随窗口数漂移：" + repr(needs)
    # 5 个窗口之间高度必须一模一样，且等于「基常量 × 倍率」（不是被反复乘过的值）
    from djb_node.desktop import ui_kit as kit
    assert len(set(scales)) == 1, "倍率本身不该随窗口数变化：" + repr(scales)
    if ui_scale is not None:
        assert scales[0] == float(ui_scale)
    expected = int(round(kit.NAV_ITEM_HEIGHT * scales[0]))
    assert heights[0] == expected, (heights, expected, scales)
    # 布局结论也必须一致：同样尺寸的窗口，滚动条该在就在、该收就收，不允许第 N 个才变
    assert len(set(mapped)) == 1, "侧栏滚动条状态随窗口数变化：" + repr(mapped)


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
