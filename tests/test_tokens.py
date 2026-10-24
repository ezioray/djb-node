# SPDX-License-Identifier: Apache-2.0
"""C8 · token 计数：中英文、空串、消息结构、tools、估算标记、tiktoken 存在/缺失、确定性。

约束：测试不联网、不装包。tiktoken 的「存在」用注入假模块模拟（sys.modules），
「缺失」用 ``sys.modules[name] = None``（import 必失败）模拟。
"""
from __future__ import annotations

import importlib
import importlib.util
import sys

import pytest

from djb_node import facts, tokens

TIKTOKEN_INSTALLED = importlib.util.find_spec("tiktoken") is not None


# ─────────────────────────────────────────────────────────── 夹具 / 假模块
@pytest.fixture(autouse=True)
def fresh_tokenizers():
    """每个用例前后清空分词器缓存与 import 探测状态（防止假模块泄漏）。"""
    saved = dict(tokens._COUNTER_FACTORIES)
    tokens.reset_tokenizers()
    yield
    tokens._COUNTER_FACTORIES.clear()
    tokens._COUNTER_FACTORIES.update(saved)
    tokens.reset_tokenizers()


class FakeEncoding:
    """假 encoding：每字符 2 token（与启发式的「每个英文字符 0.25 token」明显不同）。"""

    def __init__(self, name: str) -> None:
        self.name = name

    def encode(self, text: str) -> list[int]:
        return list(range(len(text) * 2))


class FakeTiktoken:
    """假 tiktoken 模块：记录被请求的 encoding，可注入两种失败。"""

    def __init__(self) -> None:
        self.requested: list[str] = []
        self.fail_get_encoding = False
        self.fail_encoding_for_model = False

    def get_encoding(self, name: str) -> FakeEncoding:
        self.requested.append(name)
        if self.fail_get_encoding:
            raise ValueError("unknown encoding: " + name)
        return FakeEncoding(name)

    def encoding_for_model(self, model: str) -> FakeEncoding:
        if self.fail_encoding_for_model:
            raise KeyError(model)
        return FakeEncoding("o200k_base")


@pytest.fixture
def fake_tiktoken(monkeypatch):
    module = FakeTiktoken()
    monkeypatch.setitem(sys.modules, "tiktoken", module)
    tokens.reset_tokenizers()
    return module


@pytest.fixture
def no_tiktoken(monkeypatch):
    """模拟「未安装 tiktoken」：sys.modules 里放 None，import 必抛 ImportError。"""
    monkeypatch.setitem(sys.modules, "tiktoken", None)
    tokens.reset_tokenizers()


class FakeCounter:
    """自定义族计数器（注册表用例）。"""

    tokenizer_id = "fake:1"
    estimated = False

    def count_text(self, text: str) -> int:
        return len(text) * 3


# ─────────────────────────────────────────────────────────── count_text
def test_empty_text_is_zero():
    result = tokens.count_text("gpt-4o", "")
    assert result.tokens == 0
    assert result.tokenizer_id == facts.TOKENIZER_HEURISTIC_ID
    assert result.estimated is True
    assert tokens.count_text("gpt-4o", None).tokens == 0


def test_ascii_english_is_about_four_chars_per_token():
    text = "The quick brown fox jumps over the lazy dog"
    result = tokens.count_text("gpt-4o", text)
    # 43 字符 / 4 ≈ 11；允许 ±3 的启发式误差，但必须远小于「按字符计」
    assert abs(result.tokens - len(text) // 4) <= 3
    assert result.tokens < len(text)


def test_chinese_is_not_underestimated_like_len_div_four():
    text = "中" * 100
    result = tokens.count_text("gpt-4o", text)
    # PROBE §2.2：平台 len//4 对中文低估约 4 倍；本地估算必须 ≥ 3 倍 len//4
    assert result.tokens >= (len(text) // 4) * 3
    assert result.tokens == len(text)          # 1 token / 字
    assert result.estimated is True


def test_mixed_chinese_and_ascii_adds_up():
    chinese = tokens.count_text("gpt-4o", "中文四个字").tokens
    ascii_part = tokens.count_text("gpt-4o", "hello world").tokens
    mixed = tokens.count_text("gpt-4o", "中文四个字hello world").tokens
    assert mixed == chinese + ascii_part


def test_ascii_symbols_weigh_more_than_letters():
    letters = tokens.count_text("gpt-4o", "aaaaaa").tokens
    symbols = tokens.count_text("gpt-4o", "{{{{{{").tokens
    assert symbols > letters


def test_whitespace_only_is_cheap():
    result = tokens.count_text("gpt-4o", "\n\n\n")
    assert 0 < result.tokens <= 2


def test_count_is_deterministic_across_calls_and_resets():
    text = "你好，world！" * 7
    first = tokens.count_text("gpt-4o", text).tokens
    second = tokens.count_text("gpt-4o", text).tokens
    tokens.reset_tokenizers()
    third = tokens.count_text("gpt-4o", text).tokens
    assert first == second == third


def test_token_count_shape_is_frozen_and_typed():
    result = tokens.count_for_model("gpt-4o", "hi")
    assert isinstance(result, tokens.TokenCount)
    assert isinstance(result.tokens, int) and not isinstance(result.tokens, bool)
    assert isinstance(result.estimated, bool)
    assert isinstance(result.tokenizer_id, str)
    assert result.as_dict() == {"tokens": result.tokens,
                                "tokenizer_id": result.tokenizer_id,
                                "estimated": result.estimated}
    with pytest.raises(Exception):
        result.tokens = 1                       # frozen dataclass


def test_count_for_model_is_alias_of_count_text():
    assert (tokens.count_for_model("gpt-4o", "ab").as_dict()
            == tokens.count_text("gpt-4o", "ab").as_dict())


# ─────────────────────────────────────────────────────────── count_messages
def test_chat_template_constants_are_four_and_three():
    assert facts.TOKEN_CHAT_MESSAGE_OVERHEAD == 4
    assert facts.TOKEN_CHAT_REPLY_PRIMING == 3


def test_single_message_adds_overhead_and_priming():
    messages = [{"role": "user", "content": "hi"}]
    result = tokens.count_messages("gpt-4o", messages)
    # 4（每消息）+ "user" 1 + "hi" 1 + 3（reply priming）
    assert result.tokens == 9
    assert tokens.count_messages("gpt-4o", messages * 2).tokens == 9 + 4 + 1 + 1


def test_empty_message_list_is_priming_only():
    assert tokens.count_messages("gpt-4o", []).tokens == facts.TOKEN_CHAT_REPLY_PRIMING
    assert tokens.count_messages("gpt-4o", None).tokens == facts.TOKEN_CHAT_REPLY_PRIMING


def test_tools_increase_count():
    messages = [{"role": "user", "content": "hi"}]
    tools = [{"type": "function",
              "function": {"name": "get_price", "description": "查价",
                           "parameters": {"type": "object", "properties": {}}}}]
    without = tokens.count_messages("gpt-4o", messages).tokens
    with_tools = tokens.count_messages("gpt-4o", messages, tools=tools).tokens
    assert with_tools > without
    tools_only = tokens.count_text("gpt-4o", tokens._canonical_json(tools)).tokens
    assert with_tools == without + tools_only + facts.TOKEN_CHAT_MESSAGE_OVERHEAD


def test_multimodal_content_counts_text_and_image_overhead():
    messages = [{"role": "user", "content": [{"type": "text", "text": "hi"},
                                             {"type": "image_url",
                                              "image_url": {"url": "http://127.0.0.1/x.png"}}]}]
    result = tokens.count_messages("gpt-4o", messages)
    text_only = tokens.count_messages("gpt-4o", [{"role": "user", "content": "hi"}]).tokens
    assert result.tokens > text_only + facts.TOKEN_IMAGE_PART_OVERHEAD


def test_messages_tolerate_odd_shapes():
    # 字符串消息 / 缺 role / content=None / messages 本身是单个 dict：一律不抛
    assert tokens.count_messages("gpt-4o", ["hi"]).tokens == (
        facts.TOKEN_CHAT_MESSAGE_OVERHEAD + facts.TOKEN_CHAT_REPLY_PRIMING + 1)
    assert tokens.count_messages("gpt-4o", [{"content": None}]).tokens == (
        facts.TOKEN_CHAT_MESSAGE_OVERHEAD + facts.TOKEN_CHAT_REPLY_PRIMING)
    assert tokens.count_messages("gpt-4o", {"role": "user",
                                            "content": "hi"}).tokens == 9


def test_tool_calls_in_message_are_counted():
    plain = tokens.count_messages("gpt-4o", [{"role": "assistant", "content": "ok"}]).tokens
    called = tokens.count_messages(
        "gpt-4o",
        [{"role": "assistant", "content": "ok",
          "tool_calls": [{"id": "c1", "type": "function",
                          "function": {"name": "f", "arguments": "{}"}}]}]).tokens
    assert called > plain


# ─────────────────────────────────────────────────────────── 注册表 / 族判定
def test_model_family_detection():
    assert tokens.model_family("gpt-4o-mini") == "openai"
    assert tokens.model_family("GPT-4") == "openai"
    assert tokens.model_family("o3-mini") == "openai"
    assert tokens.model_family("text-embedding-3-small") == "openai"
    assert tokens.model_family("qwen2.5-72b-instruct") == "generic"
    assert tokens.model_family("llama-3.1-70b") == "generic"
    assert tokens.model_family("") == "generic"


def test_registry_lists_default_families():
    families = tokens.counter_families()
    assert "openai" in families and "heuristic" in families


def test_heuristic_counter_satisfies_protocol():
    counter = tokens.tokenizer_for("qwen2.5-72b")
    assert isinstance(counter, tokens.TokenCounter)
    assert counter.tokenizer_id == facts.TOKENIZER_HEURISTIC_ID
    assert counter.estimated is True


def test_register_counter_plugs_in_custom_family(monkeypatch):
    tokens.register_counter("custom", lambda model: FakeCounter())
    monkeypatch.setattr(tokens, "model_family", lambda model: "custom")
    result = tokens.count_text("whatever", "ab")
    assert result.tokens == 6
    assert result.tokenizer_id == "fake:1"
    assert result.estimated is False


def test_register_counter_rejects_bad_arguments():
    with pytest.raises(ValueError):
        tokens.register_counter("", FakeCounter)
    with pytest.raises(ValueError):
        tokens.register_counter("x", None)      # type: ignore[arg-type]


def test_failing_factory_degrades_to_heuristic(monkeypatch):
    def boom(model):
        raise RuntimeError("factory down")

    tokens.register_counter("custom", boom)
    monkeypatch.setattr(tokens, "model_family", lambda model: "custom")
    result = tokens.count_text("whatever", "hi")
    assert result.estimated is True
    assert result.tokenizer_id == facts.TOKENIZER_HEURISTIC_ID


# ─────────────────────────────────────────────────────────── tiktoken 存在 / 缺失
def test_missing_tiktoken_degrades_to_heuristic(no_tiktoken):
    counter = tokens.tokenizer_for("gpt-4o")
    assert counter.tokenizer_id == facts.TOKENIZER_HEURISTIC_ID
    assert counter.estimated is True
    assert tokens.count_text("gpt-4o", "hello").tokens == 2   # 启发式：5 字符 → 2


def test_tiktoken_present_is_used_and_not_marked_estimated(fake_tiktoken):
    result = tokens.count_text("gpt-4o", "hello")
    assert result.tokenizer_id == facts.TOKENIZER_TIKTOKEN_PREFIX + "o200k_base"
    assert result.estimated is False
    assert result.tokens == 10                                # 假 encoding：每字符 2 token
    assert fake_tiktoken.requested == ["o200k_base"]


def test_tiktoken_encoding_table_maps_older_models(fake_tiktoken):
    assert tokens.count_text("gpt-4-turbo", "ab").tokenizer_id.endswith("cl100k_base")
    assert tokens.count_text("gpt-3.5-turbo", "ab").tokenizer_id.endswith("cl100k_base")


def test_tiktoken_falls_back_to_module_lookup_for_new_models(fake_tiktoken):
    # gpt-6-preview 不在 facts 表里 → 问 tiktoken.encoding_for_model（假模块返回 o200k_base）
    assert tokens.count_text("gpt-6-preview", "ab").tokenizer_id.endswith("o200k_base")


def test_tiktoken_unknown_encoding_degrades(fake_tiktoken):
    fake_tiktoken.fail_get_encoding = True
    result = tokens.count_text("gpt-4o", "hello")
    assert result.estimated is True
    assert result.tokenizer_id == facts.TOKENIZER_HEURISTIC_ID


def test_tiktoken_unknown_model_degrades(fake_tiktoken):
    fake_tiktoken.fail_encoding_for_model = True
    result = tokens.count_text("gpt-6-preview", "hello")
    assert result.estimated is True


def test_non_openai_model_stays_heuristic_even_with_tiktoken(fake_tiktoken):
    result = tokens.count_text("qwen2.5-72b-instruct", "hello")
    assert result.estimated is True
    assert result.tokenizer_id == facts.TOKENIZER_HEURISTIC_ID
    assert fake_tiktoken.requested == []        # 不把 cl100k 套到非 OpenAI 模型上


def test_tiktoken_import_is_probed_once(monkeypatch, no_tiktoken):
    calls: list[str] = []
    real_import = importlib.import_module

    def counting_import(name, *args, **kwargs):
        calls.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", counting_import)
    tokens.reset_tokenizers()
    for model in ("gpt-4o", "gpt-4", "gpt-3.5-turbo"):
        assert tokens.count_text(model, "hi").estimated is True
    assert calls.count("tiktoken") == 1          # 失败只探测一次，请求路径零成本


def test_reset_tokenizers_rebuilds_after_install(monkeypatch):
    # 先当作没装 → 降级；装好后 reset → 用真分词（模拟用户补装 optional extra）
    monkeypatch.setitem(sys.modules, "tiktoken", None)
    tokens.reset_tokenizers()
    assert tokens.count_text("gpt-4o", "hi").estimated is True
    monkeypatch.setitem(sys.modules, "tiktoken", FakeTiktoken())
    tokens.reset_tokenizers()
    result = tokens.count_text("gpt-4o", "hi")
    assert result.estimated is False
    assert result.tokenizer_id.startswith(facts.TOKENIZER_TIKTOKEN_PREFIX)


@pytest.mark.skipif(TIKTOKEN_INSTALLED, reason="本机已安装 tiktoken，默认路径不是启发式")
def test_default_environment_has_no_tiktoken_and_estimates():
    """本仓 venv 不带 tiktoken：默认路径必须走启发式并标 estimated=True。"""
    counter = tokens.tokenizer_for("gpt-4o")
    assert counter.tokenizer_id == facts.TOKENIZER_HEURISTIC_ID
    assert counter.estimated is True
