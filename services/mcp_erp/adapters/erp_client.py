"""ERP 系统适配层：mock（开发期）与 httpx（真实 OpenAPI）双模式（ARCHITECTURE 4.2.1）。

只读语义（PRD 6.1）：
- ERP 优先场景（库存查询/采购订单/财务凭证摘要）Phase 2 全部只读，
  无写入幂等需求；写路径（采购下单/凭证生成）留待后续经官方 OpenAPI 实现
- ERP_MODE=mock（默认开发期）：进程内假数据
- ERP_MODE=http：Service Account + 「代理人」双标记（PRD 8.5.5）

mock 数据与 mcp-crm 呼应：CRM 跟单异常「SKU-C 缺货」对应
ERP 库存 SKU-C 低于安全库存 + 采购在途 PO20260910003。
"""

import os
from decimal import Decimal
from typing import Any, Protocol

import httpx

# 采购订单状态枚举（与 packages/protocol/tools/erp__*.json 对齐，CI 校验）
PO_STATUSES = ("draft", "approved", "in_transit", "received")

# 商品目录（与 mcp-crm SKU_CATALOG 同源，mock 假数据；真实环境经 OpenAPI 读取）
SKU_CATALOG: dict[str, str] = {"SKU-A": "商品 A", "SKU-B": "商品 B", "SKU-C": "商品 C"}

# mock 库存（跨仓行；available = on_hand - 冻结，inbound_transit = 采购在途）
_MOCK_INVENTORY: list[dict[str, Any]] = [
    {"sku": "SKU-A", "warehouse": "总仓", "on_hand": 320, "available": 300, "inbound_transit": 0, "safety_stock": 100},
    {"sku": "SKU-A", "warehouse": "苏州仓", "on_hand": 80, "available": 80, "inbound_transit": 0, "safety_stock": 50},
    {"sku": "SKU-B", "warehouse": "总仓", "on_hand": 150, "available": 150, "inbound_transit": 0, "safety_stock": 80},
    {"sku": "SKU-C", "warehouse": "总仓", "on_hand": 5, "available": 5, "inbound_transit": 500, "safety_stock": 50},
    {"sku": "SKU-C", "warehouse": "苏州仓", "on_hand": 0, "available": 0, "inbound_transit": 0, "safety_stock": 30},
]

# mock 采购订单（与 CRM 跟单异常呼应：SKU-C 在途 500 件，ETA 09-25）
_MOCK_PURCHASE_ORDERS: list[dict[str, Any]] = [
    {
        "po_no": "PO20260910003",
        "supplier": "华东机械供应有限公司",
        "sku": "SKU-C",
        "sku_name": "商品 C",
        "qty": 500,
        "amount": 44000.0,
        "eta": "2026-09-25",
        "status": "in_transit",
    },
    {
        "po_no": "PO20260905011",
        "supplier": "华南五金贸易有限公司",
        "sku": "SKU-B",
        "sku_name": "商品 B",
        "qty": 200,
        "amount": 24000.0,
        "eta": "2026-09-08",
        "status": "received",
    },
]

# mock 财务凭证摘要（按月汇总；借贷必平衡，会计恒等式）
_MOCK_VOUCHER_SUMMARIES: dict[str, dict[str, Any]] = {
    "2026-09": {
        "period": "2026-09",
        "voucher_count": 128,
        "by_subject": [
            {"subject": "银行存款", "debit": 1200000.0, "credit": 800000.0},
            {"subject": "应收账款", "debit": 600000.0, "credit": 350000.0},
            {"subject": "应付账款", "debit": 420000.0, "credit": 500000.0},
            {"subject": "主营业务收入", "debit": 0.0, "credit": 650000.0},
            {"subject": "主营业务成本", "debit": 480000.0, "credit": 0.0},
            {"subject": "应交税费", "debit": 100000.0, "credit": 200000.0},
            {"subject": "其他应收款", "debit": 0.0, "credit": 300000.0},
        ],
    },
    "2026-08": {
        "period": "2026-08",
        "voucher_count": 96,
        "by_subject": [
            {"subject": "银行存款", "debit": 900000.0, "credit": 750000.0},
            {"subject": "应收账款", "debit": 200000.0, "credit": 100000.0},
            {"subject": "主营业务收入", "debit": 0.0, "credit": 250000.0},
        ],
    },
}


def _with_summary_totals(row: dict[str, Any]) -> dict[str, Any]:
    """汇总借/贷合计（服务端重算，不信任存储值）。"""
    debit = sum(Decimal(str(s["debit"])) for s in row["by_subject"])
    credit = sum(Decimal(str(s["credit"])) for s in row["by_subject"])
    return {
        **row,
        "debit_total": float(debit),
        "credit_total": float(credit),
        "balanced": debit == credit,
    }


class ErpAdapter(Protocol):
    """ERP 适配器协议：mcp-erp 工具层唯一依赖（底层可替换，ARCHITECTURE 4.2.1）。"""

    async def query_inventory(
        self, sku: str | None, keyword: str | None, below_safety: bool, limit: int
    ) -> list[dict[str, Any]]: ...

    async def query_purchase_orders(
        self, sku: str | None, status: str | None, limit: int
    ) -> list[dict[str, Any]]: ...

    async def query_voucher_summary(self, period: str) -> dict[str, Any]: ...


class MockErpAdapter:
    """开发期 mock：进程内假数据（全部只读，无写入路径）。"""

    async def query_inventory(
        self, sku: str | None, keyword: str | None, below_safety: bool, limit: int
    ) -> list[dict[str, Any]]:
        rows = _MOCK_INVENTORY
        if sku:
            rows = [r for r in rows if r["sku"] == sku.strip().upper()]
        if keyword:
            kw = keyword.strip().lower()
            rows = [r for r in rows if kw in r["warehouse"].lower() or kw in SKU_CATALOG.get(r["sku"], "").lower()]
        result = [
            {**r, "sku_name": SKU_CATALOG[r["sku"]], "below_safety": r["available"] < r["safety_stock"]}
            for r in rows
        ]
        if below_safety:
            result = [r for r in result if r["below_safety"]]
        return result[:limit]

    async def query_purchase_orders(
        self, sku: str | None, status: str | None, limit: int
    ) -> list[dict[str, Any]]:
        rows = _MOCK_PURCHASE_ORDERS
        if sku:
            rows = [r for r in rows if r["sku"] == sku.strip().upper()]
        if status:
            rows = [r for r in rows if r["status"] == status]
        return [dict(r) for r in rows[:limit]]

    async def query_voucher_summary(self, period: str) -> dict[str, Any]:
        row = _MOCK_VOUCHER_SUMMARIES.get(period.strip())
        if row is None:
            available = "、".join(sorted(_MOCK_VOUCHER_SUMMARIES))
            raise ValueError(f"无 {period} 期间凭证摘要，已有期间：{available}")
        return _with_summary_totals(dict(row))


class HttpErpAdapter:
    """真实 ERP OpenAPI 客户端（httpx，对接期启用）。

    TODO（对接期）：
    - Service Account 认证头 + 「代理人」双标记（PRD 8.5.5）
    - 熔断：连续失败达到阈值即摘除（ARCHITECTURE 4.2 统一职责）
    - 只读 GET 可安全重试；缓存 5 分钟（热点查询，PRD R6 同 mcp-bi 策略）
    """

    def __init__(self, base_url: str | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url or os.environ.get("ERP_BASE_URL", "http://erp.example.internal/api")
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def query_inventory(
        self, sku: str | None, keyword: str | None, below_safety: bool, limit: int
    ) -> list[dict[str, Any]]:
        resp = await self._client.get(
            "/inventory",
            params={"sku": sku, "keyword": keyword, "below_safety": str(below_safety).lower(), "limit": limit},
        )
        resp.raise_for_status()
        items: list[dict[str, Any]] = resp.json()["items"]
        return items

    async def query_purchase_orders(
        self, sku: str | None, status: str | None, limit: int
    ) -> list[dict[str, Any]]:
        resp = await self._client.get(
            "/purchase-orders", params={"sku": sku, "status": status, "limit": limit}
        )
        resp.raise_for_status()
        items: list[dict[str, Any]] = resp.json()["items"]
        return items

    async def query_voucher_summary(self, period: str) -> dict[str, Any]:
        resp = await self._client.get("/vouchers/summary", params={"period": period})
        resp.raise_for_status()
        summary: dict[str, Any] = resp.json()
        return summary


_adapter: ErpAdapter | None = None


def get_adapter() -> ErpAdapter:
    """按 ERP_MODE 选择适配器（默认 mock，开发期零外部依赖）。"""
    global _adapter
    if _adapter is None:
        mode = os.environ.get("ERP_MODE", "mock")
        _adapter = HttpErpAdapter() if mode == "http" else MockErpAdapter()
    return _adapter
