"""MES 系统适配层：mock（开发期）与 httpx（真实接口）双模式（ARCHITECTURE 4.2.1）。

只读语义（PRD 7.1）：
- MES 场景为「生产报工查询」：工单进度 + 报工明细，全部只读，
  无写入幂等需求（报工写入仍由 MES 原生终端完成，Agent 侧不代写）
- MES_MODE=mock（默认开发期）：进程内假数据（PRD 7.1 文件交换场景：
  定时轮询 MES 导出文件 → 解析缓存，真实环境经该路径刷新数据）
- MES_MODE=http：Service Account 调 MES 接口 + 「代理人」双标记（PRD 8.5.5）

mock 数据与 mcp-erp 呼应：SKU-C 缺料（库存 5 < 安全库存 50，在途
PO20260910003 ETA 09-25）→ 对应新工单 MO20260901001 未开工（等料）。
"""

import os
from typing import Any, Protocol

import httpx

# 工单状态枚举（与 packages/protocol/tools/mes__*.json 对齐，CI 校验）
WO_STATUSES = ("pending", "running", "done", "closed")

# mock 工单（生产进度：计划量/完工量/良品/不良；SKU-C 新单等料未开工）
_MOCK_WORK_ORDERS: list[dict[str, Any]] = [
    {
        "work_order": "MO20260901001",
        "sku": "SKU-C",
        "sku_name": "商品 C",
        "plan_qty": 500,
        "completed_qty": 0,
        "good_qty": 0,
        "ng_qty": 0,
        "status": "pending",
        "workstation": "总装一线",
        "plan_start": "2026-09-26",
        "plan_end": "2026-09-30",
    },
    {
        "work_order": "MO20260902002",
        "sku": "SKU-A",
        "sku_name": "商品 A",
        "plan_qty": 300,
        "completed_qty": 260,
        "good_qty": 252,
        "ng_qty": 8,
        "status": "running",
        "workstation": "总装二线",
        "plan_start": "2026-09-18",
        "plan_end": "2026-09-24",
    },
    {
        "work_order": "MO20260901003",
        "sku": "SKU-B",
        "sku_name": "商品 B",
        "plan_qty": 200,
        "completed_qty": 200,
        "good_qty": 196,
        "ng_qty": 4,
        "status": "done",
        "workstation": "总装一线",
        "plan_start": "2026-09-10",
        "plan_end": "2026-09-16",
    },
    {
        "work_order": "MO20260801004",
        "sku": "SKU-A",
        "sku_name": "商品 A",
        "plan_qty": 400,
        "completed_qty": 400,
        "good_qty": 392,
        "ng_qty": 8,
        "status": "closed",
        "workstation": "总装二线",
        "plan_start": "2026-08-20",
        "plan_end": "2026-08-28",
    },
]

# mock 报工记录（按工单分桶；操作工/工时/良品/不良，生产报工明细口径）
_MOCK_PRODUCTION_REPORTS: dict[str, list[dict[str, Any]]] = {
    "MO20260902002": [
        {
            "report_no": "PR20260920001",
            "work_order": "MO20260902002",
            "operator": "王强",
            "report_time": "2026-09-20 16:30",
            "good_qty": 80,
            "ng_qty": 4,
            "work_hours": 8.0,
        },
        {
            "report_no": "PR20260921001",
            "work_order": "MO20260902002",
            "operator": "李敏",
            "report_time": "2026-09-21 16:30",
            "good_qty": 72,
            "ng_qty": 2,
            "work_hours": 8.0,
        },
    ],
    "MO20260901003": [
        {
            "report_no": "PR20260915002",
            "work_order": "MO20260901003",
            "operator": "赵芳",
            "report_time": "2026-09-15 16:30",
            "good_qty": 100,
            "ng_qty": 2,
            "work_hours": 8.0,
        },
        {
            "report_no": "PR20260916001",
            "work_order": "MO20260901003",
            "operator": "王强",
            "report_time": "2026-09-16 16:30",
            "good_qty": 96,
            "ng_qty": 2,
            "work_hours": 8.0,
        },
    ],
}


class MesAdapter(Protocol):
    """MES 适配器协议：mcp-mes 工具层唯一依赖（底层可替换，ARCHITECTURE 4.2.1）。"""

    async def query_work_orders(
        self, sku: str | None, status: str | None, limit: int
    ) -> list[dict[str, Any]]: ...

    async def query_production_reports(
        self, work_order: str, limit: int
    ) -> list[dict[str, Any]]: ...


class MockMesAdapter:
    """开发期 mock：进程内假数据（全部只读，无写入路径）。"""

    async def query_work_orders(
        self, sku: str | None, status: str | None, limit: int
    ) -> list[dict[str, Any]]:
        rows = _MOCK_WORK_ORDERS
        if sku:
            rows = [r for r in rows if r["sku"] == sku.strip().upper()]
        if status:
            rows = [r for r in rows if r["status"] == status]
        return [dict(r) for r in rows[:limit]]

    async def query_production_reports(
        self, work_order: str, limit: int
    ) -> list[dict[str, Any]]:
        rows = _MOCK_PRODUCTION_REPORTS.get(work_order.strip().upper())
        if rows is None:
            known = "、".join(sorted(_MOCK_PRODUCTION_REPORTS))
            raise ValueError(f"工单 {work_order} 无报工记录（已有报工工单：{known}）")
        return [dict(r) for r in rows[:limit]]


class HttpMesAdapter:
    """真实 MES 接口客户端（httpx，对接期启用）。

    TODO（对接期）：
    - 文件交换兜底（PRD 7.1/8.2）：定时轮询 MES 导出文件 → 解析 → 内存缓存
    - Service Account 认证头 + 「代理人」双标记（PRD 8.5.5）
    - 熔断：连续失败达到阈值即摘除（ARCHITECTURE 4.2 统一职责）
    - 只读 GET 可安全重试；缓存 5 分钟（热点查询，PRD R6 同 mcp-erp 策略）
    """

    def __init__(self, base_url: str | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url or os.environ.get("MES_BASE_URL", "http://mes.example.internal/api")
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def query_work_orders(
        self, sku: str | None, status: str | None, limit: int
    ) -> list[dict[str, Any]]:
        resp = await self._client.get(
            "/work-orders", params={"sku": sku, "status": status, "limit": limit}
        )
        resp.raise_for_status()
        items: list[dict[str, Any]] = resp.json()["items"]
        return items

    async def query_production_reports(
        self, work_order: str, limit: int
    ) -> list[dict[str, Any]]:
        resp = await self._client.get(
            "/production-reports", params={"work_order": work_order, "limit": limit}
        )
        resp.raise_for_status()
        items: list[dict[str, Any]] = resp.json()["items"]
        return items


_adapter: MesAdapter | None = None


def get_adapter() -> MesAdapter:
    """按 MES_MODE 选择适配器（默认 mock，开发期零外部依赖）。"""
    global _adapter
    if _adapter is None:
        mode = os.environ.get("MES_MODE", "mock")
        _adapter = HttpMesAdapter() if mode == "http" else MockMesAdapter()
    return _adapter
