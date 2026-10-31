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


@dataclass(frozen=True)
class ControlResult:
    verified: bool
    message: str
    state: str = "unknown"


def apply_control(action: TrayAction, *, call: Callable[..., Mapping[str, Any]],
                  enabled: Callable[[], bool], confirm: Callable[[str], bool]) -> ControlResult:
    """Confirmation, fresh preflight, command, independent status readback.

    A resume may remain blocked by guards; command acknowledgement is NOT success.
    No fallback persists offline preferences. Error details never reach UI/logs.
    """
    if action not in (TrayAction.PAUSE, TrayAction.RESUME):
        return ControlResult(False, "不支持的控制动作；未执行。")
    if not enabled():
        return ControlResult(False, "控制开关关闭或已到期；命令指南，未执行。")
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
