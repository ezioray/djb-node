# SPDX-License-Identifier: Apache-2.0
"""S2 修复的核心：把「取数」搬出 Tk 主线程。

问题（客户原话）
================

「点刷新整窗卡死，拖不动、切不了页，像死机；再点几次还是没反应。」

原因不是网络慢，而是**主线程同步取数**：``DesktopApp.refresh()`` 直接在 Tk 主线程里跑
``controller.refresh()`` → ``LocalDataSource.snapshot()`` → 6 次 CLI 子进程
（冻结包 onefile 冷启动真机 1.5-1.6 s/次）+ 2 次 nvidia-smi + 3 次 powershell。
Tk 的事件循环在这几秒里完全停摆：重绘、拖动、点击全部排队 → 观感就是「卡死」。

设计约束（为什么不能「开个线程直接刷 UI」）
==========================================

Tk 不是线程安全的：控件只能在**创建它的线程**（主线程）里碰。数据层取数完会
``controller._publish()`` → 总线订阅者 → ``DesktopApp._on_bus`` → 直接 ``configure`` 控件。
所以边界只能划在：

* **worker 线程**：只跑 ``source.snapshot()``（纯 stdlib + 子进程 + HTTP，不碰控件）；
* **主线程**：拿到结果后 ``apply_refresh()``（合并快照、写 message、``_publish``、渲染）。

回主线程的方式用 ``root.after`` **轮询**（不是从 worker 线程里调 ``after``：``after`` 本身
也不是线程安全的）。``schedule`` / ``cancel`` / ``clock`` 都可注入，所以单测不需要 Tk，
也不需要真线程（用假 scheduler 手动 ``flush``）。

行为要求（对应验收项）
======================

* (a) 立即反馈：``start()`` 同步回调 ``on_start``（按钮文案「刷新中…」/状态栏），
  这一句在**取数开始之前**执行，客户点下去就看到变化；
* (b) 不阻塞：``start()`` 立刻返回；主线程只做「派发 + 轮询」；
* (c) 并发闸门：一次只跑一个刷新，重复点击走 ``on_busy``（不叠加、不排队）；
* (d) 异常不吞：worker 里的任何异常都原样交给 ``apply(result, error, sections)``，
  由 controller 维持「刷新失败（TypeError）」+「界面保留上一次结果」的老口径。
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Sequence

__all__ = ["RefreshWorker"]


class RefreshWorker:
    """一次一个刷新：worker 线程取数、主线程应用；重复点击直接丢弃。"""

    def __init__(self, fetch: Callable[..., Any], *,
                 apply: Callable[..., None],
                 on_start: Callable[[], None] | None = None,
                 on_busy: Callable[[], None] | None = None,
                 on_finish: Callable[[], None] | None = None,
                 schedule: Callable[[int, Callable[[], None]], Any] | None = None,
                 cancel: Callable[[Any], None] | None = None,
                 poll_ms: int = 50,
                 clock: Callable[[], float] | None = None) -> None:
        self._fetch = fetch
        self._apply = apply
        self._on_start = on_start
        self._on_busy = on_busy
        self._on_finish = on_finish
        self._schedule = schedule
        self._cancel = cancel
        self._poll_ms = max(1, int(poll_ms))
        self._clock = clock

        self._busy = False
        self._closed = False
        self._thread: threading.Thread | None = None
        self._done: threading.Event | None = None
        self._result: Any = None
        self._error: BaseException | None = None
        self._sections: Sequence[str] | None = None
        self._job: Any = None
        self._started_at = 0.0
        #: 上一次刷新耗时（秒）；用于状态栏「已刷新（1.2 s）」与真机取证
        self.last_duration_s = 0.0

    # ─────────────────────────────────────────────── 状态
    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def closed(self) -> bool:
        return self._closed

    # ─────────────────────────────────────────────── 入口
    def start(self, sections: Sequence[str] | None = None) -> bool:
        """请求一次刷新。返回 ``True`` = 真的开始取数；``False`` = 已有刷新在跑。"""
        if self._closed:
            return False
        if self._busy:
            self._notify(self._on_busy)
            return False
        self._busy = True
        self._sections = sections
        self._result = None
        self._error = None
        self._done = threading.Event()
        if self._clock is not None:
            self._started_at = self._clock()
        # (a) 先反馈，再干活：这两行的顺序就是「点了有没有反应」的全部区别
        self._notify(self._on_start)
        thread = threading.Thread(target=self._work, name="djb-refresh", daemon=True)
        self._thread = thread
        thread.start()
        self._arm()
        return True

    def close(self) -> None:
        """窗口关闭：停止轮询（不会去杀已经派出去的取数线程）。"""
        self._closed = True
        job, self._job = self._job, None
        if job is not None and self._cancel is not None:
            try:
                self._cancel(job)
            except Exception:                      # pragma: no cover - 解释器已在退出
                pass

    # ─────────────────────────────────────────────── worker 侧
    def _work(self) -> None:
        try:
            # 关键字调用：``source.snapshot(sections=…)`` 是 kw-only，位置传参会 TypeError
            self._result = self._fetch(sections=self._sections)
        except BaseException as exc:                # noqa: BLE001 - (d) 原样交给主线程
            self._error = exc
        finally:
            done = self._done
            if done is not None:
                done.set()

    # ─────────────────────────────────────────────── 主线程侧
    def _arm(self) -> None:
        if self._schedule is None:
            return                              # 单测模式：由测试手动调用 poll_once()
        try:
            self._job = self._schedule(self._poll_ms, self._poll)
        except Exception:                       # pragma: no cover - 控件已销毁
            self._job = None

    def _poll(self) -> None:
        self._job = None
        if self._closed:
            return
        done = self._done
        if done is None or not done.is_set():
            self._arm()                          # 还没好：继续等（不阻塞主线程）
            return
        self._deliver()

    def poll_once(self) -> bool:
        """同步驱动一次轮询（单测/无 Tk 环境用）。返回 ``True`` = 结果已应用。"""
        self._poll()
        return not self._busy

    def _deliver(self) -> None:
        result, error = self._result, self._error
        sections = self._sections
        self._result = self._error = None
        if self._clock is not None and self._started_at:
            self.last_duration_s = max(0.0, self._clock() - self._started_at)
        self._busy = False
        try:
            self._apply(result, error, sections=sections)
        finally:
            self._notify(self._on_finish)

    @staticmethod
    def _notify(hook: Callable[[], None] | None) -> None:
        if hook is None:
            return
        try:
            hook()
        except Exception:                       # noqa: BLE001 - 纯展示回调，不许影响刷新
            pass
