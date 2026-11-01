# SPDX-License-Identifier: Apache-2.0
"""Ed25519 验签（RFC 8032）—— 更新清单 / 安装包的**公钥**信任锚。

为什么自带实现
==============

* node 客户端是 **stdlib-only**（M15 §8.2 / README-CLIENT：安装包不拖第三方密码库），
  而更新验签必须是非对称的（对称密钥发到每台机器 = 每台机器都能伪造更新，见 G10 /
  审计 P1-3）。标准库里没有 Ed25519，于是在这里放一份 RFC 8032 参考实现。
* 生产路径**只用 verify**：只碰公钥与公开数据，没有需要保密的输入，因此不涉及
  常数时间要求。
* :func:`sign` / :func:`public_key_from_seed` 只给本地工具与单测用（`tools/djb-update-sign.py`），
  私钥永远不进入客户端进程。

正确性证据（见 `tests/test_ed25519.py`）
=======================================

1. RFC 8032 §7.1 官方测试向量（TEST 1 / TEST 2 / TEST 1024 / SHA(abc)）逐字节比对，
   sign 与 verify 双向都验；
2. 公钥/签名畸形（长度、非规范 y、s ≥ L、低阶点）一律返回 False，不抛异常到调用方。

口径
====

``sign(seed, msg) -> 64 bytes``；``verify(pubkey, msg, sig) -> bool``；
``public_key_from_seed(seed) -> 32 bytes``。全部小端、raw bytes（不是 hex、不是 PEM）。
"""
from __future__ import annotations

import hashlib

__all__ = ["public_key_from_seed", "sign", "verify", "is_valid_public_key_hex", "P"]

#: 域参数（RFC 8032 §5.1）
P = 2 ** 255 - 19
_D = -121665 * pow(121666, P - 2, P) % P
_Q = 2 ** 252 + 27742317777372353535851937790883648493  # 群的阶 L
_HEX_DIGITS = set("0123456789abcdef")


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _inv(value: int) -> int:
    return pow(value, P - 2, P)


def _recover_x(y: int, sign_bit: int) -> int | None:
    """由 y 与符号位恢复 x；不在曲线上 / 非规范编码返回 None。"""
    if y >= P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1) % P
    if x2 == 0:
        return None if sign_bit else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = x * pow(2, (P - 1) // 4, P) % P
    if (x * x - x2) % P != 0:
        return None
    if (x & 1) != sign_bit:
        x = P - x
    return x


def _base_point() -> tuple[int, int, int, int]:
    y = 4 * _inv(5) % P
    x = _recover_x(y, 0)
    assert x is not None
    return (x, y, 1, x * y % P)


_G = _base_point()


def _add(left: tuple[int, int, int, int],
         right: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """扩展扭曲爱德华坐标点加（RFC 8032 §5.1.4）。"""
    a = (left[1] - left[0]) * (right[1] - right[0]) % P
    b = (left[1] + left[0]) * (right[1] + right[0]) % P
    c = 2 * left[3] * right[3] * _D % P
    d = 2 * left[2] * right[2] % P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(scalar: int, point: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    result = (0, 1, 1, 0)
    addend = point
    while scalar > 0:
        if scalar & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        scalar >>= 1
    return result


def _equal(left: tuple[int, int, int, int],
           right: tuple[int, int, int, int]) -> bool:
    if (left[0] * right[2] - right[0] * left[2]) % P != 0:
        return False
    return (left[1] * right[2] - right[1] * left[2]) % P == 0


def _compress(point: tuple[int, int, int, int]) -> bytes:
    zinv = _inv(point[2])
    x = point[0] * zinv % P
    y = point[1] * zinv % P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(raw: bytes) -> tuple[int, int, int, int] | None:
    if len(raw) != 32:
        return None
    y = int.from_bytes(raw, "little")
    sign_bit = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign_bit)
    if x is None:
        return None
    return (x, y, 1, x * y % P)


def _expand_seed(seed: bytes) -> tuple[int, bytes]:
    if len(seed) != 32:
        raise ValueError("Ed25519 seed 必须是 32 字节")
    digest = _sha512(seed)
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    return scalar, digest[32:]


def public_key_from_seed(seed: bytes) -> bytes:
    """seed(32B) -> 公钥(32B)。只给本地工具 / 单测用。"""
    scalar, _ = _expand_seed(seed)
    return _compress(_mul(scalar, _G))


def sign(seed: bytes, message: bytes) -> bytes:
    """seed(32B) + message -> 签名(64B)。只给本地工具 / 单测用。"""
    scalar, prefix = _expand_seed(seed)
    public = _compress(_mul(scalar, _G))
    nonce = int.from_bytes(_sha512(prefix + message), "little") % _Q
    encoded_r = _compress(_mul(nonce, _G))
    challenge = int.from_bytes(_sha512(encoded_r + public + message), "little") % _Q
    s = (nonce + challenge * scalar) % _Q
    return encoded_r + int.to_bytes(s, 32, "little")


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """验签：任何畸形输入都返回 False（不抛异常、不「宽松通过」）。"""
    try:
        if len(public_key) != 32 or len(signature) != 64:
            return False
        point_a = _decompress(public_key)
        if point_a is None:
            return False
        point_r = _decompress(signature[:32])
        if point_r is None:
            return False
        s = int.from_bytes(signature[32:], "little")
        if s >= _Q:
            return False
        challenge = int.from_bytes(
            _sha512(signature[:32] + public_key + message), "little") % _Q
        return _equal(_mul(s, _G), _add(point_r, _mul(challenge, point_a)))
    except (ValueError, TypeError, OverflowError):        # 防御：畸形输入不炸调用方
        return False


def is_valid_public_key_hex(text: str) -> bool:
    """64 位小写/大写 hex 且能解压成曲线点（用于内置公钥自检）。"""
    if not isinstance(text, str) or len(text) != 64:
        return False
    if any(char not in _HEX_DIGITS for char in text.lower()):
        return False
    try:
        raw = bytes.fromhex(text)
    except ValueError:                                   # pragma: no cover - 长度已校验
        return False
    return _decompress(raw) is not None
