# SPDX-License-Identifier: Apache-2.0
# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────────────────────
# DJB 联脑 · C5 机房版节点镜像（repos/djb-node 的 djb_node 包）
#
# 设计约束（与 deploy/machine-room/README.md 第 1 节逐条对应）：
#   * 非 root：UID/GID 10001；只读根文件系统下可运行，可写点只有 /data、/config、/tmp；
#   * 无入站端口：daemon 只绑 127.0.0.1:1717（M15 INV-03），故**不 EXPOSE**、集群里不需要 Service；
#   * 零第三方运行时依赖：pyproject.toml 的 dependencies = []，只用标准库；
#   * 镜像内不含任何凭据 / token / 生产域名：凭据只在运行时经 Secret 注入并落 /data（0600）；
#   * 构建期不访问 PyPI：--no-build-isolation 使用镜像自带的 setuptools/wheel，构建可离线复现。
#
# 构建（context 必须是**仓库根**，否则 pyproject.toml / djb_node 拷不进去）：
#   docker build -f deploy/machine-room/Dockerfile -t djb-node:0.2.0 .
# 忽略清单：deploy/machine-room/.dockerignore（同目录仅此一份，见 README）
# ─────────────────────────────────────────────────────────────────────────────

# 层 1/7 · 基础镜像
# 固定发行版（bookworm）+ 完整补丁版本的官方 slim 镜像；禁止 :latest —— 机房 100 节点必须可复现。
FROM python:3.11-slim-bookworm

# 层 2/7 · Python 与进程行为（纯环境变量，不产生文件）
# PYTHONDONTWRITEBYTECODE：根文件系统只读，禁止运行时写 .pyc；
# PYTHONUNBUFFERED：前台容器，日志必须及时出 stdout（JSON 行日志，已脱敏）；
# PIP_NO_CACHE_DIR：镜像层不留 pip 缓存；DJB_HOME：唯一的状态根（SQLite/日志/凭据都在它下面）；
# HOME 指向 /data/home：个别标准库工具在没有可写 HOME 时会报错，机房版不给它们写镜像层的机会。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJB_HOME=/data \
    DJB_MIRROR_PORT=1717 \
    HOME=/data/home

# 层 3/7 · 运行身份与卷目录
# 固定 UID/GID 10001（与 Helm 的 runAsUser/fsGroup 对齐，便于 PVC 权限）；
# /data = 持久状态（state.db / config.json / credentials 0600 / logs / runtime）；
# /config = 可选的只读种子目录（Helm 把 config.json 的 ConfigMap 挂这里，bootstrap 再复制进 /data）；
# 全部目录 0700，只有 UID 10001 能进。
RUN groupadd --gid 10001 djb \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /data/home \
            --shell /usr/sbin/nologin djb \
 && mkdir -p /data/home /config \
 && chown -R 10001:10001 /data /config \
 && chmod 0700 /data /config

# 层 4/7 · 打包元数据（最小复制，最大化层缓存命中；pyproject 的 readme 指向 README-CLIENT.md）
WORKDIR /src
COPY --chown=10001:10001 pyproject.toml README-CLIENT.md ./

# 层 5/7 · 运行时源码：只拷 djb_node 包；tests/、ci/、deploy/、openapi.yaml 不进镜像
COPY --chown=10001:10001 djb_node ./djb_node

# 层 6/7 · 安装本仓 wheel（dependencies = []，不拉任何第三方包）
# --no-build-isolation：用镜像自带的 setuptools/wheel 构建，构建期不访问 PyPI；
# 装完删源码与 __pycache__：镜像层不留构建输入与字节码（根文件系统只读时也无从生成）。
RUN python -m pip install --no-build-isolation . \
 && rm -rf /src \
 && find /usr/local/lib/python3.11/site-packages -name '__pycache__' -type d -prune -exec rm -rf {} +

# 层 7/7 · 运行形态：非 root + 卷 + 无入站端口 + 健康检查
USER 10001:10001
WORKDIR /data

# 状态与配置放在卷里，绝不在镜像层里写运行数据
VOLUME ["/data", "/config"]

# 刻意不写 EXPOSE：daemon 只绑 127.0.0.1:1717（INV-03），宿主机与集群都无法入站；
# 集群侧也不要建 Service / Ingress（见 helm/djb-node/NOTES.txt）。

# SIGTERM = 优雅停止（daemon.main 注册了 SIGTERM/SIGINT -> stop()）。
STOPSIGNAL SIGTERM

# 健康检查：只打本机回环的免令牌 /v1/health（无外网、无 curl/wget、无额外工具）：
#   daemon 未起 / 卡死 -> 连接失败或超时 -> unhealthy（Docker 标 unhealthy，k8s 用自己的 exec 探针）。
# 端口跟随 DJB_MIRROR_PORT（默认 1717）；改了镜像端口必须同步改这个 ENV。
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request as u; p=os.environ.get('DJB_MIRROR_PORT','1717'); r=u.urlopen('http://127.0.0.1:'+p+'/v1/health', timeout=3); sys.exit(0 if r.status == 200 else 1)"]

# 入口固定为 djb；默认前台跑 daemon（容器 PID 1，SIGTERM 直接进 daemon 的优雅停止）。
# 卷是空的首次启动会以 exit 3「未初始化」退出（INV-01：不留静默失败）——
# 先 `djb init --no-env`，Helm 里由 initContainer 统一完成（见 templates/deployment.yaml）。
ENTRYPOINT ["djb"]
CMD ["start", "--foreground", "--no-token"]
