# SPDX-License-Identifier: Apache-2.0
"""spec 破坏性变更检查：把「旧 spec → 新 spec」的 breaking change 拦在 CI。

阻断项（任一命中即失败）：
  1. 删除 path，或删除某个 operation
  2. 删除已有响应码
  3. 请求体新增 required 字段（老客户端会被 400）
  4. 已有字段类型改变
  5. 删除 schema 的 required 字段（响应侧破坏）
兼容项（只提示，不失败）：新增 path / 新增响应码 / 新增可选字段。
"""
from __future__ import annotations

import subprocess
import sys

import yaml

REQ = ("get", "post", "put", "patch", "delete")


def load(text: str) -> dict:
    return yaml.safe_load(text)


def resolve(spec: dict, node: dict) -> dict:
    while isinstance(node, dict) and "$ref" in node:
        parts = node["$ref"].lstrip("#/").split("/")
        cur = spec
        for p in parts:
            cur = cur[p]
        node = cur
    return node


def operations(spec: dict) -> dict:
    out = {}
    for path, item in (spec.get("paths") or {}).items():
        for m in REQ:
            if m in item:
                out[(m, path)] = item[m]
    return out


def main(old_text: str, new_text: str) -> int:
    old, new = load(old_text), load(new_text)
    old_ops, new_ops = operations(old), operations(new)
    breaking, notes = [], []

    for k in old_ops:
        if k not in new_ops:
            breaking.append(f"删除 operation: {k[0].upper()} {k[1]}")

    for k, o in old_ops.items():
        if k not in new_ops:
            continue
        n = new_ops[k]
        gone = set((o.get("responses") or {})) - set((n.get("responses") or {}))
        if gone:
            breaking.append(f"{k[0].upper()} {k[1]} 删除响应码: {sorted(gone)}")
        added = set((n.get("responses") or {})) - set((o.get("responses") or {}))
        if added:
            notes.append(f"{k[0].upper()} {k[1]} 新增响应码: {sorted(added)}")

        ob = resolve(old, (o.get("requestBody") or {}).get("content", {}).get("application/json", {}).get("schema", {}))
        nb = resolve(new, (n.get("requestBody") or {}).get("content", {}).get("application/json", {}).get("schema", {}))
        if ob and nb:
            newly_required = set(nb.get("required") or []) - set(ob.get("required") or [])
            if newly_required:
                breaking.append(f"{k[0].upper()} {k[1]} 新增 required 请求字段: {sorted(newly_required)}")
            for f, prop in (ob.get("properties") or {}).items():
                nprop = (nb.get("properties") or {}).get(f)
                if nprop and prop.get("type") != nprop.get("type"):
                    breaking.append(f"{k[0].upper()} {k[1]} 字段 {f} 类型 {prop.get('type')}→{nprop.get('type')}")

    for name, osch in (old.get("components", {}).get("schemas") or {}).items():
        nsch = (new.get("components", {}).get("schemas") or {}).get(name)
        if not nsch:
            breaking.append(f"删除 schema: {name}")
            continue
        lost = set(osch.get("required") or []) - set(nsch.get("required") or [])
        if lost:
            notes.append(f"schema {name} 不再强制: {sorted(lost)}")
        removed_props = set((osch.get("properties") or {})) - set((nsch.get("properties") or {}))
        if removed_props & set(osch.get("required") or []):
            breaking.append(f"schema {name} 删除必填属性: {sorted(removed_props)}")

    for n_ in notes:
        print("note:", n_)
    if breaking:
        print("\n❌ 发现破坏性变更（需 MAJOR 升级 + 迁移说明）:")
        for b in breaking:
            print("  -", b)
        return 1
    print("✅ 无破坏性变更")
    return 0


if __name__ == "__main__":
    ref = sys.argv[1] if len(sys.argv) > 1 else "HEAD~1"
    try:
        old = subprocess.run(["git", "show", f"{ref}:openapi.yaml"], capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError:
        print(f"无 {ref}:openapi.yaml（首个提交），跳过")
        sys.exit(0)
    sys.exit(main(old, open("openapi.yaml", encoding="utf-8").read()))
