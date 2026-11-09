#!/usr/bin/env sh
# SPDX-License-Identifier: Apache-2.0
# T1 内层自检（在干净容器/VM 里跑，POSIX sh，不依赖 bash）
#
# 环境变量：
#   PKG_KIND  consumer | machine-room | official-supply
#   PKG_DIR   制品所在目录（容器里是 /pkg）
#   WORK      临时工作目录（默认 /tmp/t1）
# 输出：RESULT T1-<kind>-<case> PASS|FAIL|SKIP <detail>；最后一行 SUMMARY
set -u

KIND=${PKG_KIND:-consumer}
PKG_DIR=${PKG_DIR:-/pkg}
WORK=${WORK:-/tmp/t1}
HOME_DIR="$WORK/home"
FAIL=0; PASS=0; SKIP=0
mkdir -p "$WORK" "$HOME_DIR"

say() { printf '%s\n' "$*"; }
result() { # name status detail
  say "RESULT T1-$KIND-$1 $2 $3"
  case "$2" in
    PASS) PASS=$((PASS+1)) ;;
    FAIL) FAIL=$((FAIL+1)) ;;
    SKIP) SKIP=$((SKIP+1)) ;;
  esac
}

# ── 机器信息（证据用）
say "== machine =="
( uname -a; cat /etc/os-release 2>/dev/null | head -3; id ) 2>&1 | sed 's/^/  /'

# ── 依赖探测：python3 >= 3.10（缺就尝试装；装不上 => SKIP 而不是 FAIL）
PY=""
pick_python() {
  for cand in python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys;raise SystemExit(0 if sys.version_info[:2] >= (3,10) else 1)' 2>/dev/null; then
        PY=$cand; return 0
      fi
    fi
  done
  return 1
}
pick_python
if [ -z "$PY" ]; then
  say "== python3>=3.10 缺失，尝试本地包管理器安装 =="
  if command -v apt-get >/dev/null 2>&1; then
    (apt-get update -qq && apt-get install -y -qq python3 >/dev/null 2>&1) || true
  elif command -v dnf >/dev/null 2>&1; then
    dnf -q install -y python3.11 >/dev/null 2>&1 || dnf -q install -y python3 >/dev/null 2>&1 || true
  elif command -v microdnf >/dev/null 2>&1; then
    microdnf -y install python3.11 >/dev/null 2>&1 || microdnf -y install python3 >/dev/null 2>&1 || true
  fi
  pick_python || true
fi
SYS_PY=$(command -v python3 >/dev/null 2>&1 && python3 -c 'import sys;print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo none)
if [ -z "$PY" ]; then
  result python3 SKIP "无 python3>=3.10（系统 python3=$SYS_PY；Rocky 9 需 dnf install python3.11）"
else
  PYV=$("$PY" -c 'import sys;print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "?")
  result python3 PASS "$PY $PYV（系统 python3=$SYS_PY）"
fi

# ── 三包各自的检查
case "$KIND" in
consumer)
  SRC=$(ls -1 "$PKG_DIR"/djb-node-*-linux-x64-src.tar.gz | sort -V | tail -1); tar -xzf "$SRC" -C "$WORK" || { result unpack FAIL "解包失败"; }
  D=$(find "$WORK" -maxdepth 1 -type d -name 'djb-node-*-linux-x64-src' | head -1)
  if [ -z "$D" ]; then result unpack FAIL "找不到解包目录"; else result unpack PASS "$(basename "$D")"; fi
  if [ -n "$PY" ] && [ -n "$D" ]; then
    V=$(DJB_HOME="$HOME_DIR" DJB_BOOTSTRAP_DISABLE=1 DJB_DEFAULT_GATEWAY=http://127.0.0.1:18100 "$D/djb" --version 2>&1)
    case "$V" in
      *"djb "*) result version PASS "$V" ;;
      *)        result version FAIL "输出异常：$V" ;;
    esac
    # 非 root 跑一次（容器/VM 里通常有 nobody）
    if id nobody >/dev/null 2>&1; then
      su -s /bin/sh nobody -c "DJB_HOME=$WORK/home-nobody $D/djb --version" >/dev/null 2>&1 \
        && result nonroot PASS "nobody 可运行" || result nonroot FAIL "nobody 跑不起来"
    else
      result nonroot SKIP "无 nobody 用户"
    fi
    # 残留扫描：生产域名 / 内网地址 / 工作区绝对路径
    # 只扫「不该出现在公开包里」的东西：内网地址 / 工作区绝对路径 / 已废弃下载域名。
    # tkbank.ai 是正式生产域名（facts.WEB_CONSOLE_URL 默认值），不算残留。
    leaves=$(grep -rIl '192\.168\.\|/vol[12]/@appdata\|dl\.aitokenmall\.ai' "$D" 2>/dev/null | head -3)
    if [ -n "$leaves" ]; then
      result residue FAIL "$(printf '%s' "$leaves" | tr '\n' ' ')"
    else
      result residue PASS "无内网地址/工作区路径/废弃 dl 域名"
    fi
  fi
  ;;
machine-room)
  MR=$(ls -1 "$PKG_DIR"/djb-node-*-machine-room-offline.tar.gz | sort -V | tail -1); tar -xzf "$MR" -C "$WORK" || { result unpack FAIL "解包失败"; }
  D=$(find "$WORK" -maxdepth 1 -type d -name 'djb-node-*-machine-room-offline' | head -1)
  if [ -z "$D" ]; then result unpack FAIL "找不到解包目录"; else result unpack PASS "$(basename "$D")"; fi
  if [ -n "$D" ]; then
    ( cd "$D" && ./verify.sh >"$WORK/verify.log" 2>&1 ) \
      && result verify PASS "verify.sh rc=0" || result verify FAIL "见 verify.log"
    WHEEL=$(ls -1 "$D"/djb_node-*.whl 2>/dev/null | head -1)
    cp "$WHEEL" "$WORK/whl.bak" 2>/dev/null
    # 注意：dash 不会对重定向目标做 glob 展开，必须先显式取到文件
    printf x >> "$WHEEL" 2>/dev/null
    ( cd "$D" && ./verify.sh >/dev/null 2>&1 ) \
      && result tamper FAIL "篡改后仍通过" || result tamper PASS "篡改被拒"
    cp "$WORK/whl.bak" "$WHEEL" 2>/dev/null || true
    # 3.9 必须被拒（有 python3.9 才测）
    if command -v python3.9 >/dev/null 2>&1; then
      ( cd "$D" && ./install.sh --python "$(command -v python3.9)" --dry-run >/dev/null 2>&1 ) \
        && result py39_guard FAIL "3.9 竟然通过" || result py39_guard PASS "3.9 被拒"
    else
      result py39_guard SKIP "无 python3.9"
    fi
    if [ -n "$PY" ]; then
      ( cd "$D" && PYTHON="$PY" ./install.sh --dry-run >"$WORK/mr-dryrun.log" 2>&1 ) \
        && result install_dryrun PASS "verify+打印 pip 命令（PYTHON=$PY）" \
        || result install_dryrun FAIL "$(tail -2 "$WORK/mr-dryrun.log" 2>/dev/null | tr '\n' ' ')"
    fi
  fi
  ;;
official-supply)
  OS=$(ls -1 "$PKG_DIR"/djb-node-*-official-supply-x64.tar.gz | sort -V | tail -1); tar -xzf "$OS" -C "$WORK" || { result unpack FAIL "解包失败"; }
  D=$(find "$WORK" -maxdepth 1 -type d -name 'djb-node-*-official-supply-x64' | head -1)
  if [ -z "$D" ]; then result unpack FAIL "找不到解包目录"; else result unpack PASS "$(basename "$D")"; fi
  if [ -n "$D" ]; then
    for f in install.sh config.json djb-official-supply.service README.md; do
      [ -f "$D/profiles/official-supply/$f" ] || result "prep_$f" FAIL "缺 profiles/official-supply/$f"
    done
    result prep_profile PASS "四件预置档齐全"
    PYTHON="${PY:-python3}" sh "$D/profiles/official-supply/install.sh" \
      --gateway https://gateway.example.invalid --dry-run >"$WORK/supply-dryrun.log" 2>&1 \
      && result dryrun PASS "install.sh --dry-run rc=0（PYTHON=${PY:-python3}）" \
      || result dryrun FAIL "$(tail -2 "$WORK/supply-dryrun.log" 2>/dev/null | tr '\n' ' ')"
    if [ -n "$PY" ]; then
      V=$(DJB_HOME="$HOME_DIR" DJB_BOOTSTRAP_DISABLE=1 DJB_DEFAULT_GATEWAY=http://127.0.0.1:18100 "$D/djb" --version 2>&1)
      case "$V" in
        *"djb "*) result version PASS "$V" ;;
        *)        result version FAIL "输出异常：$V" ;;
      esac
    fi
    # unit 文件静态检查：非 root + 只写自己状态目录 + 只回环
    grep -q 'User=djb-supply' "$D/profiles/official-supply/djb-official-supply.service" \
      && result unit_nonroot PASS "User=djb-supply" || result unit_nonroot FAIL "unit 未指定非 root"
    grep -q 'ReadWritePaths=/var/lib/djb-official-supply' "$D/profiles/official-supply/djb-official-supply.service" \
      && result unit_writepath PASS "" || result unit_writepath FAIL "缺 ReadWritePaths"
  fi
  ;;
*)
  result kind FAIL "未知 PKG_KIND=$KIND"
  ;;
esac

say "RESULT T1-$KIND-SUMMARY $([ "$FAIL" -eq 0 ] && echo PASS || echo FAIL) pass=$PASS fail=$FAIL skip=$SKIP"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
