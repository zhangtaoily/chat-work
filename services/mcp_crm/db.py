"""异步数据库引擎工厂（双库兼容，ARCHITECTURE 4.6）。

- 生产：DATABASE_URL=mysql+aiomysql://...（MySQL 8 优先）或 postgresql+psycopg://...
- 开发冒烟：缺省 sqlite+aiosqlite:///:memory:（零外部依赖起服务）
- 全部经 SQLAlchemy ORM 访问，禁止手写方言 SQL

惰性初始化：首次使用时在当前事件循环创建 engine 并建表，
避免跨 loop 复用连接（FastMCP streamable-http 自管 event loop）。
"""

import os
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[Any] | None = None


def _database_url() -> str:
    """DATABASE_URL 优先；未配置时回退 SQLite 内存（开发期冒烟）。"""
    return os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///:memory:")


async def get_engine() -> AsyncEngine:
    """获取（惰性创建）全局异步引擎。"""
    global _engine
    if _engine is None:
        _engine = create_async_engine(_database_url(), pool_pre_ping=True)
    return _engine


async def get_session_factory() -> async_sessionmaker[Any]:
    """获取（惰性创建）会话工厂。"""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(await get_engine(), expire_on_commit=False)
    return _session_factory
