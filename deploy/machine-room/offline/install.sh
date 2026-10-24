#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# ─────────────────────────────────────────────────────────────────────────────
# C5 机房版 · 离线安装（install.sh）
#
# 只从**本目录**的 wheel 安装，绝不联网：
#   pip install --no-index --find-links <本目录> --no-deps <wheel>
# 安装前默认先跑 verify.sh（哈希 + 可选签名）；校验不过就装不了。
#
# 用法：
#   sudo ./install.sh                     # 装进当前 Python 环境（机房节点常用系统 Python）
#   ./install.sh --prefix /opt/djb        # 装进前缀目录（产物在 /opt/djb/bin/djb）
#   ./install.sh --python /usr/bin/python3.11
#   ./install.sh --skip-verify            # 仅限已在可信介质上校验过（不推荐）
#   ./install.sh --dry-run                # 只打印将执行的命令
#
# 环境变量：PYTHON（默认 python3）、DJB_OFFLINE_VERIFY_KEY（验签公钥，见 verify.sh）
# ─────────────────────────────────────────────────────────────────────────────
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${PYTHON:-python3}
PREFIX=""
SKIP_VERIFY=0
DRY_RUN=0

usage() {
    sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --python) PYTHON=${2:?--python 需要路径}; shift 2 ;;
        --prefix) PREFIX=${2:?--prefix 需要目录}; shift 2 ;;
        --skip-verify) SKIP_VERIFY=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install.sh: 未知参数 $1（--help 看用法）" >&2; exit 2 ;;
    esac
done

# 解释器自检：需要 3.10+（pyproject requires-python）
command -v "$PYTHON" >/dev/null 2>&1 || { echo "install.sh: 找不到解释器 $PYTHON" >&2; exit 2; }
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || { echo "install.sh: 需要 Python 3.10+（当前 $($PYTHON -V 2>&1)）" >&2; exit 2; }

# 找 wheel（离线包里只应有一个）
WHEEL=""
for candidate in "$HERE"/djb_node-*.whl; do
    [ -e "$candidate" ] || continue
    if [ -n "$WHEEL" ]; then
        echo "install.sh: 目录里有多个 wheel，拒绝猜测该装哪个：" >&2
        ls -1 "$HERE"/djb_node-*.whl >&2
        exit 2
    fi
    WHEEL=$candidate
done
[ -n "$WHEEL" ] || { echo "install.sh: 本目录没有 djb_node-*.whl（这不是一个完整的离线包）" >&2; exit 2; }

# 先校验再安装（--dry-run 也校验：这样「dry-run 通过」才有意义）
if [ "$SKIP_VERIFY" = "1" ]; then
    echo "install.sh: 警告：已跳过 verify.sh（--skip-verify）"
else
    "$HERE/verify.sh"
fi

set -- -m pip install --no-index --find-links "$HERE" --no-deps --no-build-isolation
if [ -n "$PREFIX" ]; then
    set -- "$@" --prefix "$PREFIX"
fi
set -- "$@" "$WHEEL"

if [ "$DRY_RUN" = "1" ]; then
    echo "install.sh: 将执行：$PYTHON $*"
    exit 0
fi

# PIP_NO_INDEX 双保险：即使命令行被改坏也不会去公网
PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1 "$PYTHON" "$@"

# 装完自检：包装得上、import 得动、CLI 能跑。
# -I（隔离模式）= 忽略 PYTHONPATH 且不把当前目录加入 sys.path：
# 否则在仓库根里跑 install.sh 会 import 到源码目录，装失败也会「看起来成功」。
"$PYTHON" -I -c 'import djb_node, os; print("install.sh: djb-node " + djb_node.__version__ + " 已安装（" + os.path.dirname(djb_node.__file__) + "）")'

if command -v djb >/dev/null 2>&1; then
    djb --version
else
    echo "install.sh: 提示：djb 不在 PATH 上；用 $PYTHON -m djb_node 或把 <prefix>/bin 加进 PATH"
fi

echo "install.sh: 下一步（每个节点一次）："
echo "  djb init --no-env            # 建 ~/.djb（或 DJB_HOME）与 state.db"
echo "  djb auth --set-key -         # 从 stdin 读 client key（不回显，不落日志）"
echo "  djb start                    # 起 daemon（只绑 127.0.0.1:1717）"
echo "  djb status                   # 看状态 / 原因 / 下一步"
