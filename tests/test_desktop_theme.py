# SPDX-License-Identifier: Apache-2.0
"""Daylight Signal 主题回归：token 取值、对比度、clam 浅色 map（不创建 Tk）。

对比度用 :func:`djb_node.gui.theme.contrast_ratio` 现算，数字不手抄；
暗色主题已按 `UI-BASELINE-2026-11-01` 整体替换，这里同时锁住「旧暗色不再回来」。
"""
from __future__ import annotations

import pytest

from djb_node.gui import theme

#: 基准表 §1 的取值（页面底 / 卡 / 字 / 主色 / 等待 / 失败）
BASELINE = {
    "bg": "#EEF5F1", "bg2": "#E6F0EB", "surface": "#FFFFFF", "surface2": "#F7FBF9",
    "ink": "#0D2925", "ink2": "#29443F", "muted": "#607A74", "line": "#D6E4DE",
    "line2": "#C2D6CE", "mint": "#008F78", "mint2": "#00B89A", "mint3": "#D9F5EC",
    "amber": "#A9741C", "red": "#B94F47", "cyan": "#2F7FA3", "violet": "#6C55A3",
}
#: 旧暗色值：出现任何一个都说明换肤没换干净
DARK_LEFTOVERS = ("#090C12", "#121622", "#1A2130", "#E6EDF3", "#ADB9CA", "#71839C",
                  "#64F4D5", "#B49AFF", "#F2C45C", "#FF8C83", "#66E3AC")


class FakeStyle:
    def __init__(self, root):
        self.root = root
        self.theme = None
        self.configured: dict[str, dict] = {}
        self.mapped: dict[str, dict] = {}

    def theme_use(self, name):
        self.theme = name

    def configure(self, name, **options):
        self.configured.setdefault(name, {}).update(options)

    def map(self, name, **options):
        self.mapped.setdefault(name, {}).update(options)


class FakeTtk:
    def __init__(self):
        self.styles: list[FakeStyle] = []

    def Style(self, root):                       # noqa: N802 - 模拟 ttk.Style
        style = FakeStyle(root)
        self.styles.append(style)
        return style


class FakeRoot:
    def __init__(self):
        self.background = None
        self.options: dict[str, object] = {}
        self.tk = self

    def configure(self, **options):
        self.background = options.get("background", self.background)

    def option_add(self, pattern, value):
        self.options[pattern] = value

    def call(self, *args):
        return ("Fake Family", "DejaVu Sans", "Consolas")


def test_tokens_match_baseline():
    for name, value in BASELINE.items():
        assert theme.TOKENS[name] == value, name


def test_every_colour_is_six_digit_hex():
    for name, value in theme.TOKENS.items():
        assert len(value) == 7 and value.startswith("#"), name
        int(value[1:], 16)                                       # 非法 hex 直接抛 ValueError


def test_no_dark_theme_leftovers():
    all_values = set(theme.TOKENS.values()) | set(theme.COLORS.values())
    for leftover in DARK_LEFTOVERS:
        assert leftover not in all_values, leftover


@pytest.mark.parametrize("name", ["text", "text2", "muted", "cyan", "danger", "ok", "wait"])
def test_text_tokens_readable_on_page_and_card(name):
    for background in ("bg", "panel", "input"):
        ratio = theme.contrast_ratio(theme.COLORS[name], theme.COLORS[background])
        assert ratio >= 4.5, (name, background, round(ratio, 2))


def test_control_boundary_is_visible():
    assert theme.contrast_ratio(theme.BORDER, theme.TOKENS["surface"]) >= 3
    assert theme.contrast_ratio(theme.BORDER, theme.COLORS["input"]) >= 3


def test_pills_readable():
    pairs = ((theme.TOKENS["mint3"], theme.MINT_TEXT),
             (theme.TOKENS["amber_bg"], theme.AMBER_TEXT),
             (theme.TOKENS["red_bg"], theme.RED_TEXT),
             (theme.TOKENS["bg2"], theme.MUTED_TEXT))
    for background, foreground in pairs:
        assert theme.contrast_ratio(foreground, background) >= 4.5, (background, foreground)


def test_mint_fill_and_white_label():
    assert theme.contrast_ratio("#FFFFFF", theme.TOKENS["mint"]) >= 3   # 非文字 / 大字号


def test_apply_theme_uses_clam_and_light_fields():
    root, ttk = FakeRoot(), FakeTtk()
    style = theme.apply_theme(root, ttk)
    assert style.theme == "clam"
    assert root.background == theme.TOKENS["bg"]
    assert root.options["*Font"][0] in theme.FONT_FAMILIES
    # 关键控件的底色必须是浅色（不能被系统灰盖住）
    assert style.configured["TFrame"]["background"] == theme.TOKENS["bg"]
    assert style.configured["Card.TFrame"]["background"] == theme.TOKENS["surface"]
    assert style.configured["Sidebar.TFrame"]["background"] == theme.TOKENS["bg2"]
    assert style.configured["TEntry"]["fieldbackground"] == theme.TOKENS["surface2"]
    assert style.configured["Treeview"]["fieldbackground"] == theme.TOKENS["surface"]
    assert style.configured["TEntry"]["foreground"] == theme.TOKENS["ink"]
    # 浅色 map：禁用态用弱化文字色而不是白字
    disabled = dict(style.mapped["TButton"]["foreground"])["disabled"]
    assert disabled == theme.MUTED_TEXT
    # 焦点环用主色
    assert dict(style.mapped["TButton"]["bordercolor"])["focus"] == theme.TOKENS["focus"]


def test_apply_theme_accepts_injected_families():
    root, ttk = FakeRoot(), FakeTtk()
    theme.apply_theme(root, ttk, available_families={"PingFang SC", "Menlo"})
    assert theme.FONT[0] == "PingFang SC"
    assert theme.MONO[0] == "Menlo"


def test_named_styles_exist_for_every_surface():
    root, ttk = FakeRoot(), FakeTtk()
    style = theme.apply_theme(root, ttk)
    for name in ("H1.TLabel", "H2.TLabel", "H3.TLabel", "Card.TLabel", "CardMuted.TLabel",
                 "Meta.TLabel", "Signal.TLabel", "Pill.TLabel", "PillWait.TLabel",
                 "PillFail.TLabel", "PillMuted.TLabel", "Primary.TButton", "Nav.TButton",
                 "NavOn.TButton", "TopH2.TLabel", "TopH3.TLabel", "TopMeta.TLabel",
                 "Card.TFrame", "CardAlt.TFrame", "Sidebar.TFrame", "Topbar.TFrame"):
        assert name in style.configured, name


def test_menu_options_are_light():
    options = theme.menu_options()
    assert options["background"] == theme.TOKENS["surface"]
    assert options["foreground"] == theme.TOKENS["ink"]
    assert options["activebackground"] == theme.TOKENS["mint3"]


def test_contrast_helpers_reject_bad_input():
    assert theme.contrast_ratio("#FFFFFF", "#000000") == pytest.approx(21.0)
    assert theme.contrast_ratio("#777777", "#777777") == pytest.approx(1.0)
    with pytest.raises(ValueError):
        theme.luminance("white")
    with pytest.raises(ValueError):
        theme.luminance("#GGGGGG")
    with pytest.raises(KeyError):
        theme.token("no-such-token")


def test_legacy_keys_still_present():
    for key in ("bg", "panel", "input", "text", "muted", "border", "cyan", "violet",
                "wait", "danger", "ok"):
        assert key in theme.COLORS, key
