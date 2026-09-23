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

P2.6 跨系统编排（PLAN P2.6，PRD 6.3）：
- cross_system_order_flow  跨系统订单一条龙（经 workflow 域 workflow_id
  引用官方编排执行：CRM 下单 → 审批进度 → WMS 备货参考 → 通知业务员；
  read_tools/write_tool 皆空 → hitl 直通，计划卡/确认卡由 execute 分支
  调 workflow 引擎签发，写步骤确认卡 payload key 为 "workflow"）

技能优先：intent 命中技能 → 用技能编排工具；未命中降级闲聊兜底。

三模式（PLAN P2.6，PRD 3.3）：mode 为技能默认运行模式——ask 只读无
确认卡 / plan 先出执行计划卡批准后执行 / craft 表单确认卡 HITL 后立即
执行。内置技能映射：写技能（含行内审批）craft（现行 HITL 行为）、读
技能 ask。请求级临时切换经 ChatState.mode_override 覆盖（API /chat
mode 参数）；规则2：写入步骤无论什么模式强制 HITL（plan 批准后仍挂
确认卡）；规则3：科室自定义技能 mode 仅 ask/plan（禁 craft）。

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
        "mode": "craft",  # 写技能默认 craft：表单确认卡 HITL 后执行（PRD 3.3）
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
        "mode": "craft",  # 行内审批含写动作，保持现行 HITL 行为（PRD 3.3）
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
        "mode": "ask",
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
        "mode": "ask",
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
        "mode": "craft",  # 写技能默认 craft：表单确认卡 HITL 后执行（PRD 3.3）
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
        "mode": "ask",
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
        "mode": "ask",
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
        "mode": "ask",
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
        "mode": "ask",
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
        "mode": "ask",
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
        "mode": "ask",
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
        "mode": "ask",  # 科室技能仅 ask/plan（规则3：禁 craft，PRD 3.3）
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
        "mode": "ask",  # 科室技能仅 ask/plan（规则3：禁 craft，PRD 3.3）
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
        "mode": "ask",  # 科室技能仅 ask/plan（规则3：禁 craft，PRD 3.3）
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
        "mode": "ask",  # 科室技能仅 ask/plan（规则3：禁 craft，PRD 3.3）
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
        "mode": "ask",  # 科室技能仅 ask/plan（规则3：禁 craft，PRD 3.3）
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
        "mode": "ask",
        "intent_patterns": [],
        "read_tools": [],
        "write_tool": None,
        "required_roles": [],
        "ask_messages": {},
        "required_fields": [],
    },
    # ---- P2.6 跨系统编排（PLAN P2.6，PRD 6.3）----
    # 编排技能经 workflow_id 引用官方编排（独立 workflow 域），自身不持有
    # 工具：extract 复用 CRM 订单抽取（客户匹配 + 默认值链产出 inputs 全
    # 字段），required_fields 仅客户/联系人/明细（其余由默认值链兜底）
    "cross_system_order_flow": {
        "name": "cross_system_order_flow",
        "title": "跨系统订单一条龙",
        "rw": "write",  # permission 节点角色门禁兜底（workflow 引擎无角色矩阵）
        "mode": "plan",  # PRD 6.3：Plan 模式先出执行计划卡批准后执行
        "required_roles": ["sales"],  # 写路径校验角色（销售岗才能发起下单编排）
        "intent_patterns": ["跨系统", "一条龙", "订单编排", "全流程下单", "跨系统下单"],
        "read_tools": [],  # 皆空 → hitl_node 直通，计划卡由编排执行分支签发
        "write_tool": None,
        # 补问文案与 CRM 录单同口径（format 缺字段分支复用 _crm_ask_text）
        "ask_messages": {
            "customer_id": "请问是哪家客户？请提供客户名称，我来 CRM 匹配。",
            "contact": "请问订单联系人是谁？（需为该客户在册联系人）",
            "items": "请提供商品明细（如：SKU-A x10 单价 50）",
        },
        "required_fields": ["customer_id", "contact", "items"],
    },
    # ---- P3.1 MES / U8 接入（PLAN P3.1，PRD 7.1：新增系统对接，自建 MCP 封装）----
    # 只读查询场景：MES 生产报工查询（工单进度 + 报工明细）、
    # U8 财务总账辅助（科目余额 + 凭证明细，PRD 8.2 严禁写入）
    "mes_production_report": {
        "name": "mes_production_report",
        "title": "生产报工查询",
        "rw": "read",
        "mode": "ask",
        "intent_patterns": ["报工", "生产进度", "产量", "工单", "完工"],
        "read_tools": ["mes__query_work_orders", "mes__query_production_reports"],
        "write_tool": None,
        "required_roles": [],
        "ask_messages": {},
        "required_fields": [],
    },
    "u8_gl_summary": {
        "name": "u8_gl_summary",
        "title": "U8 财务总账",
        "rw": "read",
        "mode": "ask",
        "intent_patterns": ["总账", "科目余额", "余额表", "U8", "财务汇总"],
        "read_tools": ["u8__query_gl_balance", "u8__query_voucher_detail"],
        "write_tool": None,
        "required_roles": [],
        "ask_messages": {},
        "required_fields": [],
    },
    # ---- P3.2 全部科室覆盖（PLAN P3.2，PRD 7.2）：剩余五科室只读技能 ----
    # 复用 P2.2 范式：rw=read + mode=ask + dept_scope 本科室门禁；
    # 关键词避让既有占用词（组合词 ≥3 字，match_skill 最长优先）
    "hr_roster": {
        "name": "hr_roster",
        "title": "人力资源速览",
        "rw": "read",
        "mode": "ask",
        "dept_scope": "人力资源科",
        "intent_patterns": ["人力速览", "员工动态", "人事动态", "在职动态", "招聘线索"],
        "read_tools": ["oa__query_activity_log"],
        "write_tool": None,
        "required_roles": [],
        "ask_messages": {},
        "required_fields": [],
    },
    "mgmt_overview": {
        "name": "mgmt_overview",
        "title": "管理层速览",
        "rw": "read",
        "mode": "ask",
        "dept_scope": "企管科",
        "intent_patterns": ["管理速览", "经营概览", "高管速览", "综合指标", "经营全貌"],
        "read_tools": ["bi__execute_query", "erp__query_voucher_summary"],
        "write_tool": None,
        "required_roles": [],
        "ask_messages": {},
        "required_fields": [],
    },
    "audit_trace": {
        "name": "audit_trace",
        "title": "审计流水查询",
        "rw": "read",
        "mode": "ask",
        "dept_scope": "法务科",
        "intent_patterns": ["审计流水", "审计轨迹", "操作留痕", "我的审计", "谁动过"],
        "read_tools": [],  # 进程内 audit.recent（不走 MCP），execute 分支直调
        "write_tool": None,
        "required_roles": [],
        "ask_messages": {},
        "required_fields": [],
    },
    "tech_inventory": {
        "name": "tech_inventory",
        "title": "技术备件巡检",
        "rw": "read",
        "mode": "ask",
        "dept_scope": "技术科",
        "intent_patterns": ["备件巡检", "备件库存", "技术备件", "备件预警"],
        "read_tools": ["erp__query_inventory", "wms__query_stock_alerts"],
        "write_tool": None,
        "required_roles": [],
        "ask_messages": {},
        "required_fields": [],
    },
    "product_sales": {
        "name": "product_sales",
        "title": "产品销售看板",
        "rw": "read",
        "mode": "ask",
        "dept_scope": "产品科",
        "intent_patterns": ["产品看板", "产品销售", "产品线销售", "产品线业绩"],
        "read_tools": ["bi__execute_query", "crm__search_customers"],
        "write_tool": None,
        "required_roles": [],
        "ask_messages": {},
        "required_fields": [],
    },
    # ---- P3.3 个人记忆（PLAN P3.3，PRD 9.3 写入触发①）：显式「记住」句式 ----
    # 记忆写入非业务系统写入（rw=read 无 HITL；PIPL 门禁 + 敏感过滤在 memory store），
    # 进程内直调 memory.add（read_tools 空，同 audit_trace 范式）
    "memory_save": {
        "name": "memory_save",
        "title": "记住偏好",
        "rw": "read",
        "mode": "ask",
        "dept_scope": None,
        "intent_patterns": ["记住这个", "帮我记住", "记一下", "记住这条"],
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
