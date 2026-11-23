# SPDX-License-Identifier: Apache-2.0
"""快照缓存（R2.4 S3/S4）：启动先画上次的结果，**启动路径零网络**。

为什么要有它
============

真机冷启动的 3 s 里，最大一段是「等第一次取数」：``status`` 走控制口、账户/收益走云端
HTTP。客户看到的是空窗口 —— 不是慢，是**没有内容**。缓存让窗口在第一次启动后就能
立刻画出上一次的结果，然后后台刷新把它换掉。

诚实口径（不许拿旧数据冒充新数据）
==================================

* 只认 **30 分钟内** 的快照（``MAX_AGE_S``）；超时当作没有；
* 画缓存时 ``Snapshot.source`` 标成 ``"cache"``，界面状态条写「显示 12:03 的缓存，正在刷新」，
  绝不写「已刷新」；
* 缓存文件只是**派生数据**：读失败/写失败/格式变了都当成没有，不影响取数；
* 权限 0600、目录 0700（与 credentials 同级要求），路径不在 ``home`` 外。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Mapping

__all__ = ["MAX_AGE_S", "CACHE_SUBDIR", "CACHE_FILE", "cache_path", "load", "save", "clear",
           "jsonable"]

#: 缓存有效期（秒）：超过就当没有 —— 旧数据比空白更危险
MAX_AGE_S = 1800
CACHE_SUBDIR = "desktop"
CACHE_FILE = "snapshot-cache.json"


def jsonable(value: Any) -> Any:
    """把快照投影转成可 JSON 化的形状（``Unavailable`` 走 ``to_dict``，其它原样）。

    快照里既有 dict/list，也有 ``Unavailable`` 这种 dataclass。缓存只存**投影**，
    所以这里只认 ``to_dict()``，不做通用 dataclass 反射 —— 不认识的对象一律转字符串，
    宁可在缓存里丢一格，也不许把内部对象写进磁盘。
    """
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return jsonable(to_dict())
        except Exception:                     # pragma: no cover - to_dict 自己坏了
            return str(value)
    return str(value)


def cache_path(home: str | os.PathLike[str] | None) -> str | None:
    """``<home>/desktop/snapshot-cache.json``；home 为空返回 ``None``（demo/无本机目录）。"""
    if not home:
        return None
    base = os.path.abspath(os.path.expanduser(os.fspath(home)))
    return os.path.join(base, CACHE_SUBDIR, CACHE_FILE)


def save(home: str | os.PathLike[str] | None, sections: Mapping[str, Any] | None,
         *, fetched_at: str = "", now: float | None = None) -> bool:
    """把快照的 ``sections`` 落盘（**只存投影数据，不存任何密钥**）。

    返回 ``True`` = 真写成功；任何异常都返回 ``False``，调用方不需要 try。
    """
    path = cache_path(home)
    if not path or not isinstance(sections, Mapping) or not sections:
        return False
    payload = {"version": 1, "saved_at": float(now if now is not None else time.time()),
               "fetched_at": str(fetched_at or ""),
               "sections": {str(key): jsonable(item) for key, item in sections.items()}}
    try:
        folder = os.path.dirname(path)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        # 先写 tmp 再 rename：崩在写一半也不会留下半个 JSON（下次启动读到就是「没有缓存」）
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:                       # Windows 上 chmod 只是尽力而为
            pass
        os.replace(tmp, path)
        return True
    except Exception:                         # 磁盘满 / 权限 / 只读盘：当作没缓存
        return False


def load(home: str | os.PathLike[str] | None, *, max_age_s: float = MAX_AGE_S,
         now: float | None = None) -> dict[str, Any] | None:
    """读缓存；没有/过期/格式不对都返回 ``None``（**不抛**）。

    返回形状：``{"sections": {...}, "fetched_at": "…", "saved_at": 1763…, "age_s": 12.3}``
    """
    path = cache_path(home)
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:                         # 不存在/坏 JSON/无权限
        return None
    if not isinstance(payload, Mapping) or payload.get("version") != 1:
        return None
    sections = payload.get("sections")
    if not isinstance(sections, Mapping) or not sections:
        return None
    try:
        saved_at = float(payload.get("saved_at") or 0)
    except (TypeError, ValueError):
        return None
    age = float(now if now is not None else time.time()) - saved_at
    if saved_at <= 0 or age < -300 or age > float(max_age_s):     # 未来时间也当坏数据
        return None
    return {"sections": dict(sections), "fetched_at": str(payload.get("fetched_at") or ""),
            "saved_at": saved_at, "age_s": age}


def clear(home: str | os.PathLike[str] | None) -> bool:
    """删掉缓存文件（排障/「清空本机缓存」用）。删不到也算成功。"""
    path = cache_path(home)
    if not path:
        return False
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except Exception:
        return False
