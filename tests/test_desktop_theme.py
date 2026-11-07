# SPDX-License-Identifier: Apache-2.0
"""Aurora Signal 主题回归：token 取值、对比度、clam 浅色 map（不创建 Tk）。

口径：`design/15-DJB-Aurora-Signal-配色修订-v1.md` §1.1 的 **day（藕粉晨光）** 列。
对比度用 :func:`djb_node.gui.theme.contrast_ratio` 现算，数字不手抄；
阈值按 WCAG 2.1 AA —— 正文 4.5、控件/图形 3.0、**实心按钮上的字 4.5**，一律不放宽。
粉色只做非状态强调，本文件另有一条色相距离断言把它和状态色隔开。
"""
from __future__ import annotations

import pytest

from djb_node.gui import theme

#: §1.1 day 列的直接取值（含 §1.2 的 amber-3）
BASELINE = {
    "bg": "#F5F1FA", "bg2": "#EDE7F6", "surface": "#FFFFFF", "surface2": "#FAF7FE",
    "surface3": "#F3EDFA",
    "ink": "#1B1530", "ink2": "#3C3457", "muted": "#6B6385",
    "mint": "#2F7D66", "mint2": "#3FA184", "mint3": "#DDF3EA",
    "mint_strong": "#256B57", "mint_hover": "#2A735E",
    "cyan": "#2F7F9E", "cyan_text": "#2A6E8C",
    "violet": "#6A4FBF", "violet2": "#8A6FE0", "violet3": "#EEE8FF",
    "violet_text": "#5B3FA8",
    "pink": "#B93C7E", "pink2": "#D967A6", "pink3": "#FBE7F3", "pink_text": "#A83270",
    "amber": "#9A6A16", "amber_bg": "#FDF4E1",
    "red": "#B94F47", "red_bg": "#FBEAE8",
    "focus": "#2F7D66", "white": "#FFFFFF",
}

#: Tk 派生值（Tk 没有 alpha：rgba 必须落成合成后的实色）
DERIVED = {
    # --line = rgba(60,40,110,.14) 合成到页面底
    "line": "#DBD5E6",
    # --line-2 = rgba(60,40,110,.24) 合成到页面底
    "line2": "#C9C1D8",
    # --line-3 = rgba(60,40,110,.55) 合成到页面底（白卡 3.52:1 / 页底 3.16:1）
    "line3": "#8F82AD",
}

#: 文字专用派生值（原始 token 只做填充/状态点/进度条）
TEXT_OVERRIDES = {
    "MUTED_TEXT": "#5B5375",
    "MINT_TEXT": "#256B57",
    "AMBER_TEXT": "#8A5D12",
    "RED_TEXT": "#A84540",
    "INFO_TEXT": "#2A6E8C",
    "CYAN_TEXT": "#2A6E8C",
    "VIOLET_TEXT": "#5B3FA8",
    "PINK_TEXT": "#A83270",
}

#: 实心填充上的字
ON_FILLS = {"ON_VIOLET": "#FFFFFF", "ON_PINK": "#FFFFFF"}

#: 新加入的紫/粉具名常量（task C1 要求）
NEW_NAMED = ("VIOLET", "VIOLET2", "VIOLET3", "VIOLET_TEXT", "PINK", "PINK2", "PINK3",
             "PINK_TEXT", "ON_VIOLET", "ON_PINK", "MINT_STRONG", "MINT_HOVER", "CYAN_TEXT")


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


def _hue(color: str) -> float:
    """HSL 色相角（0–360）；用来证明「粉」不是「红/琥珀/玉绿」的近似色。"""
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


def _state_pairs(style, name: str):
    """按钮每个状态的前景/背景对（禁用态 WCAG 豁免，不计入）。"""
    base_background = style.configured[name]["background"]
    base_foreground = style.configured[name]["foreground"]
    backgrounds = dict(style.mapped.get(name, {}).get("background", []))
    foregrounds = dict(style.mapped.get(name, {}).get("foreground", []))
    pairs = [("base", base_foreground, base_background)]
    for state, background in backgrounds.items():
        if state == "disabled":
            continue
        pairs.append((state, foregrounds.get(state, base_foreground), background))
    return pairs


# ────────────────────────────────────────────────────────────── token 取值
def test_tokens_match_baseline():
    for name, value in BASELINE.items():
        assert theme.TOKENS[name] == value, name


def test_tk_derived_tokens_match():
    for name, value in DERIVED.items():
        assert theme.TOKENS[name] == value, name


def test_named_text_constants_match():
    for name, value in TEXT_OVERRIDES.items():
        assert getattr(theme, name) == value, name
    for name, value in ON_FILLS.items():
        assert getattr(theme, name) == value, name


def test_named_purple_pink_constants_are_exposed():
    for name in NEW_NAMED:
        assert isinstance(getattr(theme, name), str), name
    assert theme.VIOLET == theme.TOKENS["violet"]
    assert theme.VIOLET2 == theme.TOKENS["violet2"]
    assert theme.VIOLET3 == theme.TOKENS["violet3"]
    assert theme.VIOLET_TEXT == theme.TOKENS["violet_text"]
    assert theme.PINK == theme.TOKENS["pink"]
    assert theme.PINK_TEXT == theme.TOKENS["pink_text"]
    assert theme.BORDER == theme.TOKENS["line3"]      # 控件边界只有一个来源


def test_every_colour_is_six_digit_hex():
    for name, value in theme.TOKENS.items():
        assert len(value) == 7 and value.startswith("#"), name
        int(value[1:], 16)                                       # 非法 hex 直接抛 ValueError


def test_every_token_is_pinned_to_a_documented_value():
    """取值全量钉死：色板里不许有「没登记期望值」的键。

    旧薄荷/旧青柠/旧紫要回来，必须同时改 :data:`BASELINE` 或 :data:`DERIVED`——
    这两个表就是回归网，所以这里不再单独维护一张「退役色值」清单（那张表本身会把
    旧十六进制写回实现仓，验收的残留 grep 会命中）。
    """
    assert set(theme.TOKENS) == set(BASELINE) | set(DERIVED)


# ────────────────────────────────────────────────────────────── 对比度（阈值不放宽）
@pytest.mark.parametrize("name", ["text", "text2", "muted", "cyan", "danger", "ok", "wait"])
def test_text_tokens_readable_on_page_and_card(name):
    for background in ("bg", "panel", "input"):
        ratio = theme.contrast_ratio(theme.COLORS[name], theme.COLORS[background])
        assert ratio >= 4.5, (name, background, round(ratio, 2))


@pytest.mark.parametrize("name", sorted(TEXT_OVERRIDES))
def test_named_text_variants_readable_on_every_surface(name):
    """紫/粉文字也按正文标准：白卡、输入底、页底三处都 ≥4.5:1。"""
    for background in ("surface", "surface2", "bg"):
        ratio = theme.contrast_ratio(getattr(theme, name), theme.TOKENS[background])
        assert ratio >= 4.5, (name, background, round(ratio, 2))


def test_control_boundary_is_visible():
    """控件描边 ≥3:1（WCAG 1.4.11）；line3 就是边界色，四处底色都要过。"""
    assert theme.contrast_ratio(theme.BORDER, theme.TOKENS["surface"]) >= 3
    assert theme.contrast_ratio(theme.BORDER, theme.COLORS["input"]) >= 3
    for background in ("surface", "surface2", "surface3", "bg"):
        ratio = theme.contrast_ratio(theme.TOKENS["line3"], theme.TOKENS[background])
        assert ratio >= 3, (background, round(ratio, 2))


def test_pills_readable():
    pairs = ((theme.TOKENS["mint3"], theme.MINT_TEXT),
             (theme.TOKENS["amber_bg"], theme.AMBER_TEXT),
             (theme.TOKENS["red_bg"], theme.RED_TEXT),
             (theme.TOKENS["bg2"], theme.MUTED_TEXT),
             (theme.TOKENS["violet3"], theme.VIOLET_TEXT),
             (theme.TOKENS["pink3"], theme.PINK_TEXT))
    for background, foreground in pairs:
        assert theme.contrast_ratio(foreground, background) >= 4.5, (background, foreground)


def test_solid_fills_carry_readable_labels():
    """实心主色/紫/粉上的字：按钮字按 4.5:1（不是非文字的 3:1）。"""
    assert theme.contrast_ratio("#FFFFFF", theme.TOKENS["mint"]) >= 3        # 非文字/大字号
    for foreground, background in ((theme.TOKENS["white"], theme.TOKENS["mint"]),
                                   (theme.ON_VIOLET, theme.TOKENS["violet"]),
                                   (theme.ON_PINK, theme.TOKENS["pink"])):
        ratio = theme.contrast_ratio(foreground, background)
        assert ratio >= 4.5, (foreground, background, round(ratio, 2))


@pytest.mark.parametrize("status", ["red", "amber", "mint"])
def test_pink_is_not_a_status_colour(status):
    """粉只做装饰：与失败/等待/完成三色的色相至少差 25°，且不共用取值。"""
    status_colour = theme.TOKENS[status]
    for pink in (theme.PINK, theme.PINK_TEXT, theme.PINK2):
        assert pink != status_colour
        assert _hue_gap(pink, status_colour) >= 25, (pink, status_colour,
                                                     round(_hue_gap(pink, status_colour), 1))


def test_status_tokens_keep_their_semantics():
    """成功=玉绿、等待=琥珀、失败=珊瑚：三条都不能被紫/粉顶替。"""
    assert theme.COLORS["ok"] == theme.MINT_TEXT
    assert theme.COLORS["wait"] == theme.AMBER_TEXT
    assert theme.COLORS["danger"] == theme.RED_TEXT
    for accent in (theme.VIOLET, theme.PINK):
        assert accent not in (theme.MINT_TEXT, theme.AMBER_TEXT, theme.RED_TEXT)


# ────────────────────────────────────────────────────────────── ttk 样式
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


def test_apply_theme_wires_violet_and_pink():
    """紫/粉落到具体样式上：标题紫、eyebrow 粉、选中紫底、hover 转紫。"""
    root, ttk = FakeRoot(), FakeTtk()
    style = theme.apply_theme(root, ttk)
    assert style.configured["H1.TLabel"]["foreground"] == theme.VIOLET_TEXT
    assert style.configured["TopH2.TLabel"]["foreground"] == theme.VIOLET_TEXT
    assert style.configured["Accent.TLabel"]["foreground"] == theme.VIOLET_TEXT
    assert style.configured["Eyebrow.TLabel"]["foreground"] == theme.PINK_TEXT
    assert style.configured["PillViolet.TLabel"]["background"] == theme.TOKENS["violet3"]
    assert style.configured["PillViolet.TLabel"]["foreground"] == theme.VIOLET_TEXT
    assert style.configured["PillPink.TLabel"]["background"] == theme.TOKENS["pink3"]
    assert style.configured["PillPink.TLabel"]["foreground"] == theme.PINK_TEXT
    assert style.configured["NavOn.TButton"]["background"] == theme.TOKENS["violet3"]
    assert style.configured["NavOn.TButton"]["foreground"] == theme.MINT_TEXT
    assert dict(style.mapped["Treeview"]["background"])["selected"] == theme.TOKENS["violet3"]
    assert dict(style.mapped["TButton"]["foreground"])["active"] == theme.VIOLET_TEXT
    # 文本选中用粉底（--selection），不是紫色也不是状态色
    assert style.configured["."]["selectbackground"] == theme.TOKENS["pink3"]
    assert style.configured["TCombobox"]["selectbackground"] == theme.TOKENS["pink3"]
    assert root.options["*TCombobox*Listbox.selectBackground"] == theme.TOKENS["pink3"]
    # 粉色不进状态位：状态胶囊仍是玉绿/琥珀/珊瑚
    assert style.configured["Pill.TLabel"]["foreground"] == theme.MINT_TEXT
    assert style.configured["PillWait.TLabel"]["foreground"] == theme.AMBER_TEXT
    assert style.configured["PillFail.TLabel"]["foreground"] == theme.RED_TEXT


@pytest.mark.parametrize("name", ["TButton", "Primary.TButton", "Violet.TButton"])
def test_filled_button_labels_stay_readable_in_every_state(name):
    """按钮字对底 ≥4.5:1：base / active / pressed 全查，禁用态豁免。"""
    root, ttk = FakeRoot(), FakeTtk()
    style = theme.apply_theme(root, ttk)
    assert name in style.configured, name
    for state, foreground, background in _state_pairs(style, name):
        ratio = theme.contrast_ratio(foreground, background)
        assert ratio >= 4.5, (name, state, foreground, background, round(ratio, 2))


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
                 "PillFail.TLabel", "PillMuted.TLabel", "PillViolet.TLabel",
                 "PillPink.TLabel", "Eyebrow.TLabel", "Accent.TLabel",
                 "CardAccent.TLabel", "Primary.TButton", "Violet.TButton",
                 "Ghost.TButton", "Nav.TButton", "NavOn.TButton", "TopH2.TLabel",
                 "TopH3.TLabel", "TopMeta.TLabel", "Card.TFrame", "CardAlt.TFrame",
                 "Sidebar.TFrame", "Topbar.TFrame"):
        assert name in style.configured, name


def test_menu_options_are_light():
    options = theme.menu_options()
    assert options["background"] == theme.TOKENS["surface"]
    assert options["foreground"] == theme.TOKENS["ink"]
    assert options["activebackground"] == theme.TOKENS["violet3"]
    assert options["activeforeground"] == theme.VIOLET_TEXT


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
                "wait", "danger", "ok", "line", "line2", "mint", "mint2", "mint3",
                "amber", "red", "red_text", "focus", "white", "violet_text", "pink",
                "pink_text"):
        assert key in theme.COLORS, key


# ── 偏好落盘（功能测试 F2 的回归：换肤能存住、坏文件不拖垮启动）
def test_theme_pref_roundtrip(tmp_path) -> None:
    from djb_node.desktop import theme as desktop_theme

    desktop_theme.save("night", tmp_path)
    assert desktop_theme.load(tmp_path) == "night"
    mode = (tmp_path / desktop_theme.PREFS_FILE).stat().st_mode & 0o777
    assert mode == 0o600, f"偏好文件权限 {oct(mode)}，应 0600"


def test_theme_pref_corrupt_or_unknown_falls_back(tmp_path) -> None:
    from djb_node.desktop import theme as desktop_theme

    path = tmp_path / desktop_theme.PREFS_FILE
    path.write_text("{not json", encoding="utf-8")
    assert desktop_theme.load(tmp_path) == desktop_theme.DEFAULT_THEME
    path.write_text('{"theme": "neon"}', encoding="utf-8")
    assert desktop_theme.load(tmp_path) == desktop_theme.DEFAULT_THEME


def test_theme_save_rejects_unknown(tmp_path) -> None:
    from djb_node.desktop import theme as desktop_theme

    with pytest.raises(ValueError):
        desktop_theme.save("neon", tmp_path)
