# SPDX-License-Identifier: Apache-2.0
"""桌面端双主题（day / night）：色板齐全、文字可读、偏好读写、切换不抛异常。

对应 ``ops/uta/DESIGN-DUAL-THEME-2026-11-01.md``：
**两套都是赛博朋克，差异只在明度与光晕强度**；不允许一套有动效另一套没有。
这里只锁前三件事（色板 / 偏好 / 切换），动效靠「切换不抛异常 + 最终态正确」间接保证。
"""
from __future__ import annotations

import json
import os
import stat
import time

import pytest

from djb_node.desktop import theme
from djb_node.gui.theme import TOKENS as GUI_TOKENS
from djb_node.gui.theme import contrast_ratio, luminance

#: 规范 §3 里两套必须一致的那几个 token（取值同源 web theme.css）
SPEC = {
    "day": {"bg": "#E9F6F1", "bg2": "#DDF0E8", "surface": "#FFFFFF", "surface2": "#F4FBF8",
            "mint": "#00866F", "lime": "#5E9C1F"},
    "night": {"bg": "#061A18", "bg2": "#0A2521", "surface": "#0C2B26", "surface2": "#103A33",
              "mint": "#79F2D9", "lime": "#C8F36A"},
}

#: 文字要在卡片底与页面底上都 ≥4.5:1（WCAG AA 正文）
TEXT_TOKENS = ("ink", "ink2", "muted_text", "mint_text", "amber_text", "red_text", "info_text")


# ────────────────────────────────────────────────────────────── 色板
def test_both_themes_exist_and_share_the_same_token_set():
    """两套主题 token 名必须完全一致——少一个键就会在另一套下掉回默认色。"""
    assert set(theme.available()) == {"day", "night"}
    keys = {name: set(theme.palette(name)) for name in theme.available()}
    assert keys["day"] == keys["night"]


@pytest.mark.parametrize("name", ["day", "night"])
def test_spec_tokens_match_the_shared_design_file(name):
    """规范 §3 的 token 取值原样落地（两套同源，才能和 web 对得上）。"""
    palette = theme.palette(name)
    for token, expected in SPEC[name].items():
        assert palette[token].upper() == expected.upper(), token


def test_themes_differ_in_lightness_not_in_structure():
    """差异只在明度与光晕强度：day 亮、night 暗，token 数量与键完全一样。"""
    day, night = theme.palette("day"), theme.palette("night")
    assert luminance(day["bg"]) > 0.8
    assert luminance(night["bg"]) < 0.05
    assert luminance(day["surface"]) > luminance(night["surface"])
    assert len(day) == len(night)


@pytest.mark.parametrize("name", ["day", "night"])
def test_every_text_token_is_readable_on_card_and_page(name):
    palette = theme.palette(name)
    for token in TEXT_TOKENS:
        for ground in ("surface", "bg"):
            ratio = contrast_ratio(palette[token], palette[ground])
            assert ratio >= 4.5, (name, token, ground, round(ratio, 2))


@pytest.mark.parametrize("name", ["day", "night"])
def test_primary_button_label_stays_readable(name):
    """主按钮底是 mint 填充，字色必须跟着主题走（夜间不能用白字压亮底）。"""
    palette = theme.palette(name)
    assert contrast_ratio(palette["on_mint"], palette["mint"]) >= 4.5


def test_gui_token_mapping_is_complete():
    """``gui.theme.TOKENS`` 的每个键都要有映射，否则 apply 时会 KeyError。"""
    assert set(theme.gui_tokens("night")) == set(GUI_TOKENS)
    assert theme.gui_tokens("night")["bg"] == theme.palette("night")["bg"]


def test_unknown_theme_falls_back_to_day_instead_of_raising():
    assert theme.palette("dawn")["bg"] == theme.palette("day")["bg"]
    assert theme.set_active("nope") == "day"


def test_toggle_flips_between_the_two():
    assert theme.toggle("day") == "night"
    assert theme.toggle("night") == "day"


def test_blend_walks_from_start_to_end_and_clamps():
    assert theme.blend("#000000", "#FFFFFF", 0.0) == "#000000"
    assert theme.blend("#000000", "#FFFFFF", 1.0) == "#FFFFFF"
    assert theme.blend("#000000", "#FFFFFF", 0.5) == "#808080"
    assert theme.blend("#000000", "#FFFFFF", 2.0) == "#FFFFFF"      # 越界夹住
    assert theme.mix_step("#000000", "#FFFFFF", 3, 3) == "#FFFFFF"


# ────────────────────────────────────────────────────────────── 偏好文件
def test_prefs_round_trip_is_private_and_only_holds_the_theme(tmp_path):
    path = theme.save("night", tmp_path)
    assert path.name == theme.PREFS_FILE
    assert theme.load(tmp_path) == "night"
    assert theme.load(tmp_path) == "night"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {"theme": "night"}                # 只有主题名，没有任何凭据
    assert theme.save("day", tmp_path) == path
    assert theme.load(tmp_path) == "day"


def test_missing_or_broken_prefs_read_as_day(tmp_path):
    assert theme.load(tmp_path) == theme.DEFAULT_THEME
    (tmp_path / theme.PREFS_FILE).write_text("{not json", encoding="utf-8")
    assert theme.load(tmp_path) == theme.DEFAULT_THEME
    (tmp_path / theme.PREFS_FILE).write_text('{"theme": "disco"}', encoding="utf-8")
    assert theme.load(tmp_path) == theme.DEFAULT_THEME


def test_saving_an_unknown_theme_is_refused(tmp_path):
    with pytest.raises(ValueError):
        theme.save("disco", tmp_path)
    assert not (tmp_path / theme.PREFS_FILE).exists()


# ────────────────────────────────────────────────────────────── 切换（真窗口）
def _window_app(tmp_path):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:                                  # pragma: no cover - 无显示
        pytest.skip("没有可用的 Tk 显示")
    from djb_node.desktop.app import DesktopApp, _DEMO_SECTIONS
    from djb_node.desktop.controller import DesktopController
    from djb_node.desktop.datasource import FakeDataSource

    controller = DesktopController(FakeDataSource(dict(_DEMO_SECTIONS), source="test"))
    app = DesktopApp(controller, root=root, prefs_home=tmp_path)
    root.update_idletasks()
    return app, root


def test_theme_toggle_switches_palette_persists_and_does_not_raise(tmp_path):
    """点一下 day↔night：样式换过去、偏好写进去、当前视图重画不抛异常。"""
    app, root = _window_app(tmp_path)
    try:
        assert app._theme_name == "day"                # noqa: SLF001 - 断言用
        assert app.toggle_theme(animate=False) == "night"
        root.update_idletasks()
        assert theme.current() == "night"
        assert theme.load(tmp_path) == "night"         # 偏好落盘
        assert root.cget("background").upper() == theme.palette("night")["bg"]
        assert app.toggle_theme(animate=False) == "day"
        assert theme.load(tmp_path) == "day"
    finally:
        root.destroy()


def test_animated_toggle_lands_on_the_target_theme(tmp_path):
    """带动画的切换（3 帧背景渐变）跑完后必须停在目标主题，不能卡在中间色。"""
    app, root = _window_app(tmp_path)
    try:
        app.toggle_theme(animate=True)
        deadline = time.time() + 2.0
        while time.time() < deadline and app._fade_job is not None:   # noqa: SLF001
            root.update()
            time.sleep(0.01)
        root.update()
        assert app._fade_job is None                    # noqa: SLF001 - 渐变已收尾
        assert app._theme_name == "night"               # noqa: SLF001
        assert root.cget("background").upper() == theme.palette("night")["bg"]
    finally:
        root.destroy()


def test_switching_theme_does_not_touch_the_data_path(tmp_path):
    """换肤只动样式：视图数据、当前视图 key、快照来源都不该变。"""
    app, root = _window_app(tmp_path)
    try:
        before = (app._controller.view, app._controller.snapshot.source)  # noqa: SLF001
        app.toggle_theme(animate=False)
        after = (app._controller.view, app._controller.snapshot.source)   # noqa: SLF001
        assert before == after
    finally:
        root.destroy()
