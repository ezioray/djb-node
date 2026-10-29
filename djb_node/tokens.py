# SPDX-License-Identifier: Apache-2.0
"""C8 · tokenizer 注册表 + 本地 token 计数 + 上游 usage 差异判定。

背景见内部审计记录（不随公开仓发布）：

* 客户端此前**没有分词器**，只读上游 `usage`；缺失时平台用 ``len//4`` 估算，
  中文低估约 4 倍（100 汉字 → 估 25，实际约 100）。
* 本模块提供**本地**计数能力，作为 usage 缺失时的兜底与「本地 vs 上游」对账基准。

分级策略：

===========  ==========================================  =============
优先级        条件                                        tokenizer_id
===========  ==========================================  =============
1（真分词）   OpenAI 系模型 且 已装 tiktoken（optional） ``tiktoken:<encoding>``
2（启发式）   其余一切情况                                ``heuristic:v1``
===========  ==========================================  =============

* 只有 OpenAI 系模型才用 tiktoken：把 cl100k 套到 Llama / Qwen 上会产出
  「看起来精确、其实错误」的数字，比标 ``estimated=True`` 更危险。
* tiktoken **不是**运行时硬依赖：``tiktoken`` 只在 ``pyproject.toml`` 的
  optional extra ``tokens`` 里；import 失败 / 版本不兼容 / encoding 查不到一律
  降级到启发式，请求路径不受影响（``_import_tiktoken`` 只尝试一次）。
* 计数只吃「长度 / 结构」，**不落正文**：本模块无任何 I/O，返回值只有数字与标识。

启发式口径（常量见 ``facts``，校准依据 PROBE §2.2）：

* CJK / 全角 ≈ 1 token / 字（修正 ``len//4`` 对中文的 4 倍低估）；
* ASCII 字母 / 数字 / 空白 ≈ 4 字符 1 token（英文经验值）；
* ASCII 标点 / 符号 ≈ 2 字符 1 token（BPE 对符号压缩差）；
* 其它非 ASCII（西里尔 / 重音拉丁 / emoji）≈ 1 token / 字（宁高不低）。

chat template 粗略开销：每消息 +4、回复预备 +3、tools / tool_calls 按 JSON 全量计数。
"""
from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from . import config as client_config, facts

__all__ = [
    "TokenCount",
    "TokenCounter",
    "TokenMismatch",
    "HeuristicCounter",
    "TiktokenCounter",
    "compare_counts",
    "compare_enabled",
    "count_for_model",
    "count_messages",
    "count_text",
    "counter_families",
    "flag_enabled",
    "model_family",
    "register_counter",
    "reset_tokenizers",
    "tokenizer_for",
]


# ─────────────────────────────────────────────────────────── 返回值 / 协议
@dataclass(frozen=True)
class TokenCount:
    """一次计数的结果：数量 + 用了哪个分词器 + 是否为估算。

    ``estimated=True`` 表示数字来自启发式，**不可**当作精确账单依据；
    只用于量级兜底（限额预警、上下文窗口粗判）与差异检测。
    """

    tokens: int
    tokenizer_id: str
    estimated: bool

    def as_dict(self) -> dict[str, Any]:
        """结构化日志 / CLI 展示用（只有数字与标识，不含正文）。"""
        return {"tokens": int(self.tokens), "tokenizer_id": self.tokenizer_id,
                "estimated": bool(self.estimated)}


@dataclass(frozen=True)
class TokenMismatch:
    """本地计数与上游 usage 的差异（只在超过阈值时构造）。"""

    local_tokens: int
    upstream_tokens: int
    delta_tokens: int
    delta_pct: float

    def as_dict(self) -> dict[str, Any]:
        return {"local_tokens": int(self.local_tokens),
                "upstream_tokens": int(self.upstream_tokens),
                "delta_tokens": int(self.delta_tokens),
                "delta_pct": float(self.delta_pct)}


@runtime_checkable
class TokenCounter(Protocol):
    """分词器协议：实现它就能进注册表。

    ``tokenizer_id`` 稳定可读（落日志 / 诊断包用）；``estimated`` 标记是否估算值。
    """

    tokenizer_id: str
    estimated: bool

    def count_text(self, text: str) -> int: ...


# ─────────────────────────────────────────────────────────── 启发式估算
def _is_cjk(code: int) -> bool:
    """CJK（含假名 / 谚文 / CJK 标点 / 全角）判定。全角按 1 token/字（保守）。"""
    return (0x3000 <= code <= 0x303F        # CJK 标点（。，、等）
            or 0x3040 <= code <= 0x30FF     # 平假名 / 片假名
            or 0x3400 <= code <= 0x4DBF     # CJK 扩展 A
            or 0x4E00 <= code <= 0x9FFF     # CJK 基本区
            or 0xAC00 <= code <= 0xD7AF     # 谚文音节
            or 0xF900 <= code <= 0xFAFF     # CJK 兼容汉字
            or 0xFF00 <= code <= 0xFF65)    # 全角 ASCII / 全角标点


def _ceil_div(value: int, divisor: int) -> int:
    """整数向上取整除法（避免浮点误差；divisor > 0）。"""
    if value <= 0:
        return 0
    return (value + divisor - 1) // divisor


def _heuristic_tokens(text: str) -> int:
    """纯字符分类估算：不依赖任何第三方包，结果确定（同输入同输出）。"""
    ascii_chars = 0
    ascii_symbols = 0
    cjk_chars = 0
    other_chars = 0
    for char in text:
        code = ord(char)
        if 0x20 <= code <= 0x7E or char in "\n\r\t":
            if char.isalnum() or char.isspace():
                ascii_chars += 1
            else:
                ascii_symbols += 1
        elif _is_cjk(code):
            cjk_chars += 1
        else:
            other_chars += 1
    return (_ceil_div(ascii_chars, facts.TOKEN_HEURISTIC_ASCII_CHARS_PER_TOKEN)
            + _ceil_div(ascii_symbols, facts.TOKEN_HEURISTIC_ASCII_SYMBOL_CHARS_PER_TOKEN)
            + cjk_chars * facts.TOKEN_HEURISTIC_CJK_TOKENS_PER_CHAR
            + other_chars * facts.TOKEN_HEURISTIC_OTHER_TOKENS_PER_CHAR)


class HeuristicCounter:
    """无分词器时的估算器（纯标准库、确定性、零 I/O）。"""

    tokenizer_id = facts.TOKENIZER_HEURISTIC_ID
    estimated = True

    def count_text(self, text: str) -> int:
        return _heuristic_tokens(text or "")

    def __repr__(self) -> str:                                  # pragma: no cover - 调试
        return "HeuristicCounter()"


# ─────────────────────────────────────────────────────────── tiktoken（optional）
class TiktokenCounter:
    """真分词：只在已安装 tiktoken 且模型属 OpenAI 系时使用。"""

    estimated = False

    def __init__(self, model: str, encoding_name: str, encoding: Any) -> None:
        self.model = model
        self.encoding_name = encoding_name
        self.tokenizer_id = facts.TOKENIZER_TIKTOKEN_PREFIX + encoding_name
        self._encoding = encoding

    def count_text(self, text: str) -> int:
        if not text:
            return 0
        return len(self._encoding.encode(text))

    def __repr__(self) -> str:                                  # pragma: no cover - 调试
        return "TiktokenCounter(" + self.encoding_name + ")"


# import 只尝试一次：失败后不再反复走 import 机制（请求路径零成本降级）
_TIKTOKEN_IMPORT: dict[str, Any] = {"attempted": False, "module": None}


def _import_tiktoken() -> Any:
    """返回 tiktoken 模块；未安装 / 损坏一律返回 None（不抛、不装包、不联网）。"""
    if _TIKTOKEN_IMPORT["attempted"]:
        return _TIKTOKEN_IMPORT["module"]
    module = None
    try:
        module = importlib.import_module("tiktoken")
    except Exception:                                          # ImportError / 安装损坏
        module = None
    _TIKTOKEN_IMPORT["attempted"] = True
    _TIKTOKEN_IMPORT["module"] = module
    return module


def _encoding_name_for(module: Any, model: str) -> str | None:
    """模型名 → encoding 名：先查 facts 表，再问 tiktoken 自带查表。"""
    lowered = (model or "").strip().lower()
    for prefix, encoding_name in facts.TOKEN_TIKTOKEN_ENCODINGS.items():
        if lowered.startswith(prefix):
            return encoding_name
    lookup = getattr(module, "encoding_for_model", None)
    if not callable(lookup):
        return None
    try:
        encoding = lookup(model)
    except Exception:                                          # 未知模型 -> KeyError
        return None
    name = getattr(encoding, "name", None)
    return name if isinstance(name, str) and name else None


def _build_tiktoken_counter(model: str) -> TokenCounter | None:
    """构建真分词器；任何一步不可用返回 None（由 tokenizer_for 降级）。"""
    module = _import_tiktoken()
    if module is None:
        return None
    encoding_name = _encoding_name_for(module, model)
    if not encoding_name:
        return None
    getter = getattr(module, "get_encoding", None)
    if not callable(getter):
        return None
    try:
        encoding = getter(encoding_name)
    except Exception:                                          # encoding 名不被支持
        return None
    encode = getattr(encoding, "encode", None)
    if not callable(encode):
        return None
    return TiktokenCounter(model, encoding_name, encoding)


# ─────────────────────────────────────────────────────────── 注册表
def _family_openai(model: str) -> TokenCounter | None:
    return _build_tiktoken_counter(model)


def _family_heuristic(model: str) -> TokenCounter | None:
    return HeuristicCounter()


_COUNTER_FACTORIES: dict[str, Callable[[str], TokenCounter | None]] = {
    "openai": _family_openai,
    "heuristic": _family_heuristic,
}

_CACHE: dict[str, TokenCounter] = {}


def register_counter(family: str, factory: Callable[[str], TokenCounter | None]) -> None:
    """注册一个模型族的计数器工厂（只加不改：同族再注册即显式替换）。

    工厂签名为 ``factory(model) -> TokenCounter | None``；返回 None 表示该族
    当前不可用，调用方会降级到启发式。
    """
    key = (family or "").strip().lower()
    if not key:
        raise ValueError("family 不能为空")
    if not callable(factory):
        raise ValueError("factory 必须可调用")
    _COUNTER_FACTORIES[key] = factory
    _CACHE.clear()


def counter_families() -> tuple[str, ...]:
    """已注册的模型族（诊断 / 测试用）。"""
    return tuple(_COUNTER_FACTORIES)


def reset_tokenizers() -> None:
    """清空分词器缓存与 import 探测状态（测试 / 运行期换装后调用）。"""
    _CACHE.clear()
    _TIKTOKEN_IMPORT["attempted"] = False
    _TIKTOKEN_IMPORT["module"] = None


def model_family(model: str) -> str:
    """模型名 → 分词族。命中 OpenAI 前缀才算 openai，其余 generic（走启发式）。"""
    lowered = (model or "").strip().lower()
    for prefix in facts.TOKEN_OPENAI_MODEL_PREFIXES:
        if lowered.startswith(prefix):
            return "openai"
    return "generic"


def tokenizer_for(model: str) -> TokenCounter:
    """按模型取计数器（带缓存）；构建失败一律降级启发式，绝不抛给请求路径。"""
    key = (model or "").strip()
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    family = model_family(key)
    factory = _COUNTER_FACTORIES.get(family) or _COUNTER_FACTORIES["heuristic"]
    counter: TokenCounter | None = None
    try:
        counter = factory(key)
    except Exception:                                          # 注册表工厂自身异常
        counter = None
    if counter is None:
        counter = _COUNTER_FACTORIES["heuristic"](key) or HeuristicCounter()
    _CACHE[key] = counter
    return counter


# ─────────────────────────────────────────────────────────── 计数入口
def count_text(model: str, text: str | None) -> TokenCount:
    """单段文本的 token 数（不含 chat template 开销）。"""
    counter = tokenizer_for(model)
    tokens = int(counter.count_text(text or ""))
    return TokenCount(tokens=max(0, tokens), tokenizer_id=counter.tokenizer_id,
                      estimated=bool(counter.estimated))


def count_for_model(model: str, text: str | None) -> TokenCount:
    """任务卡别名：给 Lead 接 CLI 用（``djb tokens --model m --text ...``）。"""
    return count_text(model, text)


def _canonical_json(value: Any) -> str:
    """稳定序列化（工具定义 / tool_calls 计数口径固定，保证同输入同输出）。"""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
    except (TypeError, ValueError):                             # pragma: no cover - 防御
        return str(value)


def _content_tokens(counter: TokenCounter, content: Any) -> int:
    """content 支持 str / 多模态片段数组 / 其它结构（按 JSON 计）。"""
    if content is None:
        return 0
    if isinstance(content, str):
        return int(counter.count_text(content))
    if isinstance(content, (list, tuple)):
        tokens = 0
        for part in content:
            if isinstance(part, str):
                tokens += int(counter.count_text(part))
                continue
            if not isinstance(part, Mapping):
                tokens += int(counter.count_text(_canonical_json(part)))
                continue
            part_type = part.get("type")
            if isinstance(part_type, str):
                tokens += int(counter.count_text(part_type))
            if part_type in ("image_url", "input_image", "image"):
                tokens += facts.TOKEN_IMAGE_PART_OVERHEAD
            text = part.get("text")
            if isinstance(text, str):
                tokens += int(counter.count_text(text))
        return tokens
    return int(counter.count_text(_canonical_json(content)))


def _message_tokens(counter: TokenCounter, message: Any) -> int:
    """单条消息的正文部分（不含每消息固定开销）。"""
    if isinstance(message, str):
        return int(counter.count_text(message))
    if not isinstance(message, Mapping):
        return int(counter.count_text(_canonical_json(message)))
    tokens = 0
    role = message.get("role")
    if isinstance(role, str):
        tokens += int(counter.count_text(role))
    name = message.get("name")
    if isinstance(name, str):
        tokens += int(counter.count_text(name))
    tokens += _content_tokens(counter, message.get("content"))
    for key in ("tool_calls", "function_call"):
        value = message.get(key)
        if value:
            tokens += int(counter.count_text(_canonical_json(value)))
    return tokens


def _tools_tokens(counter: TokenCounter, tools: Any) -> int:
    """tools 定义按 JSON 全量计数（+ 一次包裹开销；粗略口径）。"""
    if not tools:
        return 0
    return int(counter.count_text(_canonical_json(tools))) + facts.TOKEN_CHAT_MESSAGE_OVERHEAD


def count_messages(model: str, messages: Any, tools: Any = None) -> TokenCount:
    """对话请求的 prompt token 估算（含 chat template 粗略开销）。

    口径：每条消息 +4、reply priming +3（常量见 ``facts``），tools 全量 JSON。
    ``messages`` 容忍异常结构（字符串 / 非 dict）——不抛，按最接近的形态计。
    """
    counter = tokenizer_for(model)
    total = 0
    items = messages if isinstance(messages, (list, tuple)) else ([] if not messages
                                                                 else [messages])
    for message in items:
        total += facts.TOKEN_CHAT_MESSAGE_OVERHEAD
        total += _message_tokens(counter, message)
    total += _tools_tokens(counter, tools)
    total += facts.TOKEN_CHAT_REPLY_PRIMING
    return TokenCount(tokens=max(0, total), tokenizer_id=counter.tokenizer_id,
                      estimated=bool(counter.estimated))


# ─────────────────────────────────────────────────────────── 差异判定 / 开关
def compare_counts(local_tokens: int, upstream_tokens: int,
                   *, threshold_pct: float | None = None) -> TokenMismatch | None:
    """本地 vs 上游 prompt tokens：严格大于阈值才返回差异，否则 None。

    * 上游为 0（或负数）视为「没有可比基准」→ None（不做除法、不误报）；
    * 阈值默认 ``facts.TOKEN_COMPARE_THRESHOLD_PCT``（1%），边界 ``== 1%`` 不记。
    """
    upstream = int(upstream_tokens)
    if upstream <= 0:
        return None
    local = max(0, int(local_tokens))
    delta = local - upstream
    pct = abs(delta) / float(upstream) * 100.0
    threshold = (facts.TOKEN_COMPARE_THRESHOLD_PCT if threshold_pct is None
                 else float(threshold_pct))
    if pct <= threshold:
        return None
    return TokenMismatch(local_tokens=local, upstream_tokens=upstream,
                         delta_tokens=delta, delta_pct=round(pct, 4))


def _feature_flag_resolver() -> Callable[[Any, str], Any] | None:
    """开关求值器：唯一实现是 ``config.feature_flag``（C7 提供）。"""
    resolver = getattr(client_config, "feature_flag", None)
    return resolver if callable(resolver) else None


def _as_client_config(settings: Any) -> Any:
    """把 dict / None 归一成 ``ClientConfig``（``config.feature_flag`` 的入参形状）。

    只是形状归一（tolerant reader 在 config 里），解析规则仍归 ``config.feature_flag``。
    """
    if isinstance(settings, client_config.ClientConfig):
        return settings
    if settings is None:
        return client_config.ClientConfig()
    if isinstance(settings, Mapping):
        return client_config.ClientConfig.from_dict(dict(settings))
    return settings


def flag_enabled(name: str, settings: Any = None) -> bool:
    """特性开关判定（薄委托，不重复解析）。

    * 求值唯一实现：``config.feature_flag(config, name)``（C7；读
      ``config.extra.feature_flags`` 或平铺键，缺省 False）；
    * 该实现尚未合入时按 ``facts.FEATURE_FLAGS`` 的 ``default``（关闭）返回——
      正常安装路径上永远走 resolver，这里只是未合入分支的兜底；
    * ``settings`` 兼容 dict / ``ClientConfig`` / None（调用方原样传）。
    """
    resolver = _feature_flag_resolver()
    if resolver is None:
        meta = getattr(facts, "FEATURE_FLAGS", {}).get(name) or {}
        return meta.get("default") is True
    return bool(resolver(_as_client_config(settings), name))


def compare_enabled(settings: Any = None) -> bool:
    """开关 ``client.token.compare.enabled``（默认关闭；回滚=只记上游 usage）。"""
    return flag_enabled(facts.TOKEN_COMPARE_FLAG, settings)
