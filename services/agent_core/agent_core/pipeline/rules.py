"""规则兜底：意图识别 + 参数抽取 + 工作日时长计算（LLM 未配置时的冒烟路径）。

生产接入 LLM（OpenAI 兼容 /vLLM）后，intent/extract 节点优先走 LLM，
本模块保留为降级路径与 LLM 输出的后校验器（架构不变）。

P2.1 业务系统扩展（PRD 6.1）：CRM/ERP/WMS 场景的消息抽取
（客户关键词/商品明细/交期/付款方式/单号/期间）与查询结果渲染辅助。
P2.2 科室扩展（PRD 6.2）：五科室工作台只读视图的渲染辅助
（备料齐套/到货计划/效期批次/仓库概览/数据巡检）。
"""

import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

# 标准工作日半天槽边界（OA 请假 0.5 天粒度口径，PRD 5.2）
_AM_START = time(9, 0)
_AM_END = time(12, 0)
_PM_START = time(13, 0)
_PM_END = time(18, 0)

# 假期类型中文关键词 → 协议枚举（与 oa__*.json 枚举对齐，CI 校验）
LEAVE_TYPE_KEYWORDS: dict[str, str] = {
    "年假": "annual",
    "调休": "comp",
    "病假": "sick",
    "事假": "personal",
    "婚假": "marriage",
    "丧假": "bereavement",
    "产假": "maternity",
}

LEAVE_TYPE_LABELS: dict[str, str] = {
    "annual": "年假",
    "comp": "调休",
    "sick": "病假",
    "personal": "事假",
    "marriage": "婚假",
    "bereavement": "丧假",
    "maternity": "产假",
}

_WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6}

_ISO_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?")
_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*天")
_REASON = re.compile(r"(?:事由|原因|因为)[：:\s]*(.+?)(?:[。；;]|$)")

# 行内审批（PRD 5.2 场景 2）：动作词 + 目标（"第 N 条" / 单号直达 / "这条"承接上轮）
# 注意顺序："不同意"包含"同意"子串，须先判驳回词组
_REJECT_ACTION = re.compile(r"驳回|拒绝|否决|不同意|reject")
_APPROVE_ACTION = re.compile(r"同意|批准|通过|approve")
_APPROVAL_INDEX = re.compile(r"第\s*(\d+)\s*条")
_APPROVAL_ID = re.compile(r"AP-\d{4}-\d+", re.IGNORECASE)
# 指代词："这条驳回，原因是 X"（PRD 5.2 场景 2 验收句）承接上轮定位的条目
_THIS_ONE = re.compile(r"这一条|这条|此条|该条")
# 审批附言（驳回原因）：「原因是 X / 原因：X / 理由是 X / 因为 X」，取至句末标点
_APPROVAL_COMMENT = re.compile(r"(?:原因是|原因[：:]|理由是|理由[：:]|因为)\s*(.+?)(?:[。；;]|$)")


def extract_leave_type(message: str) -> str | None:
    for kw, code in LEAVE_TYPE_KEYWORDS.items():
        if kw in message:
            return code
    return None


def parse_approval_action(message: str) -> tuple[str, str] | None:
    """解析行内审批意图：返回 (action, target_spec)。

    action: approve/reject；target_spec: "id:AP-..."（单号直达）、
    "index:N"（最近待办列表序号）、"last"（"这条"承接上轮定位的条目）。
    无审批动作词返回 None（读列表路径）；有动作但无目标返回 (action, "")，
    由 extract 定位失败走补问。
    """
    if _REJECT_ACTION.search(message):
        action = "reject"
    elif _APPROVE_ACTION.search(message):
        action = "approve"
    else:
        return None
    m = _APPROVAL_ID.search(message)
    if m:
        return action, f"id:{m.group(0).upper()}"
    m = _APPROVAL_INDEX.search(message)
    if m:
        return action, f"index:{int(m.group(1))}"
    if _THIS_ONE.search(message):
        return action, "last"
    return action, ""


def extract_approval_comment(message: str) -> str | None:
    """提取行内审批附言（驳回原因等，PRD 5.2 场景 2）。

    支持「原因是 X / 原因：X / 理由是 X / 因为 X」句式，取至句末标点；
    无附言返回 None（comment 保持默认空值）。
    """
    m = _APPROVAL_COMMENT.search(message)
    if m:
        return m.group(1).strip()
    return None


def _today() -> date:
    """本地时区当天（业务日期口径，与用户输入的本地日期对齐）。"""
    return datetime.now().astimezone().date()


def _parse_cn_dates(message: str) -> list[str]:
    """解析中文相对日期（明天/后天/下周X）→ ISO 日期串。"""
    out: list[str] = []
    today = _today()
    if "明天" in message:
        out.append((today + timedelta(days=1)).isoformat())
    if "后天" in message:
        out.append((today + timedelta(days=2)).isoformat())
    m = re.search(r"下周([一二三四五六日])", message)
    if m:
        target_wd = _WEEKDAY_CN[m.group(1)]
        days_ahead = (target_wd - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        out.append((today + timedelta(days=days_ahead)).isoformat())
    return out


def extract_times(message: str) -> list[str]:
    """抽取起止时间（ISO 优先，回退中文相对日期）。"""
    iso = _ISO_DATETIME.findall(message)
    if iso:
        return iso
    return _parse_cn_dates(message)


def normalize_time(value: str, end: bool = False) -> str:
    """时间规格化为 ISO 8601 datetime（缺省时刻：开始 09:00 / 结束 18:00）。"""
    v = value.replace("T", " ").strip()
    default = "18:00:00" if end else "09:00:00"
    if len(v) == 10:  # 仅日期
        v = f"{v} {default}"
    elif len(v) == 16:  # 日期 + HH:MM
        v = f"{v}:00"
    return datetime.fromisoformat(v).isoformat()


def extract_duration(message: str) -> float | None:
    m = _DURATION.search(message)
    if m:
        return float(m.group(1))
    return None


def extract_reason(message: str) -> str | None:
    m = _REASON.search(message)
    if m:
        return m.group(1).strip()
    return None


def calculate_workdays(start_time: str, end_time: str) -> float:
    """按起止时间计算工作日时长（0.5 天粒度，PRD 5.2）。

    口径：标准工作日分上午（09:00-12:00）/ 下午（13:00-18:00）两个半天槽，
    请假区间 [start, end] 与任一半天槽有交集即计入该槽 0.5 天；周末不计。
    整天输入（09:00 起 / 18:00 止）结果与整天口径一致，向下兼容。
    """
    start = datetime.fromisoformat(start_time)
    end = datetime.fromisoformat(end_time)
    if end < start:
        raise ValueError("结束时间早于开始时间")
    days = 0.0
    cur = start.date()
    while cur <= end.date():
        if cur.weekday() < 5:  # 排除周末
            day = cur
            am_start = datetime.combine(day, _AM_START)
            am_end = datetime.combine(day, _AM_END)
            pm_start = datetime.combine(day, _PM_START)
            pm_end = datetime.combine(day, _PM_END)
            if start < am_end and end > am_start:
                days += 0.5
            if start < pm_end and end > pm_start:
                days += 0.5
        cur += timedelta(days=1)
    return days


def extract_leave_fields(message: str) -> dict[str, Any]:
    """从消息抽取请假草稿字段（来源标记 ask/computed，见 draft_card 事件）。"""
    draft: dict[str, Any] = {}
    leave_type = extract_leave_type(message)
    if leave_type:
        draft["leave_type"] = {"value": leave_type, "source": "ask"}
    times = extract_times(message)
    if len(times) >= 1:
        draft["start_time"] = {"value": normalize_time(times[0]), "source": "ask"}
    if len(times) >= 2:
        draft["end_time"] = {"value": normalize_time(times[1], end=True), "source": "ask"}
    duration = extract_duration(message)
    if duration is not None:
        draft["duration_days"] = {"value": duration, "source": "ask"}
    reason = extract_reason(message)
    if reason:
        draft["reason"] = {"value": reason, "source": "ask"}
    return draft


def merge_leave_draft(message: str, pending: dict[str, Any] | None) -> dict[str, Any]:
    """补问轮合并：本条消息提取的字段覆盖挂起草稿（分段收集语义，PRD 5.2）。

    单时间分配启发式：pending 已有开始时间但缺结束时间时，补答的孤立时间
    视为结束时间（如补问答"2026-09-24 09:00"），避免误覆盖开始时间；
    消息含"结束"字样或给出两个时间时按显式语义分配。
    """
    draft: dict[str, Any] = dict(pending) if pending else {}
    new = extract_leave_fields(message)
    times = extract_times(message)
    new.pop("start_time", None)
    new.pop("end_time", None)
    if len(times) >= 2:
        new["start_time"] = {"value": normalize_time(times[0]), "source": "ask"}
        new["end_time"] = {"value": normalize_time(times[1], end=True), "source": "ask"}
    elif len(times) == 1:
        value = {"value": normalize_time(times[0]), "source": "ask"}
        if "结束" in message or pending and "start_time" in pending and "end_time" not in pending:
            new["end_time"] = value
        else:
            new["start_time"] = value
    draft.update(new)
    # 补答整句视为事由：收集轮中本条消息未提取到其他字段且事由缺失时，
    # 将整句回复作为事由（如问"请假事由是什么"答"家里有事需要回去处理"）
    if pending and "reason" not in draft and not new and not times and message.strip():
        draft["reason"] = {"value": message.strip(), "source": "ask"}
    return draft


def is_bi_followup(message: str, prev_result: Any) -> bool:
    """BI 追加维度判定（PRD 5.2 场景 3「按产品类别拆分一下」）。

    存在上轮 BI 查询结果（dict 含 kpi）且本条消息只谈拆分/维度、
    不含新指标词时，视为追加追问：拼接上轮查询原文继承口径。
    """
    if not isinstance(prev_result, dict) or "kpi" not in prev_result:
        return False
    has_split = any(k in message for k in ("拆分", "分布", "对比", "按", "趋势", "维度"))
    has_metric = any(k in message for k in ("销售", "业绩", "营收"))
    return has_split and not has_metric


# OA 操作类型中文标签（与 mcp_oa mock 模板对齐；真实环境按 OA 字典扩展）
ACTIVITY_TYPE_LABELS: dict[str, str] = {
    "submit_leave": "请假提交",
    "approve": "审批处理",
    "query_balance": "假期查询",
    "query_bi": "数据查询",
}


def build_weekly_markdown(items: list[dict[str, Any]]) -> str:
    """聚合 OA 操作记录为 Markdown 周报草稿（PRD 5.2 场景 4）。

    结构：标题（周期）→ 本周工作概要（按类型计数）→ 操作明细表 → 下周计划占位。
    IM 群发 Phase 1 降级为「编辑后复制/下载」（PRD 场景 4 前置依赖②）。
    """
    dates = sorted(str(it.get("occurred_at", ""))[:10] for it in items if it.get("occurred_at"))
    if dates:
        period = f"{dates[0]} ~ {dates[-1]}"
    else:
        today = _today()
        period = f"{today - timedelta(days=6)} ~ {today}"
    lines = [f"# 个人工作周报（{period}）", "", "## 本周工作概要", ""]
    if items:
        counts: dict[str, int] = {}
        for it in items:
            t = str(it.get("action_type", "other"))
            counts[t] = counts.get(t, 0) + 1
        for t, n in counts.items():
            lines.append(f"- {ACTIVITY_TYPE_LABELS.get(t, t)}：{n} 项")
        lines += ["", "## 本周操作明细", "", "| 日期 | 类型 | 内容 |", "| --- | --- | --- |"]
        for it in items:
            occurred = str(it.get("occurred_at", ""))[:10]
            label = ACTIVITY_TYPE_LABELS.get(str(it.get("action_type", "")), "其他")
            summary = str(it.get("summary", "")).replace("|", "\\|")
            lines.append(f"| {occurred} | {label} | {summary} |")
    else:
        lines.append("-（本周暂无 OA 操作记录）")
    lines += ["", "## 下周计划", "", "-（待补充）", ""]
    return "\n".join(lines)


# ---- P2.1 业务系统扩展：CRM/ERP/WMS 抽取与渲染（PRD 6.1）----

# CRM 枚举关键词映射（与 mcp-crm ORDER_TYPES/PAYMENT_TERMS 枚举对齐，CI 校验）
PAYMENT_TERM_KEYWORDS: dict[str, str] = {"预付": "prepay", "月结30": "net30", "月结60": "net60"}
PAYMENT_TERM_LABELS: dict[str, str] = {"prepay": "预付", "net30": "月结 30 天", "net60": "月结 60 天"}
ORDER_TYPE_KEYWORDS: dict[str, str] = {"样品": "sample"}
ORDER_TYPE_LABELS: dict[str, str] = {"standard": "标准销售", "sample": "样品单"}

CRM_ORDER_STATUS_LABELS: dict[str, str] = {
    "submitted": "已提交",
    "approving": "审批中",
    "producing": "生产备货中",
    "delayed": "延期",
    "shipped": "已发货",
    "done": "已完成",
}
PROGRESS_STATE_LABELS: dict[str, str] = {"done": "已完成", "doing": "进行中", "todo": "未开始"}
PO_STATUS_LABELS: dict[str, str] = {
    "draft": "草稿",
    "approved": "已审批",
    "in_transit": "在途",
    "received": "已入库",
}
WMS_STATUS_LABELS: dict[str, str] = {"pending": "待处理", "done": "已完成", "blocked": "阻塞"}
ALERT_TYPE_LABELS: dict[str, str] = {"low_stock": "低库存", "expiry": "效期临期"}
# P3.1 MES 工单状态（PLAN P3.1，PRD 7.1；与 mes__*.json 枚举对齐）
MES_STATUS_LABELS: dict[str, str] = {
    "pending": "未开工",
    "running": "生产中",
    "done": "已完工",
    "closed": "已结案",
}
# P3.1 U8 科目关键词映射（PLAN P3.1：口语「银行/应收」→ 科目名模糊匹配）
_GL_SUBJECT_KEYWORDS: dict[str, str] = {
    "银行": "银行",
    "应收": "应收",
    "应付": "应付",
    "收入": "收入",
    "成本": "成本",
    "税费": "税费",
}

_SKU_TOKEN = re.compile(r"SKU-[A-Za-z0-9]+")
_ORDER_NO = re.compile(r"SO\d{8,}", re.IGNORECASE)
# 明细段数量：「x10」「×10」「数量 10」「10 件/个/台/套/只」
_QTY_IN_SEGMENT = re.compile(
    r"[xX×]\s*(\d+(?:\.\d+)?)|数量\s*:?\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:件|个|台|套|只)"
)
# 明细段单价：「单价 50」「@50」「50 元/块」
_PRICE_IN_SEGMENT = re.compile(r"(?:单价|价格|@)\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:元|块)")
# 客户关键词：显式引导词句式（下订单/查档案）
_CUSTOMER_KW_PATTERNS = (
    re.compile(r"(?:给|对|向|为)\s*([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,24}?)(?:下|录|开|订|[，,。；;]|$)"),
    re.compile(r"(?:(?:查一下|查|看看?|找)\s*)?([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,24}?)(?:的)?(?:客户)?(?:360|档案|资料|信息|视图)"),
)
# 补问轮裸回复兜底排除的泛词
_CUSTOMER_STOPWORDS = {"下订单", "下单", "订货", "订单录入", "销售订单", "订单"}
# ERP 期间抽取（YYYY-MM / YYYY年M月）
_PERIOD_TOKEN = re.compile(r"(20\d{2})[-/年](\d{1,2})")
# P3.3 显式记忆句式（PRD 9.3：「记住这个」等引导词 + 内容）
_MEMORY_PATTERNS = (
    re.compile(r"(?:请|麻烦)?(?:帮我)?记住(?:这个|一下|这条)?[：:，,]?\s*(.+)", re.DOTALL),
    re.compile(r"(?:请|麻烦)?帮我?记一下[：:，,]?\s*(.+)", re.DOTALL),
)


def extract_memory_content(message: str) -> str | None:
    """抽取显式记忆内容（PRD 9.3 写入触发①「用户显式说记住这个」）。

    引导词前缀剥离后取剩余正文；未命中返回 None（普通消息不误伤）。
    """
    for pat in _MEMORY_PATTERNS:
        m = pat.search(message)
        if m:
            content = (m.group(m.lastindex) or "").strip().strip("。.！!？? ")
            if content:
                return content
    return None


def extract_customer_keyword(message: str, allow_bare: bool = True) -> str | None:
    """抽取客户名称关键词（CRM 模糊匹配输入，PRD 6.1.1）。

    三级句式：①「给/对/向/为 X 下订单」②「（查）X 的客户档案/360」
    ③补问轮裸回复（allow_bare 时整句去标点后即客户名，排除泛词与 SKU 段；
    客户已锁定后置 False——联系人点名等裸回复不再当作改选客户）。
    """
    for pat in _CUSTOMER_KW_PATTERNS:
        m = pat.search(message)
        if m:
            kw = m.group(1).strip()
            if kw and kw not in _CUSTOMER_STOPWORDS:
                return kw
    if not allow_bare:
        return None
    stripped = re.sub(r"[，,。；;！!？?\s（）()]", "", message)
    if stripped and 2 <= len(stripped) <= 24 and not _SKU_TOKEN.search(stripped) and stripped not in _CUSTOMER_STOPWORDS:
        return stripped
    return None


def parse_crm_items(message: str) -> list[dict[str, Any]]:
    """解析商品明细（PRD 6.1.1 表单行）：按 SKU token 分段提取数量与单价。

    支持「SKU-A x10 单价 50」「SKU-A 10 件 50 元」「SKU-A×10@50」等写法；
    单价可缺省（extract 默认值链用客户最近成交价补齐，PRD 6.1.1）。
    """
    items: list[dict[str, Any]] = []
    matches = list(_SKU_TOKEN.finditer(message))
    for i, m in enumerate(matches):
        seg = message[m.end(): matches[i + 1].start() if i + 1 < len(matches) else len(message)]
        seg = re.split(r"[，,。；;！!？?\n]", seg)[0]
        qty: float | None = None
        price: float | None = None
        mq = _QTY_IN_SEGMENT.search(seg)
        if mq:
            qty = float(mq.group(1) or mq.group(2) or mq.group(3))
        mp = _PRICE_IN_SEGMENT.search(seg)
        if mp:
            price = float(mp.group(1) or mp.group(2))
        items.append({"sku": m.group(0).upper(), "qty": qty, "price": price})
    return items


def extract_delivery_date(message: str) -> str | None:
    """抽取交货日期：ISO 日期优先，「月底」→ 当月最后一天。"""
    if "月底" in message:
        today = _today()
        nxt = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        return (nxt - timedelta(days=1)).isoformat()
    times = extract_times(message)
    return times[0][:10] if times else None


def default_delivery_date() -> str:
    """交货日期默认值：T+7（PRD 6.1.1 默认值链）。"""
    return (_today() + timedelta(days=7)).isoformat()


def extract_payment_term(message: str) -> str | None:
    for kw, code in PAYMENT_TERM_KEYWORDS.items():
        if kw in message:
            return code
    return None


def extract_order_type(message: str) -> str | None:
    for kw, code in ORDER_TYPE_KEYWORDS.items():
        if kw in message:
            return code
    return None


def extract_order_no(message: str) -> str | None:
    m = _ORDER_NO.search(message)
    return m.group(0).upper() if m else None


def extract_sku(message: str) -> str | None:
    m = _SKU_TOKEN.search(message)
    return m.group(0).upper() if m else None


def extract_period(message: str) -> str | None:
    """抽取会计期间（YYYY-MM）：「2026-09」「2026年9月」等写法。"""
    m = _PERIOD_TOKEN.search(message)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    return None


def current_period() -> str:
    """默认会计期间：当月（如 2026-09）。"""
    return f"{_today():%Y-%m}"


def extract_po_status(message: str) -> str | None:
    if "在途" in message:
        return "in_transit"
    if "已入库" in message or "已到货" in message:
        return "received"
    return None


def extract_work_order_no(message: str) -> str | None:
    """抽取 MES 工单号（MO + 8 位以上数字，对齐 _ORDER_NO 的 SO 模式）。"""
    m = re.search(r"MO\d{8,}", message, re.IGNORECASE)
    return m.group(0).upper() if m else None


def extract_voucher_no(message: str) -> str | None:
    """抽取 U8 凭证号（记/转/收/付 + 横杠 + 数字，如 记-2026090128）。"""
    m = re.search(r"(?:记|转|收|付)\s*[-－]?\s*\d{6,}", message)
    return m.group(0).replace("－", "-").replace(" ", "") if m else None


def extract_gl_subject(message: str) -> str | None:
    """抽取 U8 科目关键词（口语「银行存款/应收」→ 余额表科目模糊匹配）。"""
    for kw in _GL_SUBJECT_KEYWORDS:
        if kw in message:
            return kw
    return None


def merge_crm_order_draft(message: str, pending: dict[str, Any] | None) -> dict[str, Any]:
    """CRM 订单补问轮合并：本条消息抽取字段覆盖挂起草稿（分段收集，PRD 6.1.1）。

    客户关键词更新时作废旧命中（customer_id/候选/未命中标记），
    触发下一轮 extract 重新匹配。
    """
    draft: dict[str, Any] = dict(pending) if pending else {}
    # 客户已锁定后裸回复不再视为改选客户（联系人点名等回复，PRD 6.1.1）
    locked = "customer_id" in draft
    kw = extract_customer_keyword(message, allow_bare=not locked)
    if kw:
        old_kw = draft.get("customer_kw", {}).get("value")
        draft["customer_kw"] = {"value": kw, "source": "ask"}
        if kw != old_kw:
            # 多候选补问轮全名点名（如回复完整公司名）：直接锁定，不作废候选
            chosen = match_candidate_customer(
                message, (draft.get("customer_candidates", {}) or {}).get("value") or []
            )
            if chosen:
                draft["customer_id"] = {"value": chosen["customer_id"], "source": "ask"}
                draft["customer_name"] = {"value": chosen["name"], "source": "ask"}
                draft.pop("customer_candidates", None)
                draft.pop("customer_not_found", None)
            else:
                # 真实换客户：作废旧命中（customer_id/候选/未命中标记），
                # 触发下一轮 extract 重新匹配
                for key in ("customer_id", "customer_name", "customer_candidates", "customer_not_found"):
                    draft.pop(key, None)
                draft.pop("_crm_profile_done", None)
    items = parse_crm_items(message)
    if items:
        # 同 SKU 行增量更新（补数量/单价缺失字段），新 SKU 追加；避免整表覆盖
        old_items = list((draft.get("items", {}) or {}).get("value") or [])
        if old_items:
            by_sku = {it["sku"]: it for it in old_items}
            for it in items:
                old = by_sku.get(it["sku"])
                if old:
                    if it.get("qty") is not None:
                        old["qty"] = it["qty"]
                    if it.get("price") is not None:
                        old["price"] = it["price"]
                else:
                    old_items.append(it)
            draft["items"] = {"value": old_items, "source": "ask"}
        else:
            draft["items"] = {"value": items, "source": "ask"}
    delivery = extract_delivery_date(message)
    if delivery:
        draft["delivery_date"] = {"value": delivery, "source": "ask"}
    payment = extract_payment_term(message)
    if payment:
        draft["payment_term"] = {"value": payment, "source": "ask"}
    order_type = extract_order_type(message)
    if order_type:
        draft["order_type"] = {"value": order_type, "source": "ask"}
    return draft


def match_candidate_contact(message: str, contacts: list[str]) -> str | None:
    """多联系人补问轮：本条消息点名的候选联系人（PRD 6.1.1 多联系人必问）。"""
    for c in contacts:
        if c and c in message:
            return c
    return None


def match_candidate_customer(message: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """多客户命中补问轮：本条消息点名的候选客户（按名称/编码包含匹配）。"""
    for c in candidates:
        if c.get("name") and c["name"] in message:
            return c
        if c.get("customer_id") and c["customer_id"] in message:
            return c
    return None


def build_crm_order_confirm_fields(draft: dict[str, Any]) -> dict[str, str]:
    """CRM 订单确认卡展示字段（items 摊平为明细文本 + 合计金额）。"""

    def _v(key: str) -> str:
        val = draft.get(key, {}).get("value")
        return "" if val is None else str(val)

    items = draft.get("items", {}).get("value") or []
    total = Decimal(0)
    lines: list[str] = []
    for it in items:
        if it.get("price") is None:
            continue
        amount = Decimal(str(it["qty"])) * Decimal(str(it["price"]))
        total += amount
        lines.append(f"{it['sku']} × {it['qty']:g} @ {it['price']:g} = {amount:.2f} 元")
    return {
        "客户": _v("customer_name") or _v("customer_id"),
        "单据类型": ORDER_TYPE_LABELS.get(_v("order_type"), _v("order_type")),
        "商品明细": "；".join(lines) or "（待补充）",
        "合计金额": f"{total:,.2f} 元",
        "联系人": _v("contact") or "（待补充）",
        "收货地址": _v("address") or "（待补充）",
        "期望交货": _v("delivery_date"),
        "付款方式": PAYMENT_TERM_LABELS.get(_v("payment_term"), _v("payment_term")),
    }


def build_customer_360_text(profile: dict[str, Any]) -> str:
    """客户 360 文本摘要（档案 + 联系人 + 付款条件 + 最近成交价 + 近期订单）。"""
    lines = [
        f"{profile.get('name', '')}（{profile.get('customer_id', '')}）",
        "信用等级：{level}｜付款条件：{term}".format(
            level=profile.get("credit_level", "-"),
            term=PAYMENT_TERM_LABELS.get(profile.get("payment_term", ""), profile.get("payment_term", "-")),
        ),
        f"默认收货地址：{profile.get('default_address', '-')}",
        "在册联系人：" + ("、".join(profile.get("contacts") or []) or "-"),
    ]
    prices = profile.get("last_prices") or {}
    if prices:
        lines.append("最近成交价：" + "、".join(f"{k} {v:g} 元" for k, v in prices.items()))
    orders = profile.get("recent_orders") or []
    if orders:
        recent = "；".join(
            f"{o.get('order_no', '')}（{CRM_ORDER_STATUS_LABELS.get(o.get('status', ''), o.get('status', ''))}）"
            for o in orders[:3]
        )
        lines.append(f"近期订单 {len(orders)} 笔：{recent}")
    return "\n".join(lines)


def build_order_progress_text(data: dict[str, Any]) -> str:
    """跟单进度文本：状态 + 节点推进 + 异常（PRD 6.1.2）。"""
    status = CRM_ORDER_STATUS_LABELS.get(str(data.get("status", "")), str(data.get("status", "")))
    lines = [
        f"订单 {data.get('order_no', '')}（{data.get('customer_name', '')}）当前状态：{status}",
        f"合计金额 {data.get('total_amount', 0):,.2f} 元，期望交货 {data.get('delivery_date', '-')}",
    ]
    lines += [
        f"- [{PROGRESS_STATE_LABELS.get(str(p.get('state', '')), str(p.get('state', '')))}] "
        f"{p.get('node', '')}" + (f"：{p['note']}" if p.get("note") else "")
        for p in data.get("progress") or []
    ]
    lines += [f"异常：{exc}" for exc in data.get("exceptions") or []]
    return "\n".join(lines)


def build_inventory_lines(rows: list[dict[str, Any]]) -> list[str]:
    """ERP 库存行文本（现存量/可用量/在途/安全库存 + 缺料标记）。"""
    return [
        "{sku} {name} @{wh}：可用 {avail:g}（现存 {on_hand:g}，在途 {transit:g}，安全库存 {safety:g}）".format(
            sku=r.get("sku", ""),
            name=r.get("sku_name", ""),
            wh=r.get("warehouse", ""),
            avail=float(r.get("available", 0)),
            on_hand=float(r.get("on_hand", 0)),
            transit=float(r.get("inbound_transit", 0)),
            safety=float(r.get("safety_stock", 0)),
        ) + ("（低于安全库存）" if r.get("below_safety") else "")
        for r in rows
    ]


def build_po_lines(rows: list[dict[str, Any]]) -> list[str]:
    """ERP 采购单行文本（供应商/数量/金额/ETA/状态）。"""
    return [
        "{no} {sku}×{qty:g}（{supplier}）：{status}，ETA {eta}，金额 {amount:,.2f} 元".format(
            no=r.get("po_no", ""),
            sku=r.get("sku", ""),
            qty=float(r.get("qty", 0)),
            supplier=r.get("supplier", ""),
            status=PO_STATUS_LABELS.get(str(r.get("status", "")), str(r.get("status", ""))),
            eta=r.get("eta", "-"),
            amount=float(r.get("amount", 0)),
        )
        for r in rows
    ]


def build_voucher_text(data: dict[str, Any]) -> str:
    """财务凭证摘要文本：期间/张数/借贷合计/平衡校验 + 科目明细。"""
    balanced = "借贷平衡" if data.get("balanced") else "借贷不平（需核查）"
    lines = [
        f"期间 {data.get('period', '')}：凭证 {data.get('voucher_count', 0)} 张",
        "借方合计 {d:,.2f} 元，贷方合计 {c:,.2f} 元，{b}".format(
            d=float(data.get("debit_total", 0)),
            c=float(data.get("credit_total", 0)),
            b=balanced,
        ),
    ]
    lines += [
        "- {subject}：借 {debit:,.2f} / 贷 {credit:,.2f}".format(
            subject=s.get("subject", ""),
            debit=float(s.get("debit", 0)),
            credit=float(s.get("credit", 0)),
        )
        for s in data.get("by_subject") or []
    ]
    return "\n".join(lines)


def _stock_alert_lines(alerts: list[dict[str, Any]]) -> list[str]:
    """库存预警行文本（类型/库位/明细/建议），供 WMS 相关视图复用。"""
    return [
        f"- [{ALERT_TYPE_LABELS.get(str(a.get('alert_type', '')), str(a.get('alert_type', '')))}] "
        f"{a.get('sku', '')} @{a.get('warehouse', '')}：{a.get('detail', '')}；建议：{a.get('suggested_action', '')}"
        for a in alerts
    ]


def _stock_order_lines(orders: list[dict[str, Any]]) -> list[str]:
    """出入库单行文本（单号/物料/往来方/状态/关联单据），供 WMS 相关视图复用。"""
    return [
        f"- {o.get('doc_no', '')} {o.get('sku', '')}×{o.get('qty', 0):g}（{o.get('partner', '')}）："
        f"{WMS_STATUS_LABELS.get(str(o.get('status', '')), str(o.get('status', '')))}"
        + (f"，关联 {o['ref_no']}" if o.get("ref_no") else "")
        for o in orders
    ]


def build_stock_text(payload: dict[str, Any]) -> str:
    """WMS 预警 + 出入库单合并文本（缺料/临期处置建议 + 单据状态）。"""
    lines: list[str] = []
    alerts = payload.get("alerts") or []
    if alerts:
        lines.append(f"库存预警 {len(alerts)} 条：")
        lines += _stock_alert_lines(alerts)
    orders = payload.get("orders") or []
    if orders:
        label = "出库单" if orders[0].get("order_type") == "outbound" else "入库单"
        lines.append(f"{label} {len(orders)} 笔：")
        lines += _stock_order_lines(orders)
    if not lines:
        lines.append("暂无相关预警或出入库单。")
    return "\n".join(lines)


# ---- P2.2 科室工作台渲染（PRD 6.2，五科室只读视图）----


def build_material_check_text(payload: dict[str, Any]) -> str:
    """生产科备料齐套检查：缺料清单 + 近期出库单（领料进度）。"""
    inventory = payload.get("inventory") or []
    shortages = [r for r in inventory if r.get("below_safety")]
    if shortages:
        lines = [f"缺料清单（低于安全库存）{len(shortages)} 项："]
        lines += build_inventory_lines(shortages)
    else:
        lines = ["物料齐套：全部物料均高于安全库存，无缺料风险。"]
    orders = payload.get("orders") or []
    if orders:
        lines.append(f"近期出库单 {len(orders)} 笔：")
        lines += _stock_order_lines(orders)
    return "\n".join(lines)


def build_inbound_view_text(payload: dict[str, Any]) -> str:
    """计划科物料到货视图：在途采购单（ETA）+ 关联库存水位。"""
    pos = payload.get("pos") or []
    if pos:
        lines = [f"在途采购单 {len(pos)} 笔（到货计划）："]
        lines += build_po_lines(pos)
    else:
        lines = ["当前无在途采购订单。"]
    inventory = payload.get("inventory") or []
    if inventory:
        lines.append(f"关联库存 {len(inventory)} 行：")
        lines += build_inventory_lines(inventory)
    return "\n".join(lines)


def build_batch_trace_text(payload: dict[str, Any]) -> str:
    """品管科效期/批次追溯：效期临期预警（含批次明细）+ 入库单（批次来源）。"""
    lines: list[str] = []
    alerts = payload.get("alerts") or []
    if alerts:
        lines.append(f"效期/库存预警 {len(alerts)} 条：")
        lines += _stock_alert_lines(alerts)
    orders = payload.get("orders") or []
    if orders:
        lines.append(f"近期入库单 {len(orders)} 笔（批次来源）：")
        lines += _stock_order_lines(orders)
    if not lines:
        lines.append("暂无效期预警或入库批次记录。")
    return "\n".join(lines)


def build_stock_overview_text(payload: dict[str, Any]) -> str:
    """仓库物流科运营概览：库存水位汇总 + 待处置预警。"""
    inventory = payload.get("inventory") or []
    alerts = payload.get("alerts") or []
    shortages = [r for r in inventory if r.get("below_safety")]
    lines = [f"在库 SKU {len(inventory)} 个，低于安全库存 {len(shortages)} 个。"]
    if shortages:
        lines.append("缺料明细：")
        lines += build_inventory_lines(shortages)
    if alerts:
        lines.append(f"待处置预警 {len(alerts)} 条：")
        lines += _stock_alert_lines(alerts)
    if not inventory and not alerts:
        lines.append("暂无库存与预警数据。")
    return "\n".join(lines)


def build_data_check_text(payload: dict[str, Any]) -> str:
    """信息科经营数据巡检：BI 指标快照 + 凭证借贷平衡核查。"""
    lines: list[str] = []
    bi = payload.get("bi") or {}
    if isinstance(bi, dict) and bi.get("kpi"):
        kpi = bi["kpi"]
        text = (
            f"BI：{kpi.get('region', '')} {kpi.get('period', '')} "
            f"{kpi.get('label', '')}：{kpi.get('value', 0):,.2f} 元"
        )
        mom = bi.get("mom_pct")
        if mom is not None:
            text += f"，环比{'+' if mom >= 0 else ''}{mom}%"
        lines.append(text)
    voucher = payload.get("voucher") or {}
    if isinstance(voucher, dict) and voucher:
        balanced = "借贷平衡" if voucher.get("balanced") else "借贷不平（需核查）"
        lines.append(
            "凭证 {p}：{n} 张，借 {d:,.2f} / 贷 {c:,.2f}，{b}".format(
                p=voucher.get("period", ""),
                n=voucher.get("voucher_count", 0),
                d=float(voucher.get("debit_total", 0)),
                c=float(voucher.get("credit_total", 0)),
                b=balanced,
            )
        )
    if not lines:
        lines.append("暂无可巡检数据。")
    return "\n".join(lines)


# ---- P3.1 MES / U8 渲染（PLAN P3.1，PRD 7.1：只读查询场景）----


def build_work_order_lines(rows: list[dict[str, Any]]) -> list[str]:
    """MES 工单进度行文本（计划/完工/良品/不良/状态/工位）。"""
    return [
        "{no} {name}（{sku}）@{ws}：计划 {plan:g}，完工 {done:g}（良品 {good:g}/不良 {ng:g}），{status}".format(
            no=r.get("work_order", ""),
            name=r.get("sku_name", ""),
            sku=r.get("sku", ""),
            ws=r.get("workstation", "-"),
            plan=float(r.get("plan_qty", 0)),
            done=float(r.get("completed_qty", 0)),
            good=float(r.get("good_qty", 0)),
            ng=float(r.get("ng_qty", 0)),
            status=MES_STATUS_LABELS.get(str(r.get("status", "")), str(r.get("status", ""))),
        )
        for r in rows
    ]


def build_production_report_lines(rows: list[dict[str, Any]]) -> list[str]:
    """MES 报工明细行文本（操作工/时间/良品/不良/工时）。"""
    return [
        "- {time} {op}：报工 {good:g}（不良 {ng:g}），工时 {hours:g}h（{no}）".format(
            time=r.get("report_time", "-"),
            op=r.get("operator", "-"),
            good=float(r.get("good_qty", 0)),
            ng=float(r.get("ng_qty", 0)),
            hours=float(r.get("work_hours", 0)),
            no=r.get("report_no", ""),
        )
        for r in rows
    ]


def build_gl_balance_text(data: dict[str, Any]) -> str:
    """U8 科目余额表文本：期间/本期借贷合计 + 期初/期末明细。"""
    rows = data.get("rows") or []
    lines = [
        "期间 {p}：{n} 个科目，本期借方合计 {d:,.2f} 元，贷方合计 {c:,.2f} 元（U8 只读）".format(
            p=data.get("period", ""),
            n=len(rows),
            d=float(data.get("debit_total", 0)),
            c=float(data.get("credit_total", 0)),
        )
    ]
    lines += [
        "- {subject}（{dir}）：期初 {opening:,.2f}，本期 借 {debit:,.2f} / 贷 {credit:,.2f}，期末 {closing:,.2f}".format(
            subject=r.get("subject", ""),
            dir="借" if r.get("direction") == "debit" else "贷",
            opening=float(r.get("opening", 0)),
            debit=float(r.get("debit", 0)),
            credit=float(r.get("credit", 0)),
            closing=float(r.get("closing", 0)),
        )
        for r in rows
    ]
    return "\n".join(lines)


def build_voucher_detail_text(data: dict[str, Any]) -> str:
    """U8 凭证明细文本：凭证号/日期/摘要 + 借贷分录 + 平衡校验。"""
    balanced = "借贷平衡" if data.get("balanced") else "借贷不平（需核查）"
    lines = [
        "凭证 {no}（{date}）：{summary}，{b}".format(
            no=data.get("voucher_no", ""),
            date=data.get("voucher_date", "-"),
            summary=data.get("summary", ""),
            b=balanced,
        )
    ]
    lines += [
        "- {dir}：{subject} {amount:,.2f} 元".format(
            dir="借" if e.get("direction") == "debit" else "贷",
            subject=e.get("subject", ""),
            amount=float(e.get("amount", 0)),
        )
        for e in data.get("entries") or []
    ]
    return "\n".join(lines)


# ---- P3.2 全部科室覆盖渲染（PLAN P3.2，PRD 7.2）----
# 管理速览复用 build_data_check_text（{bi, voucher} 同构）、
# 技术备件复用 build_stock_overview_text（{inventory, alerts} 同构）。


def build_hr_activity_lines(rows: list[dict[str, Any]]) -> list[str]:
    """人力资源科员工动态行文本（近 7 天 OA 操作记录）。"""
    return [
        "- {t} [{type}] {summary}".format(
            t=str(r.get("occurred_at", ""))[:16],
            type=r.get("action_type", ""),
            summary=r.get("summary", ""),
        )
        for r in rows
    ]


def build_audit_trace_text(rows: list[dict[str, Any]]) -> str:
    """法务科审计流水文本：本人操作留痕（D3 敏感：仅本人范围）。"""
    if not rows:
        return "暂无审计流水记录。"
    lines = [f"本人近 {len(rows)} 条操作留痕（最新在前）："]
    lines += [
        "- {t} {action}（{tool}）→ {res}".format(
            t=str(r.get("time", ""))[:19],
            action=r.get("action", ""),
            tool=r.get("tool") or "-",
            res=r.get("result", ""),
        )
        for r in rows
    ]
    return "\n".join(lines)


def build_product_sales_text(payload: dict[str, Any]) -> str:
    """产品科销售看板：BI 产品线拆分 + 可选客户 360 摘要。"""
    lines: list[str] = []
    bi = payload.get("bi") or {}
    series = bi.get("series") or []
    period = (bi.get("kpi") or {}).get("period", "")
    if series:
        lines.append(f"{period}产品线销售额：")
        lines += [
            "- {label}：{value:,.2f} 元".format(label=s.get("label", ""), value=float(s.get("value", 0)))
            for s in series
        ]
    else:
        kpi = bi.get("kpi") or {}
        if kpi:
            lines.append(
                "BI：{p} {label}：{value:,.2f} 元".format(
                    p=kpi.get("period", ""),
                    label=kpi.get("label", ""),
                    value=float(kpi.get("value", 0)),
                )
            )
    customer = payload.get("customer") or {}
    if isinstance(customer, dict) and customer:
        lines.append(
            "头部客户：{name}（信用 {credit}，历史订单 {n} 笔）".format(
                name=customer.get("name", "-"),
                credit=customer.get("credit_level", "-"),
                n=len(customer.get("orders") or []),
            )
        )
    elif payload.get("customer_candidates"):
        lines.append(f"客户候选 {len(payload['customer_candidates'])} 家，请指明具体客户。")
    if not lines:
        lines.append("暂无产品销售数据。")
    return "\n".join(lines)
