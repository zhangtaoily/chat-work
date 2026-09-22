"""HR 主数据种子（身份权威源，PRD 8.5.6）。

工号为全局唯一主键；登录时 IdP 身份必须能映射到工号，
无法匹配 → 拒绝登录（不自动创建账号，防影子账号）。
种子与 mcp_oa MockOaAdapter 的既有工号保持一致（E1001 张三 / E1002 李四 / E1003 王五）。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class HrUser:
    """HR 主数据条目（工号权威 + IdP 侧映射）。"""

    emp_no: str  # 员工工号（sub）
    name: str
    dept: str  # 归属组织（展示用）
    roles: list[str]
    idp: str  # 来源 IdP：ad | wecom | dingtalk
    idp_sub: str  # IdP 侧原始 ID（如 AD UPN）


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
    ]
}


def find_by_emp_no(emp_no: str) -> HrUser | None:
    """按工号查 HR 主数据；未匹配返回 None（调用方必须拒绝登录）。"""
    return HR_USERS.get(emp_no.strip().upper())
