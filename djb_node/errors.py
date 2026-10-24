# SPDX-License-Identifier: Apache-2.0
"""错误、退出码与 Error 信封（00-conventions §5 / spec components.schemas.Error）。

三个层次：

* DjbError -- 本模块异常的根；带 spec 数字错误码、HTTP 状态与 CLI 退出码。
* 子类按 CLI 语义分组，退出码见 djb_node.facts 的 EXIT_* 常量。
* error_envelope 系列 -- 本机回环 API 的错误信封，字段与 spec 一致
  （type / code / message / request_id；可带 retry_after / reason）。

硬规则：message 只供人类阅读，不含凭据 / 堆栈 / 上游细节；程序只看 type / code。
"""
from __future__ import annotations

from typing import Any

from . import facts


class DjbError(Exception):
    """本模块异常根。"""

    #: spec Error.type 枚举值（不新造）
    error_type: str = "server_error"
    http_status: int = 500
    exit_code: int = facts.EXIT_ERROR

    def __init__(self, message: str, *, code: int = 50001,
                 details: dict[str, Any] | None = None,
                 retry_after: int | None = None,
                 reason: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = int(code)
        self.details = dict(details or {})
        self.retry_after = retry_after
        self.reason = reason

    def envelope(self, request_id: str) -> dict[str, Any]:
        body: dict[str, Any] = {"error": {
            "type": self.error_type,
            "code": self.code,
            "message": self.message,
            "request_id": request_id,
        }}
        if self.retry_after is not None:
            body["error"]["retry_after"] = int(self.retry_after)
        if self.reason is not None:
            body["error"]["reason"] = self.reason
        return body

    def __str__(self) -> str:                                  # pragma: no cover - 直通
        return self.message


class UsageError(DjbError):
    error_type = "invalid_request_error"
    http_status = 400
    exit_code = facts.EXIT_USAGE


class NotInstalledError(DjbError):
    """未初始化 / 未安装：先 djb init。"""

    error_type = "conflict_error"
    http_status = 409
    exit_code = facts.EXIT_NOT_INSTALLED


class NotRunningError(DjbError):
    error_type = "conflict_error"
    http_status = 409
    exit_code = facts.EXIT_NOT_RUNNING


class AuthError(DjbError):
    """凭据缺失 / 无效（本机或平台侧）。"""

    error_type = "authentication_error"
    http_status = 401
    exit_code = facts.EXIT_AUTH


class NetworkError(DjbError):
    """网络 / 上游不可达（可重试）。"""

    error_type = "upstream_error"
    http_status = 503
    exit_code = facts.EXIT_NETWORK


class VerifyError(DjbError):
    """签名 / 哈希 / 策略校验失败（拒绝采用，不静默降级）。"""

    error_type = "protocol_error"
    http_status = 426
    exit_code = facts.EXIT_VERIFY


class ConflictError(DjbError):
    """状态冲突：端口占用、状态迁移不允许、重复注册等。"""

    error_type = "conflict_error"
    http_status = 409
    exit_code = facts.EXIT_CONFLICT


class ForbiddenError(DjbError):
    """权限 / 通道关闭（403）：节点冻结、未准入、通道关闭等。"""

    error_type = "permission_error"
    http_status = 403
    exit_code = facts.EXIT_AUTH


class RateLimitError(DjbError):
    """429：可重试（Retry-After / retry_after 只作退避提示）。"""

    error_type = "rate_limit_error"
    http_status = 429
    exit_code = facts.EXIT_NETWORK


class StoreError(DjbError):
    error_type = "server_error"
    http_status = 500
    exit_code = facts.EXIT_ERROR


class StoreDowngradeError(StoreError):
    """本地 schema 比本进程新：拒绝运行（PRAGMA user_version 只前进，M15 §5）。"""


class StoreNotInitializedError(NotInstalledError):
    """本机状态库不存在（等价未安装）。"""


class PolicyError(VerifyError):
    """策略过期 / digest 不合法 / 协议不兼容 -> 保守模式。"""


class ReceiptError(DjbError):
    """收据构造 / 签名 / 缓冲错误。"""

    error_type = "invalid_request_error"
    http_status = 400
    exit_code = facts.EXIT_ERROR


class UpdateError(VerifyError):
    """更新包校验 / 安装 / 回滚失败。"""


class PortInUseError(ConflictError):
    """1717 绑定失败：拒绝启动并给改端口指引（M15 §7）。"""


class StateTransitionError(ConflictError):
    """非法状态迁移（M15 §3）。"""


class BudgetExceededError(DjbError):
    """配额 / 时间预算超限（环境检测 ≤10 s、首次运行 ≤3 min）。"""

    error_type = "rate_limit_error"
    http_status = 429
    exit_code = facts.EXIT_ERROR


class FeatureDisabled(UsageError):
    """特性开关关闭（M17 §9：默认关闭；关闭后已有配置/记录只读，不删除）。"""

    def __init__(self, name: str, *, rollback_rule: str | None = None) -> None:
        rule = rollback_rule
        if rule is None:
            spec = facts.FEATURE_FLAGS.get(str(name)) or {}
            rule = spec.get("rollback_rule")
        super().__init__(
            "特性开关 " + str(name) + " 处于关闭状态（默认关闭）："
            + (str(rule) if rule else "先按 M17 §9 演练后再开启"),
            reason="feature_disabled")
        self.flag = str(name)


TYPE_FOR_STATUS: dict[int, str] = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "invalid_request_error",
    403: "permission_error",
    404: "not_found_error",
    409: "conflict_error",
    413: "invalid_request_error",
    424: "authentication_error",
    426: "protocol_error",
    429: "rate_limit_error",
    500: "server_error",
    502: "upstream_error",
    503: "upstream_error",
    504: "upstream_error",
}


def error_type_for_status(status: int) -> str:
    return TYPE_FOR_STATUS.get(int(status), "server_error")


def envelope_for_status(status: int, code: int, message: str, request_id: str,
                        *, reason: str | None = None,
                        retry_after: int | None = None) -> dict[str, Any]:
    """按 HTTP 状态构造 Error 信封（本机回环 API 与出站 mock 共用）。"""
    err: dict[str, Any] = {
        "type": error_type_for_status(status),
        "code": int(code),
        "message": message,
        "request_id": request_id,
    }
    if retry_after is not None:
        err["retry_after"] = int(retry_after)
    if reason is not None:
        err["reason"] = reason
    return {"error": err}


def exit_code_of(exc: BaseException) -> int:
    """异常 -> CLI 退出码（语义见 facts.EXIT_CODE_HELP）。"""
    if isinstance(exc, DjbError):
        return exc.exit_code
    return facts.EXIT_ERROR
