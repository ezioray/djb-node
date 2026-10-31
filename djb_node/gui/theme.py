# SPDX-License-Identifier: Apache-2.0
"""Night Signal desktop tokens; static, no animation and no GUI import."""
from __future__ import annotations

COLORS = {
    "bg": "#090C12", "panel": "#121622", "input": "#1A2130",
    "text": "#E6EDF3", "muted": "#ADB9CA", "border": "#71839C",
    "cyan": "#64F4D5", "violet": "#B49AFF", "wait": "#F2C45C",
    "danger": "#FF8C83", "ok": "#66E3AC",
}
FONT = ("Microsoft YaHei UI", 11)
MONO = ("Consolas", 11)


def apply_theme(root, ttk):
    """Use clam so native ttk defaults cannot silently override dark fields."""
    root.configure(background=COLORS["bg"])
    root.option_add("*Font", FONT)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=COLORS["panel"], foreground=COLORS["text"],
                    font=FONT, bordercolor=COLORS["border"], focuscolor=COLORS["cyan"])
    style.configure("TFrame", background=COLORS["panel"])
    style.configure("TLabel", background=COLORS["panel"], foreground=COLORS["text"])
    style.configure("Muted.TLabel", foreground=COLORS["muted"])
    style.configure("Signal.TLabel", foreground=COLORS["cyan"], font=MONO)
    style.configure("Warning.TLabel", foreground=COLORS["wait"])
    style.configure("TButton", padding=(16, 12), focusthickness=2,
                    background=COLORS["input"], foreground=COLORS["text"])
    style.map("TButton", background=[("active", COLORS["input"])],
              foreground=[("disabled", COLORS["muted"]), ("active", COLORS["cyan"])],
              bordercolor=[("focus", COLORS["cyan"]), ("!focus", COLORS["border"])])
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, padding=(8, 11), focusthickness=2,
                        indicatorbackground=COLORS["input"], indicatorforeground=COLORS["cyan"])
        style.map(name, background=[("active", COLORS["panel"])],
                  foreground=[("disabled", COLORS["muted"]), ("active", COLORS["cyan"])],
                  indicatorbackground=[("selected", COLORS["cyan"])])
    style.configure("TNotebook", background=COLORS["bg"], tabmargins=(0, 8, 0, 0))
    style.configure("TNotebook.Tab", padding=(12, 12), background=COLORS["input"])
    style.map("TNotebook.Tab", background=[("selected", COLORS["panel"])],
              foreground=[("selected", COLORS["cyan"])])
    return style


def menu_options():
    return dict(background=COLORS["panel"], foreground=COLORS["text"],
                activebackground=COLORS["input"], activeforeground=COLORS["cyan"],
                disabledforeground=COLORS["muted"], borderwidth=1, relief="solid",
                font=FONT)
