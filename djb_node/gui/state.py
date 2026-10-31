# SPDX-License-Identifier: Apache-2.0
"""托盘 ↔ 窗口的共享状态总线（暂停 / 接单 / 让路原因）。

为什么需要
==========

托盘和主窗口**都只读 daemon 状态**，但各自有刷新节奏（托盘 5s、窗口按需）。用户在最窗口里
按「暂停接单」后，托盘菜单要立刻显示「恢复接单」，反之亦然。本模块提供一个极小的
发布/订阅缓存：谁回读到新状态就 :meth:`ControlBus.publish`，其他界面立即重渲染。

口径
====

* **不是状态权威**：真值永远来自 daemon 回读（``status --json``）；
  本总线只缓存「最近一次回读结果」并广播，不推断、不乐观更新。
* **不带凭据**：只放 ``state`` / ``accepting`` / ``reason`` / ``mode`` 四个字段的文本投影。
* **订阅可退订**：:meth:`ControlBus.subscribe` 返回退订函数，窗口销毁时必须调用，
  否则回调会打到已销毁的控件上。
* **回调异常不扩散**：一个订阅者抛异常不影响其他订阅者，也不回抛给发布者（旁路）。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping

__all__ = ["ControlBus", "ControlSnapshot", "STATE_LABELS"]

#: daemon 状态 → 中文标签（与托盘、窗口同一口径）
STATE_LABELS: dict[str, str] = {
    "online": "运行中",
    "starting": "启动中",
    "paused": "已暂停",
    "updating": "更新中",
    "stopped": "已停止",
    "error": "错误",
}
#: 未知状态一律这样写，禁止默认成「运行中」
UNKNOWN_LABEL = "状态未知"


@dataclass(frozen=True)
class ControlSnapshot:
    """一次回读的界面投影（全部字段都是可上屏文本/布尔，不含凭据）。"""

    state: str = "unknown"
    accepting: bool | None = None
    reason: str = ""
    mode: str = ""
    source: str = ""

    @property
    def known(self) -> bool:
        return self.state in STATE_LABELS

    @property
    def label(self) -> str:
        return STATE_LABELS.get(self.state, UNKNOWN_LABEL)

    @property
    def paused(self) -> bool:
        return self.state == "paused"

    @classmethod
    def from_status(cls, status: Mapping[str, Any] | None, *, mode: str = "",
                    source: str = "") -> "ControlSnapshot":
        data = status if isinstance(status, Mapping) else {}
        state = str(data.get("state") or "unknown")
        accepting = data.get("accepting")
        reason = str(data.get("reason_label") or data.get("reason") or "")
        return cls(state=state, accepting=accepting if isinstance(accepting, bool) else None,
                   reason=reason, mode=str(mode or data.get("yield_mode") or ""),
                   source=source)

    def merge(self, **changes: Any) -> "ControlSnapshot":
        return replace(self, **changes)


class ControlBus:
    """最近一次回读结果的广播器（同步、无锁；Tk 与测试都能用）。"""

    def __init__(self, snapshot: ControlSnapshot | None = None) -> None:
        self._snapshot = snapshot or ControlSnapshot()
        self._listeners: list[Callable[[ControlSnapshot], None]] = []
        self.published = 0

    @property
    def current(self) -> ControlSnapshot:
        return self._snapshot

    def subscribe(self, listener: Callable[[ControlSnapshot], None]) -> Callable[[], None]:
        """登记订阅者，返回退订函数（幂等）。"""
        if not callable(listener):
            raise TypeError("listener 必须可调用")
        self._listeners.append(listener)

        def unsubscribe() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return unsubscribe

    def publish(self, snapshot: ControlSnapshot) -> ControlSnapshot:
        """更新缓存并同步广播；与当前值完全相同时不广播（避免刷新风暴）。"""
        if not isinstance(snapshot, ControlSnapshot):
            raise TypeError("publish 需要 ControlSnapshot")
        self._snapshot = snapshot
        self.published += 1
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:                      # 旁路：一个界面出错不能拖垮另一个
                continue
        return snapshot

    def publish_status(self, status: Mapping[str, Any] | None, *, mode: str = "",
                       source: str = "") -> ControlSnapshot:
        return self.publish(ControlSnapshot.from_status(status, mode=mode, source=source))

    @property
    def listeners(self) -> int:
        return len(self._listeners)
