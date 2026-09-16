"""写入幂等记录（PG 表 idempotency_records，ARCHITECTURE 4.2/4.6 / PRD 8.7）。

幂等键 → 单据编号映射，保留 24h：
- agent-core 在 HITL 确认通过后生成幂等键
  ``{userId}_{sessionId}_{intentHash}_{draftVersion}``，随 tools/call 传递；
- mcp-oa 写入前先查幂等表：命中则直接返回已生成的单据编号（不重复下单）；
- 调用超时等未知态由调用方用同一幂等键重查结果（oa__get_idempotency_result 流程）。
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, MetaData, String, Table

metadata = MetaData()

# 幂等记录表（骨架 DDL 占位；24h 过期清理由定时任务负责）
idempotency_records = Table(
    "idempotency_records",
    metadata,
    Column("idempotency_key", String(128), primary_key=True, comment="幂等键"),
    Column("doc_no", String(64), nullable=False, comment="成功写入的单据编号"),
    Column(
        "created_at",
        DateTime(),
        nullable=False,
        default=datetime.now,
        comment="创建时间（保留 24h）",
    ),
)


async def lookup(idempotency_key: str) -> str | None:
    """按幂等键查询已写入的单据编号（未命中返回 None）。"""
    raise NotImplementedError


async def record(idempotency_key: str, doc_no: str) -> None:
    """登记幂等记录（幂等键 → 单据编号）。"""
    raise NotImplementedError
