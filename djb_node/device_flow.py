# SPDX-License-Identifier: Apache-2.0
"""设备码自动绑定（RC14 C）：客户端不必再手抄绑定码。

协议（lead 2026-11-14 给定，与 40-UX-DESIGN §3.5 一致；客户端**不自造字段**）：

* ``POST /api/v1/node/binding/device`` → ``{user_code, verify_url, expires_in,
  device_code, interval?}``；用户在浏览器打开 ``verify_url``（网页 ``/activate``）确认；
* ``POST /api/v1/node/binding/device/poll`` → ``{state: pending|approved|denied|expired}``；
  轮询节奏 **5s → 15s → 30s**（之后保持 30s），**满 10 分钟停止**；
* 云端 404 / 超时 / 连不上 → 「绑定服务未开通，请用高级→手输绑定码」，**绝不假成功**。

本模块只有状态机与请求体，**不碰 Tk、不 sleep**：等待由调用方（worker 线程 / 测试假时钟）
通过 :func:`next_delay` 与 :meth:`DeviceFlow.step` 驱动。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlparse

#: 轮询间隔（秒）：5 → 15 → 30，之后恒定 30（lead 口径）
BACKOFF_S: tuple[int, ...] = (5, 15, 30)
#: 总时长上限（秒）：满 10 分钟停止轮询（不是「无限等」）
DEADLINE_S = 600

DEVICE_PATH = "/api/v1/node/binding/device"
DEVICE_POLL_PATH = "/api/v1/node/binding/device/poll"
ACTIVATE_PATH = "/activate"          # 网页确认页（云端页面，客户端只给链接）

#: 云端未开通时的统一文案（**这一段是产品口径，测试逐字断言**）
NOT_OPEN_TEXT = ("绑定服务未开通，请用高级→手输绑定码"
                 "（本机仍可照常运行；手输码走老的 djb auth --bind 路径）")

STATES = ("idle", "pending", "approved", "denied", "expired", "unavailable")


def next_delay(attempt: int) -> int:
    """第 ``attempt`` 次轮询后的等待秒数（attempt 从 0 开始）：5 / 15 / 30 / 30…"""
    index = min(max(0, int(attempt)), len(BACKOFF_S) - 1)
    return BACKOFF_S[index]


@dataclass
class DeviceFlow:
    """一次设备码流程的本地状态（**不存 device_code 到磁盘**，只在内存里）。"""

    state: str = "idle"
    user_code: str = ""
    verify_url: str = ""
    device_code: str = ""
    expires_in: int = 0
    interval_s: int = 0
    attempts: int = 0
    elapsed_s: int = 0
    reason: str = ""
    hint: str = ""
    #: 已确认绑定（只有 approved 才是 True；其余一律 None/False，不能含糊）
    approved: bool = False
    #: approved 时云端回的落库节点 ID；为空 = 云端只批了账户，不能算绑定成功。
    bound_node_id: str = ""
    events: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "user_code": self.user_code,
                "verify_url": self.verify_url, "expires_in": self.expires_in,
                "attempts": self.attempts, "elapsed_s": self.elapsed_s,
                "reason": self.reason, "hint": self.hint, "approved": self.approved,
                "node_id": self.bound_node_id}


def absolute_verify_url(verify_url: object, base_url: object = "") -> str:
    """把云端给的 ``verify_url`` 变成可打开的绝对地址。

    云端现网回的是相对路径 ``/activate``（F3 实测）：``webbrowser.open("/activate")``
    在 Windows 上什么都不做，客户看到的就是「点了没反应」。有网关基址时用
    :func:`urljoin` 拼成绝对地址；已经是 http(s) 的原样返回；两者都没有就返回空串，
    让调用方**不打开**也不假装打开了。
    """
    url = str(verify_url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
        return url
    base = str(base_url or "").strip()
    if not base:
        return ""
    if url.startswith("//") or url.startswith("\\\\"):
        return ""                     # 协议相对/反斜杠路径可能指向别的主机，一律不认
    resolved = urljoin(base.rstrip("/") + "/", url.lstrip("/"))
    final = urlparse(resolved)
    return resolved if final.scheme.lower() in ("http", "https") and final.netloc else ""


def parse_start(payload: Mapping[str, Any], *, base_url: object = "") -> DeviceFlow:
    """``POST /device`` 的响应 → 初始状态。缺字段就当云端没开通（不编造码）。"""
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    user_code = str((data or {}).get("user_code") or "").strip()
    device_code = str((data or {}).get("device_code") or "").strip()
    verify_url = absolute_verify_url((data or {}).get("verify_url"), base_url)
    if not (user_code and device_code):
        return unavailable("云端没有返回设备码（user_code / device_code 缺失）")
    try:
        expires_in = int((data or {}).get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    try:
        interval_s = int((data or {}).get("interval") or 0)
    except (TypeError, ValueError):
        interval_s = 0
    flow = DeviceFlow(state="pending", user_code=user_code, device_code=device_code,
                      verify_url=verify_url, expires_in=expires_in, interval_s=interval_s)
    flow.events.append("started")
    return flow


def unavailable(reason: str) -> DeviceFlow:
    """服务未开通 / 不可用：给统一文案，**state 不是 pending**（不假装在等）。"""
    return DeviceFlow(state="unavailable", reason=str(reason or "云端没有响应"),
                      hint=NOT_OPEN_TEXT)


def apply_poll(flow: DeviceFlow, payload: Mapping[str, Any] | None,
               *, attempts: int | None = None) -> DeviceFlow:
    """把一次 ``poll`` 的结果落进状态机（``payload=None`` = 请求本身失败）。"""
    if attempts is not None:
        flow.attempts = int(attempts)
    if payload is None:
        flow.state = "unavailable"
        flow.reason = flow.reason or "云端连不上（poll 没有响应）"
        flow.hint = NOT_OPEN_TEXT
        flow.events.append("poll_unreachable")
        return flow
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    # R8（F3）：云端现网 poll 回的是 ``status``（rc8_admin.device_auth.poll），
    # 客户端历史只读 ``state`` → 永远「无法识别的状态」。两个字段都认：state 优先，
    # status 兜底（旧云端给 state 时行为不变）。
    raw_state = (data or {}).get("state")
    if raw_state in (None, ""):
        raw_state = (data or {}).get("status")
    state = str(raw_state or "").strip().lower()
    if state not in ("pending", "approved", "denied", "expired"):
        flow.state = "unavailable"
        flow.reason = "云端返回了无法识别的状态：" + (state or "（空）")
        flow.hint = NOT_OPEN_TEXT
        flow.events.append("poll_unknown_state")
        return flow
    flow.state = state
    flow.approved = state == "approved"
    flow.bound_node_id = (str((data or {}).get("node_id") or "").strip()
                          if state == "approved" else "")
    if state == "denied":
        flow.reason = "这次绑定被拒绝（网页端点了拒绝）"
        flow.hint = "要重试就再点「在浏览器确认绑定」，或用高级→手输绑定码"
    elif state == "expired":
        flow.reason = "设备码已过期"
        flow.hint = "再点一次「在浏览器确认绑定」拿新码；或用高级→手输绑定码"
    flow.events.append("poll_" + state)
    return flow


def poll_body(flow: DeviceFlow) -> dict[str, Any]:
    """``POST /device/poll`` 的请求体（字段名按 lead 给的口径，逐字）。"""
    return {"device_code": flow.device_code}


def should_stop(flow: DeviceFlow, elapsed_s: float) -> bool:
    """该不该停止轮询：终态 / 服务不可用 / 超过 10 分钟。"""
    if flow.state in ("approved", "denied", "expired", "unavailable"):
        return True
    return float(elapsed_s) >= DEADLINE_S


def timeout_flow(flow: DeviceFlow) -> DeviceFlow:
    """满 10 分钟：如实说「等太久了」，并给手输码这条路。"""
    if flow.state == "pending":
        flow.state = "expired"
        flow.reason = "等了 10 分钟还没在网页确认，已停止等待"
        flow.hint = "重新点「在浏览器确认绑定」拿新码；或用高级→手输绑定码"
        flow.events.append("deadline")
    return flow


#: 状态 → 界面文案（绑定面板一句话）
STATE_TEXT = {
    "idle": "还没开始",
    "pending": "等你到浏览器确认",
    "approved": "已绑定",
    "denied": "被拒绝",
    "expired": "已过期",
    "unavailable": "绑定服务未开通",
}


class DeviceFlowRunner:
    """可注入的轮询执行器：**不做网络、不 sleep**，只把时钟与请求交给调用方。

    ``post`` = ``(path, body) -> Mapping``（云端客户端；抛异常 = 请求失败）；
    ``sleep`` = 等待函数；``clock`` = 单调秒。
    生产由 worker 线程驱动；测试注入假时钟，一秒都不真等。
    """

    def __init__(self, post: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
                 *, sleep: Callable[[float], None], clock: Callable[[], float],
                 on_update: Callable[[DeviceFlow], None] | None = None,
                 stop: Callable[[], bool] | None = None,
                 node_id: str = "", base_url: str = "") -> None:
        self._post = post
        self._node_id = str(node_id or "").strip()
        #: 网关基址：云端 ``verify_url`` 是相对路径时用它拼绝对地址（F3）
        self._base_url = str(base_url or "").strip()
        self._sleep = sleep
        self._clock = clock
        self._on_update = on_update
        self._stop = stop or (lambda: False)

    def _emit(self, flow: DeviceFlow) -> None:
        if self._on_update is not None:
            try:
                self._on_update(flow)
            except Exception:                             # 播报失败不许打断流程
                pass

    def run(self) -> DeviceFlow:
        """跑完整流程：起码 → 轮询到终态 / 10 分钟 / 服务不可用。"""
        flow = self.start()
        self._emit(flow)
        if flow.state != "pending":
            return flow
        started = self._clock()
        while True:
            if self._stop():                              # 用户关窗口 / 点了取消
                flow.events.append("cancelled")
                break
            delay = next_delay(flow.attempts)
            self._sleep(delay)
            flow.elapsed_s = int(self._clock() - started)
            if should_stop(flow, flow.elapsed_s):
                timeout_flow(flow)
                self._emit(flow)
                break
            flow = self.step(flow, started)
            self._emit(flow)
            if should_stop(flow, flow.elapsed_s):
                break
        return flow

    def start(self) -> DeviceFlow:
        body = {"node_id": self._node_id} if self._node_id else {}
        try:
            payload = self._post(DEVICE_PATH, body)
        except Exception as exc:                           # 404 / 超时 / 连不上
            return unavailable(_error_text(exc))
        return parse_start(payload, base_url=self._base_url)

    def step(self, flow: DeviceFlow, started: float) -> DeviceFlow:
        flow.attempts += 1
        try:
            payload = self._post(DEVICE_POLL_PATH, poll_body(flow))
        except Exception as exc:                           # 404/超时/连不上：见 _fail
            flow.elapsed_s = int(self._clock() - started)
            return _fail(flow, exc, started=started, clock=self._clock)
        flow.elapsed_s = int(self._clock() - started)
        return apply_poll(flow, payload, attempts=flow.attempts)


def _fail(flow: DeviceFlow, exc: BaseException, *, started: float,
          clock: Callable[[], float]) -> DeviceFlow:
    """poll 请求失败：**先看是不是还能再试**（超时是常态），只有 404/不认才有结论。"""
    status = getattr(exc, "status", None)
    if status in (404, 405, 501):
        flow.state = "unavailable"
        flow.reason = "云端没有这个接口（" + str(status) + "）"
        flow.hint = NOT_OPEN_TEXT
        flow.events.append("poll_not_open")
        return flow
    if float(clock() - started) >= DEADLINE_S:
        return timeout_flow(flow)
    flow.state = "pending"                                # 网络抖动：继续按退避重试
    flow.reason = _error_text(exc)
    flow.events.append("poll_retry")
    return flow


def _error_text(exc: BaseException) -> str:
    status = getattr(exc, "status", None)
    if status in (404, 405, 501):
        return "云端没有这个接口（" + str(status) + "）"
    reason = str(getattr(exc, "reason", "") or "").strip()
    return reason or (type(exc).__name__ + "：" + str(exc)[:80])
