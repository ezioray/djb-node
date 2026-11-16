# SPDX-License-Identifier: Apache-2.0
"""键位清单守卫：**Windows 上真的能绑**才算数（Linux Tk 认 X11 keysym，抓不到）。

来历：``8b345e5`` 给 Shift+Tab 加了 X11 专有别名 ``<ISO_Left_Tab>``。Linux CI 全绿，
到 Windows 真机是 ``TclError: bad event type or keysym``，``DesktopApp.__init__`` 直接抛 =
客户端起不来（2026-11-16 真机会话抓到）。这条测试把「所有 bind 的序列」在 Windows 上过一遍。

``ISO_Left_Tab`` 的处理口径：**保留** ``_bind_keys`` 里的 ``try/except``（绑不上就跳过，
Shift+Tab 另有 ``<Shift-Tab>`` 覆盖），并在下面白名单里**写明原因**。白名单只允许这一个，
以后谁再写 X11 keysym 必须自己来解释，不能默默滑过去。
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="Linux Tk 接受 X11 keysym，测不出这个洞")

#: Windows Tk 不认、但**有意保留**在代码里的序列（每条都要写清为什么）
ALLOWED = {
    "ISO_Left_Tab": "X11 的 Shift+Tab 别名；_bind_keys 里 try/except 跳过，"
                    "Windows 由 <Shift-Tab> 覆盖",
}

BIND = re.compile(r'bind(?:_all)?\(\s*"<([^">]+)>"')


def _bound_sequences() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    root = pathlib.Path(__file__).resolve().parent.parent / "djb_node" / "desktop"
    for path in root.glob("*.py"):
        for seq in BIND.findall(path.read_text(encoding="utf-8")):
            found.setdefault(seq, set()).add(path.name)
    return found


def test_every_bound_sequence_is_valid_on_windows():
    tk = pytest.importorskip("tkinter")
    try:
        window = tk.Tk()
    except Exception:                              # pragma: no cover - 无桌面会话
        pytest.skip("没有可用的 Tk 显示")
    window.withdraw()
    bad: list[str] = []
    try:
        for seq, files in sorted(_bound_sequences().items()):
            if seq in ALLOWED:
                continue
            try:
                window.bind("<" + seq + ">", lambda _e: None)
            except tk.TclError:
                bad.append("<%s> (%s)" % (seq, ", ".join(sorted(files))))
    finally:
        window.destroy()
    assert not bad, "Windows Tk 不认这些绑定：" + "; ".join(bad)


def test_allowlist_entries_still_exist():
    """白名单不许留「已经删掉的」条目——否则它只是在给未来的洞打掩护。"""
    sequences = _bound_sequences()
    stale = [seq for seq in ALLOWED if seq not in sequences]
    assert not stale, "白名单里的序列代码里已经没有了，删掉它：" + ", ".join(stale)
