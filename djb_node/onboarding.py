# SPDX-License-Identifier: Apache-2.0
"""首次引导（RC14 B）：3 步 + 完成页，每步可跳过；状态只落 ``desktop/onboarding.json``。

设计口径（``reports/UX-PRICING-20261114/40-UX-DESIGN.md`` §3.4）：

* 引导是**应用内视图**，不是模态：不阻塞主窗口，也不依赖 daemon 启动成功；
* 每一步都有「跳过这一步」，第一步另有「跳过设置，直接进入」（= 全部跳过）；``Esc``
  等价于跳过当前步；
* **跳过不写任何凭据**：只写本文件（``version`` / ``started_at`` / ``completed_at`` /
  ``dismissed_at`` / ``skipped_at_step``）；
* 跳过后主界面照常可用，「重新运行首次引导」入口**永久**在「账户与安全」，
  另有 ``djb onboarding --reset`` / ``--status``；
* 开关 ``client.onboarding.enabled`` 默认关闭（§5.4）：关 → 不渲染引导，本文件保留但不读，
  零数据迁移（回滚规则原文）。

本模块只做状态机与落盘，**不碰 Tk、不发请求**：GUI 与 CLI 共用同一份语义。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from .util import ensure_dir, iso, read_json, utcnow, write_json

#: 状态文件相对 home 的路径（spec §3.4 原文）
STATE_RELATIVE = os.path.join("desktop", "onboarding.json")
SCHEMA_VERSION = 1

#: 步骤定义（1-based）：标题 / 一句话说明 / i18n key 预留
STEPS: tuple[dict[str, str], ...] = (
    {"step": "1", "title": "欢迎",
     "body": "TokenBank 联脑把这台机器闲置的算力接进平台，按成交计费。"
             "你随时可以在「账户与安全」里改设置。"},
    {"step": "2", "title": "绑定这台电脑",
     "body": "在网页端确认后，这台电脑的收益才能结算到你的账户。"},
    {"step": "3", "title": "提供 Token Plan（可选）",
     "body": "填一个请求地址与密钥，就能把上游模型挂到平台报价。"},
)
TOTAL_STEPS = len(STEPS)
DONE_TITLE = "完成"


def state_path(home: str | os.PathLike[str]) -> str:
    return os.path.join(str(home), STATE_RELATIVE)


@dataclass
class OnboardingState:
    """引导进度（**不含任何凭据/密钥/邮箱**）。"""

    version: int = SCHEMA_VERSION
    started_at: str | None = None
    completed_at: str | None = None
    dismissed_at: str | None = None
    skipped_at_step: int | None = None
    last_step: int = 1
    skipped_steps: list[int] = field(default_factory=list)

    # ── 派生
    def should_show(self) -> bool:
        """还没完成、也没被整体跳过 → 该出现（首次启动或「重新运行」之后）。"""
        return not self.completed_at and not self.dismissed_at

    def as_dict(self) -> dict[str, Any]:
        return {"version": self.version, "started_at": self.started_at,
                "completed_at": self.completed_at, "dismissed_at": self.dismissed_at,
                "skipped_at_step": self.skipped_at_step, "last_step": self.last_step,
                "skipped_steps": list(self.skipped_steps)}

    @classmethod
    def from_mapping(cls, raw: Any) -> "OnboardingState":
        """宽容读取：**只认已知字段**，坏值一律回默认（多出来的键直接丢掉）。"""
        if not isinstance(raw, Mapping):
            return cls()
        state = cls()
        state.version = _int(raw.get("version"), SCHEMA_VERSION)
        state.started_at = _text(raw.get("started_at"))
        state.completed_at = _text(raw.get("completed_at"))
        state.dismissed_at = _text(raw.get("dismissed_at"))
        state.skipped_at_step = _int(raw.get("skipped_at_step"), None)
        state.last_step = max(1, min(TOTAL_STEPS, _int(raw.get("last_step"), 1)))
        skipped = raw.get("skipped_steps")
        if isinstance(skipped, list):
            state.skipped_steps = sorted({step for step in (_int(item, None) for item in skipped)
                                          if step is not None and 1 <= step <= TOTAL_STEPS})
        return state


def _int(value: Any, default: int | None) -> Any:
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


class OnboardingStore:
    """``desktop/onboarding.json`` 的读写（0600；写失败不阻断引导本身）。"""

    def __init__(self, home: str | os.PathLike[str], *, clock: Any = None) -> None:
        self.home = str(home)
        self.path = state_path(self.home)
        self.clock = clock or utcnow

    # ── 读
    def load(self) -> OnboardingState:
        if not self.home:
            return OnboardingState()
        return OnboardingState.from_mapping(read_json(self.path, default=None))

    def exists(self) -> bool:
        return os.path.exists(self.path)

    # ── 写
    def save(self, state: OnboardingState) -> bool:
        """落盘成功返回 True；写不进去返回 False（调用方只 toast，不阻断）。"""
        try:
            ensure_dir(os.path.dirname(self.path), mode=0o700)
            write_json(self.path, state.as_dict(), mode=0o600)
            return True
        except OSError:                                    # 只读盘 / 权限不足
            return False

    def start(self, state: OnboardingState | None = None) -> OnboardingState:
        """开始（或继续）引导：首次给 ``started_at``，不覆盖已有的完成/跳过后重跑语义。"""
        state = state or self.load()
        if not state.started_at:
            state.started_at = iso(self.clock())
        self.save(state)
        return state

    def skip_step(self, step: int, state: OnboardingState | None = None) -> OnboardingState:
        state = state or self.load()
        step = max(1, min(TOTAL_STEPS, int(step)))
        if step not in state.skipped_steps:
            state.skipped_steps.append(step)
        state.skipped_at_step = step
        if not state.started_at:
            state.started_at = iso(self.clock())
        self.save(state)
        return state

    def skip_all(self, state: OnboardingState | None = None) -> OnboardingState:
        """「跳过设置，直接进入」：记录 ``dismissed_at`` + ``skipped_at_step=1``。"""
        state = state or self.load()
        if not state.started_at:
            state.started_at = iso(self.clock())
        state.dismissed_at = iso(self.clock())
        state.skipped_at_step = 1
        if 1 not in state.skipped_steps:
            state.skipped_steps.append(1)
        self.save(state)
        return state

    def complete(self, state: OnboardingState | None = None) -> OnboardingState:
        state = state or self.load()
        if not state.started_at:
            state.started_at = iso(self.clock())
        state.completed_at = iso(self.clock())
        self.save(state)
        return state

    def touch_step(self, step: int, state: OnboardingState | None = None
                   ) -> OnboardingState:
        """记住看到第几步（下次重跑从这一步继续）；坏值不写盘。"""
        state = state or self.load()
        state.last_step = max(1, min(TOTAL_STEPS, int(step)))
        self.save(state)
        return state

    def reset(self) -> OnboardingState:
        """``djb onboarding --reset``：删掉状态文件（= 下次启动重新引导）。"""
        try:
            if os.path.exists(self.path):
                os.remove(self.path)
        except OSError:                                    # pragma: no cover - 权限
            pass
        return OnboardingState()


# ─────────────────────────────────────────────────────────── 步骤 → 面板投影
def step_title(step: int) -> str:
    """「欢迎（步骤 1 / 3）」；完成页不显示序号。"""
    if step < 1:
        return DONE_TITLE
    if step > TOTAL_STEPS:
        return DONE_TITLE
    entry = STEPS[step - 1]
    return entry["title"] + "（步骤 " + str(step) + " / " + str(TOTAL_STEPS) + "）"


def step_body(step: int) -> str:
    if 1 <= step <= TOTAL_STEPS:
        return STEPS[step - 1]["body"]
    return "引导结束了：主界面随时可用；要再跑一次，去「账户与安全 → 重新运行首次引导」。"
