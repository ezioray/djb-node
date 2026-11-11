#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# ─────────────────────────────────────────────────────────────────────────────
# 官方特供版（B 端多模型供方）· 一键安装 install.sh
#
# 做四件事，且**只做**这四件：
#   1) 建系统用户 djb-supply（已存在就跳过，不改它的 shell / home）
#   2) 建状态目录 $DJB_HOME（0700，属主 djb-supply）并铺上本目录的 config.json
#      —— **只在不存在时写**，重跑绝不覆盖已有配置（更不会覆盖凭据）
#   3) 建 venv 并装 djb-node（有 --wheel 就用本地 wheel，离线可装）
#   4) 装 systemd 单元 /etc/systemd/system/djb-official-supply.service
#
# 不碰别的服务：不 stop / 不 restart / 不 disable 任何既有单元；
# 本单元也**不会**被自动 start（要 --start 显式要求）。
#
# 用法：
#   sudo ./install.sh --gateway https://gateway.example.com
#   sudo ./install.sh --gateway https://gateway.example.com --wheel ./djb_node-0.3.4-py3-none-any.whl
#   sudo ./install.sh --gateway https://gateway.example.com --start
#   ./install.sh --dry-run                       # 只打印将执行的动作（不需要 root）
#
# 装完之后（**以服务用户身份**跑，凭据落在 0600 文件里）：
#   sudo -u djb-supply DJB_HOME=/var/lib/djb-official-supply \
#        /opt/djb-official-supply/venv/bin/djb auth --set-key -
#
# 环境变量：PYTHON（默认 python3，需要 3.10+）
# ─────────────────────────────────────────────────────────────────────────────
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${PYTHON:-python3}

HOME_DIR=/var/lib/djb-official-supply
PREFIX=/opt/djb-official-supply
RUN_USER=djb-supply
UNIT_PATH=/etc/systemd/system/djb-official-supply.service
GATEWAY=""
UPDATE_SOURCE="https://tkbank.ai/download/files/update-manifest.json"
WHEEL=""
DO_START=0
DRY_RUN=0

usage() {
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
}

say() { printf '%s\n' "$*"; }
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        say "[dry-run] $*"
    else
        "$@"
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --gateway) GATEWAY=${2:?--gateway 需要 URL}; shift 2 ;;
        --update-source) UPDATE_SOURCE=${2:?--update-source 需要 URL}; shift 2 ;;
        --home) HOME_DIR=${2:?--home 需要目录}; shift 2 ;;
        --prefix) PREFIX=${2:?--prefix 需要目录}; shift 2 ;;
        --user) RUN_USER=${2:?--user 需要用户名}; shift 2 ;;
        --wheel) WHEEL=${2:?--wheel 需要文件路径}; shift 2 ;;
        --start) DO_START=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) say "install.sh: 未知参数 $1（--help 看用法）" >&2; exit 2 ;;
    esac
done

[ -n "$GATEWAY" ] || { say "install.sh: 必须给 --gateway（本机的服务端地址）" >&2; exit 2; }
case "$GATEWAY" in
    https://*|http://127.0.0.1:*|http://localhost:*) ;;
    http://*) say "install.sh: 明文 http 只允许本机回环（当前 $GATEWAY）" >&2; exit 2 ;;
    *) say "install.sh: --gateway 必须是 http(s) URL（当前 $GATEWAY）" >&2; exit 2 ;;
esac

if [ "$DRY_RUN" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
    say "install.sh: 需要 root（要建用户 / 写 /etc/systemd）；先看计划可用 --dry-run" >&2
    exit 2
fi

command -v "$PYTHON" >/dev/null 2>&1 || { say "install.sh: 找不到解释器 $PYTHON" >&2; exit 2; }
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || { say "install.sh: 需要 Python 3.10+（当前 $($PYTHON -V 2>&1)）" >&2; exit 2; }

# ── 1) 用户（幂等：已存在就不动）
if id -u "$RUN_USER" >/dev/null 2>&1; then
    say "√ 用户已存在：$RUN_USER（保持原样）"
else
    run useradd --system --home-dir "$HOME_DIR" --create-home \
        --shell /usr/sbin/nologin "$RUN_USER"
fi

# ── 2) 状态目录 + 配置（幂等：目录只补权限，配置只在缺失时写）
run install -d -m 0700 -o "$RUN_USER" -g "$RUN_USER" "$HOME_DIR"
if [ -f "$HOME_DIR/config.json" ]; then
    say "√ 已有配置，不覆盖：$HOME_DIR/config.json"
    say "  （要改服务端地址：djb init --gateway <URL> 或直接编辑后 djb daemon reload）"
    say "  ⚠ 升级请确认档位：djb init --edition official-supply"
    say "     （0.3.3 及以前的 config 没有 edition 字段；本脚本第 3 步会补；漏了则"
    say "     update --status 的 edition/source_channel 为空，自动升级只剩 config 里的 URL）"
else
    if [ "$DRY_RUN" -eq 1 ]; then
        say "[dry-run] 写 $HOME_DIR/config.json（gateway_url=$GATEWAY）"
    else
        # 原子写：临时文件 -> mv，并且先落成 0600 再改属主
        tmp=$(mktemp "$HOME_DIR/.config.json.XXXXXX")
        # 两个字段都要落到正式环境：gateway_url 由 --gateway 决定；
        # update_source 决定客户端能不能自动升级（沿用旧版占位/旧域会永远升不了）。
        sed -e "s#\"gateway_url\": \"[^\"]*\"#\"gateway_url\": \"$GATEWAY\"#" \
            -e "s#\"update_source\": \"[^\"]*\"#\"update_source\": \"$UPDATE_SOURCE\"#" \
            "$HERE/config.json" > "$tmp"
        chmod 600 "$tmp"
        chown "$RUN_USER:$RUN_USER" "$tmp"
        mv -f "$tmp" "$HOME_DIR/config.json"
        say "√ 已写配置：$HOME_DIR/config.json（gateway_url=$GATEWAY，update_source=$UPDATE_SOURCE）"
    fi
fi

# ── 3) venv + 客户端（幂等：重复装同一个版本是 no-op）
run install -d -m 0755 "$PREFIX"
if [ ! -x "$PREFIX/venv/bin/python" ]; then
    run "$PYTHON" -m venv "$PREFIX/venv"
fi
if [ -n "$WHEEL" ]; then
    [ -f "$WHEEL" ] || { say "install.sh: wheel 不存在：$WHEEL" >&2; exit 2; }
    run "$PREFIX/venv/bin/pip" install --no-index --no-deps --upgrade "$WHEEL"
else
    run "$PREFIX/venv/bin/pip" install --upgrade djb-node
fi

# 首次初始化状态库（幂等；DJB_HOME 指到状态目录，凭据仍为空）
if [ "$DRY_RUN" -eq 1 ]; then
    say "[dry-run] 以 $RUN_USER 身份：DJB_HOME=$HOME_DIR djb init --no-env --edition official-supply"
else
    su -s /bin/sh "$RUN_USER" -c \
        "DJB_HOME='$HOME_DIR' '$PREFIX/venv/bin/djb' init --no-env --edition official-supply" >/dev/null
    say "√ 状态库就绪：$HOME_DIR/state.db"
fi

# ── 4) systemd 单元（内容没变就不动文件、不 daemon-reload）
# 非默认 --home/--prefix 时按实际路径改写单元，避免「装到别处但单元还指默认路径」。
UNIT_SRC="$HERE/djb-official-supply.service"
if [ "$HOME_DIR" != /var/lib/djb-official-supply ] \
   || [ "$PREFIX" != /opt/djb-official-supply ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        say "[dry-run] 按 --home/--prefix 改写单元里的状态目录与程序目录"
        UNIT_SRC=""
    else
        UNIT_SRC=$(mktemp /tmp/djb-official-supply.unit.XXXXXX)
        trap 'rm -f "$UNIT_SRC"' EXIT
        sed -e "s#/var/lib/djb-official-supply#$HOME_DIR#g" \
            -e "s#/opt/djb-official-supply#$PREFIX#g" \
            "$HERE/djb-official-supply.service" > "$UNIT_SRC"
    fi
fi
if [ -n "$UNIT_SRC" ] && [ -f "$UNIT_PATH" ] && cmp -s "$UNIT_SRC" "$UNIT_PATH"; then
    say "√ 单元未变化：$UNIT_PATH"
else
    if [ -n "$UNIT_SRC" ]; then
        run install -m 0644 "$UNIT_SRC" "$UNIT_PATH"
    fi
    run systemctl daemon-reload
    if [ "$DRY_RUN" -eq 0 ]; then
        say "√ 已安装单元：$UNIT_PATH"
    fi
fi

# ── 收尾：只提示，不自动 start / 不重启任何东西
say ""
say "下一步（三条，缺一不可）："
say "  1) 写入节点密钥（交互输入，不进 shell 历史）："
say "     su -s /bin/sh $RUN_USER -c \"DJB_HOME=$HOME_DIR $PREFIX/venv/bin/djb auth --set-key -\""
say "  2) 接一个聚合上游（本预置档已把 kind 固定成 third_party_relay，--kind 可省略；"
say "     换成别的值会被拒绝——预置档是承诺，不是默认值）："
say "     $PREFIX/venv/bin/djb upstream add --access B \\"
say "         --base-url https://agg.example.com/v1 --models gpt-4o,gpt-4o-mini --secret-stdin"
say "  3) 体检 + 干跑 + 开跑："
say "     djb upstream probe --id <id> && djb quotes publish --dry-run"
if [ "$DO_START" -eq 1 ]; then
    run systemctl enable --now djb-official-supply
    say "√ 已启用并启动 djb-official-supply"
else
    say "  然后：systemctl enable --now djb-official-supply（本次未自动启动）"
fi
