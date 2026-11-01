# SPDX-License-Identifier: Apache-2.0
"""桌面端双主题（day / night）：色板齐全、文字可读、偏好读写、切换不抛异常。

口径：`design/15-DJB-Aurora-Signal-配色修订-v1.md` §1.1 —— night 紫墨夜色 / day 藕粉晨光，
两套共用同一组 token 名，差异只在明度与光晕强度；不允许一套有动效另一套没有。
Tk 没有 alpha，``rgba()`` 的 token 在这里落成**合成后的实色**，合成底写在 ``DERIVED`` 的注释里。
对比度一律用 :func:`djb_node.gui.theme.contrast_ratio` 现算，阈值不放宽。
"""
from __future__ import annotations

import json
import os
import stat
import time

import pytest

from djb_node.desktop import theme
from djb_node.gui import theme as gui_theme
from djb_node.gui.theme import TOKENS as GUI_TOKENS
from djb_node.gui.theme import contrast_ratio, luminance

#: §1.1 直接取值（day 列 = 藕粉晨光）
SPEC_DAY = {
    "bg": "#F5F1FA", "bg2": "#EDE7F6", "surface": "#FFFFFF", "surface2": "#FAF7FE",
    "surface3": "#F3EDFA",
    "ink": "#1B1530", "ink2": "#3C3457", "muted": "#6B6385",
    "mint": "#2F7D66", "mint2": "#3FA184", "mint3": "#DDF3EA",
    "mint_strong": "#256B57", "mint_hover": "#2A735E", "mint_text": "#256B57",
    "lime": "#5F7F3A", "cyan": "#2F7F9E", "cyan_text": "#2A6E8C", "info_text": "#2A6E8C",
    "violet": "#6A4FBF", "violet2": "#8A6FE0", "violet3": "#EEE8FF",
    "violet_text": "#5B3FA8",
    "pink": "#B93C7E", "pink2": "#D967A6", "pink3": "#FBE7F3", "pink_text": "#A83270",
    "amber": "#9A6A16", "amber_text": "#8A5D12", "amber_bg": "#FDF4E1",
    "red": "#B94F47", "red_text": "#A84540", "red_bg": "#FBEAE8",
    "on_mint": "#FFFFFF", "on_violet": "#FFFFFF", "on_pink": "#FFFFFF",
    "focus": "#2F7D66", "white": "#FFFFFF",
}
#: §1.1 直接取值（night 列 = 紫墨夜色）
SPEC_NIGHT = {
    "bg": "#0A0915", "bg2": "#111020", "surface": "#151327", "surface2": "#1D1933",
    "surface3": "#241F3D",
    "ink": "#F1EDFB", "ink2": "#D3CCE8", "muted": "#A49BC0",
    "mint": "#8FD9BE", "mint2": "#B4EAD6", "mint_strong": "#C9F5E5",
    "mint_hover": "#A5E4CC", "mint_text": "#A9E8D3",
    "lime": "#C6DD9B", "cyan": "#7FCBE8", "cyan_text": "#A5DDF1", "info_text": "#A5DDF1",
    "violet": "#B9A2FF", "violet2": "#D4C7FF", "violet_text": "#CDBDFF",
    "pink": "#F4A6D7", "pink2": "#F9C9E4", "pink_text": "#F8C4E2",
    "amber": "#F0C56B", "amber_text": "#F7DCA6",
    "red": "#FF9B93", "red_text": "#FFC4BF",
    "on_mint": "#08221B", "on_violet": "#14112B", "on_pink": "#2A1030",
    "focus": "#8FD9BE", "white": "#08221B",
}
#: Tk 派生值（Tk 无 alpha：合成后的实色；每个值下面注明合成了什么）
DERIVED = {
    "day": {
        "line": "#DBD5E6",      # rgba(60,40,110,.14) over bg
        "line2": "#C9C1D8",     # rgba(60,40,110,.24) over bg
        "line3": "#8F82AD",     # rgba(60,40,110,.55) over bg（控件边界 ≥3:1）
        "glow": "#CDDADC",      # rgba(47,125,102,.20) over bg
        "glow2": "#DFD7F1",     # rgba(106,79,191,.16) over bg
        "glow3": "#EEDBEB",     # rgba(185,60,126,.12) over bg
        "muted_text": "#5B5375",  # 说明文字（muted 再深一档）
        "lime_text": "#4F6A2C",   # 装饰绿文字
        "border": "#8F82AD",      # 与 line3 同源
    },
    "night": {
        "line": "#2A2741",      # rgba(198,183,255,.12) over surface
        "line2": "#3C3757",     # rgba(198,183,255,.22) over surface
        "line3": "#76699E",     # ≈ rgba(198,183,255,.50) over surface-3（控件边界 ≥3:1）
        "glow": "#324748",      # rgba(143,217,190,.30) over bg
        "glow2": "#3B3457",     # rgba(185,162,255,.28) over bg
        "glow3": "#3D2C40",     # rgba(244,166,215,.22) over bg
        "mint3": "#262F3C",     # rgba(143,217,190,.14) over surface
        "violet3": "#2C2745",   # rgba(185,162,255,.14) over surface
        "pink3": "#32263E",     # rgba(244,166,215,.13) over surface
        "amber_bg": "#312A30",  # rgba(240,197,107,.13) over surface
        "red_bg": "#362636",    # rgba(255,155,147,.14) over surface
        "muted_text": "#B4ACCB",  # 说明文字（muted 再亮一档）
        "lime_text": "#D2E7AE",   # 装饰绿文字
        "border": "#76699E",      # 与 line3 同源
    },
}

#: 文字要在卡片底、输入底与页面底上都 ≥4.5:1（WCAG AA 正文）
TEXT_TOKENS = ("ink", "ink2", "muted_text", "mint_text", "amber_text", "red_text",
               "info_text", "cyan_text", "violet_text", "pink_text")

def _hue(color: str) -> float:
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    high, low = max(channels), min(channels)
    span = high - low
    if span == 0:
        return 0.0
    red, green, blue = channels
    if high == red:
        hue = ((green - blue) / span) % 6
    elif high == green:
        hue = (blue - red) / span + 2
    else:
        hue = (red - green) / span + 4
    return hue * 60


def _hue_gap(first: str, second: str) -> float:
    gap = abs(_hue(first) - _hue(second)) % 360
    return min(gap, 360 - gap)


# ────────────────────────────────────────────────────────────── 色板
def test_both_themes_exist_and_share_the_same_token_set():
    """两套主题 token 名必须完全一致——少一个键就会在另一套下掉回默认色。"""
    assert set(theme.available()) == {"day", "night"}
    keys = {name: set(theme.palette(name)) for name in theme.available()}
    assert keys["day"] == keys["night"]


@pytest.mark.parametrize("name,spec", (("day", SPEC_DAY), ("night", SPEC_NIGHT)))
def test_spec_tokens_match_the_shared_design_file(name, spec):
    """规范 §1.1 的取值原样落地（两套同源，才能和 web 对得上）。"""
    palette = theme.palette(name)
    for token, expected in spec.items():
        assert palette[token].upper() == expected.upper(), token


@pytest.mark.parametrize("name", ("day", "night"))
def test_tk_derived_tokens_match_and_cover_every_key(name):
    """派生值逐个对齐；色板里不许有「没登记来源」的键。"""
    palette = theme.palette(name)
    spec = SPEC_DAY if name == "day" else SPEC_NIGHT
    for token, expected in DERIVED[name].items():
        assert palette[token].upper() == expected.upper(), token
    assert set(palette) == set(spec) | set(DERIVED[name])


@pytest.mark.parametrize("name", ("day", "night"))
def test_violet_and_pink_keys_are_present(name):
    palette = theme.palette(name)
    for token in ("violet2", "violet3", "violet_text", "pink", "pink2", "pink3",
                  "pink_text", "on_violet", "on_pink"):
        assert token in palette, token


def test_themes_differ_in_lightness_not_in_structure():
    """差异只在明度与光晕强度：day 亮、night 暗，token 数量与键完全一样。"""
    day, night = theme.palette("day"), theme.palette("night")
    assert luminance(day["bg"]) > 0.8
    assert luminance(night["bg"]) < 0.05
    assert luminance(day["surface"]) > luminance(night["surface"])
    assert len(day) == len(night)


@pytest.mark.parametrize("name", ("day", "night"))
def test_every_palette_value_is_pinned(name):
    """两套色板的每个键都钉死在 SPEC/DERIVED 上：旧色要回来就必须改期望值表。

    这里刻意不维护「退役色值」清单——那张表会把旧十六进制写回实现仓，
    与验收的「旧值 grep 0 命中」冲突；回归保护由上面的全量取值断言承担。
    """
    spec = SPEC_DAY if name == "day" else SPEC_NIGHT
    palette = theme.palette(name)
    assert set(palette) == set(spec) | set(DERIVED[name])
    for token, value in palette.items():
        assert value.upper() == (spec.get(token) or DERIVED[name][token]).upper(), token


# ────────────────────────────────────────────────────────────── 对比度（阈值不放宽）
@pytest.mark.parametrize("name", ("day", "night"))
def test_every_text_token_is_readable_on_card_and_page(name):
    palette = theme.palette(name)
    for token in TEXT_TOKENS:
        for ground in ("surface", "surface2", "bg"):
            ratio = contrast_ratio(palette[token], palette[ground])
            assert ratio >= 4.5, (name, token, ground, round(ratio, 2))


@pytest.mark.parametrize("name", ("day", "night"))
def test_primary_button_label_stays_readable(name):
    """主按钮底是玉绿填充，字色必须跟着主题走（夜间不能用白字压亮底）。"""
    palette = theme.palette(name)
    assert contrast_ratio(palette["on_mint"], palette["mint"]) >= 4.5


@pytest.mark.parametrize("name", ("day", "night"))
def test_violet_and_pink_button_labels_stay_readable(name):
    """紫/粉实心填充上的字也必须 ≥4.5:1（按钮字标准，不是非文字的 3:1）。"""
    palette = theme.palette(name)
    assert contrast_ratio(palette["on_violet"], palette["violet"]) >= 4.5
    assert contrast_ratio(palette["on_pink"], palette["pink"]) >= 4.5


@pytest.mark.parametrize("name", ("day", "night"))
def test_violet_and_pink_tints_carry_readable_text(name):
    palette = theme.palette(name)
    assert contrast_ratio(palette["violet_text"], palette["violet3"]) >= 4.5
    assert contrast_ratio(palette["pink_text"], palette["pink3"]) >= 4.5


@pytest.mark.parametrize("name", ("day", "night"))
def test_control_boundary_is_at_least_three_to_one(name):
    """控件边界 ≥3:1（WCAG 1.4.11）；line3 与 border 同源，四处底色都要过。"""
    palette = theme.palette(name)
    assert palette["border"] == palette["line3"]
    for ground in ("surface", "surface2", "surface3", "bg"):
        ratio = contrast_ratio(palette["border"], palette[ground])
        assert ratio >= 3, (name, ground, round(ratio, 2))


@pytest.mark.parametrize("name", ("day", "night"))
def test_status_pills_stay_readable(name):
    palette = theme.palette(name)
    for foreground, background in (("mint_text", "mint3"), ("amber_text", "amber_bg"),
                                   ("red_text", "red_bg"), ("muted_text", "bg2")):
        ratio = contrast_ratio(palette[foreground], palette[background])
        assert ratio >= 4.5, (name, foreground, background, round(ratio, 2))


@pytest.mark.parametrize("name", ("day", "night"))
@pytest.mark.parametrize("status", ("red", "amber", "mint"))
def test_pink_never_stands_in_for_a_status(name, status):
    """粉只做装饰：与失败/等待/完成三色的色相至少差 25°，取值也不共用。"""
    palette = theme.palette(name)
    for pink in ("pink", "pink2", "pink_text"):
        assert palette[pink] != palette[status], pink
        gap = _hue_gap(palette[pink], palette[status])
        assert gap >= 25, (name, pink, status, round(gap, 1))


# ────────────────────────────────────────────────────────────── gui 映射
def test_gui_token_mapping_is_complete():
    """``gui.theme.TOKENS`` 的每个键都要有映射，否则 apply 时会 KeyError。"""
    assert set(theme.gui_tokens("night")) == set(GUI_TOKENS)
    assert theme.gui_tokens("night")["bg"] == theme.palette("night")["bg"]


def test_gui_named_constants_match_the_day_palette():
    """``gui.theme`` 的默认（静态浅色）就是 day 列：具名常量不许和色板漂移。"""
    day = theme.palette("day")
    for constant, token in theme.GUI_CONSTANTS.items():
        assert getattr(gui_theme, constant) == day[token], constant


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
