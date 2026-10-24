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

# ─────────────────────────────────────────────────────────── M17 · supply-integrity（C11 relay / C12 attest）
# 口径来源：product/v4/modules/M17-supply-integrity.md §4（relay）、§5.1–5.3（L0–L5 / tokenizer）、
# §8（接口只加不改）、§9（开关三字段）。只加不改：本段不修改任何既有常量。
UPSTREAM_KINDS: tuple[str, ...] = (
    "official_api", "local_agent", "enterprise_gateway", "third_party_relay")
"""M17 §4.1：可选 upstream_kind（老服务端忽略未知值）。"""
RELAY_KIND = "third_party_relay"

RELAY_AUTH_SCHEMES: tuple[str, ...] = ("bearer", "x-api-key", "api-key", "custom")
"""M17 §4.3：auth_scheme；custom 必须显式给 header_name。"""
RELAY_AUTH_HEADER: dict[str, str] = {
    "bearer": "Authorization", "x-api-key": "X-Api-Key", "api-key": "Api-Key"}
RELAY_AUTH_PREFIX: dict[str, str] = {"bearer": "Bearer ", "x-api-key": "", "api-key": ""}
RELAY_FORBIDDEN_HEADERS: tuple[str, ...] = (
    "host", "authorization", "content-length", "transfer-encoding", "connection",
    "cookie", "set-cookie", "proxy-authorization", "proxy-authenticate", "upgrade",
    "te", "trailer")
"""extra_headers 永不可覆盖的传输/认证头（Host / Authorization 冲突一律拒绝）。"""
RELAY_EXTRA_HEADER_ALLOWLIST: tuple[str, ...] = (
    "x-title", "x-app", "x-app-name", "x-client", "x-client-name", "x-request-id",
    "x-api-version", "anthropic-version", "anthropic-beta", "openai-beta",
    "x-stainless-lang", "x-stainless-package-version")
"""M17 §4.3：extra_headers 白名单（小写比较，未列入即拒绝）。"""
RELAY_SECRET_QUERY_PARAMS: tuple[str, ...] = (
    "key", "api_key", "apikey", "api-key", "token", "access_token", "auth", "secret",
    "sig", "signature", "password", "passwd", "credential")
"""URL query 里出现这些参数名即视为「key 进 URL」→ 拒绝（M17 §4.3）。"""
RELAY_LOCAL_HOSTS: tuple[str, ...] = ("127.0.0.1", "::1", "localhost")
"""只对回环允许 http://；其余 relay 端点必须 https://。"""
RELAY_TIMEOUT_DEFAULTS: dict[str, float] = {"connect": 5.0, "first_byte": 30.0, "total": 120.0}
RELAY_TIMEOUT_MAX_S = 600.0
RELAY_MAX_EXTRA_HEADERS = 16
RELAY_HEADER_VALUE_MAX_LEN = 256
RELAY_PROFILE_KEY_PREFIX = "relay:"
RELAY_INDEX_KEY = "relay:index"
RELAY_PROFILE_SCHEMA = 1
RELAY_MODEL_STATES: tuple[str, ...] = ("mapped", "unmapped", "rejected")
"""unmapped = 未映射：不公告、不接单（M17 §4.4）。"""

RELAY_VENDOR_HINTS: dict[str, str] = {
    # 模型 id 前缀 → canonical vendor（最长前缀优先）。只用于「能归一就自动映射」。
    "gpt": "openai", "o1": "openai", "o3": "openai", "o4": "openai",
    "chatgpt": "openai", "text-embedding": "openai", "dall-e": "openai",
    "claude": "anthropic", "gemini": "google", "gemma": "google",
    "deepseek": "deepseek", "qwen": "qwen", "qwq": "qwen",
    "glm": "zhipu", "chatglm": "zhipu", "kimi": "moonshot", "moonshot": "moonshot",
    "llama": "meta", "mistral": "mistral", "mixtral": "mistral", "grok": "xai",
}
RELAY_CANONICAL_CANDIDATE_LIMIT = 5

# ── C12 attestation（§5.1–5.3）
ATTEST_LEVELS: tuple[str, ...] = ("L0", "L1", "L2", "L3", "L4", "L5")
ATTEST_LEVEL_NAMES: dict[str, str] = {
    "L0": "declared", "L1": "reachable", "L2": "inference",
    "L3": "tokenizer", "L4": "behavioral", "L5": "provider"}
ATTEST_LEVEL_METHOD: dict[str, str] = {
    "L1": "l1_models", "L2": "l2_inference", "L3": "l3_tokenizer",
    "L4": "behavioral", "L5": "provider"}
"""等级 → 证据方法（§4.5 / §5.2）。"""
ATTEST_LEVEL_TTL_S: dict[str, int] = {
    "L0": 0, "L1": 24 * 3600, "L2": 24 * 3600, "L3": 7 * 86400,
    "L4": 30 * 86400, "L5": 90 * 86400}
"""M17 §5.7：L1/L2 24h、L3 7d、L4 30d、L5 90d（可配，不硬编码在业务里）。"""
ATTEST_STATES: tuple[str, ...] = (
    "declared", "reachable", "inference", "tokenizer", "behavioral", "provider",
    "failed", "expired", "suspended")
ATTEST_BLOCKING_STATES: tuple[str, ...] = ("failed", "expired", "suspended")
"""这些状态下一律不公告、不接单（与等级无关）。"""
ATTEST_MIN_SERVICE_LEVEL = "L3"
"""§5.1：容量公告与调度只认 L3+；S2 付费流量最低门槛。"""
ATTEST_TRUST = "client_side_clue"
"""§1 / §11：客户端证据只作线索，不得自我宣称 verified。"""
ATTEST_TOKENIZER_TOLERANCE_PCT = 1.0
ATTEST_TOKENIZER_TOLERANCE_TOKENS = 1
"""§5.1 / §5.3：容差 ±1 token 或 1%，取较大者（整数化 = ceil）。"""
ATTEST_TOKENIZER_RESULTS: tuple[str, ...] = ("match", "mismatch", "unknown")
ATTEST_EVIDENCE_RESULTS: tuple[str, ...] = ("ok", "match", "mismatch", "unknown", "error")
ATTEST_EVIDENCE_MAX_KEPT = 20
ATTEST_NONCE_TTL_S = 24 * 3600
ATTEST_NONCE_MAX_ENTRIES = 4096
ATTEST_KEY_PREFIX = "attest:"
ATTEST_INDEX_KEY = "attest:index"
ATTEST_NONCE_KEY = "attest:nonces"
ATTEST_RECORD_SCHEMA = 1
ATTEST_TOKENIZER_HINTS: dict[str, str] = {
    # 本地已知 tokenizer 家族指纹（最长前缀优先）；拿不到就 unknown，不猜。
    "gpt-4o": "openai:o200k", "gpt-4.1": "openai:o200k", "o1": "openai:o200k",
    "o3": "openai:o200k", "o4": "openai:o200k",
    "gpt-4": "openai:cl100k", "gpt-3.5": "openai:cl100k",
    "claude": "anthropic:claude", "gemini": "google:sentencepiece",
    "deepseek": "deepseek:bbpe", "qwen": "qwen:bbpe", "glm": "zhipu:bbpe",
    "kimi": "moonshot:bbpe", "llama": "meta:llama3", "mistral": "mistral:tekken",
}
ATTEST_COUNTER_CANDIDATES: tuple[str, ...] = ("count_prompt_tokens", "count_tokens")
"""惰性 import djb_node.tokens（C8 写域）时尝试的函数名；缺失即降级 unknown。"""

# ── §9 特性开关（三字段齐全 + default，默认关闭；客户端侧只用这两条）
# Lead 集成口径：开关只登记在 facts.FEATURE_FLAGS，取值统一走 config.feature_flag()。
# C7（client.probe.periodic.enabled）与 C8 的条目由 Lead 合并本段，不要在这里重定义他人开关。
FEATURE_FLAGS: dict[str, dict] = {
    "client.relay.enabled": {
        "owner": "client-seat",
        "expire_at": "2026-12-31",
        "rollback_rule": "关闭 → 隐藏 relay 接入；已有配置只读、不删除",
        "default": False,
        "description": "第三方中转站接入（M17 §4 / C11），默认关闭",
    },
    "supply.attestation.enabled": {
        "owner": "lead",
        "expire_at": "2026-12-31",
        "rollback_rule": "关闭 → 回退 L2 可达性判定；已有路由/账本不变；已存证明保留只读",
        "default": False,
        "description": "模型身份证明（M17 §5 / C12 客户端侧线索采集），默认关闭",
    },
}
"""特性开关（M17 §9：owner / expire_at / rollback_rule / default，默认关闭）。"""
RELAY_FLAG = "client.relay.enabled"
ATTEST_FLAG = "supply.attestation.enabled"
FLAG_REQUIRED_FIELDS: tuple[str, ...] = ("owner", "expire_at", "rollback_rule", "default")
