"""财务凭证摘要工具：erp__query_voucher_summary（只读，PRD 6.1）。

契约源：packages/protocol/tools/erp__query_voucher_summary.json
（参数/枚举/必填与此保持一致，CI 校验）

借贷平衡由服务端重算并断言（会计恒等式：借方合计 = 贷方合计）。
"""

import re
from typing import Any

from mcp.server.fastmcp import FastMCP

from adapters.erp_client import get_adapter

_PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def register(mcp: FastMCP) -> None:
    """注册财务凭证摘要查询工具。"""
    adapter = get_adapter()

    @mcp.tool()
    async def erp__query_voucher_summary(period: str) -> dict[str, Any]:
        """查询指定月份财务凭证摘要（只读）：凭证张数 / 按科目借贷汇总 / 借贷平衡校验。

        Args:
            period: 会计期间，格式 YYYY-MM（如 2026-09）
        """
        if not _PERIOD_PATTERN.match(period.strip()):
            raise ValueError(f"期间格式不合法（应为 YYYY-MM）：{period}")
        return await adapter.query_voucher_summary(period.strip())
