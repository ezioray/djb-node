# C5 机房版 · 节点客户端部署（djb-node）

面向**没有 GUI 的机房节点**：容器 / Helm / 离线包三种装法，100 节点批量配置。
本文只描述本目录（`deploy/machine-room/`）的制品；客户端行为本身见仓库根 `README-CLIENT.md`。

| 项 | 值 |
| --- | --- |
| 上游基线 | `djb-node` main `f54b8f6` → 分支 `feat/DEV-C5` |
| 客户端版本 | `0.2.0`（`pyproject.toml` → Chart `appVersion`） |
| 制品 | `Dockerfile`、`helm/djb-node/`、`offline/`、`fleet/` |
| 状态目录 | `/data`（`DJB_HOME`；SQLite / 日志 / 凭据 0600），种子配置 `/config`（只读挂载） |
| 入站端口 | **无**（daemon 只绑 `127.0.0.1:1717/v1`，M15 INV-03） |
| 运行身份 | UID/GID `10001`，只读根文件系统 |

---

## 0. 安全基线（三条不变量，测试逐条断言）

1. **非 root + 只读根**：UID/GID 10001；可写点只有 `/data`、`/config`、`/tmp`；
   `allowPrivilegeEscalation=false`、`privileged=false`、`capabilities.drop=[ALL]`。
2. **无入站**：不 `EXPOSE`、不建 Service / Ingress、`hostNetwork=false`；
   探针用容器内 `127.0.0.1` 的 exec 探针（kubelet 的 `httpGet` 打 Pod IP，绑回环必然失败）。
3. **无凭据进制品**：镜像 / Chart / 离线包 / inventory CSV 里没有任何 token、密钥或生产域名；
   凭据只经「已存在的 Secret」或交互式 `djb auth --set-key -` 注入，落在 `/data` 的 0600 文件里。

网关默认值 `https://api.example.invalid` 是不可解析的占位域（RFC 6761），**必须**由运维替换。

---

## 1. 方式一：Docker / Podman（单机最快）

```sh
# 构建（context 必须是仓库根：要拷 pyproject.toml / README-CLIENT.md / djb_node）
docker build -f deploy/machine-room/Dockerfile -t djb-node:0.2.0 .

# 首次初始化（幂等）——卷必须归 UID 10001 所有
install -d -o 10001 -g 10001 /srv/djb/data /srv/djb/config
docker run --rm --read-only --tmpfs /tmp:rw,size=64m --user 10001:10001 \
  -v /srv/djb/data:/data -v /srv/djb/config:/config \
  djb-node:0.2.0 init --no-env --preset 省心 --daily-tokens 500000 --concurrent 3

# 前台运行（容器 PID 1；SIGTERM 走 daemon 的优雅停止）
docker run -d --name djb-node --read-only --tmpfs /tmp:rw,size=64m --user 10001:10001 \
  -v /srv/djb/data:/data -v /srv/djb/config:/config -e DJB_MIRROR_PORT=1717 \
  djb-node:0.2.0

# 验收
docker exec djb-node djb --version
docker exec djb-node djb status --json
docker exec djb-node python -c "import json,urllib.request;print(json.load(urllib.request.urlopen('http://127.0.0.1:1717/v1/health'))['status'])"
docker inspect --format '{{.State.Health.Status}}' djb-node        # healthy
```

说明：

* **不要** `-p 1717:1717`：端口只在容器回环里，映射出去既没用也多了一个暴露面。
* `--read-only` 是推荐姿势（已验证）：`/tmp` 用 tmpfs，`/data`、`/config` 用卷。
* 镜像健康检查用镜像内的 `python` 打 `127.0.0.1:$DJB_MIRROR_PORT/v1/health`（免令牌、无外网、
  不装 curl/wget）；**改了 `mirrorPort` 必须同步改 `DJB_MIRROR_PORT`**，否则健康检查会误报。
* `.dockerignore` 只在**构建上下文根**生效。本仓库根没有放这个文件（多仓库共用根目录），
  所以用上面的命令构建时上下文会包含整个仓库；`COPY` 只拷三处，镜像里不会有别的东西。
  想严格限制上下文体积，用 `docker build -f deploy/machine-room/Dockerfile -t djb-node:0.2.0 <只含三处的目录>`。

---

## 2. 方式二：Helm（机房 / K8s 集群）

```sh
helm -n djb upgrade --install djb-node deploy/machine-room/helm/djb-node \
  --set image.repository=registry.internal.example/djb-node \
  --set image.tag=0.2.0 \
  --set node.gatewayUrl=https://gateway.example.invalid     # ← 换成真实网关
```

关键 values（完整默认值见 `helm/djb-node/values.yaml`，默认值即最安全值）：

| key | 默认 | 说明 |
| --- | --- | --- |
| `mode` | `deployment` | `daemonset` 时渲染 `templates/daemonset.yaml`（每机一实例） |
| `replicaCount` | `1` | 100 节点机房按需放大，配合 `affinity`/`nodeSelector` |
| `hostNetwork` | `false` | 硬值，模板里不接受 true |
| `securityContext.readOnlyRootFilesystem` | `true` | 只读根；`/tmp` 用 emptyDir |
| `securityContext.capabilities.drop` | `["ALL"]` | 无任何 capability |
| `resources` | req 100m/128Mi · lim 500m/512Mi | requests + limits 必填 |
| `node.mirrorPort` | `1717` | 同时驱动 `DJB_MIRROR_PORT` 与探针 |
| `node.featureFlags` | `{}`（全关） | 开启必须带 `owner`/`expire_at`/`rollback_rule`，否则模板 `fail` |
| `auth.existingSecret` | `""` | 只引用已存在的 Secret，Chart 不创建、不渲染密钥 |
| `persistence.enabled` | `false` | 默认 emptyDir；要保 `state.db` 就开 PVC 或给 `existingClaim` |

凭据（**不要**写进 values / git）：

```sh
# 方式 A（推荐）：Secret + existingSecret，bootstrap initContainer 首次启动时导入
kubectl -n djb create secret generic djb-node-client --from-literal=client-key="$DJB_CLIENT_KEY"
helm -n djb upgrade djb-node deploy/machine-room/helm/djb-node --reuse-values \
  --set auth.existingSecret=djb-node-client

# 方式 B：交互补（不回显）
kubectl -n djb exec -it deploy/djb-node -- djb auth --set-key -
```

bootstrap initContainer 做了三件事（都幂等）：把 `/config/config.json` 复制到 `/data/config.json`（只补空缺）、
`djb init --no-env`、按需从只读 Secret 导入 client key。

---

## 3. 方式三：离线包（无外网 / 气隙机房）

```sh
# 在能联网的构建机打一次包（构建期可以联网取构建后端；产物本身不含外网动作）
cd deploy/machine-room/offline
./build.sh                                  # 产物在 ./dist/
# 需要签名时（私钥永不进仓库）：
DJB_OFFLINE_SIGNING_KEY=/secure/djb-offline.pem ./build.sh

# 拷贝 dist/ 到目标机，然后：
./dist/verify.sh                            # 哈希校验；有签名则配 DJB_OFFLINE_VERIFY_KEY=公钥.pem
sudo ./dist/install.sh                      # 只从本目录装：pip install --no-index --find-links
djb init --no-env && djb auth --set-key - && djb start
```

* 安装**只**用 `pip install --no-index --find-links <包目录> --no-deps`（外加 `PIP_NO_INDEX=1` 双保险），
  不访问任何索引；wheel 的 `dependencies = []`，不存在运行时依赖解析。
* `verify.sh` 三层校验：签名（可选）→ `sha256sum -c` → `manifest.json` 与磁盘交叉核对；
  **篡改 wheel / install.sh / verify.sh / manifest.json / sha256sums 任一个都会非 0 退出**。
* 签名只在设置了 `DJB_OFFLINE_SIGNING_KEY` 时生成（`openssl dgst -sha256 -sign`）。
  哈希只能防损坏/误改；要防「有权限的恶意替换」必须有签名 + 独立保管的公钥。
  包内有签名但没给公钥时默认只告警，`DJB_OFFLINE_STRICT=1` 可直接拒绝。
* 构建机的工具链要求：能产出 wheel（`python -m build` 或 `pip wheel`）。本机 venv 只有
  setuptools 66 + 无 `wheel` 包时，`./build.sh` 会自动退化到「构建隔离」路线（需联网）；
  完全离线的构建机请先装好 `wheel`，或用 `./build.sh --wheel <已有.whl>` 复用现成 wheel。

---

## 4. 100 节点批量配置（`fleet/`）

```sh
python3 deploy/machine-room/fleet/djb-fleet.py \
  --inventory deploy/machine-room/fleet/inventory.example.csv \
  --template  deploy/machine-room/fleet/config.json.template \
  --out       /srv/djb-fleet/out

# 幂等演练：第二次必须 0 差异
python3 deploy/machine-room/fleet/djb-fleet.py --inventory ... --template ... --out ... --check
# 只看差异不落盘
python3 deploy/machine-room/fleet/djb-fleet.py ... --dry-run
```

* 每个节点产出 `out/<node_id>/config.json`（本机非敏感配置）与 `out/<node_id>/init.args`
  （`djb init` 的参数行：预设 + 限额）；`out/manifest.json` 记录节点清单与逐文件 sha256。
* inventory 列：必需 `node_id,gateway,preset,port`；可选 `update_channel,log_level,yield_enabled,
  labels,daily_tokens,weekly_tokens,monthly_tokens,concurrent,cpu,gpu,mem`。
* 退出码：`0` 成功 / `1` 有差异（`--check`/`--dry-run`）/ `2` 输入错误。
* 硬校验：`node_id` 重复、非法端口（`1024–65535`）、未知预设、`gateway` 带 userinfo、
  以及**任何凭据列或凭据形态的值**（`sk-` / `ghp_` / `AKIA` / `Bearer` / JWT）一律 `exit 2`，
  并且**出错时一个字都不写**。
* 节点侧的落地顺序：`verify.sh` → `install.sh` → `djb init $(cat init.args)` →
  `djb auth --set-key -` → `djb start`。凭据永远是每台机器各自注入，批量配置里不出现。

---

## 5. 升级 / 回滚

| 形态 | 升级 | 回滚 |
| --- | --- | --- |
| Docker | 构建/拉取新 tag（固定补丁版本，禁止 `latest`），停旧容器起新容器，卷不动 | 起回上一个 tag 的容器；`state.db` 的 `PRAGMA user_version` 只前进，客户端拒绝降级 → 回滚镜像版本时必须同步复位 `/data` |
| Helm | `helm upgrade <rel> <chart> --version <new> -f values`（`maxUnavailable=0` 滚动，不丢在途） | `helm rollback <rel>`；`helm history <rel>` 看修订 |
| 离线包 | 用新包 `verify.sh` → `install.sh`（同版本覆盖安装是幂等的） | 装回旧版本包；同样注意 `state.db` 版本只前进 |

客户端自身的更新通道（`djb update`）是另一条线：`client.update.channel`（stable/beta）+
分桶灰度，见 `README-CLIENT.md` §8；机房版建议**显式关掉**自动更新（`update_channel` 固定、
由镜像/Chart 版本统一管理），避免 100 台机器各自漂移。

## 6. 卸载

```sh
# Docker
docker rm -f djb-node && docker rmi djb-node:0.2.0
sudo rm -rf /srv/djb/data /srv/djb/config        # 状态/凭据；先确认不再需要

# Helm（PVC 带 helm.sh/resource-policy: keep，不会被自动删）
helm -n djb uninstall djb-node
kubectl -n djb get pvc                            # 人工确认后删
kubectl -n djb delete secret djb-node-client      # 本 Chart 从不创建 Secret，得自己清

# 离线包安装的节点
djb uninstall --yes --dry-run                     # 先看会删什么（含服务/配置/凭据/缓存）
djb uninstall --yes                               # 正式卸载并自检残留（0 命中）
```

客户端自带的 `djb uninstall` 会做残留自检（服务 / 配置 / 凭据 / 数据 / 缓存 / 日志），
容器与 Helm 这两种形态下它只看容器内路径，宿主机侧清理仍需人工。

---

## 7. 验收命令（复制即可跑）

```sh
# 1) 静态与脚本测试（本机无 helm/kubectl 也能跑）
cd worktrees/C5-machine-room
PYTHONPATH=. <venv>/bin/python -m pytest tests/test_machine_room.py -o addopts="" -q

# 2) 镜像
docker build -f deploy/machine-room/Dockerfile -t djb-node:0.2.0 .
docker image inspect djb-node:0.2.0 --format '{{.Config.User}} {{json .Config.Entrypoint}} {{json .Config.Cmd}}'
docker run --rm --read-only --tmpfs /tmp:rw,size=64m --user 10001:10001 \
  --entrypoint id djb-node:0.2.0                     # uid=10001(djb) gid=10001(djb)

# 3) Chart（有 helm 时）
helm lint deploy/machine-room/helm/djb-node
helm template t deploy/machine-room/helm/djb-node | grep -E 'runAsNonRoot|readOnlyRootFilesystem|kind:'
helm template t deploy/machine-room/helm/djb-node --set node.featureFlags.foo.enabled=true  # 应 fail（缺三字段）

# 4) 离线包
./deploy/machine-room/offline/build.sh --out /tmp/pkg && /tmp/pkg/verify.sh
printf 'x' >> /tmp/pkg/manifest.json && /tmp/pkg/verify.sh; echo "tamper exit=$?"   # 必须非 0

# 5) fleet 幂等
F=deploy/machine-room/fleet/djb-fleet.py
python3 $F --inventory deploy/machine-room/fleet/inventory.example.csv \
  --template deploy/machine-room/fleet/config.json.template --out /tmp/fleet
python3 $F --inventory deploy/machine-room/fleet/inventory.example.csv \
  --template deploy/machine-room/fleet/config.json.template --out /tmp/fleet --check   # 差异=0
```

---

## 8. 已知限制 / 待办（如实记录，不粉饰）

1. **容器日志里有一次性令牌（已缓解）**：`djb start --foreground --no-token` 不再打印进程级 `local_token`；机房制品默认已带 `--no-token`
   （客户端运行时代码，本卡不改）。该令牌只在内存、退出即失效、不落盘，但仍属敏感流：
   机房版请把容器 stdout 当敏感日志（限制采集范围与保留期），或覆盖 CMD 为
   `["python","-m","djb_node.daemon"]`（daemon 直接启动，不打印令牌，令牌用 `DJB_LOCAL_TOKEN` 注入）。
2. **本机没有 helm / kubectl**：Chart 只做了 `pyyaml` 解析 + 文本级安全断言 + Go 模板括号/块配平检查，
   没跑过 `helm lint` / `helm template`。合并前建议在有机群的环境补跑一次（命令见 §7.3）。
3. **本机 venv 缺 `wheel`（setuptools 66）**：`build.sh` 的三条路线里前两条（纯本地）在本机不可用，
   实跑走的是「构建隔离」路线。完全离线的构建机请预装 `wheel`，或用 `--wheel` 复用现成 wheel。
4. **daemonset 模式的 hostPath 默认关闭**：打开会写宿主机目录，需要运维确认预建目录与属主（10001）。
5. **Chart 不管理 M02 侧的注册/绑定**：节点仍需要 `djb auth --bind <一次性码>` 或
   `djb auth --set-key` 才能接单（状态 `paused / unbound` 是预期）。
6. **发布纪律**：本目录只在 `feat/DEV-C5` 上开发，不 push、不合并、不发布；
   镜像 tag / Chart version 的递增与发布由 release-manager 在批准窗口内执行。

## 9. 目录结构

```
deploy/machine-room/
├── Dockerfile                 # 机房版镜像（7 层，层注释说明目的）
├── .dockerignore              # 构建上下文忽略清单（含凭据类文件）
├── README.md                  # 本文
├── helm/djb-node/
│   ├── Chart.yaml
│   ├── values.yaml            # 默认值 = 最安全值
│   └── templates/
│       ├── _helpers.tpl        # 名字/标签/镜像/SA 名
│       ├── deployment.yaml     # 主形态（bootstrap initContainer + 回环 exec 探针）
│       ├── daemonset.yaml      # values.mode=daemonset 时渲染
│       ├── configmap.yaml      # /config/config.json（非敏感配置；开关三字段校验）
│       ├── serviceaccount.yaml # 空 SA，不注入 token
│       ├── pdb.yaml            # 仅 deployment 模式
│       ├── pvc.yaml            # 可选持久卷（keep 注解）
│       └── NOTES.txt
├── offline/
│   ├── build.sh               # 打 wheel + manifest + sha256sums（可选签名）
│   ├── verify.sh              # 签名/哈希/manifest 交叉校验
│   └── install.sh             # 只从本地 wheel 装（--no-index --find-links）
└── fleet/
    ├── djb-fleet.py           # 100 节点批量渲染（--check / --dry-run）
    ├── config.json.template    # 占位符模板
    └── inventory.example.csv   # 100 行示例（占位网关、无凭据）
```
