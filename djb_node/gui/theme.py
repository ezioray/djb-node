# SPDX-License-Identifier: Apache-2.0
"""DJB Aurora Signal 浅色 token + ttk 样式（静态；本模块**不 import tkinter**）。

唯一取值来源是 :data:`TOKENS`，与 `design/15-DJB-Aurora-Signal-配色修订-v1.md` §1.1 的
**day（藕粉晨光）** 列逐项对齐；夜间列在 :mod:`djb_node.desktop.theme` 的
``PALETTES["night"]``——两套主题共用同一组 token 名，只换值。

Tk 没有 alpha 合成（CSS 的 ``rgba()`` 在这里必须落成实色），派生规则固定：

* **分隔线** ``line`` / ``line2``：把 §1.1 的 rgba 合成到**页面底** ``TOKENS["bg"]``；
* **控件边界** ``line3``：§1.1 的 ``--line-3`` 是 ``rgba(60,40,110,.55)``（≥3:1 口径），
  这里取它合成到页面底的实色 :data:`BORDER`——白卡 3.52:1 / 输入底 3.32:1 /
  三级面 3.07:1 / 页面底 3.16:1，四处都过 3:1；
* **文字**：每个色相单独给 ``*_TEXT``（≥4.5:1）；实心填充上的字单独给 ``ON_*``。

命名常量与 token 的对照（``*_TEXT`` / ``BORDER`` / ``ON_*`` 是 Tk 派生，不是 §1.1 的 token 名）：

================  ==============================  ====================================
常量              取值                            用途
================  ==============================  ====================================
``MUTED_TEXT``    ``#5B5375``                     说明/元信息（比 ``muted`` 再深一档）
``BORDER``        ``TOKENS["line3"]``             输入框/表格/控件描边
``MINT_TEXT``     ``#256B57``                     成功、链接、主色文字
``MINT_STRONG``   ``TOKENS["mint_strong"]``       主按钮按下 / 实心强调
``MINT_HOVER``    ``TOKENS["mint_hover"]``        主按钮 hover
``AMBER_TEXT``    ``#8A5D12``                     等待/同步文字
``RED_TEXT``      ``#A84540``                     真失败文字
``INFO_TEXT``     ``TOKENS["cyan_text"]``         信息/开发者辅助文字
``VIOLET``        ``TOKENS["violet"]``            第二强调：导航/链接 hover/选中
``VIOLET2``       ``TOKENS["violet2"]``           紫色浅色/描边
``VIOLET3``       ``TOKENS["violet3"]``           紫色底（选中底/紫胶囊底）
``VIOLET_TEXT``   ``TOKENS["violet_text"]``       紫色文字专用（≥4.5:1）
``PINK``          ``TOKENS["pink"]``              第三强调：eyebrow/装饰徽标/图表
``PINK2``         ``TOKENS["pink2"]``             粉色浅色/描边
``PINK3``         ``TOKENS["pink3"]``             粉色底（文本选中/粉胶囊底）
``PINK_TEXT``     ``TOKENS["pink_text"]``         粉色文字专用（≥4.5:1）
``ON_VIOLET``     ``#FFFFFF``                     压紫实心上的字
``ON_PINK``       ``#FFFFFF``                     压粉实心上的字
================  ==============================  ====================================

粉色**只做非状态强调**：不得表达成功/等待/失败/危险（:func:`apply_theme` 里没有粉色按钮，
粉底只出现在 eyebrow 与装饰胶囊上）。状态语义仍是 玉绿=完成 / 琥珀=等待 / 珊瑚=失败。

兼容层
======
历史代码（:mod:`djb_node.gui.window`、:mod:`djb_node.tray.app`、
:mod:`djb_node.tray.panels`）与 `tests/test_cyberpunk_ui.py` 读的是旧键名
（``bg`` / ``panel`` / ``input`` / ``text`` / ``muted`` / ``border`` / ``cyan`` /
``violet`` / ``wait`` / ``danger`` / ``ok``），且断言「文字对 ``panel`` ≥4.5:1、
``border`` 对 ``input`` ≥3:1」。键名全部保留，值换成 Aurora Signal 新口径：

============  =========================  ====================================
旧键名        现取值                      说明
============  =========================  ====================================
``bg``        ``TOKENS["bg"]``            页面底
``panel``     ``TOKENS["surface"]``       卡片
``input``     ``TOKENS["surface2"]``      输入底
``text``      ``TOKENS["ink"]``           正文
``muted``     :data:`MUTED_TEXT`          说明文字
``border``    :data:`BORDER`              控件边界
``cyan``      :data:`INFO_TEXT`           信息/开发者文字
``violet``    ``TOKENS["violet"]``        第二强调
``wait``      :data:`AMBER_TEXT`          等待文字
``danger``    ``TOKENS["red"]``           真失败
``ok``        :data:`MINT_TEXT`           完成/成功
============  =========================  ====================================

所有对比度数字由 :func:`contrast_ratio` 现算，`tests/test_desktop_theme.py` 与
`tests/test_desktop_dual_theme.py` 用同一函数复算，不手抄、不放宽阈值。
"""
from __future__ import annotations

__all__ = [
    "AMBER_TEXT", "BORDER", "COLORS", "CONTROL_HEIGHT", "CYAN_TEXT", "FONT",
    "FONT_FAMILIES", "INFO_TEXT", "MINT_HOVER", "MINT_STRONG", "MINT_TEXT", "MONO",
    "MONO_FAMILIES", "MUTED_TEXT", "ON_PINK", "ON_VIOLET", "PINK", "PINK2", "PINK3",
    "PINK_TEXT", "RADIUS", "RED_TEXT", "ROW_HEIGHT", "SPACE", "TOKENS", "TYPE_SCALE",
    "VIOLET", "VIOLET2", "VIOLET3", "VIOLET_TEXT", "apply_theme", "contrast_ratio",
    "luminance", "menu_options", "pick_family", "token",
]

#: 设计 token（唯一取值；禁止页面自定义十六进制）
TOKENS: dict[str, str] = {
    # ── 底 / 面
    "bg": "#F5F1FA",
    "bg2": "#EDE7F6",
    "surface": "#FFFFFF",
    "surface2": "#FAF7FE",
    "surface3": "#F3EDFA",
    # ── 正文
    "ink": "#1B1530",
    "ink2": "#3C3457",
    "muted": "#6B6385",
    # ── 分隔 / 控件边界（line3 见模块 docstring 的合成口径）
    "line": "#DBD5E6",
    "line2": "#C9C1D8",
    "line3": "#8F82AD",
    # ── 玉绿主色（温和玉，不再是荧光薄荷）
    "mint": "#2F7D66",
    "mint2": "#3FA184",
    "mint3": "#DDF3EA",
    "mint_strong": "#256B57",
    "mint_hover": "#2A735E",
    # ── 信息 / 开发者辅助
    "cyan": "#2F7F9E",
    "cyan_text": "#2A6E8C",
    # ── 紫（第二强调）
    "violet": "#6A4FBF",
    "violet2": "#8A6FE0",
    "violet3": "#EEE8FF",
    "violet_text": "#5B3FA8",
    # ── 粉（第三强调；不承担状态语义）
    "pink": "#B93C7E",
    "pink2": "#D967A6",
    "pink3": "#FBE7F3",
    "pink_text": "#A83270",
    # ── 状态（语义固定：琥珀等待 / 珊瑚失败）
    "amber": "#9A6A16",
    "amber_bg": "#FDF4E1",
    "red": "#B94F47",
    "red_bg": "#FBEAE8",
    # ── 焦点环 / 实心主色上的字
    "focus": "#2F7D66",
    "white": "#FFFFFF",
}

#: 说明/元信息文字（``muted`` 自身白卡 5.60:1，这里再深一档给 9px 小字留余量）
MUTED_TEXT = "#5B5375"
#: 控件边界 = ``line3``（白卡 3.52:1 / 输入底 3.32:1 / 三级面 3.07:1 / 页底 3.16:1）
BORDER = TOKENS["line3"]
#: 主色文字（白卡 6.33:1 / 页底 5.68:1）
MINT_TEXT = "#256B57"
#: 主按钮按下态底色（白字 6.33:1）
MINT_STRONG = TOKENS["mint_strong"]
#: 主按钮 hover 底色（白字 5.65:1）
MINT_HOVER = TOKENS["mint_hover"]
#: 等待文字（白卡 5.75:1 / 页底 5.16:1）
AMBER_TEXT = "#8A5D12"
#: 信息/开发者文字（白卡 5.66:1 / 页底 5.08:1）
INFO_TEXT = TOKENS["cyan_text"]
#: ``INFO_TEXT`` 的新名字（token 名与 §1.1 对齐；旧名保留）
CYAN_TEXT = TOKENS["cyan_text"]
#: 失败文字（白卡 5.85:1 / 页底 5.25:1）
RED_TEXT = "#A84540"
#: 紫（第二强调）：白卡 6.03:1
VIOLET = TOKENS["violet"]
VIOLET2 = TOKENS["violet2"]
VIOLET3 = TOKENS["violet3"]
#: 紫色文字（白卡 7.72:1 / 页底 6.93:1）
VIOLET_TEXT = TOKENS["violet_text"]
#: 粉（第三强调；只做装饰，不做状态）：白卡 5.24:1（对白字）
PINK = TOKENS["pink"]
PINK2 = TOKENS["pink2"]
PINK3 = TOKENS["pink3"]
#: 粉色文字（白卡 6.26:1 / 页底 5.62:1）
PINK_TEXT = TOKENS["pink_text"]
#: 压紫实心上的字（紫底 6.03:1）
ON_VIOLET = "#FFFFFF"
#: 压粉实心上的字（粉底 5.24:1）
ON_PINK = "#FFFFFF"

#: 兼容旧键名；新代码不要新增旧键
COLORS: dict[str, str] = {
    "bg": TOKENS["bg"],
    "panel": TOKENS["surface"],
    "panel2": TOKENS["surface2"],
    "panel3": TOKENS["surface3"],
    "input": TOKENS["surface2"],
    "text": TOKENS["ink"],
    "text2": TOKENS["ink2"],
    "muted": MUTED_TEXT,
    "border": BORDER,
    "line": TOKENS["line"],
    "line2": TOKENS["line2"],
    "line3": TOKENS["line3"],
    "cyan": INFO_TEXT,
    "violet": TOKENS["violet"],
    "violet2": VIOLET2,
    "violet3": VIOLET3,
    "violet_text": VIOLET_TEXT,
    "pink": PINK,
    "pink2": PINK2,
    "pink3": PINK3,
    "pink_text": PINK_TEXT,
    "on_violet": ON_VIOLET,
    "on_pink": ON_PINK,
    "wait": AMBER_TEXT,
    "danger": RED_TEXT,
    "ok": MINT_TEXT,
    "mint": TOKENS["mint"],
    "mint2": TOKENS["mint2"],
    "mint3": TOKENS["mint3"],
    "mint_strong": MINT_STRONG,
    "mint_hover": MINT_HOVER,
    "amber": TOKENS["amber"],
    "red": TOKENS["red"],
    "red_text": RED_TEXT,
    "focus": TOKENS["focus"],
    "white": TOKENS["white"],
}

#: 字体族优先级（Windows / macOS / Linux 回退；不引第三方字体依赖）
FONT_FAMILIES: tuple[str, ...] = (
    "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC",
    "Noto Sans SC", "Inter", "Source Han Sans SC", "DejaVu Sans", "system-ui",
)
#: 等宽族（哈希 / seq / 命令）
MONO_FAMILIES: tuple[str, ...] = (
    "Cascadia Mono", "Consolas", "Menlo", "JetBrains Mono", "DejaVu Sans Mono",
    "Noto Sans Mono", "Courier New",
)
#: 正文族（模块级默认；apply_theme 会按可用族重算并回写）
FONT = ("Microsoft YaHei UI", 11)
MONO = ("Consolas", 11)

#: 字阶（基准表 §1）
TYPE_SCALE: dict[str, int] = {"xs": 9, "sm": 10, "md": 11, "lg": 13, "xl": 17, "display": 22}
#: 圆角（tk/ttk 无原生圆角，保留给自绘/文档一致的常量）
RADIUS = {"card": 12, "control": 8, "popover": 14, "pill": 999}
#: 间距刻度
SPACE = {"1": 4, "2": 8, "3": 12, "4": 16, "6": 24, "8": 32, "12": 48, "16": 64}
#: 表格行高 / 控件高（基准 §1：行 32–36、按钮命中区 ≥40）
ROW_HEIGHT = 34
CONTROL_HEIGHT = 40


# ─────────────────────────────────────────────────────────────── 颜色计算（纯函数）
def luminance(color: str) -> float:
    """WCAG 相对亮度；非法输入抛 ``ValueError``（不静默返回 0）。"""
    text = str(color or "").strip()
    if len(text) != 7 or not text.startswith("#"):
        raise ValueError("颜色必须是 #RRGGBB")
    try:
        channels = [int(text[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    except ValueError:
        raise ValueError("颜色必须是 #RRGGBB") from None
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
              for c in channels]
    return sum(a * b for a, b in zip(linear, (0.2126, 0.7152, 0.0722)))


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG 对比度（1.0–21.0）。"""
    first, second = luminance(foreground), luminance(background)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


def token(name: str) -> str:
    """取设计 token；未知名字抛 ``KeyError``（拼错时不许静默上默认色）。"""
    return TOKENS[name]


# ─────────────────────────────────────────────────────────────── 字体
def _available_families(root) -> frozenset[str]:
    try:
        raw = root.tk.call("font", "families")
    except Exception:                                  # 无 Tk / 受限环境：用首选族
        return frozenset()
    names = set()
    for item in raw if isinstance(raw, (list, tuple)) else ():
        names.add(str(item))
    return frozenset(names)


def pick_family(candidates: tuple[str, ...], available=frozenset()) -> str:
    """首选族里第一个系统可用的；一个都没有就回首选第一项（Tk 会自行替换）。"""
    for name in candidates:
        if name in available:
            return name
    return candidates[0]


# ─────────────────────────────────────────────────────────────── ttk 样式
def apply_theme(root, ttk, *, available_families=None):
    """clam + 浅色 map：原生 ttk 默认值不能悄悄把浅色字段变回系统灰。

    ``available_families`` 可注入（测试用），默认从 ``root`` 现查。
    紫色只进「导航当前项 / 选中 / hover / 信息徽标 / 标题」，粉色只进
    「eyebrow / 装饰胶囊」，两者都不进状态位（等待/失败/成功仍是琥珀/珊瑚/玉绿）。
    """
    global FONT, MONO
    families = (frozenset(available_families) if available_families is not None
                else _available_families(root))
    family = pick_family(FONT_FAMILIES, families)
    mono_family = pick_family(MONO_FAMILIES, families)
    FONT = (family, TYPE_SCALE["md"])
    MONO = (mono_family, TYPE_SCALE["md"])
    body, small = FONT, (family, TYPE_SCALE["sm"])
    eyebrow = (family, TYPE_SCALE["xs"], "bold")
    heading, title = (family, TYPE_SCALE["lg"], "bold"), (family, TYPE_SCALE["xl"], "bold")
    display, mono = (family, TYPE_SCALE["display"], "bold"), MONO

    root.configure(background=TOKENS["bg"])
    root.option_add("*Font", body)
    # Combobox 的下拉是 tk.Listbox（不是 ttk），只能走 option 数据库；文本选中用粉底
    root.option_add("*TCombobox*Listbox.background", TOKENS["surface"])
    root.option_add("*TCombobox*Listbox.foreground", TOKENS["ink"])
    root.option_add("*TCombobox*Listbox.selectBackground", PINK3)
    root.option_add("*TCombobox*Listbox.selectForeground", TOKENS["ink"])
    style = ttk.Style(root)
    style.theme_use("clam")
    # 文本选中用粉（§1.1 --selection），行选中用紫底（VIOLET3），两者都不承担状态语义
    style.configure(".", background=TOKENS["bg"], foreground=TOKENS["ink"],
                    font=body, bordercolor=BORDER, focuscolor=TOKENS["focus"],
                    troughcolor=TOKENS["bg2"], insertcolor=TOKENS["ink"],
                    selectbackground=PINK3, selectforeground=TOKENS["ink"],
                    fieldbackground=TOKENS["surface"], lightcolor=TOKENS["surface"],
                    darkcolor=TOKENS["line2"])
    # ── 容器
    for name, background in (("TFrame", TOKENS["bg"]), ("App.TFrame", TOKENS["bg"]),
                             ("Card.TFrame", TOKENS["surface"]),
                             ("CardAlt.TFrame", TOKENS["surface2"]),
                             ("Sidebar.TFrame", TOKENS["bg2"]),
                             ("Topbar.TFrame", TOKENS["surface"])):
        style.configure(name, background=background, relief="flat", borderwidth=0)
    # ── 文字
    style.configure("TLabel", background=TOKENS["bg"], foreground=TOKENS["ink"], font=body)
    style.configure("Body.TLabel", background=TOKENS["bg"], foreground=TOKENS["ink"])
    style.configure("Card.TLabel", background=TOKENS["surface"], foreground=TOKENS["ink"])
    style.configure("Muted.TLabel", background=TOKENS["bg"], foreground=MUTED_TEXT, font=small)
    style.configure("CardMuted.TLabel", background=TOKENS["surface"],
                    foreground=MUTED_TEXT, font=small)
    style.configure("Meta.TLabel", background=TOKENS["bg"], foreground=MUTED_TEXT, font=small)
    style.configure("Signal.TLabel", background=TOKENS["bg"], foreground=MINT_TEXT, font=mono)
    style.configure("Mono.TLabel", background=TOKENS["surface"], foreground=TOKENS["ink2"],
                    font=mono)
    # 一级标题用紫（第二强调），二三级仍用正文色保证信息层级不被强调色抢走
    style.configure("H1.TLabel", background=TOKENS["bg"], foreground=VIOLET_TEXT, font=display)
    style.configure("H2.TLabel", background=TOKENS["bg"], foreground=TOKENS["ink"], font=title)
    style.configure("H3.TLabel", background=TOKENS["surface"], foreground=TOKENS["ink"],
                    font=heading)
    style.configure("Value.TLabel", background=TOKENS["surface"], foreground=TOKENS["ink"],
                    font=title)
    # 指标卡取值按 tone 上色（颜色 + 文案双通道，不靠颜色单独表意）
    for name, color in (("ValueMint", MINT_TEXT), ("ValueWait", AMBER_TEXT),
                        ("ValueFail", RED_TEXT), ("ValueMuted", MUTED_TEXT),
                        ("ValueDefault", TOKENS["ink"])):
        style.configure(name + ".TLabel", background=TOKENS["surface"], foreground=color,
                        font=title)
    # 顶栏（白底）用的同字号变体：同一页面里底色不同必须换样式，不能只改颜色
    style.configure("TopH2.TLabel", background=TOKENS["surface"], foreground=VIOLET_TEXT,
                    font=title)
    style.configure("TopH3.TLabel", background=TOKENS["surface"], foreground=TOKENS["ink2"],
                    font=heading)
    style.configure("TopMeta.TLabel", background=TOKENS["surface"], foreground=MUTED_TEXT,
                    font=small)
    style.configure("Warning.TLabel", background=TOKENS["bg"], foreground=AMBER_TEXT)
    style.configure("Danger.TLabel", background=TOKENS["bg"], foreground=RED_TEXT)
    style.configure("Success.TLabel", background=TOKENS["bg"], foreground=MINT_TEXT)
    style.configure("Info.TLabel", background=TOKENS["bg"], foreground=INFO_TEXT)
    # 紫＝第二强调（链接/关注/信息徽标），粉＝第三强调（eyebrow/装饰），都不是状态
    style.configure("Accent.TLabel", background=TOKENS["bg"], foreground=VIOLET_TEXT)
    style.configure("CardAccent.TLabel", background=TOKENS["surface"],
                    foreground=VIOLET_TEXT)
    style.configure("Eyebrow.TLabel", background=TOKENS["bg"], foreground=PINK_TEXT,
                    font=eyebrow)
    # 胶囊（ttk 无圆角：底色 + padding 近似；文字色保证 ≥4.5:1）
    style.configure("Pill.TLabel", background=TOKENS["mint3"], foreground=MINT_TEXT,
                    font=small, padding=(10, 4))
    style.configure("PillWait.TLabel", background=TOKENS["amber_bg"], foreground=AMBER_TEXT,
                    font=small, padding=(10, 4))
    style.configure("PillFail.TLabel", background=TOKENS["red_bg"], foreground=RED_TEXT,
                    font=small, padding=(10, 4))
    style.configure("PillMuted.TLabel", background=TOKENS["bg2"], foreground=MUTED_TEXT,
                    font=small, padding=(10, 4))
    style.configure("PillViolet.TLabel", background=VIOLET3, foreground=VIOLET_TEXT,
                    font=small, padding=(10, 4))
    style.configure("PillPink.TLabel", background=PINK3, foreground=PINK_TEXT,
                    font=small, padding=(10, 4))
    # ── 按钮（命中区 ≥40）
    style.configure("TButton", padding=(16, 10), focusthickness=2, font=body,
                    background=TOKENS["surface"], foreground=TOKENS["ink"],
                    bordercolor=BORDER, lightcolor=TOKENS["surface"],
                    darkcolor=TOKENS["surface"])
    style.map("TButton",
              background=[("disabled", TOKENS["bg2"]), ("pressed", VIOLET3),
                          ("active", TOKENS["surface2"])],
              foreground=[("disabled", MUTED_TEXT), ("active", VIOLET_TEXT)],
              # hover 转紫（§2.1-2）；focus 优先，否则回落到控件边界色
              bordercolor=[("focus", TOKENS["focus"]), ("active", VIOLET_TEXT),
                           ("!focus", BORDER)])
    style.configure("Primary.TButton", padding=(16, 10), font=body,
                    background=TOKENS["mint"], foreground=TOKENS["white"],
                    bordercolor=TOKENS["mint"], lightcolor=TOKENS["mint"],
                    darkcolor=TOKENS["mint"])
    style.map("Primary.TButton",
              # hover/pressed 都往深里走：按钮字对底仍 ≥4.5:1（不许用更亮的 mint2 压白字）
              background=[("disabled", TOKENS["bg2"]), ("pressed", MINT_STRONG),
                          ("active", MINT_HOVER)],
              foreground=[("disabled", MUTED_TEXT), ("active", TOKENS["white"])],
              # clam 的边框/3D 边由 bordercolor/lightcolor/darkcolor 画，禁用态必须一起变色，
              # 否则会留下主色描边，看起来「可点」。
              bordercolor=[("disabled", TOKENS["line2"]), ("focus", TOKENS["focus"])],
              lightcolor=[("disabled", TOKENS["line2"]), ("active", MINT_HOVER)],
              darkcolor=[("disabled", TOKENS["line2"]), ("active", MINT_HOVER)])
    style.configure("Violet.TButton", padding=(16, 10), font=body,
                    background=VIOLET, foreground=ON_VIOLET,
                    bordercolor=VIOLET, lightcolor=VIOLET, darkcolor=VIOLET)
    style.map("Violet.TButton",
              background=[("disabled", TOKENS["bg2"]), ("pressed", VIOLET_TEXT),
                          ("active", VIOLET_TEXT)],
              foreground=[("disabled", MUTED_TEXT), ("active", ON_VIOLET)],
              bordercolor=[("disabled", TOKENS["line2"]), ("focus", TOKENS["focus"])],
              lightcolor=[("disabled", TOKENS["line2"]), ("active", VIOLET_TEXT)],
              darkcolor=[("disabled", TOKENS["line2"]), ("active", VIOLET_TEXT)])
    style.configure("Ghost.TButton", padding=(12, 8), font=small,
                    background=TOKENS["bg"], foreground=MINT_TEXT, bordercolor=TOKENS["bg"])
    style.map("Ghost.TButton",
              background=[("active", VIOLET3)],
              foreground=[("disabled", MUTED_TEXT), ("active", VIOLET_TEXT)])
    # 侧栏导航：当前项＝玉绿字 + 紫底（§2.1-1/3）
    for name, background, foreground in (("Nav.TButton", TOKENS["bg2"], TOKENS["ink2"]),
                                         ("NavOn.TButton", VIOLET3, MINT_TEXT)):
        style.configure(name, padding=(14, 11), font=body, anchor="w",
                        background=background, foreground=foreground,
                        bordercolor=background, lightcolor=background, darkcolor=background)
        style.map(name, background=[("active", VIOLET3)],
                  foreground=[("active", MINT_TEXT)],
                  bordercolor=[("focus", TOKENS["focus"])])
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, padding=(8, 8), focusthickness=2, font=body,
                        background=TOKENS["surface"], foreground=TOKENS["ink"],
                        indicatorbackground=TOKENS["surface"],
                        indicatorforeground=MINT_TEXT,
                        upperbordercolor=BORDER, lowerbordercolor=BORDER)
        style.map(name,
                  background=[("active", TOKENS["surface"])],
                  foreground=[("disabled", MUTED_TEXT), ("active", TOKENS["ink"])],
                  indicatorbackground=[("selected", TOKENS["mint"]),
                                       ("disabled", TOKENS["bg2"])])
    # ── 输入
    for name in ("TEntry", "TSpinbox", "TCombobox"):
        style.configure(name, padding=(8, 8), font=body,
                        fieldbackground=TOKENS["surface2"], foreground=TOKENS["ink"],
                        background=TOKENS["surface2"], bordercolor=BORDER,
                        arrowcolor=TOKENS["ink2"], insertcolor=TOKENS["ink"])
        style.map(name,
                  fieldbackground=[("disabled", TOKENS["bg2"]), ("readonly", TOKENS["bg2"])],
                  foreground=[("disabled", MUTED_TEXT), ("readonly", TOKENS["ink2"])],
                  bordercolor=[("focus", TOKENS["focus"]), ("!focus", BORDER)])
    style.configure("TCombobox", selectbackground=PINK3,
                    selectforeground=TOKENS["ink"])
    # ── 表格 / 滚动 / 分隔 / 进度
    style.configure("Treeview", background=TOKENS["surface"],
                    fieldbackground=TOKENS["surface"], foreground=TOKENS["ink"],
                    font=body, rowheight=ROW_HEIGHT, bordercolor=TOKENS["line"],
                    lightcolor=TOKENS["surface"], darkcolor=TOKENS["surface"])
    style.map("Treeview",
              background=[("selected", VIOLET3)],
              foreground=[("selected", TOKENS["ink"])])
    style.configure("Treeview.Heading", background=TOKENS["surface2"],
                    foreground=TOKENS["ink2"], font=small, relief="flat",
                    padding=(8, 8), bordercolor=TOKENS["line"])
    style.map("Treeview.Heading", background=[("active", TOKENS["bg2"])],
              foreground=[("active", TOKENS["ink"])])
    style.configure("TScrollbar", background=TOKENS["line2"], troughcolor=TOKENS["bg2"],
                    bordercolor=TOKENS["bg2"], arrowcolor=TOKENS["ink2"])
    style.map("TScrollbar", background=[("active", BORDER)])
    style.configure("TSeparator", background=TOKENS["line"], bordercolor=TOKENS["line"])
    style.configure("Horizontal.TProgressbar", background=TOKENS["mint2"],
                    troughcolor=TOKENS["bg2"], bordercolor=TOKENS["bg2"],
                    lightcolor=TOKENS["mint2"], darkcolor=TOKENS["mint2"])
    # ── 外壳（兼容旧的 Notebook 用法）
    style.configure("TNotebook", background=TOKENS["bg"], tabmargins=(0, 8, 0, 0),
                    bordercolor=TOKENS["line"])
    style.configure("TNotebook.Tab", padding=(12, 10), font=body,
                    background=TOKENS["bg2"], foreground=TOKENS["ink2"],
                    lightcolor=TOKENS["bg2"], bordercolor=TOKENS["line"])
    style.map("TNotebook.Tab",
              background=[("selected", TOKENS["surface"])],
              foreground=[("selected", MINT_TEXT)],
              lightcolor=[("selected", TOKENS["surface"])])
    style.configure("TLabelframe", background=TOKENS["surface"], bordercolor=TOKENS["line"],
                    foreground=TOKENS["ink"], lightcolor=TOKENS["surface"])
    style.configure("TLabelframe.Label", background=TOKENS["surface"],
                    foreground=TOKENS["ink2"], font=small)
    return style


def menu_options() -> dict:
    """``tk.Menu`` 的浅色参数（托盘右键菜单用；选中项走紫底）。"""
    return dict(background=TOKENS["surface"], foreground=TOKENS["ink"],
                activebackground=VIOLET3, activeforeground=VIOLET_TEXT,
                disabledforeground=MUTED_TEXT, borderwidth=1, relief="solid",
                font=FONT)
