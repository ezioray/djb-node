# SPDX-License-Identifier: Apache-2.0
"""D17（owner 2026-11-24）：13 视图按模块分五组 + 组标题可折叠 + 折叠状态持久化。

三条硬要求，一条一个测试面：

1. **分组渲染**：五组（接入/共享/收益/账户/诊断）、组序固定、13 项一个不少、组内保留原顺序；
2. **折叠持久化**：默认全展开；折叠后写进客户端偏好（``nav_collapsed``），重开窗口仍是折叠；
3. **13 个入口仍可达**：侧栏项都在（折叠只是不显示，不是删掉）、``show_view`` 都能到，
   去折叠组里的页会自动展开该组。
"""
from __future__ import annotations

import json

import pytest

from djb_node.desktop import app as app_module
from djb_node.desktop import ia, theme
from djb_node.desktop.controller import DesktopController
from djb_node.desktop.datasource import FakeDataSource
from djb_node.desktop.views import VIEW_KEYS

from tests.tk_support import needs_tk, new_root


def _app(root, tmp_path):
    """真窗口 + 临时偏好目录（不碰真实 ~/.djb）。"""
    controller = DesktopController(FakeDataSource(dict(app_module._DEMO_SECTIONS), source="test"))
    return app_module.DesktopApp(controller, root=root, prefs_home=str(tmp_path))

EXPECTED_GROUPS = (
    ("connect", "接入", ("agent", "access")),
    ("share", "共享", ("token_plan", "models")),
    ("earn", "收益", ("receipts", "earnings")),
    ("account", "账户", ("yield", "account")),
    ("diag", "诊断", ("overview", "bench", "tasks", "gpu", "logs")),
)


# ───────────────────────────────────────────────────── 1) 分组渲染（GUI-free）
def test_five_groups_by_module_cover_all_thirteen_views():
    plan = app_module.nav_plan(ia.resolve_ia(None))
    assert [(section.key, section.title, tuple(section.keys)) for section, _i, _f in plan] \
        == list(EXPECTED_GROUPS)
    flat = [item[0] for _section, items, _folded in plan for item in items]
    assert tuple(flat) == ia.flat_keys()
    assert sorted(flat) == sorted(VIEW_KEYS)
    assert len(set(flat)) == 13


def test_group_titles_are_chinese_and_have_no_english():
    for section, _items, _folded in app_module.nav_plan(ia.resolve_ia(None)):
        assert not ia.has_english(section.title), section.title
        assert not ia.has_english(section.micro), section.micro


def test_plan_reports_collapsed_state_per_group():
    plan = app_module.nav_plan(ia.resolve_ia(None), {"earn", "diag"})
    folded = {section.key: folded for section, _items, folded in plan}
    assert folded == {"connect": False, "share": False, "earn": True,
                      "account": False, "diag": True}


# ───────────────────────────────────────────────────── 2) 折叠 + 持久化（真窗口）
@needs_tk
def test_sidebar_starts_fully_expanded(tmp_path):
    root = new_root()
    try:
        app = _app(root, tmp_path)
        for key in ("connect", "share", "earn", "account", "diag"):
            assert app._nav_section_bodies[key].winfo_ismapped(), key
    finally:
        root.destroy()


@needs_tk
def test_collapsing_a_group_hides_items_and_persists(tmp_path):
    root = new_root()
    try:
        app = _app(root, tmp_path)
        root.update()
        app._toggle_nav_section("earn")
        root.update()
        assert not app._nav_section_bodies["earn"].winfo_ismapped()
        # 偏好文件里记着这个组（默认不写 = 全展开）
        prefs = json.loads(theme.prefs_path(str(tmp_path)).read_text(encoding="utf-8"))
        assert prefs["nav_collapsed"] == ["earn"]
        # 其它组不受影响
        assert app._nav_section_bodies["share"].winfo_ismapped()
    finally:
        root.destroy()


@needs_tk
def test_collapsed_state_survives_a_restart(tmp_path):
    root = new_root()
    try:
        app = _app(root, tmp_path)
        root.update()
        app._set_nav_collapsed("diag", True)
        root.update()
    finally:
        root.destroy()

    second_root = new_root()
    try:
        again = _app(second_root, tmp_path)
        second_root.update()
        assert "diag" in again._collapsed_sections
        assert not again._nav_section_bodies["diag"].winfo_ismapped()
        assert again._nav_section_chevrons["diag"].cget("text") == "▸"
        # 再点一次展开并落盘
        again._toggle_nav_section("diag")
        second_root.update()
        assert again._nav_section_bodies["diag"].winfo_ismapped()
        prefs = json.loads(theme.prefs_path(str(tmp_path)).read_text(encoding="utf-8"))
        assert prefs["nav_collapsed"] == []
    finally:
        second_root.destroy()


@needs_tk
def test_unknown_saved_group_keys_are_ignored(tmp_path):
    """换了 IA 表/回滚 legacy 后，旧偏好里不存在的组 key 不能把侧栏搞坏。"""
    theme.save_list("nav_collapsed", ["earn", "system", "no_such_group"], str(tmp_path))
    root = new_root()
    try:
        app = _app(root, tmp_path)
        root.update()
        assert app._collapsed_sections == {"earn"}
        for key in ("connect", "share", "account", "diag"):
            assert app._nav_section_bodies[key].winfo_ismapped(), key
    finally:
        root.destroy()


# ───────────────────────────────────────────────────── 3) 13 个入口仍可达
@needs_tk
def test_every_view_is_reachable_even_when_its_group_is_collapsed(tmp_path):
    root = new_root()
    try:
        app = _app(root, tmp_path)
        root.update()
        for key in ("connect", "share", "earn", "account", "diag"):
            app._set_nav_collapsed(key, True)
        root.update()
        for key in VIEW_KEYS:
            assert key in app._nav_buttons, key          # 折叠不等于删掉
            app.show_view(key)
            root.update_idletasks()
            assert app._controller.view == key
            # 去折叠组里的页 → 该组自动展开（选中项不许藏在收起的组里）
            section = ia.section_for(key)
            assert app._nav_section_bodies[section.key].winfo_ismapped(), key
    finally:
        root.destroy()


@needs_tk
def test_nav_items_keep_the_i18n_titles(tmp_path):
    """13 个入口不许改名：侧栏标题就是 ``ia.PAGES`` 里的注册名。"""
    root = new_root()
    try:
        app = _app(root, tmp_path)
        root.update()
        for key in VIEW_KEYS:
            assert app._nav_buttons[key].text == ia.page(key).title, key
    finally:
        root.destroy()
