#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# T2 核心链路（RC8.3-C）：--version / env / init / auth --bind / start / status /
# 127.0.0.1:17171 /v1/health /v1/models / quotes show（publish 需 --quotes）。
#
# 用法：
#   bash tests/rc8/t2-core-chain.sh --pkg /path/djb-node-0.3.3-linux-x64-src.tar.gz [--port 17171] [--quotes]
#
# 纪律（写死，别改）：
#   * 只跑本机假网关（tests/rc8/t2_gateway.py，默认 127.0.0.1:18190）；
#   * 端口默认 17171 —— **不占 1717**（生产节点在本机 1717 上跑着）；
#   * DJB_HOME 一次性临时目录，trap 里 stop --force + 杀网关 + 删目录；
#   * 不设任何真实凭据；不访问 tkbank.ai / 192.168.51.83。
set -u

WS=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)          # 仓库根
PKG=""
PORT=17171
GW_PORT=18190
DO_QUOTES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --pkg) PKG="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --gateway-port) GW_PORT="$2"; shift 2 ;;
    --quotes) DO_QUOTES=1; shift ;;
    -h|--help) sed -n "2,20p" "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done
if [ -z "$PKG" ]; then
  PKG=$(ls -1 /vol2/@appdata/deepseek.harness/home/DJB联脑/ops/rc/artifacts/downloads/djb-node-*-linux-x64-src.tar.gz 2>/dev/null | tail -1)
fi
[ -f "$PKG" ] || { echo "缺少 --pkg（源码包 tar.gz）" >&2; exit 2; }

W=$(mktemp -d /tmp/t2-chain-XXXXXX)
GW_PID=""
cleanup() {
  [ -n "$GW_PID" ] && kill "$GW_PID" 2>/dev/null
  DJB_HOME="$W/home" "$W/pkg/djb" stop --force >/dev/null 2>&1
  pkill -f "djb_node.daemon --home $W/home" 2>/dev/null
  [ "${KEEP:-0}" = "1" ] && { echo "kept: $W"; return 0; }
  rm -rf "$W"   # token.json 随目录一起删；证据里只留 status.json（无 token）
}
trap cleanup EXIT INT TERM

export DJB_BOOTSTRAP_DISABLE=1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8
mkdir -p "$W/pkg" "$W/home"
tar -xzf "$PKG" -C "$W/pkg" --strip-components=1 || { echo "解包失败：$PKG" >&2; exit 2; }

PASS=0; FAIL=0
result() { # $1=name $2=0/1 $3=detail
  if [ "$2" = "0" ]; then echo "RESULT T2-$1 PASS $3"; PASS=$((PASS+1));
  else echo "RESULT T2-$1 FAIL $3"; FAIL=$((FAIL+1)); fi
}

port_free() { python3 - "$1" <<'PY'
import socket, sys
sock = socket.socket()
try:
    sock.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    raise SystemExit(1)
finally:
    sock.close()
PY
}
# 端口被占（上次跑残留）就顺延，避免假红
for _ in 1 2 3 4 5; do port_free "$GW_PORT" && break; GW_PORT=$((GW_PORT + 1)); done
for _ in 1 2 3 4 5; do port_free "$PORT" && break; PORT=$((PORT + 1)); done
echo "== 0) 假网关 127.0.0.1:$GW_PORT / 客户端 $PORT =="
python3 "$WS/tests/rc8/t2_gateway.py" "$W/gateway.log" --port "$GW_PORT" &
GW_PID=$!
sleep 1.2
curl -sS -o /dev/null --max-time 5 "http://127.0.0.1:$GW_PORT/api/v1/protocol/capabilities" \
  && result gateway-up 0 "port=$GW_PORT" || { result gateway-up 1 "假网关起不来"; exit 1; }

cd "$W/pkg" || exit 2
export DJB_HOME="$W/home"

echo "== 1) --version =="
V=$(./djb --version 2>&1); rc=$?
echo "$V" | grep -qE "djb [0-9]+\.[0-9]+\.[0-9]+" && result version 0 "out=$V" || result version 1 "rc=$rc out=$V"

echo "== 2) init（临时 HOME + $PORT）=="
./djb init --gateway "http://127.0.0.1:$GW_PORT" --port "$PORT" --no-env \
  --preset 全力以赴 --mem 95 --cpu 95 >"$W/init.log" 2>&1
rc=$?
[ "$rc" = "0" ] && result init 0 "rc=0 port=$PORT" || result init 1 "rc=$rc（见 init.log）"
grep -q "\"mirror_port\": $PORT" "$W/home/config.json" 2>/dev/null \
  && result init-port 0 "mirror_port=$PORT" || result init-port 1 "config.json 端口不符"

echo "== 3) env =="
./djb env >"$W/env.log" 2>&1
OK_LINES=$(grep -cE "^\[OK\]" "$W/env.log" || true)
if grep -q "Traceback" "$W/env.log"; then result env 1 "有 Traceback"; 
elif [ "$OK_LINES" -ge 3 ]; then result env 0 "ok_lines=$OK_LINES"; 
else result env 1 "ok_lines=$OK_LINES（见 env.log）"; fi

echo "== 4) start（未绑定：rc=4 属预期）=="
./djb start --timeout 30 >"$W/start.log" 2>&1
START_RC=$?
echo "start_rc=$START_RC"
grep -q "api/v1/node/register" "$W/gateway.log" && result register 0 "POST /api/v1/node/register 已发出" \
  || result register 1 "假网关没收到 register"
grep -q "api/v1/node/heartbeat" "$W/gateway.log" && result heartbeat 0 "POST heartbeat 已发出" \
  || result heartbeat 1 "假网关没收到 heartbeat"

echo "== 5) auth --bind + reload =="
./djb auth --bind TESTCODE >"$W/bind.log" 2>&1
rc=$?
[ "$rc" = "0" ] && result bind 0 "rc=0" || result bind 1 "rc=$rc（见 bind.log）"
./djb daemon reload >"$W/reload.log" 2>&1
sleep 3

echo "== 6) status =="
./djb status --json >"$W/status.json" 2>&1
STATE=$(python3 -c "import json,sys;d=json.load(open('$W/status.json'));print(d.get('state'))" 2>/dev/null || echo "?")
ACCEPT=$(python3 -c "import json,sys;d=json.load(open('$W/status.json'));print(str(d.get('accepting')).lower())" 2>/dev/null || echo "?")
[ "$STATE" = "online" ] && result status-online 0 "state=$STATE" || result status-online 1 "state=$STATE（见 status.json）"
[ "$ACCEPT" = "true" ] && result accepting 0 "accepting=true" || result accepting 1 "accepting=$ACCEPT"
python3 - "$W/status.json" <<'PY' && result policy 0 "verify_state=digest_only" || result policy 1 "策略没落库"
import json, sys
d = json.load(open(sys.argv[1]))
policy = d.get("policy") or {}
assert policy.get("present") is True, policy
assert policy.get("verify_state") == "digest_only", policy
PY

echo "== 7) 回环端点 =="
H=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/v1/health")
[ "$H" = "200" ] && result health 0 "health=200" || result health 1 "health=$H"
# 一次性 local_token：单独文件（--show-token 才输出），只在本进程用，不打印、不落证据
./djb status --show-token --json >"$W/token.json" 2>/dev/null
TOKEN=$(python3 - "$W/token.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
for path in (("local_token",), ("daemon", "local_token"), ("bind", "local_token")):
    cur, ok = d, True
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            ok = False
            break
    if ok and isinstance(cur, str) and cur:
        print(cur)
        break
PY
)
[ -n "$TOKEN" ] && result token 0 "local_token 已取到（不打印）" || result token 1 "取不到 local_token"
NO=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/v1/models")
[ "$NO" = "401" ] && result models-no-token 0 "401" || result models-no-token 1 "got=$NO"
YES=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 -H "Authorization: Bearer $TOKEN" \
      "http://127.0.0.1:$PORT/v1/models")
[ "$YES" = "200" ] && result models-token 0 "200" || result models-token 1 "got=$YES"
curl -sSS -H "Authorization: Bearer $TOKEN" --max-time 5 "http://127.0.0.1:$PORT/v1/models" \
  | grep -q '"object": *"list"' && result models-shape 0 "{\"object\":\"list\"}" \
  || result models-shape 1 "不是 list 形状"

echo "== 8) 报价（本地预演）=="
./djb quotes show >"$W/quotes-show.log" 2>&1
[ $? = "0" ] && result quotes-show 0 "rc=0" || result quotes-show 1 "见 quotes-show.log"
if [ "$DO_QUOTES" = "1" ]; then
  ./djb announce --dry-run >"$W/announce.log" 2>&1
  [ $? = "0" ] && result announce-dryrun 0 "rc=0" || result announce-dryrun 1 "见 announce.log"
fi

echo "== 9) stop =="
./djb stop --timeout 12 >"$W/stop.log" 2>&1
sleep 1
AFTER=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$PORT/v1/health" || true)
[ "$AFTER" = "000" ] && result stop 0 "端口不可连" || result stop 1 "after_stop=$AFTER"

echo "RESULT T2-SUMMARY $([ "$FAIL" = "0" ] && echo PASS || echo FAIL) pass=$PASS fail=$FAIL"
[ "$FAIL" = "0" ] && exit 0 || exit 1
