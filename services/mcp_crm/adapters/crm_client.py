"""CRM 系统适配层：mock（开发期）与 httpx（真实 OpenAPI）双模式（ARCHITECTURE 4.2.1）。

读写分离铁律（PRD 8.2）：
- 读操作（客户匹配/360/跟单）走 CRM OpenAPI 查询；写操作（销售订单）必须走
  CRM 官方受认可路径（OpenAPI），禁止 DB 直写
- CRM_MODE=mock（默认开发期）：进程内假数据，写路径仅内存模拟
- CRM_MODE=http：Service Account + 「代理人」双标记（PRD 8.5.5）

业务规则（PRD 6.1.1）：
- 客户名称必须命中 CRM 客户库（模糊匹配列候选）
- 客户联系人：仅 1 个自动带出；多联系人必填（Agent 列候选）
- 单价默认最近一次成交价；付款方式默认客户付款条件；地址默认客户主数据
- 大额订单（合计 ≥ ¥5,000）审批流加销售总监节点
"""

import os
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

import httpx

# 单据类型 / 付款方式枚举（与 packages/protocol/tools/crm__*.json 对齐，CI 校验）
ORDER_TYPES = ("standard", "sample")
PAYMENT_TERMS = ("prepay", "net30", "net60")
PAYMENT_TERM_LABELS = {"prepay": "预付", "net30": "月结 30 天", "net60": "月结 60 天"}

# 大额订单阈值（PRD 6.1.1：金额 ≥ ¥5,000 审批流加销售总监节点）
LARGE_ORDER_THRESHOLD = Decimal(5000)

# 商品编码目录（mock 假数据；真实环境经 OpenAPI 读取）
SKU_CATALOG: dict[str, str] = {"SKU-A": "商品 A", "SKU-B": "商品 B", "SKU-C": "商品 C"}

# mock 客户库（对齐 PRD 6.1.1 对话流：'XX 公司' 模糊命中 3 条候选）
_MOCK_CUSTOMERS: list[dict[str, Any]] = [
    {
        "customer_id": "C-1001",
        "name": "XX 贸易（上海）有限公司",
        "contacts": ["张三"],
        "default_address": "上海市浦东新区 XX 路 88 号",
        "payment_term": "net30",
        "last_prices": {"SKU-A": 50.0},
        "credit_level": "A",
    },
    {
        "customer_id": "C-1002",
        "name": "XX 实业（苏州）有限公司",
        "contacts": ["李四", "王五"],
        "default_address": "苏州工业园区 XX 路 6 号",
        "payment_term": "net60",
        "last_prices": {"SKU-A": 48.5},
        "credit_level": "B",
    },
    {
        "customer_id": "C-1003",
        "name": "XX 进出口（深圳）有限公司",
        "contacts": ["赵六"],
        "default_address": "深圳市南山区 XX 大道 1 号",
        "payment_term": "prepay",
        "last_prices": {"SKU-B": 120.0},
        "credit_level": "A",
    },
    {
        "customer_id": "C-2001",
        "name": "华辰机械制造有限公司",
        "contacts": ["钱七", "孙八", "周九"],
        "default_address": "无锡市新吴区 XX 工业园 3 栋",
        "payment_term": "net30",
        "last_prices": {"SKU-C": 88.0},
        "credit_level": "B",
    },
]

# mock 订单簿：预置 2 笔历史订单（跟单查询数据源；真实环境经 OpenAPI 读取）
_MOCK_ORDERS: dict[str, dict[str, Any]] = {
    "SO20260901001": {
        "order_no": "SO20260901001",
        "customer_id": "C-1001",
        "customer_name": "XX 贸易（上海）有限公司",
        "order_type": "standard",
        "items": [{"sku": "SKU-A", "sku_name": "商品 A", "qty": 200, "price": 50.0, "amount": 10000.0}],
        "total_amount": 10000.0,
        "delivery_date": "2026-09-15",
        "payment_term": "net30",
        "status": "producing",
        "approval_flow": ["直属主管", "销售总监"],
        "progress": [
            {"node": "审批", "state": "done", "note": "2026-09-01 通过"},
            {"node": "生产备货", "state": "doing", "note": "预计 09-12 完成"},
            {"node": "发货", "state": "todo", "note": ""},
        ],
        "exceptions": [],
    },
    "SO20260820007": {
        "order_no": "SO20260820007",
        "customer_id": "C-1002",
        "customer_name": "XX 实业（苏州）有限公司",
        "order_type": "standard",
        "items": [{"sku": "SKU-C", "sku_name": "商品 C", "qty": 50, "price": 88.0, "amount": 4400.0}],
        "total_amount": 4400.0,
        "delivery_date": "2026-09-05",
        "payment_term": "net60",
        "status": "delayed",
        "approval_flow": ["直属主管"],
        "progress": [
            {"node": "审批", "state": "done", "note": "2026-08-20 通过"},
            {"node": "仓库备货", "state": "doing", "note": "SKU-C 缺货，采购在途"},
        ],
        "exceptions": ["库存缺货：SKU-C 在途采购预计 09-25 到货，交期将延后"],
    },
}


def _find_customer(customer_id: str) -> dict[str, Any] | None:
    return next((c for c in _MOCK_CUSTOMERS if c["customer_id"] == customer_id), None)


class CrmAdapter(Protocol):
    """CRM 适配器协议：mcp-crm 工具层唯一依赖（底层可替换，ARCHITECTURE 4.2.1）。"""

    async def search_customers(self, keyword: str, limit: int = 10) -> list[dict[str, Any]]: ...

    async def get_customer_360(self, customer_id: str) -> dict[str, Any]: ...

    async def submit_sales_order(
        self,
        user_id: str,
        order_type: str,
        customer_id: str,
        contact: str,
        address: str,
        items: list[dict[str, Any]],
        delivery_date: str,
        payment_term: str,
        idempotency_key: str,
    ) -> dict[str, Any]: ...

    async def query_order_progress(self, order_no: str) -> dict[str, Any]: ...


class MockCrmAdapter:
    """开发期 mock：进程内假数据（读 = 静态数据；写 = 内存模拟官方提交路径）。"""

    def __init__(self) -> None:
        self._customers = [dict(c) for c in _MOCK_CUSTOMERS]
        self._orders = {k: dict(v) for k, v in _MOCK_ORDERS.items()}
        self._next_seq = 100

    async def search_customers(self, keyword: str, limit: int = 10) -> list[dict[str, Any]]:
        kw = keyword.strip().lower()
        if not kw:
            return []
        hits = [c for c in self._customers if kw in c["name"].lower()]
        return [
            {
                "customer_id": c["customer_id"],
                "name": c["name"],
                "contact_count": len(c["contacts"]),
                "payment_term": c["payment_term"],
            }
            for c in hits[:limit]
        ]

    async def get_customer_360(self, customer_id: str) -> dict[str, Any]:
        customer = _find_customer(customer_id)
        if customer is None:
            raise ValueError(f"客户不存在：{customer_id}")
        orders = [o for o in self._orders.values() if o["customer_id"] == customer_id]
        return {
            "customer_id": customer["customer_id"],
            "name": customer["name"],
            "contacts": list(customer["contacts"]),
            "default_address": customer["default_address"],
            "payment_term": customer["payment_term"],
            "last_prices": dict(customer["last_prices"]),
            "credit_level": customer["credit_level"],
            "recent_orders": [
                {
                    "order_no": o["order_no"],
                    "status": o["status"],
                    "total_amount": o["total_amount"],
                    "delivery_date": o["delivery_date"],
                }
                for o in orders
            ],
        }

    async def submit_sales_order(
        self,
        user_id: str,
        order_type: str,
        customer_id: str,
        contact: str,
        address: str,
        items: list[dict[str, Any]],
        delivery_date: str,
        payment_term: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        # 走「官方认可路径」：调 CRM 提交接口（mock 即内存记账）
        customer = _find_customer(customer_id)
        if customer is None:
            raise ValueError(f"客户不存在：{customer_id}")
        for it in items:
            if it["sku"] not in SKU_CATALOG:
                raise ValueError(f"无效商品编码：{it['sku']}，可选值 {'/'.join(SKU_CATALOG)}")
        # 补齐品名（真实环境由 CRM 接口返回）
        items = [
            {**it, "sku_name": SKU_CATALOG[it["sku"]]} for it in items
        ]
        now = datetime.now()  # noqa: DTZ005  mock 单号用本地时间，刻意 naive
        order_no = f"SO{now:%Y%m%d}{self._next_seq:03d}"
        self._next_seq += 1
        total = sum(Decimal(str(it["amount"])) for it in items)
        # 大额订单审批流自动加销售总监节点（PRD 6.1.1）
        flow = (
            ["直属主管", "销售总监"]
            if total >= LARGE_ORDER_THRESHOLD
            else ["直属主管"]
        )
        self._orders[order_no] = {
            "order_no": order_no,
            "customer_id": customer_id,
            "customer_name": customer["name"],
            "order_type": order_type,
            "items": [dict(it) for it in items],
            "total_amount": float(total),
            "delivery_date": delivery_date,
            "payment_term": payment_term,
            "salesperson": user_id,
            "status": "approving",
            "approval_flow": flow,
            "progress": [
                {"node": "审批", "state": "doing", "note": " → ".join(flow)},
            ],
            "exceptions": [],
        }
        return {
            "doc_no": order_no,
            "customer_name": customer["name"],
            "total_amount": float(total),
            "approval_flow": flow,
            "status": "submitted",
            "message": "销售订单已录入 CRM 并提交审批",
        }

    async def query_order_progress(self, order_no: str) -> dict[str, Any]:
        order = self._orders.get(order_no)
        if order is None:
            raise ValueError(f"订单不存在：{order_no}")
        return {
            "order_no": order["order_no"],
            "customer_name": order["customer_name"],
            "status": order["status"],
            "items": [dict(it) for it in order["items"]],
            "total_amount": order["total_amount"],
            "delivery_date": order["delivery_date"],
            "progress": [dict(p) for p in order["progress"]],
            "exceptions": list(order["exceptions"]),
        }


class HttpCrmAdapter:
    """真实 CRM OpenAPI 客户端（httpx，对接期启用）。

    TODO（对接期）：
    - Service Account 认证头 + 「代理人」双标记（PRD 8.5.5）
    - 熔断：连续失败达到阈值即摘除（ARCHITECTURE 4.2 统一职责）
    - 重试：幂等 GET 可安全重试；订单写入不盲目重试（幂等键兜底）
    """

    def __init__(self, base_url: str | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url or os.environ.get("CRM_BASE_URL", "http://crm.example.internal/api")
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search_customers(self, keyword: str, limit: int = 10) -> list[dict[str, Any]]:
        resp = await self._client.get(
            "/customers/search", params={"keyword": keyword, "limit": limit}
        )
        resp.raise_for_status()
        items: list[dict[str, Any]] = resp.json()["items"]
        return items

    async def get_customer_360(self, customer_id: str) -> dict[str, Any]:
        resp = await self._client.get(f"/customers/{customer_id}/profile")
        resp.raise_for_status()
        profile: dict[str, Any] = resp.json()
        return profile

    async def submit_sales_order(
        self,
        user_id: str,
        order_type: str,
        customer_id: str,
        contact: str,
        address: str,
        items: list[dict[str, Any]],
        delivery_date: str,
        payment_term: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        resp = await self._client.post(
            "/sales-orders",
            json={
                "user_id": user_id,
                "order_type": order_type,
                "customer_id": customer_id,
                "contact": contact,
                "address": address,
                "items": items,
                "delivery_date": delivery_date,
                "payment_term": payment_term,
                "idempotency_key": idempotency_key,
            },
        )
        resp.raise_for_status()
        submitted: dict[str, Any] = resp.json()
        return submitted

    async def query_order_progress(self, order_no: str) -> dict[str, Any]:
        resp = await self._client.get(f"/sales-orders/{order_no}/progress")
        resp.raise_for_status()
        progress: dict[str, Any] = resp.json()
        return progress


_adapter: CrmAdapter | None = None


def get_adapter() -> CrmAdapter:
    """按 CRM_MODE 选择适配器（默认 mock，开发期零外部依赖）。

    模块级单例：customers/orders 工具模块必须共享同一 adapter，
    否则 mock 内存态（提交的订单、客户库）跨工具不可见。
    """
    global _adapter
    if _adapter is None:
        mode = os.environ.get("CRM_MODE", "mock")
        _adapter = HttpCrmAdapter() if mode == "http" else MockCrmAdapter()
    return _adapter


def calc_amount(qty: int, price: float) -> float:
    """金额 = 数量 × 单价（2 位小数，四舍五入；服务端重算不信任调用方）。"""
    return float((Decimal(str(qty)) * Decimal(str(price))).quantize(Decimal("0.01"), ROUND_HALF_UP))
