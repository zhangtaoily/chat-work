"""企微群机器人 webhook 推送（PLAN P2.6 p2-6e，PRD 14 章）。

automation._deliver 与 workflow._notify 共用的出站通道：
WECOM_WEBHOOK_URL 已配置时推企微群机器人（markdown 消息）；未配置或
推送失败返回 False，调用方降级进程内信箱（零依赖默认值，通知不丢）。
"""

import httpx

from agent_core.config import settings

_TIMEOUT_S = 5  # 对齐 auth.py JWKS 拉取超时


async def push_markdown(text: str) -> bool:
    """推送 markdown 消息到企微群机器人。返回 True=已推送（调用方免落信箱）。

    企微 markdown 消息上限 4096 字节，超限截断；网络失败/业务错误码
    （errcode≠0）一律返回 False 由调用方降级信箱，本模块不抛异常、
    不阻断自动化/编排主流程。
    """
    url = settings.wecom_webhook_url
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(
                url,
                json={"msgtype": "markdown", "markdown": {"content": (text or "")[:4000]}},
            )
            return bool(resp.json().get("errcode") == 0)
    except Exception:  # noqa: BLE001 - 降级信箱兜底：任何推送失败不阻断自动化/编排主流程
        return False
