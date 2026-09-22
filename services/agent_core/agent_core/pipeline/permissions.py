"""权限面（PLAN P1.1，PRD 2.3 五维权限矩阵的 MVP 落地）。

原则（PRD 8.5）：权限不进 Token——JWT 只带 roles/perm_ver，本模块把
身份上下文映射为可执行范围：

- 技能级（操作维）：registry.required_roles any-of，仅约束写路径
  （读/查询放行；数据可见性由"本人范围"保证——工具入参 user_id
  恒取 JWT sub，无法代他人操作）
- 数据级·业务维：BI 区域白名单，与组织维度独立、二者"与"关系
  （PRD 2.3）；MVP 静态配置，生产置换为 HR 主数据/权限中心
- 数据级·组织维（本人→科室→部门→集团，PRD 2.3）："本人"级由工具
  入参 user_id 恒取 JWT sub 保证；P2.2 实装第二级"本科室"——
  科室工作台技能带 dept_scope 元数据，auth.dept 尾段匹配才放行
  （跨科室隔离）；行级"本科室全部数据"依赖单据系统 dept 字段过滤，
  留 P3 MES 接入时在服务侧统一实装
- auth 缺省（本地冒烟无 token）→ 放行，保持既有链路不变
"""

from typing import Any

# 业务维数据范围：工号 → 可见区域白名单（缺省与 mcp_bi 服务侧
# 过滤口径一致：华东、华南；USER_REGIONS 覆盖项留给区域授权场景）
DEFAULT_REGIONS: tuple[str, ...] = ("华东", "华南")
USER_REGIONS: dict[str, tuple[str, ...]] = {}

# 查询文本中可识别的区域词（命中白名单外区域在 Agent 侧前置拦截）
_KNOWN_REGIONS = ("华东", "华南", "华北", "华中", "西南", "东北", "西北")


def visible_regions(user_id: str) -> tuple[str, ...]:
    """用户可见区域白名单（业务维数据范围）。"""
    return USER_REGIONS.get(user_id, DEFAULT_REGIONS)


def check_skill_roles(
    auth: dict[str, Any] | None, skill: dict[str, Any]
) -> str | None:
    """技能级校验：required_roles any-of；返回拒绝文案，None = 放行。

    调用方（permission 节点）仅在写路径调用；auth 为 None（本地冒烟
    无 token）放行。文案遵循 PRD 阶段5 要求：友好提示、不暴露权限细节。
    """
    if auth is None:
        return None
    required = skill.get("required_roles") or []
    if not required:
        return None
    roles = set(auth.get("roles") or [])
    if roles & set(required):
        return None
    if required == ["dept_manager"]:
        return "该操作需要审批权限，请联系主管处理。"
    return f"该操作需要 {'、'.join(required)} 权限，请联系管理员开通。"


def check_region_scope(
    auth: dict[str, Any] | None, query: str
) -> str | None:
    """数据级·业务维校验：查询命中白名单外区域 → 拒绝文案。"""
    if auth is None:
        return None
    allowed = visible_regions(str(auth.get("user_id") or ""))
    for region in _KNOWN_REGIONS:
        if region in query and region not in allowed:
            return (
                f"暂无「{region}」区域的数据权限（当前可见：{'、'.join(allowed)}），"
                "请联系管理员开通。"
            )
    return None


def check_dept_scope(
    auth: dict[str, Any] | None, skill: dict[str, Any]
) -> str | None:
    """数据级·组织维校验（P2.2「本科室」级，PRD 2.3）。

    科室工作台技能带 dept_scope（科室名，如「生产科」）：auth.dept
    尾段匹配即放行（「事业部A/生产科」→「生产科」，本科室全员可查
    科室只读视图）；跨科室 → 拒绝文案。auth 缺省放行（本地冒烟）。
    """
    scope = skill.get("dept_scope")
    if not scope:
        return None
    if auth is None:
        return None
    dept = str(auth.get("dept") or "")
    if dept.split("/")[-1] == scope:
        return None
    return f"暂无「{scope}」的数据权限（当前科室：{dept or '未设置'}），请联系管理员开通。"
