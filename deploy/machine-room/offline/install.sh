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
#   ./install.sh --djb-home /var/lib/djb-node --gateway https://gw.example.com
#                                         # 装完顺手写 config.json（含**显式 update_source**）
#   ./install.sh --update-source <URL>    # 覆盖更新源（默认取包内 config.template.json）
#   ./install.sh --config-only --djb-home <DIR> --gateway <URL>
#                                         # 已经装过 wheel、只想补/改 config.json（不碰 pip）
#
# 环境变量：PYTHON（默认 python3）、DJB_OFFLINE_VERIFY_KEY（验签公钥，见 verify.sh）、
#           DJB_INSTALL_UPDATE_SOURCE（等价 --update-source）
#
# 0.3.1（RC8.3-C）：机房通道的 update_source 以前没有载体（审计 P1-3）。现在：
#   * 包里带 config.template.json（显式 update_source/edition）；
#   * 本脚本 --djb-home 时按模板写 config.json，不写就打印**带 --update-source 的 init 命令**。
# ─────────────────────────────────────────────────────────────────────────────
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${PYTHON:-python3}
PREFIX=""
SKIP_VERIFY=0
DRY_RUN=0
DJB_HOME_DIR=""
GATEWAY=""
EDITION="machine-room"
UPDATE_SOURCE=${DJB_INSTALL_UPDATE_SOURCE:-}
CONFIG_ONLY=0

usage() {
    sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --python) PYTHON=${2:?--python 需要路径}; shift 2 ;;
        --prefix) PREFIX=${2:?--prefix 需要目录}; shift 2 ;;
        --skip-verify) SKIP_VERIFY=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --djb-home) DJB_HOME_DIR=${2:?--djb-home 需要目录}; shift 2 ;;
        --gateway) GATEWAY=${2:?--gateway 需要地址}; shift 2 ;;
        --edition) EDITION=${2:?--edition 需要档位}; shift 2 ;;
        --update-source) UPDATE_SOURCE=${2:?--update-source 需要 URL 或目录}; shift 2 ;;
        --config-only) CONFIG_ONLY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install.sh: 未知参数 $1（--help 看用法）" >&2; exit 2 ;;
    esac
done

# 解释器自检：需要 3.10+（pyproject requires-python）
command -v "$PYTHON" >/dev/null 2>&1 || { echo "install.sh: 找不到解释器 $PYTHON" >&2; exit 2; }
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || { echo "install.sh: 需要 Python 3.10+（当前 $($PYTHON -V 2>&1)）" >&2; exit 2; }

# 0.3.1：--config-only —— 只写 config.json（不校验 wheel、不跑 pip）。
# 用途：机房批量装机时 wheel 已由镜像铺好，只需把显式 update_source 落盘。
if [ "$CONFIG_ONLY" = "1" ]; then
    [ -n "$DJB_HOME_DIR" ] || { echo "install.sh: --config-only 需要 --djb-home" >&2; exit 2; }
    TEMPLATE_FILE="$HERE/config.template.json"
    [ -n "$UPDATE_SOURCE" ] || UPDATE_SOURCE=$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("update_source",""))' "$TEMPLATE_FILE" 2>/dev/null || true)
    [ -n "$UPDATE_SOURCE" ] || { echo "install.sh: 缺 update_source（包内无模板且未给 --update-source）" >&2; exit 2; }
    if [ "$DRY_RUN" = "1" ]; then
        echo "install.sh: 将写 $DJB_HOME_DIR/config.json（update_source=$UPDATE_SOURCE edition=$EDITION）"
        exit 0
    fi
    mkdir -p "$DJB_HOME_DIR"
    chmod 0700 "$DJB_HOME_DIR"
    "$PYTHON" -c '
import json, os, sys
template, target, source, edition, gateway = sys.argv[1:6]
try:
    with open(template, encoding="utf-8") as handle:
        config = json.load(handle)
except OSError:
    config = {"mirror_port": 1717, "update_channel": "stable", "log_level": "info",
              "yield_enabled": False}
config["update_source"] = source
config["edition"] = edition
if gateway:
    config["gateway_url"] = gateway
with open(target, "w", encoding="utf-8") as handle:
    json.dump(config, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
os.chmod(target, 0o600)
print("install.sh: 已写 " + target + "（update_source 已显式，edition=" + edition + "）")
' "$TEMPLATE_FILE" "$DJB_HOME_DIR/config.json" "$UPDATE_SOURCE" "$EDITION" "$GATEWAY"
    exit 0
fi

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

# 0.3.1：更新源来自包内 config.template.json（带 --update-source 时以参数为准）
TEMPLATE="$HERE/config.template.json"
TEMPLATE_SOURCE=""
if [ -f "$TEMPLATE" ]; then
    TEMPLATE_SOURCE=$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("update_source",""))' "$TEMPLATE")
fi
[ -n "$UPDATE_SOURCE" ] || UPDATE_SOURCE=$TEMPLATE_SOURCE
[ -n "$UPDATE_SOURCE" ] || { echo "install.sh: 包里没有 config.template.json 也没有 --update-source；机房版必须显式给更新源（留空 = 不联网的 mock 源）" >&2; exit 2; }

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
    if [ -n "$DJB_HOME_DIR" ]; then
        echo "install.sh: 并将写 $DJB_HOME_DIR/config.json（update_source=$UPDATE_SOURCE edition=$EDITION）"
    else
        echo "install.sh: 未给 --djb-home，不写 config；装完请用带 --update-source 的 init（见下方提示）"
    fi
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

if [ -n "$DJB_HOME_DIR" ]; then
    # 0.3.1：把显式 update_source 落到 config.json（目录 0700 / 文件 0600，与客户端一致）
    mkdir -p "$DJB_HOME_DIR"
    chmod 0700 "$DJB_HOME_DIR"
    "$PYTHON" -c '
import json, os, sys
template, target, source, edition, gateway = sys.argv[1:6]
try:
    with open(template, encoding="utf-8") as handle:
        config = json.load(handle)
except OSError:
    config = {"mirror_port": 1717, "update_channel": "stable", "log_level": "info",
              "yield_enabled": False}
config["update_source"] = source
config["edition"] = edition
if gateway:
    config["gateway_url"] = gateway
with open(target, "w", encoding="utf-8") as handle:
    json.dump(config, handle, ensure_ascii=False, indent=2)
    handle.write("
")
os.chmod(target, 0o600)
print("install.sh: 已写 " + target + "（update_source 已显式，edition=" + edition + "）")
' "$TEMPLATE" "$DJB_HOME_DIR/config.json" "$UPDATE_SOURCE" "$EDITION" "$GATEWAY"
fi

echo "install.sh: 下一步（每个节点一次）："
echo "  djb init --no-env --edition $EDITION --update-source $UPDATE_SOURCE"
echo "                               # 建 ~/.djb（或 DJB_HOME）与 state.db，并显式登记更新源"
echo "  djb auth --set-key -         # 从 stdin 读 client key（不回显，不落日志）"
echo "  djb start                    # 起 daemon（只绑 127.0.0.1:1717）"
echo "  djb status                   # 看状态 / 原因 / 下一步"
