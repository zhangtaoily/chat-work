"""FastAPI 入口：健康检查 + 对话 SSE + HITL 确认（ARCHITECTURE 4.1）。

对话为 SSE 流式响应，事件类型定义见 packages/protocol/events/chat-events.ts
（stage_progress / draft_card / confirm_card / final）。

- POST /chat：跑八节点流水线，逐节点转 SSE（写入类技能流终止于 confirm_card）
- POST /confirmations/{token}：HITL 确认（confirm → 恢复图 execute→format 完成写入；
  reject → 取消）——确认卡一次性，重复确认 404
- GET /audit：审计流水查询（PRD 10：dept_manager 全量，其余仅本人；恒需认证）
- GET /metrics/gray：灰度观测指标（PLAN P1.4，PRD 16.4：dept_manager 聚合审计流水）
- 技能市场（PLAN P2.3，PRD 3.4/3.5/5.6）：广场浏览/详情/一键安装（权限校验）/
  我的技能 + 生命周期（注册/提交/评审 SEC-RV-*/下架，security_reviewer 角色门禁，
  动作全部落审计）
- 认证（PRD 8.5）：Bearer / X-Chat-Auth JWT 验签（api/auth.py，RS256 + JWKS 缓存）；
  SSO_REQUIRED=true 强制鉴权；验证通过后 user_id 强制取 JWT sub（防请求体伪造），
  身份上下文（roles/dept/perm_ver）随 auth 字典进流水线供 permission 节点消费。
- 灰度门禁（PLAN P1.4，PRD 5.5.6）：X-Client-Version 版本协商（低于
  MIN_CLIENT_VERSION → 403 client_version_too_low 强制阻断）+ GRAY_PERCENT
  稳定分桶放量（未命中 → 403 gray_percent_exceeded）。
  生产：APISIX 网关验签后透传；同会话请求进车道队列 session_lane:{sessionId} 串行执行
"""

import json
import os
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from agent_core import audit, automation
from agent_core.api import gray
from agent_core.api.auth import AuthContext, authenticate, set_sso_required, sso_required
from agent_core.guardrail import confirm_store
from agent_core.knowledge import store as knowledge_store
from agent_core.pipeline.graph import build_graph, build_resume_graph
from agent_core.skills import store
from agent_core.skills.registry import get_skill


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """启动恢复：技能市场 / 知识库 / 自动化任务快照，并挂载调度器（PLAN P2.3-P2.5）。"""
    await store.restore()
    await knowledge_store.restore()
    await automation.restore()
    automation.start_scheduler()
    yield
    automation.stop_scheduler()


app = FastAPI(title="agent-core", version="0.1.0", lifespan=lifespan)

# 强制鉴权开关（PRD 8.5）：SSO_REQUIRED=true 时无 token / 验签失败一律 401；
# 默认 false 保留本地冒烟的请求体直传身份通道
set_sso_required(os.environ.get("SSO_REQUIRED", "").strip().lower() in {"1", "true", "yes"})

# 开发期放行桌面端 dev server / Electron file:// 的跨域 SSE 请求；
# 生产收口到内网网关（APISIX 统一鉴权后同源转发）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    """对话请求（user_id 优先取 JWT sub；无 token 且 SSO_REQUIRED=false 时才用直传值）。"""

    session_id: str = Field(min_length=1)
    user_id: str = Field(default="")
    message: str = Field(min_length=1)


class ConfirmRequest(BaseModel):
    """确认卡提交（PRD 4.1 HITL：用户点确认/取消）。

    modified：用户在确认卡上修改过参数（PRD 16.4 灰度观测——
    "修改字段"占比过高说明参数提取不准）。
    """

    action: str = Field(pattern="^(confirm|reject)$")
    modified: bool = False


class SkillRegisterRequest(BaseModel):
    """技能注册请求（PRD 3.5 生命周期起点：新建 draft 等待提交）。"""

    name: str = Field(min_length=1)
    title: str = Field(min_length=1)
    rw: str = Field(min_length=1)
    category: str = "dept"
    dept_scope: str | None = None
    version: str = "0.1.0"


class SkillNoteRequest(BaseModel):
    """技能提交/下架请求（备注可选）。"""

    note: str = ""


class SkillReviewRequest(BaseModel):
    """技能评审请求（PRD 5.6.1：通过/驳回 + 评审意见）。"""

    approve: bool
    note: str = ""


class AutomationCreateRequest(BaseModel):
    """自动化任务创建请求（PRD 3.6.2：params 为 draft 形态 {field: {"value","source"}}）。"""

    name: str = Field(min_length=1)
    skill: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    schedule: dict[str, Any]
    channel: dict[str, Any] | None = None
    scope: str = "personal"


class KnowledgeUploadRequest(BaseModel):
    """知识文档上传/沉淀请求（PRD 9.5.2 来源①文档上传 / ③会话产物沉淀）。"""

    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    space: str = "group"
    dept_scope: str | None = None
    classification: str = "D2"
    file_type: str = "md"
    tags: list[str] = Field(default_factory=list)
    source: str = "upload"


class KnowledgeUpdateRequest(BaseModel):
    """知识文档内容更新请求（PRD 9.5.2 版本规则：version+1 回 draft 重审）。"""

    content: str = Field(min_length=1)
    note: str = ""


class KnowledgeReviewRequest(BaseModel):
    """知识文档审核请求（PRD 9.5.2：通过向量化入库 / 驳回可改再提）。"""

    approve: bool
    note: str = ""


class KnowledgeNoteRequest(BaseModel):
    """知识文档下线请求（备注可选，记录失效原因供溯源）。"""

    note: str = ""


class KnowledgeSearchRequest(BaseModel):
    """知识检索请求（PRD 9.5.3 RAG：空间过滤 + 阈值 Top-K，D3 脱敏）。"""

    query: str = Field(min_length=1)
    top_k: int = Field(default=3, ge=1, le=10)


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.get("/health")
async def health() -> dict[str, str]:
    """健康检查（网关路由/容器探针）。"""
    return {"status": "ok", "version": "0.1.0"}


async def _authenticate_or_401(request: Request) -> AuthContext | None:
    """统一鉴权：SSO_REQUIRED 时未带/验签失败 → 401（PRD 8.5）。"""
    try:
        auth = await authenticate(request)
    except Exception as exc:
        if sso_required():
            raise HTTPException(status_code=401, detail=f"认证失败：{exc}") from exc
        return None
    if auth is None and sso_required():
        raise HTTPException(status_code=401, detail="缺少认证凭证（Bearer / X-Chat-Auth）")
    return auth


@app.post("/chat")
async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    """对话入口：LangGraph 八节点流水线，逐节点事件转 SSE 流。"""
    auth = await _authenticate_or_401(request)
    # 身份以 JWT 为准（防请求体伪造，PRD 8.5.5：网关验签 → 提取 sub）
    user_id = auth.user_id if auth is not None else req.user_id
    if not user_id:
        raise HTTPException(status_code=401, detail="缺少用户身份")

    # 灰度门禁（PLAN P1.4，PRD 5.5.6）：强制升级优先于放量分桶
    if not gray.client_version_allowed(request.headers.get("X-Client-Version")):
        await audit.record(
            "client_version_denied",
            user_id=user_id,
            session_id=req.session_id,
            result="denied",
            detail=f"client={request.headers.get('X-Client-Version')!r} "
            f"min={gray.min_client_version()!r}",
        )
        return JSONResponse(
            status_code=403,
            content={"code": "client_version_too_low", "message": "当前版本过旧，请更新到最新版后使用"},
        )
    if not gray.is_gray_user(user_id):
        await audit.record(
            "gray_denied",
            user_id=user_id,
            session_id=req.session_id,
            result="denied",
            detail=f"bucket={gray.bucket_of(user_id)} gray_percent={gray.gray_percent()}",
        )
        return JSONResponse(
            status_code=403,
            content={"code": "gray_percent_exceeded", "message": "灰度放量中，您暂未在小流量范围，请稍候"},
        )

    if auth is not None:
        # 审计：鉴权成功（登录后会话活动代理，PLAN P1.2）
        await audit.record(
            "auth_success",
            user_id=auth.user_id,
            session_id=req.session_id,
            detail=f"roles={','.join(auth.roles) or '-'} perm_ver={auth.perm_ver}",
        )

    async def event_stream() -> Any:
        graph = build_graph().compile()
        initial: dict[str, Any] = {
            "user_id": user_id,
            "session_id": req.session_id,
            "message": req.message,
        }
        actor_token = None
        if auth is not None:
            # 身份上下文进流水线（permission 节点消费，PLAN P1.1）
            initial["auth"] = {
                "user_id": auth.user_id,
                "dept": auth.dept,
                "roles": auth.roles,
                "perm_ver": auth.perm_ver,
            }
            # MCP 埋点经 contextvar 携带操作者（PLAN P1.2）
            actor_token = audit.set_actor(
                {
                    "user_id": auth.user_id,
                    "session_id": req.session_id,
                    "dept": auth.dept,
                    "roles": auth.roles,
                }
            )
        started = time.monotonic()
        turn_error: str = ""
        try:
            async for update in graph.astream(initial, stream_mode="updates"):
                for node_output in update.values():
                    if not isinstance(node_output, dict):
                        continue
                    for event in node_output.get("events", []):
                        yield _sse(event)
        except Exception as exc:  # noqa: BLE001 - SSE 流内兜底，避免挂死客户端
            turn_error = str(exc)
            yield _sse({"type": "final", "text": f"服务异常：{exc}", "cards": []})
        finally:
            if actor_token is not None:
                audit.reset_actor(actor_token)
            # 灰度观测埋点（PLAN P1.4，PRD 16.4）：对话总数/活跃用户/耗时
            await audit.record(
                "chat_turn",
                user_id=user_id,
                session_id=req.session_id,
                result="error" if turn_error else "ok",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/confirmations/{token}")
async def confirm(token: str, req: ConfirmRequest, request: Request) -> dict[str, Any]:
    """HITL 确认：恢复挂起的写入流程（PRD 4.1 / 8.7）。"""
    auth = await _authenticate_or_401(request)
    payload = await confirm_store.pop_token(token)
    if payload is None:
        raise HTTPException(status_code=404, detail="确认卡不存在或已过期")

    initiator = payload["state"].get("user_id")
    session_id = payload["state"].get("session_id")
    tool_call = payload["state"].get("tool_call") or {}

    # 确认人必须与发起人一致（防止他人凭 token 代替确认；无 token 时跳过——本地冒烟）
    if auth is not None and initiator != auth.user_id:
        # 审计：确认人校验失败（越权尝试，PLAN P1.2）
        await audit.record(
            "confirm_denied",
            user_id=auth.user_id,
            session_id=session_id,
            tool=tool_call.get("name"),
            params=tool_call.get("arguments"),
            result="denied",
            detail=f"确认人 {auth.user_id} ≠ 发起人 {initiator}",
        )
        raise HTTPException(status_code=403, detail="确认人与发起人不一致")

    if req.action == "reject":
        # 审计：用户主动拒绝（放弃写入，PLAN P1.2）
        await audit.record(
            "confirm_rejected",
            user_id=initiator,
            session_id=session_id,
            tool=tool_call.get("name"),
            params=tool_call.get("arguments"),
        )
        return {"status": "cancelled", "message": "已取消提交"}

    state: dict[str, Any] = dict(payload["state"])
    state["confirmed"] = True  # 恢复路径放行 execute（HITL 已确认）
    actor_token = None
    if auth is not None:
        # 恢复图内的 MCP 埋点携带操作者（PLAN P1.2）
        actor_token = audit.set_actor(
            {
                "user_id": auth.user_id,
                "session_id": session_id,
                "dept": auth.dept,
                "roles": auth.roles,
            }
        )
    try:
        resume = build_resume_graph().compile()
        final_state = await resume.ainvoke(state)
    finally:
        if actor_token is not None:
            audit.reset_actor(actor_token)
    # 审计：确认通过（写入执行完成，PLAN P1.2；修改字段标记供灰度观测，PRD 16.4）
    await audit.record(
        "confirm_approved",
        user_id=initiator,
        session_id=session_id,
        tool=tool_call.get("name"),
        params=tool_call.get("arguments"),
        detail="modified=true" if req.modified else "",
    )
    return {"status": "ok", "final": final_state.get("final")}


@app.get("/metrics/gray")
async def metrics_gray(request: Request, days: int = 1) -> dict[str, Any]:
    """灰度观测指标（PLAN P1.4，PRD 16.4）：聚合审计流水按日观测。

    dept_manager 专属（口径对齐管理端审计，PRD 10）；覆盖：
    Agent 对话总数 / 活跃用户数 / 确认卡"修改字段"占比 /
    写入失败次数与原因分布 / 门禁拒绝数；用户手动退出会话比例
    待桌面端埋点接入（恒 null，PRD 16.4 第 5 项）。
    """
    auth = await _authenticate_or_401(request)
    if auth is None:
        raise HTTPException(status_code=401, detail="灰度观测需要认证")
    if "dept_manager" not in auth.roles:
        raise HTTPException(status_code=403, detail="灰度观测仅限管理员")
    days = max(1, min(days, 180))
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="milliseconds")
    entries = [e for e in await audit.recent(limit=5000) if (e.get("time") or "") >= cutoff]

    chat_users: set[str] = set()
    total_chats = approved = modified = write_failures = 0
    fail_by_tool: Counter[str] = Counter()
    fail_by_reason: Counter[str] = Counter()
    gate_denials: Counter[str] = Counter()
    for e in entries:
        action = e.get("action")
        if action == "chat_turn":
            total_chats += 1
            if e.get("user_id"):
                chat_users.add(e["user_id"])
        elif action == "confirm_approved":
            approved += 1
            if "modified=true" in (e.get("detail") or ""):
                modified += 1
        elif action == "tool_call" and e.get("result") == "error":
            write_failures += 1
            fail_by_tool[e.get("tool") or "-"] += 1
            fail_by_reason[(e.get("detail") or "").strip()[:120] or "unknown"] += 1
        elif action in {"gray_denied", "client_version_denied"}:
            gate_denials[action] += 1

    return {
        "days": days,
        "total_chats": total_chats,
        "active_users": len(chat_users),
        "confirm": {
            "approved": approved,
            "modified": modified,
            "modified_ratio": round(modified / approved, 4) if approved else 0.0,
        },
        "write_failures": {
            "total": write_failures,
            "by_tool": dict(fail_by_tool.most_common()),
            "by_reason": dict(fail_by_reason.most_common()),
        },
        "gate_denials": {
            "gray_percent_exceeded": gate_denials["gray_denied"],
            "client_version_too_low": gate_denials["client_version_denied"],
        },
        "session_abandons": None,  # 待桌面端埋点（PRD 16.4 第 5 项）
    }


@app.get("/audit")
async def audit_list(
    request: Request, limit: int = 100, user_id: str | None = None
) -> dict[str, Any]:
    """审计流水查询（PLAN P1.2，PRD 10：管理端审计者查同一流水）。

    恒需认证（SSO_REQUIRED=false 的本地冒烟也不放行匿名查询）；
    dept_manager 可查任意/全部，其余角色仅本人（数据级权限同一口径）。
    """
    auth = await _authenticate_or_401(request)
    if auth is None:
        raise HTTPException(status_code=401, detail="审计查询需要认证")
    if "dept_manager" in auth.roles:
        target = user_id
    else:
        if user_id and user_id != auth.user_id:
            raise HTTPException(status_code=403, detail="仅可查询本人审计记录")
        target = auth.user_id
    items = await audit.recent(limit=max(1, min(limit, 500)), user_id=target)
    return {"items": items, "count": len(items)}


# ---- 技能市场（PLAN P2.3，PRD 3.4/3.5/5.6）----


def _market_auth(auth: AuthContext | None) -> AuthContext:
    """市场端点统一认证门禁（恒需认证，口径对齐 /audit）。"""
    if auth is None:
        raise HTTPException(status_code=401, detail="技能市场需要认证")
    return auth


@app.get("/skills")
async def skills_marketplace(
    request: Request,
    category: str | None = None,
    q: str | None = None,
    include_unpublished: bool = False,
) -> dict[str, Any]:
    """技能广场（PRD 3.4）：浏览/搜索已上架技能，含调用量与成功率。

    默认仅 published；dept_manager/security_reviewer 可带
    include_unpublished=true 查看未上架技能（评审工作台数据源）。
    """
    auth = _market_auth(await _authenticate_or_401(request))
    if include_unpublished and not ({"dept_manager", "security_reviewer"} & set(auth.roles)):
        raise HTTPException(status_code=403, detail="未上架技能仅限管理员查看")
    items = store.list_meta(category=category, q=q, include_unpublished=include_unpublished)
    return {"items": items, "count": len(items)}


@app.get("/skills/mine")
async def my_skills(request: Request) -> dict[str, Any]:
    """我的技能（PRD 3.4 一键安装后的清单）。"""
    auth = _market_auth(await _authenticate_or_401(request))
    items = store.installed_of(auth.user_id)
    return {"items": items, "count": len(items)}


@app.post("/skills/manage")
async def register_skill(req: SkillRegisterRequest, request: Request) -> dict[str, Any]:
    """技能注册（PRD 3.5）：新建 draft 元数据，等待提交上架。

    注册期技能不参与路由（定义另行注入），仅市场目录可见；
    生命周期后续动作（submit/review/deprecate）由 store 落审计。
    """
    auth = _market_auth(await _authenticate_or_401(request))
    try:
        return await store.register(
            name=req.name,
            title=req.title,
            rw=req.rw,
            category=req.category,
            dept_scope=req.dept_scope,
            version=req.version,
            by=auth.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/skills/{name}")
async def skill_detail(name: str, request: Request) -> dict[str, Any]:
    """技能详情（PRD 3.4）：市场元数据 + 运行时定义合并展示。"""
    _market_auth(await _authenticate_or_401(request))
    meta = store.detail(name)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"技能 {name} 未注册")
    item = {**meta}
    skill = get_skill(name)
    if skill is not None:
        item["definition"] = skill
    return item


@app.post("/skills/{name}/install")
async def install_skill(name: str, request: Request) -> dict[str, Any]:
    """一键安装（PRD 3.4）：仅 published 可装；科室技能校验 dept_scope。

    组织维口径对齐 permission.check_dept_scope：auth.dept 尾段匹配
    即放行（「事业部A/生产科」→「生产科」），跨科室 403。
    """
    auth = _market_auth(await _authenticate_or_401(request))
    meta = store.detail(name)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"技能 {name} 未注册")
    if meta["status"] != "published":
        raise HTTPException(
            status_code=400, detail=f"技能 {name} 当前状态 {meta['status']} 不可安装"
        )
    scope = meta.get("dept_scope")
    if scope and auth.dept.split("/")[-1] != scope:
        raise HTTPException(
            status_code=403,
            detail=f"暂无「{scope}」的数据权限（当前科室：{auth.dept or '未设置'}），请联系管理员开通。",
        )
    await store.install(name, user_id=auth.user_id)
    return {"status": "ok", "skill": name, "installed": True}


@app.delete("/skills/{name}/install")
async def uninstall_skill(name: str, request: Request) -> dict[str, Any]:
    """卸载：从我的技能清单移除（不影响市场状态）。"""
    auth = _market_auth(await _authenticate_or_401(request))
    await store.uninstall(name, user_id=auth.user_id)
    return {"status": "ok", "skill": name, "installed": False}


@app.post("/skills/{name}/submit")
async def submit_skill(name: str, req: SkillNoteRequest, request: Request) -> dict[str, Any]:
    """提交上架（PRD 3.4 科室管理员提交）：只读自动发布，写入进 SEC-RV-* 评审。"""
    auth = _market_auth(await _authenticate_or_401(request))
    try:
        return await store.submit(name, by=auth.user_id, note=req.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/skills/{name}/review")
async def review_skill(
    name: str, req: SkillReviewRequest, request: Request
) -> dict[str, Any]:
    """安全评审（PRD 5.6.1 信息科安全评审员）：写入类技能上架必经门禁。"""
    auth = _market_auth(await _authenticate_or_401(request))
    if "security_reviewer" not in auth.roles:
        raise HTTPException(status_code=403, detail="安全评审仅限信息科安全评审员")
    try:
        return await store.review(name, approve=req.approve, by=auth.user_id, note=req.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/skills/{name}/deprecate")
async def deprecate_skill(
    name: str, req: SkillNoteRequest, request: Request
) -> dict[str, Any]:
    """下架（PRD 3.5 生命周期收尾）：路由即时降级闲聊兜底。"""
    auth = _market_auth(await _authenticate_or_401(request))
    if not ({"security_reviewer", "dept_manager"} & set(auth.roles)):
        raise HTTPException(status_code=403, detail="下架仅限管理员操作")
    try:
        return await store.deprecate(name, by=auth.user_id, note=req.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---- 自动化任务（PLAN P2.4，PRD 3.6：定时执行只读技能，先跑通再自动化）----


def _automation_auth(auth: AuthContext | None) -> AuthContext:
    """自动化端点统一认证门禁（口径对齐市场端点）。"""
    if auth is None:
        raise HTTPException(status_code=401, detail="自动化任务需要认证")
    return auth


def _automation_own(task: dict[str, Any], auth: AuthContext) -> None:
    """归属校验：任务创建者本人或管理员（dept_manager/security_reviewer）。"""
    if task["owner"] != auth.user_id and not (
        {"dept_manager", "security_reviewer"} & set(auth.roles)
    ):
        raise HTTPException(status_code=403, detail="仅任务创建者或管理员可操作")


def _require_task(task_id: str, auth: AuthContext) -> dict[str, Any]:
    """取任务并校验归属，不存在 → 404。"""
    task = automation.detail(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
    _automation_own(task, auth)
    return task


@app.get("/automations")
async def list_automations(request: Request, status: str | None = None) -> dict[str, Any]:
    """我的自动化任务列表（仅本人创建；status 可选过滤 active/paused）。"""
    auth = _automation_auth(await _authenticate_or_401(request))
    items = automation.list_tasks(owner=auth.user_id, status=status)
    return {"items": items, "count": len(items)}


@app.post("/automations")
async def create_automation(req: AutomationCreateRequest, request: Request) -> dict[str, Any]:
    """创建自动化任务（PRD 3.6.2）：仅只读技能；防护栏校验失败 → 400。"""
    auth = _automation_auth(await _authenticate_or_401(request))
    try:
        return await automation.create(
            name=req.name,
            skill=req.skill,
            params=req.params,
            schedule=req.schedule,
            owner=auth.user_id,
            owner_auth={
                "user_id": auth.user_id,
                "dept": auth.dept,
                "roles": list(auth.roles),
            },
            channel=req.channel,
            scope=req.scope,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/automations/inbox")
async def automation_inbox(
    request: Request, limit: int = 50, unread_only: bool = False
) -> dict[str, Any]:
    """结果信箱（PRD 3.6.2）：执行结果与自动暂停通知，恒取当前用户。"""
    auth = _automation_auth(await _authenticate_or_401(request))
    items = automation.inbox(auth.user_id, limit=max(1, min(limit, 200)))
    if unread_only:
        items = [m for m in items if not m["read"]]
    return {"items": items, "count": len(items)}


@app.post("/automations/inbox/read")
async def automation_inbox_read(request: Request) -> dict[str, Any]:
    """信箱全部已读标记。"""
    auth = _automation_auth(await _authenticate_or_401(request))
    marked = automation.mark_inbox_read(auth.user_id)
    return {"status": "ok", "marked": marked}


@app.get("/automations/{task_id}")
async def automation_detail(task_id: str, request: Request) -> dict[str, Any]:
    """任务详情（创建者本人或管理员）。"""
    auth = _automation_auth(await _authenticate_or_401(request))
    return _require_task(task_id, auth)


@app.post("/automations/{task_id}/pause")
async def pause_automation(task_id: str, request: Request) -> dict[str, Any]:
    """暂停任务（取消已注册的调度 job）。"""
    auth = _automation_auth(await _authenticate_or_401(request))
    _require_task(task_id, auth)
    try:
        return await automation.pause(task_id, by=auth.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/automations/{task_id}/resume")
async def resume_automation(task_id: str, request: Request) -> dict[str, Any]:
    """恢复任务：重置失败计数并重新注册调度。"""
    auth = _automation_auth(await _authenticate_or_401(request))
    _require_task(task_id, auth)
    try:
        return await automation.resume(task_id, by=auth.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/automations/{task_id}")
async def delete_automation(task_id: str, request: Request) -> dict[str, Any]:
    """删除任务（执行历史保留在审计与 store 内，响应体不再可查）。"""
    auth = _automation_auth(await _authenticate_or_401(request))
    _require_task(task_id, auth)
    await automation.delete(task_id, by=auth.user_id)
    return {"status": "ok", "task_id": task_id, "deleted": True}


@app.get("/automations/{task_id}/history")
async def automation_history(
    task_id: str, request: Request, limit: int = 20
) -> dict[str, Any]:
    """执行历史（新的在前；含结果文本摘要与耗时）。"""
    auth = _automation_auth(await _authenticate_or_401(request))
    _require_task(task_id, auth)
    items = automation.history(task_id, limit=max(1, min(limit, 100)))
    return {"items": items, "count": len(items)}


# ---- 知识库（PLAN P2.5，PRD 9.5：两级知识空间 + 入库流程 + RAG 检索）----


def _knowledge_auth(auth: AuthContext | None) -> AuthContext:
    """知识库端点统一认证门禁（恒需认证，口径对齐技能市场）。"""
    if auth is None:
        raise HTTPException(status_code=401, detail="知识库需要认证")
    return auth


def _dept_of(auth: AuthContext) -> str:
    """科室短名（组织维口径对齐 install_skill：尾段匹配）。"""
    return auth.dept.split("/")[-1] if auth.dept else ""


def _manage_group_space(auth: AuthContext) -> None:
    """集团知识库管理权（PRD 9.5.1 角色矩阵）：信息科知识管理员。"""
    if "knowledge_manager" not in auth.roles:
        raise HTTPException(status_code=403, detail="集团知识库管理仅限信息科知识管理员")


def _require_manage(auth: AuthContext, doc: dict[str, Any]) -> None:
    """空间管理权限（PRD 9.5.1）：group=信息科知识管理员 / dept=本科室管理员。"""
    if doc["space"] == "group":
        _manage_group_space(auth)
    elif "dept_manager" not in auth.roles or _dept_of(auth) != doc["dept_scope"]:
        raise HTTPException(status_code=403, detail="科室知识空间管理仅限本科室管理员")


def _require_visible(auth: AuthContext, doc: dict[str, Any]) -> None:
    """浏览权限（PRD 9.5.1）：group 全员可见；dept 空间仅本科室（集团管理员放行）。"""
    if (
        doc["space"] == "dept"
        and doc["dept_scope"] != _dept_of(auth)
        and "knowledge_manager" not in auth.roles
    ):
        raise HTTPException(status_code=404, detail=f"知识文档 {doc['doc_id']} 不存在")


def _upload_scope(req: KnowledgeUploadRequest, auth: AuthContext) -> tuple[str, str | None]:
    """上传/沉淀的空间归属（PRD 9.5.1）：group 仅管理员；dept 强制本科室。

    source=session（会话产物沉淀）仅落科室空间走审核——校验先于空间分支，
    避免管理员绕过约束沉淀到集团空间（个人空间 Phase 3 上线）。
    """
    if req.source == "session" and req.space != "dept":
        raise HTTPException(
            status_code=400, detail="会话产物沉淀仅支持科室空间（个人空间 Phase 3 上线）"
        )
    if req.space == "group":
        _manage_group_space(auth)
        return "group", None
    dept = _dept_of(auth)
    if not dept:
        raise HTTPException(status_code=400, detail="缺少科室信息，无法上传科室知识")
    return "dept", dept


@app.post("/knowledge/docs")
async def upload_knowledge_doc(
    req: KnowledgeUploadRequest, request: Request
) -> dict[str, Any]:
    """上传/沉淀（PRD 9.5.2 入库流程起点）：切片 + 涉密检测，落 draft 待提交审核。

    source=session 即会话产物沉淀（前端「保存到知识库」）：
    D4 机密直接拒绝入库；D3 敏感放行但检索结果脱敏展示。
    """
    auth = _knowledge_auth(await _authenticate_or_401(request))
    space, dept_scope = _upload_scope(req, auth)
    try:
        return await knowledge_store.upload(
            title=req.title,
            content=req.content,
            space=space,
            dept_scope=dept_scope,
            classification=req.classification,
            file_type=req.file_type,
            tags=req.tags,
            uploaded_by=auth.user_id,
            source=req.source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/knowledge/docs")
async def knowledge_docs(
    request: Request,
    space: str | None = None,
    dept_scope: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """知识浏览（PRD 9.5.4）：group 全员可见；dept 空间仅本科室（审核工作台带 status）。"""
    auth = _knowledge_auth(await _authenticate_or_401(request))
    dept = _dept_of(auth)
    is_group_admin = "knowledge_manager" in auth.roles
    if (
        dept_scope
        and dept_scope != dept
        and not is_group_admin
        and "dept_manager" not in auth.roles
    ):
        raise HTTPException(status_code=403, detail="仅可查看本科室知识空间")
    if space == "dept" and not is_group_admin:
        dept_scope = dept_scope or dept  # 普通用户强制本科室视角
    items = knowledge_store.list_docs(
        space=space, dept_scope=dept_scope, tag=tag, q=q, status=status
    )
    visible = [
        d for d in items if d["space"] == "group" or d["dept_scope"] == dept or is_group_admin
    ]
    return {"items": visible, "count": len(visible)}


@app.get("/knowledge/docs/{doc_id}")
async def knowledge_doc_detail(doc_id: str, request: Request) -> dict[str, Any]:
    """文档详情（含切片与审核记录；dept 空间跨科室 404 不暴露存在性）。"""
    auth = _knowledge_auth(await _authenticate_or_401(request))
    doc = knowledge_store.detail(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"知识文档 {doc_id} 不存在")
    _require_visible(auth, doc)
    return doc


@app.post("/knowledge/docs/{doc_id}/submit")
async def submit_knowledge_doc(doc_id: str, request: Request) -> dict[str, Any]:
    """提交审核（PRD 9.5.2 入库流程：draft/rejected → pending_review）。"""
    auth = _knowledge_auth(await _authenticate_or_401(request))
    doc = knowledge_store.detail(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"知识文档 {doc_id} 不存在")
    try:
        return await knowledge_store.submit(doc_id, by=auth.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/knowledge/docs/{doc_id}/review")
async def review_knowledge_doc(
    doc_id: str, req: KnowledgeReviewRequest, request: Request
) -> dict[str, Any]:
    """审核（PRD 9.5.2：集团=信息科知识管理员 / 科室=本科室管理员）。

    通过即向量化入库（published 生效）；驳回回 rejected 可改后重新提交。
    """
    auth = _knowledge_auth(await _authenticate_or_401(request))
    doc = knowledge_store.detail(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"知识文档 {doc_id} 不存在")
    _require_manage(auth, doc)
    try:
        return await knowledge_store.review(
            doc_id, approve=req.approve, by=auth.user_id, note=req.note
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/knowledge/docs/{doc_id}/update")
async def update_knowledge_doc(
    doc_id: str, req: KnowledgeUpdateRequest, request: Request
) -> dict[str, Any]:
    """内容更新（PRD 9.5.2 版本规则：重新切片、version+1、回 draft 重审）。

    上传人本人或空间管理员可操作（R10 风险：内容过期 → 版本化更新而非直接覆盖）。
    """
    auth = _knowledge_auth(await _authenticate_or_401(request))
    doc = knowledge_store.detail(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"知识文档 {doc_id} 不存在")
    if doc["uploaded_by"] != auth.user_id:
        _require_manage(auth, doc)
    try:
        return await knowledge_store.update(
            doc_id, content=req.content, by=auth.user_id, note=req.note
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/knowledge/docs/{doc_id}/deprecate")
async def deprecate_knowledge_doc(
    doc_id: str, req: KnowledgeNoteRequest, request: Request
) -> dict[str, Any]:
    """失效下线（PRD 9.5.2 / R10）：向量立即出索引，文档记录保留供溯源。"""
    auth = _knowledge_auth(await _authenticate_or_401(request))
    doc = knowledge_store.detail(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"知识文档 {doc_id} 不存在")
    _require_manage(auth, doc)
    try:
        return await knowledge_store.deprecate(doc_id, by=auth.user_id, note=req.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/knowledge/stats")
async def knowledge_stats(request: Request) -> dict[str, Any]:
    """使用统计（PRD 9.5.4）：引用次数/最近引用时间，按热度排序。"""
    _knowledge_auth(await _authenticate_or_401(request))
    items = knowledge_store.stats_overview()
    return {"items": items, "count": len(items)}


@app.get("/knowledge/gaps")
async def knowledge_gaps(request: Request) -> dict[str, Any]:
    """知识缺口（PRD 9.5.4）：未命中高频问题，推荐管理员补充 FAQ/文档。"""
    _knowledge_auth(await _authenticate_or_401(request))
    items = knowledge_store.gaps_list()
    return {"items": items, "count": len(items)}


@app.post("/knowledge/search")
async def knowledge_search(req: KnowledgeSearchRequest, request: Request) -> dict[str, Any]:
    """RAG 检索（PRD 9.5.3，前端知识面板/联调）：空间过滤 + 阈值 Top-K。

    会话内注入由对话流水线完成（route/extract/execute/format 四注入点），
    本端点供独立检索场景；D3 敏感文本返回前已脱敏（PRD 10.2）。
    """
    auth = _knowledge_auth(await _authenticate_or_401(request))
    return await knowledge_store.search(req.query, dept=_dept_of(auth), top_k=req.top_k)
