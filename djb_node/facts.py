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
#: 对外申报的客户端版本 = 包版本（单一事实来源）。
#: 修前这里硬编码 "0.1.0"，而 ``djb --version`` 打的是 ``__version__``=0.2.0：
#: 用户看到的版本、云端心跳里的版本、安装记录里的版本会各说各话（M10「版本如实」）。
def _package_version() -> str:
    try:
        from . import __version__ as version       # 包自身，不会循环导入
        return str(version)
    except Exception:                              # pragma: no cover - 极端打包场景
        return "0.0.0"


CLIENT_VERSION = _package_version()
PROTOCOL_VERSION = "1.0.0"
SPEC_VERSION = "1.2.4"


def _detect_platform() -> str:
    """当前 OS 的平台标识（linux / darwin / windows / other）。

    M10 三端：原来这里硬编码 `"linux"`，导致 `update.select_artifact` /
    `store.update_install` 在 Windows / macOS 上按 linux 选包（更新永远「没有匹配的
    安装包」）。平台判定只读 `sys.platform` 等，不产生副作用；判定失败保持 linux
    （S1 行为不变）。
    """
    try:
        from .platform import get_platform
        return get_platform().os
    except Exception:                                    # pragma: no cover - 极端环境
        return "linux"


PLATFORM = _detect_platform()
USER_AGENT = "djb-node/" + CLIENT_VERSION + " (" + PLATFORM + ")"

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
"""旧口径（对称 HMAC）。**只允许 test-only mock 分支使用**（见 `UPDATE_MOCK_FLAG`）。"""
UPDATE_SIGNATURE_ALG = "hmac-sha256"
UPDATE_ED25519_PREFIX = "ed25519:"
UPDATE_SIGNATURE_ALG_ED25519 = "ed25519"

# G10：更新链路的**信任锚是内置公钥**（私钥只在发布侧 / E2E 的
# `~/.djb/secrets/e2e/update-signing.key` 0600，客户端永远不持有）。
# 原来 `UpdateManager.signing_key()` 会在本地随机自生成一把 HMAC 密钥并自动使用——
# 那等于「客户端自己造信任锚」，任何拿到本机文件的人都能签出「合法」更新（审计 P1-3）。
UPDATE_PUBLIC_KEY_HEX = "cecc3a253872f8e60b630d57d94e96433aecdfb227d49b74605cc5925e0add73"
"""内置 Ed25519 公钥（32 字节，hex）。公钥可入仓；私钥不入仓、不入对话、不进诊断包。"""
UPDATE_KEY_ID = "djb-e2e-2026-11-02"
"""公钥标识（轮换时改这里 + `UPDATE_PUBLIC_KEY_HEX`；写进 status / 诊断包便于对账）。"""
UPDATE_PUBLIC_KEY_FINGERPRINT = "30171de47e02"
"""公钥指纹 sha256(raw pubkey)[:12]，与安全席登记的指纹一致（status 里如实展示）。"""
UPDATE_TEST_ENV = "DJB_ENV"
UPDATE_TEST_ENV_VALUE = "test"
"""test-only mock 验签的环境门：必须显式 `DJB_ENV=test`。"""
UPDATE_MOCK_KEY_ENV = "DJB_UPDATE_SIGNING_KEY"
"""test-only mock 的对称密钥（hex）。**不设默认值、不落盘、不自动生成**。"""
UPDATE_MOCK_FLAG = "client.update.mock_signature.enabled"
"""test-only mock 验签开关（登记项见本文件下方的 FEATURE_FLAGS；三字段齐全、默认关闭）。"""

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

# ─────────────────────────────────────────────────────────── token 计数（C8；PROBE §2）
# 现状：客户端只读上游 usage，缺失时平台 len//4 估算——中文低估约 4 倍
# （内部审计记录 §2.2，不随公开仓发布）。
# 本组常量是 tokens.py 的唯一来源；业务代码不得再写字面量。
TOKENIZER_HEURISTIC_ID = "heuristic:v1"
"""没有可用分词器时的启发式估算器标识（estimated=True）。"""
TOKENIZER_TIKTOKEN_PREFIX = "tiktoken:"
"""已安装 tiktoken（optional extra `tokens`）时的 tokenizer_id 前缀：prefix + encoding 名。"""
TOKEN_CHAT_MESSAGE_OVERHEAD = 4
"""chat template 粗略开销：每条消息固定 +4（role / 分隔控制符）。"""
TOKEN_CHAT_REPLY_PRIMING = 3
"""回复预备开销：assistant 起手控制符 +3（最后一次 template 收尾）。"""
TOKEN_IMAGE_PART_OVERHEAD = 85
"""多模态 image_url 片段按「低清图 85 token」估算（无本地解码能力，不猜测尺寸）。"""
TOKEN_HEURISTIC_ASCII_CHARS_PER_TOKEN = 4
"""启发式：ASCII 字母 / 数字 / 空白 ≈ 4 字符 1 token（英文经验值）。"""
TOKEN_HEURISTIC_ASCII_SYMBOL_CHARS_PER_TOKEN = 2
"""启发式：ASCII 标点 / 符号 ≈ 2 字符 1 token（BPE 对符号压缩差）。"""
TOKEN_HEURISTIC_CJK_TOKENS_PER_CHAR = 1
"""启发式：CJK（中日韩 + 全角）≈ 1 token / 字；校准依据 PROBE §2.2 的 4 倍低估。"""
TOKEN_HEURISTIC_OTHER_TOKENS_PER_CHAR = 1
"""启发式：其它非 ASCII（西里尔 / 重音拉丁 / emoji）≈ 1 token / 字（宁高不低）。"""
TOKEN_COMPARE_THRESHOLD_PCT = 1.0
"""本地 token 计数 vs 上游 usage 的告警阈值（%）：严格大于才记（对齐 LIMIT_DIFF_ALERT_PCT）。"""
TOKEN_OPENAI_MODEL_PREFIXES: tuple[str, ...] = (
    "gpt-", "chatgpt-", "o1", "o3", "o4", "text-embedding-", "davinci", "curie", "babbage",
)
"""视为 OpenAI 分词族（tiktoken 有意义的模型族）的模型名前缀。"""
TOKEN_TIKTOKEN_ENCODINGS: dict[str, str] = {
    # 顺序有意义：长前缀在前（gpt-4o 先于 gpt-4）
    "gpt-4o": "o200k_base",
    "gpt-4.1": "o200k_base",
    "gpt-4.5": "o200k_base",
    "chatgpt-4o": "o200k_base",
    "gpt-5": "o200k_base",
    "o1": "o200k_base",
    "o3": "o200k_base",
    "o4": "o200k_base",
    "text-embedding-3": "cl100k_base",
    "text-embedding-ada": "cl100k_base",
    "gpt-4": "cl100k_base",
    "gpt-3.5": "cl100k_base",
    "gpt-35": "cl100k_base",
    "davinci": "cl100k_base",
    "curie": "cl100k_base",
    "babbage": "cl100k_base",
}
"""模型名前缀 → tiktoken encoding 名（只覆盖已确认映射；未命中走 tiktoken 自带查表）。"""

# ─────────────────────────────────────────────────────────── M17 · supply-integrity（C11 relay / C12 attest）
# 口径来源：product/v4/modules/M17-supply-integrity.md §4（relay）、§5.1–5.3（L0–L5 / tokenizer）、
# §8（接口只加不改）、§9（开关三字段）。只加不改：本段不修改任何既有常量。
UPSTREAM_KINDS: tuple[str, ...] = (
    "official_api", "local_agent", "enterprise_gateway", "third_party_relay")
"""M17 §4.1：可选 upstream_kind（老服务端忽略未知值）。"""
RELAY_KIND = "third_party_relay"

RELAY_FRESH_S = 900
"""C9 接线：relay 发现结论的新鲜度窗口（秒），与服务端 ``PROBE_FRESH_S=900`` 同口径。

口径来源：`app/supply/facts.py` 的「容量公告只认 ≥ now-900s 的 verified」；
relay 以「最近一次成功发现」代替 L2 探针，过期即 ``max_qps=0``（保留条目，便于审计）。
"""

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

# ─────────────────────────────────────────────────────────── 客户端模型探针（M17 §4.5 / C7）
PROBE_LEVELS: tuple[str, ...] = ("L1", "L2", "L3")
"""L1 = GET /models（upstream.probe_upstream）；L2 = 非流式 chat；L3 = 流式 chat + usage。"""
PROBE_DEFAULT_LEVEL = "L3"
"""默认做 L3（流式）；流式失败仍保留 L2 结论（降级不丢验证）。"""
PROBE_VERSION = "c7.1"
"""探针版本（本地字段；spec 1.2.7 未定义 probe_version，不上行）。"""
PROBE_PROMPT = "djb-probe"
"""合成 prompt：编译期常量，**不含任何用户正文**（M17 §10 / T9）。"""
PROBE_MAX_TOKENS = 1
PROBE_TIMEOUT_S = 5.0
PROBE_FRESH_S = 15 * 60
"""last_probe_at 新鲜窗口：超过即不进容量公告（默认 15 min）。"""
PROBE_PERIODIC_DEFAULT_S = 10 * 60
PROBE_PERIODIC_MAX_S = 6 * 3600
PROBE_MODELS_PER_STEP = 8
"""模型数退避：每多 8 个模型，周期复探间隔再加一档。"""
PROBE_MAX_BACKOFF_SHIFT = 5
"""失败指数退避上限：2**5 = 32 倍（再乘上限截断）。"""
PROBE_PASS_BUDGET_S = 10
"""单轮探针预算（秒）：启动校验与周期复探共用，超出即把剩余模型留到下一轮。"""
PROBE_NOT_PROBED = "not_probed"
"""预算内没来得及探的模型：不算失败，但不进容量公告。"""

# ─────────────────────────────────────────────────────────── 特性开关（默认关闭 + 三字段）
# 口径（Lead 集成）：FEATURE_FLAGS 是共享登记表；本卡只追加 client.token.compare.enabled。
# 条目形状统一为 default / owner / expire_at / rollback_rule / description。
# 求值逻辑唯一实现在 config.feature_flag（C7）；facts 这里只存元数据，不做解析。
TOKEN_COMPARE_FLAG = "client.token.compare.enabled"
"""真实请求路径「本地计数 vs 上游 usage」差异检测开关（默认关闭，见 FEATURE_FLAGS）。"""
TOKEN_COMPARE_FLAG_META: dict[str, object] = {
    "default": False,
    "owner": "client-seat",
    "expire_at": "2026-12-31",
    "rollback_rule": "关闭后只记录上游 usage、不做本地比较",
    "description": "真实请求路径做本地 token 计数与上游 usage 的差异检测（>1% 记 token_mismatch）",
}
FEATURE_FLAGS: dict[str, dict[str, object]] = {}
"""特性开关登记表：每个开关必须带 default（默认关闭）/ owner / expire_at / rollback_rule /
description。C7 也向本表追加条目，合并时由 Lead 统一为一个 dict。"""
FEATURE_FLAGS[TOKEN_COMPARE_FLAG] = TOKEN_COMPARE_FLAG_META

# ── M17 §9 开关（Lead 合并：与 C8 共用同一个 FEATURE_FLAGS 登记表）
FEATURE_FLAGS["client.relay.enabled"] = {
    "owner": "client-seat",
    "expire_at": "2026-12-31",
    "rollback_rule": "关闭 → 隐藏 relay 接入；已有配置只读、不删除",
    "default": False,
    "description": "第三方中转站接入（M17 §4 / C11），默认关闭",
}
FEATURE_FLAGS["supply.attestation.enabled"] = {
    "owner": "platform",
    "expire_at": "2026-12-31",
    "rollback_rule": "关闭 → 回退 L2 可达性判定；已有路由/账本不变；已存证明保留只读",
    "default": False,
    "description": "模型身份证明（M17 §5 / C12 客户端侧线索采集），默认关闭",
}
RELAY_FLAG = "client.relay.enabled"
ATTEST_FLAG = "supply.attestation.enabled"
FLAG_REQUIRED_FIELDS: tuple[str, ...] = ("owner", "expire_at", "rollback_rule", "default")

# ── C7 周期复探开关（与 C8/M17 共用同一 FEATURE_FLAGS 登记表）
FEATURE_FLAGS["client.probe.periodic.enabled"] = {
    "default": False,
    "owner": "client-seat",
    "expire_at": "2026-12-31",
    "rollback_rule": "关闭后只保留手动 probe（djb upstream probe）；"
                     "公告按已存 last_probe_at 判新鲜",
    "description": "客户端周期复探（L2/L3），默认关闭",
}
PROBE_FLAG_PERIODIC = "client.probe.periodic.enabled"

# ─────────────────────────────────────────────────────────── C10c 出站长轮询通道
# 口径：客户端不开放入站端口，主动长轮询平台取 job → 调本机 127.0.0.1 镜像端点执行 →
# 回传结果。默认关闭；关闭时 daemon 不起线程，行为与无通道版本完全一致。
FEATURE_FLAGS["client.channel.enabled"] = {
    "default": False,
    "owner": "client-seat",
    "expire_at": "2026-12-31",
    "rollback_rule": "关闭后停长轮询、退回无通道模式；已取 job 尽力回传后停止",
    "description": "客户端出站长轮询通道（C10c）：平台下发 job、本机执行、回传结果，默认关闭",
}
CHANNEL_FLAG = "client.channel.enabled"
CHANNEL_POLL_PATH = "/api/v1/node/channel/poll"
"""平台长轮询端点（spec 侧由 C10a/b 落地；客户端只消费）。"""
CHANNEL_RESULT_PATH = "/api/v1/node/channel/result"
CHANNEL_POLL_WAIT_S = 25
"""长轮询 wait_s（服务端最多挂起该秒数；204 = 无 job）。"""
CHANNEL_POLL_TIMEOUT_S = 35
"""poll 的客户端超时：wait_s + 10s 网络余量。"""
CHANNEL_BACKOFF_S: tuple[int, ...] = (1, 5, 30)
"""poll 失败退避序列（秒）：用尽后停在最后一档，不退出。"""
CHANNEL_RESULT_TIMEOUT_S = 20
CHANNEL_RESULT_ATTEMPTS = 2
"""结果回传尝试次数（幂等键 = job_id；重复提交由服务端判 duplicate）。"""
CHANNEL_RESULT_RETRY_S = 1
CHANNEL_JOB_TIMEOUT_DEFAULT_S = 120
"""job 未给 timeout_s 时的默认上限。"""
CHANNEL_JOB_TIMEOUT_MAX_S = 300
"""本地执行上限：超过即截断（单在途，不能把 poll 主循环拖死）。"""
CHANNEL_JOB_TIMEOUT_MIN_S = 1
CHANNEL_BODY_FIELDS: tuple[str, ...] = ("model", "messages", "max_tokens", "temperature",
                                        "stop")
"""转发给本机镜像端点的字段白名单（不透传未知字段，避免把平台注入物带到上游）。"""
CHANNEL_STOP_JOIN_S = 5
"""daemon 停止时等待通道线程的秒数。"""
CHANNEL_STATUS_KINDS: tuple[str, ...] = ("idle", "polling", "executing", "stopping")

# ─────────────────────────────────────────────────────────── M18b 客户端模型 / tokenizer 同步
# 口径：客户端拉取服务端**签名 bundle**（版本 + payload_hash + TTL），校验后热应用本地
# 模型元数据；无法本地支持的 tokenizer → unsupported，**不伪造精确 tokenizer、不装依赖**。
# 失败一律保留旧版（M18 §5「签名失败/回退版本 → 保留旧版并上报 sync_rejected」）。
FEATURE_FLAGS["client.model_sync.enabled"] = {
    "default": False,
    "owner": "client-seat",
    "expire_at": "2026-12-31",
    "rollback_rule": "关闭后停拉取、保留已应用版本（本地 model_sync.json 只读不删）",
    "description": "客户端模型/tokenizer 同步（M18b）：拉签名 bundle、校验后热应用，默认关闭",
}
MODEL_SYNC_FLAG = "client.model_sync.enabled"
MODEL_CATALOG_PATH = "/api/v1/node/model-catalog"
MODEL_SYNC_STATE_PATH = "/api/v1/node/model-sync/state"
MODEL_SYNC_FILE = "model_sync.json"
"""本地已应用 bundle（原子写、0600；只存元数据，不存正文/凭据）。"""
MODEL_SYNC_SCHEMA = 1
MODEL_SYNC_INTERVAL_S = 15 * 60
"""周期拉取间隔（M18 §5：启动 + 每 15 min）。"""
MODEL_SYNC_BACKOFF_S: tuple[int, ...] = (60, 300, 900, 1800)
"""拉取失败退避（秒）：用尽停在最后一档，不退出、不影响接单。"""
MODEL_SYNC_TIMEOUT_S = 20
MODEL_SYNC_STATE_TIMEOUT_S = 15
MODEL_SYNC_STOP_JOIN_S = 5
MODEL_SYNC_FIELDS: tuple[str, ...] = ("vendor", "canonical", "aliases", "context_window",
                                      "tokenizer_family", "tokenizer_version",
                                      "tokenizer_hash", "effective_at")
"""bundle 里每个模型必须携带的字段（缺 canonical 的条目整条丢弃）。"""
MODEL_SYNC_KNOWN_TOKENIZERS: tuple[str, ...] = ("tiktoken", "openai", "cl100k_base",
                                               "o200k_base", "heuristic")
"""本地**有实现**的 tokenizer family（其余一律 unsupported，计数标 estimated）。"""
MODEL_SYNC_ERROR_CLASSES: tuple[str, ...] = (
    "version_invalid", "version_regression", "hash_mismatch", "expired",
    "signature_invalid", "payload_invalid", "not_modified", "fetch_failed",
)
MODEL_SYNC_DRIFT_FIELDS: tuple[str, ...] = ("tokenizer_family", "tokenizer_version",
                                            "tokenizer_hash", "context_window")
"""漂移只比这几项（其余字段变化不算漂移，避免噪声）。"""

# ─────────────────────────────────────────────────────────── C16 客户端消息中心
# 口径：服务端下发**站内信**（severity/category/template_key/payload），客户端本地存一份，
# 支持未读、已读、ack、离线补拉与离线读态重试；**只存元数据，不存正文/凭据**。
# 默认关闭；S2 测试网用环境变量 DJB_NOTIFY=1 显式激活（不做 GUI，只提供数据层）。
FEATURE_FLAGS["client.notifications.enabled"] = {
    "default": False,
    "owner": "client-seat",
    "expire_at": "2026-12-31",
    "rollback_rule": "关闭后停拉取、保留本地消息与待补发读态（本地只读不删）",
    "description": "客户端消息中心（C16）：拉取站内信、未读/已读/ack、离线补拉，默认关闭",
}
# M3：把机器画像（CPU/内存/磁盘/GPU/tokens-s）加性地带进心跳，供云端算力视图展示。
# 纯加性字段（cloud 侧忽略未知键），但**默认关**：先由 E2E 显式打开验证，再谈默认开。
MACHINE_PROFILE_FLAG = "client.machine_profile.report"
FEATURE_FLAGS[MACHINE_PROFILE_FLAG] = {
    "default": False,
    "owner": "client-seat",
    "expire_at": "2027-06-30",
    "rollback_rule": "关闭即心跳不再携带 machine 字段（云端忽略该字段，历史数据无需迁移）；"
                     "本地 djb machine / djb status / 诊断包展示不受影响",
    "description": "心跳上报机器画像（加性字段 machine：cpu/memory/disk/gpu/benchmark 摘要）",
}

# G10：更新验签的 test-only mock 分支（默认关闭；必须 DJB_ENV=test + 显式密钥）
FEATURE_FLAGS[UPDATE_MOCK_FLAG] = {
    "default": False,
    "owner": "client-seat",
    "expire_at": "2026-12-31",
    "rollback_rule": "关闭即回到内置公钥验签（唯一信任锚）；mock 不产生任何持久状态，"
                     "已安装版本与回滚点不变",
    "description": "自动更新验签的 test-only HMAC mock（必须同时满足 DJB_ENV=test + "
                   "显式 DJB_UPDATE_SIGNING_KEY）；默认关闭，生产永远走内置 Ed25519 公钥",
}

NOTIFICATIONS_FLAG = "client.notifications.enabled"
NOTIFICATIONS_ENV = "DJB_NOTIFY"
"""S2 测试网显式激活开关（值 "1" 生效；不改变 facts 里的默认关闭定义）。"""
NOTIFICATIONS_PATH = "/api/v1/node/notifications"
# G4：路径口径以 spec 1.2.13 / openapi.yaml 为准 —— `/api/v1/node/notifications/unread_count`
# 是**下划线**。原来写成 `unread-count`（连字符）→ 服务端 100% 404 → 未读角标永远像
# 「没有新消息」。同文件的 read / ack 两条路径本来就对得上，只有这一条是手写常量漏测。
NOTIFICATIONS_UNREAD_PATH = "/api/v1/node/notifications/unread_count"
NOTIFICATIONS_UNREAD_FIELDS: tuple[str, ...] = ("unread", "unread_count")
"""未读数响应字段（按优先级读取）。

spec `NodeNotificationUnread` 的 required 字段是 ``unread``（cloud 实际就回 `{"unread": n}`）；
``unread_count`` 作为 tolerant reader 的回退（早期客户端口径 / 旧网关）。两者都没有 → None，
**不猜 0**（猜 0 等于把「拿不到」伪装成「没有新消息」，正是 G4 的用户可感知症状）。
"""
NOTIFICATIONS_DB = "notifications.db"
"""本地消息库（独立 SQLite 文件，不动 state.db 的 schema 版本）。"""
NOTIFICATIONS_SCHEMA_VERSION = 1
NOTIFICATIONS_DEFAULT_LIMIT = 50
NOTIFICATIONS_MAX_LIMIT = 200
NOTIFICATIONS_SYNC_TIMEOUT_S = 20
NOTIFICATIONS_ACTION_TIMEOUT_S = 15
NOTIFICATIONS_INTERVAL_S = 15 * 60
"""周期同步间隔（启动即拉一次，之后每 15 min）。"""
NOTIFICATIONS_BACKOFF_S: tuple[int, ...] = (60, 300, 900, 1800)
NOTIFICATIONS_STOP_JOIN_S = 5
NOTIFICATIONS_RETENTION_DAYS = 180
"""本地保留天数（超过即 prune；服务端另有保留策略）。"""
NOTIFICATIONS_MAX_PENDING = 500
"""离线读态队列上限（超过丢最旧，避免无界增长）。"""
NOTIFICATIONS_SEVERITIES: tuple[str, ...] = ("P0", "P1", "P2")
NOTIFICATIONS_ACTIONS: tuple[str, ...] = ("read", "ack")

# UI-NODE: local tray control is opt-in; expiry enforced at every action boundary.
FEATURE_FLAGS["client.tray.control.enabled"] = {
    "default": False,
    "owner": "client-seat",
    "expire_at": "2026-12-31",
    "rollback_rule": "关闭后托盘暂停/恢复退回命令指南；不改变 daemon 状态，不写离线偏好",
    "description": "托盘用户确认后暂停/恢复；先读状态、独立回读、到期强制关闭",
}

# IPC-WIN-001: opt-in local named-pipe transport; runtime checks enforce UTC expiry.
FEATURE_FLAGS["client.ipc.windows.enabled"] = {
    "default": False,
    "owner": "client-seat",
    "expire_at": "2026-12-31",
    "rollback_rule": "关闭Windows命名管道控制并显示不可用；不退TCP/不更改daemon运行偏好",
    "description": "Windows同SID/session/integrity安全命名管道；不解禁GUI托盘控制",
}

# ─────────────────────────────────────────────────────────── 多 plan 组（组内自动切换 + 组级预算）
# 口径：一条上游 = 一个 plan。多个 plan 先分组（组级共享预算，日/月上限，NULL=不限），
# 组按 priority 升序，组内按成员 priority 升序（同档按 weight 加权随机）依次尝试；
# 组预算耗尽 / 全部失败 → 用下一组。**默认关闭**；关闭时 /v1/chat/completions 走原来的
# 单上游 route() 路径，行为与接入前一致（plan_router 都不会被调用）。
FEATURE_FLAGS["plan.groups"] = {
    "default": False,
    "owner": "plan-groups",
    "expire_at": "2027-06-30",
    "rollback_rule": "关闭（默认即关）→ /v1/chat/completions 退回单上游 route() 路径，"
                     "plan_router 不被调用、不记账；plan_groups / plan_group_members 表与"
                     " spent_* / unknown_usage_count 数据只读保留，不删除、不改写；"
                     "本地库版本只前进（v4），回退安装旧客户端会被拒绝，故回滚动作只关开关",
    "description": "多 plan 组：组内按 priority 自动切换、组级共享日/月预算，默认关闭",
}
PLAN_GROUPS_FLAG = "plan.groups"
# 注意：尝试次数上限与冷却秒数只在 djb_node/plan_router.py 里定义一份
# （MAX_ATTEMPTS / AUTH_COOLDOWN_S），这里不再复制，避免两处漂移。
