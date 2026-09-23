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
            roles=["system_admin"],
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
