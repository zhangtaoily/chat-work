"""BI 查询工具：bi__execute_query（只读）。

契约源：packages/protocol/tools/bi__execute_query.json（_meta.rw: read）。
"""

from typing import Any

from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """注册 BI 查询工具（骨架占位）。"""

    @mcp.tool()
    async def bi__execute_query(
        query: str, dimensions: list[str] | None = None
    ) -> dict[str, Any]:
        """自然语言 BI 数据查询（只读：仅 SELECT 语义，禁止任何写操作）。

        TODO: 自然语言 → BI 查询语义（维度/指标/筛选），经 BI OpenAPI 执行。
        """
        raise NotImplementedError
