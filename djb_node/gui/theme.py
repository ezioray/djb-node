# SPDX-License-Identifier: Apache-2.0
"""DJB Daylight Signal 浅色 token + ttk 样式（静态；本模块**不 import tkinter**）。

唯一取值来源是 :data:`TOKENS`（对齐 `ops/uta/UI-BASELINE-2026-11-01.md` §1）。
页面/控件不得自定义十六进制，只能引用本模块常量或样式名。

兼容层
======
历史代码（:mod:`djb_node.gui.window`、:mod:`djb_node.tray.app`、
:mod:`djb_node.tray.panels`）与 `tests/test_cyberpunk_ui.py` 读的是旧键名
（``bg`` / ``panel`` / ``input`` / ``text`` / ``muted`` / ``border`` / ``cyan`` /
``violet`` / ``wait`` / ``danger`` / ``ok``），且断言「文字对 ``panel`` ≥4.5:1、
``border`` 对 ``input`` ≥3:1」。本模块保留这些键名，取值改成浅色语义：

============  =========================  ====================================
旧键名        浅色取值                    说明
============  =========================  ====================================
``bg``        ``TOKENS["bg"]``            页面底
``panel``     ``TOKENS["surface"]``       卡片（原暗色面板）
``input``     ``TOKENS["surface2"]``      输入底
``text``      ``TOKENS["ink"]``           正文
``muted``     :data:`MUTED_TEXT`          说明文字（见下）
``border``    :data:`BORDER`              控件边界（见下）
``cyan``      :data:`INFO_TEXT`           信息/开发者文字（见下）
``violet``    ``TOKENS["violet"]``        非状态强调
``wait``      :data:`AMBER_TEXT`          等待文字（见下）
``danger``    ``TOKENS["red"]``           真失败
``ok``        :data:`MINT_TEXT`           成功
============  =========================  ====================================

四处「取值 ≠ 基准表」是有意的、可复核的无障碍修正（基准表自己的 5:1 声明也达不到）：

* ``--muted #607A74`` 对白底只有 **4.63:1**、对 ``#EEF5F1`` 只有 4.18:1，
  低于基准表宣称的 ≥5:1；说明文字改用 :data:`MUTED_TEXT` ``#526B65``
  （白底 5.75:1 / 页底 5.20:1）。
* ``--mint #008F78`` 对白底 4.04:1，只够 WCAG 1.4.11 的 3:1（非文字/大字号），
  因此「薄荷色**文字**」用 :data:`MINT_TEXT` ``#007A66``（白底 5.28:1）；
  ``#008F78`` 仍用于填充、进度条、状态点与选中底。
* ``--amber #A9741C`` 4.04:1、``--cyan #2F7FA3`` 4.48:1、``--red #B94F47``
  4.44:1（对**页底** ``#EEF5F1``）同理：文字用 :data:`AMBER_TEXT` ``#8F6118``
  (页底 4.87:1) / :data:`INFO_TEXT` ``#2C7699``(白底 5.05:1) /
  :data:`RED_TEXT` ``#A84540``(页底 5.28:1)；原始值保留给徽章底、状态点、
  进度条等非文字用途。
* ``--line-2 #C2D6CE`` 对白底只有 1.52:1，做**交互控件描边**不达标（WCAG 1.4.11 要 3:1），
  所以控件边界用 :data:`BORDER` ``#7A948C``（对白 3.26:1、对 ``#F7FBF9`` 3.12:1），
  ``#C2D6CE`` 降级为纯分隔细线 ``TOKENS["line2"]``。

所有数字由 :func:`contrast_ratio` 现算，`tests/test_desktop_theme.py` 与
`ops/uta/evidence/wp6/palette-contrast.txt` 用同一函数复算，避免手抄。
"""
from __future__ import annotations

__all__ = [
    "AMBER_TEXT", "BORDER", "COLORS", "CONTROL_HEIGHT", "FONT", "FONT_FAMILIES",
    "INFO_TEXT", "MINT_TEXT", "MONO", "MONO_FAMILIES", "MUTED_TEXT", "RADIUS",
    "ROW_HEIGHT", "SPACE", "TOKENS", "TYPE_SCALE", "apply_theme", "contrast_ratio",
    "luminance", "menu_options", "pick_family", "token",
]

#: 设计 token（唯一取值；禁止页面自定义十六进制）
TOKENS: dict[str, str] = {
    "bg": "#EEF5F1",
    "bg2": "#E6F0EB",
    "surface": "#FFFFFF",
    "surface2": "#F7FBF9",
    "ink": "#0D2925",
    "ink2": "#29443F",
    "muted": "#607A74",
    "line": "#D6E4DE",
    "line2": "#C2D6CE",
    "mint": "#008F78",
    "mint2": "#00B89A",
    "mint3": "#D9F5EC",
    "amber": "#A9741C",
    "red": "#B94F47",
    "cyan": "#2F7FA3",
    "violet": "#6C55A3",
    "focus": "#008F78",
    "white": "#FFFFFF",
    "amber_bg": "#F6EBD8",
    "red_bg": "#F8E3E1",
}

#: 可读性修正后的文字色（见模块 docstring 的四条说明）
MUTED_TEXT = "#526B65"
BORDER = "#7A948C"
MINT_TEXT = "#007A66"
AMBER_TEXT = "#8F6118"
INFO_TEXT = "#2C7699"
RED_TEXT = "#A84540"

#: 兼容旧键名（浅色取值）；新代码不要新增旧键
COLORS: dict[str, str] = {
    "bg": TOKENS["bg"],
    "panel": TOKENS["surface"],
    "panel2": TOKENS["surface2"],
    "input": TOKENS["surface2"],
    "text": TOKENS["ink"],
    "text2": TOKENS["ink2"],
    "muted": MUTED_TEXT,
    "border": BORDER,
    "line": TOKENS["line"],
    "line2": TOKENS["line2"],
    "cyan": INFO_TEXT,
    "violet": TOKENS["violet"],
    "wait": AMBER_TEXT,
    "danger": RED_TEXT,
    "ok": MINT_TEXT,
    "mint": TOKENS["mint"],
    "mint2": TOKENS["mint2"],
    "mint3": TOKENS["mint3"],
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
    """
    global FONT, MONO
    families = (frozenset(available_families) if available_families is not None
                else _available_families(root))
    family = pick_family(FONT_FAMILIES, families)
    mono_family = pick_family(MONO_FAMILIES, families)
    FONT = (family, TYPE_SCALE["md"])
    MONO = (mono_family, TYPE_SCALE["md"])
    body, small = FONT, (family, TYPE_SCALE["sm"])
    heading, title = (family, TYPE_SCALE["lg"], "bold"), (family, TYPE_SCALE["xl"], "bold")
    display, mono = (family, TYPE_SCALE["display"], "bold"), MONO

    root.configure(background=TOKENS["bg"])
    root.option_add("*Font", body)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=TOKENS["bg"], foreground=TOKENS["ink"],
                    font=body, bordercolor=BORDER, focuscolor=TOKENS["focus"],
                    troughcolor=TOKENS["bg2"], insertcolor=TOKENS["ink"],
                    selectbackground=TOKENS["mint3"], selectforeground=TOKENS["ink"],
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
    style.configure("H1.TLabel", background=TOKENS["bg"], foreground=TOKENS["ink"], font=display)
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
    style.configure("TopH2.TLabel", background=TOKENS["surface"], foreground=TOKENS["ink"],
                    font=title)
    style.configure("TopH3.TLabel", background=TOKENS["surface"], foreground=TOKENS["ink2"],
                    font=heading)
    style.configure("TopMeta.TLabel", background=TOKENS["surface"], foreground=MUTED_TEXT,
                    font=small)
    style.configure("Warning.TLabel", background=TOKENS["bg"], foreground=AMBER_TEXT)
    style.configure("Danger.TLabel", background=TOKENS["bg"], foreground=RED_TEXT)
    style.configure("Success.TLabel", background=TOKENS["bg"], foreground=MINT_TEXT)
    style.configure("Info.TLabel", background=TOKENS["bg"], foreground=INFO_TEXT)
    # 胶囊（ttk 无圆角：底色 + padding 近似；文字色保证 ≥4.5:1）
    style.configure("Pill.TLabel", background=TOKENS["mint3"], foreground=MINT_TEXT,
                    font=small, padding=(10, 4))
    style.configure("PillWait.TLabel", background=TOKENS["amber_bg"], foreground=AMBER_TEXT,
                    font=small, padding=(10, 4))
    style.configure("PillFail.TLabel", background=TOKENS["red_bg"], foreground=RED_TEXT,
                    font=small, padding=(10, 4))
    style.configure("PillMuted.TLabel", background=TOKENS["bg2"], foreground=MUTED_TEXT,
                    font=small, padding=(10, 4))
    # ── 按钮（命中区 ≥40）
    style.configure("TButton", padding=(16, 10), focusthickness=2, font=body,
                    background=TOKENS["surface"], foreground=TOKENS["ink"],
                    bordercolor=BORDER, lightcolor=TOKENS["surface"],
                    darkcolor=TOKENS["surface"])
    style.map("TButton",
              background=[("disabled", TOKENS["bg2"]), ("pressed", TOKENS["mint3"]),
                          ("active", TOKENS["surface2"])],
              foreground=[("disabled", MUTED_TEXT), ("active", TOKENS["ink"])],
              bordercolor=[("focus", TOKENS["focus"]), ("!focus", BORDER)])
    style.configure("Primary.TButton", padding=(16, 10), font=body,
                    background=TOKENS["mint"], foreground=TOKENS["white"],
                    bordercolor=TOKENS["mint"], lightcolor=TOKENS["mint"],
                    darkcolor=TOKENS["mint"])
    style.map("Primary.TButton",
              background=[("disabled", TOKENS["bg2"]), ("pressed", MINT_TEXT),
                          ("active", TOKENS["mint2"])],
              foreground=[("disabled", MUTED_TEXT), ("active", TOKENS["white"])],
              # clam 的边框/3D 边由 bordercolor/lightcolor/darkcolor 画，禁用态必须一起变色，
              # 否则会留下主色描边，看起来「可点」。
              bordercolor=[("disabled", TOKENS["line2"]), ("focus", TOKENS["focus"])],
              lightcolor=[("disabled", TOKENS["line2"]), ("active", TOKENS["mint2"])],
              darkcolor=[("disabled", TOKENS["line2"]), ("active", TOKENS["mint2"])])
    style.configure("Ghost.TButton", padding=(12, 8), font=small,
                    background=TOKENS["bg"], foreground=MINT_TEXT, bordercolor=TOKENS["bg"])
    style.map("Ghost.TButton",
              background=[("active", TOKENS["mint3"])],
              foreground=[("disabled", MUTED_TEXT), ("active", MINT_TEXT)])
    # 侧栏导航：左对齐 + 44 行高
    for name, background, foreground in (("Nav.TButton", TOKENS["bg2"], TOKENS["ink2"]),
                                         ("NavOn.TButton", TOKENS["mint3"], MINT_TEXT)):
        style.configure(name, padding=(14, 11), font=body, anchor="w",
                        background=background, foreground=foreground,
                        bordercolor=background, lightcolor=background, darkcolor=background)
        style.map(name, background=[("active", TOKENS["mint3"])],
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
    style.configure("TCombobox", selectbackground=TOKENS["surface"],
                    selectforeground=TOKENS["ink"])
    # ── 表格 / 滚动 / 分隔 / 进度
    style.configure("Treeview", background=TOKENS["surface"],
                    fieldbackground=TOKENS["surface"], foreground=TOKENS["ink"],
                    font=body, rowheight=ROW_HEIGHT, bordercolor=TOKENS["line"],
                    lightcolor=TOKENS["surface"], darkcolor=TOKENS["surface"])
    style.map("Treeview",
              background=[("selected", TOKENS["mint3"])],
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
    """``tk.Menu`` 的浅色参数（托盘右键菜单用）。"""
    return dict(background=TOKENS["surface"], foreground=TOKENS["ink"],
                activebackground=TOKENS["mint3"], activeforeground=MINT_TEXT,
                disabledforeground=MUTED_TEXT, borderwidth=1, relief="solid",
                font=FONT)
