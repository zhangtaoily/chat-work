"""销售订单工具：crm__submit_sales_order / crm__query_order_progress（PRD 6.1.1）。

契约源：packages/protocol/tools/crm__submit_sales_order.json
        packages/protocol/tools/crm__query_order_progress.json
（参数/枚举/必填与此保持一致，CI 校验）

规则（PRD 6.1.1）：
- 客户/联系人/地址/单价/付款方式默认值由 Agent 侧填充，服务端兜底校验
- 联系人必须是该客户在册联系人（多联系人必问，单联系人自动带出）
- 交货日期无默认值必问；金额服务端按 数量×单价 重算（不信任调用方）
- 大额订单（合计 ≥ ¥5,000）审批流加销售总监节点
- 写入幂等：幂等键命中直接返回原单据编号（24h，ARCHITECTURE 4.2）
"""

from datetime import date, datetime
from typing import Any

from mcp.server.fastmcp import FastMCP

import idempotency
from adapters.crm_client import ORDER_TYPES, PAYMENT_TERMS, calc_amount, get_adapter


def _normalize_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """明细行结构校验 + 金额服务端重算（2 位小数，四舍五入）。"""
    if not items:
        raise ValueError("订单明细不能为空（至少 1 行商品）")
    normalized: list[dict[str, Any]] = []
    for idx, it in enumerate(items, start=1):
        if not isinstance(it, dict) or not it.get("sku") or it.get("qty") is None or it.get("price") is None:
            raise ValueError(f"第 {idx} 行明细缺少 sku / qty / price")
        qty, price = int(it["qty"]), float(it["price"])
        if qty <= 0:
            raise ValueError(f"第 {idx} 行数量必须大于 0")
        if price <= 0:
            raise ValueError(f"第 {idx} 行单价必须大于 0")
        normalized.append(
            {"sku": it["sku"], "qty": qty, "price": price, "amount": calc_amount(qty, price)}
        )
    return normalized


def _parse_delivery_date(value: str) -> str:
    """交货日期校验：YYYY-MM-DD，且不得早于今天（PRD：无默认值必问）。"""
    try:
        d = datetime.strptime(value, "%Y-%m-%d").date()  # noqa: DTZ007  仅取 date 部分
    except ValueError as exc:
        raise ValueError(f"交货日期格式不合法（应为 YYYY-MM-DD）：{value}") from exc
    if d < date.today():  # noqa: DTZ011  业务口径为本地日历日
        raise ValueError(f"交货日期不得早于今天：{value}")
    return value


async def submit_sales_order(
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
    """销售订单录入（写入类全链路：幂等 → 校验 → 官方路径 → 登记）。独立函数便于直测。"""
    if not user_id.strip():
        raise ValueError("user_id 必填（销售员工号，写入操作人标记）")
    if order_type not in ORDER_TYPES:
        raise ValueError(f"无效单据类型：{order_type}，可选值 {'/'.join(ORDER_TYPES)}")
    if payment_term not in PAYMENT_TERMS:
        raise ValueError(f"无效付款方式：{payment_term}，可选值 {'/'.join(PAYMENT_TERMS)}")
    if not idempotency_key:
        raise ValueError("幂等键缺失：写入类调用必须携带")

    # 1. 幂等检查：命中直接返回原单据编号，不重复下单
    existing = await idempotency.lookup(idempotency_key)
    if existing is not None:
        return {
            "doc_no": existing,
            "status": "submitted",
            "idempotent_reuse": True,
            "message": "幂等命中：返回原单据编号，未重复提交",
        }

    # 2. 表单校验（Agent 侧默认值链之外的兜底）
    if not contact.strip():
        raise ValueError("联系人必填：单联系人自动带出，多联系人须向用户确认")
    if not address.strip():
        raise ValueError("收货地址必填（默认取客户主数据地址）")
    _parse_delivery_date(delivery_date)
    normalized = _normalize_items(items)

    # 3. 联系人必须在客户在册联系人中（不合法时列候选）
    adapter = get_adapter()
    profile = await adapter.get_customer_360(customer_id)
    if contact.strip() not in profile["contacts"]:
        raise ValueError(
            f"联系人「{contact}」不在客户 {profile['name']} 的在册联系人中，"
            f"候选：{'、'.join(profile['contacts'])}"
        )

    # 4. 走官方受认可提交路径（mock / OpenAPI，禁 DB 直写）
    result = await adapter.submit_sales_order(
        user_id=user_id.strip(),
        order_type=order_type,
        customer_id=customer_id,
        contact=contact.strip(),
        address=address.strip(),
        items=normalized,
        delivery_date=delivery_date,
        payment_term=payment_term,
        idempotency_key=idempotency_key,
    )

    # 5. 登记幂等记录（失败重试靠调用方以同键重查）
    await idempotency.record(idempotency_key, result["doc_no"])
    return result


def register(mcp: FastMCP) -> None:
    """注册销售订单工具。"""

    @mcp.tool()
    async def crm__submit_sales_order(
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
        """提交销售订单（写入类：HITL 确认后执行；幂等提交；大额自动加销售总监审批）。

        Args:
            user_id: 销售员工号（写入操作人）
            order_type: 单据类型（standard=标准 / sample=样品）
            customer_id: 客户编码（来自 crm__search_customers 命中结果）
            contact: 联系人（须为该客户在册联系人）
            address: 收货地址（默认取客户主数据地址）
            items: 明细行列表 [{sku, qty, price}]，金额服务端重算
            delivery_date: 交货日期 YYYY-MM-DD（无默认值必问）
            payment_term: 付款方式（prepay=预付 / net30=月结 30 天 / net60=月结 60 天）
            idempotency_key: 幂等键 {userId}_{sessionId}_{intentHash}_{draftVersion}
        """
        return await submit_sales_order(
            user_id=user_id,
            order_type=order_type,
            customer_id=customer_id,
            contact=contact,
            address=address,
            items=items,
            delivery_date=delivery_date,
            payment_term=payment_term,
            idempotency_key=idempotency_key,
        )

    @mcp.tool()
    async def crm__query_order_progress(order_no: str) -> dict[str, Any]:
        """按订单号查询跟单进度：状态 / 节点推进 / 异常（只读）。

        Args:
            order_no: 销售订单号（如 SO20260901001）
        """
        if not order_no.strip():
            raise ValueError("订单号必填")
        return await get_adapter().query_order_progress(order_no.strip())
