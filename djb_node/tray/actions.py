# SPDX-License-Identifier: Apache-2.0
"""Explicit local control, fail closed, never start/stop or write offline intent."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Mapping

from .. import facts
from ..config import feature_flag
from .model import TrayAction

FLAG = "client.tray.control.enabled"


def control_enabled(config: Any, *, today: date | None = None) -> bool:
    spec = facts.FEATURE_FLAGS.get(FLAG, {})
    try:
        expires = date.fromisoformat(str(spec["expire_at"]))
        now = today or datetime.now(timezone.utc).date()
        return now <= expires and feature_flag(config, FLAG)
    except (KeyError, TypeError, ValueError):
        return False


#: 开关关着时给客户看的话（RC15-039 #3）。**托盘兜底与直调共用这一条**——
#: 以前托盘在 `enabled()` 为假时根本不进 ``apply_control``，直接落「命令指南 / 未执行」，
#: 把 CLI 当唯一指引；文案只改 ``apply_control`` 等于只改了没人走的那条路。
CONTROL_DISABLED_MESSAGE = (
    "没有执行：这台机器的接单控制开关现在是关闭状态（或已过期），"
    "所以暂停/恢复发不出去，服务状态一个字都没改。"
    "下一步：去网页端「设备 · 本机节点」把控制开关打开或续期，"
    "再回到托盘点一次；开关没打开之前点多少次都不会生效。"
)


@dataclass(frozen=True)
class ControlResult:
    verified: bool
    message: str
    state: str = "unknown"


def disabled_control(action: TrayAction) -> ControlResult:
    """开关关着时的唯一结果（托盘兜底路径必须调它，不许自己写文案）。"""
    return ControlResult(False, CONTROL_DISABLED_MESSAGE)


def apply_control(action: TrayAction, *, call: Callable[..., Mapping[str, Any]],
                  enabled: Callable[[], bool], confirm: Callable[[str], bool]) -> ControlResult:
    """Confirmation, fresh preflight, command, independent status readback.

    A resume may remain blocked by guards; command acknowledgement is NOT success.
    No fallback persists offline preferences. Error details never reach UI/logs.
    """
    if action not in (TrayAction.PAUSE, TrayAction.RESUME):
        return ControlResult(False, "不支持的控制动作；未执行。")
    if not enabled():
        # RC15-039 #3：上一版文案是「开关…；命令指南，未执行。」——客户读不懂
        # （「命令指南」是什么？没执行的是哪个命令？）。人话版集中在
        # :data:`CONTROL_DISABLED_MESSAGE`，托盘兜底走的是同一条，文案不许分叉。
        return disabled_control(action)
    message = ("暂停接收新任务；在途任务按现有护栏处理。不停止服务。" if action is TrayAction.PAUSE
               else "恢复用户暂停；现有护栏仍可阻止接单。不启动服务。")
    if not confirm(message):
        return ControlResult(False, "已取消；未执行。")
    try:
        before = call({"cmd": "status"}, timeout=5.0)
        status = before.get("status")
        if before.get("ok") is not True or not isinstance(status, Mapping):
            return ControlResult(False, "状态回读无效；未执行。")
        allowed = {"online", "starting"} if action is TrayAction.PAUSE else {"paused"}
        if status.get("state") not in allowed:
            return ControlResult(False, "当前状态不允许此动作；未执行。")
        # Resume only a known user pause, never an automatic/unknown guard pause.
        if action is TrayAction.RESUME and status.get("reason") != "user_paused":
            return ControlResult(False, "不是可确认的用户暂停；请先检查护栏，未执行。")
        if not enabled():
            return ControlResult(False, "确认期间开关关闭或到期；未执行。")
        response = call({"cmd": action.value}, timeout=5.0)
        if response.get("ok") is not True:
            return ControlResult(False, "控制响应未确认；结果未知，请刷新状态，不自动重试。")
        readback = call({"cmd": "status"}, timeout=5.0)
        current = readback.get("status")
        if readback.get("ok") is not True or not isinstance(current, Mapping):
            return ControlResult(False, "动作已发出，回读失败；结果未知，不自动重试。")
        state = str(current.get("state") or "unknown")
        verified = (state == "paused" and current.get("accepting") is False
                    if action is TrayAction.PAUSE else
                    state == "online" and current.get("accepting") is True)
        return ControlResult(verified,
                             "已回读确认暂停接单。" if verified and action is TrayAction.PAUSE else
                             "已回读确认恢复接单。" if verified else
                             "动作已响应，但目标接单状态未确认；可能仍受护栏约束，请刷新。", state)
    except Exception:
        return ControlResult(False, "控制通道失败；结果未知，请刷新，不自动重试或写入离线偏好。")
