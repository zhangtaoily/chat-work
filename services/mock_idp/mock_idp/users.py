"""HR 主数据种子（身份权威源，PRD 8.5.6）。

工号为全局唯一主键；登录时 IdP 身份必须能映射到工号，
无法匹配 → 拒绝登录（不自动创建账号，防影子账号）。
种子与 mcp_oa MockOaAdapter 的既有工号保持一致（E1001 张三 / E1002 李四 / E1003 王五）。

口令（仅内网 dev 用，生产切 Keycloak，密码学要求适度）：
- PBKDF2-SHA256（200_000 次）+ 每用户独立随机 salt，格式 pbkdf2$<salt_hex>$<hash_hex>
- 全部种子用户初始密码 = SEED_PASSWORD，模块加载时生成哈希（源码不落任何哈希值）
"""

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, replace

SEED_PASSWORD = "ChatWork@2026"  # 统一初始密码（内网 dev 种子，可经管理端点重置）
_PBKDF2_ITERATIONS = 200_000


@dataclass(frozen=True)
class HrUser:
    """HR 主数据条目（工号权威 + IdP 侧映射 + 本地口令）。"""

    emp_no: str  # 员工工号（sub）
    name: str
    dept: str  # 归属组织（展示用）
    roles: list[str]
    idp: str  # 来源 IdP：ad | wecom | dingtalk
    idp_sub: str  # IdP 侧原始 ID（如 AD UPN）
    enabled: bool = True  # 账号启停（管理端点切换，停用即拒绝登录）
    password_hash: str = ""  # pbkdf2$<salt_hex>$<hash_hex>（模块加载时惰性生成）
    password_set_at: float = 0.0  # 当前口令首次生成时间（Unix 秒）


def _digest_hex(password: str, salt_hex: str) -> str:
    """PBKDF2-SHA256 摘要（十六进制）。"""
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), _PBKDF2_ITERATIONS
    )
    return digest.hex()


def make_password_hash(password: str) -> str:
    """生成口令哈希：每用户独立随机 salt（secrets.token_hex(16)）。"""
    salt = secrets.token_hex(16)
    return f"pbkdf2${salt}${_digest_hex(password, salt)}"


def verify_password(user: HrUser, password: str) -> bool:
    """校验口令（常数时间比较）；哈希格式异常一律视为不匹配。"""
    try:
        algo, salt, expected = user.password_hash.split("$", 2)
    except ValueError:
        return False
    if algo != "pbkdf2":
        return False
    return hmac.compare_digest(_digest_hex(password, salt), expected)


# 每日全量同步 + 变更事件实时推送（PRD 8.5.6）；此处为静态种子
HR_USERS: dict[str, HrUser] = {
    u.emp_no: u
    for u in [
        HrUser(
            emp_no="E1001",
            name="张三",
            dept="事业部A/销售科",
            roles=["employee"],
            idp="ad",
            idp_sub="zhangsan@corp.com",
        ),
        HrUser(
            emp_no="E1002",
            name="李四",
            dept="事业部A/销售科",
            roles=["employee"],
            idp="ad",
            idp_sub="lisi@corp.com",
        ),
        HrUser(
            emp_no="E1003",
            name="王五",
            dept="事业部A/销售科",
            roles=["employee", "dept_manager"],
            idp="ad",
            idp_sub="wangwu@corp.com",
        ),
        # P2.2 科室用户（与 infra/keycloak/realm-export.json 种子对齐）
        HrUser(
            emp_no="E2001",
            name="郑拓",
            dept="事业部A/信息科",
            roles=["employee"],
            idp="ad",
            idp_sub="zhengtuo@corp.com",
        ),
        HrUser(
            emp_no="E2002",
            name="冯刚",
            dept="事业部A/生产科",
            roles=["employee"],
            idp="ad",
            idp_sub="fenggang@corp.com",
        ),
        HrUser(
            emp_no="E2003",
            name="许芹",
            dept="事业部A/计划科",
            roles=["employee"],
            idp="ad",
            idp_sub="xuqin@corp.com",
        ),
        HrUser(
            emp_no="E2004",
            name="贺平",
            dept="事业部A/品管科",
            roles=["employee"],
            idp="ad",
            idp_sub="heping@corp.com",
        ),
        HrUser(
            emp_no="E2005",
            name="鲁仓",
            dept="事业部A/仓库物流",
            roles=["employee"],
            idp="ad",
            idp_sub="lucang@corp.com",
        ),
        # P2.7 管理角色用户（PRD 5.6.1 矩阵；双评审员甲/乙支撑双人复核体验）
        HrUser(
            emp_no="E8001",
            name="系统管理员",
            dept="信息科",
            # org_admin：管理后台「数字分身账号」工作台的管理角色（P0-3）
            roles=["system_admin", "org_admin"],
            idp="ad",
            idp_sub="sysadmin@corp.com",
        ),
        HrUser(
            emp_no="E8002",
            name="安全评审员甲",
            dept="信息科",
            roles=["security_reviewer"],
            idp="ad",
            idp_sub="secrev-a@corp.com",
        ),
        HrUser(
            emp_no="E8003",
            name="知识管理员",
            dept="信息科",
            roles=["knowledge_manager"],
            idp="ad",
            idp_sub="knowmgr@corp.com",
        ),
        HrUser(
            emp_no="E8004",
            name="审计员",
            dept="信息科",
            roles=["auditor"],
            idp="ad",
            idp_sub="auditor@corp.com",
        ),
        HrUser(
            emp_no="E8005",
            name="安全评审员乙",
            dept="信息科",
            roles=["security_reviewer"],
            idp="ad",
            idp_sub="secrev-b@corp.com",
        ),
    ]
}


def find_by_emp_no(emp_no: str) -> HrUser | None:
    """按工号查 HR 主数据；未匹配返回 None（调用方必须拒绝登录）。"""
    return HR_USERS.get(emp_no.strip().upper())


def _seed_initial_passwords() -> None:
    """模块加载时为全部种子用户生成初始口令哈希（不把哈希写死在源码）。"""
    seeded_at = time.time()
    for emp_no, user in HR_USERS.items():
        salt = secrets.token_hex(16)
        HR_USERS[emp_no] = replace(
            user,
            password_hash=f"pbkdf2${salt}${_digest_hex(SEED_PASSWORD, salt)}",
            password_set_at=seeded_at,
        )


_seed_initial_passwords()


def reset_password(emp_no: str, new_password: str) -> HrUser | None:
    """重置口令（内网管理端点用）；工号不存在返回 None。"""
    user = HR_USERS.get(emp_no.strip().upper())
    if user is None:
        return None
    updated = replace(
        user,
        password_hash=make_password_hash(new_password),
        password_set_at=time.time(),
    )
    HR_USERS[user.emp_no] = updated
    return updated


def set_enabled(emp_no: str, enabled: bool) -> HrUser | None:
    """启用/停用账号（内网管理端点用）；工号不存在返回 None。"""
    user = HR_USERS.get(emp_no.strip().upper())
    if user is None:
        return None
    updated = replace(user, enabled=enabled)
    HR_USERS[user.emp_no] = updated
    return updated
