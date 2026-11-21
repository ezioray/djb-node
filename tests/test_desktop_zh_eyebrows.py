# SPDX-License-Identifier: Apache-2.0
"""UX-E2E-20261121 P1-6（客户端半）：英文眉标中文化 + 同一个东西只叫一个名字。

走查原文三件事：
1. 侧栏分组、页头眉标、hero、图表、右栏用的都是**拉丁大写微标**
   （``NODE / EDGE``、``NODE STATUS / …``、``SHARE``、``CONTRIBUTION RHYTHM``、
   ``SETUP / 接入进度``、``PAUSE / 自动暂停``、``● LIVE``、``SYNC —``）——
   客户的评价是「像没做完的开发者预览版」。
2. 顶栏那颗被读成「6 日新」（证据见 ``_theme_button_text`` 的 docstring）。
3. 同一个功能两个名字：「探针」/「探测」混用（同一页里既有「最近探测」又有
   「模型探测」「立即探测」）。

这里锁的是**客户能看见的字**（投影层字段 + 真窗口控件文案），不锁实现。
"""
from __future__ import annotations

import re

import pytest

from djb_node.desktop import app as desktop_app
from djb_node.desktop import controller as desktop_controller
from djb_node.desktop import ia as ia_mod
from djb_node.desktop import views as vw
from djb_node.desktop.datasource import FakeDataSource, Snapshot

#: 眉标里允许出现的拉丁大写（专有名词 / 单位 / 层号）。新增要写理由。
EYEBROW_ASCII_ALLOWED = frozenset({"GPU", "CPU", "VRAM", "GB", "MB", "L2", "L3", "QPS", "USD", "BETA"})

_UPPER_RUN = re.compile(r"[A-Z]{2,}")


def _ascii_words(text: str) -> set[str]:
    return {word for word in _UPPER_RUN.findall(str(text or "")) if word not in EYEBROW_ASCII_ALLOWED}


def _window_app(tmp_path, width: int = 1440):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:                                     # pragma: no cover - 无显示
        pytest.skip("没有可用的 Tk 显示")
    from djb_node.desktop.app import DesktopApp, _DEMO_SECTIONS
    from djb_node.desktop.controller import DesktopController

    controller = DesktopController(FakeDataSource(dict(_DEMO_SECTIONS), source="test"))
    app = DesktopApp(controller, root=root)
    root.minsize(1, 1)
    root.geometry(str(width) + "x780")
    root.update_idletasks()
    root.update()
    return app, root


# ───────────────────────────────────────────── 1 眉标不再说两种语言
def test_every_page_eyebrow_is_chinese():
    """13 页的 hero / 图表 / 右栏眉标：不许再有拉丁大写词。"""
    snapshot = desktop_app.demo_snapshot()
    findings: dict[str, set[str]] = {}
    for key in vw.VIEW_KEYS:
        view = vw.build_view(key, snapshot)
        texts = [view.hero.kicker if view.hero else "",
                 view.hero.chip if view.hero else ""]
        texts += [view.chart.kicker] if view.chart else []
        texts += [card.kicker for card in view.rail]
        words = set()
        for text in texts:
            words |= _ascii_words(text)
        if words:
            findings[key] = words
    assert not findings, findings


def test_hero_state_pill_is_the_same_language_as_its_title():
    """pill 与标题说的是同一件事，必须同一种语言（原来 pill=英文、标题=中文）。"""
    snapshot = desktop_app.demo_snapshot()
    view = vw.build_view("overview", snapshot)
    chip = view.hero.chip
    assert chip and not _ascii_words(chip), chip
    # 六个运行状态都要有中文说法（不许回落成英文大写或「状态未知」）
    base = dict(desktop_app._DEMO_SECTIONS)
    for state in ("online", "starting", "paused", "updating", "stopped", "error"):
        sections = {**base, "status": {**dict(base.get("status") or {}), "state": state}}
        hero = vw.build_view("overview", Snapshot(sections)).hero
        assert hero is not None
        assert hero.chip and hero.chip != "? 状态未知", (state, hero.chip)
        assert not _ascii_words(hero.chip), (state, hero.chip)


def test_nav_kickers_are_chinese_on_every_page():
    share = ia_mod.resolve_ia(None)
    for key in vw.VIEW_KEYS:
        kicker = desktop_app.nav_kicker(share, key)
        assert kicker and not _UPPER_RUN.search(kicker.replace("GPU", "")), (key, kicker)


def test_legacy_rollback_still_gets_the_0_3_4_english_kicker():
    """回滚态必须与 0.3.4 逐字相等（审计要求）：中文眉标只在新 IA 下生效。"""
    legacy = desktop_app.ia_state("legacy")
    for key in vw.VIEW_KEYS:
        assert desktop_app.nav_kicker(legacy, key) == vw.page_kicker(key)


# ───────────────────────────────────────────── 2 真窗口：侧栏 / 身份块 / SYNC
def _all_label_texts(widget) -> list[str]:
    texts = []
    try:
        children = widget.winfo_children()
    except Exception:                                     # pragma: no cover - 已销毁
        return texts
    for child in children:
        try:
            text = child.cget("text")
        except Exception:
            text = None
        if isinstance(text, str) and text:
            texts.append(text)
        texts.extend(_all_label_texts(child))
    return texts


def test_sidebar_and_identity_block_drop_the_latin_micro_labels(tmp_path):
    app, root = _window_app(tmp_path)
    try:
        texts = _all_label_texts(root)
        assert "本机节点" in texts                       # 原 "NODE / EDGE"
        assert "NODE / EDGE" not in texts
        # 分组头只留中文组名：不许再出现 "SHARE" 这类纯拉丁组头
        sections = {part.title for part in ia_mod.resolve_ia(None).sections}
        assert sections & set(texts)
        assert not [text for text in texts if _UPPER_RUN.fullmatch(text)], texts
    finally:
        root.destroy()


def test_sync_label_says_sync_in_chinese(tmp_path):
    app, root = _window_app(tmp_path)
    try:
        text = app._sync_label.cget("text")
        assert text.startswith("同步"), text
        assert "SYNC" not in text
    finally:
        root.destroy()


def test_theme_button_says_what_it_is_and_flips_the_label(tmp_path):
    """走查把它读成「6 日新」：文案要写「主题：」，切换后跟着变，读屏名说清会变成什么。"""
    app, root = _window_app(tmp_path)
    try:
        assert app._theme_button.text == "主题：日间"
        assert "日间" in app._theme_button_a11y()
        app.toggle_theme(animate=False, persist=False)
        root.update()
        assert app._theme_button.text == "主题：夜间"
        assert "夜间" in app._theme_button_a11y() and "点一下" in app._theme_button_a11y()
    finally:
        root.destroy()


# ───────────────────────────────────────────── 3 探针只有一个名字
def test_models_page_only_says_probe_as_zhencha():
    snapshot = desktop_app.demo_snapshot()
    view = vw.build_view("models", snapshot)
    text = view.plain_text()
    assert "探针" in text
    assert "探测" not in text, text
    for action in view.actions:
        assert "探测" not in action.label and "探测" not in action.reason
    for field in view.fields:
        assert "探测" not in field.hint
    for card in view.cards:
        assert "探测" not in card.title and "探测" not in card.meta


def test_probe_messages_from_the_controller_say_zhencha():
    controller = desktop_controller.DesktopController(FakeDataSource(dict(desktop_app._DEMO_SECTIONS)))
    result = controller.action("probe.run")
    assert "探测" not in result.message
    assert "探针" in result.message


def test_probe_action_label_names_the_thing():
    view = vw.build_view("models", desktop_app.demo_snapshot())
    action = next(item for item in view.actions if item.key == "probe.run")
    assert "探针" in action.label
