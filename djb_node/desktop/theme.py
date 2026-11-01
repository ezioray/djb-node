# SPDX-License-Identifier: Apache-2.0
"""桌面端双主题（Aurora Signal · Day / Night）：两套色板 + 偏好持久化 + 轻量动效助手。

取值与 web 的 ``theme.css`` 同源，口径见
`design/15-DJB-Aurora-Signal-配色修订-v1.md` §1.1（night = 紫墨夜色 / day = 藕粉晨光）。
**两套主题共用同一组 token 名**，差异只在明度与光晕强度，
不允许一套有动效、另一套没有。

本模块相对 web 的三处差异（都是 Tk 的限制，不是另立口径）
==========================================================
1. **没有 alpha**：§1.1 里 ``rgba(...)`` 的 token（``line`` / ``line2`` / ``line3`` /
   ``glow*`` 与夜间的 ``*_3`` 底色）在这里落成**合成到某一层底色后的实色**——
   分隔线合成到 ``bg``，卡片内底色合成到 ``surface``，合成结果逐个写在下面色板里。
2. **文字单独给值**：``mint_text`` / ``violet_text`` / ``pink_text`` / ``amber_text`` /
   ``red_text`` / ``cyan_text`` / ``muted_text`` 是「文字专用」派生值（≥4.5:1），
   原始 token 只做填充、状态点、进度条。
3. **实心填充上的字**：``on_mint`` / ``on_violet`` / ``on_pink``；夜间主色/紫色/粉色都偏亮，
   所以夜间压在上面的字是深色，日间才是白字。

新增 Tk 派生键（task C1）：``violet2`` / ``violet3`` / ``violet_text`` / ``pink`` /
``pink2`` / ``pink3`` / ``pink_text`` / ``on_violet`` / ``on_pink``，另有
``surface3`` / ``line3`` / ``mint_strong`` / ``mint_hover`` / ``cyan_text``。
旧键名（``bg`` / ``mint`` / ``lime`` / ``glow`` / ``border`` / ``white`` 等）全部保留，
只换值，谁也不许在页面里写十六进制。

状态语义不变：完成=玉绿、等待=琥珀、真失败=珊瑚；**粉色不承担任何状态语义**。

关于 ``gui.theme``
==================
桌面端历史上复用 :func:`djb_node.gui.theme.apply_theme`（静态浅色，托盘/gui 也用它）。
这里不改那个模块的默认值，而是在**调用期间**把它的模块级 token 与具名常量换成当前主题
（调用完立刻还原），得到「同一套样式结构 + 另一套颜色」；托盘/其它调用方读到的仍是浅色基准。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_THEME", "GUI_CONSTANTS", "PALETTES", "PREFS_FILE", "THEME_LABELS", "apply",
    "available", "blend", "color", "current", "gui_tokens", "load", "mix_step", "palette",
    "prefs_path", "save", "set_active", "toggle",
]

#: 没有偏好文件时的主题（白天，与既有验收截图一致）
DEFAULT_THEME = "day"

#: 偏好文件名（落在 ``$DJB_HOME`` 下；只有主题名，没有任何凭据）
PREFS_FILE = "desktop-prefs.json"

#: 顶栏切换控件上的文案
THEME_LABELS = {"day": "日间", "night": "夜间"}

#: 两套色板：token 名与 web ``theme.css`` 对齐。
#: ``*_text`` / ``on_*`` / ``border`` 是为 **Tk 对比度**单独定的派生值（Tk 没有 alpha 合成，
#: 也不能像 CSS 那样用半透明 ``rgba()``；数值由 ``tests/test_desktop_dual_theme.py`` 复核）。
PALETTES: dict[str, dict[str, str]] = {
    "day": {
        # ── web token（§1.1 day 列：藕粉晨光）
        "bg": "#F5F1FA",
        "bg2": "#EDE7F6",
        "surface": "#FFFFFF",
        "surface2": "#FAF7FE",
        "surface3": "#F3EDFA",
        "ink": "#1B1530",
        "ink2": "#3C3457",
        "muted": "#6B6385",
        "mint": "#2F7D66",
        "mint2": "#3FA184",
        "mint3": "#DDF3EA",
        "mint_strong": "#256B57",
        "mint_hover": "#2A735E",
        "lime": "#5F7F3A",
        "cyan": "#2F7F9E",
        "cyan_text": "#2A6E8C",
        "violet": "#6A4FBF",
        "violet2": "#8A6FE0",
        "violet3": "#EEE8FF",
        "violet_text": "#5B3FA8",
        "pink": "#B93C7E",
        "pink2": "#D967A6",
        "pink3": "#FBE7F3",
        "pink_text": "#A83270",
        "amber": "#9A6A16",
        "amber_text": "#8A5D12",
        "amber_bg": "#FDF4E1",
        "red": "#B94F47",
        "red_text": "#A84540",
        "red_bg": "#FBEAE8",
        # 分隔线：rgba 合成到 page ground（bg）
        "line": "#DBD5E6",
        "line2": "#C9C1D8",
        # 控件边界：--line-3 = rgba(60,40,110,.55) 合成到 page ground，白卡 3.52:1 / 页底 3.16:1
        "line3": "#8F82AD",
        # 光晕：合成到 page ground（Tk 不做发光，只留同色系的静态底）
        "glow": "#CDDADC",
        "glow2": "#DFD7F1",
        "glow3": "#EEDBEB",
        # ── Tk 派生（文字对比度 / 不能透明的替代）
        "muted_text": "#5B5375",
        "mint_text": "#256B57",
        "lime_text": "#4F6A2C",
        "info_text": "#2A6E8C",
        "border": "#8F82AD",
        "on_mint": "#FFFFFF",
        "on_violet": "#FFFFFF",
        "on_pink": "#FFFFFF",
        "focus": "#2F7D66",
        "white": "#FFFFFF",
    },
    "night": {
        # ── web token（§1.1 night 列：紫墨夜色）
        "bg": "#0A0915",
        "bg2": "#111020",
        "surface": "#151327",
        "surface2": "#1D1933",
        "surface3": "#241F3D",
        "ink": "#F1EDFB",
        "ink2": "#D3CCE8",
        "muted": "#A49BC0",
        "mint": "#8FD9BE",
        "mint2": "#B4EAD6",
        "mint_strong": "#C9F5E5",
        "mint_hover": "#A5E4CC",
        "lime": "#C6DD9B",
        "cyan": "#7FCBE8",
        "cyan_text": "#A5DDF1",
        "violet": "#B9A2FF",
        "violet2": "#D4C7FF",
        "violet_text": "#CDBDFF",
        "pink": "#F4A6D7",
        "pink2": "#F9C9E4",
        "pink_text": "#F8C4E2",
        "amber": "#F0C56B",
        "amber_text": "#F7DCA6",
        "red": "#FF9B93",
        "red_text": "#FFC4BF",
        # 夜间 *_3 是半透明底：合成到 surface（卡片）后的实色
        "mint3": "#262F3C",
        "violet3": "#2C2745",
        "pink3": "#32263E",
        "amber_bg": "#312A30",
        "red_bg": "#362636",
        # 分隔线：rgba 合成到 surface
        "line": "#2A2741",
        "line2": "#3C3757",
        # 控件边界：--line-3 = rgba(198,183,255,.50) 合成到 surface-3 的实色（取整），
        # 卡片 3.71:1 / 输入底 3.46:1 / 三级面 3.20:1 / 页底 4.03:1
        "line3": "#76699E",
        # 光晕：合成到 page ground
        "glow": "#324748",
        "glow2": "#3B3457",
        "glow3": "#3D2C40",
        # ── Tk 派生
        "muted_text": "#B4ACCB",
        "mint_text": "#A9E8D3",
        "lime_text": "#D2E7AE",
        "info_text": "#A5DDF1",
        "border": "#76699E",
        # 主按钮底是亮玉绿 / 紫 / 粉都偏亮 → 上面的字必须是深色（夜间不能用白字压亮底）
        "on_mint": "#08221B",
        "on_violet": "#14112B",
        "on_pink": "#2A1030",
        "focus": "#8FD9BE",
        "white": "#08221B",
    },
}

#: ``gui.theme.TOKENS`` 的键 ↔ 本模块色板键（同名为主，``white`` 历史上就是 ``on_mint``）
_GUI_TOKEN_MAP = {
    "bg": "bg", "bg2": "bg2", "surface": "surface", "surface2": "surface2",
    "surface3": "surface3", "ink": "ink", "ink2": "ink2", "muted": "muted",
    "line": "line", "line2": "line2", "line3": "line3",
    "mint": "mint", "mint2": "mint2", "mint3": "mint3",
    "mint_strong": "mint_strong", "mint_hover": "mint_hover",
    "amber": "amber", "red": "red", "cyan": "cyan", "cyan_text": "cyan_text",
    "violet": "violet", "violet2": "violet2", "violet3": "violet3",
    "violet_text": "violet_text", "pink": "pink", "pink2": "pink2", "pink3": "pink3",
    "pink_text": "pink_text", "focus": "focus", "white": "on_mint",
    "amber_bg": "amber_bg", "red_bg": "red_bg",
}

#: ``gui.theme`` 的具名颜色常量 ↔ 本模块色板键。
#: ``apply()`` 期间逐个临时替换（含新增的紫/粉常量），调用完立刻还原。
GUI_CONSTANTS: dict[str, str] = {
    "MUTED_TEXT": "muted_text",
    "BORDER": "border",
    "MINT_TEXT": "mint_text",
    "MINT_STRONG": "mint_strong",
    "MINT_HOVER": "mint_hover",
    "AMBER_TEXT": "amber_text",
    "RED_TEXT": "red_text",
    "INFO_TEXT": "info_text",
    "CYAN_TEXT": "cyan_text",
    "VIOLET": "violet",
    "VIOLET2": "violet2",
    "VIOLET3": "violet3",
    "VIOLET_TEXT": "violet_text",
    "PINK": "pink",
    "PINK2": "pink2",
    "PINK3": "pink3",
    "PINK_TEXT": "pink_text",
    "ON_VIOLET": "on_violet",
    "ON_PINK": "on_pink",
}

_ACTIVE = DEFAULT_THEME


# ────────────────────────────────────────────────────────────── 色板
def available() -> tuple[str, ...]:
    """可选主题名（顺序稳定：先 day 后 night）。"""
    return tuple(PALETTES)


def palette(name: str | None = None) -> dict[str, str]:
    """取一套色板；未知名字退回默认（不抛，界面不该因为偏好文件写坏而起不来）。"""
    return dict(PALETTES.get(name or _ACTIVE, PALETTES[DEFAULT_THEME]))


def current() -> str:
    return _ACTIVE


def set_active(name: str) -> str:
    global _ACTIVE
    if name not in PALETTES:
        name = DEFAULT_THEME
    _ACTIVE = name
    return _ACTIVE


def color(token: str, default: str = "") -> str:
    """当前主题的 token 值（渲染期取色唯一入口）。"""
    return PALETTES.get(_ACTIVE, {}).get(token) or PALETTES[DEFAULT_THEME].get(token, default)


def toggle(name: str) -> str:
    """day ↔ night。"""
    return "night" if name == "day" else "day"


def gui_tokens(name: str | None = None) -> dict[str, str]:
    """把本模块色板映射成 ``gui.theme.TOKENS`` 的键（供 apply 期间临时替换）。"""
    source = palette(name)
    return {key: source[value] for key, value in _GUI_TOKEN_MAP.items()}


def apply(root: Any, ttk: Any, name: str = DEFAULT_THEME) -> str:
    """把主题装到 ttk 上（结构/字体/间距沿用 ``gui.theme``，只换颜色）。

    做法：**临时**把 ``gui.theme`` 的模块级 token 与具名常量换成目标主题 → 调
    ``apply_theme`` → 立刻还原。这样托盘/gui 这些同样 import 该模块的调用方不受影响，
    也不会把「桌面端当前主题」泄漏成进程级全局状态。
    """
    from ..gui import theme as gui_theme

    target = name if name in PALETTES else DEFAULT_THEME
    values = palette(target)
    saved = {"TOKENS": getattr(gui_theme, "TOKENS")}
    saved.update({key: getattr(gui_theme, key) for key in GUI_CONSTANTS})
    try:
        gui_theme.TOKENS = gui_tokens(target)
        for constant, token_name in GUI_CONSTANTS.items():
            setattr(gui_theme, constant, values[token_name])
        gui_theme.apply_theme(root, ttk)
    finally:
        for key, value in saved.items():
            setattr(gui_theme, key, value)
    set_active(target)
    try:
        root.configure(background=values["bg"])
    except Exception:                              # pragma: no cover - 无窗口/已销毁
        pass
    return target


# ────────────────────────────────────────────────────────────── 动效助手（纯算术）
def _rgb(value: str) -> tuple[int, int, int]:
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def blend(start: str, end: str, ratio: float) -> str:
    """两色线性插值（``ratio`` 0→start，1→end）；非法输入原样返回 end。"""
    try:
        ratio = max(0.0, min(1.0, float(ratio)))
        a, b = _rgb(start), _rgb(end)
    except (ValueError, TypeError):
        return end
    parts = [round(a[i] + (b[i] - a[i]) * ratio) for i in range(3)]
    return "#" + "".join(f"{max(0, min(255, part)):02X}" for part in parts)


def mix_step(start: str, end: str, step: int, steps: int) -> str:
    """第 ``step`` 帧（1-based）的颜色；``steps`` 帧走完刚好到 ``end``。"""
    if steps <= 1:
        return end
    return blend(start, end, step / float(steps))


# ────────────────────────────────────────────────────────────── 偏好持久化
def prefs_path(home: str | os.PathLike[str] | None = None) -> Path:
    """``$DJB_HOME/desktop-prefs.json``（显式给 home 时用给的；不读真实 ``~/.djb`` 以外的东西）。"""
    if home:
        base = Path(home)
    else:
        from ..config import resolve_home
        base = Path(resolve_home())
    return base / PREFS_FILE


def load(home: str | os.PathLike[str] | None = None) -> str:
    """读主题偏好；文件缺失/损坏/值不认识 → 默认日间（界面照常起）。"""
    try:
        payload = json.loads(prefs_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_THEME
    name = payload.get("theme") if isinstance(payload, dict) else None
    return name if name in PALETTES else DEFAULT_THEME


def save(name: str, home: str | os.PathLike[str] | None = None) -> Path:
    """写主题偏好（0600，原子替换）。只写主题名，绝不写凭据。"""
    if name not in PALETTES:
        raise ValueError("未知主题：" + str(name))
    path = prefs_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:                          # pragma: no cover - 只读 home
        raise OSError("无法创建偏好目录：" + str(exc)) from None
    payload = json.dumps({"theme": name}, ensure_ascii=False, sort_keys=True) + "\n"
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
    finally:
        try:
            os.chmod(temporary, 0o600)
        except OSError:                             # pragma: no cover - 文件系统不支持
            pass
    os.replace(temporary, path)
    return path
