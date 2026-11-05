# SPDX-License-Identifier: Apache-2.0
"""C3 托盘菜单模型（纯数据 + 纯函数）：六项菜单、状态投影、角标、action intent。

设计边界
========

* **不写 GUI、不执行动作**：本模块不 import 任何 GUI 工具包、不 import subprocess、不开线程；
  ``PAUSE`` / ``RESUME`` / ``QUIT`` 等只产出 :class:`TrayIntent`（动作 + 建议 CLI 命令 + 说明），
  由三端壳决定是否执行、是否二次确认。
* **纯投影**：只读传入的 ``status`` / ``notifications`` 映射，不做 IO。
  余额 / 今日收益**原样展示服务端下发的值**（字符串或数字只做 ``str()``），
  本层不做任何计算、换算、四舍五入；字段缺失 / 空值统一显示「—」。
* **不展开正文**：消息只取未读计数与角标；正文 / payload 不进入托盘模型。
  标题投影在 :mod:`djb_node.tray.notifications`（同样只取标题，不读正文）。
* **S2 基础六项**：状态、暂停/恢复（按 daemon 状态二选一）、消息中心、诊断、日志、退出。
  未绑定账户时额外出现第 7 项「绑定网页端」（``node.bound is False`` 才显示；绑定后回到六项），
  因此菜单长度是 6 或 6+1，测试断言用 ``S2_MENU_SIZE + S2_BIND_MENU_SIZE``，不写死 7。
  S3 入口（上游、模型同步、抽检等）不在本层出现，也不预留占位项。

状态口径
========

``status`` 的形状来自 ``djb status --json``（本机回环 / 离线兜底），本层容错读取：

===========  ==============  ==============================================
status.state  TrayModel.state  说明
===========  ==============  ==============================================
online        running         接单中（``accepting`` 另存）
starting      running         启动中（标签不同）
paused        paused          已暂停（带 reason）
updating      paused          更新中（不可恢复，见下）
stopped       stopped         已停止
error         error           错误
缺失 / 未知   stopped         保守兜底，不显示假「运行中」
===========  ==============  ==============================================

暂停/恢复二选一只看**原始状态**：``paused`` → 显示「恢复接单」，其余显示「暂停接单」；
「暂停接单」仅在 daemon 运行且状态为 running 时可用，避免在 stopped / error / updating
时给用户一个按了也没用的按钮。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from ..errors import UsageError
from ..state import reason_label as state_reason_label

__all__ = [
    "TrayAction",
    "TrayIntent",
    "TrayMenuItem",
    "TrayModel",
    "EMPTY_VALUE",
    "S2_MENU_SIZE",
    "S2_BIND_MENU_SIZE",
]

#: 缺字段 / 空值时的展示占位（不猜、不补 0）
EMPTY_VALUE = "—"
#: S2 基础菜单项数量（状态 / 暂停恢复 / 消息中心 / 诊断 / 日志 / 退出）
S2_MENU_SIZE = 6
#: 未绑定账户时额外出现的条件项数量（「绑定网页端」）；绑定后回到基础 6 项
S2_BIND_MENU_SIZE = 1


class TrayAction(str, Enum):
    """托盘可发起的动作（只有 intent，不在这里执行）。"""

    OPEN_STATUS = "open_status"
    PAUSE = "pause"
    RESUME = "resume"
    OPEN_MESSAGES = "open_messages"
    DIAGNOSE = "diagnose"
    BIND = "bind"
    QUIT = "quit"
    OPEN_LOGS = "open_logs"


@dataclass(frozen=True)
class TrayIntent:
    """动作意图：壳按 ``command`` 调用 CLI（或用自己的原生实现），本层不执行。"""

    action: TrayAction
    command: tuple[str, ...] = ()
    requires_daemon: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.action, TrayAction):
            raise UsageError("TrayIntent.action 必须是 TrayAction")
        command = tuple(str(item) for item in self.command)
        if any(not item for item in command):
            raise UsageError("TrayIntent.command 不能含空参数")
        object.__setattr__(self, "command", command)

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action.value, "command": list(self.command),
                "requires_daemon": self.requires_daemon,
                "description": self.description}


@dataclass(frozen=True)
class TrayMenuItem:
    """一个菜单项：``key`` 稳定（壳做点击路由），``label`` 可本地化，``badge`` 只放计数。"""

    key: str
    label: str
    action: TrayAction
    enabled: bool = True
    badge: str | None = None

    def __post_init__(self) -> None:
        key = str(self.key or "").strip()
        if not key:
            raise UsageError("TrayMenuItem.key 不能为空")
        if not isinstance(self.action, TrayAction):
            raise UsageError("TrayMenuItem.action 必须是 TrayAction")
        label = str(self.label or "").strip()
        if not label:
            raise UsageError("TrayMenuItem.label 不能为空")
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "enabled", bool(self.enabled))
        badge = self.badge
        object.__setattr__(self, "badge", str(badge) if badge not in (None, "") else None)

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "action": self.action.value,
                "enabled": self.enabled, "badge": self.badge}


#: 每个动作对应的 intent（静态、无状态；command 只是建议，不执行）
INTENTS: dict[TrayAction, TrayIntent] = {
    TrayAction.OPEN_STATUS: TrayIntent(
        TrayAction.OPEN_STATUS, ("djb", "status", "--json"),
        description="打开状态面板（只读）"),
    TrayAction.PAUSE: TrayIntent(
        TrayAction.PAUSE, ("djb", "pause"), requires_daemon=True,
        description="暂停接单（不影响本机已排队的收据）"),
    TrayAction.RESUME: TrayIntent(
        TrayAction.RESUME, ("djb", "resume"), requires_daemon=True,
        description="恢复接单"),
    TrayAction.OPEN_MESSAGES: TrayIntent(
        TrayAction.OPEN_MESSAGES, ("djb", "notify", "list", "--json"),
        description="打开消息中心（只读列表，不展开正文）"),
    TrayAction.DIAGNOSE: TrayIntent(
        TrayAction.DIAGNOSE, ("djb", "diagnose", "--json"),
        description="生成脱敏诊断包"),
    TrayAction.BIND: TrayIntent(
        TrayAction.BIND, ("djb", "auth", "--bind", "<一次性码>"),
        description="绑定网页端（一次性码在 网页端 → 账户/节点 → 生成绑定码）"),
    TrayAction.OPEN_LOGS: TrayIntent(
        TrayAction.OPEN_LOGS, ("djb", "logs", "--tail", "200"),
        description="查看脱敏日志（壳也可直接打开日志目录）"),
    TrayAction.QUIT: TrayIntent(
        TrayAction.QUIT, (),
        description="退出托盘壳（不改变 daemon 状态）"),
}

#: status.state → (TrayModel.state, 中文标签)
_STATE_VIEW: dict[str, tuple[str, str]] = {
    "online": ("running", "运行中"),
    "starting": ("running", "启动中"),
    "paused": ("paused", "已暂停"),
    "updating": ("paused", "更新中"),
    "stopped": ("stopped", "已停止"),
    "error": ("error", "错误"),
}
_UNKNOWN_STATE = ("stopped", "已停止")
_PAUSE_AVAILABLE_STATES = frozenset({"online", "starting"})


def _text(value: Any) -> str:
    """只做展示归一：None / 空串 → 「—」；其余原样转字符串（不计算、不格式化数字）。"""
    if value is None or isinstance(value, bool):
        return EMPTY_VALUE
    text = str(value).strip()
    return text or EMPTY_VALUE


def _count(notifications: Mapping[str, Any] | None, *names: str) -> int:
    if not isinstance(notifications, Mapping):
        return 0
    for name in names:
        value = notifications.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return max(0, value)
        if isinstance(value, str) and value.strip().isdigit():
            return max(0, int(value.strip()))
    return 0


def _badge(unread: int, p0_unread: int) -> str | None:
    if unread <= 0:
        return None
    return ("!" if p0_unread > 0 else "") + str(unread)


@dataclass(frozen=True)
class TrayModel:
    """一次状态投影的结果；``items`` 的顺序就是菜单展示顺序。"""

    state: str
    raw_state: str
    state_label: str
    daemon_running: bool
    accepting: bool
    reason: str | None
    reason_label: str
    next_step: str | None
    balance: str
    today_earnings: str
    unread: int
    p0_unread: int
    bound: bool | None
    items: tuple[TrayMenuItem, ...]

    # ─────────────────────────────────────────────────── 构造
    @classmethod
    def build(cls, status: Mapping[str, Any] | None,
              notifications: Mapping[str, Any] | None = None) -> "TrayModel":
        """把 ``djb status --json`` + 消息投影变成菜单模型（纯函数，无 IO）。"""
        data: Mapping[str, Any] = status if isinstance(status, Mapping) else {}
        raw_state = str(data.get("state") or "").strip()
        normalized, state_label = _STATE_VIEW.get(raw_state, _UNKNOWN_STATE)

        reason_value = data.get("reason")
        reason = str(reason_value).strip() if isinstance(reason_value, str) and \
            reason_value.strip() else None
        reason_text = ""
        if reason:
            raw_label = data.get("reason_label")
            reason_text = str(raw_label).strip() if isinstance(raw_label, str) and \
                raw_label.strip() else state_reason_label(reason)

        raw_running = data.get("daemon_running")
        daemon_running = raw_running if isinstance(raw_running, bool) else \
            raw_state in ("online", "starting", "paused", "updating")
        accepting = data.get("accepting") is True

        next_step_value = data.get("next_step")
        next_step = str(next_step_value).strip() if isinstance(next_step_value, str) and \
            next_step_value.strip() else None

        unread = _count(notifications, "unread", "unread_count")
        p0_unread = _count(notifications, "p0_unread", "p0")

        # 绑定态来自 ``djb status --json`` 的 ``node.bound``（本机有没有客户端密钥）；
        # 缺字段 / 类型不对 → None（不知道就不显示绑定入口，避免误导已绑定用户）。
        node = data.get("node") if isinstance(data.get("node"), Mapping) else {}
        bound_value = node.get("bound")
        bound = bound_value if isinstance(bound_value, bool) else None

        status_label = "状态：" + state_label + (
            "（" + reason_text + "）" if reason_text else "")

        if raw_state == "paused":
            pause_item = TrayMenuItem(
                "resume", "恢复接单", TrayAction.RESUME,
                enabled=daemon_running)
        else:
            pause_item = TrayMenuItem(
                "pause", "暂停接单", TrayAction.PAUSE,
                enabled=daemon_running and raw_state in _PAUSE_AVAILABLE_STATES)

        # 基础 6 项；仅在**明确未绑定**时插入第 7 项「绑定网页端」（退出保持末位）。
        bind_items = () if bound is not False else (
            TrayMenuItem("bind", "绑定网页端", TrayAction.BIND, enabled=True),)
        items = (
            TrayMenuItem("status", status_label, TrayAction.OPEN_STATUS, enabled=True),
            pause_item,
            TrayMenuItem("messages", "消息中心", TrayAction.OPEN_MESSAGES, enabled=True,
                         badge=_badge(unread, p0_unread)),
            TrayMenuItem("diagnose", "生成诊断包", TrayAction.DIAGNOSE, enabled=True),
            *bind_items,
            TrayMenuItem("logs", "打开日志", TrayAction.OPEN_LOGS, enabled=True),
            TrayMenuItem("quit", "退出", TrayAction.QUIT, enabled=True),
        )
        return cls(
            state=normalized, raw_state=raw_state or "unknown", state_label=state_label,
            daemon_running=daemon_running, accepting=accepting, reason=reason,
            reason_label=reason_text, next_step=next_step,
            balance=_text(data.get("balance")),
            today_earnings=_text(data.get("today_earnings")),
            unread=unread, p0_unread=p0_unread, bound=bound, items=items)

    # ─────────────────────────────────────────────────── 查询
    @property
    def menu_keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.items)

    def item(self, key: str) -> TrayMenuItem | None:
        target = str(key or "")
        for item in self.items:
            if item.key == target:
                return item
        return None

    def status_line(self) -> str:
        """菜单首行：状态 + 服务端下发的余额 / 今日收益（缺失显示「—」）。"""
        return (self.items[0].label + " · 余额 " + self.balance
                + " · 今日收益 " + self.today_earnings)

    def intent_for(self, key: str | TrayAction) -> TrayIntent | None:
        """动作意图（不执行）；禁用项 / 未知 key 返回 None。"""
        if isinstance(key, TrayAction):
            item = next((candidate for candidate in self.items
                         if candidate.action is key), None)
        else:
            item = self.item(str(key))
        if item is None or not item.enabled:
            return None
        return INTENTS.get(item.action)

    # ─────────────────────────────────────────────────── 渲染
    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "raw_state": self.raw_state,
            "state_label": self.state_label,
            "daemon_running": self.daemon_running,
            "accepting": self.accepting,
            "reason": self.reason,
            "reason_label": self.reason_label,
            "next_step": self.next_step,
            "balance": self.balance,
            "today_earnings": self.today_earnings,
            "unread": self.unread,
            "p0_unread": self.p0_unread,
            "bound": self.bound,
            "items": [item.to_dict() for item in self.items],
        }

    def render_json(self) -> str:
        """稳定 JSON（排序键、缩进 2）——壳 / 测试都可直接比对。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)

    def render_text(self) -> str:
        """纯文本菜单（调试 / 日志 / 无 GUI 环境）；不写任何终端控制字符。"""
        lines = [self.status_line()]
        if self.next_step:
            lines.append("下一步：" + self.next_step)
        for item in self.items:
            mark = "✓" if item.enabled else "✗"
            badge = " [" + item.badge + "]" if item.badge else ""
            lines.append(mark + " " + item.label + badge)
        return "\n".join(lines)
