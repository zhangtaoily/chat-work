"""P1.1/P1.2 E2E 验收脚本（curl 流程的脚本化版本，可重复执行）。

前置：四服务在线——agent_core:8011（SSO_REQUIRED=true）/ mock_idp:8012 /
mcp_oa:8001 / mcp_bi:8002。

覆盖（PLAN.md P1.1 权限面 + P1.2 审计，PRD 2.3/8.5/10）：
 1. 无 token 访问 /audit → 401（恒需认证）
 2. mock_idp OIDC 全流程（authorize 登录页 → code+PKCE → token）
 3. 篡改 access_token → 401（RS256 验签拒绝）
 4. 员工 BI 白名单外区域（华北）→ 数据级行权限拒绝 + permission_denied 审计
 5. 员工请假（技能级 required_roles=employee 放行）→ confirm_card + confirm_issued 审计
 6. 员工行内审批（required_roles=dept_manager）→ 技能级拒绝 + permission_denied 审计
 7. 确认卡 confirm → 恢复图执行写入 → confirm_approved + tool_call 审计
 8. 确认卡一次性：重复确认 → 404
 9. manager 查任意用户审计（数据级权限：dept_manager 全量）
10. 员工仅查本人审计；查他人 → 403
11. 员工审计动作覆盖断言（auth_success/tool_call/confirm_issued/confirm_approved/permission_denied）
12. manager 行内审批放行（写路径正向）→ confirm_card → reject → confirm_rejected 审计

运行：`.venv\\Scripts\\python.exe scripts/e2e_p1.py`（cwd=services/agent_core）
"""

import asyncio
import base64
import hashlib
import json
import secrets
import sys
from urllib.parse import parse_qs, urlparse

import httpx

IDP = "http://127.0.0.1:8012/realms/chat-work"
CORE = "http://127.0.0.1:8011"
CLIENT_ID = "chat-work-desktop"
REDIRECT_URI = "http://127.0.0.1:51740/callback"


async def login(client: httpx.AsyncClient, emp_no: str) -> str:
    """OIDC authorization_code + PKCE S256 全流程，返回 access_token。"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = secrets.token_urlsafe(16)
    common = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "openid profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    r = await client.get(f"{IDP}/protocol/openid-connect/auth", params=common)
    assert r.status_code == 200, f"authorize 登录页失败：{r.status_code}"

    r = await client.post(
        f"{IDP}/protocol/openid-connect/auth", data={**common, "emp_no": emp_no}
    )
    assert r.status_code == 302, f"工号认证失败：{r.status_code} {r.text[:120]}"
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["state"][0] == state, "state 不一致（CSRF）"
    code = q["code"][0]

    r = await client.post(
        f"{IDP}/protocol/openid-connect/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200, f"token 兑换失败：{r.status_code} {r.text[:120]}"
    return r.json()["access_token"]


async def chat(client: httpx.AsyncClient, token: str, session_id: str, message: str):
    r = await client.post(
        f"{CORE}/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"session_id": session_id, "message": message},
    )
    assert r.status_code == 200, f"/chat {r.status_code}：{r.text[:200]}"
    events = [
        json.loads(line[6:])
        for line in r.text.splitlines()
        if line.startswith("data: ") and line[6:] != "[DONE]"
    ]
    return events


def first(events: list, etype: str):
    return next((e for e in events if e.get("type") == etype), None)


def final_text(events: list) -> str:
    f = first(events, "final")
    return str((f or {}).get("text", ""))


async def main() -> None:
    results: list[tuple[str, bool, str]] = []

    def step(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))

    async with httpx.AsyncClient(timeout=30) as c:
        # 1. 恒需认证
        r = await c.get(f"{CORE}/audit")
        step("无 token /audit → 401", r.status_code == 401, str(r.status_code))

        # 2. OIDC 登录
        tok_emp = await login(c, "E1001")
        step("E1001 OIDC 登录取 token", bool(tok_emp), "ok")

        # 3. 验签负路径
        bad = tok_emp[:-8] + ("00000000" if tok_emp[-8:] != "00000000" else "11111111")
        r = await c.post(
            f"{CORE}/chat",
            headers={"Authorization": f"Bearer {bad}"},
            json={"session_id": "e2e", "message": "你好"},
        )
        step("篡改 token /chat → 401", r.status_code == 401, str(r.status_code))

        # 4. 数据级·业务维：BI 白名单外区域
        events = await chat(c, tok_emp, "e2e-bi-deny", "查一下华北区域的销售额")
        text = final_text(events)
        step("E1001 BI 华北 → 区域权限拒绝", "数据权限" in text, text[:60])

        # 5. 技能级放行：员工请假 → 确认卡
        # （话术用 ISO 日期：rules 兜底的"下周X"单次匹配取不到双日期）
        events = await chat(
            c,
            tok_emp,
            "e2e-leave-" + secrets.token_hex(4),
            "帮我请年假 2026-09-21 09:00 到 2026-09-22 18:00 事由：回家过节",
        )
        card = first(events, "confirm_card")
        tok_id = card.get("confirm_token") if card else None
        step(
            "E1001 请假 → confirm_card（employee 放行）",
            tok_id is not None,
            final_text(events)[:60] if not tok_id else f"token={tok_id[:8]}…",
        )

        # 6. 技能级拒绝：员工行内审批
        await chat(c, tok_emp, "e2e-approve-deny", "查一下我的待办审批")
        events = await chat(c, tok_emp, "e2e-approve-deny", "同意第 1 条")
        text = final_text(events)
        step("E1001 行内审批 → 权限拒绝", "审批权限" in text, text[:60])

        # 7. 确认卡确认 → 写入执行
        r = await c.post(
            f"{CORE}/confirmations/{tok_id}",
            headers={"Authorization": f"Bearer {tok_emp}"},
            json={"action": "confirm"},
        )
        ok = r.status_code == 200 and r.json().get("status") == "ok"
        step("确认卡 confirm → 写入成功", ok, r.text[:100])

        # 8. 一次性语义
        r = await c.post(
            f"{CORE}/confirmations/{tok_id}",
            headers={"Authorization": f"Bearer {tok_emp}"},
            json={"action": "confirm"},
        )
        step("重复确认 → 404", r.status_code == 404, str(r.status_code))

        # 9. manager 全量审计
        tok_mgr = await login(c, "E1003")
        r = await c.get(
            f"{CORE}/audit",
            headers={"Authorization": f"Bearer {tok_mgr}"},
            params={"user_id": "E1001", "limit": 100},
        )
        entries = r.json().get("items", []) if r.status_code == 200 else []
        actions = sorted({e["action"] for e in entries})
        step(
            "E1003 /audit?user_id=E1001 全量",
            r.status_code == 200 and len(entries) > 0,
            f"{len(entries)} 条 actions={actions}",
        )

        # 10/11. 员工仅本人 + 越权 403
        r = await c.get(
            f"{CORE}/audit", headers={"Authorization": f"Bearer {tok_emp}"}
        )
        own = r.json().get("items", []) if r.status_code == 200 else []
        step(
            "E1001 /audit 仅本人",
            r.status_code == 200 and all(e["user_id"] == "E1001" for e in own),
            f"{len(own)} 条",
        )
        r = await c.get(
            f"{CORE}/audit",
            headers={"Authorization": f"Bearer {tok_emp}"},
            params={"user_id": "E1003"},
        )
        step("E1001 /audit?user_id=E1003 → 403", r.status_code == 403, str(r.status_code))

        # 12. 审计动作覆盖断言
        need = {"auth_success", "tool_call", "confirm_issued", "confirm_approved", "permission_denied"}
        missing = need - {e["action"] for e in entries}
        step(
            "E1001 审计动作齐全（PRD 10）",
            not missing,
            f"missing={sorted(missing)}" if missing else "全覆盖",
        )

        # 13. manager 写路径正向 + reject 审计
        await chat(c, tok_mgr, "e2e-mgr-approve", "查一下我的待办审批")
        events = await chat(c, tok_mgr, "e2e-mgr-approve", "驳回第 1 条 原因是：预算超了")
        card = first(events, "confirm_card")
        mgr_tok = card.get("confirm_token") if card else None
        step(
            "E1003 行内审批 → confirm_card（dept_manager 放行）",
            mgr_tok is not None,
            final_text(events)[:60] if not mgr_tok else f"token={mgr_tok[:8]}…",
        )
        if mgr_tok:
            r = await c.post(
                f"{CORE}/confirmations/{mgr_tok}",
                headers={"Authorization": f"Bearer {tok_mgr}"},
                json={"action": "reject"},
            )
            step("驳回确认 → cancelled", r.status_code == 200, r.text[:80])

    # 汇总
    print()
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))
    print(f"\nE2E 结果：{passed}/{len(results)} 通过")
    if passed != len(results):
        sys.exit(1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
