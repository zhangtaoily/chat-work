"""U8 财务系统适配层：mock（开发期）与 httpx（真实接口）双模式（ARCHITECTURE 4.2.1）。

只读语义（PRD 7.1 / 8.2）：
- U8 场景为「财务总账辅助查询」：科目余额表 + 凭证明细，全部只读；
  JDBC 直连只读场景严禁写入（PRD 8.2：SQL 查询封装为 read-only tools）
- U8_MODE=mock（默认开发期）：进程内假数据
- U8_MODE=http：Service Account 调 U8 接口 + 「代理人」双标记（PRD 8.5.5）

mock 数据与 mcp-erp 凭证摘要呼应：2026-09 各科目本期借/贷发生额同口径
（银行存款 借 120 万/贷 150 万等），余额表为其期初+本期滚存的细视图。
"""

import os
from decimal import Decimal
from typing import Any, Protocol

import httpx

# 科目余额方向枚举（与 packages/protocol/tools/u8__*.json 对齐，CI 校验）
GL_DIRECTIONS = ("debit", "credit")

# mock 科目余额表（按期间分桶；期初/本期借/本期贷/期末，U8 总账口径）
_MOCK_GL_BALANCES: dict[str, dict[str, Any]] = {
    "2026-09": {
        "period": "2026-09",
        "rows": [
            {
                "subject": "银行存款",
                "direction": "debit",
                "opening": 800000.0,
                "debit": 1200000.0,
                "credit": 1500000.0,
                "closing": 500000.0,
            },
            {
                "subject": "应收账款",
                "direction": "debit",
                "opening": 350000.0,
                "debit": 600000.0,
                "credit": 300000.0,
                "closing": 650000.0,
            },
            {
                "subject": "应付账款",
                "direction": "credit",
                "opening": 420000.0,
                "debit": 300000.0,
                "credit": 380000.0,
                "closing": 500000.0,
            },
            {
                "subject": "主营业务收入",
                "direction": "credit",
                "opening": 0.0,
                "debit": 0.0,
                "credit": 650000.0,
                "closing": 650000.0,
            },
            {
                "subject": "主营业务成本",
                "direction": "debit",
                "opening": 0.0,
                "debit": 480000.0,
                "credit": 0.0,
                "closing": 480000.0,
            },
            {
                "subject": "应交税费",
                "direction": "credit",
                "opening": 200000.0,
                "debit": 100000.0,
                "credit": 100000.0,
                "closing": 200000.0,
            },
        ],
    },
    "2026-08": {
        "period": "2026-08",
        "rows": [
            {
                "subject": "银行存款",
                "direction": "debit",
                "opening": 650000.0,
                "debit": 900000.0,
                "credit": 750000.0,
                "closing": 800000.0,
            },
            {
                "subject": "应收账款",
                "direction": "debit",
                "opening": 250000.0,
                "debit": 200000.0,
                "credit": 100000.0,
                "closing": 350000.0,
            },
            {
                "subject": "主营业务收入",
                "direction": "credit",
                "opening": 0.0,
                "debit": 0.0,
                "credit": 250000.0,
                "closing": 250000.0,
            },
        ],
    },
}

# mock 凭证明细（借贷分录；本期借/贷发生额与余额表同口径）
_MOCK_VOUCHER_DETAILS: dict[str, dict[str, Any]] = {
    "记-2026090128": {
        "voucher_no": "记-2026090128",
        "voucher_date": "2026-09-05",
        "summary": "收到客户货款（XX 贸易）",
        "entries": [
            {"subject": "银行存款", "direction": "debit", "amount": 300000.0},
            {"subject": "应收账款", "direction": "credit", "amount": 300000.0},
        ],
    },
    "记-2026090136": {
        "voucher_no": "记-2026090136",
        "voucher_date": "2026-09-12",
        "summary": "采购原材料入账（华东机械）",
        "entries": [
            {"subject": "应付账款", "direction": "debit", "amount": 44000.0},
            {"subject": "银行存款", "direction": "credit", "amount": 44000.0},
        ],
    },
}


def _with_balance_totals(row: dict[str, Any]) -> dict[str, Any]:
    """余额表汇总：本期借/贷合计（服务端重算，不信任存储值）。"""
    debit = sum(Decimal(str(r["debit"])) for r in row["rows"])
    credit = sum(Decimal(str(r["credit"])) for r in row["rows"])
    return {**row, "debit_total": float(debit), "credit_total": float(credit)}


def _with_voucher_balance(detail: dict[str, Any]) -> dict[str, Any]:
    """凭证明细：借贷平衡校验（会计恒等式：借方合计 = 贷方合计）。"""
    debit = sum(Decimal(str(e["amount"])) for e in detail["entries"] if e["direction"] == "debit")
    credit = sum(Decimal(str(e["amount"])) for e in detail["entries"] if e["direction"] == "credit")
    return {**detail, "debit_total": float(debit), "credit_total": float(credit), "balanced": debit == credit}


class U8Adapter(Protocol):
    """U8 适配器协议：mcp-u8 工具层唯一依赖（底层可替换，ARCHITECTURE 4.2.1）。"""

    async def query_gl_balance(self, period: str, subject: str | None) -> dict[str, Any]: ...

    async def query_voucher_detail(self, voucher_no: str) -> dict[str, Any]: ...


class MockU8Adapter:
    """开发期 mock：进程内假数据（全部只读，无写入路径）。"""

    async def query_gl_balance(self, period: str, subject: str | None) -> dict[str, Any]:
        row = _MOCK_GL_BALANCES.get(period.strip())
        if row is None:
            available = "、".join(sorted(_MOCK_GL_BALANCES))
            raise ValueError(f"无 {period} 期间科目余额，已有期间：{available}")
        result = dict(row)
        if subject:
            kw = subject.strip()
            result["rows"] = [r for r in result["rows"] if kw in r["subject"]]
        return _with_balance_totals(result)

    async def query_voucher_detail(self, voucher_no: str) -> dict[str, Any]:
        detail = _MOCK_VOUCHER_DETAILS.get(voucher_no.strip())
        if detail is None:
            known = "、".join(sorted(_MOCK_VOUCHER_DETAILS))
            raise ValueError(f"凭证 {voucher_no} 不存在，已有凭证：{known}")
        return _with_voucher_balance(dict(detail))


class HttpU8Adapter:
    """真实 U8 接口客户端（httpx，对接期启用）。

    TODO（对接期）：
    - JDBC/只读视图兜底（PRD 8.2）：U8 无 OpenAPI 时经只读账号直连查询，
      SQL 封装为 read-only 查询，严禁写入
    - Service Account 认证头 + 「代理人」双标记（PRD 8.5.5）
    - 熔断：连续失败达到阈值即摘除（ARCHITECTURE 4.2 统一职责）
    - 只读 GET 可安全重试；缓存 5 分钟（热点查询，PRD R6 同 mcp-erp 策略）
    """

    def __init__(self, base_url: str | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url or os.environ.get("U8_BASE_URL", "http://u8.example.internal/api")
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def query_gl_balance(self, period: str, subject: str | None) -> dict[str, Any]:
        resp = await self._client.get(
            "/gl/balance", params={"period": period, "subject": subject}
        )
        resp.raise_for_status()
        balance: dict[str, Any] = resp.json()
        return balance

    async def query_voucher_detail(self, voucher_no: str) -> dict[str, Any]:
        resp = await self._client.get("/vouchers/detail", params={"voucher_no": voucher_no})
        resp.raise_for_status()
        detail: dict[str, Any] = resp.json()
        return detail


_adapter: U8Adapter | None = None


def get_adapter() -> U8Adapter:
    """按 U8_MODE 选择适配器（默认 mock，开发期零外部依赖）。"""
    global _adapter
    if _adapter is None:
        mode = os.environ.get("U8_MODE", "mock")
        _adapter = HttpU8Adapter() if mode == "http" else MockU8Adapter()
    return _adapter
