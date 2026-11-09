#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# T1 四发行版 × 三制品矩阵（RC8.3-C）。默认跑 L0（容器）；L1（KVM 干净机）用 --lane l1。
#
# 用法：
#   bash tests/rc8/t1_matrix.sh                       # L0 全矩阵（4 发行版 × 3 包 = 12 格）
#   bash tests/rc8/t1_matrix.sh --distros "debian:12 ubuntu:24.04" --kinds consumer
#   bash tests/rc8/t1_matrix.sh --lane l1 --images /var/lib/libvirt/images
#   bash tests/rc8/t1_matrix.sh --evidence /tmp/t1-evidence
#
# 判据：每格输出 RESULT T1-<distro>-<kind>-<case>；末尾 SUMMARY。
# 纪律：只读挂载制品目录；容器 --network none 时 python3 缺失记 SKIP（不算 FAIL）；
#       不写生产、不注册节点、不占 1717。
set -u

WS=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
PKG_DIR=/vol2/@appdata/deepseek.harness/home/DJB联脑/ops/rc/artifacts/downloads
DISTROS="ubuntu:22.04 ubuntu:24.04 debian:12 rockylinux:9"
KINDS="consumer machine-room official-supply"
LANE=l0
IMAGES=""
EVIDENCE=""
NET_MODE=bridge

while [ $# -gt 0 ]; do
  case "$1" in
    --pkg-dir) PKG_DIR="$2"; shift 2 ;;
    --distros) DISTROS="$2"; shift 2 ;;
    --kinds) KINDS="$2"; shift 2 ;;
    --lane) LANE="$2"; shift 2 ;;
    --images) IMAGES="$2"; shift 2 ;;
    --evidence) EVIDENCE="$2"; shift 2 ;;
    --network-none) NET_MODE=none; shift ;;
    -h|--help) sed -n "2,16p" "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

[ -d "$PKG_DIR" ] || { echo "制品目录不存在：$PKG_DIR" >&2; exit 2; }
for pattern in 'djb-node-*-linux-x64-src.tar.gz' 'djb-node-*-machine-room-offline.tar.gz' 'djb-node-*-official-supply-x64.tar.gz'; do
  # shellcheck disable=SC2086
  ls $PKG_DIR/$pattern >/dev/null 2>&1 || { echo "缺制品：$pattern（$PKG_DIR）" >&2; exit 2; }
done
[ -n "$EVIDENCE" ] || EVIDENCE=$(mktemp -d /tmp/t1-evidence-XXXXXX)
mkdir -p "$EVIDENCE"

TOTAL_FAIL=0; TOTAL_PASS=0
run_l0() { # $1=image $2=kind
  local image=$1 kind=$2 tag distro_log
  tag=$(printf '%s' "$image" | tr ':/' '--')
  distro_log="$EVIDENCE/$tag-$kind.log"
  echo "---- L0 $image / $kind"
  docker run --rm -v "$PKG_DIR":/pkg:ro -v "$WS/tests/rc8":/t1:ro \
    -e PKG_KIND="$kind" -e PKG_DIR=/pkg -e WORK=/tmp/t1 \
    --network "$NET_MODE" "$image" sh /t1/t1_inner.sh 2>&1 | tee "$distro_log"
  local rc=${PIPESTATUS[0]}
  if grep -q "RESULT T1-$kind-SUMMARY PASS" "$distro_log"; then
    TOTAL_PASS=$((TOTAL_PASS+1))
  else
    echo "RESULT T1-$tag-$kind FAIL rc=$rc（见 $distro_log）"
    TOTAL_FAIL=$((TOTAL_FAIL+1))
  fi
}

run_l1() { # $1=image-file  目前只做骨架：真跑需 qemu + cloud-init
  echo "RESULT T1-l1 SKIP 'L1 骨架（需 --images 提供 qcow2）；L0 已覆盖包级判据'"
}

case "$LANE" in
  l0)
    for image in $DISTROS; do
      for kind in $KINDS; do run_l0 "$image" "$kind"; done
    done
    ;;
  l1)
    if ! command -v qemu-system-x86_64 >/dev/null 2>&1; then
      echo "RESULT T1-l1 SKIP '没有 qemu-system-x86_64'" >&2
    elif [ -z "$IMAGES" ]; then
      echo "RESULT T1-l1 SKIP '需 --images <dir> 放 Ubuntu/Debian/Rocky 云镜像'" >&2
    else
      # 干净机内层同样是 t1_inner.sh；镜像/seed/ssh 步骤见
      # reports/AUDIT-20261110/LINUX-CLIENT-SIGNING.md §3.1.2（本脚本只留 hook，避免误跑重活）
      for image in $IMAGES/*.qcow2; do
        [ -f "$image" ] || continue
        echo "RESULT T1-l1-hook SKIP 'image=$(basename "$image")：按报告 §3.1.2 串行手工/CI 触发'"
      done
    fi
    ;;
  *) echo "未知 lane：$LANE" >&2; exit 2 ;;
esac

echo "RESULT T1-MATRIX-SUMMARY $([ "$TOTAL_FAIL" -eq 0 ] && echo PASS || echo FAIL) cells_pass=$TOTAL_PASS cells_fail=$TOTAL_FAIL evidence=$EVIDENCE"
[ "$TOTAL_FAIL" -eq 0 ] && exit 0 || exit 1
