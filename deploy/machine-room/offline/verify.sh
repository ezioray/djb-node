#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# ─────────────────────────────────────────────────────────────────────────────
# C5 机房版 · 离线包校验（verify.sh）
#
# 做三件事，任一步失败就非 0 退出：
#   1) 签名（可选）：包内有 sha256sums.sig 且给了 DJB_OFFLINE_VERIFY_KEY -> openssl 验签；
#      有签名但没给公钥：默认只告警（DJB_OFFLINE_STRICT=1 时拒绝）；
#   2) 哈希：sha256sum -c（或 macOS 的 shasum -a 256 -c）逐文件比对；
#   3) 交叉核对：manifest.json 里的 version / 文件清单 / sha256 与磁盘一致（需要 python3）。
#
# 篡改任意一个文件（wheel / install.sh / verify.sh / manifest.json / sha256sums）都必须失败。
# 注意：哈希只能防「传输损坏 / 意外改动」；要防「有权限的恶意替换」必须依赖签名
#       （DJB_OFFLINE_SIGNING_KEY 生成的 sha256sums.sig + 独立保管的公钥）。
#
# 环境变量：
#   DJB_OFFLINE_VERIFY_KEY  公钥 PEM（验签用；缺失时只校验哈希）
#   DJB_OFFLINE_STRICT      1 = 包内有签名但没公钥时直接失败（默认 0：告警后继续）
#   PYTHON                  解释器（默认 python3；缺失时跳过第 3 步）
# ─────────────────────────────────────────────────────────────────────────────
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$HERE"

fail() {
    echo "verify: 失败：$*" >&2
    exit 1
}

[ -f sha256sums ] || fail "缺少 sha256sums（这不是一个完整的离线包）"
[ -f manifest.json ] || fail "缺少 manifest.json"
[ -s sha256sums ] || fail "sha256sums 是空的"

# ── 1) 签名（可选）
if [ -f sha256sums.sig ]; then
    if [ -n "${DJB_OFFLINE_VERIFY_KEY:-}" ]; then
        [ -f "$DJB_OFFLINE_VERIFY_KEY" ] || fail "公钥不存在：$DJB_OFFLINE_VERIFY_KEY"
        command -v openssl >/dev/null 2>&1 || fail "包内有签名，但本机没有 openssl"
        if openssl dgst -sha256 -verify "$DJB_OFFLINE_VERIFY_KEY" \
                -signature sha256sums.sig sha256sums >/dev/null 2>&1; then
            echo "verify: 签名校验通过（openssl dgst -sha256 -verify）"
        else
            fail "签名校验不通过（sha256sums 被改过，或公钥不对）"
        fi
    elif [ "${DJB_OFFLINE_STRICT:-0}" = "1" ]; then
        fail "包内有签名但未提供 DJB_OFFLINE_VERIFY_KEY（STRICT=1 拒绝降级为仅哈希）"
    else
        echo "verify: 警告：包内有签名但未提供 DJB_OFFLINE_VERIFY_KEY，跳过签名校验（仅校验哈希）"
    fi
else
    echo "verify: 未签名，仅校验哈希"
fi

# ── 2) 哈希
if command -v sha256sum >/dev/null 2>&1; then
    if ! sha256sum -c sha256sums >/dev/null 2>&1; then
        sha256sum -c sha256sums || true
        fail "哈希不匹配：文件被改动、缺失或下载损坏"
    fi
elif command -v shasum >/dev/null 2>&1; then
    if ! shasum -a 256 -c sha256sums >/dev/null 2>&1; then
        shasum -a 256 -c sha256sums || true
        fail "哈希不匹配：文件被改动、缺失或下载损坏"
    fi
else
    fail "既没有 sha256sum 也没有 shasum，无法校验"
fi
echo "verify: sha256sums 全部匹配（$(wc -l < sha256sums | tr -d ' ') 个文件）"

# ── 3) manifest 与磁盘交叉核对
PY=${PYTHON:-python3}
if command -v "$PY" >/dev/null 2>&1; then
    "$PY" - "$HERE" <<'PY'
import hashlib
import json
import os
import sys

root = sys.argv[1]

def die(message):
    print("verify: 失败：" + message, file=sys.stderr)
    raise SystemExit(1)

with open(os.path.join(root, "manifest.json"), encoding="utf-8") as handle:
    manifest = json.load(handle)

if manifest.get("name") != "djb-node":
    die("manifest.name 不是 djb-node")
version = str(manifest.get("version") or "")
if not version:
    die("manifest.version 缺失")
if manifest.get("kind") != "offline":
    die("manifest.kind 不是 offline")
if manifest.get("runtime_dependencies") not in ([], None):
    die("manifest 声明了运行时依赖（本包必须是零依赖）")

files = manifest.get("files") or []
if not files:
    die("manifest.files 为空")
wheel = [item for item in files if str(item.get("path", "")).endswith(".whl")]
if len(wheel) != 1:
    die("manifest 里必须恰好有一个 wheel")
if version not in str(wheel[0]["path"]):
    die("wheel 文件名与 manifest.version 不一致")

with open(os.path.join(root, "sha256sums"), encoding="utf-8") as handle:
    listed = []
    for line in handle:
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            die("sha256sums 行格式不对：" + line[:40])
        listed.append(parts[1].lstrip("*").strip())
expected = sorted([str(item.get("path")) for item in files] + ["manifest.json"])
if sorted(listed) != expected:
    missing = sorted(set(expected) - set(listed))
    extra = sorted(set(listed) - set(expected))
    die("sha256sums 与 manifest 的文件清单不一致（缺 %s / 多 %s）"
        % (missing or "-", extra or "-"))

for item in files:
    path = str(item.get("path") or "")
    if not path or os.path.basename(path) != path:
        die("manifest 文件名不合法：" + repr(path))
    target = os.path.join(root, path)
    if not os.path.isfile(target):
        die("manifest 声明的文件不存在：" + path)
    digest = hashlib.sha256()
    with open(target, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != item.get("sha256"):
        die(path + " 的 sha256 与 manifest 不一致")
    if int(item.get("size") or -1) != os.path.getsize(target):
        die(path + " 的大小与 manifest 不一致")

print("verify: manifest 交叉核对通过（djb-node " + version
      + "，%d 个载荷文件）" % len(files))
PY
else
    echo "verify: 没有 $PY，跳过 manifest 交叉核对（哈希已校验）"
fi

echo "verify: OK"
