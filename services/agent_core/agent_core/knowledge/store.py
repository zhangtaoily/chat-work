"""知识库域（PRD 9.5）：两级知识空间 + 入库流程 + RAG 检索注入。

空间分级（PRD 9.5.1，Phase 2 落集团与科室两级）：
- group 集团知识库：信息科（knowledge_manager 角色）管理，全员按密级可见
- dept 科室知识空间：科室管理员管理，本科室可见；普通员工可上传（走审核）
- personal 个人空间留 Phase 3

入库流程（PRD 9.5.2）：上传（切片 + 涉密检测）→ 提交审核 → 通过即向量化入库
状态机：draft → pending_review → published → deprecated（rejected 可改再提）
- 涉密检测：D4 机密直接拒绝（不入知识库）；D3 敏感放行但检索结果脱敏展示（PRD 10.2）
- 版本：update 重新切片、version+1、回 draft 重审；向量随重审重建
- 会话产物沉淀（来源③）：source="session" 与上传同流程

RAG 检索（PRD 9.5.3）：published 向量索引，相似度达阈值取 Top-K
（API 后端 0.75 / 本地降级后端 0.35，见 embedding.sim_threshold）；
空间过滤（group 全员 / dept 本科室）；命中必须附来源（文档名 + 章节）；
D3 脱敏展示；未命中记知识缺口（PRD 9.5.4 缺口分析数据源）。

存储：进程内 + 可选 Redis 快照 `knowledge:snapshot`（向量不入快照，
restore 后检索侧检测索引后端缺失自动全量重建）。
"""

import json
import re
from datetime import UTC, datetime
from typing import Any

from agent_core import audit, persist
from agent_core.knowledge.embedding import (
    backend_tag,
    cosine,
    embed_texts,
    sim_threshold,
)

_MAX_CHUNK_CHARS = 500  # 切片单块上限（≈Token，PRD 9.5.2 300~500）
SNAPSHOT_KEY = "knowledge:snapshot"

_docs: dict[str, dict[str, Any]] = {}
_gaps: dict[str, dict[str, Any]] = {}  # query -> {count, last_at, dept}
_seq = 0
_index_backend: str | None = None  # 入库时向量后端标记（检索侧一致性校验）
_redis_client: Any = None

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _get_redis() -> Any:
    global _redis_client
    if _redis_client is None:
        from agent_core.config import settings

        if settings.redis_url:
            import redis.asyncio as aioredis

            _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def reset() -> None:
    """清空知识库状态（测试隔离用）。"""
    global _seq, _index_backend
    _docs.clear()
    _gaps.clear()
    _seq = 0
    _index_backend = None


# ---- 切片（PRD 9.5.2：按标题层级，单块 ≤500 字，保留文档路径元数据）----


def _split_long(text: str) -> list[str]:
    """超长段落按句边界二次切片。"""
    if len(text) <= _MAX_CHUNK_CHARS:
        return [text]
    parts: list[str] = []
    buf = ""
    for seg in re.split(r"(?<=[。.!?！？;\n；])", text):
        if not seg:
            continue
        if buf and len(buf) + len(seg) > _MAX_CHUNK_CHARS:
            parts.append(buf.strip())
            buf = ""
        buf += seg
    if buf.strip():
        parts.append(buf.strip())
    return parts


def _split_chunks(content: str) -> list[tuple[str, str]]:
    """按标题层级切片，返回 [(section 路径, 块文本)]；无标题视为「正文」。"""
    sections: list[tuple[str, str]] = []
    stack: dict[int, str] = {}
    current: list[str] = []

    def section_name() -> str:
        parts = [stack[k] for k in sorted(stack)]
        return " / ".join(parts) if parts else "正文"

    def flush() -> None:
        text = "\n".join(current).strip()
        current.clear()
        if text:
            sections.extend((section_name(), piece) for piece in _split_long(text))

    for line in content.splitlines():
        matched = _HEADING_RE.match(line.strip())
        if matched:
            flush()
            level = len(matched.group(1))
            for key in [k for k in stack if k >= level]:
                del stack[key]
            stack[level] = matched.group(2).strip()
        else:
            current.append(line)
    flush()
    if not sections:
        sections.extend(("正文", piece) for piece in _split_long(content.strip()))
    return sections


def _mask_sensitive(text: str) -> str:
    """D3 脱敏展示（PRD 10.2）：手机号 138****5678、身份证前 6 后 4。"""
    text = re.sub(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)", r"\1****\2", text)
    text = re.sub(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)", r"\1********\2", text)
    return text


# ---- 查询（同步无 IO）----


def detail(doc_id: str) -> dict[str, Any] | None:
    doc = _docs.get(doc_id)
    return dict(doc) if doc else None


def list_docs(
    *,
    space: str | None = None,
    dept_scope: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """浏览过滤（PRD 9.5.4 知识浏览）；科室空间可见性由 API 层按调用者约束。"""
    out: list[dict[str, Any]] = []
    for doc in _docs.values():
        if space and doc["space"] != space:
            continue
        if dept_scope and doc["dept_scope"] != dept_scope:
            continue
        if tag and tag not in doc["tags"]:
            continue
        if status and doc["status"] != status:
            continue
        if q and q not in doc["title"] and q not in doc["content"]:
            continue
        out.append(dict(doc))
    return sorted(out, key=lambda d: d["created_at"], reverse=True)


def stats_overview() -> list[dict[str, Any]]:
    """使用统计（PRD 9.5.4）：引用次数/最近引用时间，按热度排序。"""
    rows = [
        {
            "doc_id": d["doc_id"],
            "title": d["title"],
            "space": d["space"],
            "dept_scope": d["dept_scope"],
            "classification": d["classification"],
            "status": d["status"],
            "chunk_count": len(d["chunks"]),
            "version": d["version"],
            "hits": d["stats"]["hits"],
            "last_hit_at": d["stats"]["last_hit_at"],
        }
        for d in _docs.values()
    ]
    return sorted(rows, key=lambda r: (-r["hits"], r["doc_id"]))


def gaps_list() -> list[dict[str, Any]]:
    """知识缺口（PRD 9.5.4）：未命中高频问题，按次数排序推荐科室管理员补 FAQ。"""
    rows = [{"query": q, **v} for q, v in _gaps.items()]
    return sorted(rows, key=lambda r: (-r["count"], r["query"]))


# ---- 生命周期（写路径，async 以落 Redis 快照；权限校验在 API 层）----


def _validate_doc(space: str, classification: str, title: str, content: str) -> None:
    if space not in {"group", "dept"}:
        raise ValueError("space 仅支持 group/dept")
    if classification not in {"D1", "D2", "D3"}:
        if classification == "D4":
            raise ValueError("D4 机密不入知识库（PRD 9.5.3 涉密检测拦截）")
        raise ValueError("classification 仅支持 D1/D2/D3")
    if not title.strip():
        raise ValueError("标题不能为空")
    if not content.strip():
        raise ValueError("内容不能为空")


async def _snapshot() -> None:
    r = _get_redis()
    # 向量字段不入快照（restore 后 reindex 重建，同时规避后端切换后的混空间）
    stripped = {
        "docs": {
            k: {**v, "chunks": [{**c, "vector": None, "section_vector": None} for c in v["chunks"]]}
            for k, v in _docs.items()
        },
        "gaps": _gaps,
        "seq": _seq,
    }
    if r is None:
        await persist.write_json(SNAPSHOT_KEY, stripped)
        return
    await r.set(SNAPSHOT_KEY, json.dumps(stripped, ensure_ascii=False))


async def restore() -> None:
    """启动恢复（API lifespan 调用）；Redis 快照优先，无 Redis 读本地文件兜底；
    向量不入快照，检索时自动重建。"""
    global _seq, _index_backend
    snap: dict[str, Any] | None = None
    r = _get_redis()
    if r is not None:
        raw = await r.get(SNAPSHOT_KEY)
        if raw:
            snap = json.loads(raw)
    if snap is None:
        snap = await persist.read_json(SNAPSHOT_KEY)
    if snap:
        _docs.update(snap.get("docs", {}))
        for d in _docs.values():  # 旧快照无 approvals 字段（双人复核引入前）兜底
            d.setdefault("approvals", [])
        _gaps.update(snap.get("gaps", {}))
        _seq = int(snap.get("seq", 0))
    _index_backend = None


async def upload(
    *,
    title: str,
    content: str,
    space: str,
    dept_scope: str | None = None,
    classification: str = "D2",
    file_type: str = "md",
    tags: list[str] | None = None,
    uploaded_by: str,
    source: str = "upload",
) -> dict[str, Any]:
    """上传/沉淀（来源①③）：切片 + 涉密检测，落 draft 待提交审核。"""
    _validate_doc(space, classification, title, content)
    if space == "dept" and not (dept_scope or "").strip():
        raise ValueError("科室知识空间必须指定 dept_scope")
    if space == "group":
        dept_scope = None
    if source not in {"upload", "session"}:
        raise ValueError("source 仅支持 upload/session")
    global _seq
    _seq += 1
    doc_id = f"kb_{_seq:05d}"
    chunks = [
        {"chunk_id": f"{doc_id}_c{i:02d}", "section": sec, "text": text, "vector": None, "section_vector": None}
        for i, (sec, text) in enumerate(_split_chunks(content))
    ]
    doc: dict[str, Any] = {
        "doc_id": doc_id,
        "space": space,
        "dept_scope": dept_scope,
        "title": title.strip(),
        "content": content,
        "file_type": file_type,
        "tags": [t.strip() for t in (tags or []) if t.strip()],
        "classification": classification,
        "source": source,
        "status": "draft",
        "version": 1,
        "uploaded_by": uploaded_by,
        "reviewed_by": None,
        "reviewed_at": None,
        "review_note": None,
        "approvals": [],
        "published_at": None,
        "deprecated_at": None,
        "chunks": chunks,
        "stats": {"hits": 0, "last_hit_at": None},
        "created_at": _now(),
        "updated_at": _now(),
    }
    _docs[doc_id] = doc
    await audit.record(
        "knowledge_upload",
        tool="knowledge_store",
        params={
            "doc_id": doc_id,
            "space": space,
            "classification": classification,
            "source": source,
            "chunks": len(chunks),
        },
        user_id=uploaded_by or None,
        result="ok",
        detail=f"上传知识文档「{doc['title']}」（{len(chunks)} 个切片，待审核）",
    )
    await _snapshot()
    return dict(doc)


def _get_or_fail(doc_id: str) -> dict[str, Any]:
    doc = _docs.get(doc_id)
    if doc is None:
        raise ValueError(f"知识文档 {doc_id} 不存在")
    return doc


async def submit(doc_id: str, *, by: str) -> dict[str, Any]:
    """提交审核（PRD 9.5.2 入库流程）。"""
    doc = _get_or_fail(doc_id)
    if doc["status"] not in {"draft", "rejected"}:
        raise ValueError(f"文档当前状态 {doc['status']} 不可提交")
    doc["status"] = "pending_review"
    doc["approvals"] = []  # 重新审核清空历史批准（双人复核从零起算）
    await audit.record(
        "knowledge_submit",
        tool="knowledge_store",
        params={"doc_id": doc_id, "space": doc["space"]},
        user_id=by or None,
        result="ok",
        detail=f"提交知识文档「{doc['title']}」进入审核",
    )
    await _snapshot()
    return dict(doc)


async def review(doc_id: str, *, approve: bool, by: str, note: str = "") -> dict[str, Any]:
    """审核（PRD 9.5.2：集团=信息科 / 科室=科室管理员，角色校验在 API 层）。

    通过即向量化入库（published 生效）；驳回回 rejected 可改后重新提交。
    双人复核（PRD 5.6.4：D2+ 知识放行需双人）：D2/D3 文档第一名审核人
    通过后保持 pending_review 并记录 approvals[0]（审计 result=first_approved），
    第二名不同审核人通过才发布；D1 公开文档单人即过。驳回单人生效。
    """
    doc = _get_or_fail(doc_id)
    if doc["status"] != "pending_review":
        raise ValueError(f"文档当前状态 {doc['status']} 不在审核中")
    if not approve:
        doc["reviewed_by"] = by
        doc["reviewed_at"] = _now()
        doc["review_note"] = note or "驳回"
        doc["approvals"] = []
        doc["status"] = "rejected"
    else:
        approvals = list(doc.get("approvals", []))
        if any(a["by"] == by for a in approvals):
            raise ValueError("D2+ 知识放行需两名不同审核人复核，请由第二审核人操作")
        if doc["classification"] in {"D2", "D3"}:
            approvals.append({"by": by, "at": _now(), "note": note})
            if len(approvals) < 2:
                doc["approvals"] = approvals
                doc["reviewed_by"] = by
                doc["reviewed_at"] = _now()
                doc["review_note"] = note or "通过（待第二审核人复核）"
                await audit.record(
                    "knowledge_review",
                    tool="knowledge_store",
                    params={"doc_id": doc_id, "approve": True},
                    user_id=by or None,
                    result="first_approved",
                    detail=(
                        f"知识文档「{doc['title']}」（{doc['classification']}）"
                        f"第一复核通过（{by}），待第二审核人复核（PRD 5.6.4 双人复核）"
                    ),
                )
                await _snapshot()
                return dict(doc)
        doc["approvals"] = approvals
        doc["reviewed_by"] = by
        doc["reviewed_at"] = _now()
        doc["review_note"] = note or "通过"
        doc["status"] = "published"
        doc["published_at"] = _now()
        await _embed_doc(doc)
    if approve:
        if doc["classification"] in {"D2", "D3"} and doc.get("approvals"):
            signers = "、".join(a["by"] for a in doc["approvals"])
            detail_text = f"审核通过，已向量化入库（双人复核：{signers}）"
        else:
            detail_text = "审核通过，已向量化入库"
    else:
        detail_text = "审核驳回"
    await audit.record(
        "knowledge_review",
        tool="knowledge_store",
        params={"doc_id": doc_id, "approve": approve},
        user_id=by or None,
        result="approved" if approve else "rejected",
        detail=(
            f"知识文档「{doc['title']}」{detail_text}{('：' + note) if note else ''}"
        ),
    )
    await _snapshot()
    return dict(doc)


async def update(doc_id: str, *, content: str, by: str, note: str = "") -> dict[str, Any]:
    """文档更新（PRD 9.5.2 版本规则）：重新切片、version+1、回 draft 重审。"""
    doc = _get_or_fail(doc_id)
    if doc["status"] not in {"draft", "rejected", "published"}:
        raise ValueError(f"文档当前状态 {doc['status']} 不可更新")
    if not content.strip():
        raise ValueError("内容不能为空")
    doc["content"] = content
    doc["version"] += 1
    doc["status"] = "draft"
    doc["chunks"] = [
        {"chunk_id": f"{doc['doc_id']}_c{i:02d}", "section": sec, "text": text, "vector": None, "section_vector": None}
        for i, (sec, text) in enumerate(_split_chunks(content))
    ]
    doc["updated_at"] = _now()
    await audit.record(
        "knowledge_update",
        tool="knowledge_store",
        params={"doc_id": doc_id, "version": doc["version"]},
        user_id=by or None,
        result="ok",
        detail=f"更新知识文档「{doc['title']}」至 v{doc['version']}，重新入库待审"
        f"{('：' + note) if note else ''}",
    )
    await _snapshot()
    return dict(doc)


async def deprecate(doc_id: str, *, by: str, note: str = "") -> dict[str, Any]:
    """失效下线（PRD 9.5.2）：向量立即下线，文档记录保留供溯源。"""
    doc = _get_or_fail(doc_id)
    if doc["status"] != "published":
        raise ValueError(f"文档当前状态 {doc['status']} 不可下线")
    doc["status"] = "deprecated"
    doc["deprecated_at"] = _now()
    for chunk in doc["chunks"]:
        chunk["vector"] = None
    doc["updated_at"] = _now()
    await audit.record(
        "knowledge_deprecate",
        tool="knowledge_store",
        params={"doc_id": doc_id},
        user_id=by or None,
        result="ok",
        detail=f"知识文档「{doc['title']}」失效下线，向量已出索引{('：' + note) if note else ''}",
    )
    await _snapshot()
    return dict(doc)


# ---- 向量索引 ----


async def _embed_chunks(chunks: list[dict[str, Any]]) -> str:
    """嵌入 chunk 正文与标题路径，返回实际后端标记。

    标题路径（section）是强检索信号：query 常为标题式短语（「报销流程」），
    与正文相比其 bigram/语义重叠更密集——评分取两者相似度最大值（见 search）。
    """
    if not chunks:
        return backend_tag()
    tag, vectors = await embed_texts(
        [c["text"] for c in chunks] + [c["section"] for c in chunks]
    )
    n = len(chunks)
    for chunk, tvec, svec in zip(chunks, vectors[:n], vectors[n:]):
        chunk["vector"] = tvec
        chunk["section_vector"] = svec
    return tag


async def _embed_doc(doc: dict[str, Any]) -> None:
    """文档向量化入库（review 通过时调用）。

    全局 _index_backend 置 None：增量入库不保证与既有索引同后端
    （API 可能中途降级），交给 search 的一致性校验触发全量重建自愈。
    """
    global _index_backend
    await _embed_chunks(doc["chunks"])
    _index_backend = None
    doc["updated_at"] = _now()


async def reindex() -> int:
    """全量重建 published 向量索引（后端切换/restore 自愈，PRD R10 向量同步）。"""
    global _index_backend
    flat = [c for d in _docs.values() if d["status"] == "published" for c in d["chunks"]]
    _index_backend = await _embed_chunks(flat)
    return len(flat)


# ---- 检索（RAG，PRD 9.5.3）----


def record_hit(doc: dict[str, Any]) -> None:
    """命中统计（PRD 9.5.4 使用统计）。"""
    doc["stats"]["hits"] += 1
    doc["stats"]["last_hit_at"] = _now()


async def record_gap(query: str, dept: str) -> None:
    """知识缺口记录（未命中，PRD 9.5.4 缺口分析数据源）。"""
    key = query.strip()
    if not key:
        return
    item = _gaps.setdefault(key, {"count": 0, "last_at": None, "dept": dept})
    item["count"] += 1
    item["last_at"] = _now()
    item["dept"] = dept


async def search(
    query: str,
    *,
    dept: str,
    top_k: int = 3,
    tags_filter: list[str] | None = None,
) -> dict[str, Any]:
    """RAG 检索：空间/标签过滤 → 相似度 ≥ 阈值 Top-K → 命中统计/D3 脱敏。

    - 空间过滤：group 全员；dept 仅本科室（PRD 9.5.1 注入范围）
    - 后端一致性：query 向量后端与索引不一致（API 上线/降级/restore）自动全量重建
    - 阈值按后端：API 后端 0.75（PRD 口径）/ 本地降级后端 0.35（独立校准）
    - 未命中记知识缺口（query/dept），回复侧提示「知识库未找到」
    """
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for doc in _docs.values():
        if doc["status"] != "published":
            continue
        if doc["space"] == "dept" and doc["dept_scope"] != dept:
            continue
        if tags_filter and not (set(doc["tags"]) & set(tags_filter)):
            continue
        for chunk in doc["chunks"]:
            candidates.append((doc, chunk))

    query_tag, query_vectors = await embed_texts([query])
    query_vec = query_vectors[0]
    if _index_backend != query_tag:
        await reindex()
    threshold = sim_threshold(_index_backend or query_tag)

    scored: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    for doc, chunk in candidates:
        if chunk["vector"] is None:  # reindex 未覆盖（并发写竞态）→ 跳过本轮
            continue
        score = cosine(query_vec, chunk["vector"])
        if chunk.get("section_vector") is not None:  # 标题路径匹配（query 常为标题式短语）
            score = max(score, cosine(query_vec, chunk["section_vector"]))
        if score >= threshold:
            scored.append((score, doc, chunk))
    scored.sort(key=lambda item: -item[0])

    results: list[dict[str, Any]] = []
    for score, doc, chunk in scored[:top_k]:
        record_hit(doc)
        text = chunk["text"]
        if doc["classification"] == "D3":
            text = _mask_sensitive(text)
        results.append(
            {
                "doc_id": doc["doc_id"],
                "title": doc["title"],
                "section": chunk["section"],
                "text": text,
                "score": round(score, 4),
                "classification": doc["classification"],
                "version": doc["version"],
                "tags": doc["tags"],
                "space": doc["space"],
            }
        )
    if not results:
        await record_gap(query, dept)
    return {"backend": _index_backend or backend_tag(), "results": results}
