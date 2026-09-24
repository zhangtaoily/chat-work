"""Skill Store：技能市场（PLAN P2.3，PRD 3.4/3.5/5.6）。

定位：registry.SKILLS 是运行时技能定义 SSOT（内置硬编码）；本模块只管
市场层生命周期元数据与使用统计——注册/提交/评审/上架/下架/安装。

状态机（PRD 3.5 生命周期的 MVP 简化形态）：
    draft ──submit──> published            （只读技能：自动发布）
    draft ──submit──> in_review ──approve──> published
                       └──reject───> rejected ──submit──> in_review
    published ──deprecate──> deprecated    （下架后路由不再命中）

评审约束（PRD 3.4：写入类必须经信息科安全评审）：
- rw="write"：submit 后进 in_review，须 security_reviewer 角色放行
- rw="read"：submit 即自动发布（评审留痕 auto，简化 MVP）

评审单 SEC-RV-{seq:04d}（PRD 5.6.1 安全评审员职责载体），提交/评审/
下架均落审计（skill_register/skill_submit/skill_review/skill_deprecate，
"审计者也被审计"，PRD 5.6.4）。

分类（PRD 3.4）：official（信息科，全集团）/ dept（科室技能，
dept_scope 约束）/ personal（个人）。

存储：进程内 dict + 快照（skill_store:snapshot：REDIS_URL 配置走
Redis，未配置落本地文件兜底；写事件时镜像、restore 恢复）——与
audit/slots 同风格；内置技能基线为 published（官方/科室技能视为已上架）。
registry.match_skill 同步查内存状态，仅 published 技能参与路由
（下架即时降级闲聊兜底）。

使用统计（PRD 3.4）：graph format 节点对触达 MCP 的调用 record_usage
累加调用量/成功率，供广场展示与技能优化反哺（16.4 灰度观测联动）。
"""

import json
from datetime import UTC, datetime
from typing import Any

from agent_core import audit, persist

_SNAPSHOT_KEY = "skill_store:snapshot"

_STATUSES = ("draft", "submitted", "in_review", "published", "rejected", "deprecated")
_CATEGORIES = ("official", "dept", "personal")

_meta: dict[str, dict[str, Any]] = {}
_installs: dict[str, set[str]] = {}  # user_id -> 已安装技能名
_seq = 0  # 评审单号自增（SEC-RV-*）
_redis_client: Any = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _get_redis() -> Any:
    """惰性初始化 Redis（与 audit/slots 同一单例约定；无 REDIS_URL 返回 None）。"""
    global _redis_client
    if _redis_client is None:
        from agent_core.config import settings

        if settings.redis_url:
            import redis.asyncio as aioredis

            _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def _builtin_meta(name: str, skill: dict[str, Any]) -> dict[str, Any]:
    """内置技能基线元数据（官方已上架；科室技能按 dept_scope 归类）。"""
    return {
        "name": name,
        "title": skill["title"],
        "version": "1.0.0",
        "rw": skill["rw"],
        "category": "dept" if skill.get("dept_scope") else "official",
        "dept_scope": skill.get("dept_scope"),
        "status": "published",
        "submitted_by": None,
        "submitted_at": None,
        "review_id": None,
        "reviewed_by": None,
        "reviewed_at": None,
        "review_note": "内置技能基线上架",
        "approvals": [],
        "published_at": _now(),
        "stats": {"calls": 0, "success": 0, "failed": 0},
    }


def _ensure() -> None:
    """同步惰性基线：进程内元数据为空时按 registry 内置技能重建（官方已上架）。"""
    if _meta:
        return
    from agent_core.skills.registry import SKILLS

    for name, skill in SKILLS.items():
        _meta[name] = _builtin_meta(name, skill)


def reset() -> None:
    """清空市场状态并重建内置基线（测试隔离用）。"""
    global _seq
    _meta.clear()
    _installs.clear()
    _seq = 0
    _ensure()


# ---- 查询（registry.match_skill 同步消费，必须无 IO）----


def status(name: str) -> str | None:
    """技能市场状态（未注册返回 None）。"""
    _ensure()
    meta = _meta.get(name)
    return meta["status"] if meta else None


def is_published(name: str) -> bool:
    """路由联动：仅 published 技能参与意图匹配（PRD 3.4 一键安装前提同源）。"""
    return status(name) == "published"


def list_meta(
    *,
    category: str | None = None,
    q: str | None = None,
    include_unpublished: bool = False,
) -> list[dict[str, Any]]:
    """技能广场列表（默认仅 published；评审视角可看全部）。"""
    _ensure()
    items = []
    for meta in _meta.values():
        if not include_unpublished and meta["status"] != "published":
            continue
        if category and meta["category"] != category:
            continue
        if q and q not in meta["name"] and q not in meta["title"]:
            continue
        items.append(dict(meta))
    return sorted(items, key=lambda m: (-m["stats"]["calls"], m["name"]))


def detail(name: str) -> dict[str, Any] | None:
    """技能市场元数据详情（技能定义由 API 层从 registry 合并）。"""
    _ensure()
    meta = _meta.get(name)
    return dict(meta) if meta else None


# ---- 生命周期（写路径，async 以落 Redis 快照）----


async def _snapshot() -> None:
    """写事件后镜像快照（无 Redis 落本地文件兜底；stats 一并保存，重启不清零）。"""
    payload = {
        "meta": _meta,
        "installs": {u: sorted(v) for u, v in _installs.items()},
        "seq": _seq,
    }
    r = _get_redis()
    if r is None:
        await persist.write_json(_SNAPSHOT_KEY, payload)
        return
    await r.set(_SNAPSHOT_KEY, json.dumps(payload, ensure_ascii=False))


async def restore() -> None:
    """启动恢复：Redis 快照优先，无 Redis 读本地文件兜底；否则内置基线（API lifespan 调用）。"""
    snap: dict[str, Any] | None = None
    r = _get_redis()
    if r is not None:
        raw = await r.get(_SNAPSHOT_KEY)
        if raw:
            snap = json.loads(raw)
    if snap is None:
        snap = await persist.read_json(_SNAPSHOT_KEY)
    if snap:
        _meta.update(snap.get("meta", {}))
        for m in _meta.values():  # 旧快照无 approvals 字段（双人复核引入前）兜底
            m.setdefault("approvals", [])
        _installs.update({u: set(v) for u, v in snap.get("installs", {}).items()})
        global _seq
        _seq = int(snap.get("seq", 0))
        return
    _ensure()


async def register(
    *,
    name: str,
    title: str,
    rw: str,
    category: str = "dept",
    dept_scope: str | None = None,
    version: str = "0.1.0",
    by: str = "",
) -> dict[str, Any]:
    """技能注册（PRD 3.5 生命周期起点）：新建 draft 元数据，等待提交上架。

    与 registry.SKILLS 解耦：注册期技能不参与路由（定义另行注入），
    仅市场目录可见。重复注册拒绝。
    """
    _ensure()
    if name in _meta:
        raise ValueError(f"技能 {name} 已注册")
    if rw not in {"read", "write"}:
        raise ValueError("rw 仅支持 read/write")
    if category not in _CATEGORIES:
        raise ValueError(f"category 仅支持 {'/'.join(_CATEGORIES)}")
    meta = {
        "name": name,
        "title": title,
        "version": version,
        "rw": rw,
        "category": category,
        "dept_scope": dept_scope,
        "status": "draft",
        "submitted_by": None,
        "submitted_at": None,
        "review_id": None,
        "reviewed_by": None,
        "reviewed_at": None,
        "review_note": None,
        "approvals": [],
        "published_at": None,
        "stats": {"calls": 0, "success": 0, "failed": 0},
    }
    _meta[name] = meta
    await audit.record(
        "skill_register",
        tool="skill_store",
        params={"skill": name, "rw": rw, "category": category},
        user_id=by or None,
        result="ok",
        detail=f"注册技能「{title}」",
    )
    await _snapshot()
    return dict(meta)


async def submit(name: str, *, by: str, note: str = "") -> dict[str, Any]:
    """提交上架（科室管理员提交，PRD 3.4）：只读自动发布，写入进评审队列。"""
    _ensure()
    meta = _meta.get(name)
    if meta is None:
        raise ValueError(f"技能 {name} 未注册")
    if meta["status"] not in {"draft", "rejected"}:
        raise ValueError(f"技能 {name} 当前状态 {meta['status']} 不可提交")
    global _seq
    meta["submitted_by"] = by
    meta["submitted_at"] = _now()
    meta["approvals"] = []  # 重新评审清空历史批准（双人复核从零起算）
    if meta["rw"] == "write":
        _seq += 1
        meta["status"] = "in_review"
        meta["review_id"] = f"SEC-RV-{_seq:04d}"
        detail_text = f"写入类技能提交安全评审（{meta['review_id']}）"
    else:
        meta["status"] = "published"
        meta["review_id"] = None
        meta["reviewed_by"] = None
        meta["reviewed_at"] = None
        meta["review_note"] = "只读技能自动发布"
        meta["published_at"] = _now()
        detail_text = "只读技能自动发布"
    if note:
        detail_text += f"；备注：{note}"
    await audit.record(
        "skill_submit",
        tool="skill_store",
        params={"skill": name, "status": meta["status"], "review_id": meta["review_id"]},
        user_id=by or None,
        result="ok",
        detail=detail_text,
    )
    await _snapshot()
    return dict(meta)


async def review(name: str, *, approve: bool, by: str, note: str = "") -> dict[str, Any]:
    """安全评审（信息科 security_reviewer，PRD 5.6.1）：通过/驳回均留痕。

    双人复核（PRD 5.6.4：技能评审通过需双人）：第一名安全评审员通过后
    保持 in_review 并记录 approvals[0]（审计 result=first_approved），
    第二名不同评审员通过才发布；驳回单人即生效并清空已积累的批准。
    同一评审人重复通过拒绝（防自批自核）。
    """
    _ensure()
    meta = _meta.get(name)
    if meta is None:
        raise ValueError(f"技能 {name} 未注册")
    if meta["status"] != "in_review":
        raise ValueError(f"技能 {name} 当前状态 {meta['status']} 不在评审中")
    if not approve:
        meta["reviewed_by"] = by
        meta["reviewed_at"] = _now()
        meta["review_note"] = note or "驳回"
        meta["approvals"] = []
        meta["status"] = "rejected"
        await audit.record(
            "skill_review",
            tool="skill_store",
            params={"skill": name, "review_id": meta["review_id"], "approve": False},
            user_id=by or None,
            result="rejected",
            detail=f"评审单 {meta['review_id']} 驳回{('：' + note) if note else ''}",
        )
        await _snapshot()
        return dict(meta)
    if any(a["by"] == by for a in meta["approvals"]):
        raise ValueError("技能评审通过需两名不同安全评审员复核，请由第二评审人操作")
    meta["approvals"].append({"by": by, "at": _now(), "note": note})
    meta["reviewed_by"] = by
    meta["reviewed_at"] = _now()
    meta["review_note"] = note or "通过"
    if len(meta["approvals"]) < 2:
        meta["review_note"] = note or "通过（待第二评审人复核）"
        await audit.record(
            "skill_review",
            tool="skill_store",
            params={"skill": name, "review_id": meta["review_id"], "approve": True},
            user_id=by or None,
            result="first_approved",
            detail=(
                f"评审单 {meta['review_id']} 第一复核通过（{by}），"
                "待第二评审人复核（PRD 5.6.4 双人复核）"
            ),
        )
        await _snapshot()
        return dict(meta)
    meta["status"] = "published"
    meta["published_at"] = _now()
    await audit.record(
        "skill_review",
        tool="skill_store",
        params={
            "skill": name,
            "review_id": meta["review_id"],
            "approve": True,
            "signers": [a["by"] for a in meta["approvals"]],
        },
        user_id=by or None,
        result="approved",
        detail=(
            f"评审单 {meta['review_id']} 双人复核通过"
            f"（{'、'.join(a['by'] for a in meta['approvals'])}）"
            f"{('：' + note) if note else ''}"
        ),
    )
    await _snapshot()
    return dict(meta)


async def deprecate(name: str, *, by: str, note: str = "") -> dict[str, Any]:
    """下架（PRD 3.5 生命周期收尾）：历史调用记录保留审计（PRD 10 覆盖）。"""
    _ensure()
    meta = _meta.get(name)
    if meta is None:
        raise ValueError(f"技能 {name} 未注册")
    if meta["status"] != "published":
        raise ValueError(f"技能 {name} 当前状态 {meta['status']} 不可下架")
    meta["status"] = "deprecated"
    meta["review_note"] = note or "下架"
    await audit.record(
        "skill_deprecate",
        tool="skill_store",
        params={"skill": name},
        user_id=by or None,
        result="ok",
        detail=f"下架技能「{meta['title']}」{('：' + note) if note else ''}",
    )
    await _snapshot()
    return dict(meta)


# ---- 安装（PRD 3.4 一键安装：权限校验在 API 层，store 只记清单）----


async def install(name: str, *, user_id: str) -> None:
    _ensure()
    if name not in _meta:
        raise ValueError(f"技能 {name} 未注册")
    _installs.setdefault(user_id, set()).add(name)
    await _snapshot()


async def uninstall(name: str, *, user_id: str) -> None:
    _ensure()
    _installs.get(user_id, set()).discard(name)
    await _snapshot()


def installed_of(user_id: str) -> list[dict[str, Any]]:
    """我的技能（安装清单 + 市场元数据快照）。"""
    _ensure()
    return [dict(_meta[n]) for n in sorted(_installs.get(user_id, set())) if n in _meta]


# ---- 使用统计（PRD 3.4：调用量/成功率反哺优化）----


def record_usage(name: str, *, ok: bool) -> None:
    """技能调用埋点（graph format 节点同步调用，无 IO）。"""
    _ensure()
    meta = _meta.get(name)
    if meta is None:
        return
    meta["stats"]["calls"] += 1
    if ok:
        meta["stats"]["success"] += 1
    else:
        meta["stats"]["failed"] += 1
