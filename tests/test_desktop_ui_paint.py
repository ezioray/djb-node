# SPDX-License-Identifier: Apache-2.0
"""Canvas 绘制的两条硬不变量（B6 与重绘风暴）。

**B6：「按钮缺底边」** —— Tk ``create_polygon(..., width=1)`` 的描边是**以路径为中心**向两侧
各铺半个线宽。路径贴在画布边缘（``0.5``）时，外侧那半像素被画布裁掉，剩下的半像素在浅色底上
几乎等于背景 ⇒ 浅色 ghost 按钮的底边/右边「看不见」。本文件把「描边必须完整落在画布内」写成
断言：``bbox`` 含描边外沿，四边都至少留 1px，就说明 1px 线整条可见。

**重绘风暴** —— ``<Configure>`` 处理器在尺寸没变时必须直接返回。P0 事故里 Bars 的宽度在
567↔568 之间抖动，每次抖动都触发 ``_redraw`` → ``reqwidth`` 变 → 再抖动；去抖后 1px 抖动
不再放大（根因在 app.py 的 grid 权重，另一条线在修，这里是防御）。

窗口判据、建根重试、读 px 都在 ``tests/tk_support.py``。
"""
from __future__ import annotations

import pytest

from djb_node.desktop import ui_kit
from tests.tk_support import needs_tk, new_root, path_bounds


@needs_tk
def test_button_border_is_fully_inside_the_canvas():
    """B6 回归：ghost 按钮的 1px 描边四边都要落在画布内（旧实现贴边 ⇒ 底边被裁掉）。"""
    root = new_root()
    try:
        button = ui_kit.Button(root, "去接入配置", kind="ghost")
        button.pack()
        root.update_idletasks()
        root.update()
        width = int(button.cget("width"))
        height = int(button.cget("height"))
        x0, y0, x1, y1 = path_bounds(button, button.find_all()[0])
        # 路径离画布边 ≥ 1.5 ⇒ 1px 描边（各铺 0.5）整条落在画布内，底边/右边不再是半像素
        assert x0 >= 1.5 and y0 >= 1.5, "描边贴左/上边：%r" % ((x0, y0, x1, y1),)
        assert x1 <= width - 1.5 and y1 <= height - 1.5, \
            "描边贴右/下边：%r（画布 %dx%d）" % ((x0, y0, x1, y1), width, height)
        # 底座不许缩太多（别用"缩到看不见边"来糊弄断言）
        assert x1 >= width - 3 and y1 >= height - 3, \
            "底形缩得过头：%r（画布 %dx%d）" % ((x0, y0, x1, y1), width, height)
    finally:
        root.destroy()


@needs_tk
def test_chip_border_is_fully_inside_the_canvas():
    """同一类病：ghost chip 的描边也不许贴边（否则小胶囊看着像缺一笔）。"""
    root = new_root()
    try:
        chip = ui_kit.Chip(root, "已受理", tone="muted")
        chip.pack()
        root.update_idletasks()
        root.update()
        width = int(chip.cget("width"))
        height = int(chip.cget("height"))
        x0, y0, x1, y1 = path_bounds(chip, chip.find_all()[0])
        assert x0 >= 1.5 and y0 >= 1.5 and x1 <= width - 1.5 and y1 <= height - 1.5, \
            "chip 描边贴边：%r（画布 %dx%d）" % ((x0, y0, x1, y1), width, height)
    finally:
        root.destroy()


@needs_tk
def test_rounded_card_border_is_inset_by_one_pixel():
    """RoundedCard 的描边同样要内缩（``inset = border/2 + 1``）。"""
    root = new_root()
    try:
        card = ui_kit.RoundedCard(root)
        card.pack(fill="both", expand=True)
        card.configure(width=240, height=120)
        root.update_idletasks()
        root.update()
        assert card._shape is not None, "卡片没画出来"
        width, height = card.winfo_width(), card.winfo_height()
        x0, y0, x1, y1 = path_bounds(card._back, card._shape)
        assert x0 >= 1.5 and y0 >= 1.5, "卡片描边贴上/左边：%r" % ((x0, y0, x1, y1),)
        assert x1 <= width - 1.5 and y1 <= height - 1.5, \
            "卡片描边贴下/右边：%r（%dx%d）" % ((x0, y0, x1, y1), width, height)
    finally:
        root.destroy()


@needs_tk
def test_focus_ring_stays_inside_the_border():
    """focus ring 必须整体落在描边以内，不许压到描边或越界。"""
    root = new_root()
    try:
        button = ui_kit.Button(root, "重试", kind="ghost")
        button.pack()
        button.focus_set()
        root.update_idletasks()
        root.update()
        items = button.find_all()
        assert len(items) >= 2, "聚焦态应有两个图元（底形 + ring）：%r" % (items,)
        ring = path_bounds(button, items[1])
        shape = path_bounds(button, items[0])
        assert ring[0] > shape[0] and ring[1] > shape[1], "ring 压到描边：%r vs %r" % (ring, shape)
        assert ring[2] < shape[2] and ring[3] < shape[3], "ring 越出底形：%r vs %r" % (ring, shape)
    finally:
        root.destroy()


@needs_tk
@pytest.mark.parametrize("factory", [
    lambda root: ui_kit.Bars(root, height=84),
    lambda root: ui_kit.NavItem(root, text="总览"),
    lambda root: ui_kit.RoundedCard(root),
])
def test_configure_handler_ignores_an_identical_size(factory):
    """尺寸没变的 ``<Configure>`` 不许触发重画（防 1px 抖动放大成风暴）。"""
    root = new_root()
    try:
        widget = factory(root)
        widget.pack(fill="x", expand=True)
        root.update_idletasks()
        root.update()
        calls = []
        widget._redraw = lambda *a, **k: calls.append(1)     # 只数次数，不真画
        widget._on_configure()
        widget._on_configure()
        assert calls == [], "同尺寸还重画了 %d 次" % len(calls)
        # 反向对照：尺寸真变了必须重画（别把去抖写成「永远不画」）
        widget.configure(height=max(20, widget.winfo_height() + 40))
        root.update_idletasks()
        root.update()
        assert calls, "尺寸变了却没重画（去抖去过头）"
    finally:
        root.destroy()
