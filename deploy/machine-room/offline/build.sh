#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# ─────────────────────────────────────────────────────────────────────────────
# C5 机房版 · 离线包构建（build.sh）
#
# 产物（默认落在 deploy/machine-room/offline/dist/）：
#   djb_node-<version>-py3-none-any.whl   本仓 wheel（零第三方运行时依赖）
#   install.sh                            离线安装脚本（pip --no-index --find-links）
#   verify.sh                             哈希/签名校验脚本
#   manifest.json                         版本 + 文件清单 + 每个文件的 sha256 + 签名声明
#   sha256sums                            上面四个文件（含 manifest 自己以外的全部）的哈希清单
#   sha256sums.sig                        **仅当** DJB_OFFLINE_SIGNING_KEY 存在时生成
#
# 纪律：
#   * 不联网安装：install.sh 只用 --no-index --find-links 从本目录装 wheel；
#   * 不硬编码任何密钥：签名私钥只能由 DJB_OFFLINE_SIGNING_KEY 指向外部文件；
#   * 公钥不落包：验签方用 DJB_OFFLINE_VERIFY_KEY 提供；
#   * 重复构建先清旧载荷，避免「旧 wheel 混进新包」。
#
# 用法：
#   ./build.sh                       # 自动找工具链构建 wheel
#   ./build.sh --out /tmp/pkg        # 指定输出目录
#   ./build.sh --wheel /tmp/x.whl    # 复用已有 wheel（完全不联网、不依赖构建后端）
#   PYTHON=/path/to/python ./build.sh
#
# 环境变量：
#   PYTHON                    解释器（默认 python3，需 3.10+）
#   DJB_OFFLINE_OUT           输出目录（等价 --out）
#   DJB_OFFLINE_WHEEL         已有 wheel 路径（等价 --wheel）
#   DJB_OFFLINE_SIGNING_KEY   PEM 私钥路径；存在则对 sha256sums 生成 .sig
# ─────────────────────────────────────────────────────────────────────────────
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH= cd -- "$HERE/../../.." && pwd)   # offline -> machine-room -> deploy -> 仓库根
PYTHON=${PYTHON:-python3}
OUT=${DJB_OFFLINE_OUT:-$HERE/dist}
WHEEL_IN=${DJB_OFFLINE_WHEEL:-}

usage() {
    sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT=${2:?--out 需要目录}; shift 2 ;;
        --wheel) WHEEL_IN=${2:?--wheel 需要文件}; shift 2 ;;
        --python) PYTHON=${2:?--python 需要路径}; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "build.sh: 未知参数 $1（--help 看用法）" >&2; exit 2 ;;
    esac
done

# 路径守卫：后面的 rm -f 只允许在包输出目录里发生
[ -n "$OUT" ] || { echo "build.sh: 输出目录为空" >&2; exit 2; }
case "$OUT" in
    /|/usr|/etc|/var|/bin|/sbin) echo "build.sh: 拒绝把包输出到系统目录 $OUT" >&2; exit 2 ;;
esac

VERSION=$(PYTHONPATH="$REPO" "$PYTHON" -c 'import djb_node; print(djb_node.__version__)')
[ -n "$VERSION" ] || { echo "build.sh: 读不到 djb_node.__version__" >&2; exit 2; }
echo "build.sh: djb-node $VERSION -> $OUT"

WORK=$(mktemp -d "${TMPDIR:-/tmp}/djb-offline-build.XXXXXX")
trap 'rm -rf "$WORK"' EXIT INT TERM HUP

# ── 1) wheel（三条路线，按「越不依赖外网越优先」排序）
WHEEL=$WHEEL_IN
if [ -z "$WHEEL" ]; then
    mkdir -p "$WORK/wheel"
    built=0
    # 1.1 python -m build --no-isolation（有 build 包且本机已装 setuptools/wheel）
    if "$PYTHON" -m build --version >/dev/null 2>&1; then
        if "$PYTHON" -m build --wheel --no-isolation --outdir "$WORK/wheel" "$REPO" >"$WORK/build.log" 2>&1; then
            built=1; echo "build.sh: wheel 由 'python -m build --no-isolation' 构建"
        fi
    fi
    # 1.2 pip wheel --no-build-isolation（纯本地构建后端）
    if [ "$built" -eq 0 ]; then
        if "$PYTHON" -m pip wheel "$REPO" --no-deps --no-build-isolation -w "$WORK/wheel" \
                >>"$WORK/build.log" 2>&1; then
            built=1; echo "build.sh: wheel 由 'pip wheel --no-build-isolation' 构建"
        fi
    fi
    # 1.3 pip wheel（允许构建隔离；构建机若联网会临时取 setuptools/wheel）
    if [ "$built" -eq 0 ]; then
        if "$PYTHON" -m pip wheel "$REPO" --no-deps -w "$WORK/wheel" >>"$WORK/build.log" 2>&1; then
            built=1; echo "build.sh: wheel 由 'pip wheel'（构建隔离）构建"
        fi
    fi
    if [ "$built" -eq 0 ]; then
        echo "build.sh: 构建 wheel 失败；三种路线都没成功。" >&2
        echo "         常见原因：本机缺 setuptools/wheel 且不能联网。" >&2
        echo "         解决：先装 wheel（pip install wheel），或用 --wheel 复用已有 wheel。" >&2
        tail -20 "$WORK/build.log" >&2 || true
        exit 2
    fi
    WHEEL=$(find "$WORK/wheel" -maxdepth 1 -name "djb_node-$VERSION-*.whl" -print | head -1)
    if [ -z "$WHEEL" ]; then
        echo "build.sh: 没找到 djb_node-$VERSION-*.whl（构建出的文件名不对？）" >&2
        ls -1 "$WORK/wheel" >&2 || true
        exit 2
    fi
fi
[ -f "$WHEEL" ] || { echo "build.sh: wheel 不存在：$WHEEL" >&2; exit 2; }

# ── 2) 组装载荷（先清旧载荷，避免旧 wheel 混进新包）
mkdir -p "$OUT"
rm -f "$OUT"/djb_node-*.whl "$OUT"/sha256sums "$OUT"/sha256sums.sig "$OUT"/manifest.json
cp "$WHEEL" "$OUT"/
cp "$HERE/install.sh" "$HERE/verify.sh" "$OUT"/
chmod 0755 "$OUT/install.sh" "$OUT/verify.sh"
chmod 0644 "$OUT/$(basename "$WHEEL")"

# ── 3) 签名声明（有私钥才签名；绝不硬编码密钥）
SIGNED=0
if [ -n "${DJB_OFFLINE_SIGNING_KEY:-}" ]; then
    if [ ! -f "$DJB_OFFLINE_SIGNING_KEY" ]; then
        echo "build.sh: DJB_OFFLINE_SIGNING_KEY 指向的私钥不存在：$DJB_OFFLINE_SIGNING_KEY" >&2
        exit 2
    fi
    if ! command -v openssl >/dev/null 2>&1; then
        echo "build.sh: 设置了签名私钥但没有 openssl" >&2
        exit 2
    fi
    SIGNED=1
fi

# ── 4) manifest.json + sha256sums（用 python 写，避免 sha256sum/shasum 差异）
DJB_BUILD_SIGNED=$SIGNED "$PYTHON" - "$OUT" "$VERSION" "$REPO" <<'PY'
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

out, version, repo = sys.argv[1], sys.argv[2], sys.argv[3]
signed = os.environ.get("DJB_BUILD_SIGNED") == "1"

def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()

wheel = sorted(name for name in os.listdir(out)
               if name.startswith("djb_node-%s-" % version) and name.endswith(".whl"))
if len(wheel) != 1:
    sys.exit("manifest: 期望恰好一个 wheel，实际 %d 个" % len(wheel))
payload = [wheel[0], "install.sh", "verify.sh"]

try:
    import tomllib
    with open(os.path.join(repo, "pyproject.toml"), "rb") as handle:
        requires = tomllib.load(handle)["project"].get("requires-python", ">=3.10")
except Exception:
    requires = ">=3.10"

manifest = {
    "schema": 1,
    "name": "djb-node",
    "version": version,
    "kind": "offline",
    "requires_python": requires,
    "runtime_dependencies": [],
    "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "built_by": "deploy/machine-room/offline/build.sh",
    "files": [
        {"path": name,
         "sha256": sha256(os.path.join(out, name)),
         "size": os.path.getsize(os.path.join(out, name))}
        for name in payload
    ],
    "signature": ({"algorithm": "openssl-dgst-sha256", "file": "sha256sums.sig"}
                  if signed else None),
}
with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as handle:
    json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")

# sha256sums 覆盖「载荷 + manifest.json」；签名（可选）覆盖 sha256sums 本身
targets = payload + ["manifest.json"]
lines = []
for name in targets:
    lines.append("%s  %s\n" % (sha256(os.path.join(out, name)), name))
with open(os.path.join(out, "sha256sums"), "w", encoding="utf-8") as handle:
    handle.writelines(lines)
os.chmod(os.path.join(out, "sha256sums"), 0o644)
os.chmod(os.path.join(out, "manifest.json"), 0o644)
print("manifest: %d 个文件（wheel=%s）" % (len(targets), wheel[0]))
PY

# ── 5) 签名（可选）
if [ "$SIGNED" = "1" ]; then
    openssl dgst -sha256 -sign "$DJB_OFFLINE_SIGNING_KEY" \
        -out "$OUT/sha256sums.sig" "$OUT/sha256sums"
    chmod 0644 "$OUT/sha256sums.sig"
    echo "build.sh: 已签名（openssl dgst -sha256 -sign，私钥来自 DJB_OFFLINE_SIGNING_KEY）"
    echo "          验签需要 DJB_OFFLINE_VERIFY_KEY=<公钥 PEM>"
else
    echo "build.sh: 未签名，仅校验哈希（如需签名：DJB_OFFLINE_SIGNING_KEY=<私钥 PEM> ./build.sh）"
fi

# ── 6) 自检：用 verify.sh 验一遍自己刚打的包
"$OUT/verify.sh"
echo "build.sh: 完成 -> $OUT"
ls -l "$OUT"
