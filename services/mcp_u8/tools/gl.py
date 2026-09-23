"""财务总账查询工具：u8__query_gl_balance / u8__query_voucher_detail（只读，PRD 7.1）。

契约源：packages/protocol/tools/u8__query_gl_balance.json
        packages/protocol/tools/u8__query_voucher_detail.json
（参数/枚举/必填与此保持一致，CI 校验）

只读辅助查询：科目余额（期初/本期/期末）与凭证明细（借贷分录 +
平衡校验）；严禁写入（PRD 8.2：U8 只读场景 SQL 封装 read-only）。
"""

import re
from typing import Any

from mcp.server.fastmcp import FastMCP

from adapters.u8_client import get_adapter

_PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def register(mcp: FastMCP) -> None:
    """注册 U8 科目余额与凭证明细查询工具。"""
    adapter = get_adapter()

    @mcp.tool()
    async def u8__query_gl_balance(
        period: str, subject: str | None = None
    ) -> dict[str, Any]:
        """查询指定期间科目余额表（只读）：期初/本期借/本期贷/期末 + 本期合计。

        Args:
            period: 会计期间，格式 YYYY-MM（如 2026-09）
            subject: 科目名称关键词过滤（如「银行」「应收」），缺省查全部科目
        """
        if not _PERIOD_PATTERN.match(period.strip()):
            raise ValueError(f"期间格式不合法（应为 YYYY-MM）：{period}")
        return await adapter.query_gl_balance(period.strip(), subject)

    @mcp.tool()
    async def u8__query_voucher_detail(voucher_no: str) -> dict[str, Any]:
        """查询指定凭证的借贷分录明细（只读）：摘要/科目/方向/金额 + 平衡校验。

        Args:
            voucher_no: 凭证号（如 记-2026090128），必填
        """
        if not voucher_no.strip():
            raise ValueError("voucher_no 不能为空")
        return await adapter.query_voucher_detail(voucher_no.strip())
