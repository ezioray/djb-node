# SPDX-License-Identifier: Apache-2.0
"""CR-20261119-CLIENT-EXPORT-TEXT-GBK：中文 Windows 上 ``--export-text`` 不许崩。

真实报障（支持取证时）：

    UnicodeEncodeError: 'gbk' codec can't encode character '\\u25d0' in position 60

触发字符是主题按钮的 ``◐``（U+25D0）。这里不用开窗口：把 ``sys.stdout`` 换成一个
**只认 GBK** 的文本流，等价于中文 Windows 控制台，然后调真正的 ``_export_text``。

断言三件事：
1. 不抛（导出是兜底功能，它自己不能成为新的故障点）；
2. 字节流按 UTF-8 能解回原文（重定向到文件/管道时拿到的是干净的 UTF-8）；
3. 没有 ``buffer`` 的纯文本流也不崩（不可编码字符降级成 ``?``）。
"""
from __future__ import annotations

import io

from djb_node.desktop import app as desktop_app
from djb_node.desktop import views as vw

ICON = "◐"          # U+25D0：GBK 编不出来的那个字符


def _gbk_view() -> vw.View:
    return vw.View(key="token_plan", title="提供 Token Plan",
                   fields=(vw.Field("主题", ICON + " 日间", key="theme"),))


class _GbkConsole(io.TextIOWrapper):
    """等价于中文 Windows 控制台：文本层只认 gbk，底层是字节流。"""

    def __init__(self) -> None:
        self.raw = io.BytesIO()
        super().__init__(self.raw, encoding="gbk", newline="")


class _NoBufferStream:
    """没有 ``buffer`` 的纯文本流（某些被替换过的 stdout/logger 长这样）。"""

    encoding = "gbk"

    def __init__(self) -> None:
        self.parts: list[str] = []

    def write(self, text: str) -> int:
        text.encode("gbk")                 # 编不出就抛，与真控制台一致
        self.parts.append(text)
        return len(text)

    def flush(self) -> None:
        pass


class _FakeApp:
    def __init__(self, view: vw.View) -> None:
        self._view = view
        self._controller = self
        self.view = view.key
        self.copied = ""

    def show(self, key: str) -> vw.View:
        return self._view

    def copy_text(self, text: str) -> bool:
        self.copied = text
        return True


def test_print_utf8_survives_a_gbk_only_console(monkeypatch):
    console = _GbkConsole()
    monkeypatch.setattr(desktop_app.sys, "stdout", console)
    desktop_app._print_utf8(ICON + " 日间")            # 修前这里会抛 UnicodeEncodeError
    console.flush()
    assert console.raw.getvalue().decode("utf-8") == ICON + " 日间\n"


def test_print_utf8_falls_back_when_there_is_no_byte_buffer(monkeypatch):
    stream = _NoBufferStream()
    monkeypatch.setattr(desktop_app.sys, "stdout", stream)
    desktop_app._print_utf8(ICON + " 日间")            # 不许抛
    text = "".join(stream.parts)
    assert ICON not in text and "日间" in text        # 不可编码字符降级成 ?
    assert "?" in text


def test_export_text_returns_ok_on_a_gbk_console(monkeypatch):
    console = _GbkConsole()
    monkeypatch.setattr(desktop_app.sys, "stdout", console)
    app = _FakeApp(_gbk_view())
    code = desktop_app._export_text(app, "token_plan")
    assert code == desktop_app.EXIT_OK
    console.flush()
    printed = console.raw.getvalue().decode("utf-8")
    assert ICON in printed and "提供 Token Plan" in printed
    assert app.copied == _gbk_view().plain_text()      # 剪贴板拿到的仍是整页文字
