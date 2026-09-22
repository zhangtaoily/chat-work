# Chat-Work 产品开发计划（阶段总览）

> 回溯补档（2026-09-18）。与 PRD 13 章「里程碑计划」对齐，按实际交付情况标注状态。
> 状态口径：✅ 已完成 = 代码交付 + 单测/E2E 验证通过；⏳ 进行中；📋 待开发。
> 详细功能设计见 [PRD-chat-work-agent.md](../PRD-chat-work-agent.md)，技术架构见 [ARCHITECTURE.md](../ARCHITECTURE.md)。

## 总览

| 阶段 | 主题 | 对应 PRD | 状态 |
|------|------|----------|------|
| **P0** | 地基与 MVP 核心能力 | 13 章 MVP（前半） | ✅ 已完成 |
| **P1** | MVP 收尾与小流量上线 | 13 章 MVP（后半） | ✅ 已完成 |
| **P2** | 业务扩展与平台化 | 13 章 Phase 2 | 📋 待开发 |
| **P3** | 全渠道与智能化 | 13 章 Phase 3 | 📋 待开发 |

依赖关系：P0 全部完成 → P1 可启动；P1 灰度验证通过后 → P2 按业务优先级并行推进；P3 在 P2 平台化（技能市场 / 知识库 / 自动化）就绪后开展。

---

## P0 地基与 MVP 核心能力 ✅

> 覆盖：Agent 核心 + OA/BI 两大技能族 + 桌面端 + SSO 登录。

| # | 任务 | 交付物 | 状态 | 验证证据 |
|---|------|--------|------|----------|
| P0.1 | Agent Core 七节点流水线（意图 → 技能路由 → 参数提取 → 校验 → HITL → 执行 → 格式化，含恢复图） | `services/agent_core` | ✅ | 单测 40 passed |
| P0.2 | MCP 服务层：OA 适配（请假/待办审批/活动日志，幂等提交）+ BI 查询 | `services/mcp_oa`、`services/mcp_bi` | ✅ | 单测 + E2E |
| P0.3 | 首批技能：`oa_leave_request`（余额校验/缺失补问/确认卡/幂等）、`oa_todo_approve`（行内确认/"这条"承接/驳回原因提取）、`bi_query`、`weekly_report` | `agent_core/skills`、`pipeline` | ✅ | E2E：请假 6 项、审批 5 步（含 PRD 场景 2 验收句）、审批流规则（<3 天 / ≥3 天） |
| P0.4 | 桌面端客户端（Electron）：主/渲染/preload 三进程 + IPC 白名单 + 本地 MCP（文件工具）+ SSE 接入 Agent Core | `apps/desktop` | ✅ | E2E 全链路（dev 模式） |
| P0.5 | SSO 登录（PRD 8.5，详见 [docs/PLAN-sso.md](PLAN-sso.md)）：mock OIDC Broker / 桌面端 Loopback 登录 + token 加密存储 + 静默刷新 / agent_core JWT 验签 / 前端登录门 | `services/mock_idp`、`apps/desktop/main/auth*`、`agent_core/api/auth.py` | ✅ | 桌面单测 17 passed；agent_core 54 passed；curl E2E（含 401 负路径） |

---

## P1 MVP 收尾与小流量上线 ✅（P1.1–P1.4 已完成）

> 目标：补齐安全与管理面，达成第 13 周小流量灰度（10% 用户）。
> P1.3 部署物与 P1.4 灰度门禁均已交付（`infra/README.md`）；容器栈实际运行与灰度放量仍依赖 PRD 14 章待确认项（GPU 节点、内网基建、真实 OA/BI 地址）。

| # | 任务 | 说明 | 状态 | 验证证据 |
|---|------|------|------|----------|
| P1.1 | 组织权限实接 | 基于 JWT roles + perm_ver 构建权限面（PRD 8.5.5），技能级/数据级行权限 | ✅ | 单测 87 passed（permissions 17 项）；E2E 14/14（`services/agent_core/scripts/e2e_p1.py`，SSO_REQUIRED=true）：员工行内审批拒绝 / dept_manager 放行 / BI 白名单外区域（华北）拒绝（PRD 2.3 数据级业务维） |
| P1.2 | 审计日志 | 敏感操作（OA 写入/确认卡/登录登出）落审计存储，OA 侧留痕联动 | ✅ | mcp_client 单点收口 tool_call 全量审计 + 六类埋点（auth_success / confirm_issued / confirm_approved / confirm_rejected / confirm_denied / permission_denied）；E2E：E1001 审计动作全覆盖；GET /audit 数据级权限——员工仅本人、越权 403、dept_manager 全量；确认卡一次性（重复确认 404） |
| P1.3 | 生产部署 | APISIX 网关统一验签、MySQL/Redis、LLM 部署方案、mock_idp → 真实 Keycloak（改 `SSO_ISSUER`） | ✅ | 部署物全量交付（`infra/README.md` 含起停与冒烟步骤）：3 服务 Dockerfile（uv frozen + 非 root）；compose 补齐 build/env + Keycloak 26（realm：PKCE S256 client、dept/roles/perm_ver mappers、工号种子用户）+ APISIX 独立模式路由（`/api/*`→agent-core，MCP 不出网关）+ MySQL init 建库；agent-core 生产化——`SSO_ISSUER`/`SSO_JWKS_URI` env 外置（mock→Keycloak 零代码切换）、jti 黑名单 Redis 双写跨 worker 共享（PRD 8.5.7）、audit 归档滑动 TTL 180 天（PRD 10）；单测 89 passed（新增 preferred_username 工号优先、Redis 黑名单共享 2 项）+ ruff 干净 + realm/YAML 语法校验通过。本机无 Docker：容器栈起停步骤写入 README，待有 Docker 环境执行 |
| P1.4 | 小流量灰度 | 第 13 周 10% 用户；桌面端分发与灰度更新通道（PRD 6.4.5） | ✅ | 服务端：GRAY_PERCENT sha256 确定性分桶门禁（强制升级优先）+ `X-Client-Version` 版本协商（低于 `MIN_CLIENT_VERSION` → 403 `client_version_too_low`，超灰度 → `gray_percent_exceeded`）+ `GET /metrics/gray` 灰度观测（PRD 16.4 五类指标，dept_manager 限定）；agent_core 单测 104 passed + ruff 0 错误。桌面端：authedFetch 自动附加 X-Client-Version + 403 门禁码解析（`ClientVersionTooLowError` 强制升级 Modal / `GrayGateError` 提示）+ `main/updater.ts` electron-updater（`UPDATER_CHANNEL` stable/beta 通道，`app.isPackaged=false` 跳过）+ IPC 补 4 通道（app:getVersion / updater:check|download|install）；typecheck 0 错误 + vitest 17 passed。基建：compose update-server 转正（nginx:1.27 :8090）+ `infra/nginx/update-server.conf`（latest.yml no-cache + healthz）+ `releases/desktop/{stable,beta}/` 目录约定；compose YAML 校验通过 |

---

## P2 业务扩展与平台化 📋（Phase 2，Month 4-6）

> 目标：MVP 全量上线后扩展业务面，沉淀平台能力。

| # | 任务 | 说明 | 状态 | 验证证据 |
|---|------|------|------|----------|
| P2.1 | 业务系统扩展 | CRM / ERP / WMS 接入（MCP 适配器复用 OA 模式） | ✅ | 新增 3 MCP 服务：mcp_crm:8003（客户模糊匹配/360 视图/销售订单录入/跟单；写入幂等键 `{userId}_{sessionId}_{intentHash}_{draftVersion}` + 大额合计 ≥ ¥5,000 自动加销售总监审批节点 + 联系人在册校验，PRD 6.1.1）/ mcp_erp:8004（库存/采购订单/凭证摘要，全只读）/ mcp_wms:8005（出入库单/库存预警，全只读）；读写分离铁律（PRD 8.2）：CRM 写走官方 OpenAPI 路径（mock 内存记账），ERP/WMS 无写路径。agent_core 接入：client×3 + `MCP_CRM_URL`/`MCP_ERP_URL`/`MCP_WMS_URL` + registry 7 技能（最长关键词优先路由，防「销售订单」被 BI 短词抢先）+ rules 默认值链（单价/付款条件/地址/联系人/交期 T+7/单据类型）与多候选点名流 + graph 只读分支与写入 HITL 确认卡。契约 9 JSON（`packages/protocol/tools/crm__*` ×4 / `erp__*` ×3 / `wms__*` ×2）与 compose 三服务装配（env + depends_on + crm 幂等表 DATABASE_URL）校验通过。回归：pytest 116（agent_core）+ 17（crm）+ 12（erp）+ 9（wms）全绿，ruff 4 服务 0 错误 |
| P2.2 | 科室扩展 | 信息 / 生产 / 计划 / 品管 / 仓库 | ✅ | 组织维权限实装「本科室」级（PRD 2.3）：`check_dept_scope` —— 科室技能带 `dept_scope` 元数据，permission 节点比对 auth.dept 尾段（「事业部A/生产科」→「生产科」），跨科室前置拦截（不触达工具 + 落 permission_denied 审计 + 友好文案）；行级过滤依赖单据系统 dept 字段，留 P3 MES 接入时服务侧实装。Keycloak 种子补 5 科室用户 E2001-E2005（realm-export.json，perm_ver 17）。registry 注册 5 科室只读技能：prod_material_check（生产·备料齐套=ERP 缺料+WMS 出库）/ plan_inbound_view（计划·到货视图=ERP 在途 PO+库存）/ qa_batch_trace（品管·效期批次=WMS 效期预警+入库单）/ wh_stock_overview（仓库·运营概览=ERP 库存+WMS 预警）/ it_data_check（信息·数据巡检=BI+ERP 凭证平衡）；触发词最长关键词优先（「生产缺料」「效期预警」等组合词避让既有短词）；graph extract/execute/format 三节点编排（复用 P2.1 只读工具，无新增写路径）+ rules 渲染辅助（缺料清单/到货计划/批次追溯/库存水位/巡检快照）。回归：pytest 127（agent_core，新增 test_dept 11 项：dept_scope 单元 5 + 流水线 6）+ 17（crm）+ 12（erp）+ 9（wms）全绿，ruff 0 错误 |
| P2.3 | 技能市场 | 技能注册/评审/上架（PRD 5.6） | ✅ | 市场元数据制：registry 保持运行时技能定义 SSOT（零迁移），新增 `skills/store.py` 只管生命周期元数据——状态机 draft→submitted→in_review→published/rejected→deprecated（PRD 3.5 MVP 简化），写入类技能 submit 进 in_review 并生成 `SEC-RV-{seq:04d}` 评审单（security_reviewer 角色门禁，PRD 5.6.1），只读技能提交即自动发布；内置技能按 dept_scope 有无自动归类 official/dept，基线一律 published（惰性重建，与 audit/slots 同风格进程内存储 + 可选 Redis 快照 `skill_store:snapshot` 写镜像/启动 restore）。路由联动：`match_skill` 仅 published 技能参与匹配（进程内同步查询无 IO），下架/未上架即时降级闲聊兜底；使用统计（PRD 3.4）：graph format 节点对触达 MCP 的调用 record_usage（permission 拒绝不计、MCP 报错计失败），广场展示调用量/成功率并按热度排序。市场 API（api/main.py，恒需认证口径对齐 /audit）：广场浏览（category/q 过滤，include_unpublished 仅管理员=评审工作台数据源）/ 详情（元数据+运行时定义合并）/ 一键安装（published 校验 + dept_scope 组织维跨科室 403）/ 我的技能（装/卸）/ 注册/提交/评审/下架；生命周期动作全落审计 skill_register/skill_submit/skill_review/skill_deprecate（PRD 5.6.4 评审留痕）。回归：pytest 143（agent_core，新增 test_skill_store 16 项）+ 17（crm）+ 12（erp）+ 9（wms）全绿，ruff 0 错误 |
| P2.4 | 自动化任务 | 定时/触发式执行，先跑通再自动化（PRD 3.6） | ✅ | 新增 `automation/` 域（store.py 单文件域模型，进程内存储 + 可选 Redis 快照 `automation:snapshot` 写镜像/启动 restore）：任务 CRUD（id `auto_{seq:06d}`，字段 PRD 3.6.2：name/skill/params(draft 形态)/schedule/channel/perm_mode/scope/status(failure_count)/next_run_at/stats/owner_auth 快照）+ 调度纯函数校验（daily 空 days=每天 / ["workday"] 仅工作日；weekly days 0-6 且 0=周一，与 apscheduler CronTrigger 及 Python weekday() 一致；monthly days 1-31；once at ISO；非法立即 ValueError→API 400）+ `next_run_time` 纯函数推演；防护栏（PRD 3.6.2）：仅只读技能可自动化（市场 store ∩ 运行时 registry 双层校验，绑定 write 技能直接 400——「无人值守不写入」）、最小间隔 15 分钟、单用户 active ≤3（暂停不占额度）、单次执行 30 分钟超时；执行管线复用（无 AI 决策）：创建时快照 owner_auth 三键（user_id/dept/roles），执行时注入 ChatState 以创建者身份跑子图 validate→permission→hitl→execute→format（params 已是 draft 形态跳过 intent/route/extract），权限随创建者走（跨科室技能执行期仍会被 permission 拦截计失败且不触达 MCP）；session_id=`auto_{tid}_{seq:03d}` 串起历史与审计；失败不重试，连续 3 次自动暂停 + 信箱通知 + 审计 automation_paused，成功清零 failure_count；结果信箱（wecom 推送留 P2.6 前接口）：run 成/败均推 result 消息，inbox 新的在前 + 已读标记。调度器：apscheduler AsyncIOScheduler 进程内（daily/weekly/monthly CronTrigger + once DateTrigger），lifespan 挂载 start/stop，事件触发（event 类型）留 P2.6。自动化 API（api/main.py，9 端点恒需认证）：列表（owner 过滤 + status 可选）/ 创建（owner_auth 快照，校验失败 400）/ 收件箱（字面量路由注册在 {task_id} 之前，limit clamp 1-200 + unread_only）/ 已读标记 / 详情 / 暂停 / 恢复 / 删除（owner 或 dept_manager/security_reviewer，否则 403）/ 执行历史（limit clamp 1-100）。回归：pytest 174（agent_core，新增 test_automation 31 项：调度校验 4 类+workday+生效期、13 种非法调度、next_run 场景、防护栏 5、生命周期 4、真实子图执行（monkeypatch call_erp_tool 验证 stats/history/信箱）、权限随创建者、3 败暂停联动+成功清零、API 全链路 401/400/403/404）+ 17（crm）+ 12（erp）全绿，ruff 4 服务 0 错误 |
| P2.5 | 知识库 | 集团与科室两级知识空间 + 入库流程 + RAG 注入（PRD 9.5） | ✅ | 新增 `knowledge/` 域：embedding.py——OpenAI 兼容 /embeddings 客户端（`EMBEDDING_BASE_URL`/`EMBEDDING_API_KEY`/`EMBEDDING_MODEL` env 外置）+ 未配置/调用失败整批降级本地 char-bigram（数字折叠对称清洗），后端标记 `api:{model}` vs `local:char-bigram` + (tag,vec) 缓存杜绝混空间比较。store.py——文档生命周期状态机 draft→pending_review→published/rejected→deprecated（PRD 9.5.2 入库流程：上传切片→涉密检测→审核→向量化→生效；D4 机密直接拒绝入库、D3 敏感放行但检索结果脱敏展示 `138****5678`，PRD 10.2；update version+1 回 draft 重审 = R10 版本管理，deprecate 向量立即出索引）+ Markdown 标题层级切片（section 路径 + 500 字句界二次切）+ RAG 检索（PRD 9.5.3：group/dept 两级空间过滤、tags 交集、阈值 Top-K——API 后端 0.75/本地 0.35 独立校准、正文与标题路径双向量取 max、后端标记不一致自动全量 reindex 自愈）+ 使用统计/知识缺口（PRD 9.5.4：record_hit/record_gap）+ Redis 快照 `knowledge:snapshot`（向量不入快照，restore 后检索自愈重建）。graph 四注入点全接（route 未命中检索消歧转 knowledge_qa 纯 RAG 流不调业务工具 / extract 缺字段按技能 title+knowledge_tags 注入填写提示 / execute 成功后附制度参考 / format 渲染「来源：《标题》章节」+「相关制度参考」Top-2）；knowledge_qa 技能 intent_patterns 为空仅经知识检索进入。API 10 端点恒需认证：上传（含 source=session 会话产物沉淀「保存到知识库」，仅科室空间走审核）/ 列表（普通用户本科室视角，跨科室 dept_scope 查询 403）/ 详情（跨科室 404 不暴露存在性）/ submit/review/update/deprecate/search/stats/gaps；权限矩阵：group=knowledge_manager、dept=dept_manager+本科室；生命周期动作全落审计 knowledge_*。修复两个真 bug：graph `_auth_dept` 取组织全路径尾段（对齐 permissions/API 口径，否则 dept 空间知识流水线侧永不命中）；`_upload_scope` session 校验先于 group 分支（否则管理员可绕过「沉淀仅科室空间」约束）。回归：pytest 201（agent_core，新增 test_knowledge 27 项：切片/状态机/驳回流/脱敏/空间隔离/标签过滤/后端切换自愈/四注入点 e2e 含 dept 隔离/API 权限与生命周期）+ 17（crm）+ 12（erp）+ 9（wms）+ 5（bi）全绿，ruff 0 错误 |
| P2.6 | 跨系统编排 | 多系统串联工作流 | 📋 | |
| P2.7 | Web 管理后台 | 技能评审 / 知识库审核 / 自动化治理 / 系统配置工作台（PRD 5.6） | 📋 | |
| P2.8 | 桌面端 macOS 版 | 视需求启动 | 📋 | |

---

## P3 全渠道与智能化 📋（Phase 3，Month 7-12）

> 目标：制造执行深度集成 + 记忆智能 + 全渠道入口。

| # | 任务 | 说明 | 状态 |
|---|------|------|------|
| P3.1 | MES / U8 接入 | 生产执行与财务域 | 📋 |
| P3.2 | 全部科室覆盖 | 剩余科室技能补齐 | 📋 |
| P3.3 | 个人 / 组织记忆 | 跨会话偏好与组织知识沉淀（PRD 记忆设计） | 📋 |
| P3.4 | 小程序 | 移动端入口 | 📋 |
| P3.5 | 语音 | 语音输入/播报 | 📋 |

---

## 关键风险提示（详见 PRD 15 章）

- **R8 进度延期**：OA 厂商配合慢 → 接口联调提前至 Month 1
- P1.3 部署物与 P1.4 灰度机制已交付；容器栈起停、vLLM 与灰度放量（GRAY_PERCENT 从 0 逐步调至 10）仍依赖 GPU 节点与内网基础设施确认（PRD 14 章待确认项）
- P2 起多线并行，建议每阶段启动前做一次范围裁剪评审
