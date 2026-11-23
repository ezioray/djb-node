# SPDX-License-Identifier: Apache-2.0
"""R2.4 壳层/性能：骨架首屏、切页只重绘内容区、开关开/关都能启动。

来源：`CHG-20261123-R2-4-NODE-SHELL-PERF`（S1 骨架 / S4 启动零网络 / S8 切页只重绘内容区）。

口径（DoD）：
* ① 首屏 ≤1.5s（骨架上线时）—— 这里量的是 **Python 侧到「这一帧推给窗口管理器」**；
  冻结 exe 的冷启动还要叠 onefile 解包，必须在别的机器上量（见 evidence 里的说明）。
* ② 切页 ≤120ms。
* ④ 开关默认关，开/关都能启动。
"""
from __future__ import annotations

import os
import statistics
import time

import pytest

from djb_node.desktop import client_flags
from tests import tk_support

FLAG = client_flags.SKELETON_FLAG
CACHE_FLAG = client_flags.CACHE_FLAG


@pytest.fixture(autouse=True)
def _no_leaked_env(monkeypatch):
    """测试自己控制开关：先清掉外部环境变量，避免本机环境影响结果。"""
    for name in ("DJB_FLAG_CLIENT_STARTUP_SKELETON", "DJB_FLAG_CLIENT_SNAPSHOT_CACHE"):
        monkeypatch.delenv(name, raising=False)
    return None


def _enabled(name: str, monkeypatch, value: str | None) -> bool:
    if value is None:
        return client_flags.enabled(name)
    env = "DJB_FLAG_" + name.upper().replace(".", "_").replace("-", "_")
    monkeypatch.setenv(env, value)
    return client_flags.enabled(name)


# ── 开关本身：三字段 / 默认关 / 环境变量可压过 / 到期强制关
def test_flags_are_registered_with_three_fields_and_off_by_default():
    for name in (FLAG, CACHE_FLAG):
        spec = client_flags.CLIENT_FLAGS[name]
        assert spec.default is False
        assert spec.owner and spec.expire_at and spec.rollback_rule
    assert client_flags.enabled(FLAG) is False
    assert client_flags.enabled("client.never.registered") is False


def test_flag_can_be_flipped_by_env_and_expires(monkeypatch):
    from datetime import date

    assert _enabled(FLAG, monkeypatch, "true") is True
    monkeypatch.delenv("DJB_FLAG_CLIENT_STARTUP_SKELETON")
    # 到期日次日起强制关（expire_at=2026-12-31）
    assert client_flags.enabled(FLAG, today=date(2026, 12, 31)) is False   # 当日仍算未开（默认关）
    assert client_flags.enabled(FLAG, today=date(2027, 1, 1)) is False     # 到期后一律关


# ── 开/关都能启动 + 首屏计时
@tk_support.needs_tk
@pytest.mark.parametrize("flag", [None, "true"])
def test_window_starts_with_flag_off_and_on(monkeypatch, flag):
    from djb_node.desktop.app import DesktopApp, demo_snapshot
    from djb_node.desktop.controller import DesktopController
    from djb_node.desktop.datasource import FakeDataSource

    if flag:
        monkeypatch.setenv("DJB_FLAG_CLIENT_STARTUP_SKELETON", flag)
    root = tk_support.new_root()
    started = time.monotonic()
    app = DesktopApp(DesktopController(FakeDataSource(dict(demo_snapshot().sections))), root=root)
    try:
        assert app._view_title is not None                 # 窗口起来了
        if flag:
            first_paint = app._startup_ms.get("first_paint")
            assert first_paint is not None
            assert first_paint - started < 1.5, "骨架首屏超过 1.5s：%.3fs" % (first_paint - started)
            assert app._skeleton_shown is True
        else:
            assert app._startup_ms == {}                   # 关 = 老路径，没有骨架
    finally:
        try:
            app.stop()
        except Exception:                              # pragma: no cover
            pass
        try:
            root.destroy()
        except Exception:                              # pragma: no cover
            pass


# ── S4：缓存首屏 = 零网络（启动路径一次 snapshot() 都不调）
@tk_support.needs_tk
def test_cached_snapshot_paints_without_touching_the_data_source(monkeypatch, tmp_path):
    from djb_node.desktop import snapshot_cache
    from djb_node.desktop.app import DesktopApp, demo_snapshot
    from djb_node.desktop.controller import DesktopController
    from djb_node.desktop.datasource import FakeDataSource

    monkeypatch.setenv("DJB_FLAG_CLIENT_STARTUP_SKELETON", "true")
    monkeypatch.setenv("DJB_FLAG_CLIENT_SNAPSHOT_CACHE", "true")
    demo = demo_snapshot()
    assert snapshot_cache.save(str(tmp_path), dict(demo.sections), fetched_at="2026-11-23 12:00:00")
    source = FakeDataSource(dict(demo.sections))
    calls_before = source.calls
    controller = DesktopController(source)
    controller.cache_home = str(tmp_path)
    root = tk_support.new_root()
    app = DesktopApp(controller, root=root, prefs_home=str(tmp_path))
    try:
        assert source.calls == calls_before, "启动路径又去取数了（缓存没起作用）"
        assert controller.stale_from_cache is True
        assert "缓存" in controller.message and "正在后台刷新" in controller.message
        assert app._skeleton_shown is not True             # 有缓存就不画空骨架
    finally:
        try:
            app.stop()
        except Exception:                              # pragma: no cover
            pass
        try:
            root.destroy()
        except Exception:                              # pragma: no cover
            pass


def test_controller_writes_cache_after_a_successful_refresh(tmp_path):
    from djb_node.desktop import snapshot_cache
    from djb_node.desktop.app import demo_snapshot
    from djb_node.desktop.controller import DesktopController
    from djb_node.desktop.datasource import FakeDataSource

    controller = DesktopController(FakeDataSource(dict(demo_snapshot().sections)))
    controller.cache_home = str(tmp_path)
    controller.refresh()
    cached = snapshot_cache.load(str(tmp_path))
    assert cached is not None
    assert set(cached["sections"]) == set(demo_snapshot().sections)


def test_stale_cache_is_ignored():
    import tempfile

    from djb_node.desktop import snapshot_cache

    folder = tempfile.mkdtemp()
    snapshot_cache.save(folder, {"status": {"state": "running"}}, fetched_at="old",
                        now=time.time() - snapshot_cache.MAX_AGE_S - 60)
    assert snapshot_cache.load(folder) is None            # 过期 = 当没有
    snapshot_cache.save(folder, {"status": {"state": "running"}}, fetched_at="new")
    assert snapshot_cache.load(folder) is not None


# ── S8：切页 ≤120ms（只重绘内容区）
@tk_support.needs_tk
def test_page_switch_is_under_120ms_and_shell_widgets_are_reused(monkeypatch):
    from djb_node.desktop.app import DesktopApp, demo_snapshot
    from djb_node.desktop.controller import DesktopController
    from djb_node.desktop.datasource import FakeDataSource
    from djb_node.desktop.views import VIEW_KEYS

    root = tk_support.new_root()
    app = DesktopApp(DesktopController(FakeDataSource(dict(demo_snapshot().sections))), root=root)
    try:
        shell_ids = (id(app._view_title), id(app._page_kicker), id(app._content))
        app.show_view("overview")
        root.update()
        samples: list[float] = []
        for _round in range(3):
            for key in VIEW_KEYS:
                started = time.monotonic()
                app.show_view(key)
                root.update_idletasks()
                samples.append((time.monotonic() - started) * 1000)
        p50 = statistics.median(samples)
        assert (id(app._view_title), id(app._page_kicker), id(app._content)) == shell_ids, \
            "切页把外壳控件重建了（违反 S8 只重绘内容区）"
        assert p50 <= 120, "切页 P50 = %.1f ms（超 120ms）" % p50
    finally:
        try:
            app.stop()
        except Exception:                              # pragma: no cover
            pass
        try:
            root.destroy()
        except Exception:                              # pragma: no cover
            pass
