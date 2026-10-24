#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""C5 机房版 · 100 节点批量配置渲染器（纯标准库，无网络、无凭据）。

把一份 inventory CSV 渲染成每个节点一套本机配置，供机房批量铺开：

    inventory.csv ──┐
    config.json.template ─┴─> out/<node_id>/config.json   每个节点的非敏感配置
                        └──> out/<node_id>/init.args    djb init 的参数行（预设/限额）
                        └──> out/manifest.json          节点清单 + 每个文件的 sha256

用法：

    python3 djb-fleet.py --inventory inventory.csv \\
                         --template config.json.template \\
                         --out out/

    python3 djb-fleet.py ... --check     # 只比较：有差异 exit 1（幂等演练：第二次必须 0 差异）
    python3 djb-fleet.py ... --check     # 只比较：不写盘
    python3 djb-fleet.py ... --dry-run   # 打印 unified diff，不写盘（有差异 exit 1）

退出码：0 成功 / 1 有差异（--check / --dry-run）/ 2 输入或 IO 错误。

设计红线（与 README「不写凭据」一致）：
  * inventory 里出现 token / secret / password / key 之类的列，**直接报错退出**；
  * 值里出现 sk- / ghp_ / AKIA / Bearer / JWT 之类形态的字符串，直接报错退出；
  * 网关 URL 不得带 userinfo（user:pass@），只允许 http/https；
  * 输出目录里只会有 config.json / init.args / manifest.json 三种文件。
"""
from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
from dataclasses import dataclass, field
from string import Template
from typing import Any, Iterable

# ── 与 djb_node/facts.py 对齐的常量（tests/test_machine_room.py 会断言一致）
PRESETS: tuple[str, ...] = ("省心", "均衡", "全力以赴")
PORT_MIN = 1024
PORT_MAX = 65535
DEFAULT_MIRROR_PORT = 1717
UPDATE_CHANNELS: tuple[str, ...] = ("stable", "beta")
LOG_LEVELS: tuple[str, ...] = ("info", "debug")
DAILY_TOKENS_MIN = 10_000
DAILY_TOKENS_MAX = 100_000_000
CONCURRENT_MIN = 1
CONCURRENT_MAX = 64
RESOURCE_CAPS: dict[str, tuple[int, int, int]] = {   # (默认, 下限, 上限)
    "cpu": (40, 5, 100),
    "gpu": (70, 10, 100),
    "mem": (70, 10, 100),
}

NODE_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
GATEWAY_RE = re.compile(r"^https?://[^\s/?#]+", re.IGNORECASE)
#: inventory 中**禁止出现**的列名（凭据一律不进批量配置）
FORBIDDEN_COLUMNS = re.compile(
    r"(token|secret|passw|credential|authorization|api[_-]?key|client[_-]?key|private[_-]?key)",
    re.IGNORECASE,
)
#: 值里出现即视为疑似凭据（宁可疑也不放过）
SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{16}"
    r"|Bearer\s+[A-Za-z0-9._-]{8,}|eyJ[A-Za-z0-9_-]{10,}\.)"
)
REQUIRED_COLUMNS = ("node_id", "gateway", "preset", "port")
OPTIONAL_DEFAULTS: dict[str, str] = {
    "update_channel": "stable",
    "update_source": "",
    "log_level": "info",
    "yield_enabled": "true",
    "labels": "{}",
    "daily_tokens": "500000",
    "weekly_tokens": "0",
    "monthly_tokens": "0",
    "concurrent": "3",
    "cpu": "40",
    "gpu": "70",
    "mem": "70",
}
TEMPLATE_FIELDS = ("node_id", "gateway", "port", "preset", "update_channel",
                   "update_source", "log_level", "yield_enabled", "labels_json")
#: 已知的非凭据列：`daily_tokens` 这类「额度」列名里虽然带 token，但不是凭据。
ALLOWED_COLUMNS = frozenset(REQUIRED_COLUMNS) | frozenset(OPTIONAL_DEFAULTS)


class FleetError(Exception):
    """输入 / IO 错误（exit 2）。"""


# ─────────────────────────────────────────────────────────── inventory
@dataclass
class Node:
    node_id: str
    gateway: str
    preset: str
    port: int
    update_channel: str = "stable"
    update_source: str = ""
    log_level: str = "info"
    yield_enabled: bool = True
    labels: dict[str, str] = field(default_factory=dict)
    limits: dict[str, int] = field(default_factory=dict)

    def context(self) -> dict[str, str]:
        """模板占位符 -> 字符串值（string.Template 只认字符串）。"""
        return {
            "node_id": self.node_id,
            "gateway": self.gateway,
            "port": str(self.port),
            "preset": self.preset,
            "update_channel": self.update_channel,
            "update_source": self.update_source,
            "log_level": self.log_level,
            "yield_enabled": "true" if self.yield_enabled else "false",
            "labels_json": json.dumps(self.labels, ensure_ascii=False, sort_keys=True),
        }

    def init_args(self) -> str:
        """渲染 `djb init` 的参数行（预设 + 本地限额；不含任何凭据）。"""
        parts = ["--preset", self.preset,
                 "--daily-tokens", str(self.limits["daily_tokens"]),
                 "--weekly-tokens", str(self.limits["weekly_tokens"]),
                 "--monthly-tokens", str(self.limits["monthly_tokens"]),
                 "--concurrent", str(self.limits["concurrent"]),
                 "--cpu", str(self.limits["cpu"]),
                 "--gpu", str(self.limits["gpu"]),
                 "--mem", str(self.limits["mem"])]
        return " ".join(shlex.quote(part) for part in parts) + "\n"


def _as_int(row_no: int, column: str, raw: str, low: int, high: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise FleetError("第 " + str(row_no) + " 行 " + column
                         + " 不是整数：" + repr(raw)) from None
    if value < low or value > high:
        raise FleetError("第 " + str(row_no) + " 行 " + column + " 必须在 "
                         + str(low) + "-" + str(high) + " 之间（收到 " + str(value) + "）")
    return value


def _as_bool(row_no: int, column: str, raw: str) -> bool:
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off", ""):
        return False
    raise FleetError("第 " + str(row_no) + " 行 " + column
                     + " 必须是 true/false（收到 " + repr(raw) + "）")


def _parse_labels(row_no: int, raw: str) -> dict[str, str]:
    text = str(raw or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise FleetError("第 " + str(row_no) + " 行 labels 不是合法 JSON：" + str(exc)) from None
        if not isinstance(parsed, dict):
            raise FleetError("第 " + str(row_no) + " 行 labels 必须是 JSON 对象")
        return {str(k): str(v) for k, v in parsed.items()}
    labels: dict[str, str] = {}
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise FleetError("第 " + str(row_no) + " 行 labels 需要用 k=v;k2=v2 或 JSON 对象")
        key, value = chunk.split("=", 1)
        labels[key.strip()] = value.strip()
    return labels


def _check_no_secrets(where: str, value: str) -> None:
    match = SECRET_VALUE_RE.search(value or "")
    if match:
        # 不打印命中的内容本身（那可能就是凭据）
        raise FleetError(where + " 疑似包含凭据（匹配 " + match.group(0)[:4] + "…）；"
                         "inventory 不允许携带任何凭据")


def load_inventory(path: str) -> list[Node]:
    if not os.path.isfile(path):
        raise FleetError("inventory 不存在：" + path)
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise FleetError("inventory 为空（至少要有表头）")
        columns = [name.strip() for name in reader.fieldnames]
        missing = [name for name in REQUIRED_COLUMNS if name not in columns]
        if missing:
            raise FleetError("inventory 缺列：" + ", ".join(missing)
                             + "（必需 " + ",".join(REQUIRED_COLUMNS) + "）")
        banned = sorted({name for name in columns
                         if FORBIDDEN_COLUMNS.search(name) and name not in ALLOWED_COLUMNS})
        if banned:
            raise FleetError("inventory 不允许携带凭据列：" + ", ".join(banned)
                             + "（凭据只能用 Secret / djb auth --set-key 注入）")
        nodes: list[Node] = []
        seen: dict[str, int] = {}
        for row_no, raw in enumerate(reader, start=2):
            if not any((value or "").strip() for value in raw.values()):
                continue                                    # 跳过空行
            row = {(key or "").strip(): (value or "").strip() for key, value in raw.items()}
            for key, value in row.items():
                _check_no_secrets("第 " + str(row_no) + " 行 " + (key or "?"), value)
            node_id = row.get("node_id", "")
            if not NODE_ID_RE.match(node_id):
                raise FleetError("第 " + str(row_no) + " 行 node_id 非法：" + repr(node_id)
                                 + "（小写字母/数字/中划线，1-63 字符，首末不能是中划线）")
            if node_id in seen:
                raise FleetError("node_id 重复：" + node_id
                                 + "（第 " + str(seen[node_id]) + " 行与第 " + str(row_no) + " 行）")
            seen[node_id] = row_no
            gateway = row.get("gateway", "")
            if not GATEWAY_RE.match(gateway):
                raise FleetError("第 " + str(row_no) + " 行 gateway 必须是 http(s) URL："
                                 + repr(gateway))
            if "@" in gateway.split("/", 3)[2]:
                raise FleetError("第 " + str(row_no) + " 行 gateway 不得带用户名/口令（userinfo）")
            preset = row.get("preset", "")
            if preset not in PRESETS:
                raise FleetError("第 " + str(row_no) + " 行 preset 必须是 "
                                 + " / ".join(PRESETS) + "（收到 " + repr(preset) + "）")
            port = _as_int(row_no, "port", row.get("port", ""), PORT_MIN, PORT_MAX)
            update_channel = row.get("update_channel") or OPTIONAL_DEFAULTS["update_channel"]
            if update_channel not in UPDATE_CHANNELS:
                raise FleetError("第 " + str(row_no) + " 行 update_channel 必须是 "
                                 + "/".join(UPDATE_CHANNELS))
            log_level = row.get("log_level") or OPTIONAL_DEFAULTS["log_level"]
            if log_level not in LOG_LEVELS:
                raise FleetError("第 " + str(row_no) + " 行 log_level 必须是 "
                                 + "/".join(LOG_LEVELS))
            update_source = row.get("update_source") or ""
            if update_source and not (update_source.startswith(("http://", "https://"))
                                      or update_source.startswith("/")):
                raise FleetError("第 " + str(row_no) + " 行 update_source 只允许 http(s) URL 或绝对路径")
            yield_enabled = _as_bool(row_no, "yield_enabled",
                                    row.get("yield_enabled", OPTIONAL_DEFAULTS["yield_enabled"]))
            labels = _parse_labels(row_no, row.get("labels", "{}"))
            limits = {
                "daily_tokens": _as_int(row_no, "daily_tokens",
                                        row.get("daily_tokens", OPTIONAL_DEFAULTS["daily_tokens"]),
                                        DAILY_TOKENS_MIN, DAILY_TOKENS_MAX),
                "weekly_tokens": _as_int(row_no, "weekly_tokens",
                                         row.get("weekly_tokens", "0"), 0, DAILY_TOKENS_MAX * 12),
                "monthly_tokens": _as_int(row_no, "monthly_tokens",
                                          row.get("monthly_tokens", "0"), 0, DAILY_TOKENS_MAX * 52),
                "concurrent": _as_int(row_no, "concurrent",
                                      row.get("concurrent", OPTIONAL_DEFAULTS["concurrent"]),
                                      CONCURRENT_MIN, CONCURRENT_MAX),
            }
            for key in ("cpu", "gpu", "mem"):
                low, high = RESOURCE_CAPS[key][1], RESOURCE_CAPS[key][2]
                limits[key] = _as_int(row_no, key, row.get(key) or str(RESOURCE_CAPS[key][0]),
                                      low, high)
            nodes.append(Node(node_id=node_id, gateway=gateway, preset=preset, port=port,
                              update_channel=update_channel, update_source=update_source,
                              log_level=log_level, yield_enabled=yield_enabled,
                              labels=labels, limits=limits))
    if not nodes:
        raise FleetError("inventory 里没有有效节点行")
    return nodes


# ─────────────────────────────────────────────────────────── 渲染
def load_template(path: str) -> Template:
    if not os.path.isfile(path):
        raise FleetError("模板不存在：" + path)
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    if not text.strip():
        raise FleetError("模板为空：" + path)
    return Template(text)


def render_config(template: Template, node: Node) -> bytes:
    try:
        text = template.substitute(node.context())
    except KeyError as exc:
        raise FleetError("模板占位符没有对应字段：" + str(exc)
                         + "（可用：" + ", ".join(TEMPLATE_FIELDS) + "）") from None
    except ValueError as exc:
        raise FleetError("模板占位符语法错误：" + str(exc)) from None
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise FleetError(node.node_id + " 渲染结果不是合法 JSON：" + str(exc)) from None
    if not isinstance(parsed, dict):
        raise FleetError(node.node_id + " 渲染结果必须是 JSON 对象")
    # 双保险：渲染出来的配置里也不许出现凭据形态的字符串
    _check_no_secrets(node.node_id + " 渲染结果", text)
    return (json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def build_outputs(template: Template, nodes: Iterable[Node]) -> dict[str, bytes]:
    """返回 {相对路径: 内容}；排序固定，保证两次生成逐字节一致。"""
    outputs: dict[str, bytes] = {}
    manifest_nodes: list[dict[str, Any]] = []
    for node in sorted(nodes, key=lambda item: item.node_id):
        config_bytes = render_config(template, node)
        args_bytes = node.init_args().encode("utf-8")
        config_rel = node.node_id + "/config.json"
        args_rel = node.node_id + "/init.args"
        outputs[config_rel] = config_bytes
        outputs[args_rel] = args_bytes
        manifest_nodes.append({
            "node_id": node.node_id,
            "mirror_port": node.port,
            "preset": node.preset,
            "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
            "init_args_sha256": hashlib.sha256(args_bytes).hexdigest(),
        })
    manifest = {
        "schema": 1,
        "generator": "djb-fleet",
        "count": len(manifest_nodes),
        "nodes": manifest_nodes,
        # 明确写出：清单里没有凭据，凭据走 Secret / djb auth --set-key
        "credentials": "none",
    }
    outputs["manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2,
                                          sort_keys=True) + "\n").encode("utf-8")
    return outputs


# ─────────────────────────────────────────────────────────── 落盘 / 比较
@dataclass
class Report:
    mode: str
    expected: int = 0
    written: int = 0
    unchanged: int = 0
    missing: list[str] = field(default_factory=list)
    differ: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)

    @property
    def differences(self) -> int:
        return len(self.missing) + len(self.differ) + len(self.extra)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "expected": self.expected,
            "written": self.written,
            "unchanged": self.unchanged,
            "missing": self.missing,
            "differ": self.differ,
            "extra": self.extra,
            "differences": self.differences,
        }


def _scan_extra(out_dir: str, expected: set[str]) -> list[str]:
    found: list[str] = []
    for root, _dirs, files in os.walk(out_dir):
        for name in files:
            rel = os.path.relpath(os.path.join(root, name), out_dir).replace(os.sep, "/")
            if rel not in expected:
                found.append(rel)
    return sorted(found)


def compare(out_dir: str, outputs: dict[str, bytes], *, report: Report,
            show_diff: bool = False, max_diffs: int = 20) -> None:
    for rel in sorted(outputs):
        target = os.path.join(out_dir, rel)
        if not os.path.exists(target):
            report.missing.append(rel)
            if show_diff:
                print("--- 缺失：" + rel)
            continue
        with open(target, "rb") as handle:
            current = handle.read()
        if current == outputs[rel]:
            report.unchanged += 1
            continue
        report.differ.append(rel)
        if show_diff and len(report.differ) <= max_diffs:
            old = current.decode("utf-8", "replace").splitlines(keepends=True)
            new = outputs[rel].decode("utf-8", "replace").splitlines(keepends=True)
            sys.stdout.writelines(difflib.unified_diff(old, new, fromfile="现有/" + rel,
                                                       tofile="期望/" + rel))
    if os.path.isdir(out_dir):
        report.extra = _scan_extra(out_dir, set(outputs))


def write_outputs(out_dir: str, outputs: dict[str, bytes], *, report: Report) -> None:
    for rel in sorted(outputs):
        target = os.path.join(out_dir, rel)
        payload = outputs[rel]
        if os.path.exists(target):
            with open(target, "rb") as handle:
                if handle.read() == payload:
                    report.unchanged += 1
                    continue
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, mode=0o750, exist_ok=True)
        # 原子写：临时文件 + rename，避免半截 config.json 被 daemon 读到
        handle = tempfile.NamedTemporaryFile(mode="wb", dir=parent or ".",
                                             prefix=".tmp-", delete=False)
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.chmod(handle.name, 0o640)
        os.replace(handle.name, target)
        report.written += 1


# ─────────────────────────────────────────────────────────── CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="djb-fleet",
        description="C5 机房版 · 100 节点批量配置渲染（纯标准库；不写凭据、不联网）",
        epilog="退出码：0 成功 / 1 有差异（--check / --dry-run）/ 2 输入或 IO 错误",
    )
    parser.add_argument("--inventory", required=True, help="inventory CSV（node_id,gateway,preset,port,...）")
    parser.add_argument("--template", required=True, help="config.json 模板（${node_id} 等占位符）")
    parser.add_argument("--out", required=True, help="输出目录")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只比较不写；有差异 exit 1")
    mode.add_argument("--dry-run", action="store_true", help="打印 diff 不写；有差异 exit 1")
    parser.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        nodes = load_inventory(args.inventory)
        template = load_template(args.template)
        outputs = build_outputs(template, nodes)
    except FleetError as exc:
        print("djb-fleet: " + str(exc), file=sys.stderr)
        return 2
    report = Report(mode="write")
    try:
        if args.check or args.dry_run:
            report.mode = "check" if args.check else "dry-run"
            compare(args.out, outputs, report=report, show_diff=args.dry_run)
        else:
            write_outputs(args.out, outputs, report=report)
            # 写完再核一遍（防止落盘错位：写入成功但内容不等于期望）
            verify = Report(mode="write")
            compare(args.out, outputs, report=verify)
            if verify.differences:
                raise OSError("落盘后校验不一致：" + ", ".join(verify.missing + verify.differ))
    except OSError as exc:
        print("djb-fleet: IO 错误：" + str(exc), file=sys.stderr)
        return 2
    report.expected = len(outputs)
    summary = report.to_dict()
    summary["nodes"] = len(nodes)
    summary["out"] = os.path.abspath(args.out)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("djb-fleet: 模式=" + report.mode + " 节点=" + str(len(nodes))
              + " 期望文件=" + str(report.expected)
              + " 写入=" + str(report.written)
              + " 未变=" + str(report.unchanged)
              + " 缺失=" + str(len(report.missing))
              + " 差异=" + str(len(report.differ))
              + " 多余=" + str(len(report.extra)))
    if report.mode in ("check", "dry-run") and report.differences:
        return 1
    return 0


if __name__ == "__main__":                                      # pragma: no cover
    raise SystemExit(main())
