"""Skill Registry：内置技能表（PRD 3.2 技能优先路由，ARCHITECTURE 4.1）。

MVP 4 官方技能（PRD 6.1 / ARCHITECTURE v1.8）：
- oa_leave_request  请假审批（唯一写入表单：查余额→抽取→校验→HITL→幂等提交）
- oa_todo_approve   待办审批（只读列表 + 行内确认处理）
- bi_query          BI 数据查询（只读：KPI/趋势/拆分，场景 3）
- weekly_report     周报生成（只读聚合 OA 操作记录 → Markdown 草稿，场景 4）

P2.1 业务系统扩展技能（PRD 6.1）：
- crm_sales_order_entry  销售订单录入（唯一 CRM 写入技能：客户匹配→默认值链
  （单价/付款方式/地址/联系人）→HITL→幂等提交，大额自动加销售总监审批）
- crm_customer_360       客户 360 视图（只读：档案+联系人+付款条件+最近成交价）
- crm_order_track        订单跟单进度（只读：状态/节点推进/异常）
- erp_inventory_query    ERP 库存查询（只读：现存量/可用量/在途/缺料过滤）
- erp_po_sync            ERP 采购订单（只读：供应商/数量/ETA/在途过滤）
- erp_voucher_summary    财务凭证摘要（只读：凭证张数/科目借贷汇总/平衡校验）
- wms_stock_alert        WMS 出入库单 + 库存预警（只读：缺料/临期 + 处置建议）

P2.2 科室视角技能（PLAN P2.2，PRD 6.2：信息/生产/计划/品管/仓库 5 科室）：
只读复用现有 MCP 工具组合编排，不新增后端系统；dept_scope 元数据
（科室名）由 permission 节点做组织维「本科室」校验（auth.dept 尾段
匹配才放行，跨科室隔离，PRD 2.3）：
- prod_material_check    生产备料齐套（生产科：ERP 缺料库存 + WMS 领料出库）
- plan_inbound_view      计划物料到货（计划科：ERP 在途采购 + ERP 库存水位）
- qa_batch_trace         品管效期批次（品管科：WMS 预警 + WMS 入库单批次）
- wh_stock_overview      仓库运营概览（仓库物流：ERP 库存 + WMS 预警）
- it_data_check          经营数据巡检（信息科：BI 销售额 + ERP 凭证平衡核对）

P2.5 知识库（PLAN P2.5，PRD 9.5）：
- knowledge_qa  知识问答（纯 RAG 流：route 技能未命中时知识检索命中后进入，
  不调业务工具；回复附来源文档名+章节，PRD 9.5.3 注入点1/3）
- knowledge_tags：技能绑定知识标签——extract 缺字段时注入填写说明（注入点2）、
  execute 成功后注入 SOP/规范引用（注入点4），tags 与知识文档 tags 匹配

技能优先：intent 命中技能 → 用技能编排工具；未命中降级闲聊兜底。

权限元数据（PLAN P1.1，PRD 2.3）：required_roles 为写路径 any-of 角色
（permission 节点消费）；读/查询路径放行，数据可见性由"本人范围"保证
（科室技能额外受 dept_scope 组织维约束）。
"""

from typing import Any

SKILLS: dict[str, dict[str, Any]] = {
    "oa_leave_request": {
        "name": "oa_leave_request",
        "title": "请假申请",
        "rw": "write",
        "required_roles": ["employee"],  # 全员可提交本人请假（写路径校验）
        "intent_patterns": ["请假", "年假", "调休", "病假", "事假", "婚假", "丧假", "产假"],
        "read_tools": ["oa__query_leave_balance"],  # extract 阶段填草稿 computed 字段
        "write_tool": "oa__submit_leave_request",  # HITL 确认后执行
        # 缺失必填字段的追问文案（事由必问无默认，PRD 5.2）
        "ask_messages": {
            "leave_type": "请问要请哪种假？（年假/调休/病假/事假/婚假/丧假/产假）",
            "start_time": "请问开始时间？（如 2026-09-21 09:00）",
            "end_time": "请问结束时间？（如 2026-09-22 18:00）",
            "duration_days": "请问请假时长？（0.5 天粒度，如 2 或 1.5）",
            "reason": "请问请假事由是什么？",
        },
        "required_fields": ["leave_type", "start_time", "end_time", "duration_days", "reason"],
        # 知识绑定（P2.5，PRD 9.5.3 注入点2）：缺字段补问时检索「请假」标签
        # 知识文档，注入填写说明/制度提示（process hint）
        "knowledge_tags": ["请假"],
    },
    "oa_todo_approve": {
        "name": "oa_todo_approve",
        "title": "待办审批",
        "rw": "read",  # 默认读路径：列表查询直接执行
        "intent_patterns": ["待办", "审批", "我的审批", "同意", "驳回", "批准", "否决"],
        "read_tools": ["oa__query_pending_approvals"],
        "write_tool": "oa__approve",  # 行内审批写入（HITL 确认后执行）
        "write_mode": "inline",  # 行内模式：draft 含 approval_id 才走写路径
        "required_roles": ["dept_manager"],  # 审批属管理动作（仅写路径校验，查询放行）
        # "同意第 N 条"但槽位无最近查询结果（或序号越界）时的补问文案
        "ask_messages": {
            "approval_target": "请先查询待办列表（例如：查一下我的待办审批），再回复「同意/驳回第 N 条」。",
        },
        "required_fields": [],
    },
    # Phase 1 后续里程碑（占位，PRD 6.1）
    "bi_query": {
        "name": "bi_query",
        "title": "BI 数据查询",
        "rw": "read",  # 只读查询：直接执行，无 HITL（PRD 5.2 场景 3）
        "intent_patterns": [
            "销售额",
            "销售",
            "业绩",
            "营收",
            "报表",
            "数据查询",
            "指标",
            "拆分",
        ],
        "read_tools": ["bi__execute_query"],
        "write_tool": None,
        "required_roles": [],  # 只读技能不限角色；数据级区域白名单在 permission 节点校验
        "ask_messages": {},
        "required_fields": [],
    },
    "weekly_report": {
        "name": "weekly_report",
        "title": "周报生成",
        "rw": "read",  # 只读聚合：OA 操作记录 → Markdown 草稿（PRD 5.2 场景 4）
        "intent_patterns": ["周报", "写周报", "生成周报"],
        "read_tools": ["oa__query_activity_log"],
        "write_tool": None,
        "required_roles": [],  # 只读聚合本人数据
        "ask_messages": {},
        "required_fields": [],
        # 知识绑定（P2.5，PRD 9.5.3 注入点4）：执行成功后检索「周报」标签
        # 知识文档（如周报撰写规范），回复尾部附引用
        "knowledge_tags": ["周报"],
    },
    # ---- P2.1 业务系统扩展（PRD 6.1）----
    "crm_sales_order_entry": {
        "name": "crm_sales_order_entry",
        "title": "销售订单录入",
        "rw": "write",
        "required_roles": ["sales"],  # 写路径校验角色（销售岗才能下单）
        "intent_patterns": ["销售订单", "下订单", "录订单", "订单录入", "下单", "订货"],
        "read_tools": ["crm__search_customers", "crm__get_customer_360"],
        "write_tool": "crm__submit_sales_order",  # HITL 确认后执行（幂等提交）
        # 必填项仅有客户/联系人/明细：单据类型默认标准销售、地址默认主数据、
        # 付款方式默认客户付款条件、单价默认最近成交价、交期默认 T+7（PRD 6.1.1）
        "ask_messages": {
            "customer": "请问是哪家客户？请提供客户名称，我来 CRM 匹配。",
            "contact": "请问订单联系人是谁？（需为该客户在册联系人）",
            "items": "请提供商品明细（如：SKU-A x10 单价 50）",
        },
        "required_fields": ["customer_id", "contact", "items"],
    },
    "crm_customer_360": {
        "name": "crm_customer_360",
        "title": "客户 360 视图",
        "rw": "read",
        "intent_patterns": ["客户360", "客户信息", "客户档案", "客户视图", "查客户"],
        "read_tools": ["crm__search_customers", "crm__get_customer_360"],
        "write_tool": None,
        "required_roles": [],
        "ask_messages": {
            "keyword": "请问要查哪家客户？请提供客户名称，我来 CRM 匹配。",
        },
        "required_fields": ["keyword"],
    },
    "crm_order_track": {
        "name": "crm_order_track",
        "title": "订单跟单进度",
        "rw": "read",
        "intent_patterns": ["跟单", "订单进度", "订单到哪了", "发货进度", "交期"],
        "read_tools": ["crm__query_order_progress"],
        "write_tool": None,
        "required_roles": [],
        "ask_messages": {
            "order_no": "请提供销售订单号（如 SO20260901001）。",
        },
        "required_fields": ["order_no"],
    },
    "erp_inventory_query": {
        "name": "erp_inventory_query",
        "title": "ERP 库存查询",
        "rw": "read",
        "intent_patterns": ["库存", "现存量", "可用量", "缺料", "安全库存"],
        "read_tools": ["erp__query_inventory"],
        "write_tool": None,
        "required_roles": [],
        "ask_messages": {},
        "required_fields": [],
    },
    "erp_po_sync": {
        "name": "erp_po_sync",
        "title": "采购订单查询",
        "rw": "read",
        "intent_patterns": ["采购单", "采购订单", "在途", "到货"],
        "read_tools": ["erp__query_purchase_orders"],
        "write_tool": None,
        "required_roles": [],
        "ask_messages": {},
        "required_fields": [],
    },
    "erp_voucher_summary": {
        "name": "erp_voucher_summary",
        "title": "财务凭证摘要",
        "rw": "read",
        "intent_patterns": ["凭证", "记账", "借贷", "账务"],
        "read_tools": ["erp__query_voucher_summary"],
        "write_tool": None,
        "required_roles": [],
        "ask_messages": {},
        "required_fields": [],
    },
    "wms_stock_alert": {
        "name": "wms_stock_alert",
        "title": "出入库与库存预警",
        "rw": "read",
        "intent_patterns": ["出库单", "入库单", "出入库", "库存预警", "预警", "临期"],
        "read_tools": ["wms__query_stock_orders", "wms__query_stock_alerts"],
        "write_tool": None,
        "required_roles": [],
        "ask_messages": {},
        "required_fields": [],
    },
    # ---- P2.2 科室视角技能（PLAN P2.2，PRD 6.2）----
    # dept_scope：组织维「本科室」门禁（permission 节点消费，PRD 2.3）
    # intent_patterns 避让既有占用词：科室组合词（≥3 字）优先于既有短词
    # （最长关键词路由，如「效期预警」4 字胜 wms 的「预警」2 字）
    "prod_material_check": {
        "name": "prod_material_check",
        "title": "生产备料齐套检查",
        "rw": "read",
        "dept_scope": "生产科",
        "intent_patterns": ["备料", "齐套", "领料", "生产缺料", "生产物料"],
        "read_tools": ["erp__query_inventory", "wms__query_stock_orders"],
        "write_tool": None,
        "required_roles": [],
        "ask_messages": {},
        "required_fields": [],
    },
    "plan_inbound_view": {
        "name": "plan_inbound_view",
        "title": "计划物料到货视图",
        "rw": "read",
        "dept_scope": "计划科",
        "intent_patterns": ["物料计划", "到货计划", "在途到货", "采购到货", "排产"],
        "read_tools": ["erp__query_purchase_orders", "erp__query_inventory"],
        "write_tool": None,
        "required_roles": [],
        "ask_messages": {},
        "required_fields": [],
    },
    "qa_batch_trace": {
        "name": "qa_batch_trace",
        "title": "品管效期批次追溯",
        "rw": "read",
        "dept_scope": "品管科",
        "intent_patterns": ["效期预警", "效期", "批次", "保质期", "批次追溯", "质量追溯"],
        "read_tools": ["wms__query_stock_alerts", "wms__query_stock_orders"],
        "write_tool": None,
        "required_roles": [],
        "ask_messages": {},
        "required_fields": [],
    },
    "wh_stock_overview": {
        "name": "wh_stock_overview",
        "title": "仓库运营概览",
        "rw": "read",
        "dept_scope": "仓库物流",
        "intent_patterns": ["仓库概览", "仓库库存", "仓储", "库房", "仓库动态"],
        "read_tools": ["erp__query_inventory", "wms__query_stock_alerts"],
        "write_tool": None,
        "required_roles": [],
        "ask_messages": {},
        "required_fields": [],
    },
    "it_data_check": {
        "name": "it_data_check",
        "title": "经营数据巡检",
        "rw": "read",
        "dept_scope": "信息科",
        "intent_patterns": ["数据巡检", "数据核对", "经营速览", "数据质量"],
        "read_tools": ["bi__execute_query", "erp__query_voucher_summary"],
        "write_tool": None,
        "required_roles": [],
        "ask_messages": {},
        "required_fields": [],
    },
    # ---- P2.5 知识库（PLAN P2.5，PRD 9.5.3）----
    # 不参与 match_skill 关键词路由（intent_patterns 为空）：由 route 节点在
    # 技能未命中时经知识检索命中后进入（纯 RAG 问答流，不调业务工具）
    "knowledge_qa": {
        "name": "knowledge_qa",
        "title": "知识问答",
        "rw": "read",
        "intent_patterns": [],
        "read_tools": [],
        "write_tool": None,
        "required_roles": [],
        "ask_messages": {},
        "required_fields": [],
    },
}


def get_skill(name: str) -> dict[str, Any] | None:
    """按名取技能（补问轮从挂起槽位恢复技能定义）。"""
    return SKILLS.get(name)


def match_skill(message: str) -> dict[str, Any] | None:
    """技能优先路由：按意图关键词匹配技能（LLM 路由接入后替换此实现）。

    同句命中多技能时取最长关键词（「销售订单」优先于 BI 的「销售」、
    「库存预警」优先于 ERP 的「库存」），避免短词抢先误路由。

    技能市场联动（PLAN P2.3，PRD 3.4）：仅 published 技能参与路由——
    下架/未上架技能即时降级闲聊兜底（store 进程内同步查询，无 IO）。
    """
    from agent_core.skills import store

    best_len = 0
    best: dict[str, Any] | None = None
    for skill in SKILLS.values():
        if skill.get("placeholder"):
            continue
        if not store.is_published(skill["name"]):
            continue
        matched = max((len(p) for p in skill["intent_patterns"] if p in message), default=0)
        if matched > best_len:
            best_len, best = matched, skill
    return best
