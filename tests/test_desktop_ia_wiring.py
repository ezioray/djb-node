# SPDX-License-Identifier: Apache-2.0
"""IA 接线（第三轮）：5 组 13 页 / legacy 回滚 / 占位空态 / BETA / 快捷键顺序。

全部 **GUI-free**：断言的是 ``app.py`` 里的纯数据派生（``nav_plan`` / ``nav_order`` /
``nav_kicker`` / ``nav_combo_values`` / ``route_for`` / ``placeholder_view``）与 ``ia`` 表本身。
真实 Tk 侧栏（分组头位置、13 项、滚动条）在 ``tests/test_desktop_app.py`` 里用真窗口测。

回滚口径（硬要求）：``DJB_UI_IA=legacy`` ⇒ 侧栏/快捷键/下拉/页头眉标**逐项等于 0.3.4**，
即 ``views.NAV_SECTIONS`` + ``views.page_kicker`` 那套；新基线是 ``ia.IA_SECTIONS_SHARE``。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from djb_node.desktop import app as desktop_app
from djb_node.desktop import ia, views
from djb_node.desktop.views import VIEW_KEYS, VIEW_TITLES

#: 0.3.4 回滚口径
LEGACY = ia.resolve_ia("legacy")
#: 新基线（方案 A：5 组 13 页）
SHARE = ia.resolve_ia(None)

APP_SOURCE = pathlib.Path(desktop_app.__file__)


# ── 0.3.4 回滚：表 / 顺序 / 眉标 / 快捷键 逐项相等
def test_legacy_table_equals_0_3_4_nav_sections():
    """``ia.IA_SECTIONS_LEGACY`` 与 ``views.NAV_SECTIONS`` **逐项**相等（4 组 13 页）。"""
    assert len(ia.IA_SECTIONS_LEGACY) == len(views.NAV_SECTIONS) == 4
    for new, old in zip(ia.IA_SECTIONS_LEGACY, views.NAV_SECTIONS):
        assert (new.key, new.title, new.micro, new.keys) == (old.key, old.title, old.micro, old.keys)


def test_legacy_order_labels_and_icons_are_item_by_item_identical():
    """回滚模式下侧栏计划：分组数、顺序、标题、图标全与 0.3.4 一致。"""
    plan = desktop_app.nav_plan(LEGACY)
    assert [section.key for section, _ in plan] == [s.key for s in views.NAV_SECTIONS]
    flat = [item[0] for _, items in plan for item in items]
    assert tuple(flat) == VIEW_KEYS
    for _, items in plan:
        for key, label, icon, _badge in items:
            assert label == VIEW_TITLES[key]
            assert icon == desktop_app.NAV_ICONS[key]


def test_legacy_has_no_beta_badge_and_no_new_order():
    """BETA 徽标只在方案 A 出现：0.3.4 的侧栏没有这个标记，回滚要逐项相等。"""
    badges = [item[3] for _, items in desktop_app.nav_plan(LEGACY) for item in items]
    assert badges == [""] * 13
    assert desktop_app.nav_order(LEGACY) == VIEW_KEYS
    assert desktop_app.nav_order(SHARE) != VIEW_KEYS          # 新基线的确换了顺序


def test_legacy_kickers_match_0_3_4_page_kicker():
    """回滚时页头眉标逐项等于 ``views.page_kicker``（``RUN / OVERVIEW`` 那套）。"""
    for key in VIEW_KEYS:
        assert desktop_app.nav_kicker(LEGACY, key) == views.page_kicker(key)
    # 模块级常量仍是 0.3.4 口径（给旧调用方/截图基线的兼容面）
    assert desktop_app.PAGE_KICKERS == {key: desktop_app.nav_kicker(LEGACY, key) for key in VIEW_KEYS}


def test_legacy_shortcuts_are_the_0_3_4_bindings():
    assert (desktop_app.view_shortcut_bindings(keys=desktop_app.nav_order(LEGACY))
            == desktop_app.view_shortcut_bindings())
    bindings = dict(desktop_app.view_shortcut_bindings(keys=desktop_app.nav_order(LEGACY)))
    assert bindings["<Control-Key-1>"] == "overview"          # 0.3.4：Ctrl+1 = 总览


# ── 新基线：5 组 13 页
def test_share_baseline_is_five_groups_and_thirteen_pages():
    sections = SHARE.sections
    assert SHARE.mode == "share"
    assert len(sections) == 5
    assert [s.key for s in sections] == ["share", "run", "earn", "connect", "system"]
    order = desktop_app.nav_order(SHARE)
    assert len(order) == len(set(order)) == 13
    assert order[0] == "token_plan"                           # 共享前置
    assert order == ia.flat_keys(ia.IA_SECTIONS_SHARE)
    # 13 个页面一个不少、一个不并
    assert set(order) == set(ia.PAGES) == set(VIEW_KEYS)


def test_gpu_stays_out_of_the_first_screen():
    """§5.3：GPU 不许进前 N 项（首屏不出现 GPU 主 CTA）。"""
    order = desktop_app.nav_order(SHARE)
    assert order.index("gpu") >= ia.MIN_GPU_INDEX
    assert ia.page("gpu").section == "system"


def test_beta_badge_is_only_on_gpu():
    plan = desktop_app.nav_plan(SHARE)
    badged = {key: badge for _, items in plan for key, _l, _i, badge in items if badge}
    assert badged == {"gpu": "BETA"}
    assert ia.page("gpu").beta and ia.page("gpu").subtitle.startswith("GPU 监控（BETA）")


def test_share_kickers_follow_the_new_sections():
    assert desktop_app.nav_kicker(SHARE, "token_plan") == "SHARE / TOKEN PLAN"
    assert desktop_app.nav_kicker(SHARE, "models") == "SHARE / MODELS"
    assert desktop_app.nav_kicker(SHARE, "gpu") == "SYSTEM / GPU"
    assert desktop_app.nav_kicker(SHARE, "agent") == "CONNECT / AGENT"
    # 与 0.3.4 相比确实变了（证明新表真的生效，而不是回滚表）
    changed = [key for key in VIEW_KEYS
               if desktop_app.nav_kicker(SHARE, key) != desktop_app.nav_kicker(LEGACY, key)]
    assert "token_plan" in changed and "gpu" in changed


def test_shortcuts_follow_the_active_order():
    order = desktop_app.nav_order(SHARE)
    bindings = dict(desktop_app.view_shortcut_bindings(keys=order))
    assert bindings["<Control-Key-1>"] == "token_plan"
    assert bindings["<Control-Key-9>"] == "agent"
    assert bindings["<Control-Shift-at>"] == "gpu"        # Shift+2 = @（X11 键名）
    assert len(bindings) == 13 and set(bindings.values()) == set(order)


def test_combo_values_match_the_sidebar_order():
    values = desktop_app.nav_combo_values(SHARE)
    assert values[0] == "1 · 共享 Token Plan"
    assert values == tuple(str(i) + " · " + ia.page(key).title
                           for i, key in enumerate(desktop_app.nav_order(SHARE), start=1))
    assert desktop_app.nav_combo_values(LEGACY)[0] == "1 · " + VIEW_TITLES[VIEW_KEYS[0]]


# ── 13 页可达 + 占位空态
def test_all_thirteen_pages_route_to_a_real_view():
    """13 页全部有渲染实现：不需要占位页，也不会走到 unknown。"""
    for key in ia.PAGES:
        assert desktop_app.route_for(key, VIEW_TITLES) == ("view", key), key


def test_every_page_is_reachable_from_navigation():
    """可达性 = 在侧栏里 + 有 Ctrl+N + 在下拉里有编号（三条路都指向同一个 key）。"""
    order = desktop_app.nav_order(SHARE)
    combos = desktop_app.nav_combo_values(SHARE)
    shortcuts = [key for _seq, key in desktop_app.view_shortcut_bindings(keys=order)]
    for index, key in enumerate(order, start=1):
        assert combos[index - 1].startswith(str(index) + " · ")
        assert key in shortcuts
    assert sorted(shortcuts, key=order.index) == list(order)


def test_registered_but_unimplemented_page_renders_a_placeholder(monkeypatch):
    """新基线先占位：IA 里注册了、渲染层还没实现的页，必须给空态而不是崩。"""
    spec = ia.PageSpec("future_page", "未来页", "FUTURE", "system")
    monkeypatch.setitem(ia.PAGES, "future_page", spec)
    route, payload = desktop_app.route_for("future_page", VIEW_TITLES)
    assert route == "placeholder" and payload is spec
    view = desktop_app.placeholder_view(spec)
    text = view.plain_text()
    assert view.key == "future_page" and "未来页" in text
    assert "占位页" in text and "非故障" in text             # 空态 ≠ 报错 ≠ 暂无数据


def test_unknown_key_keeps_the_0_3_4_behaviour():
    """不在 IA 注册表里的 key：回当前页（0.3.4 的 ``ValueError`` 语义不变）。"""
    assert desktop_app.route_for("no_such_page", VIEW_TITLES) == ("unknown", None)


# ── 单一数据源守门
def test_app_no_longer_reads_the_legacy_nav_table():
    """侧栏不许再读 ``views.NAV_SECTIONS``（回滚表只准经 ``ia`` 生效）。"""
    tree = ast.parse(APP_SOURCE.read_text())
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "NAV_SECTIONS" not in names
    assert "ia" in {alias.name for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) for alias in node.names}


@pytest.mark.parametrize("raw, mode, ignored", [
    (None, "share", False),
    ("legacy", "legacy", False),
    ("LEGACY", "legacy", False),
    ("  legacy  ", "legacy", False),
    ("share", "share", False),
    ("nonsense", "share", True),                            # 未识别：不静默当默认
    ("", "share", True),
])
def test_ia_mode_resolution(raw, mode, ignored):
    decision = desktop_app.ia_state(raw)
    assert (decision.mode, decision.ignored) == (mode, ignored)


def test_ia_state_reads_the_environment(monkeypatch):
    monkeypatch.setenv("DJB_UI_IA", "legacy")
    assert desktop_app.ia_state().mode == "legacy"
    monkeypatch.delenv("DJB_UI_IA", raising=False)
    assert desktop_app.ia_state().mode == "share"
