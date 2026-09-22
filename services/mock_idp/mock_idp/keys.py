"""RSA 签发密钥与 JWKS 导出（RS256，PRD 8.5.4）。

开发期密钥持久化到 keys/private.pem（首次启动自动生成），
保证 Broker 重启后已签发 token 仍可验签（对齐 8.5.7"已签发 token 本地验签不受影响"）。
"""

import base64
import hashlib
import json
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_KEYS_DIR = Path(__file__).resolve().parent.parent / "keys"
_PRIVATE_KEY_PATH = _KEYS_DIR / "private.pem"

_KID_PREFIX = "mock-idp-"
_KEY_SIZE = 2048


def _load_or_create_private_key() -> rsa.RSAPrivateKey:
    """加载持久化私钥；不存在则生成并落盘（仅开发期，生产密钥由 Keycloak 托管）。"""
    if _PRIVATE_KEY_PATH.exists():
        pem = _PRIVATE_KEY_PATH.read_bytes()
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise RuntimeError(f"密钥文件不是 RSA 私钥：{_PRIVATE_KEY_PATH}")
        return key

    key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_SIZE)
    _KEYS_DIR.mkdir(parents=True, exist_ok=True)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    _PRIVATE_KEY_PATH.write_bytes(pem)
    return key


_private_key = _load_or_create_private_key()

_public_numbers = _private_key.public_key().public_numbers()


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _thumbprint(n: bytes, e: bytes) -> str:
    """RFC 7638 JWK 指纹（SHA-256 前 16 hex 位作 kid）。"""
    jwk = {"e": _b64u(e), "kty": "RSA", "n": _b64u(n)}
    canonical = json.dumps(jwk, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


_n_bytes = _public_numbers.n.to_bytes((_public_numbers.n.bit_length() + 7) // 8, "big")
_e_bytes = _public_numbers.e.to_bytes((_public_numbers.e.bit_length() + 7) // 8, "big")
KID = _KID_PREFIX + _thumbprint(_n_bytes, _e_bytes)


def jwks() -> dict[str, object]:
    """JWKS 文档（agent_core / 网关本地验签公钥来源）。"""
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": KID,
                "n": _b64u(_n_bytes),
                "e": _b64u(_e_bytes),
            }
        ]
    }


def sign_jwt(claims: dict[str, object]) -> str:
    """用 RS256 + kid 签发 JWT。"""
    return jwt.encode(claims, _private_key, algorithm="RS256", headers={"kid": KID})
