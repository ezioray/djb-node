# SPDX-License-Identifier: Apache-2.0
"""桌面端双主题（Day / Night 赛博朋克）：两套色板 + 偏好持久化 + 轻量动效助手。

取值与 web 的 ``theme.css`` 同源（见 ``ops/uta/DESIGN-DUAL-THEME-2026-11-01.md`` §3）：
**两套主题都用赛博朋克质感，差异只在明度与光晕强度**（alpha 差 ≤ .15），
不允许一套有动效、另一套没有。

设计约束
========
* 本模块**不 import tkinter**（纯逻辑环境也要能 import 整个包）。
* 页面/控件不得写十六进制：只能 ``color("ink")`` 这类取值。
* 夜色不是「另一套 UI」：组件结构、间距、字号、动效时长完全一致，只换颜色。

关于 ``gui.theme``
==================
桌面端历史上复用 :func:`djb_node.gui.theme.apply_theme`（静态浅色，托盘/gui 也用它）。
这里不改那个模块，而是在**调用期间**把它的模块级 token 换成当前主题（调用完立刻还原），
得到「同一套样式结构 + 另一套颜色」，托盘/其它调用方读到的仍是浅色基准。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_THEME", "PALETTES", "PREFS_FILE", "THEME_LABELS", "apply", "available",
    "blend", "color", "current", "gui_tokens", "load", "mix_step", "palette",
    "prefs_path", "save", "set_active", "toggle",
]

#: 没有偏好文件时的主题（白天，与既有验收截图一致）
DEFAULT_THEME = "day"

#: 偏好文件名（落在 ``$DJB_HOME`` 下；只有主题名，没有任何凭据）
PREFS_FILE = "desktop-prefs.json"

#: 顶栏切换控件上的文案
THEME_LABELS = {"day": "日间", "night": "夜间"}

#: 两套色板：token 名与 web ``theme.css`` 对齐。
#: ``*_text`` / ``border`` 是为 **Tk 文字对比度**单独定的派生值（Tk 没有 alpha 合成，
#: 也不能像 CSS 那样用半透明 ``rgba()``；数值由 ``tests/test_desktop_dual_theme.py`` 复核）。
PALETTES: dict[str, dict[str, str]] = {
    "day": {
        # ── web token（§3 表，day 列）
        "bg": "#E9F6F1",
        "bg2": "#DDF0E8",
        "surface": "#FFFFFF",
        "surface2": "#F4FBF8",
        "ink": "#06231D",
        "ink2": "#1E453D",
        "muted": "#4A6F66",
        "line": "#D8E7E1",
        "mint": "#00866F",
        "lime": "#5E9C1F",
        "cyan": "#0B7F95",
        "violet": "#6D4BD1",
        "glow": "#D3EDE6",
        "glow2": "#E4EFD9",
        # ── Tk 派生（文字对比度 / 不能透明的替代）
        "muted_text": "#40625A",
        "mint_text": "#007A66",
        "mint2": "#00A98C",
        "mint3": "#D6EFE7",
        "lime_text": "#4B7D14",
        "amber": "#A9741C",
        "amber_text": "#7E5514",
        "amber_bg": "#F6EBD8",
        "red": "#B94F47",
        "red_text": "#9C3F3A",
        "red_bg": "#F7E2E0",
        "info_text": "#2C7699",
        "border": "#7A948C",
        "line2": "#C2D6CE",
        "on_mint": "#FFFFFF",
        "focus": "#00866F",
        "white": "#FFFFFF",
    },
    "night": {
        # ── web token（§3 表，night 列）
        "bg": "#061A18",
        "bg2": "#0A2521",
        "surface": "#0C2B26",
        "surface2": "#103A33",
        "ink": "#EAFFF9",
        "ink2": "#C7E8DF",
        "muted": "#8DBAB0",
        "line": "#1E4A42",
        "mint": "#79F2D9",
        "lime": "#C8F36A",
        "cyan": "#64D8E8",
        "violet": "#B79CFF",
        "glow": "#14453C",
        "glow2": "#2A4A22",
        # ── Tk 派生
        "muted_text": "#9CC6BC",
        "mint_text": "#8DF6E1",
        "mint2": "#A7F7E6",
        "mint3": "#123F38",
        "lime_text": "#D6F68C",
        "amber": "#F2C45C",
        "amber_text": "#F6CF77",
        "amber_bg": "#2E2A16",
        "red": "#FF8C83",
        "red_text": "#FFA39B",
        "red_bg": "#33191A",
        "info_text": "#8FE0EE",
        "border": "#3E7A6E",
        "line2": "#2A5A50",
        # 主按钮底是亮薄荷色 → 上面的字必须是深色（夜间不能用白字压亮底）
        "on_mint": "#04120F",
        "focus": "#79F2D9",
        "white": "#EAFFF9",
    },
}

#: gui.theme.TOKENS 的 20 个键 ↔ 本模块色板键（同名为主，少数改名）
_GUI_TOKEN_MAP = {
    "bg": "bg", "bg2": "bg2", "surface": "surface", "surface2": "surface2",
    "ink": "ink", "ink2": "ink2", "muted": "muted", "line": "line", "line2": "line2",
    "mint": "mint", "mint2": "mint2", "mint3": "mint3", "amber": "amber", "red": "red",
    "cyan": "cyan", "violet": "violet", "focus": "focus", "white": "on_mint",
    "amber_bg": "amber_bg", "red_bg": "red_bg",
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

    做法：**临时**把 ``gui.theme`` 的模块级 token 换成目标主题 → 调 ``apply_theme``
    → 立刻还原。这样托盘/gui 这些同样 import 该模块的调用方不受影响，
    也不会把「桌面端当前主题」泄漏成进程级全局状态。
    """
    from ..gui import theme as gui_theme

    target = name if name in PALETTES else DEFAULT_THEME
    values = palette(target)
    saved = {key: getattr(gui_theme, key) for key in
             ("TOKENS", "MUTED_TEXT", "BORDER", "MINT_TEXT", "AMBER_TEXT", "RED_TEXT",
              "INFO_TEXT")}
    try:
        gui_theme.TOKENS = gui_tokens(target)
        gui_theme.MUTED_TEXT = values["muted_text"]
        gui_theme.BORDER = values["border"]
        gui_theme.MINT_TEXT = values["mint_text"]
        gui_theme.AMBER_TEXT = values["amber_text"]
        gui_theme.RED_TEXT = values["red_text"]
        gui_theme.INFO_TEXT = values["info_text"]
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
