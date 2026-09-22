"""pytest 共享 fixture（tests 无 __init__.py，经 conftest 自动发现）。"""

from typing import Any

import pytest

from agent_core.api import auth as auth_mod


@pytest.fixture
def rsa_key(monkeypatch: pytest.MonkeyPatch) -> Any:
    """生成测试 RSA 密钥并注入 JWKS（替代远程 Broker）。"""
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_numbers()
    n = pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")
    e = pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big")
    import base64

    b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    jwks = {
        "keys": [
            {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": "test-kid", "n": b64u(n), "e": b64u(e)}
        ]
    }

    async def fake_fetch() -> dict[str, Any]:
        return jwks

    monkeypatch.setattr(auth_mod, "_fetch_jwks", fake_fetch)
    monkeypatch.setattr(auth_mod, "_jwks_cache", None)
    monkeypatch.setattr(auth_mod, "_jwks_cached_at", 0.0)
    monkeypatch.setattr(auth_mod, "_jti_blacklist", {})
    return key
