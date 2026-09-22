"""灰度门禁（PLAN P1.4，PRD 5.5.6 版本协商 + 小流量放量）。

- 稳定分桶：sha256(user_id) 取模 100，同一用户恒定命中同一桶，
  避免放量期间同一用户会话时通时断；
  GRAY_PERCENT 控制放量：未设/0=门禁关闭（全量放行，本地冒烟兼容），
  10=仅 0-9 桶放行（约 10% 用户），100=全量。
- 版本协商：客户端带 X-Client-Version 请求头，低于 MIN_CLIENT_VERSION
  返回 client_version_too_low（PRD 5.5.6 强制升级协议：出现安全漏洞
  版本时网关阻断）；未设 MIN_CLIENT_VERSION 不启用版本协商。
"""

import hashlib
import os

BUCKETS = 100


def gray_percent() -> int:
    """放量百分比（请求时读 env，便于运维热调与测试注入；钳制 0-100）。"""
    raw = os.environ.get("GRAY_PERCENT", "").strip()
    if not raw:
        return 0
    try:
        return max(0, min(100, int(raw)))
    except ValueError:
        return 0


def bucket_of(user_id: str) -> int:
    """用户稳定分桶：sha256 前 8 位十六进制 % 100。"""
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % BUCKETS


def is_gray_user(user_id: str) -> bool:
    """是否放行：未设/0=门禁关闭全量放行；N=仅前 N 桶放行；100=全量。"""
    pct = gray_percent()
    if pct <= 0:
        return True
    return bucket_of(user_id) < pct


def min_client_version() -> str:
    """最低支持版本（空串=未启用版本协商）。"""
    return os.environ.get("MIN_CLIENT_VERSION", "").strip()


def version_tuple(version: str) -> tuple[int, ...]:
    """'v0.2.1-beta.3' → (0, 2, 1, 3)：剥离前缀/非数字后缀逐段取数。"""
    parts: list[int] = []
    for seg in version.strip().lstrip("vV").split("."):
        digits = ""
        for ch in seg:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def version_lt(a: str, b: str) -> bool:
    """a < b（短版本号补零逐段比较：0.2 < 0.2.1）。"""
    la, lb = version_tuple(a), version_tuple(b)
    n = max(len(la), len(lb))
    la += (0,) * (n - len(la))
    lb += (0,) * (n - len(lb))
    return la < lb


def client_version_allowed(header_value: str | None) -> bool:
    """版本协商：未启用最低版本 → 放行；未带头 → 视为过旧阻断
    （无法判定版本即按强制升级口径处理，PRD 5.5.6）。"""
    minimum = min_client_version()
    if not minimum:
        return True
    if not header_value:
        return False
    return not version_lt(header_value, minimum)
