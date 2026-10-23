# SPDX-License-Identifier: Apache-2.0
"""指数退避（M15 §7：1s / 5s / 30s / 5min / 30min，≤8 次）。

只用于**幂等**上报（心跳 / 策略 / 收据）；资金与签名类不自动重试（§7）。
可注入 clock，测试不 sleep。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from . import facts
from .util import iso, utcnow


@dataclass(frozen=True)
class RetryDecision:
    attempt: int                 # 已失败次数（本次失败后）
    delay_s: int | None          # 距离下次重试的秒数；None = 不再自动重试
    next_at: str | None
    exhausted: bool


class Backoff:
    def __init__(self, *, delays: tuple[int, ...] = facts.RETRY_DELAYS_S,
                 max_attempts: int = facts.RETRY_MAX_ATTEMPTS,
                 jitter: float = 0.0, now: Callable[[], datetime] = utcnow) -> None:
        if not delays:
            raise ValueError("delays 不能为空")
        if jitter < 0 or jitter >= 1:
            raise ValueError("jitter 必须在 [0, 1)")
        self.delays = tuple(int(item) for item in delays)
        self.max_attempts = int(max_attempts)
        self.jitter = float(jitter)
        self._now = now

    def delay_for(self, attempt: int) -> int:
        """第 attempt 次失败后的等待秒数（attempt 从 1 开始）。"""
        index = min(max(1, int(attempt)), len(self.delays)) - 1
        return self.delays[index]

    def exhausted(self, attempt: int) -> bool:
        return int(attempt) >= self.max_attempts

    def after_failure(self, attempt: int, *, now: datetime | None = None) -> RetryDecision:
        moment = now or self._now()
        attempt = int(attempt)
        if self.exhausted(attempt):
            return RetryDecision(attempt=attempt, delay_s=None, next_at=None, exhausted=True)
        delay = self.delay_for(attempt)
        if self.jitter:
            import random
            delay = int(delay * (1 + random.uniform(-self.jitter, self.jitter)))
            delay = max(0, delay)
        return RetryDecision(attempt=attempt, delay_s=delay,
                             next_at=iso(moment + timedelta(seconds=delay)),
                             exhausted=False)

    def describe(self) -> dict[str, int]:
        return {"delays": list(self.delays), "max_attempts": self.max_attempts}
