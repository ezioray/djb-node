# SPDX-License-Identifier: Apache-2.0
"""M15 · 常量（唯一来源；业务代码不得再写字面量）。

口径来源：

* 03-facts §11（Owner M15）：阶段 / 自动更新分桶与观测窗 / A2 五字段 / 不做清单。
* M15 §8.1：client.* 配置键的默认值与范围（仅本地展示，不覆盖服务端 policy）。
* M15 §8.2：本模块常量（回环端点、心跳上限、缓冲 24 h、让路 10 s、资源上限、日志 7 天、
  支持矩阵、验收线）。
* spec 1.2.4 / 04-api-contracts：协议版本、头名、错误码段、收据签名（CR-2 已裁决
  hmac-sha256 + 6 字段顺序 + 原样字符串）。
* M02 CR-6（已裁决）：A2 五字段以产品 §5.4.1 为准
  endpoint / model / credential_ref / concurrency_limit / receipt_sample。
"""

# ─────────────────────────────────────────────────────────── 模块 / 版本
MODULE_ID = "M15"
CLIENT_VERSION = "0.1.0"
PROTOCOL_VERSION = "1.0.0"
SPEC_VERSION = "1.2.4"
PLATFORM = "linux"
USER_AGENT = "djb-node/" + CLIENT_VERSION + " (linux)"

# ─────────────────────────────────────────────────────────── 状态机（M15 §3 / §5.9）
DAEMON_STATES: tuple[str, ...] = ("stopped", "starting", "online", "paused", "error", "updating")
"""daemon 运行态（M15 §3 状态机的运行侧投影）。"""

LIFECYCLE_STATES: tuple[str, ...] = (
    "not_installed", "installed", "unbound", "bound", "online", "retired")
"""安装 / 绑定生命周期（M15 §3「未安装 → 已安装 → 未绑定 → 已上线」）。"""

PAUSE_REASONS: tuple[str, ...] = (
    "user_paused", "yield_paused", "resource_limited", "quota_exhausted", "schedule_paused",
    "unbound", "conservative", "receipt_backlog", "upstream_invalid", "credential_invalid",
    "account_frozen", "retired", "update_failed")
"""暂停原因（INV-01：任何「不在赚」的状态都必须给出原因 + 下一步）。"""

RUNTIME_STATE_MAP: dict[str, str] = {
    "online": "online",
    "paused:user_paused": "user_paused",
    "paused:yield_paused": "yield_paused",
    "paused:resource_limited": "yield_paused",
    "paused:quota_exhausted": "quota_exhausted",
    "paused:schedule_paused": "quota_exhausted",
    "paused:unbound": "unbound",
    "paused:conservative": "error",
    "paused:receipt_backlog": "error",
    "paused:upstream_invalid": "error",
    "paused:credential_invalid": "error",
    "paused:account_frozen": "error",
    "paused:retired": "error",
    "paused:update_failed": "error",
    "error": "error",
    "starting": "unbound",
    "updating": "error",
    "stopped": "unbound",
}
"""心跳 runtime_state 的约定值（spec 1.2.0 heartbeat；未知值服务端忽略不报错）。"""

# 允许的状态迁移（M15 §3）。不在表内的迁移一律拒绝（不静默）。
STATE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "stopped": ("starting",),
    "starting": ("online", "paused", "error", "stopped"),
    "online": ("paused", "error", "updating", "stopped"),
    "paused": ("paused", "online", "starting", "error", "stopped", "updating"),
    "error": ("starting", "paused", "stopped"),
    "updating": ("online", "paused", "error"),
}

# ─────────────────────────────────────────────────────────── 本机回环端点（M15 §8.2）
MIRROR_HOST = "127.0.0.1"
"""INV-03：只绑回环。UNSPECIFIED(0.0.0.0) 是禁止值，server.py 会拒绝。"""
MIRROR_PORT = 1717
FORBIDDEN_BIND_HOSTS: tuple[str, ...] = ("0.0.0.0", "", "::", "*", "0:0:0:0:0:0:0:0")
LOCAL_TOKEN_ENV = "DJB_LOCAL_TOKEN"
LOCAL_TOKEN_MIN_BYTES = 32
LOCAL_API_PREFIX = "/v1"
LOCAL_ERROR_CODES = {
    "unauthorized": 40101,      # M15 §4.1（本机令牌无效；码值待 spec 登记，见 CR-M15-07）
    "invalid_request": 40001,
    "quota_exceeded": 40201,
    "rate_limited": 42901,
    "no_upstream": 50901,
    "upstream_timeout": 50902,
    "upstream_error": 50903,
}

# ─────────────────────────────────────────────────────────── 心跳 / 缓冲 / 让路（§8.2）
HEARTBEAT_DEFAULT_S = 30
HEARTBEAT_MAX_S = 60
HEARTBEAT_MIN_S = 5              # 服务端限流：每节点 5 s 一次（M02 facts，客户端不得抢跑）
HEARTBEAT_FAIL_CONSERVATIVE_AFTER = 3
RECEIPT_BUFFER_MAX_AGE_S = 24 * 3600
RECEIPT_BATCH_MAX = 500          # spec ReceiptBatchRequest.maxItems
RECEIPT_BATCH_MIN = 1
YIELD_DRAIN_MAX_S = 10           # 让路在途让出 ≤10 s
DEFAULT_CPU_PCT = 40
DEFAULT_GPU_PCT = 70
DEFAULT_MEM_PCT = 70
RESOURCE_CAPS: dict[str, tuple[int, int, int]] = {
    # key: (默认, 最小, 最大)；用户可调，但不得越过 spec 上限
    "cpu_pct": (DEFAULT_CPU_PCT, 5, 100),
    "gpu_pct": (DEFAULT_GPU_PCT, 10, 100),
    "mem_pct": (DEFAULT_MEM_PCT, 10, 100),
}
LIMIT_DIFF_ALERT_PCT = 1.0       # 本地执行 / 平台记账差异 >1% 告警
ENV_CHECK_BUDGET_S = 10
FIRST_RUN_BUDGET_S = 180

# ─────────────────────────────────────────────────────────── client.* 默认值（§8.1）
PRESETS: tuple[str, ...] = ("省心", "均衡", "全力以赴")
PRESET_CONCURRENCY: dict[str, int] = {"省心": 2, "均衡": 3, "全力以赴": 8}
DEFAULT_PRESET = "省心"
DEFAULT_DAILY_TOKENS = 500_000
DAILY_TOKENS_MIN = 10_000
DAILY_TOKENS_MAX = 100_000_000
DEFAULT_CONCURRENT = 3
CONCURRENT_MIN = 1
CONCURRENT_MAX = 64
DEFAULT_YIELD_ENABLED = True
DEFAULT_UPDATE_CHANNEL = "stable"
UPDATE_CHANNELS: tuple[str, ...] = ("stable", "beta")
DEFAULT_LOG_LEVEL = "info"
LOG_LEVELS: tuple[str, ...] = ("info", "debug")
PORT_MIN = 1024
PORT_MAX = 65535

# ─────────────────────────────────────────────────────────── 日志（§8.2）
LOG_RETENTION_DAYS = 7
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE = "djb.log"

# ─────────────────────────────────────────────────────────── 重试（M15 §7）
RETRY_DELAYS_S: tuple[int, ...] = (1, 5, 30, 300, 1800)
RETRY_MAX_ATTEMPTS = 8
CONSECUTIVE_FAILURES_CONSERVATIVE = 3

# ─────────────────────────────────────────────────────────── 收据签名（M05 §8.2 / CR-2）
RECEIPT_SIGNATURE_ALG = "hmac-sha256"
RECEIPT_SIGNATURE_PREFIX = RECEIPT_SIGNATURE_ALG + ":"
RECEIPT_CANONICAL_FIELDS: tuple[str, ...] = (
    "receipt_id", "task_id", "access", "usage_amount", "usage_unit", "price_amount")
RECEIPT_CANONICAL_SEPARATOR = "\n"
RECEIPT_SIG_HEX_LEN = 64
RECEIPT_KEY_MIN_BYTES = 32

# ─────────────────────────────────────────────────────────── 上游接入（M15 §1 / §8.2）
ACCESS_TYPES: tuple[str, ...] = ("A1", "A2", "B")
A2_FIVE_FIELDS: tuple[str, ...] = (
    "endpoint", "model", "credential_ref", "concurrency_limit", "receipt_sample")
"""M02 CR-6 裁决：A2 五字段以产品 §5.4.1 为准。"""
A2_PROMPT_SECTIONS: tuple[str, ...] = ("目标", "约束", "输入", "输出", "验收")
"""03-facts §11：A2 prompt 模板五字段（目标 / 约束 / 输入 / 输出 / 验收）。"""
UPSTREAM_STATES: tuple[str, ...] = ("declared", "probing", "verified", "rejected")
DEFAULT_UPSTREAM_PROTOCOL = "openai-chat"
DEFAULT_MIN_PRICE_UNIT = "usd_per_1m_tokens"
TOKEN_RELAY_KIND = "token_relay"

# ─────────────────────────────────────────────────────────── 更新（03-facts §11 / §8.2）
UPDATE_BUCKET_WAVES: tuple[int, ...] = (5, 25, 100)
UPDATE_BUCKET_OBSERVE_S = 24 * 3600
UPDATE_KEEP_VERSIONS = 2
UPDATE_SIGNATURE_PREFIX = "hmac-sha256:"
UPDATE_SIGNATURE_ALG = "hmac-sha256"

# ─────────────────────────────────────────────────────────── 支持矩阵（§8.2）
MIN_UBUNTU = (22, 4)
MIN_DEBIAN = (12, 0)
MIN_KERNEL = (5, 10)
SUPPORTED_ARCHES: tuple[str, ...] = ("x86_64", "aarch64")

# ─────────────────────────────────────────────────────────── 策略（INV-06）
POLICY_DEFAULT_TTL_S = 3600
"""spec NodePolicy 无 expires_at（M15 §3 要求）；本地退化为 issued_at + TTL。见 CR-M15-05。"""
POLICY_MAX_CLOCK_SKEW_S = 300

# ─────────────────────────────────────────────────────────── 退出码（CLI 语义）
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_INSTALLED = 3
EXIT_NOT_RUNNING = 4
EXIT_AUTH = 5
EXIT_NETWORK = 6
EXIT_VERIFY = 7
EXIT_CONFLICT = 8

EXIT_CODE_HELP: dict[int, str] = {
    0: "成功",
    1: "未分类错误（详见 stderr / djb logs）",
    2: "命令用法错误",
    3: "未初始化 / 未安装（先 djb init）",
    4: "daemon 未运行",
    5: "凭据缺失 / 无效（先 djb auth）",
    6: "网络或上游不可达（可重试）",
    7: "校验失败（签名 / 哈希 / 策略）",
    8: "状态冲突（端口占用、状态迁移不允许等）",
}

# ─────────────────────────────────────────────────────────── 诊断包（§9 验收）
DIAGNOSE_BUNDLE_PREFIX = "djb-diagnose-"
DIAGNOSE_SCAN_PATTERNS: tuple[str, ...] = (
    r"sk-[A-Za-z0-9_\-]{16,}",
    r"DJB_[A-Z_]*KEY[=:][^\s\"']+",
    r"hmac-sha256:[0-9a-f]{64}",
    r"Bearer\s+[A-Za-z0-9._\-]{16,}",
)
