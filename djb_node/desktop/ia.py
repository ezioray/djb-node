# SPDX-License-Identifier: Apache-2.0
"""IA 骨架（0.3.5 · M1-T1）：导航分组 / 页面注册表的**数据结构**，不改 UI 外观。

设计源：``reports/CLIENT-UX-20261112/02-IA-TOKENPLAN.md`` §2 方案 A（5 组、共享前置）、
§4 兼容（视图 key 不变、``DJB_UI_IA=legacy`` 回旧顺序）、§5 量化验收。

**本轮只落数据 + 纯函数**：:mod:`djb_node.desktop.views` 一个字节不动，
``NAV_SECTIONS`` 仍是 0.3.4 的 4 组 ⇒ 侧栏外观、截图基线、13 个视图投影全不变；
接线（侧栏渲染改读本模块）属于 T4，届时删掉 ``views.NAV_SECTIONS`` 改从
:func:`nav_sections` 取即可，回滚键是 ``DJB_UI_IA=legacy``。

5 组方案 A（§2 逐字誊写）
==========================

    共享 SHARE    token_plan, models, access     ← 首屏第一组
    运行 RUN      overview, bench, tasks
    收益 EARN     receipts, earnings
    接入 CONNECT  agent
    系统 SYSTEM   yield, gpu, account, logs      ← GPU 降到系统组，标 BETA

校验（:func:`violations`，全部可执行）：13 项恰好覆盖一次、``token_plan`` 在 ``access`` 之前、
首组首项 == ``token_plan``、``gpu`` 不在前 5 项、``gpu`` 带 BETA 副标、眉标纯 ASCII。

为什么 ``legacy`` 表也留在这里
==============================

§6「回滚」写明：运维可用 ``DJB_UI_IA=legacy`` 回旧顺序（代码保留旧表，仅排障用）。
把它落成本模块的一张数据表，才能在不改代码的前提下回滚——否则「回滚」只是文档上的字。
:data:`IA_SECTIONS_LEGACY` 与 0.3.4 的 ``views.NAV_SECTIONS`` 逐项一致，
``tests/test_desktop_ia_registry.py`` 会断言这一点（漂移即红）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

__all__ = [
    "FOCUS_KEY", "IA_SECTIONS_LEGACY", "IA_SECTIONS_SHARE", "IA_MODES", "MIN_GPU_INDEX",
    "IaDecision", "PageSpec", "RegionSpec", "SectionSpec", "flat_keys", "index_of",
    "nav_sections", "page", "resolve_ia", "section_for", "violations",
]

#: 首屏第一组、一屏可见的「共享 Token Plan」——产品主路径（§1 现状：它原来是第 8 项）
FOCUS_KEY = "token_plan"

#: GPU 不许进前 N 项（§5.3：首屏不出现 GPU 主 CTA）
MIN_GPU_INDEX = 5

#: 可选的 IA 模式：``share``（默认，方案 A）/ ``legacy``（0.3.4 旧顺序，仅排障）
IA_MODES: tuple[str, ...] = ("share", "legacy")


@dataclass(frozen=True)
class SectionSpec:
    """侧栏分组：一个使用场景 = 一段连续页面。"""

    key: str            # 组 key（英文小写，测试/样式用）
    title: str          # 中文组名（侧栏分组头主文案）
    micro: str          # 拉丁微标（只放 ASCII，别让中文进 mono）
    keys: tuple[str, ...]   # 组成员，顺序即侧栏顺序


@dataclass(frozen=True)
class PageSpec:
    """页面注册表的一条：key 是**契约**（深链 / ``djb --view`` / 快捷键都用它）。"""

    key: str
    title: str              # 侧栏标题（中文）
    kicker: str             # 页头眉标的页名部分（纯 ASCII，可交给 mono）
    section: str            # 所属分组 key（与 :class:`SectionSpec.key` 对应）
    subtitle: str = ""      # 页头副标（如 GPU 的 BETA 说明）
    beta: bool = False      # 是否为滞后/BETA 能力（渲染层加徽标，并降主 CTA 权重）
    hidden: bool = False    # 不进侧栏（深链仍可达）


#: 方案 A：5 组、共享前置（02-IA-TOKENPLAN §2）
IA_SECTIONS_SHARE: tuple[SectionSpec, ...] = (
    SectionSpec("share", "共享", "SHARE", ("token_plan", "models", "access")),
    SectionSpec("run", "运行", "RUN", ("overview", "bench", "tasks")),
    SectionSpec("earn", "收益", "EARN", ("receipts", "earnings")),
    SectionSpec("connect", "接入", "CONNECT", ("agent",)),
    SectionSpec("system", "系统", "SYSTEM", ("yield", "gpu", "account", "logs")),
)

#: 0.3.4 现状（回滚路径）：4 组，与 ``views.NAV_SECTIONS`` 逐项一致
IA_SECTIONS_LEGACY: tuple[SectionSpec, ...] = (
    SectionSpec("run", "运行", "RUN", ("overview", "bench", "gpu", "models", "tasks")),
    SectionSpec("earn", "收益", "EARN", ("receipts", "earnings", "token_plan")),
    SectionSpec("connect", "接入", "CONNECT", ("agent", "access")),
    SectionSpec("system", "系统", "SYSTEM", ("yield", "account", "logs")),
)

#: 页面注册表：13 项一个不少、一个不并（§1 约束）
PAGES: dict[str, PageSpec] = {
    "token_plan": PageSpec("token_plan", "共享 Token Plan", "TOKEN PLAN", "share"),
    "models": PageSpec("models", "可提供模型", "MODELS", "share"),
    "access": PageSpec("access", "接入配置", "ACCESS", "share"),
    "overview": PageSpec("overview", "总览", "OVERVIEW", "run"),
    "bench": PageSpec("bench", "性能基准", "BENCH", "run"),
    "tasks": PageSpec("tasks", "任务队列", "TASKS", "run"),
    "receipts": PageSpec("receipts", "收据", "RECEIPTS", "earn"),
    "earnings": PageSpec("earnings", "收益", "EARNINGS", "earn"),
    "agent": PageSpec("agent", "Agent 接入", "AGENT", "connect"),
    "yield": PageSpec("yield", "让路 / 节能", "YIELD", "system"),
    "gpu": PageSpec("gpu", "GPU 监控", "GPU", "system", beta=True,
                    subtitle="GPU 监控（BETA）· 共享算力为后续能力，当前仅本机监控"),
    "account": PageSpec("account", "账户与安全", "ACCOUNT", "system"),
    "logs": PageSpec("logs", "日志", "LOGS", "system"),
}


# ── 取数据
def page(key: str) -> PageSpec:
    """按 key 取页面；未知 key 抛 KeyError（深链校验靠它，不猜、不返回 None）。"""
    name = str(key or "").strip()
    try:
        return PAGES[name]
    except KeyError:
        raise KeyError("未知视图 key：" + repr(key) + "（已知：" + " / ".join(PAGES) + "）") from None


def nav_sections(legacy: bool = False) -> tuple[SectionSpec, ...]:
    """当前生效的分组表。``legacy=True`` 回 0.3.4 旧顺序（§6 排障用）。"""
    return IA_SECTIONS_LEGACY if legacy else IA_SECTIONS_SHARE


def flat_keys(sections: Sequence[SectionSpec] | None = None) -> tuple[str, ...]:
    """把分组表展平成侧栏顺序（与 ``Ctrl+1…9`` 的序号一致）。"""
    table = IA_SECTIONS_SHARE if sections is None else sections
    return tuple(key for section in table for key in section.keys)


def index_of(key: str, sections: Sequence[SectionSpec] | None = None) -> int:
    """页面在侧栏里的序号；不在表里返回 ``-1``（调用方 decide，别在这里抛）。"""
    keys = flat_keys(sections)
    name = str(key or "").strip()
    return keys.index(name) if name in keys else -1


def section_for(key: str, sections: Sequence[SectionSpec] | None = None) -> SectionSpec | None:
    """页面所属分组（面包屑/组头用）；不在表里返回 None。"""
    name = str(key or "").strip()
    for section in (IA_SECTIONS_SHARE if sections is None else sections):
        if name in section.keys:
            return section
    return None


def violations(sections: Sequence[SectionSpec] | None = None,
               pages: Mapping[str, PageSpec] | None = None) -> list[str]:
    """返回 IA 表的不变量违规（空列表 = 合格）。参数可注入 ⇒ 变异测试能证明它不是空转。"""
    table = IA_SECTIONS_SHARE if sections is None else sections
    registry = PAGES if pages is None else pages
    problems: list[str] = []
    expected = set(registry)

    # 1) 13 项恰好覆盖一次、无重复、无空组
    seen: list[str] = []
    for section in table:
        if not section.keys:
            problems.append(f"分组 {section.key} 是空组")
        if not section.micro.isascii():
            problems.append(f"分组 {section.key} 的微标含非 ASCII：{section.micro!r}")
        for key in section.keys:
            if key not in registry:
                problems.append(f"分组 {section.key} 含未注册页面 {key}")
            seen.append(key)
    duplicates = sorted({key for key in seen if seen.count(key) > 1})
    if duplicates:
        problems.append("页面重复出现：" + " / ".join(duplicates))
    missing = sorted(expected - set(seen))
    if missing:
        problems.append("页面未挂载：" + " / ".join(missing))
    extra = sorted(set(seen) - expected)
    if extra:
        problems.append("表里有注册表外的页面：" + " / ".join(extra))

    # 2) 页面注册表的 section 反指必须与分组表一致
    for key, spec in registry.items():
        owner = section_for(key, table)
        if owner is None:
            continue
        if spec.section != owner.key:
            problems.append(f"{key}: 注册表 section={spec.section} 与分组表 {owner.key} 不一致")
        if not spec.kicker.isascii():
            problems.append(f"{key}: 眉标含非 ASCII：{spec.kicker!r}")
        if spec.kicker != spec.kicker.upper():
            problems.append(f"{key}: 眉标应全大写：{spec.kicker!r}")

    # 3) 产品主路径：token_plan 一屏可见、在 access 之前
    focus = index_of(FOCUS_KEY, table)
    access = index_of("access", table)
    if focus == -1:
        problems.append("共享 Token Plan 不在侧栏里")
    if access != -1 and focus != -1 and focus >= access:
        problems.append(f"token_plan（第 {focus + 1} 项）没有排在 access（第 {access + 1} 项）之前")
    if table and table[0].keys and table[0].keys[0] != FOCUS_KEY:
        problems.append(f"首组首项是 {table[0].keys[0]}，应为 {FOCUS_KEY}")

    # 4) GPU 降级：不在前 5 项、并且带 BETA 标记
    gpu = index_of("gpu", table)
    if gpu != -1 and gpu < MIN_GPU_INDEX:
        problems.append(f"GPU 在侧栏第 {gpu + 1} 项（要求 ≥ 第 {MIN_GPU_INDEX + 1} 项）")
    gpu_spec = registry.get("gpu")
    if gpu_spec is not None:
        if not gpu_spec.beta:
            problems.append("gpu 页没有标 BETA（§5.3 要求首屏不出现 GPU 主 CTA）")
        if not gpu_spec.subtitle:
            problems.append("gpu 页缺 BETA 副标文案")

    # 5) 回滚路径也必须是完整的 13 项
    legacy_problems = _coverage_problems(IA_SECTIONS_LEGACY, expected)
    problems.extend("legacy: " + item for item in legacy_problems)

    return problems


def _coverage_problems(sections: Sequence[SectionSpec], expected: set[str]) -> list[str]:
    keys = [key for section in sections for key in section.keys]
    problems: list[str] = []
    if sorted(keys) != sorted(expected):
        problems.append("分组表未恰好覆盖 13 个页面")
    if len(keys) != len(set(keys)):
        problems.append("分组表有重复页面")
    return problems


@dataclass(frozen=True)
class IaDecision:
    """``DJB_UI_IA`` 的裁决：``mode`` ∈ :data:`IA_MODES`，``raw`` 是原始值（留证）。"""

    mode: str
    raw: str | None
    sections: tuple[SectionSpec, ...]
    ignored: bool = False


def resolve_ia(raw: object = None) -> IaDecision:
    """按 ``DJB_UI_IA`` 选表：``legacy`` → 旧顺序；其它/未设 → 方案 A。

    未识别的值**不静默当默认**：回 ``ignored=True``，由调用方在诊断里说一句
    「DJB_UI_IA=xxx 未识别，已按 share 生效」。
    """
    if raw is None:
        return IaDecision("share", None, IA_SECTIONS_SHARE)
    text = str(raw).strip().lower()
    if not text:
        return IaDecision("share", str(raw), IA_SECTIONS_SHARE, ignored=True)
    if text in IA_MODES:
        return IaDecision(text, str(raw), nav_sections(legacy=text == "legacy"))
    return IaDecision("share", str(raw), IA_SECTIONS_SHARE, ignored=True)
