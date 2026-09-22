"""审批工具：oa__query_pending_approvals / oa__approve。

契约源：packages/protocol/tools/oa__query_pending_approvals.json
        packages/protocol/tools/oa__approve.json
（参数/枚举/必填与此保持一致，CI 校验）

写入规则（PRD 5.2 场景 1-2）：
- 同意/驳回为写入操作：桌面端行内二次确认 + HITL 后执行
- 幂等处理：同键重复请求返回原处理状态，不重复翻转单据状态
- 审计留痕：谁在何时以何身份处理（processed_by/processed_at）
"""

from typing import Any

from mcp.server.fastmcp import FastMCP

import idempotency
from adapters.oa_client import get_adapter

_DOC_TYPES = ("leave", "expense", "purchase")
_ACTIONS = ("approve", "reject")


def register(mcp: FastMCP) -> None:
    """注册审批相关工具。"""
    adapter = get_adapter()

    @mcp.tool()
    async def oa__query_pending_approvals(
        user_id: str, doc_type: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """查询当前用户的待办审批列表（只读）。

        Args:
            user_id: 员工工号
            doc_type: 可选，按单据类型过滤（leave/expense/purchase）
            limit: 返回条数（1-50，默认 10）
        """
        if doc_type is not None and doc_type not in _DOC_TYPES:
            raise ValueError(f"无效单据类型：{doc_type}，可选值 {'/'.join(_DOC_TYPES)}")
        if not 1 <= limit <= 50:
            raise ValueError("limit 取值范围 1-50")
        return await adapter.list_pending_approvals(user_id, doc_type, limit)

    @mcp.tool()
    async def oa__approve(
        approval_id: str,
        action: str,
        idempotency_key: str,
        comment: str = "",
    ) -> dict[str, Any]:
        """审批操作（同意/驳回；写入类：HITL 确认后执行；幂等）。

        Args:
            approval_id: 待办单据 ID（来自 oa__query_pending_approvals 返回）
            action: 审批动作（approve=同意/reject=驳回）
            idempotency_key: 幂等键 {userId}_{sessionId}_{intentHash}_{draftVersion}
            comment: 可选，审批意见（驳回时建议填写）
        """
        if action not in _ACTIONS:
            raise ValueError(f"无效审批动作：{action}，可选值 {'/'.join(_ACTIONS)}")
        if not idempotency_key:
            raise ValueError("幂等键缺失：写入类调用必须携带")

        # 1. 幂等检查：命中返回已处理状态（doc_no 字段复用存 approval_id）
        existing = await idempotency.lookup(idempotency_key)
        if existing is not None:
            return {
                "approval_id": existing,
                "status": "processed",
                "idempotent_reuse": True,
                "message": "幂等命中：该待办已处理，返回原处理结果",
            }

        # 2. 审批写入（operator 从 Service Account 上下文取，MVP 用幂等键前缀）
        result = await adapter.approve(
            approval_id=approval_id,
            action=action,
            comment=comment,
            operator_id=idempotency_key.split("_", 1)[0],
        )

        # 3. 登记幂等记录
        await idempotency.record(idempotency_key, approval_id)
        return result
