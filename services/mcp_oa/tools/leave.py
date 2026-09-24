"""请假工具：oa__query_leave_balance / oa__submit_leave_request（MVP 唯一写入表单）。

契约源：packages/protocol/tools/oa__query_leave_balance.json
        packages/protocol/tools/oa__submit_leave_request.json
（参数/枚举/必填与此保持一致，CI 校验）

校验规则（PRD 5.2 场景 1-1）：
- 年假/调休提交前必须查余额，时长不得超过剩余额度
- 开始时间 ≥ 当前时间；结束时间 > 开始时间
- 时长 0.5 天粒度；事由必填（Agent 追问获得，无默认值）
"""

import logging
from datetime import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP

import idempotency
from adapters.oa_client import LEAVE_TYPES, get_adapter

logger = logging.getLogger(__name__)


def _parse_iso(value: str, field: str) -> datetime:
    """解析 ISO 8601；naive 时间按本地时区补全（业务输入为本地时间口径）。"""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} 不是合法的 ISO 8601 时间：{value}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt


def _validate_duration(duration_days: float) -> float:
    if duration_days <= 0:
        raise ValueError("时长必须大于 0")
    if abs(duration_days * 2 - round(duration_days * 2)) > 1e-9:
        raise ValueError("时长必须为 0.5 天的整数倍")
    return float(duration_days)


def register(mcp: FastMCP) -> None:
    """注册请假相关工具。"""
    adapter = get_adapter()

    @mcp.tool()
    async def oa__query_leave_balance(
        user_id: str, leave_type: str | None = None
    ) -> list[dict[str, Any]]:
        """查询当前用户各类假期的剩余额度（只读；年假/调休提交前必须先查余额）。

        Args:
            user_id: 员工工号
            leave_type: 可选，限定查询类型（annual/comp/sick/personal/
                marriage/bereavement/maternity）；缺省返回全部
        """
        if leave_type is not None and leave_type not in LEAVE_TYPES:
            raise ValueError(f"无效假期类型：{leave_type}，可选值 {'/'.join(LEAVE_TYPES)}")
        return await adapter.get_leave_balance(user_id, leave_type)

    @mcp.tool()
    async def oa__submit_leave_request(
        user_id: str,
        leave_type: str,
        start_time: str,
        end_time: str,
        duration_days: float,
        reason: str,
        idempotency_key: str,
        tx_reason: int | None = None,
    ) -> dict[str, Any]:
        """提交请假申请单（写入类：HITL 确认后执行；幂等提交）。

        Args:
            user_id: 员工工号
            leave_type: 假期类型（annual=年假/comp=调休/sick=病假/personal=事假/
                marriage=婚假/bereavement=丧假/maternity=产假）
            start_time: 开始时间，ISO 8601
            end_time: 结束时间，ISO 8601
            duration_days: 时长（天），0.5 粒度，工作日口径
            reason: 请假事由（必填）
            idempotency_key: 幂等键 {userId}_{sessionId}_{intentHash}_{draftVersion}
            tx_reason: 可选，调休时长来源（0=加班/1=旅游/2=其他）；缺省服务端默认
        """
        logger.info(
            "oa__submit_leave_request 入参：user_id=%s leave_type=%s start=%s end=%s "
            "days=%s tx_reason=%s idem=%s reason=%r",
            user_id, leave_type, start_time, end_time, duration_days,
            tx_reason, idempotency_key, reason,
        )
        if leave_type not in LEAVE_TYPES:
            raise ValueError(f"无效假期类型：{leave_type}，可选值 {'/'.join(LEAVE_TYPES)}")
        if not reason or not reason.strip():
            raise ValueError("请假事由必填（不提供默认值）")
        if tx_reason is not None and tx_reason not in (0, 1, 2):
            raise ValueError("无效调休时长来源：可选值 0=加班/1=旅游/2=其他")
        if not idempotency_key:
            raise ValueError("幂等键缺失：写入类调用必须携带")

        # 1. 幂等检查：命中直接返回原单据编号，不重复提交
        existing = await idempotency.lookup(idempotency_key)
        if existing is not None:
            return {
                "doc_no": existing,
                "status": "submitted",
                "idempotent_reuse": True,
                "message": "幂等命中：返回原单据编号，未重复提交",
            }

        # 2. 时间与时长校验
        start = _parse_iso(start_time, "start_time")
        end = _parse_iso(end_time, "end_time")
        if start < datetime.now(start.tzinfo):
            raise ValueError("开始时间不得早于当前时间")
        if end <= start:
            raise ValueError("结束时间必须晚于开始时间")
        duration = _validate_duration(duration_days)

        # 3. 走官方受认可提交路径（mock / OpenAPI，禁 DB 直写）
        result = await adapter.submit_leave(
            user_id=user_id,
            leave_type=leave_type,
            start_time=start,
            end_time=end,
            duration_days=duration,
            reason=reason.strip(),
            tx_reason=tx_reason,
        )

        # 4. 登记幂等记录（失败重试靠调用方以同键重查）
        await idempotency.record(idempotency_key, result["doc_no"])
        logger.info("oa__submit_leave_request 成功：idem=%s -> %s", idempotency_key, result)
        return result
