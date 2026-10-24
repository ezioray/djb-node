# SPDX-License-Identifier: Apache-2.0
"""M02 / M05 出站调用（只消费，不拥有）。

端点与信封按 spec 1.2.4（只读契约）：

    POST /api/v1/node/register            201  首次 pending；同指纹幂等
    POST /api/v1/node/pair                201  生成 / 轮换 X-Client-Key（明文只回一次）
    GET  /api/v1/node/binding                  查询绑定
    POST /api/v1/node/binding                  一次性码绑定
    POST /api/v1/node/heartbeat           200  心跳 + 容量公告
    POST /api/v1/node/upstream            200  申报 A1/A2/B（只报 credential_handle）
    POST /api/v1/node/upstream/probe      200  上报**本地**体检结论（服务端不探测）
    GET  /api/v1/node/policy              200 / 304（If-None-Match = policy_version）
    GET  /api/v1/protocol/capabilities    200
    GET  /api/v1/protocol/deprecations    200
    POST /api/v1/node/channel/poll        200 / 204（C10c 长轮询取 job；204 = 暂无 job）
    POST /api/v1/node/channel/result      200  回传 job 结果（幂等键 = job_id）
    GET  /api/v1/node/model-catalog       200 / 304（M18b 模型/tokenizer 签名 bundle）
    POST /api/v1/node/model-sync/state    200  上报已应用版本 / 漂移 / 错误类

硬规则：出站头只用 spec 头名；凭据只在 X-Client-Key 头里出现，绝不进 URL / body /
日志；错误信封只按 type / code / reason 判断，不解析 message 文案。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from . import facts, protocol
from .errors import (AuthError, ConflictError, DjbError, ForbiddenError, NetworkError,
                     RateLimitError, UsageError, VerifyError)
from .httpclient import HttpClient, HttpResponse, join_url
from .util import first_line, new_request_id, utcnow


@dataclass
class PolicyFetch:
    status: int
    payload: dict[str, Any] | None
    etag: str | None
    not_modified: bool


class NodeApi:
    """M02 / M05 的薄客户端。401 一律 fail-closed；429/5xx 交给退避层。"""

    def __init__(self, base_url: str, *, client_key: str | None = None,
                 http: HttpClient | None = None,
                 protocol_version: str = facts.PROTOCOL_VERSION,
                 capabilities: tuple[str, ...] = protocol.CLIENT_CAPABILITIES,
                 clock: Callable[[], Any] = utcnow) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.client_key = client_key
        self.http = http or HttpClient()
        self.protocol_version = protocol_version
        self.client_capabilities = tuple(capabilities)
        self._clock = clock

    # ─────────────────────────────────────────────────────── 基础
    def url(self, path: str) -> str:
        return join_url(self.base_url, path)

    def _headers(self, *, idempotency_key: str | None = None,
                 request_id: str | None = None,
                 extra: dict[str, str] | None = None) -> dict[str, str]:
        return protocol.build_headers(
            client_key=self.client_key, protocol_version=self.protocol_version,
            capabilities=self.client_capabilities,
            request_id=request_id or new_request_id(),
            idempotency_key=idempotency_key, extra=extra)

    def _call(self, method: str, path: str, *, json_body: Any = None,
              idempotency_key: str | None = None,
              extra_headers: dict[str, str] | None = None,
              timeout: float | None = None) -> HttpResponse:
        response = self.http.request(
            method, self.url(path),
            headers=self._headers(idempotency_key=idempotency_key, extra=extra_headers),
            json_body=json_body, timeout=timeout)
        if response.status >= 400:
            raise raise_for_response(response, what=path)
        return response

    # ─────────────────────────────────────────────────────── 协议（无鉴权）
    def capabilities(self) -> protocol.CapabilityView:
        response = self._call("GET", protocol.PATHS["capabilities"], timeout=10)
        return protocol.parse_capabilities(response.json())

    def deprecations(self, *, protocol_major: int | None = None) -> list[dict[str, Any]]:
        path = protocol.PATHS["deprecations"]
        if protocol_major is not None:
            path = path + "?protocol_major=" + str(int(protocol_major))
        response = self._call("GET", path, timeout=10)
        return protocol.parse_deprecations(response.json())

    # ─────────────────────────────────────────────────────── 注册 / 绑定
    def register(self, *, hardware_fingerprint: str,
                 access_modes: list[str] | None = None,
                 capabilities: list[str] | None = None,
                 platform: str = facts.PLATFORM,
                 client_version: str = facts.CLIENT_VERSION) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "platform": platform,
            "hardware_fingerprint": hardware_fingerprint,
            "client_version": client_version,
            "protocol_version": self.protocol_version,
            "capabilities": list(capabilities or self.client_capabilities),
        }
        if access_modes:
            payload["access_modes"] = list(access_modes)
        response = self._call("POST", protocol.PATHS["register"], json_body=payload, timeout=20)
        body = response.json()
        if not isinstance(body, dict) or not body.get("node_id"):
            raise NetworkError("注册响应缺少 node_id（契约不匹配）", code=45001)
        return body

    def pair_key(self, node_id: str, *, rotate: bool = False,
                 overlap_seconds: int = 0) -> dict[str, Any]:
        payload = {"node_id": node_id, "rotate": bool(rotate),
                   "overlap_seconds": int(max(0, overlap_seconds))}
        response = self._call("POST", protocol.PATHS["pair"], json_body=payload, timeout=20)
        body = response.json()
        if not isinstance(body, dict) or not body.get("client_key"):
            raise NetworkError("密钥轮换响应缺少 client_key（契约不匹配）", code=45001)
        return body

    def binding(self, node_id: str) -> dict[str, Any]:
        response = self._call("GET", protocol.PATHS["binding"] + "?node_id="
                              + _quote(node_id), timeout=10)
        body = response.json()
        return body if isinstance(body, dict) else {}

    def bind(self, node_id: str, code: str) -> dict[str, Any]:
        payload = {"node_id": node_id, "code": code}
        response = self._call("POST", protocol.PATHS["binding"], json_body=payload, timeout=20)
        body = response.json()
        return body if isinstance(body, dict) else {}

    def unbind(self, node_id: str) -> dict[str, Any]:
        response = self._call("DELETE", protocol.PATHS["binding"] + "?node_id="
                              + _quote(node_id), timeout=10)
        body = response.json()
        return body if isinstance(body, dict) else {}

    # ─────────────────────────────────────────────────────── 心跳 / 公告
    def heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._call("POST", protocol.PATHS["heartbeat"], json_body=payload,
                              timeout=15)
        body = response.json()
        if not isinstance(body, dict):
            raise NetworkError("心跳响应不是 JSON 对象（契约不匹配）", code=45001)
        return body

    # ─────────────────────────────────────────────────────── 上游
    def declare_upstream(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._call("POST", protocol.PATHS["upstream"], json_body=payload,
                              timeout=20)
        body = response.json()
        return body if isinstance(body, dict) else {}

    def report_probe(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._call("POST", protocol.PATHS["upstream_probe"], json_body=payload,
                              timeout=20)
        body = response.json()
        return body if isinstance(body, dict) else {}

    # ─────────────────────────────────────────────────────── 策略
    def policy(self, *, if_none_match: str | None = None) -> PolicyFetch:
        extra: dict[str, str] = {}
        if if_none_match:
            extra[protocol.IF_NONE_MATCH] = if_none_match
        response = self.http.request(
            "GET", self.url(protocol.PATHS["policy"]),
            headers=self._headers(extra=extra), timeout=15)
        if response.status == 304:
            return PolicyFetch(status=304, payload=None,
                              etag=response.headers.get("etag"), not_modified=True)
        if response.status >= 400:
            raise raise_for_response(response, what=protocol.PATHS["policy"])
        payload = response.json()
        return PolicyFetch(status=response.status,
                           payload=payload if isinstance(payload, dict) else None,
                           etag=response.headers.get("etag"), not_modified=False)

    # ─────────────────────────────────────────────────────── 出站长轮询通道（C10c）
    def poll_channel(self, wait_s: int = facts.CHANNEL_POLL_WAIT_S) -> dict[str, Any] | None:
        """POST /api/v1/node/channel/poll 长轮询取 job。

        * 200 → 解析 job（JSON 对象；形状由 channel.parse_job 校验）；
        * 204 → 本轮没有 job，返回 None（调用方立即再 poll）；
        * >=400 → 抛错（401/403 由调用方判停，其余走退避）。

        超时按 ``wait_s + 10s`` 余量，避免服务端挂起时间略超就判网络失败。
        """
        response = self.http.request(
            "POST", self.url(facts.CHANNEL_POLL_PATH),
            headers=self._headers(), json_body={"wait_s": int(wait_s)},
            timeout=facts.CHANNEL_POLL_TIMEOUT_S)
        if response.status == 204:
            return None
        if response.status >= 400:
            raise raise_for_response(response, what=facts.CHANNEL_POLL_PATH)
        payload = response.json()
        return payload if isinstance(payload, dict) else None

    def submit_channel_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /api/v1/node/channel/result 回传执行结果（幂等键 = job_id）。

        返回 ``{"accepted": [...], "duplicate": bool}``；缺 accepted 视为契约不匹配。
        """
        job_id = str(payload.get("job_id") or "").strip()
        if not job_id:
            raise UsageError("通道结果缺少 job_id（无法幂等回传）")
        response = self._call("POST", facts.CHANNEL_RESULT_PATH, json_body=payload,
                              idempotency_key=job_id,
                              timeout=facts.CHANNEL_RESULT_TIMEOUT_S)
        body = response.json()
        if not isinstance(body, dict) or "accepted" not in body:
            raise NetworkError("通道结果响应缺少 accepted（契约不匹配）", code=45001)
        return body

    # ─────────────────────────────────────────────────────── 模型同步（M18b）
    def model_catalog(self, since: int | str | None = None) -> dict[str, Any]:
        """GET /api/v1/node/model-catalog 拉签名 bundle。

        * 带 ``since`` 时作为查询参数（服务端可回 304 / ``not_modified=true``）；
        * 200 → bundle JSON；304 → ``{"not_modified": True}``（调用方据此跳过应用）。

        返回值只做形状兜底，字段语义交给 ``model_sync.sync()`` 校验。
        """
        path = facts.MODEL_CATALOG_PATH
        if since is not None and str(since).strip() != "":
            path = path + "?since=" + _quote(str(since).strip())
        response = self.http.request(
            "GET", self.url(path), headers=self._headers(),
            timeout=facts.MODEL_SYNC_TIMEOUT_S)
        if response.status == 304:
            return {"not_modified": True, "version": since}
        if response.status >= 400:
            raise raise_for_response(response, what=facts.MODEL_CATALOG_PATH)
        payload = response.json()
        if not isinstance(payload, dict):
            raise NetworkError("模型目录响应不是 JSON 对象（契约不匹配）", code=45001)
        return payload

    def submit_model_sync_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /api/v1/node/model-sync/state 上报应用状态（只版本/漂移/错误类）。

        返回 ``{"accepted": bool, "server_version": ...}``；缺 accepted 视为契约不匹配。
        """
        response = self._call("POST", facts.MODEL_SYNC_STATE_PATH, json_body=payload,
                              timeout=facts.MODEL_SYNC_STATE_TIMEOUT_S)
        body = response.json()
        if not isinstance(body, dict) or "accepted" not in body:
            raise NetworkError("模型同步状态响应缺少 accepted（契约不匹配）", code=45001)
        return body

    # ─────────────────────────────────────────────────────── 收据（M05）
    def submit_receipts(self, receipts: list[dict[str, Any]], *,
                        idempotency_key: str) -> dict[str, Any]:
        response = self._call("POST", protocol.PATHS["receipts"],
                              json_body={"receipts": receipts},
                              idempotency_key=idempotency_key, timeout=25)
        body = response.json()
        if not isinstance(body, dict) or "accepted" not in body:
            raise NetworkError("收据批量响应缺少 accepted（契约不匹配）", code=45001)
        return body


def raise_for_response(response: HttpResponse, *, what: str) -> DjbError:
    """错误信封 -> 本模块异常（只按 type/code/reason；不解析文案）。"""
    status = int(response.status)
    code = response.error_code()
    reason = None
    payload = response.json()
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        value = payload["error"].get("reason")
        reason = value if isinstance(value, str) else None
    message = response.error_message() or ("请求失败（HTTP " + str(status) + "）")
    if reason:
        message = first_line(message) + " [reason=" + reason + "]"
    retry_after_header = response.headers.get("retry-after")
    retry_after = None
    if retry_after_header:
        try:
            retry_after = int(retry_after_header)
        except ValueError:
            retry_after = None
    default_codes = {400: 40001, 401: 40101, 403: 40305, 404: 40401,
                     409: 40919, 413: 40013, 426: 42601, 429: 42901}
    code = code if code not in (None, 0) else default_codes.get(status, 50001)
    details = {"status": status, "what": what}
    if status == 401:
        return AuthError(message, code=code, details=details, reason=reason)
    if status == 403:
        return ForbiddenError(message, code=code, details=details, reason=reason)
    if status == 409:
        return ConflictError(message, code=code, details=details, reason=reason)
    if status == 426:
        return VerifyError(message, code=code, details=details, reason=reason)
    if status == 429:
        return RateLimitError(message, code=code, details=details, reason=reason,
                              retry_after=retry_after)
    if status >= 500:
        return NetworkError(message, code=code, details=details, reason=reason)
    return UsageError(message, code=code, details=details, reason=reason)


def _quote(value: str) -> str:
    from urllib.parse import quote
    return quote(str(value), safe="")
