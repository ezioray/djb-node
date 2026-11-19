# SPDX-License-Identifier: AGPL-3.0-or-later
"""base_url 来源分类（INTEGRITY-REVIEW-20261117 §5 · M17 先验）。

口径：只认 host 精确匹配 + 缺省端口 + https；一切不认识/可绕过形态一律 unknown
（fail-closed）。分类只作信任先验与提示，**永不拦截接入**。
"""
from djb_node import upstream as up


def test_official_endpoints():
    assert up.classify_base_url("https://api.openai.com/v1") == up.ORIGIN_OFFICIAL
    assert up.classify_base_url("https://api.anthropic.com") == up.ORIGIN_OFFICIAL
    assert up.classify_base_url("https://api.deepseek.com/") == up.ORIGIN_OFFICIAL
    assert up.classify_base_url("https://api.moonshot.cn/v1") == up.ORIGIN_OFFICIAL
    assert up.classify_base_url("https://open.bigmodel.cn/api/paas/v4") == up.ORIGIN_OFFICIAL


def test_trusted_open_source():
    assert up.classify_base_url("https://openrouter.ai/api/v1") == \
        up.ORIGIN_TRUSTED_OPEN_SOURCE


def test_unknown_for_lookalikes_and_weird_forms():
    # 前后缀伪装（同形/钓鱼域名）→ unknown
    assert up.classify_base_url("https://api.openai.com.evil.tld/v1") == up.ORIGIN_UNKNOWN
    assert up.classify_base_url("https://api-openai-com.evil.tld/v1") == up.ORIGIN_UNKNOWN
    assert up.classify_base_url("https://openai.api.com/v1") == up.ORIGIN_UNKNOWN
    # 子域名不继承信任
    assert up.classify_base_url("https://vip.api.openai.com/v1") == up.ORIGIN_UNKNOWN
    # 大小写与尾点应先归一（合法官方写法不误伤）
    assert up.classify_base_url("https://API.OpenAI.com/v1") == up.ORIGIN_OFFICIAL
    assert up.classify_base_url("https://api.openai.com./v1") == up.ORIGIN_OFFICIAL
    # 非标端口（官方域名 + 前置代理常见形态）→ unknown
    assert up.classify_base_url("https://api.openai.com:8443/v1") == up.ORIGIN_UNKNOWN
    # http 官方域名（镜像/中间人风险）→ unknown
    assert up.classify_base_url("http://api.openai.com/v1") == up.ORIGIN_UNKNOWN
    # IP 直连 / 回环 / 内网 → unknown
    assert up.classify_base_url("https://1.2.3.4/v1") == up.ORIGIN_UNKNOWN
    assert up.classify_base_url("http://127.0.0.1:1717/v1") == up.ORIGIN_UNKNOWN
    assert up.classify_base_url("http://localhost:8080/v1") == up.ORIGIN_UNKNOWN
    # 非 http(s) / userinfo / 非法输入 → unknown（防御性，validate 会先拦）
    assert up.classify_base_url("ftp://api.openai.com") == up.ORIGIN_UNKNOWN
    assert up.classify_base_url("https://user:pw@api.openai.com/v1") == up.ORIGIN_UNKNOWN
    assert up.classify_base_url("") == up.ORIGIN_UNKNOWN
    assert up.classify_base_url(None) == up.ORIGIN_UNKNOWN
    # 非 ASCII 同形字（аpi.openai.com 的西里尔 а）→ unknown
    assert up.classify_base_url("https://аpi.openai.com/v1") == up.ORIGIN_UNKNOWN


def test_default_port_matches():
    # 显式 443 = 缺省端口，仍算官方
    assert up.classify_base_url("https://api.openai.com:443/v1") == up.ORIGIN_OFFICIAL
    assert up.classify_base_url("https://openrouter.ai:443/api/v1") == \
        up.ORIGIN_TRUSTED_OPEN_SOURCE


def test_origin_hint_never_empty():
    assert up.origin_hint(up.ORIGIN_OFFICIAL)
    assert up.origin_hint(up.ORIGIN_UNKNOWN)
    assert up.origin_hint(None) == up.origin_hint("")
    assert up.origin_hint("bogus") == up.origin_hint(up.ORIGIN_UNKNOWN)
