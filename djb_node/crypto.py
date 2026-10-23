# SPDX-License-Identifier: Apache-2.0
"""收据缓冲的密封 / 完整性校验（M15 §5 receipts.buf「加密缓冲」）。

S1 只在标准库里选构造（客户端要能装进最小镜像，不能拖第三方密码库）：

* 加密：HMAC-SHA256 计数器模式（HMAC(key, nonce || counter) 作为密钥流，逐块异或）；
* 完整性：HMAC-SHA256(key, domain || nonce || ciphertext) 常量时间比较；
* 密钥：256 bit，单独存 keys/buffer.key（0600），不进日志 / 诊断包 / 数据库。

**这是 S1 的临时构造，不是经审计的 AEAD**（CR-M15-02）：格式前缀 s1: 便于 S2 换成
AES-256-GCM / ChaCha20-Poly1305 后向后兼容迁移；换算法只改本文件，调用方不感知。
"""
from __future__ import annotations

import hmac as _hmac
import secrets
from hashlib import sha256

SEAL_PREFIX = "s1"
_SEAL_DOMAIN = b"djb-m15-receipt-buffer-v1"
_NONCE_BYTES = 16
_BLOCK_BYTES = 32
TAG_HEX_LEN = 64


class TamperError(Exception):
    """密封串被篡改 / 截断 / 密钥不匹配。"""


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    blocks = (length + _BLOCK_BYTES - 1) // _BLOCK_BYTES
    out = bytearray()
    for counter in range(blocks):
        out.extend(_hmac.new(key, nonce + counter.to_bytes(8, "big"), sha256).digest())
    return bytes(out[:length])


def _tag(key: bytes, nonce: bytes, ciphertext: bytes) -> str:
    return _hmac.new(key, _SEAL_DOMAIN + nonce + ciphertext, sha256).hexdigest()


def seal(key: bytes, plaintext: bytes | str) -> str:
    if not key:
        raise ValueError("密封密钥不能为空")
    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")
    nonce = secrets.token_bytes(_NONCE_BYTES)
    stream = _keystream(key, nonce, len(plaintext))
    ciphertext = bytes(a ^ b for a, b in zip(plaintext, stream))
    return ":".join((SEAL_PREFIX, nonce.hex(), ciphertext.hex(),
                     _tag(key, nonce, ciphertext)))


def open_sealed(key: bytes, token: str) -> bytes:
    if not key:
        raise ValueError("密封密钥不能为空")
    parts = str(token).split(":")
    if len(parts) != 4 or parts[0] != SEAL_PREFIX:
        raise TamperError("密封串格式不合法")
    try:
        nonce = bytes.fromhex(parts[1])
        ciphertext = bytes.fromhex(parts[2])
    except ValueError as exc:
        raise TamperError("密封串编码不合法") from exc
    expected = _tag(key, nonce, ciphertext)
    if not _hmac.compare_digest(expected, parts[3]):
        raise TamperError("密封串完整性校验失败（可能被篡改或密钥不匹配）")
    stream = _keystream(key, nonce, len(ciphertext))
    return bytes(a ^ b for a, b in zip(ciphertext, stream))


def key_fingerprint(key: bytes) -> str:
    """短指纹用于「确认是哪把钥匙」，不可反推、不能用于鉴权。"""
    return _hmac.new(key, b"djb-m15-fingerprint", sha256).hexdigest()[:12]


def new_key() -> bytes:
    return secrets.token_bytes(32)
