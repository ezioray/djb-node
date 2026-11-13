# SPDX-License-Identifier: Apache-2.0
"""S2 回归：刷新在 worker 线程里跑，主线程只做派发 + 轮询。

验收对应（客户原话「点刷新整窗卡死，拖不动、切不了页」）：

* (a) 立即反馈：``on_start`` 在取数**之前**回调（按钮「刷新中…」/状态栏）；
* (b) 不阻塞：``start()`` 立即返回，主线程一秒都不花在 ``sleep`` 的假数据源上；
* (c) 并发闸门：重复点击不叠加（第二次走 ``on_busy``，数据源只被调一次）；
* (d) 异常不吞：worker 里的异常原样交给 apply，controller 维持「刷新失败（TypeError）」
  +「界面保留上一次结果」，页面不空白、不静默。

测试用**假 scheduler**（不碰 Tk）：``FakeScheduler`` 收集 ``after`` 回调，``flush()`` 手动跑，
所以「主线程被阻塞」这件事可以被确定性地断言（不是靠 sleep 猜）。
"""
from __future__ import annotations

import threading
import time

import pytest

from djb_node.desktop.controller import DesktopController
from djb_node.desktop.datasource import FakeDataSource, ProviderError, Snapshot, Unavailable
from djb_node.desktop.refresh_worker import RefreshWorker


class FakeScheduler:
    """假的 ``root.after``：回调排队，等测试 ``flush()`` 才执行（= 主线程空闲时）。"""

    def __init__(self) -> None:
        self.jobs: list[tuple[int, object]] = []
        self.cancelled: list[object] = []
        self._seq = 0

    def after(self, delay: int, fn):
        self._seq += 1
        job = ("job", self._seq)
        self.jobs.append((int(delay), fn))
        return job

    def after_cancel(self, job) -> None:
        self.cancelled.append(job)

    def flush(self, rounds: int = 200, *, wait_s: float = 0.002) -> int:
        """把「主线程」推进若干轮，直到没有待执行回调（模拟事件循环）。"""
        for index in range(rounds):
            if not self.jobs:
                return index
            _delay, fn = self.jobs.pop(0)
            fn()
            time.sleep(wait_s)
        return rounds


def _slow_source(delay: float = 0.25, *, boom: bool = False) -> FakeDataSource:
    source = FakeDataSource({"status": {"state": "online"}, "bench": {"cpu_count": 8}},
                            source="test")

    class Slow(FakeDataSource):
        def snapshot(self, *, sections=None):
            time.sleep(delay)
            if boom:
                raise TypeError("boom")
            return super().snapshot(sections=sections)

    source.__class__ = Slow
    return source


# ─────────────────────────────────────────── (a)(b) 不阻塞 + 立即反馈
def test_start_returns_before_fetch_finishes_and_reports_immediately():
    scheduler = FakeScheduler()
    events: list[str] = []
    source = _slow_source(0.3)
    worker = RefreshWorker(source.snapshot,
                           apply=lambda fresh, error, sections=None: events.append("apply"),
                           on_start=lambda: events.append("start"),
                           schedule=scheduler.after, cancel=scheduler.after_cancel)

    began = time.monotonic()
    assert worker.start() is True
    elapsed = time.monotonic() - began

    assert elapsed < 0.15, "start() 不能等取数（实测 %.3f s）" % elapsed
    assert events == ["start"], "必须先给反馈再取数：" + repr(events)

    scheduler.flush()
    assert events == ["start", "apply"]


def test_result_is_applied_on_the_main_thread_path():
    """apply 只在 flush（主线程侧）里发生，worker 线程自己不许碰 UI。"""
    scheduler = FakeScheduler()
    seen: dict[str, object] = {}

    def apply(fresh, error, sections=None):
        seen["thread"] = threading.current_thread().name

    source = _slow_source(0.05)
    worker = RefreshWorker(source.snapshot, apply=apply,
                           schedule=scheduler.after, cancel=scheduler.after_cancel)
    worker.start()
    scheduler.flush()

    assert seen["thread"] != "djb-refresh"


# ─────────────────────────────────────────── (c) 并发闸门
def test_repeat_clicks_do_not_stack():
    scheduler = FakeScheduler()
    busy = []
    source = _slow_source(0.2)
    worker = RefreshWorker(source.snapshot, apply=lambda *a, **k: None,
                           on_busy=lambda: busy.append(1),
                           schedule=scheduler.after, cancel=scheduler.after_cancel)

    assert worker.start() is True
    for _ in range(4):
        assert worker.start() is False, "第二次点击必须被丢掉，不能排第二个取数"
    scheduler.flush()

    assert source.calls == 1, "数据源被打了 %d 次（重复点击叠加了）" % source.calls
    assert len(busy) == 4

    # 上一轮结束后，再点能正常开始
    assert worker.start() is True
    scheduler.flush()
    assert source.calls == 2


# ─────────────────────────────────────────── (d) 异常不吞
def test_error_is_forwarded_to_apply():
    scheduler = FakeScheduler()
    captured = {}

    def apply(fresh, error, sections=None):
        captured["fresh"] = fresh
        captured["error"] = error

    worker = RefreshWorker(_slow_source(0.02, boom=True).snapshot, apply=apply,
                           schedule=scheduler.after, cancel=scheduler.after_cancel)
    worker.start()
    scheduler.flush()

    assert isinstance(captured["error"], TypeError)
    assert captured["fresh"] is None


def test_controller_keeps_old_snapshot_on_error(tmp_path):
    """controller 层：失败只把本次域标成「刷新失败（TypeError）」，其它域保留。"""
    source = FakeDataSource({"status": {"state": "online"}, "logs": {"rows": []}})
    controller = DesktopController(source, cli=None)

    class Boom(FakeDataSource):
        def snapshot(self, *, sections=None):
            raise TypeError("boom")

    source.__class__ = Boom
    view = controller.refresh()

    assert "刷新失败" in controller.message
    assert "界面保留上一次结果" in controller.message
    section = controller.snapshot.get("status")
    assert isinstance(section, Unavailable) and "刷新失败（TypeError）" in section.reason
    assert view.key == "overview"


# ─────────────────────────────────────────── 生命周期
def test_close_stops_polling():
    scheduler = FakeScheduler()
    applied = []
    source = _slow_source(0.05)
    worker = RefreshWorker(source.snapshot, apply=lambda *a, **k: applied.append(1),
                           schedule=scheduler.after, cancel=scheduler.after_cancel)
    worker.start()
    worker.close()
    scheduler.flush()

    assert worker.closed is True
    assert applied == []
    assert worker.start() is False, "关掉的 worker 不许再派发"


def test_poll_once_drives_without_scheduler():
    """无 Tk 环境（没有 after）：用 ``poll_once`` 手动驱动，结果照样能落地。"""
    applied = []
    worker = RefreshWorker(_slow_source(0.02).snapshot,
                           apply=lambda fresh, error, sections=None: applied.append(fresh))
    assert worker.start() is True
    for _ in range(200):
        if worker.poll_once():
            break
        time.sleep(0.005)
    assert applied and isinstance(applied[0], Snapshot)


# ─────────────────────────────────────────── 真实「卡死」证据（耗时对照）
def test_slow_source_does_not_block_caller_thread():
    """慢数据源（0.4 s）下，主线程侧累计耗时必须远小于取数耗时。"""
    scheduler = FakeScheduler()
    worker = RefreshWorker(_slow_source(0.4).snapshot, apply=lambda *a, **k: None,
                           schedule=scheduler.after, cancel=scheduler.after_cancel)
    began = time.monotonic()
    worker.start()
    for _ in range(60):                      # 模拟主线程空闲轮询 60 次
        scheduler.flush(rounds=1, wait_s=0.001)
        if not worker.busy:
            break
        time.sleep(0.01)
    main_side = time.monotonic() - began
    assert main_side >= 0.35, "取数确实花了几百毫秒（假数据源没生效？）"
    assert worker.busy is False


# ─────────────────────────────────────────── controller 侧异步钩子
def test_controller_request_refresh_uses_async_hook(tmp_path):
    calls = []
    source = FakeDataSource({"status": {"state": "online"}})
    controller = DesktopController(source, cli=None)
    controller.refresh_via = lambda sections: calls.append(sections)

    assert controller.request_refresh(("gpu",)) is True
    assert calls == [("gpu",)]

    result = controller.action("refresh")
    assert result.effect == "refresh" and "正在刷新" in result.message


def test_controller_request_refresh_falls_back_to_sync(tmp_path):
    source = FakeDataSource({"status": {"state": "online"}})
    controller = DesktopController(source, cli=None)

    assert controller.request_refresh(None) is False
    assert controller.message.startswith("已刷新")
    assert source.calls == 1
