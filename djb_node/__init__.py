# SPDX-License-Identifier: Apache-2.0
"""DJB 联脑 · M15 client-runtime（Linux CLI + daemon，S1）。

模块边界（product/v4/modules/M15-client-runtime.md）：

* 只做本机客户端：CLI + daemon + 本机回环镜像端点 + 出站调用（M02/M05）。
* 不做服务端业务：注册准入、策略生成、收据验签、计费、调度全在服务端。
* 本机存储只有 SQLite / 0600 文件；服务端表 0 张；不改 spec。
* 不监听 0.0.0.0、不上传凭据、不落 prompt / response 正文。

包内分层（单包，避免跨模块 import）：

    facts.py      常量（03-facts §11 + M15 §8.1/§8.2），唯一来源
    errors.py     错误码 / 退出码 / Error 信封
    util.py       时间、JSON、ID、脱敏、原子写
    config.py     本机路径与 client.* 默认值
    platform/     三端平台抽象（paths / service / credentials / service_plan）
    tray/         C3 托盘模型（menu/state/badge/intent；纯数据，无 GUI）
    store.py      SQLite 本地状态（PRAGMA user_version 只前进）
    crypto.py     收据缓冲的密封 / 校验（S1 标准库构造，见 README-CLIENT CR 表）
    secure.py     凭据（0600 文件，S1 允许并显式标注；日志/诊断绝不出现）
    state.py      daemon 状态机（stopped/starting/online/paused/error/updating）
    logging_setup.py  结构化 JSON 日志 + 轮转 + 脱敏
    backoff.py    指数退避（1/5/30/300/1800 s，≤8 次）
    httpclient.py 出站 HTTP（标准库）+ SSE 转发
    protocol.py   协议常量与 tolerant reader
    api.py        M02 / M05 出站调用（register/heartbeat/policy/upstream/probe/receipts）
    outbox.py     双签收据加密缓冲（≤24 h）
    limits.py     护栏与限额 L1 > L2 > L3
    envcheck.py   环境检测（结论 + 下一步、一次授权、可回滚）
    upstream.py   A1/A2/B 上游接入、A2 五字段、模型清单
    server.py     本机回环 127.0.0.1:1717/v1（一次性 local_token）
    daemon.py     daemon 主循环（心跳 / 策略 / 公告 / 收据 / 让路 / 配额）
    update.py     更新骨架（分桶 / 签名 + 哈希 / 回滚 / 保留 2 版）
    diagnose.py   诊断包（脱敏，无凭据 / 无正文）
    cli.py        djb 全命令面
"""

__version__ = "0.3.6"
MODULE_ID = "M15"
