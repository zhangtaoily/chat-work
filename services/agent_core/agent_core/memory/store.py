"""记忆域（PLAN P3.3，PRD 9.1-9.3）：L2 个人记忆 / L3 组织记忆。

分层（PRD 9.1）：L1 会话记忆由流水线 slots 承担（30 分钟挂起/结果缓存），
本域实现 L2（个人，用户可管理）与 L3（科室级，管理员提炼）。

边界（PRD 9.2）：
- PIPL 知情同意：L2 写入前置门禁，未同意一律拒绝；撤回同意即清除全部个人记忆
- 敏感隔离：工资/合同金额/联系方式等敏感词**前置过滤**（写入前拒绝，ARCHITECTURE 4.9）
- 可感知性：列表/删除/一键清除；MEMORY.md 式 Markdown 导出（export_md）
- 遗忘机制：90 天未引用降权（排序靠后），180 天未引用归档（惰性触发）
- 默认隐私：L2 仅本人可见；L3 仅本科室可见（org 尾段口径对齐 permissions）

检索注入（PRD 9.3）：意图识别阶段 Top-K=3 相似记忆（向量复用
knowledge.embedding 本地降级后端，阈值 0.35），命中即 touch（引用计数+1）。

写入触发（PRD 9.3）：①显式「记住这个」（memory_save 技能，source=ask）
②高频行为自动沉淀（track 同 skill+摘要 ≥3 次，source=auto_consolidated，
仅偏好/习惯类）③科室管理员从 L2 提炼升级 L3（promote，source=promoted）。

存储：进程内 + 可选 Redis 快照 `memory:snapshot`（对齐 knowledge 模式）。
"""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_core import audit
from agent_core.knowledge.embedding import cosine, embed_texts, sim_threshold

SNAPSHOT_KEY = "memory:snapshot"
RECALL_TOP_K = 3  # 注入条数上限（PRD 9.3 K=3）
CONTENT_MAX = 200  # 单条记忆正文上限（注入 ≤500 Token 的量级控制，PRD 9.3）
_DECAY_DAYS = 90  # 未引用降权阈值（PRD 9.2 遗忘机制）
_ARCHIVE_DAYS = 180  # 未引用归档阈值
_AUTOSAVE_THRESHOLD = 3  # 高频行为自动沉淀次数（PRD 9.3「同一报表连续 3 次查询」）

_entries: dict[str, dict[str, Any]] = {}
_consent: dict[str, dict[str, Any]] = {}  # user_id -> {granted, at}
_track: dict[str, int] = {}  # "{user}|{skill}|{digest}" -> 连续次数
_seq = 0
_redis_client: Any = None

# 敏感前置过滤（PRD 9.2 敏感隔离：工资/合同金额/联系方式等不入记忆层）
_SENSITIVE_RE = re.compile(
    r"工资|薪资|薪酬|绩效分|合同金额|身份证|银行卡|手机号|电话号码|密码"
)


def _now() -> datetime:
    return datetime.now(UTC)


def _now_iso() -> str:
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
    """清空记忆状态（测试隔离用）。"""
    global _seq
    _entries.clear()
    _consent.clear()
    _track.clear()
    _seq = 0


# ---- 查询（同步无 IO）----


def consent_of(user_id: str) -> dict[str, Any]:
    """PIPL 知情同意状态（PRD 9.2：未同意仅保留 L1 会话记忆）。"""
    return dict(_consent.get(user_id, {"granted": False, "at": None}))


def _apply_forgetting() -> None:
    """遗忘机制（PRD 9.2）：90 天未引用降权 / 180 天未引用归档（惰性触发）。"""
    now = _now()
    for entry in _entries.values():
        if entry["status"] != "active":
            continue
        last = datetime.fromisoformat(entry["last_used_at"] or entry["created_at"])
        idle = now - last
        if idle > timedelta(days=_ARCHIVE_DAYS):
            entry["status"] = "archived"
        elif idle > timedelta(days=_DECAY_DAYS):
            entry["decayed"] = True


def list_entries(
    *,
    user_id: str,
    dept: str,
    layer: str | None = None,
    status: str = "active",
) -> list[dict[str, Any]]:
    """可见记忆清单（PRD 9.2 可感知性）：L2 本人 + L3 本科室。"""
    _apply_forgetting()
    out = []
    for entry in _entries.values():
        if status and entry["status"] != status:
            continue
        if layer and entry["layer"] != layer:
            continue
        visible = (
            entry["layer"] == "L2" and entry["owner"] == user_id
        ) or (entry["layer"] == "L3" and entry["owner"] == dept)
        if visible:
            out.append(dict(entry))
    # 降权条目靠后，其余按最近引用排序
    out.sort(key=lambda e: (bool(e.get("decayed")), e["last_used_at"]), reverse=False)
    return out


def stats(user_id: str, dept: str) -> dict[str, int]:
    """面板统计：个人/科室条数与沉淀引用。"""
    rows = list_entries(user_id=user_id, dept=dept)
    return {
        "personal": sum(1 for e in rows if e["layer"] == "L2"),
        "dept": sum(1 for e in rows if e["layer"] == "L3"),
        "auto_consolidated": sum(1 for e in rows if e["source"] == "auto_consolidated"),
        "references": sum(e["use_count"] for e in rows),
    }


def get(entry_id: str) -> dict[str, Any] | None:
    entry = _entries.get(entry_id)
    return dict(entry) if entry else None


# ---- 写路径（async 以落快照 + 审计）----


async def _snapshot() -> None:
    r = _get_redis()
    if r is None:
        return
    stripped = {"entries": _entries, "consent": _consent, "track": _track, "seq": _seq}
    await r.set(SNAPSHOT_KEY, json.dumps(stripped, ensure_ascii=False))


async def restore() -> None:
    """启动恢复（API lifespan 调用）。"""
    global _seq
    r = _get_redis()
    if r is not None:
        raw = await r.get(SNAPSHOT_KEY)
        if raw:
            snap = json.loads(raw)
            _entries.update(snap.get("entries", {}))
            for e in _entries.values():
                e.setdefault("decayed", False)
            _consent.update(snap.get("consent", {}))
            _track.update(snap.get("track", {}))
            _seq = int(snap.get("seq", 0))


def _check_sensitive(content: str) -> None:
    """敏感前置过滤（PRD 9.2 + ARCHITECTURE 4.9：写入前拒绝而非读取时脱敏）。"""
    matched = _SENSITIVE_RE.search(content)
    if matched:
        raise ValueError(f"内容含敏感字段「{matched.group(0)}」，按记忆边界不入记忆层")


async def set_consent(*, user_id: str, granted: bool) -> dict[str, Any]:
    """PIPL 知情同意开关（PRD 9.2）：同意后启用 L2；撤回即清除全部个人记忆。"""
    _consent[user_id] = {"granted": granted, "at": _now_iso()}
    removed = 0
    if not granted:
        removed = await _purge_personal(user_id)
    await audit.record(
        "memory_consent",
        user_id=user_id,
        result="ok",
        detail=f"granted={granted} purged={removed}",
    )
    await _snapshot()
    return {"granted": granted, "purged": removed}


async def _purge_personal(user_id: str) -> int:
    doomed = [k for k, e in _entries.items() if e["layer"] == "L2" and e["owner"] == user_id]
    for k in doomed:
        del _entries[k]
    return len(doomed)


async def add(
    *,
    user_id: str,
    content: str,
    kind: str = "preference",
    source: str = "ask",
    dept: str | None = None,
    layer: str = "L2",
    by_roles: list[str] | None = None,
) -> dict[str, Any]:
    """写入记忆。

    - L2 个人：consent 门禁（未同意 ValueError）+ 敏感过滤 + 幂等（同正文不重复）
    - L3 组织：科室管理员/主管提炼（PRD 9.3），由调用方传角色核验
    """
    content = content.strip()[:CONTENT_MAX]
    if not content:
        raise ValueError("记忆内容不能为空")
    if kind not in {"preference", "params", "habit", "faq"}:
        raise ValueError("kind 仅支持 preference/params/habit/faq")
    if layer == "L2":
        if not consent_of(user_id).get("granted"):
            raise ValueError("尚未开启个人记忆（PIPL 知情同意），无法写入")
    elif layer == "L3":
        if not ({"dept_manager"} & set(by_roles or [])):
            raise ValueError("L3 组织记忆仅限科室管理员提炼写入（PRD 9.3）")
        if not dept:
            raise ValueError("L3 写入缺少科室")
    else:
        raise ValueError("layer 仅支持 L2/L3")
    _check_sensitive(content)
    owner = user_id if layer == "L2" else dept
    for entry in _entries.values():  # 幂等：同层同属同正文不重复
        if entry["layer"] == layer and entry["owner"] == owner and entry["content"] == content and entry["status"] == "active":
            return entry
    global _seq
    _seq += 1
    entry = {
        "id": f"M{_seq:04d}",
        "layer": layer,
        "owner": owner,
        "dept": dept,
        "kind": kind,
        "content": content,
        "source": source,
        "status": "active",
        "decayed": False,
        "created_by": user_id,
        "created_at": _now_iso(),
        "last_used_at": _now_iso(),
        "use_count": 0,
    }
    _entries[entry["id"]] = entry
    await audit.record(
        "memory_add",
        user_id=user_id,
        tool=f"memory/{layer}",
        params=content,
        detail=f"id={entry['id']} kind={kind} source={source}",
    )
    await _snapshot()
    return dict(entry)


async def remove(*, entry_id: str, user_id: str, dept: str, roles: list[str]) -> None:
    """删除单条（PRD 9.2 可感知性）：L2 本人；L3 科室管理员。"""
    entry = _entries.get(entry_id)
    if entry is None:
        raise ValueError("记忆条目不存在")
    allowed = (entry["layer"] == "L2" and entry["owner"] == user_id) or (
        entry["layer"] == "L3" and entry["owner"] == dept and "dept_manager" in roles
    )
    if not allowed:
        raise ValueError("无权删除该记忆（L2 仅本人 / L3 仅科室管理员）")
    del _entries[entry_id]
    await audit.record(
        "memory_delete", user_id=user_id, tool=f"memory/{entry['layer']}", params=entry["content"]
    )
    await _snapshot()


async def clear_personal(*, user_id: str) -> int:
    """一键清除个人记忆（PRD 9.2）。"""
    removed = await _purge_personal(user_id)
    await audit.record("memory_clear", user_id=user_id, detail=f"purged={removed}")
    await _snapshot()
    return removed


async def promote(
    *, entry_id: str, user_id: str, dept: str, roles: list[str]
) -> dict[str, Any]:
    """L2 → L3 升级（PRD 9.3：科室管理员/主管从 L2 或会话中主动提炼）。"""
    entry = _entries.get(entry_id)
    if entry is None:
        raise ValueError("记忆条目不存在")
    if entry["layer"] != "L2":
        raise ValueError("仅个人记忆（L2）可升级为组织记忆")
    if not ({"dept_manager"} & set(roles)):
        raise ValueError("仅科室管理员可提炼组织记忆（PRD 9.3）")
    _check_sensitive(entry["content"])
    global _seq
    _seq += 1
    org: dict[str, Any] = {
        **entry,
        "id": f"M{_seq:04d}",
        "layer": "L3",
        "owner": dept,
        "source": "promoted",
        "created_by": user_id,
        "created_at": _now_iso(),
        "last_used_at": _now_iso(),
        "use_count": 0,
        "decayed": False,
    }
    _entries[org["id"]] = org
    await audit.record(
        "memory_promote", user_id=user_id, params=entry["content"], detail=f"l2={entry_id} -> {org['id']}"
    )
    await _snapshot()
    return dict(org)


async def recall(
    *, query: str, user_id: str, dept: str, top_k: int = RECALL_TOP_K
) -> list[dict[str, Any]]:
    """Top-K 相似记忆（PRD 9.3 注入点1）：L2 本人 + L3 本科室，命中即 touch。

    向量复用 knowledge.embedding（本地降级后端阈值 0.35）；降权条目仅排序惩罚，
    归档条目不参与；命中后 use_count+1 / last_used_at 刷新（遗忘机制口径）。
    """
    if not query.strip():
        return []
    _apply_forgetting()
    candidates = [
        e
        for e in _entries.values()
        if e["status"] == "active"
        and (
            (e["layer"] == "L2" and e["owner"] == user_id)
            or (e["layer"] == "L3" and dept and e["owner"] == dept)
        )
    ]
    if not candidates:
        return []
    _, vectors = await embed_texts([query] + [e["content"] for e in candidates])
    qvec = vectors[0]
    threshold = sim_threshold("local:char-bigram")
    scored = sorted(
        (
            (cosine(qvec, vec) - (0.1 if e.get("decayed") else 0.0), e)
            for e, vec in zip(candidates, vectors[1:], strict=True)
            if cosine(qvec, vec) >= threshold
        ),
        key=lambda item: -item[0],
    )
    hits: list[dict[str, Any]] = []
    for score, entry in scored[:top_k]:
        entry["use_count"] += 1
        entry["last_used_at"] = _now_iso()
        hits.append({**entry, "score": round(max(score, 0.0), 4)})
    if hits:
        await _snapshot()
    return hits


async def track(*, user_id: str, skill: str, text: str, dept: str | None = None) -> dict[str, Any] | None:
    """高频行为自动沉淀（PRD 9.3：同一报表连续 3 次查询 → L2）。

    仅已同意 PIPL 的用户写入；source=auto_consolidated，kind=habit；
    摘要经敏感过滤，同正文幂等。达标返回新条目，未达标返回 None。
    """
    text = text.strip()[:CONTENT_MAX]
    if not text or not consent_of(user_id).get("granted"):
        return None
    key = f"{user_id}|{skill}|{text}"
    _track[key] = _track.get(key, 0) + 1
    if _track[key] < _AUTOSAVE_THRESHOLD:
        return None
    _track[key] = 0  # 归零重计：再次连续达标才沉淀下一条（同正文幂等由 add 保证）
    try:
        return await add(
            user_id=user_id,
            content=text,
            kind="habit",
            source="auto_consolidated",
            dept=dept,
        )
    except ValueError:  # 敏感词等：静默放弃自动沉淀，不阻塞对话主流程
        return None


def export_md(user_id: str, dept: str) -> str:
    """MEMORY.md 式导出（ARCHITECTURE 4.9 可迁移：用户可读可编辑可版本化）。

    按「偏好 / 常用参数 / 操作习惯 / 科室经验」分节，每条含来源与时间戳。
    """
    rows = list_entries(user_id=user_id, dept=dept)
    sections: dict[str, list[dict[str, Any]]] = {
        "偏好": [e for e in rows if e["layer"] == "L2" and e["kind"] == "preference"],
        "常用参数": [e for e in rows if e["layer"] == "L2" and e["kind"] == "params"],
        "操作习惯": [e for e in rows if e["layer"] == "L2" and e["kind"] == "habit"],
        "科室经验": [e for e in rows if e["layer"] == "L3"],
    }
    source_label = {"ask": "手动添加", "auto_consolidated": "自动沉淀", "promoted": "科室提炼"}
    lines = [f"# 我的记忆（{user_id}）", "", f"> 导出时间：{_now_iso()}；共 {len(rows)} 条", ""]
    for title, items in sections.items():
        lines.append(f"## {title}（{len(items)}）")
        if not items:
            lines.append("- （暂无）")
        for e in items:
            lines.append(
                f"- {e['content']}（{source_label.get(e['source'], e['source'])}，"
                f"引用 {e['use_count']} 次，{e['created_at'][:10]}）"
            )
        lines.append("")
    return "\n".join(lines)
