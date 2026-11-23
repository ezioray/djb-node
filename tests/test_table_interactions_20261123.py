# SPDX-License-Identifier: Apache-2.0
"""R2.4 C3：原生交互 —— 列排序 / 右键行菜单 / 双击详情。

来源：`CHG-20261123-R2-4-NODE-SHELL-PERF` 的 C3（「右键/双击/排序可用」）。

三条都只在**本机**完成：排序不动数据、不改后端；右键菜单复制走剪贴板；双击详情不发请求。
"""
from __future__ import annotations

import pytest

from djb_node.desktop.app import DesktopApp, demo_snapshot
from djb_node.desktop.controller import DesktopController
from djb_node.desktop.datasource import FakeDataSource
from tests import tk_support


def _app():
    root = tk_support.new_root()
    app = DesktopApp(DesktopController(FakeDataSource(dict(demo_snapshot().sections))), root=root)
    return app, root


def _close(app, root) -> None:
    try:
        app.stop()
    except Exception:                              # pragma: no cover
        pass
    try:
        root.destroy()
    except Exception:                              # pragma: no cover
        pass


@tk_support.needs_tk
def test_clicking_a_heading_sorts_and_second_click_reverses():
    app, root = _app()
    try:
        app.show_view("models")                    # 模型与探针页有主表
        root.update()
        tree = app._tree
        assert tree is not None and tree.get_children(""), "这一页没有可排序的行"
        column = list(tree["columns"])[0]
        before = [str(tree.item(iid, "values")[0]) for iid in tree.get_children("")]
        app._sort_by_column(tree, column)
        root.update()
        first = [str(tree.item(iid, "values")[0]) for iid in tree.get_children("")]
        assert first == sorted(first), "正序排序结果不是升序：" + repr(first[:5])
        assert column + " ▲" == tree.heading(column)["text"]     # 表头带方向
        app._sort_by_column(tree, column)
        root.update()
        second = [str(tree.item(iid, "values")[0]) for iid in tree.get_children("")]
        assert column + " ▼" == tree.heading(column)["text"]
        assert second == list(reversed(first)), "再点一次不是反序"
        assert sorted(second) == sorted(before), "排序把行丢了或加了行"
    finally:
        _close(app, root)


@tk_support.needs_tk
def test_sorting_numbers_uses_numeric_order_not_string_order():
    app, root = _app()
    try:
        app.show_view("models")
        root.update()
        tree = app._tree
        # 造三行：「10」「2」「1」—— 字符串序会排成 1,10,2，数值序才是 1,2,10
        for iid in tree.get_children(""):
            tree.delete(iid)
        for value in ("10", "2", "1"):
            tree.insert("", "end", values=(value,))
        app._sort_by_column(tree, list(tree["columns"])[0])
        order = [tree.item(iid, "values")[0] for iid in tree.get_children("")]
        assert order == ["1", "2", "10"]
    finally:
        _close(app, root)


@tk_support.needs_tk
def test_right_click_offers_copy_row_and_copy_cell(monkeypatch):
    app, root = _app()
    try:
        app.show_view("models")
        root.update()
        tree = app._tree
        rows = tree.get_children("")
        assert rows
        bbox = tree.bbox(rows[0], list(tree["columns"])[0])
        assert bbox, "这一行没画出来，取不到坐标"
        copied: list[str] = []
        monkeypatch.setattr(app, "copy_text", lambda text: copied.append(str(text)) or True)
        shown: list[object] = []

        def fake_popup(self, x, y):                # 不真弹窗（无 WM 的 CI 会抢焦点）
            shown.append(self)
            return None

        monkeypatch.setattr(app._tk.Menu, "tk_popup", fake_popup, raising=False)
        app._on_table_context_menu(_event(bbox))
        assert shown, "右键没有建出菜单"
        menu = shown[-1]
        labels = [menu.entrycget(index, "label") for index in range(menu.index("end") + 1)
                  if menu.type(index) == "command"]
        assert any(label.startswith("复制这一整行") for label in labels), labels
        assert any(label.startswith("复制「") for label in labels), labels
        assert "打开详情" in labels

        # 点「复制这一整行」→ 真的把整行（tab 分隔）放进剪贴板
        row_index = labels.index("复制这一整行")
        menu.invoke(row_index)
        assert copied and "\t" in copied[-1]
        expected = [str(value) for value in tree.item(rows[0], "values")]
        assert copied[-1].split("\t") == expected

        # 点「复制「列名」：值」→ 只复制那一格
        cell_index = next(index for index, label in enumerate(labels) if label.startswith("复制「"))
        menu.invoke(cell_index)
        assert len(copied) == 2 and copied[-1] == expected[0]
    finally:
        _close(app, root)


class _event:
    """最小的 Tk 事件替身：右键处理只用到 x/y/x_root/y_root。"""

    def __init__(self, bbox: tuple[int, int, int, int]) -> None:
        self.x = bbox[0] + 2
        self.y = bbox[1] + 2
        self.x_root = 400
        self.y_root = 300


@tk_support.needs_tk
def test_double_click_opens_the_row_detail():
    app, root = _app()
    try:
        app.show_view("models")
        root.update()
        tree = app._tree
        rows = tree.get_children("")
        assert rows
        bbox = tree.bbox(rows[0], list(tree["columns"])[0])
        assert bbox
        app._on_double_click(_event(bbox))
        root.update()
        assert tree.selection() == (rows[0],)          # 双击选中了这一行
        assert str(app._status.cget("text") or "") != "" or app._detail is not None
    finally:
        _close(app, root)
