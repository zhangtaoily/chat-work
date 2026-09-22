"""写入幂等记录（表 idempotency_records，ARCHITECTURE 4.2/4.6 / PRD 8.7）。

幂等键 → 单据编号映射，保留 24h：
- agent-core 在 HITL 确认通过后生成幂等键
  ``{userId}_{sessionId}_{intentHash}_{draftVersion}``，随 tools/call 传递；
- mcp-oa 写入前先查幂等表：命中则直接返回已生成的单据编号（不重复下单）；
- 调用超时等未知态由调用方用同一幂等键重查结果。

存储经 SQLAlchemy ORM（MySQL 8 优先 / PG 兼容 / 开发期 SQLite 内存），
类型映射遵循双库兼容原则：主键 String(128)，时间 DateTime（不依赖方言专属类型）。
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, MetaData, String, Table, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from db import get_engine

metadata = MetaData()

# 幂等记录表（24h 过期清理由定时任务负责）
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

_engine_ready = False


async def _ensure_tables(engine: AsyncEngine) -> None:
    """惰性建表（当前事件循环内执行，避免跨 loop 连接）。"""
    global _engine_ready
    if not _engine_ready:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        _engine_ready = True


async def lookup(idempotency_key: str) -> str | None:
    """按幂等键查询已写入的单据编号（未命中返回 None）。"""
    engine = await get_engine()
    await _ensure_tables(engine)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(idempotency_records.c.doc_no).where(
                    idempotency_records.c.idempotency_key == idempotency_key
                )
            )
        ).first()
    return row[0] if row else None


async def record(idempotency_key: str, doc_no: str) -> None:
    """登记幂等记录（幂等键 → 单据编号）。

    主键冲突（IntegrityError）静默保留原记录——三方言（MySQL/PG/SQLite）
    通用行为，不使用 INSERT IGNORE 等方言专属语法。
    """
    engine = await get_engine()
    await _ensure_tables(engine)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                idempotency_records.insert().values(idempotency_key=idempotency_key, doc_no=doc_no)
            )
    except IntegrityError:
        # 并发同键重复提交：首个写入已生效，本事务自动回滚
        pass
