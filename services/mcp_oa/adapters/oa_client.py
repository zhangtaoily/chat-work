"""OA 系统适配层：mock（开发期）与 httpx（真实 OpenAPI）双模式（ARCHITECTURE 4.2.1）。

读写分离铁律（PRD 8.2）：
- 读操作走最快可用通道；写操作必须走 OA 官方受认可路径（OpenAPI/RPA），禁止 DB 直写
- OA_MODE=mock（默认开发期）：进程内假数据，写路径仅内存模拟
- OA_MODE=http：Service Account + 「代理人」双标记（PRD 8.5.5）
"""

import os
from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx

# 假期类型枚举（与 packages/protocol/tools/oa__*.json 对齐，CI 校验一致性）
LEAVE_TYPES = ("annual", "comp", "sick", "personal", "marriage", "bereavement", "maternity")

# 各类假默认额度（mock 假数据；真实环境经 OpenAPI 读取）
_MOCK_DEFAULT_BALANCES: dict[str, float] = {
    "annual": 5.0,
    "comp": 3.0,
    "sick": 10.0,
    "personal": 3.0,
    "marriage": 3.0,
    "bereavement": 3.0,
    "maternity": 90.0,
}

# 周报 mock 动作模板（按回溯天数循环取用，确定性输出；真实环境经 OpenAPI 读取）
_MOCK_ACTIVITY_TEMPLATE: list[tuple[str, str]] = [
    ("submit_leave", "提交年假申请（2 天，事由：家中有事）"),
    ("approve", "审批通过李四的调休申请（LV-2026-0913）"),
    ("query_balance", "查询年假余额"),
    ("query_bi", "查询华东区 2026-09 销售额"),
    ("submit_leave", "提交病假申请（0.5 天，事由：就医）"),
]


class OaAdapter(Protocol):
    """OA 适配器协议：mcp-oa 工具层唯一依赖（底层可替换，ARCHITECTURE 4.2.1）。"""

    async def get_leave_balance(
        self, user_id: str, leave_type: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def submit_leave(
        self,
        user_id: str,
        leave_type: str,
        start_time: datetime,
        end_time: datetime,
        duration_days: float,
        reason: str,
    ) -> dict[str, Any]: ...

    async def list_pending_approvals(
        self, user_id: str, doc_type: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]: ...

    async def approve(
        self, approval_id: str, action: str, comment: str, operator_id: str
    ) -> dict[str, Any]: ...

    async def list_activity_log(self, user_id: str, days: int = 7) -> list[dict[str, Any]]: ...


class MockOaAdapter:
    """开发期 mock：进程内假数据（读 = 静态数据；写 = 内存模拟官方提交路径）。"""

    def __init__(self) -> None:
        # user_id -> {leave_type -> remaining}
        self._balances: dict[str, dict[str, float]] = {}
        self._next_doc_seq = 1000
        # approval_id -> 审批单（含状态）
        self._approvals: dict[str, dict[str, Any]] = {
            "AP-2026-0001": {
                "approval_id": "AP-2026-0001",
                "doc_type": "leave",
                "doc_no": "LV-2026-0912",
                "title": "张三的年假申请（2 天）",
                "applicant": "张三",
                "applicant_id": "E1001",
                "submitted_at": "2026-09-12T10:00:00",
                "status": "pending",
                "summary": {"leave_type": "annual", "duration_days": 2.0, "reason": "家中有事"},
            },
            "AP-2026-0002": {
                "approval_id": "AP-2026-0002",
                "doc_type": "leave",
                "doc_no": "LV-2026-0913",
                "title": "李四的调休申请（1 天）",
                "applicant": "李四",
                "applicant_id": "E1002",
                "submitted_at": "2026-09-13T14:30:00",
                "status": "pending",
                "summary": {"leave_type": "comp", "duration_days": 1.0, "reason": "上周末加班调休"},
            },
            "AP-2026-0003": {
                "approval_id": "AP-2026-0003",
                "doc_type": "leave",
                "doc_no": "LV-2026-0914",
                "title": "王五的病假申请（0.5 天）",
                "applicant": "王五",
                "applicant_id": "E1003",
                "submitted_at": "2026-09-14T09:00:00",
                "status": "pending",
                "summary": {"leave_type": "sick", "duration_days": 0.5, "reason": "就医"},
            },
        }

    def _user_balances(self, user_id: str) -> dict[str, float]:
        if user_id not in self._balances:
            self._balances[user_id] = dict(_MOCK_DEFAULT_BALANCES)
        return self._balances[user_id]

    async def get_leave_balance(
        self, user_id: str, leave_type: str | None = None
    ) -> list[dict[str, Any]]:
        balances = self._user_balances(user_id)
        types = [leave_type] if leave_type else list(LEAVE_TYPES)
        return [
            {"user_id": user_id, "leave_type": t, "remaining_days": balances[t]}
            for t in types
            if t in balances
        ]

    async def submit_leave(
        self,
        user_id: str,
        leave_type: str,
        start_time: datetime,
        end_time: datetime,
        duration_days: float,
        reason: str,
    ) -> dict[str, Any]:
        # 走「官方认可路径」：调 OA 提交接口（mock 即内存记账）
        balances = self._user_balances(user_id)
        if leave_type in ("annual", "comp"):
            if duration_days > balances[leave_type]:
                raise ValueError(
                    f"余额不足：{leave_type} 剩余 {balances[leave_type]} 天，"
                    f"本次申请 {duration_days} 天"
                )
            balances[leave_type] -= duration_days
        self._next_doc_seq += 1
        doc_no = f"LV-2026-{self._next_doc_seq}"
        # 申请自动进入上级待办（< 3 天免部门总监，PRD 5.2）
        approval_id = f"AP-2026-{self._next_doc_seq}"
        self._approvals[approval_id] = {
            "approval_id": approval_id,
            "doc_type": "leave",
            "doc_no": doc_no,
            "title": f"{user_id} 的请假申请（{duration_days} 天）",
            "applicant_id": user_id,
            "submitted_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": "pending",
            "summary": {
                "leave_type": leave_type,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "duration_days": duration_days,
                "reason": reason,
            },
        }
        return {
            "doc_no": doc_no,
            "approval_id": approval_id,
            "status": "submitted",
            "message": "请假申请已提交，等待审批",
        }

    async def list_pending_approvals(
        self, user_id: str, doc_type: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        items = [a for a in self._approvals.values() if a["status"] == "pending"]
        if doc_type:
            items = [a for a in items if a["doc_type"] == doc_type]
        return items[:limit]

    async def approve(
        self, approval_id: str, action: str, comment: str, operator_id: str
    ) -> dict[str, Any]:
        approval = self._approvals.get(approval_id)
        if approval is None:
            raise ValueError(f"待办不存在：{approval_id}")
        if approval["status"] != "pending":
            return {
                "approval_id": approval_id,
                "status": approval["status"],
                "message": "该待办已被处理（幂等返回当前状态）",
            }
        approval["status"] = "approved" if action == "approve" else "rejected"
        approval["processed_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        approval["processed_by"] = operator_id
        approval["comment"] = comment
        return {
            "approval_id": approval_id,
            "doc_no": approval["doc_no"],
            "status": approval["status"],
            "message": "已同意" if action == "approve" else "已驳回",
        }

    async def list_activity_log(self, user_id: str, days: int = 7) -> list[dict[str, Any]]:
        # 确定性 mock：按回溯天数循环取模板，周末（周六/周日）不计工作记录
        now = datetime.now().astimezone()
        items: list[dict[str, Any]] = []
        seq = 0
        for offset in range(days):
            day = now - timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            action_type, summary = _MOCK_ACTIVITY_TEMPLATE[seq % len(_MOCK_ACTIVITY_TEMPLATE)]
            seq += 1
            occurred = day.replace(hour=9 + (seq % 8), minute=30, second=0, microsecond=0)
            items.append(
                {
                    "user_id": user_id,
                    "action_type": action_type,
                    "summary": summary,
                    "occurred_at": occurred.isoformat(timespec="seconds"),
                }
            )
        return items


class HttpOaAdapter:
    """真实 OA OpenAPI 客户端（httpx，Phase 1 对接期启用）。

    TODO（对接期）：
    - Service Account 认证头 + 「代理人」双标记（PRD 8.5.5）
    - 熔断：连续失败达到阈值即摘除（ARCHITECTURE 4.2 统一职责）
    - 重试：幂等 GET 可安全重试；写入不盲目重试（幂等键兜底）
    """

    def __init__(self, base_url: str | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url or os.environ.get("OA_BASE_URL", "http://oa.example.internal/api")
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_leave_balance(
        self, user_id: str, leave_type: str | None = None
    ) -> list[dict[str, Any]]:
        resp = await self._client.get(
            "/leave/balance", params={"user_id": user_id, "leave_type": leave_type}
        )
        resp.raise_for_status()
        items: list[dict[str, Any]] = resp.json()["items"]
        return items

    async def submit_leave(
        self,
        user_id: str,
        leave_type: str,
        start_time: datetime,
        end_time: datetime,
        duration_days: float,
        reason: str,
    ) -> dict[str, Any]:
        resp = await self._client.post(
            "/leave/requests",
            json={
                "user_id": user_id,
                "leave_type": leave_type,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "duration_days": duration_days,
                "reason": reason,
            },
        )
        resp.raise_for_status()
        submitted: dict[str, Any] = resp.json()
        return submitted

    async def list_pending_approvals(
        self, user_id: str, doc_type: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        resp = await self._client.get(
            "/approvals/pending",
            params={"user_id": user_id, "doc_type": doc_type, "limit": limit},
        )
        resp.raise_for_status()
        items: list[dict[str, Any]] = resp.json()["items"]
        return items

    async def approve(
        self, approval_id: str, action: str, comment: str, operator_id: str
    ) -> dict[str, Any]:
        resp = await self._client.post(
            f"/approvals/{approval_id}",
            json={"action": action, "comment": comment, "operator_id": operator_id},
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def list_activity_log(self, user_id: str, days: int = 7) -> list[dict[str, Any]]:
        resp = await self._client.get("/activity/log", params={"user_id": user_id, "days": days})
        resp.raise_for_status()
        items: list[dict[str, Any]] = resp.json()["items"]
        return items


_adapter: OaAdapter | None = None


def get_adapter() -> OaAdapter:
    """按 OA_MODE 选择适配器（默认 mock，开发期零外部依赖）。

    模块级单例：leave/approvals 等工具模块必须共享同一 adapter，
    否则 mock 内存态（提交的请假单、审批状态）跨工具不可见。
    """
    global _adapter
    if _adapter is None:
        mode = os.environ.get("OA_MODE", "mock")
        _adapter = HttpOaAdapter() if mode == "http" else MockOaAdapter()
    return _adapter
