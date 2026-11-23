# SPDX-License-Identifier: Apache-2.0
"""R2.1 C4「图标不是 logo」：托盘/窗口用 TOKENBANK 品牌资源，读不到就回落到色块。

来源：`CHG-20261123-R2-1-NODE-QUICKWINS` 的 LOGO 追加项 + owner 的 `design/logo-v3/` 交付。

三条硬约束（任何一条破了都算回归）：

1. **资源不缩放**：16/24 用微标（单 T），32/48 用简短版，256 用完整版，``.ico`` 里
   16/24/32/48/256 五个尺寸齐全 —— 代码里不许出现 ``subsample``/``zoom``；
2. **不许因为图标启动失败**：资源缺失/损坏 → 返回 ``None`` → 回落原色块，窗口照常画；
3. **托盘 16px 要认得出来**：16px 用的是微标（不是 256 缩小），见 owner 的 16px 验收图。
"""
from __future__ import annotations

import struct

import pytest

from djb_node import assets as brand
from tests import tk_support

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# ── 1. 资源清单与尺寸（不缩放口径）
def test_every_declared_asset_exists_and_is_a_png_or_ico():
    for key, name in brand.ASSETS.items():
        data = brand.resource_bytes(name)
        assert data, key + " 资源读不到：" + name
        if name.endswith(".png"):
            assert data.startswith(PNG_MAGIC), name + " 不是 PNG"
        else:
            assert data[:4] == b"\x00\x00\x01\x00", name + " 不是 ICO"


@pytest.mark.parametrize("name,width,height", [
    ("tokenbank-dark-16-16.png", 16, 16),        # 托盘微标（单 T）
    ("tokenbank-dark-16-24.png", 24, 24),
    ("tokenbank-dark-simple-32.png", 32, 32),
    ("tokenbank-dark-simple-48.png", 48, 48),
    ("tokenbank-dark-full-256.png", 256, 256),   # 窗口图标
])
def test_png_assets_keep_their_declared_size(name, width, height):
    data = brand.resource_bytes(name)
    assert data and data.startswith(PNG_MAGIC)
    got_w, got_h = struct.unpack(">II", data[16:24])       # IHDR 里的宽高
    assert (got_w, got_h) == (width, height), name + " 被改过尺寸"


def test_package_icon_has_all_five_sizes():
    data = brand.resource_bytes(brand.PACKAGE_ICON)
    assert data
    count = struct.unpack("<H", data[4:6])[0]
    sizes = []
    for index in range(count):
        offset = 6 + index * 16
        width = data[offset] or 256
        sizes.append(width)
    assert sizes == [16, 24, 32, 48, 256], "打包图标的尺寸档被改过：" + repr(sizes)


# ── 2. 读不到就返回 None（绝不抛）
def test_missing_resource_returns_none(monkeypatch):
    from importlib import resources

    class Boom:
        def joinpath(self, _name):
            raise FileNotFoundError("模拟：资源缺失")

    monkeypatch.setattr(resources, "files", lambda _pkg: Boom())
    assert brand.resource_bytes("tokenbank-dark-16-16.png") is None
    assert brand.resource_path("tokenbank-dark-16-16.png") is None


# ── 3. 托盘：品牌图标 + 缺失时回落色块（真 Tk）
@tk_support.needs_tk
def test_tray_icon_uses_the_brand_micro_mark():
    from djb_node.tray.app import TkBackend

    backend = TkBackend()
    try:
        icon = backend._icon
        assert icon.width() == 16 and icon.height() == 16
        # 微标不是纯色块：至少有 3 种以上不同颜色的像素（画出来的色块是 4 色大块）
        colors = {icon.get(x, y) for x in range(16) for y in range(16)}
        assert len(colors) >= 3
        assert backend._load_brand_icon("tray", "tray_2x") is not None
    finally:
        backend._root.destroy()


@tk_support.needs_tk
def test_tray_falls_back_to_the_drawn_icon_when_assets_are_gone(monkeypatch):
    from djb_node import assets as assets_mod
    from djb_node.tray.app import TkBackend

    monkeypatch.setattr(assets_mod, "resource_bytes", lambda _name: None)
    backend = TkBackend()                          # 资源全丢：仍然必须能起
    try:
        assert backend._icon.width() == 16 and backend._icon.height() == 16
        assert backend._load_brand_icon("tray") is None
        # 回落色块 = 4 个纯色块：颜色数量少（用来看住「别把回落也删了」）
        colors = {backend._icon.get(x, y) for x in range(16) for y in range(16)}
        assert len(colors) <= 6
    finally:
        backend._root.destroy()


# ── 4. 桌面窗口图标 = 完整版 256
@tk_support.needs_tk
def test_desktop_window_icon_is_the_full_256_mark():
    from djb_node.desktop.app import DesktopApp, demo_snapshot
    from djb_node.desktop.controller import DesktopController
    from djb_node.desktop.datasource import FakeDataSource

    root = tk_support.new_root()
    app = DesktopApp(DesktopController(FakeDataSource(dict(demo_snapshot().sections))), root=root)
    try:
        assert app._brand_icon is not None
        assert (app._brand_icon.width(), app._brand_icon.height()) == (256, 256)
    finally:
        try:
            app.stop()
        except Exception:                        # pragma: no cover
            pass
        try:
            root.destroy()
        except Exception:                        # pragma: no cover
            pass


@tk_support.needs_tk
def test_desktop_starts_even_without_any_asset(monkeypatch):
    from djb_node import assets as assets_mod
    from djb_node.desktop.app import DesktopApp, demo_snapshot
    from djb_node.desktop.controller import DesktopController
    from djb_node.desktop.datasource import FakeDataSource

    monkeypatch.setattr(assets_mod, "resource_bytes", lambda _name: None)
    root = tk_support.new_root()
    app = DesktopApp(DesktopController(FakeDataSource(dict(demo_snapshot().sections))), root=root)
    try:
        assert app._brand_icon is None           # 没图标
        assert app._view_title is not None       # 但窗口照常画出来了
    finally:
        try:
            app.stop()
        except Exception:                        # pragma: no cover
            pass
        try:
            root.destroy()
        except Exception:                        # pragma: no cover
            pass
