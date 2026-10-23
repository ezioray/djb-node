# SPDX-License-Identifier: Apache-2.0
"""daemon 状态机（M15 §3 / §5.9；INV-01 不静默失败）。

状态：stopped / starting / online / paused / error / updating（facts.DAEMON_STATES）。

三条硬规则：

1. 迁移只走 facts.STATE_TRANSITIONS；非法迁移抛 StateTransitionError（不静默纠正）。
2. 任何非 online 状态都必须带 reason + next_step（INV-01）：用户必须知道「为什么不赚」。
3. 让路（yield_paused）不是离线，也不能被当成用户暂停：两者的 reason 不同，恢复路径不同。

状态快照持久化在 kv（daemon_state），daemon 重启后先读回再决定是否自动恢复。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import facts
from .errors import StateTransitionError, UsageError
from .util import iso, parse_iso, utcnow


#: 每个暂停原因的用户可读「下一步」（INV-01：原因 + 下一步）
NEXT_STEP: dict[str, str] = {
    "user_paused": "执行 djb resume 恢复接单",
    "yield_paused": "检测到你在用这台机器，已自动让路；空闲后会自动恢复（也可 djb resume 强制恢复）",
    "resource_limited": "资源占用超过 L2 上限（CPU/GPU/内存）：暂停接单，回落后自动恢复；或 djb limits set --cpu/--gpu/--mem 调整",
    "quota_exhausted": "配额用尽：等待下个周期自动恢复，或 djb limits set 调整上限",
    "schedule_paused": "不在允许接单的时段：到时段后自动恢复，或 djb limits set --schedule 调整",
    "unbound": "执行 djb auth --bind <一次性码> 绑定账户后才会接单",
    "conservative": "策略缺失/过期或连续失败：保持保守模式（不接新单）；恢复网络后会自动重试",
    "receipt_backlog": "收据积压超过 24 小时：已停止接单；恢复网络后自动补传（djb status 查看）",
    "upstream_invalid": "上游不可用：djb upstream probe 后修正端点/模型",
    "credential_invalid": "凭据失效：djb auth --set-key 更换密钥，或重新绑定",
    "account_frozen": "账户/节点被冻结：查看平台通知并申诉；本地配置与在途收据保留",
    "retired": "节点已清退：不再接单；如需继续请重新注册",
    "update_failed": "更新失败已回滚：djb update --status 查看原因，修复后重试",
}


@dataclass(frozen=True)
class Snapshot:
    state: str
    reason: str | None = None
    since: str = ""
    next_step: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in facts.DAEMON_STATES:
            raise UsageError("未知 daemon 状态：" + str(self.state))

    # ── 语义
    def runtime_state(self) -> str:
        """心跳 runtime_state（spec 1.2.0 约定值；未知值服务端会忽略）。"""
        key = self.state if self.state != "paused" else "paused:" + str(self.reason)
        return facts.RUNTIME_STATE_MAP.get(key, "error")

    def is_accepting(self) -> bool:
        return self.state == "online" and self.reason is None

    def is_conservative(self) -> bool:
        return self.reason == "conservative"

    def is_paused_by_yield(self) -> bool:
        return self.state == "paused" and self.reason == "yield_paused"

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "reason": self.reason, "since": self.since,
                "next_step": self.next_step, "detail": dict(self.detail)}

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "Snapshot":
        if not isinstance(raw, dict) or raw.get("state") not in facts.DAEMON_STATES:
            return cls(state="stopped", since=iso(utcnow()))
        reason = raw.get("reason") if raw.get("reason") in facts.PAUSE_REASONS else None
        detail = raw.get("detail") if isinstance(raw.get("detail"), dict) else {}
        return cls(state=str(raw["state"]), reason=reason,
                   since=str(raw.get("since") or iso(utcnow())),
                   next_step=raw.get("next_step"), detail=dict(detail))


class StateMachine:
    """纯状态机（无 IO）；持久化由 daemon / CLI 负责。"""

    def __init__(self, initial: Snapshot | None = None, *, now=utcnow) -> None:
        self._now = now
        self._snapshot = initial or Snapshot(state="stopped", since=iso(now()))

    @property
    def snapshot(self) -> Snapshot:
        return self._snapshot

    def _move(self, target: str, *, reason: str | None = None,
              next_step: str | None = None,
              detail: dict[str, Any] | None = None) -> Snapshot:
        current = self._snapshot
        if target == current.state and reason == current.reason:
            # 同态同因：只刷新 detail（不制造噪声迁移）
            self._snapshot = Snapshot(state=current.state, reason=current.reason,
                                      since=current.since,
                                      next_step=next_step or current.next_step,
                                      detail=dict(detail or current.detail))
            return self._snapshot
        allowed = facts.STATE_TRANSITIONS.get(current.state, ())
        if target not in allowed:
            raise StateTransitionError(
                "非法状态迁移：" + current.state + " -> " + target
                + "（允许：" + ",".join(allowed) + "）")
        if target == "paused" and reason not in facts.PAUSE_REASONS:
            raise UsageError("暂停必须给出已登记的原因（facts.PAUSE_REASONS）")
        self._snapshot = Snapshot(state=target, reason=reason, since=iso(self._now()),
                                  next_step=next_step or (NEXT_STEP.get(reason)
                                                          if reason else None),
                                  detail=dict(detail or {}))
        return self._snapshot

    # ── 迁移入口
    def start(self) -> Snapshot:
        return self._move("starting")

    def online(self) -> Snapshot:
        return self._move("online")

    def pause(self, reason: str, *, next_step: str | None = None,
              detail: dict[str, Any] | None = None) -> Snapshot:
        return self._move("paused", reason=reason, next_step=next_step, detail=detail)

    def resume(self) -> Snapshot:
        return self._move("online")

    def fail(self, *, next_step: str | None = None,
             detail: dict[str, Any] | None = None) -> Snapshot:
        return self._move("error", next_step=next_step or "查看 djb logs；修复后 djb start",
                          detail=detail)

    def begin_update(self) -> Snapshot:
        return self._move("updating")

    def stop(self) -> Snapshot:
        return self._move("stopped")

    def restore(self, snapshot: Snapshot) -> Snapshot:
        """重启读回：不校验迁移（进程重启不是业务迁移），但原因必须合法。"""
        if snapshot.state not in facts.DAEMON_STATES:
            raise UsageError("未知 daemon 状态：" + str(snapshot.state))
        if snapshot.reason is not None and snapshot.reason not in facts.PAUSE_REASONS:
            snapshot = Snapshot(state=snapshot.state, reason=None, since=snapshot.since,
                                next_step=snapshot.next_step, detail=snapshot.detail)
        self._snapshot = snapshot
        return self._snapshot

    def describe(self) -> dict[str, Any]:
        snap = self._snapshot
        payload = snap.to_dict()
        payload["runtime_state"] = snap.runtime_state()
        payload["accepting"] = snap.is_accepting()
        if not snap.is_accepting() and snap.next_step:
            payload["next_step"] = snap.next_step
        return payload


def retry_at_iso(seconds: int, now: datetime | None = None) -> str:
    return iso((now or utcnow()) + timedelta(seconds=max(0, int(seconds))))


def reason_label(reason: str | None) -> str:
    labels = {
        "user_paused": "用户暂停",
        "yield_paused": "让路（用户在用）",
        "resource_limited": "资源超 L2 上限",
        "quota_exhausted": "配额用尽",
        "schedule_paused": "时段外",
        "unbound": "未绑定账户",
        "conservative": "保守模式（策略/网络问题）",
        "receipt_backlog": "收据积压",
        "upstream_invalid": "上游失效",
        "credential_invalid": "凭据失效",
        "account_frozen": "账户冻结",
        "retired": "已清退",
        "update_failed": "更新失败已回滚",
    }
    return labels.get(str(reason), str(reason) if reason else "-")
