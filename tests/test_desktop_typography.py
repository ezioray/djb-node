# SPDX-License-Identifier: Apache-2.0
"""排版验收（R2）：字阶 7 档 / 中文不进 mono / 平台字族回退 / ttk 字体真实生效。

这一组用例是 ``reports/UI-20261107-UIUX-R2/01-FONT-DECISION.md`` 的可执行版本。
每条断言都对应裁决里的一条硬要求，**失败即表示排版退化**：

* §2.1 字阶只剩 7 档（+ 表格 11 的特例），旧名字保留成别名，值必须收敛；
* §2.2 ``Fonts.for_text``：ASCII → mono，含非 ASCII → sans（中文最小 10px）；
  平台字族链显式；「链上一个都没装」时仍要选到含 CJK 的族（可证伪的探测）；
* §2.4 ttk 统一：``TEntry/TCombobox/TButton/Toolbutton/Treeview/Heading/Scrollbar``
  都有显式字体，且 **GC 之后仍然有效**（曾经的 F2：临时 ``Fonts`` 被回收 ⇒ 样式里的
  字体名悬空 ⇒ Tk 静默退回默认 12pt）；
* §1 F4：``StatCell`` 的值与单位同字号、同 baseline。

真窗口用例在没有图形桌面时 skip（``DISPLAY``/``WAYLAND_DISPLAY`` 都不在 ⇒ 本机测不了，
这是**确定事实**，不是运行时抖动）。
"""
from __future__ import annotations

import gc
from pathlib import Path

import pytest

from djb_node.desktop import ui_geom

# ────────────────────────────────────────────────────────────── 纯逻辑（无需图形环境）
def test_ui_kit_has_no_font_size_of_its_own():
    """T4：0.3.4 的 8 个真值常量 + 7 个旧名别名**已全部删除**，字号只有一个来源。

    旧名 → 档位的映射、「值等价」与两条例外（表格 11→12、mono 无对应）在
    ``tests/test_desktop_px_wiring.py``；这里只守两条底线：
    ① ``ui_kit`` 上不再有 ``PX_*``；② 它报出的字号与 ``typography`` **逐项相等**（无第二份真值）。
    """
    from djb_node.desktop import typography, ui_kit

    assert [name for name in dir(ui_kit) if name.startswith("PX_")] == []
    for name in typography.TOKEN_ORDER:
        assert ui_kit.px(name) == typography.px(name), name


def test_cjk_min_size_is_eleven():
    """§2.2：CJK 下限 11px（0.3.4 是 10px；8px 中文是 F1 的根因）。"""
    from djb_node.desktop import typography, ui_kit

    assert typography.CJK_MIN_PX == 11
    assert ui_kit.px("micro") == 11        # 最小的 sans 档正好踩在下限上


@pytest.mark.parametrize("platform,head", [
    ("win32", "Microsoft YaHei UI"), ("darwin", "PingFang SC"), ("linux", "Noto Sans CJK SC"),
])
def test_sans_chain_is_platform_explicit(platform, head):
    """§2.2：sans 链按平台显式给出，链首必须是含 CJK 的族。"""
    chain = ui_geom.sans_chain(platform)
    assert chain[0] == head
    assert ui_geom.looks_like_cjk_family(chain[0]), "链首必须是含中文的族"
    assert ui_geom.platform_key(platform) == {
        "win32": "windows", "darwin": "darwin", "linux": "linux"}[platform]


@pytest.mark.parametrize("platform,expected", [
    ("win32", ("Cascadia Mono", "Consolas")),
    ("darwin", ("Menlo", "Monaco")),
    ("linux", ("DejaVu Sans Mono", "Noto Sans Mono")),
])
def test_mono_chain_is_platform_explicit(platform, expected):
    """§2.2：mono 链按平台显式给出。"""
    assert tuple(ui_geom.mono_chain(platform)) == expected


def test_pick_family_still_honours_the_chain_order():
    """既有语义不许变：链序命中、忽略大小写、都没有就回链首（Tk 再兜一层）。"""
    assert ui_geom.pick_family(["segoe ui", "MICROSOFT YAHEI UI"], ui_geom.SANS_CHAIN) \
        == "Microsoft YaHei UI"
    assert ui_geom.pick_family(["Consolas"], ui_geom.MONO_CHAIN) == "Consolas"
    assert ui_geom.pick_family([], ui_geom.SANS_CHAIN) == ui_geom.SANS_CHAIN[0]
    assert ui_geom.pick_family(None, ui_geom.MONO_CHAIN) == ui_geom.MONO_CHAIN[0]


def test_pick_sans_family_survives_a_system_without_the_chain():
    """§2.2 的可证伪探测：链上一个都没装，也要选到含 CJK 的族。

    三台真实机器的字库情形（少装一个包就长这样）：
    * 只有 ``Noto Sans CJK JP``（繁体/日文版镜像）；
    * 只有 ``WenQuanYi Zen Hei``（老 Linux 中文包）；
    * ``DejaVu Sans`` + ``Noto Sans CJK JP`` 并存（CJK 族必须优先于纯拉丁族）。
    """
    assert ui_geom.pick_sans_family(["Noto Sans CJK JP"]) == "Noto Sans CJK JP"
    assert ui_geom.pick_sans_family(["WenQuanYi Zen Hei"]) == "WenQuanYi Zen Hei"
    assert ui_geom.pick_sans_family(["DejaVu Sans", "Noto Sans CJK JP"]) == "Noto Sans CJK JP"
    # 繁体/日文/韩文并存时优先简体（中文界面的可读性）
    assert ui_geom.pick_sans_family(
        ["Noto Sans CJK JP", "Noto Sans CJK TC", "Noto Sans CJK SC"]) == "Noto Sans CJK SC"
    # 只有等宽 CJK 族时不能把它当正文（它是等宽，正文会变丑）
    assert ui_geom.pick_sans_family(["Noto Sans Mono CJK SC", "Segoe UI"]) == "Segoe UI"
    # 真的什么都没有：回链首，交给 Tk 兜底（不抛异常）
    assert ui_geom.pick_sans_family([]) == ui_geom.sans_chain()[0]


def test_pick_mono_family_never_reaches_for_a_cjk_only_chain():
    """等宽族只承载 ASCII：链上一个都没装时按「名字像等宽」兜，不拿正文族冒充。"""
    assert ui_geom.pick_mono_family(["DejaVu Sans Mono"]) == "DejaVu Sans Mono"
    assert ui_geom.pick_mono_family(["Liberation Mono"]) == "Liberation Mono"
    assert ui_geom.pick_mono_family(["Noto Sans CJK SC"]) == ui_geom.mono_chain()[0]


def test_source_has_no_hand_written_cjk_in_mono_call():
    """静态扫描：不许再写 ``fonts.mono("中文…")`` 这种字面量（§2.2 的验收口径之一）。

    只查**字符串字面量实参**：``fonts.mono(kit.px("micro"))``（传字号）不算；
    变量实参由运行时用例与 ``ui_audit.py`` 的 ``cjk-in-mono`` 项覆盖。
    """
    import ast

    root = Path(__file__).resolve().parent.parent / "djb_node" / "desktop"
    offenders = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = getattr(func, "attr", getattr(func, "id", ""))
            if name not in ("mono", "mono_font"):
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str) \
                    and not first.value.isascii():
                offenders.append("%s:%d %s" % (path.name, node.lineno, first.value))
    assert not offenders, "中文被塞进 mono：" + "; ".join(offenders)


# ────────────────────────────────────────────────────────────── 真窗口
# 跳板（确定性 skip 判据 / 建根重试 / 读字体配置值）在 ``tests/tk_support.py``，见那里的注释。
from tests.tk_support import configured_px as _configured_px
from tests.tk_support import needs_tk
from tests.tk_support import new_root as _new_root


@needs_tk
def test_for_text_routes_ascii_to_mono_and_cjk_to_sans():
    """§2.2：``for_text`` = ASCII → mono；含非 ASCII → sans（中文最小 10px）。"""
    from djb_node.desktop import ui_kit

    root = _new_root()
    try:
        fonts = ui_kit.fonts_for(root)
        for text in ("v0.2.3", "SYNC 04:12", "12,288", "receipt-7f3a", ""):
            assert fonts.for_text(text, 10).actual("family") == fonts.mono_family, text
        for text in ("共享方", "峰值 12,288 tokens · 04:00", "运行中", "✓ 已开启", "日间"):
            font = fonts.for_text(text, 10)
            assert font.actual("family") == fonts.sans_family, text
        # 中文下限 11px：传 8px 也得抬到 11px（F1 的原点就是 8px 中文）
        assert _configured_px(root, fonts.for_text("共享方", 8)) == -11
        assert _configured_px(root, fonts.for_text("SHARED", 8)) == -8
    finally:
        root.destroy()


@needs_tk
def test_resolved_sans_family_carries_cjk_when_the_system_has_any():
    """可证伪：系统里只要装了含 CJK 的族，选出来的正文族就必须是含 CJK 的。"""
    from tkinter import font as tkfont

    from djb_node.desktop import ui_kit

    root = _new_root()
    try:
        fonts = ui_kit.fonts_for(root)
        available = list(tkfont.families(root))
        cjk = [name for name in available if ui_geom.looks_like_cjk_family(name)]
        if not cjk:                                    # pragma: no cover - 无中文字库的机器
            pytest.skip("本机没有任何含 CJK 的族，无从验证")
        assert ui_geom.looks_like_cjk_family(fonts.sans_family), \
            "系统有 %d 个 CJK 族却选了 %r" % (len(cjk), fonts.sans_family)
        assert fonts.sans_family.casefold() in {name.casefold() for name in available}
        # mono 只承载 ASCII：不许把「等宽 CJK 族」当成正文族
        assert not ui_geom.looks_like_cjk_family(fonts.mono_family) or \
            "mono" in fonts.mono_family.casefold()
    finally:
        root.destroy()


@needs_tk
def test_fallback_lands_on_a_cjk_family_when_the_chain_is_absent(monkeypatch):
    """端到端兜底：把系统字库换成一个只有日文版 CJK 的列表，正文族必须落到它身上。"""
    import tkinter.font as tkfont_module

    from djb_node.desktop import ui_kit

    root = _new_root()
    monkeypatch.setattr(tkfont_module, "families",
                        lambda *a, **k: ("DejaVu Sans Mono", "Noto Sans CJK JP"))
    try:
        fonts = ui_kit.Fonts(root)
        assert fonts.sans_family == "Noto Sans CJK JP"
        assert fonts.sans(13).actual("family") == "Noto Sans CJK JP"
    finally:
        root.destroy()


@needs_tk
def test_kit_styles_fonts_survive_garbage_collection():
    """§1 F2 回归：样式里的字体在 GC 之后必须**还在**（旧实现会变成悬空名字）。

    负向对照：随便造一个没人引用的 ``Font``，``del`` + ``gc.collect()`` 之后它的 Tcl 名字
    就该从 ``font names`` 里消失——这正是旧 ``apply_kit_styles`` 干的事。
    """
    from tkinter import font as tkfont
    from tkinter import ttk

    from djb_node.desktop import ui_kit

    root = _new_root()
    try:
        style = ui_kit.apply_kit_styles(root, ttk)
        fonts = ui_kit.fonts_for(root)
        gc.collect()
        for name, role in ui_kit.STYLE_FONT_ROLES.items():
            spec = style.lookup(name, "font")
            assert spec, "样式 %s 没设字体" % name
            assert str(spec) == str(fonts.named_name(role)), \
                "样式 %s 用的不是角色 %s 的命名字体" % (name, role)
            assert str(spec) in tuple(root.tk.call("font", "names")), \
                "样式 %s 的字体名 %r 已不存在（悬空 ⇒ Tk 会静默退回默认字体）" % (name, spec)
        # 负向对照：临时字体被回收后名字确实会消失（证明上面的断言能抓到旧 bug）
        temp = tkfont.Font(root=root, family=fonts.sans_family, size=-13)
        temp_name = str(temp)
        del temp
        gc.collect()
        assert temp_name not in tuple(root.tk.call("font", "names"))
    finally:
        root.destroy()


@needs_tk
def test_ttk_fonts_are_the_ramp_sizes():
    """§2.4 的硬数字：Treeview 11 / Heading 10 bold / Entry·Combobox·Button·Scrollbar 12。"""
    from tkinter import ttk

    from djb_node.desktop import ui_kit

    root = _new_root()
    try:
        style = ui_kit.apply_kit_styles(root, ttk)
        fonts = ui_kit.fonts_for(root)
        expect = {
            "Treeview": ("table", -11, "normal"),
            "Treeview.Heading": ("heading", -10, "bold"),
            "TEntry": ("small", -12, "normal"),
            "TCombobox": ("small", -12, "normal"),
            "TButton": ("small", -12, "normal"),
            "Toolbutton": ("small", -12, "normal"),
            "Vertical.TScrollbar": ("small", -12, "normal"),
        }
        for name, (role, px, weight) in expect.items():
            spec = style.lookup(name, "font")
            assert str(spec) == str(fonts.named_name(role)), name
            assert _configured_px(root, spec) == px, (name, _configured_px(root, spec))
            actual = root.tk.call("font", "actual", spec)
            assert weight in tuple(actual), (name, actual)
        # 行高按裁决保持 26
        assert int(style.lookup("Kit.Treeview", "rowheight")) == 26
        # 表头必须是 sans（不许再给 heading 单独 mono）
        assert style.lookup("Kit.Treeview.Heading", "font") == fonts.named_name("heading")
        assert fonts.named["heading"].actual("family") == fonts.sans_family
    finally:
        root.destroy()


@needs_tk
def test_default_font_option_points_at_the_named_body_font():
    """§2.4：``option_add("*Font")`` 指向 ``DJB.Body``（13px），不再是 11pt 点值。"""
    import tkinter as tk
    from tkinter import ttk

    from djb_node.desktop import ui_kit

    root = _new_root()
    try:
        ui_kit.apply_kit_styles(root, ttk)
        fonts = ui_kit.fonts_for(root)
        label = tk.Label(root, text="正文")
        assert str(label.cget("font")) == "DJB.Body", label.cget("font")
        assert _configured_px(root, "DJB.Body") == -13
        assert fonts.named["body"].actual("family") == fonts.sans_family
    finally:
        root.destroy()


@needs_tk
def test_kit_fonts_win_over_the_gui_theme_point_sizes():
    """§1 F6 回归：``gui.theme.apply_theme`` 写的是 11pt 点值，必须被命名字体顶掉。

    这条也是 ``app.py::_apply_kit_skin`` 的存在理由：``apply_theme`` 每次换肤都会再写一次
    ``option_add("*Font", (family, 11))`` 和裸 ttk 样式字体，不重钉就退回「两套字体系统」。
    """
    import tkinter as tk
    from tkinter import ttk

    from djb_node.desktop import theme as dual
    from djb_node.desktop import ui_kit

    root = _new_root()
    try:
        dual.apply(root, ttk, "day")                   # 既有主题：*Font = (family, 11)
        before = tk.Label(root, text="正文")
        assert str(before.cget("font")) != "DJB.Body"
        ui_kit.apply_kit_styles(root, ttk)
        assert str(tk.Label(root, text="正文").cget("font")) == "DJB.Body"
        assert _configured_px(root, "DJB.Body") == -13
        # 换肤会再写一次点值 —— 这就是「换肤后要重新 apply_kit_styles」的原因
        dual.apply(root, ttk, "night")
        assert str(tk.Label(root, text="正文").cget("font")) != "DJB.Body"
        ui_kit.apply_kit_styles(root, ttk)
        assert str(tk.Label(root, text="正文").cget("font")) == "DJB.Body"
        assert _configured_px(root, "DJB.Body") == -13
    finally:
        root.destroy()


@needs_tk
def test_statcell_value_and_unit_share_one_baseline():
    """§1 F4：值与单位同字号、同 baseline（曾经单位小两号 + ``pady=(3,0)`` 顶着）。"""
    import tkinter as tk

    from djb_node.desktop import ui_kit

    root = _new_root()
    try:
        cell = ui_kit.StatCell(root, label="温度", value="62", unit="°C", value_px=19)
        cell.pack()
        root.update_idletasks()
        labels = [child for child in cell.winfo_children()]
        line = [child for child in labels if child.winfo_class() == "Frame"][0]
        value_label, unit_label = list(line.winfo_children())[:2]
        assert str(value_label.cget("text")) == "62"
        assert str(unit_label.cget("text")).strip() == "°C"
        assert _configured_px(root, value_label.cget("font")) == -19
        assert _configured_px(root, unit_label.cget("font")) == -19, "单位不许再小两号"
        root.update_idletasks()
        assert value_label.winfo_y() == unit_label.winfo_y() == 0, "基线上不许再加 pady"
        assert value_label.winfo_height() == unit_label.winfo_height()
    finally:
        root.destroy()


@needs_tk
def test_fonts_for_rebuilds_when_the_root_changes():
    """同一进程换根窗口（测试会这么干）时，缓存不许把已销毁窗口的字体发出来。"""
    from djb_node.desktop import ui_kit

    first = _new_root()
    try:
        cached = ui_kit.fonts_for(first)
        assert cached.root is first
    finally:
        first.destroy()
    second = _new_root()
    try:
        fresh = ui_kit.fonts_for(second)
        assert fresh.root is second
        assert fresh is not cached
    finally:
        second.destroy()


@needs_tk
def test_scale_hook_multiplies_px_and_defaults_to_one():
    """给 DPI 适配留的钩子：``scale`` 乘到 px 上，默认 1.0（本轮不接线）。"""
    from djb_node.desktop import ui_kit

    root = _new_root()
    try:
        base = ui_kit.Fonts(root)
        assert base.scale == 1.0
        assert base.px(ui_kit.px("h1")) == 18
        assert _configured_px(root, base.sans(ui_kit.px("h1"))) == -18
        scaled = ui_kit.Fonts(root, scale=1.25)
        assert scaled.px(ui_kit.px("h1")) == 22          # 18 × 1.25 = 22.5 → round() 22（0.3.4 同）
        assert _configured_px(root, scaled.sans(ui_kit.px("h1"))) == -22
        # 非法注入不静默缩成 0
        assert ui_kit.Fonts(root, scale=0).scale == 1.0
        assert ui_kit.Fonts(root, scale=-2).scale == 1.0
    finally:
        root.destroy()
