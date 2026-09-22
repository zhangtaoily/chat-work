"""WMS 仓储系统适配层：mock（开发期）与 httpx（真实 OpenAPI）双模式（ARCHITECTURE 4.2.1）。

只读语义（PRD 6.1）：
- WMS 优先场景（入库/出库单查询、库存预警）Phase 2 全部只读，
  无写入幂等需求；出入库执行仍由 WMS 自有作业流程负责
- WMS_MODE=mock（默认开发期）：进程内假数据
- WMS_MODE=http：Service Account + 「代理人」双标记（PRD 8.5.5）

mock 数据与 mcp-crm/mcp-erp 呼应：
- OUT20260820008（SKU-C 50 件，blocked 缺货）对应 CRM 订单 SO20260820007 异常
- IN20260905012（SKU-B 200 件入库）对应 ERP 采购单 PO20260905011 已入库
"""

import os
from datetime import date, timedelta
from typing import Any, Protocol

import httpx

# 出入库单类型枚举（与 packages/protocol/tools/wms__*.json 对齐，CI 校验）
ORDER_TYPES = ("inbound", "outbound")

# 商品目录（与 mcp-crm/mcp-erp 同源，mock 假数据；真实环境经 OpenAPI 读取）
SKU_CATALOG: dict[str, str] = {"SKU-A": "商品 A", "SKU-B": "商品 B", "SKU-C": "商品 C"}

# mock 出入库单（doc_date 倒序展示；状态：pending/done/blocked）
_MOCK_STOCK_ORDERS: list[dict[str, Any]] = [
    {
        "doc_no": "IN20260905012",
        "order_type": "inbound",
        "sku": "SKU-B",
        "qty": 200,
        "partner": "华南五金贸易有限公司",
        "ref_no": "PO20260905011",
        "doc_date": "2026-09-05",
        "status": "done",
    },
    {
        "doc_no": "OUT20260901002",
        "order_type": "outbound",
        "sku": "SKU-A",
        "qty": 200,
        "partner": "XX 贸易（上海）有限公司",
        "ref_no": "SO20260901001",
        "doc_date": "2026-09-01",
        "status": "pending",  # 拣货中，待发货
    },
    {
        "doc_no": "IN20260828004",
        "order_type": "inbound",
        "sku": "SKU-A",
        "qty": 300,
        "partner": "北方原材料有限公司",
        "ref_no": "PO20260820019",
        "doc_date": "2026-08-28",
        "status": "done",
    },
    {
        "doc_no": "OUT20260820008",
        "order_type": "outbound",
        "sku": "SKU-C",
        "qty": 50,
        "partner": "XX 实业（苏州）有限公司",
        "ref_no": "SO20260820007",
        "doc_date": "2026-08-20",
        "status": "blocked",  # 缺货阻塞（与 CRM 跟单异常一致）
    },
]

# mock 库存预警（低库存 + 效期两类）
_MOCK_ALERTS: list[dict[str, Any]] = [
    {
        "alert_type": "low_stock",
        "sku": "SKU-C",
        "warehouse": "总仓",
        "detail": "可用量 5 低于安全库存 50，采购在途 500 件（ETA 2026-09-25）",
        "suggested_action": "优先保障 SO20260820007 补货，或与客户协商交期",
    },
    {
        "alert_type": "low_stock",
        "sku": "SKU-C",
        "warehouse": "苏州仓",
        "detail": "可用量 0 低于安全库存 30",
        "suggested_action": "到货后按总仓调拨计划分配",
    },
    {
        "alert_type": "expiry",
        "sku": "SKU-B",
        "warehouse": "总仓",
        "detail": "批次 B20260130 共 80 件，效期至 2026-10-15（30 天内到期）",
        "suggested_action": "优先出库临期批次（FEFO）",
    },
]


class WmsAdapter(Protocol):
    """WMS 适配器协议：mcp-wms 工具层唯一依赖（底层可替换，ARCHITECTURE 4.2.1）。"""

    async def query_stock_orders(
        self, order_type: str, keyword: str | None, days: int, limit: int
    ) -> list[dict[str, Any]]: ...

    async def query_stock_alerts(self, sku: str | None) -> list[dict[str, Any]]: ...


class MockWmsAdapter:
    """开发期 mock：进程内假数据（全部只读，无写入路径）。"""

    async def query_stock_orders(
        self, order_type: str, keyword: str | None, days: int, limit: int
    ) -> list[dict[str, Any]]:
        since = date.today() - timedelta(days=days)  # noqa: DTZ011  业务口径为本地日历日
        rows = [r for r in _MOCK_STOCK_ORDERS if r["order_type"] == order_type]
        rows = [r for r in rows if date.fromisoformat(r["doc_date"]) >= since]
        if keyword:
            kw = keyword.strip().lower()
            rows = [
                r
                for r in rows
                if kw in r["partner"].lower()
                or kw in r["sku"].lower()
                or kw in r["doc_no"].lower()
                or kw in SKU_CATALOG.get(r["sku"], "").lower()
            ]
        rows = sorted(rows, key=lambda r: r["doc_date"], reverse=True)
        return [{**r, "sku_name": SKU_CATALOG[r["sku"]]} for r in rows[:limit]]

    async def query_stock_alerts(self, sku: str | None) -> list[dict[str, Any]]:
        rows = _MOCK_ALERTS
        if sku:
            rows = [r for r in rows if r["sku"] == sku.strip().upper()]
        return [dict(r) for r in rows]


class HttpWmsAdapter:
    """真实 WMS OpenAPI 客户端（httpx，对接期启用）。

    TODO（对接期）：
    - Service Account 认证头 + 「代理人」双标记（PRD 8.5.5）
    - 熔断：连续失败达到阈值即摘除（ARCHITECTURE 4.2 统一职责）
    - 只读 GET 可安全重试；预警查询缓存 5 分钟
    """

    def __init__(self, base_url: str | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url or os.environ.get("WMS_BASE_URL", "http://wms.example.internal/api")
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def query_stock_orders(
        self, order_type: str, keyword: str | None, days: int, limit: int
    ) -> list[dict[str, Any]]:
        resp = await self._client.get(
            f"/stock-orders/{order_type}",
            params={"keyword": keyword, "days": days, "limit": limit},
        )
        resp.raise_for_status()
        items: list[dict[str, Any]] = resp.json()["items"]
        return items

    async def query_stock_alerts(self, sku: str | None) -> list[dict[str, Any]]:
        resp = await self._client.get("/stock-alerts", params={"sku": sku})
        resp.raise_for_status()
        items: list[dict[str, Any]] = resp.json()["items"]
        return items


_adapter: WmsAdapter | None = None


def get_adapter() -> WmsAdapter:
    """按 WMS_MODE 选择适配器（默认 mock，开发期零外部依赖）。"""
    global _adapter
    if _adapter is None:
        mode = os.environ.get("WMS_MODE", "mock")
        _adapter = HttpWmsAdapter() if mode == "http" else MockWmsAdapter()
    return _adapter
