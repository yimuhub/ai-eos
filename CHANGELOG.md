# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的组织方式。
完整的工程质量改造记录见 [UPGRADE_NOTES.md](UPGRADE_NOTES.md)。

## [Unreleased]

### 新增：超级 Agent（CEO）指挥台（M1–M5）

老板只面对一个入口：一句话 → 准绳 → 拆解 → 并发执行 → 回收自检 → 汇总报告。
旧的 `/orchestrate` 保留兼容，但内核已收敛到同一套实现，系统里不再有两套「CEO」。

- **准绳（锚）**：拆解前先落库目标、交付物、红线、验收标准与公司术语口径；
  子 Agent 的系统提示里强制注入，冲突时以准绳为准。开工后锚不可改，
  只允许追加指令（`anchor_version` 递增），改锚由路由返回 409。
- **拆解上限 5 → 12**：旧实现写死 `[:5]`，现在按实际需要拆，最多 12 条；
  负责人可从全公司员工里按姓名/岗位匹配，匹配不上会标记 `agent_matched=false`。
- **并发闸门**：默认 3、保底 2、界面可调 3–10；服务端按负责人主模型的
  供应商档位（OpenAI 兼容 8 / Anthropic 4 / Gemini 3 / 未知 2）与后台
  worker 余量取最小值。连续 3 次限流或超时自动降到 2 并冷却 5 分钟，
  降档原因写进 mission 与审计事件，老板在面板上看得到「今天为什么只开了 2 个」。
- **回收与补跑**：空/极短产出零成本判偏离；全部结束后一次批量锚审查
  （每场统筹只花 1 次模型调用），失败/偏离各补跑一次；补跑成功覆盖原结果。
  修复了两个只有真跑才会暴露的缺陷：补跑阶段被自己的 `RECLAIMING` 状态
  误判为「任务已暂停」而全部跳过；外层会话里的任务快照仍是开工前的 `TODO`，
  导致补跑永远不触发（改用 `populate_existing` 强制刷新）。
- **交付必达**：报告写回父任务、归档到成果中心、发站内通知
  （`ceo.mission_done`，链接直达 `/ceo?mission=<id>`）；失败/偏离单独列在
  「需要你关注」，不混进正文美化。
- **定时日报**：指定 CEO 即幂等开通每日 09:00 的 `ceo_digest` 调度，
  汇总近 24 小时的统筹/执行/成本/卡住任务与失败最多的员工；调度器
  原生支持该 action，同时发站内通知并支持 Web Push。
- **前端**：新增 `/ceo` 指挥台（CEO 指派、容量条、一句话拆解预览、
  统筹列表与作战视图、暂停/继续/取消/补跑/追加指令、报告与日报卡），
  导航入口加到「指挥中枢」。
- **接口与迁移**：`/ceo` 16 个端点全部声明响应模型（`extra="allow"`）；
  新增 `ceo_missions` 表（迁移 `0006`），`agents.is_ceo` 与 `tasks.mission_id`
  为可空新列，SQLite/PG 均可幂等升级。响应模型棘轮从 237/51% 上调到 253/53%
  （实测 256/477 = 53.7%）。
- **权限与续跑**：统筹的写操作（开工/暂停/继续/取消/补跑/追加指令）统一要求
  `OPERATE` 能力，只读成员读得到但改不了；暂停后继续会把暂停前已完成的产出
  一并带进汇总，不会丢一半结果。
- **测试**：`tests/test_ceo_contract.py` 23 项——路由/模型静态护栏、键集合相等、
  2/3/10 三档并发峰值、429 自动降档与恢复、偏离打回、补跑只一次、
  报告归档与通知、续跑保留已完成产出、只读成员写入 404、跨租户 404。

### 新增：CEO 缺岗自动招人（部门 + 组）

拆解时如果子任务指向公司里不存在的岗位，CEO 不再只能把活硬塞给现有员工，
而是**在预览里先说明、开工时自动建人**，并把新员工放进指定的部门和组。

- **全自动，缺谁建谁**：拆解阶段就返回 `new_agents` 提议（姓名/岗位/部门/组/理由），
  开工时真正创建员工——预览与执行是两件事，老板看一眼就能决定要不要开工。
- **落位层级**：新增 `agent_groups` 表（部门下的组），`agents.group_id` 为可空新列；
  部门按名字复用、没有就建，组同理，重名自动加「（2）」后缀。
- **三个硬约束**：每场统筹最多新招 3 人（超出的子任务回落给现有员工并标记
  未精确匹配）；同一场里同岗位只招一次；只在本公司内找/建部门与组，跨租户不可见。
- **模型继承**：新员工继承负责人的模型绑定（负责人没有就退到公司任一有绑定的
  员工），否则一开工就会因「没有可用模型」失败。
- **可审计**：每次建人写 `ceo.agent_hired` 审计事件（含 mission/部门/组/岗位），
  统筹详情与开工响应都会带回已入职名单。
- **组织架构页**：部门卡片新增「组」区块与成员名单，CEO 建出来的组无需额外操作
  即可看到；`/companies/{cid}/departments` 每个部门多返回 `groups`。
- **迁移与测试**：迁移 `0007`（`agent_groups` 表 + 两列可空新列 + 索引），
  SQLite 自动补列与 Alembic 双轨一致；新增 2 项契约/行为测试，覆盖落位、
  模型继承、上限、去重、重名后缀与组织架构可见性。

### 新增：员工创建即自动配备技能（本地库 → 市场 → CEO 生成）

新建员工不再只是「一个岗位名 + 一段提示词」：系统按岗位与任务描述自动配技能，
并把技能卡里的**方法论**真正注入执行提示词，而不只是挂一张技能名清单。

- **三级兜底**：① 本地技能库按岗位/任务 token 打分，命中即装（零外呼）；
  ② 本地没有就去 GitHub 公开技能目录找 `SKILL.md`；③ 市场也没有，由 CEO
  按岗位与任务上下文生成一张专属技能卡（适用场景 / 步骤 / 交付标准 / 常见错误）。
  三级都失败才退回内置 `web_search`——**不允许无技能上岗**。
- **安全边界**：市场抓取只允许 `https://raw.githubusercontent.com`，8 秒超时、
  64 KB 上限；内容必须过 `security_scan` 才能入库，只当文本注入、不执行任何代码。
  顺带修好一个真实漏检：`curl https://… | bash` 因为 URL 一长就绕过了原来的
  字面量模式，现在用正则跨任意 URL 识别管道执行。
- **方法论进提示词**：运行时把已装技能的 `instruction` 按技能分组注入系统消息
  （单项 1500 字、总量 6000 字截断），技能从「证书」变成「作业指导书」。
- **触发点**：手工建员工（本地库 + 生成）、批量建员工（仅本地库 + 兜底，
  避免 N 个员工各触发一次模型）、CEO 缺岗招人（三级全开）、
  以及新增 `POST /agents/{aid}/skills/provision?deep=` 供存量员工手动补齐。
- **可审计、可降级**：每次配备写 `skill.auto_bound` 事件，`ceo.agent_hired`
  事件带上已配技能名；任一级失败只降级不阻断建人/开工，失败原因进 `skill_errors`
  由前端展示。外呼与生成分别受 `AIEOS_SKILL_MARKET_AUTO`、
  `AIEOS_SKILL_AUTO_GENERATE` 开关控制，测试环境默认关闭、分支用 monkeypatch 覆盖。
- **顺手修掉一个真 bug**：`agent_skills` 没有数据库级级联，自动绑技能上线后
  「删除员工」会因外键失败——现在删人前先清绑定。
- **契约**：`CeoPlannedSubtaskOut.required_skills`、`CeoNewAgentOut.skills/skill_errors`、
  `AgentSkillProvisionOut`；CEO 契约 23 项与技能配备 3 项测试全部通过。

### 新增：接口契约长尾（A3 续）

- 再补 68 个响应模型：`governance` 23/23、`tokens` 13/13、`war_room` 14/15、
  `artifacts` 16/17、`models_api` 8/12。全库已声明 `response_model` 的 JSON 操作
  **113 → 181**（460 条中占 39.3%）。
- 字段逐个对照真实返回值写，并统一 `extra="allow"`：模型写漏字段时**不会**把前端
  原本能拿到的数据裁掉——`response_model` 的序列化过滤是静默的，漏一个字段就是一次
  没有报错的线上事故。
- 有意不标注的 11 个端点各有硬理由：返回文件字节流（`FileResponse`）、SSE
  （`StreamingResponse`）、`Response(media_type="text/csv")`、204 无正文、
  以及**按设计不能声明**的 `/models/diagnostics`（它必须出现 `key_present` 布尔值
  而永远不能出现密钥本身，声明模型就等于把密钥结构写进契约）。

### 新增：失败请求必须进日志

- 此前 `uvicorn.access` 被压到 WARNING 且业务层不记 HTTP 状态，于是"前端调了后端
  不存在的地址"这类契约破坏**在后端日志里完全静默**——用户只看到一句 `Not Found`，
  运维端一条记录都没有。
- 现在所有 4xx/5xx 记一行：方法、路径（**不含 query string**，避免把令牌或密钥
  写进日志）、状态码、耗时、路由模板、`request_id`。
- 分级：5xx 与"路由没匹配上的 404"记 WARNING（这两类没人会注意到）；正常业务 4xx
  记 INFO（不抢视野，但不许彻底消失）。`request_id` 取自 `request.state`，与响应头
  `X-Request-ID` 一致，用户报障给一个 id 就能 grep 到整条链路。
- 护栏：`tests/test_failure_logging.py`（9 项，含"日志里不得出现 query 内容"）。

### 修正：逐条打印 SQL 与 `DEBUG` 解耦

- 此前 `echo=settings.debug`，而 `debug` 默认为 true。本机跑真实库时调度器每 15
  秒轮询一次，每次把 SELECT、参数、BEGIN/COMMIT 全量写两遍（stdlib 与项目
  formatter 各一次），14 分钟就 245 KB，日志里真正的错误被冲走了。
- 新增 `sql_echo`（环境变量 `SQL_ECHO`，默认 **false**），主 engine 与后台任务
  engine 都改用它；`DEBUG` 只负责 /docs、/redoc、/openapi.json 与 /metrics 的门禁。
- 顺带删掉 `meetings.py`、`workflows.py` 里两处 `settings.debug = False`——那是为了
  静音 SQL 而在运行时改全局配置，副作用是把 /docs 关掉、把 /metrics 的鉴权门禁改掉。
- `SQL_ECHO=true` 现在会进启动告警：绑定参数原样落日志，里面可能有密钥、token 与
  用户数据。护栏：`test_sql_echo_is_decoupled_from_debug`、
  `test_settings_debug_is_never_reassigned_at_runtime`。

### 修正：用量统计与治理中心的 4 个实测缺陷

补响应模型时对着真实返回值逐字段比对，暴露出 4 个"接口 200、页面不报错、数据却是
错的"问题——这类缺陷不会被编译、lint 或"能打开"任何一关拦住：

- `GET /tokens/pricing/models` 的 `provider_type` 用 `str(枚举)`，返回
  `ModelProviderType.OPENAI_COMPAT` 而不是全站统一的 `openai_compat`（另有 5 处
  都用 `.value`，这里是唯一的例外）；前端计费下拉把枚举名直接显示给用户。
- `GET /tokens/stats/by-task` 的 `name` 恒等于任务 UUID：反查只取 `obj.name`，而
  `Task` 的显示列叫 `title` → 任务维度一整列都是哈希。
- `GET /tokens/stats/by-scenario` 是六个维度里唯一不带 `name` 的，前端只能自己抄一
  份场景标签表——且抄的那份比后端多 `EXPERT_SEARCH`/`EXPERT_TEAM` 两个真实场景码，
  后端标签表反而漏了（明细流水的 `scenario_label` 对这两个也显示英文原码）。现在
  后端补齐标签、维度统一带 `name`，前端那份重复表删除。
- 治理中心「已安装」面板调的是返回**能力包目录**的 `/governance/marketplace/packages`，
  还把数组当 `{installations}` 对象读，因此永远显示"暂无安装记录"；企业模板面板同
  一 bug（`{templates}`）。新增只读接口 `GET /governance/installations?company_id=…`
  （join 出 `package_key`、回滚过的不计、租户边界由 router 级 `require_company_scope`
  覆盖），两个面板改为按真实形状读数组。
- 护栏：`tests/test_usage_stats_contract.py`（6 项，含一条"不许再出现
  `str(x.provider_type)`"的静态扫描）。**遗留**：能力包的"安装"动作在 UI 里仍没有
  入口（只有发布），所以「已安装」面板要有数据得先补安装按钮——这是产品决定，未擅自
  实现。

### 新增：响应形状门禁 `tools/check_response_shape.py`

- 上面那两个"数组被当对象读"的 bug 不是靠人眼能扫得干净的：TypeScript 里
  `api.get<{templates?: Template[]}>("/governance/templates")` 的泛型是人手工写的断言，
  **从来没和后端对过账**，所以类型检查、404 门禁、lint 全都放行。
- 新工具把两边都变成机器可比的形式：后端从 `app.openapi()` 现场解析顶层形状
  （沿 `$ref`/`allOf`/`anyOf` 展开，204 与非 JSON 跳过），前端从 `api.get<T>(path)` 的
  `T` 判形（`X[]`/`Array<X>` 是数组，`{...}`/`Record<...>` 是对象，具名类型回源码
  `type`/`interface` 再判，判不出算未知、不猜）。两边都判得出来且不一致才算违规。
- 已接入 `tools/check.sh`（六道门禁变七道）。当前 **226 处**带泛型的调用零错配；
  反向验证：把修复前的治理中心页面还原回去，工具准确报出那 2 处。

### 扩展：接口契约门禁覆盖移动端 `mobile/**`

- `tools/check_api_contract.py` 原来只扫网页端的 `api.*` 调用。移动端把全部后端路径
  集中写在 `mobile/lib/api.ts` 的 `call(base, token, "/path", {method})` 里，形态不同
  但同样可比——历史上它的 404（把 `company_id` 当 query 传给只接受路径参数的
  `/dashboard/{company_id}`）只能靠人肉发现，而门禁一片绿。
- 新增 `mobile_calls()`：按引号与括号配平取出第 3 个实参，只有**字面量**才参与比对
  （`call(base, token, path)` 这种透传看不到路径，跳过），HTTP 方法取第 4 个实参里的
  `method:`，缺省按 GET；路径里的 query 与 `${…}` 沿用原归一化规则。
- 当前网页端 204 处 + 移动端 **10** 处全部命中后端路由；反向验证：把 `/dashboard/{id}`
  改成 `/dashboards/{id}`，门禁准确报出 `404 路径不存在 mobile/lib/api.ts:150`。
- 有意保留的盲区（已写进工具文档）：`mobile/app/server.tsx` 里 2 处 raw `fetch`
  （探活 `/health`、升级检查 `/clients/update/check`）——路径拼在用户自填的服务地址上，
  走不到统一客户端层；两个路径本轮已人工核对存在。

### 新增：知识库域响应模型收口（A3 长尾，20/20）

- `/knowledge`（6 条）与 `/kb2`（14 条）全部声明 `response_model`，模型集中在
  `app/schemas/responses_kb.py`。这是「一句话 → 全公司干活」主线里唯一还没收口的
  数据域：知识库直接决定员工回答里引用了谁的资料。
- 顺带修掉一个实测出来的缺陷：`POST /kb2/agents/{id}/documents` **函数体里没有
  `return`**——请求 200，响应体却是 `null`，前端新建文档后拿不到 id，只能把整张
  私人库列表重取一遍。现在与公用库那条端点一样回 `{id, chunks}`。同一条路径上还漏了
  `cache_invalidate`：新建文档不失效进程内检索缓存，同一个问题会一直返回旧命中，
  新员工刚入库的资料读不到（`edit_doc`/`delete_doc`/`create_global_doc` 都有，只这一处漏）。
- `POST /kb2/agents/{id}/upload` 的两条分支键集不同（转换成功带
  `chunks/original_saved/size`，提取失败只带 `note`），模型里可选字段 + 路由带
  `response_model_exclude_unset=True`：没走到的分支不会凭空多出 `null` 键。
- `/kb2/copy` 与 `sync-from-global` 的 `count` 会**小于**请求的 `doc_ids` 长度：
  不存在或越权的源文档按"跳过"处理（不当 403 报出去，避免透露存在性）。这本来就是
  设计行为，但现在有了 `copied`/`count` 两个键，前端能如实显示"复制了 1/2 篇"。
- 新护栏 `tests/test_kb_contract.py`（14 项）：用临时库把 20 个端点逐个真跑一遍，
  断言的是**键集合相等**——响应多吐一个键、或模型漏声明一个字段（会被序列化静默裁掉）
  都会红；另含"这两个模块的端点必须全带 `response_model`"与"模型必须 `extra='allow'`"
  两条完整性检查。反向验证：临时摘掉 `/kb2/cost-profiles` 的模型，完整性检查准确报出。
- 棘轮上调：`test_response_models.py` 全库基线 **112 → 200** 条、占比 **23% → 42%**
  （当前实测 202/461 = 43.8%）。

### 新增：「老板批准」这条线响应模型收口（A3 长尾，21/21）

- `/intervention`（10 条）与 `/experts`（11 条）全部声明 `response_model`，模型集中在
  `app/schemas/responses_gate.py`。这两个模块是主线的两端：老板在干预中心点「批准」，
  专家库把人设变成上岗的员工——它们 returned 的形状一旦变了，中间没有任何一层会报错。
- 这两个模块的**分支返回特别多**，每条分支的键集都单独钉住：
  * `GET /intervention/policy`：库里有策略回 `{id, company_id, project_id, level,
    approval_actions}`，没策略回 `{company_id, project_id, level, approval_actions,
    is_default}`。靠 `response_model_exclude_unset=True` 让两条分支各回各的键；
    反向验证：临时摘掉 `exclude_unset`，默认分支立刻多出 `id: null` 并把测试打红。
  * `POST /intervention/requests`：自动放行只有 `detail`，落单待批只有
    `request_id` + `status`。
  * `POST /intervention/requests/{id}/decide`：普通单回 `{id, status}`，批准了挂了
    高危副作用的单才多一个 `execution`（失败分支是 `{ok, error}`，成功分支才是
    `{ok, result}`）。
  * `POST /experts/search`：命中是「完整人设 + `reason`」，LLM 没谈成是
    `{id: null, message}`——用 `ExpertPickOut | ExpertSearchMissOut` 联合建模，
    所以前端不会收到一张 `id` 为空的空白卡片当结果。
- `/experts` 的列表/详情/编辑/复制/新建共用 `ExpertTemplate.public_dict()`（17 键），
  模型也只有一个 `ExpertPublicOut`，其余继承或嵌套它：`public_dict()` 改一处，
  七条端点一起红，而不是各处悄悄漏。
- 新护栏 `tests/test_expert_intervention_contract.py`（15 项）：临时库真跑两个模块的
  端点，键集合**相等**断言；含「始终允许」→ 白名单 → 撤销的整条链、越权 404、
  vendor 导入 → 原创替换的计数（`created/updated/disabled_legacy` 是前端提示语的原料），
  以及两条完整性检查（这两个模块的端点必须全带模型、模型必须 `extra='allow'`）。
  LLM 相关的 search/team 用 monkeypatch 顶掉网关，零真实调用、零额度。
- 顺带修前端一处：专家库页的缓存 key 写成 `"experts:list"`（注释断言"专家库与当前公司
  无关"），但 `lib/api.ts` 会给 `/experts` 自动补 `company_id`、后端也只按公司过滤——
  于是**切公司后这一页还显示上一家公司的专家**，点「雇佣」会把别家公司的人设招进当前
  公司。key 改为带 `cid`，并补上"名下没有公司"的明确提示而不是空列表。
- 棘轮上调：全库基线 **200 → 220** 条、占比 **42% → 47%**（当前实测 223/461 = 48.4%）。

### 新增：「本机操作」这条线响应模型收口（A3 长尾，17/17）

- 沙箱与 Computer Agent 两个模块的 17 个端点全部声明 `response_model`：`/sandbox`
  10 条（工作区、会话生命周期、逐步操作、审批、历史、工作区文件）、
  `/computer-agent` 7 条（授权、权限总览、能力清单、执行、审计日志、截屏预览）。
  新文件 `app/schemas/responses_computer.py`，同样统一 `extra="allow"`。
- `POST /computer-agent/execute` 有四条互不相交的返回分支（命中免批白名单、
  高危落审批单、`dry_run` 预检、真实执行），合并成一个全可选模型 +
  `response_model_exclude_unset=True`：待审批那条不会凭空多出 `log_id: null`，
  前端 `res.status === "awaiting_approval"` 的判断与收口前逐字节一致。
  `POST /sandbox/sessions/{sid}/act` 只有两条分支且必填字段天然互斥
  （一条要 `seq`、一条要 `requires_approval`），因此用联合类型而不是合并模型。
- `/computer-agent/permissions` 声明成 `dict[str, bool]` 而不是模型：它就是
  `capability_status()` 的「能力名 → 是否授权」映射，键集合由 `EXTENDED_CAPABILITIES`
  决定、随执行器增减，写死字段等于每次加一种本机能力都要改契约。
- 可空字段按**数据库列的 nullable** 声明，不是按"当前代码路径看起来总有值"：
  `SessionAction.verified/before/after` 在「高风险待审批」记录上是 NULL，
  `ComputerActionLog.result` 在 `nl_computer.py` 的拒绝审计上是 NULL。
  两处都做过反向验证——把这三列改成必填，`GET /sandbox/sessions/{sid}/actions`
  立刻 500，正是收口要拦住的那类事故。
- 新护栏 `tests/test_computer_sandbox_contract.py`（11 项）：临时库真跑两个模块，
  键集合**相等**断言；覆盖会话「暂停后禁止操作 → 恢复 → 结束后 409」、
  高危动作两条分支、`save_file/delete_file` 与工作区文件表的联动与 200 字截断、
  未授权 403 / 未知能力 400 / 执行失败必须 `REAL_FAILED` 不许伪造成成功、
  审批门 → 免批白名单 → 撤销白名单恢复拦截、跨公司冒用他人 Agent 404、
  截屏预览只在沙箱根内可读且别人公司取不到，外加两条完整性检查
  （17 个端点必须全带模型、模型必须 `extra='allow'`）。
  **所有会碰本机的执行器都用 `monkeypatch.setitem` 顶掉**——测试不会真的截屏、
  敲键或启动 App。
- 棘轮上调：全库基线 **220 → 237** 条、占比 **47% → 51%**（当前实测 240/461 = 52.1%）。

## [5.15.0] 从审查到九分：契约、交付、多端与安全收口 — 2026-11-01

> 来源：五视角审查（[审查报告](docs/审查报告_AI-EOS_2026-10-31.md)）与
> [达到 9 分的升级报告](docs/升级报告_AI-EOS_达到9分_2026-10-31.md) 的 30 天冲刺项。
> 一次性发布：后端契约与响应模型、交付单闭环、移动端/PWA/Web Push、多端已读、
> 套餐/BYOK/账单、安全运营与部署脚本。
> 全量门禁：**781 passed**、e2e 38/38、408 条路径、后端行覆盖 **69.8%**。
> 自本版起 CI 默认**不随 push 自动运行**（节省 Actions 额度），发布前手动触发
> `ci` / `security`；客户端安装包仅在需要时手动触发 `clients`。

### 🛡 统一错误信封与响应模型（A3 续）

- 所有错误响应补齐稳定信封 `{detail, code, message, request_id}`：`detail` 与状态码保持原样，
  新增 `code`（400/401/403/404/405/409/422/429/500 映射，只读模式保留 `license_read_only`）、
  `message` 与 `request_id`；422 的 errors 列表结构不变。
- 新增 `app/core/errors.py`（`ApiError` 可选携带 code/extra）与 `app/schemas/` 响应模型；
  订阅、BYOK、公司检索、收件箱、队列、交付物、消息等端点补 `response_model`。
- 核心业务路由（员工/任务/项目/交付/评审/调度/技能/学习/绩效/反思）补到 **81/90 = 90%**，
  共 113 个模型类（`app/schemas/responses.py`，字段逐个对照真实返回）；全库 113/471 = 24%。
- 新增 `test_response_models.py` 棘轮：核心路由 ≥90%、全库声明数不得回退、新端点必须带模型。
- 测试：`test_error_envelope.py` 5 项（401/404/409/422/500、detail 保留、request_id 头体一致）。

### 📱 Web Push 推送（C6 续）

- 新增 `push_subscriptions` 表（Alembic `0004`）：endpoint 唯一、按公司/用户级联删除。
- `services/web_push.py`：VAPID 私钥/公钥只走环境变量，缺失时 `push_available=False` 并保持轮询；
  endpoint 必须 HTTPS 且复用 IM webhook 的 SSRF 校验；404/410 才删除订阅。
- 派发挂在 `after_commit` 出站队列，业务回滚不发推送；日志不回显 endpoint/密钥。
- `/m` 增加推送开关与权限引导；`sw.js` 增加 `push` / `notificationclick`（只允许同源跳转）。
- 测试：`test_web_push.py` + `test_notifications.py` 共 29 项。

### 🔄 多端已读同步（C3）

- 新增 `thread_reads` 表（Alembic `0005`）与 `GET/POST /thread-reads`：按用户 + 公司记录会话已读
  毫秒时间戳，批量合并只增不减，未来时间戳钳到服务器时间 +5 分钟。
- 左栏打开会话时上报，挂载 / 回前台 / 每 60s 拉取并按 max 合并；置顶/归档仍留在本机，
  只同步「读完没读完」这一判断。
- 测试：后端 `test_thread_reads.py` 8 项；前端 `threadReads.test.ts` 9 项 + `sessionPrefs` 新增 4 项。

### ⚡ Lighthouse 首屏门禁（C7 续）

- 新增 `apps/web/lighthouserc.json` 与 CI `lighthouse` job：`next start` 后对 `/login` 采样 3 次，
  硬断言 LCP ≤1.5s、TBT ≤200ms、CLS ≤0.1、性能分 ≥90。
- 本机实测：性能分 100、LCP 0.5–0.6s、TBT 0ms、CLS 0.005。

### 🧪 评测集扩到 60 条（B6 续）

- `tools/eval_tasks.seed.json` 由 10 条扩到 60 条（调研汇报 / 方案计划 / 数据分析 / 文案材料 /
  流程清单各 12 条），全部可机检；评测器无改动。
- 护栏加强：条数与分类精确断言、id kebab-case 与类别前缀一致、`expected` 至少 2 条规则、规则去重。

### 🧹 前端数据层迁移收尾（A6 续）

- logs / diagnostics / test-console / warroom / device-console / lan-share / workflow-builder /
  projects/[id] / agents/[id] / agents/[id]/profile 迁入 `useResource`。
- 刻意保留手写 `useEffect`：`tasks/[id]`（5s 轮询 + AbortController + summary 合并）、
  `workspace`（流式状态机）、`command`（无请求）、`app/(main)/page`（仅 setup 探测）。

### 🧪 测试补充与覆盖率口径修正（A7 续）

- 新增 `test_coverage_agents/tasks/projects/models_api/war_room.py`，补齐 agents、任务、项目、
  模型与作战室的读写/越权/非法流转用例。
- 修正覆盖率测量口径：SQLAlchemy async 在 await 边界交给 greenlet，coverage 未配
  `concurrency = ["greenlet", "thread"]` 会系统性丢追踪（同文件 67% vs 99%、33% vs 100%）。
  修正后后端全量 **69.8%** 行覆盖（21822 行、缺 6584），`fail_under` 40 → 65，CI 同步。
- 全量收集 781 项，`pytest --cov=app` 通过。

### 🧾 交付单：验收、退回与意见（B2）

- 交付物可关联任务（`task_id`，创建时校验同公司），列表支持按任务过滤；详情返回验收人与时间。
- 新增 `POST /artifacts/{id}/accept`（仅 APPROVED，幂等）与 `POST /artifacts/{id}/decline`
  （退回修改，状态回流 REJECTED 并清空验收信息）。
- 新增交付物评论：列表 / 发表（1–4000 字）/ 删除（作者或 MANAGE）。
- 任务页「待验收」可直接跳到交付详情并自动展开，完成「任务 → 交付 → 验收」闭环。
- 测试：`test_deliverables.py` 6 项（任务关联、跨租户 404、验收幂等/409、退回后重提、评论 CRUD/超长、非成员 404）。

### 🏭 行业包：SaaS 预设 + 一键脚手架（B1）

- 新增 `content/org_presets/saas.json`：互联网/SaaS，5 部门 13 角色，3 个交付物模板 + 10 个任务模板。
- `POST /org-presets/{pid}/scaffold` 按预设批量建交付物（含 v1 Markdown 骨架）与任务；
  支持 `dry_run`，按名称/标题幂等，MANAGE 权限、租户校验。
- 预设列表返回交付物与任务模板数量，安装前可预览。
- 测试：`test_industry_pack.py` 7 项。

### 🔎 公司大脑：本地混合检索（B5）

- 新增 `services/semantic_search.py`：CJK bigram 分词、停用词、10 组双向同义词扩展、BM25（k1=1.5/b=0.75），
  不依赖外部模型或密钥。
- `GET /search` 新增 `mode=keyword|hybrid`（默认 hybrid）：关键词路径保持不变，混合模式对公司内四类资料
  重排并返回 `score`，结果上限 20 条；未知模式 400。
- 测试：`test_semantic_search.py` 15 项 + `test_company_search.py` 10 项。

### 📥 收件箱与项目分组（C1/C2）

- 任务页拆为「收件箱 / 全部任务」，默认收件箱；`/tasks/inbox` 给出进行中/待验收/完成/失败四桶计数，
  15 秒轮询；待验收项直达交付详情。原有筛选、批量操作、模板能力保留。
- 会话增加 `project_id`：创建时可归属项目，`PATCH /conversations/{id}` 可改归属或清空；
  左栏新增「项目分组」开关（localStorage），未归属会话排在最后。
- 测试：`test_conversation_projects.py` 5 项 + `workspaceGroup.test.ts` 4 项。

### 🔔 IM webhook 出站（C4）

- 任务完成/失败、待审批、会议纪要三类通知在站内通知之外可同步推送到飞书/钉钉/Slack/通用 webhook。
- 支持 HMAC 签名（飞书 `timestamp\nsecret`、钉钉 query 签名、Slack 通用体），URL 校验拦截
  localhost、私有网段、链路本地与数字主机，日志不打印查询串。
- 投递挂在 SQLAlchemy `after_commit`（`after_rollback` 丢弃），业务事务回滚不会误发。
- 配置只走 `AIEOS_IM_WEBHOOK_URL` / `AIEOS_IM_WEBHOOK_TYPE` / `AIEOS_IM_WEBHOOK_SECRET`。
- 测试：`test_im_webhook.py` 26 项。

### 📱 移动端 `/m` 与 PWA（C6）

- 新增移动优先页面 `/m`：待验收 / 待审批 / 只读会话三页签，20 秒轮询（页面隐藏时暂停），
  未登录给出「请先在电脑上登录」。
- 新增 `app/manifest.ts`、`public/icon.svg`、`public/sw.js`：可安装、离线壳、导航 network-first
  回退 `/m`，静态资源 stale-while-revalidate，**不缓存 `/api`**。
- 根布局补 `viewport`（`viewport-fit=cover`、主题色）。
- 测试：`mobile.test.ts` 11 项；构建 `/m` 首屏 106 KB；`/sw.js`、`/icon.svg`、`/manifest.webmanifest` 均 200。
- Web Push/VAPID 见上文「Web Push 推送」。

### ⚡ 命令面板拆包（C7）

- `CommandPalette` 改为 `next/dynamic` 异步加载；打开入口收敛到 `lib/palette.ts` 的事件总线，
  经典外壳、工作台与侧栏不再静态依赖命令面板。
- 首屏 124.89 → 120.00 KB；全站 665.79 KB（97.9%）、单 chunk 52.40 KB，体积预算通过。

### 💳 套餐/试用、HTML 账单与 BYOK（E2/E4/E8）

- 订阅：`GET /subscription` 自动开 14 天试用（幂等），套餐目录 trial/pro/enterprise；
  一次性领取、切换、取消幂等；展示态派生 `TRIAL_EXPIRED` / `PAST_DUE`。`test_subscription.py` 6 项。
- 账单：`GET /billing/invoices/view` 输出可打印 HTML 账单（确定性发票号、动态文本转义、打印样式），
  MANAGE 权限。`test_invoice_html.py` 4 项。
- BYOK：`PUT/GET/DELETE /byok/{name}` + `verify`；Fernet 加密落库（HKDF-SHA256 派生），
  列表只回名称与时间，名称/长度非法统一 400。`test_byok.py` 7 项。

### 🔌 `/api/v1` 版本别名（A3 部分）

- 新增 ASGI 中间件：以 `/api/v1` 开头的请求重写 path 后交给现有路由，外部集成方可用稳定前缀接入；
  前端历史无前缀路径不变，OpenAPI 与鉴权逻辑不复制、不改变。
- 测试：`test_api_v1_alias.py` 4 项（健康检查等价、带/不带 Token 的 `/api/v1/auth/me`、未知版本路径 404）。
- 统一错误信封与响应模型见上文「统一错误信封与响应模型」；`response_model` 仍未覆盖全部历史端点（已知问题 17）。

### 🧹 交付物验收护栏修正

- `CommandPalette` 拆包后，前端「禁止原生阻塞弹窗」护栏误报 PWA 的 `BeforeInstallPromptEvent.prompt()`；
  改为绑定后调用，保持护栏不放宽。

### 🔐 安全：再收两处跨租户越权

- `GET /performance/agents/{id}` 补 `assert_agent_owned`，`POST /performance/company/{id}/snapshot`
  与 `GET .../history` 补 `assert_company_capability`（写用 OPERATE、读用 VIEW）。
  他司资源统一 404，与「不存在」不可区分，防 id 枚举。
- `POST /reflection/proposals/{pid}/decide` 先按提案找到所属 Agent，再校验该公司 MANAGE；
  `apply=true` 写知识库时使用校验过的 `company_id`。此前该 router 的 `require_company_scope()`
  在没有 `company_id` 参数时会跳过，等于从未覆盖这个端点。
- 新增 7 项回归用例（跨租户 404 + 属主正向对照），并做过去掉守卫必红的反向验证。

### 🧱 一致性：内置群聊幂等 + 配额闸门

- `POST /chat-groups/preset/{pid}/ensure` 用 runtime lock（`chatgroup:{company}:{preset}`，TTL 30s）
  串行化同一内置分组的创建；`chat_groups` 增加 `(company_id, preset_id)` 唯一约束兜底，
  冲突时回查已有组按幂等返回。**既有数据库不会自动获得该约束**（启动只建表/补列），
  运行时锁是兜底路径。
- 建员工前先过与 `POST /agents` 同口径的配额闸门：超限落审批单并 409，且一个员工都不落库。

### 🧵 作战室：缩短 SQLite 写事务

- `announce` 申明阶段在每次 LLM 调用前后提交，不再跨模型调用持有写事务，避免长事务把
  `database is locked` 抛给并发请求；返回结构不变。

### 🧩 工作台：长任务结果必达

- 切会话不再 `abort` 在途流（服务端生成完成后才落库，断流会丢整段回复）：只按会话 key
  跳过界面渲染，后台流结束后用服务端历史补齐；离开工作台仍会中止所有在途流。
- 会议结束后按 3s × 10 有界补拉，直到拿到纪要/摘要或取消；即使状态不是在当前页点的结束，
  从 RUNNING 落到 COMPLETED 时也会补拉。作战室发言后 5s × 4 有界补拉（替换原先两个裸
  `setTimeout`），运行态结束的瞬间再补几轮收回尾响应。定时器随切会话/卸载清理。
- 自动滚动只在用户本来贴着底部时跟随（±80px），上翻读历史不被打断。
- 左栏新增会话/员工搜索与分批显示（首屏 50 条，可继续加载），替代原来固定 14 条的截断。

### 🚀 上手：安装向导可续装

- 向导草稿写入 `sessionStorage`（白名单字段，不含密码 / Key），刷新或误关页面可恢复。
- 模型已保存但连通测试失败时，允许「完成」进入系统，并在工作台显示琥珀色修复提示；
  刷新后若检测到待安装标记与有效登录态，直接回到第 4 步继续修复。
- 第 1 步「创建公司」按钮在未填完时点击会定位到第一个错误字段；第 1、4 步提供免 Key 演示入口。
- 会议中心改为跟随「当前激活公司」，切换公司时清空上一家公司的会议详情与轮询。

### 🔭 可观测性与 CI

- 新增 `X-Request-ID`：合法客户端 id 透传，否则生成；写入日志（`[-]` 表示非请求上下文）、
  响应头、500 响应体，并通过 CORS `expose_headers` 暴露，报错截图可直接 grep 全链路。
- CI 后端 job 增加 `tools/e2e_smoke.py`（本地假模型 + 临时 SQLite，无密钥）。
  Postgres job 暂缓：e2e 脚本仍写死 SQLite，需先支持 `DATABASE_URL` 覆盖。

### 🔔 通知中心（站内 + 桌面）

- 新增 `notifications` 表（Alembic `0002`，append-only）：任务完成/失败、待审批、会议纪要
  三类事件落库；`GET /notifications`、未读数、单条已读、全部已读，跨租户一律 404。
- 前端铃铛 + 未读徽标接入经典侧栏与极客工作台，30s 轮询，页面隐藏时暂停；桌面通知只在
  用户点击授权后申请权限。IM webhook 渠道待定，未做。

### 🗂 会话管理（置顶 / 归档 / 未读）

- 左栏每行新增「…」菜单：置顶、归档、标记已读；筛选「全部 / 置顶 / 未读」，归档可展开。
- 偏好写入带版本的 `localStorage`（按公司 + 用户名隔离），防抖落盘；17 项单测覆盖。
- 「按项目分组」因会话线程没有项目字段暂未做，先用类型筛选替代。

### ⌨️ 全局命令面板（⌘K）

- 四组结果：页面 / 会话 / 员工 / 常用动作；支持键盘上下选择、Tab 焦点圈定、关闭后焦点归还，
  带 `aria-live` 播报；18 项单测 + 构建通过。

### 🧾 License、席位与账单

- 新增 Ed25519 离线 License 校验（`cryptography`，无新依赖）：缺失 = 社区模式；签名错误/过期
  = 只读；篡改即失效。校验结果按文件 mtime 缓存。
- 员工创建前增加席位闸门：超出售席返回 409 并落 `license_seats` 审批单 + 通知；只读模式写操作 403。
- `GET /license/status`；`GET /billing/usage`（员工/Token/成本/交付物聚合，按模型与按天）；
  `GET /billing/invoices/export.csv`（12 列月度账单，`decimal` 计价）。
- 未新增数据表，因此无 `0003` 迁移。

### 🛡 安全运营与供应链

- 新增 CodeQL（Python + TypeScript，security-and-quality）、Dependabot（按生态分组 minor/patch）、
  `SECURITY.md`（P0 24h 确认 / 72h 缓解，P1 7 天，P2 下个版本）与安全响应流程文档。
- 依赖审计先以观测态接入（`continue-on-error`），避免上游数据波动直接红掉主线。

### 🧪 评测集与租户矩阵

- `tools/eval_harness.py` + 10 条种子任务（5 类），规则评分可离线自测；真实模型基线待 Key。
- 租户权限矩阵生成器：19 族 / 44 端点 / 46 攻击组合，发现并修复
  `/automations/{id}/runs` 越权与 `/delivery/agents/{id}/folder` 坏导入两处真实缺陷。

### ⚡ 性能预算

- `tools/check_bundle_budget.py` 接入 CI：首屏 140 KB / 全站 680 KB / CSS 20 KB / 单 chunk 60 KB
  （当前基线 ×1.10）。基线 118.43 / 610.52 / 12.27 / 52.40 KB（gzip）。

### 🚢 部署与数据库迁移

- `apps/api/migrations/`：Alembic 基线 `0001`（107 表 / 281 索引）+ `0002` 通知表，
  `alembic check` 无 diff；`tools/db_upgrade.sh` / `db_downgrade.sh` + [数据库迁移](docs/数据库迁移.md)。
- `deploy/Dockerfile.api` / `Dockerfile.web`、根 `docker-compose.yml`、`.dockerignore`；
  `tools/backup.sh` / `restore.sh` / `healthcheck.sh` + [部署运行手册](docs/部署运行手册.md)。
  备份→篡改→恢复 sha256 一致；compose 本机未验证。
- CI 增加 Postgres 16 job 与端到端 smoke；覆盖率门槛 30 → 40（实测 47.9%）。

### 🐛 修复

- `GET /automations/{automation_id}/runs` 未校验归属：他司 automation id 可读到运行记录，
  现在先 `_owned_automation` 再查询。
- `GET /delivery/agents/{agent_id}/folder` 引用了不存在的 `agent_chat._owned`，属主访问直接 500；
  改用 `assert_agent_owned` 并补矩阵回归用例。

### 🔗 证据链（引用来源）

- 私聊调用 `web_search` 后，把命中的 `{title,url,snippet}` 写入 assistant 消息 `meta.citations`，
  并在非流式响应与流式 `done` 事件里返回同一份；前端在回复下方渲染「来源」列表，可点回原网页。
- 只保留 http/https 链接（拒绝 `javascript:`/`data:`），按 URL 去重、最多 5 条、摘要截断 200 字；
  没有来源时不显示空壳。9 项后端测试 + 11 项前端测试。

### 🔭 SLO 与告警

- 新增 [SLO 文档](docs/SLO.md)：API 可用性 99.5%、P95 延迟 <1.5s、任务成功率 ≥95%，
  含错误预算、P1/P2 分级与 PromQL 示例。
- 新增 `deploy/prometheus/alerts.yml`：5xx 比例、P95 延迟、任务队列积压、LLM 错误率、
  任务失败率、in-flight 异常共 6 条告警；测试会校验引用到的指标都真实存在。

### 🛡 License 只读模式全局生效

- 新增 ASGI 中间件：License 过期或签名不可信时，除 `/auth`、`/license`、`/setup`、`/health`、
  `/metrics`、文档等恢复/观测路径外，所有 `POST/PUT/PATCH/DELETE` 一律 403
  （`code=license_read_only`），GET/HEAD/OPTIONS 不受影响。403 仍带 `X-Request-ID`。
- 白名单保证拿到新 License 后仍能登录、重装、查看状态，不会把只读模式变成死锁。
- `test_license_read_only_gate.py` 8 项：过期写拦截、读放行、`/auth` `/setup` 放行、
  篡改失败关闭、有效 License 与社区模式不误伤、CORS 预检放行。

### 🧪 租户矩阵扩展到 25 族 / 51 端点

- 新增 schedules、mailbox、learning、growth-center、skills、reviews 六个资源族共 10 个端点。
- 矩阵新发现并修复 4 处真实缺陷：
  `GET /schedules/{id}/runs`（可读他司调度历史）、`PATCH /learning/learning-records/{id}`
  （可改他司员工学习进度）、`GET /reviews/agents/{id}`（可读他司考核与评语）、
  `PUT /skills/{id}/scope` 对他司技能返回 409（可枚举 id）→ 改 404。
- 训练会话创建即调用 LLM，无法同步断言，登记进 `SKIPPED_SURFACES` 并写明原因。

### ⚡ 前端数据层迁移（automation / meetings）

- 两页从手写 `useEffect + setState` 迁到 `useResource`：缓存首屏立即渲染、切公司按新 key
  自动重取、窗口 30 秒后回窗重取；创建/改名/启停/复制/运行/结束/发言后统一 `refresh()`。
- 会议页把会议/部门/项目三个请求合并为一个 bundle，并用 cid 标记避免切换瞬间显示上一家的数据；
  详情 6 秒轮询与结束后的有界补拉保持原样。

### 🎭 免 Key 演示可带走交付物

- 演示完成后可「下载 .md」或「复制全文」；文件名与 Markdown 由纯函数
  `lib/demoDeliverable.ts` 生成（本地日期、路径分隔符清洗、固定演示声明），6 项单测。
- 仍是纯前端模板，不调用模型、不写数据库；所有演示消息保留「🎭 演示」标识。

### ⚡ 数据层第二批迁移与强制刷新竞态

- artifacts / delivery / intervention / projects / knowledge / schedules 六页迁入 `useResource`：
  缓存首屏、切公司跟随、回窗重取、写后 `refresh()`；各页原有筛选、弹窗、分段失败提示与
  轮询行为保持不变。
- 修复 `useResource` 的强制刷新竞态：`refresh()` 不再并进尚未返回的旧请求，改为发出新请求；
  每个 key 维护请求代号，旧响应晚到时不覆写缓存、不清除新请求的在途标记、不 `setData`。
  同时切 key 时清空上一屏的错误，避免新公司页面先显示旧公司的失败原因。

### 🧾 账单对账验证

- 新增 `test_billing_reconciliation.py` 4 项：`/billing/usage` 总数与按模型/按天分桶同原始
  `TokenUsage` SQL 求和一致（边界日 00:00 / 23:59:59 与区间外行都覆盖）；CSV 汇总与用量接口
  一致（`Decimal` 分级容差）；席位用量与 `status != DISABLED` 员工数一致（含停用前后变化）；
  两公司用量互不串账。

### ⏱ 交互延迟采样

- 新增 `lib/perf.ts`（纯函数 + 会话级 200 条环形缓冲，可注入 Storage，SSR 安全）与
  `PerfMonitor`：记录路由切换后第二帧绘制耗时与 `longtask`，诊断页展示 P50/P95/P99/max；
  只存本会话，不上报、不采集身份信息。17 项单测。
- 构建体积预算继续通过：首屏 123.59 / 全站 631.33 / CSS 12.50 / 单 chunk 52.40 KB。

### ⏰ 任务模板 → 定时任务

- 调度引擎新增 `create_task_from_template`：按调度所属公司校验模板归属，读取模板首个
  `create_task` 步骤，创建 `TODO` 任务并写 `TaskEvent` / `EventLog`；脏优先级降级为 NORMAL，
  非本公司/已删除的指派员工自动清空，缺少创建人时回退公司 owner。
- 自动化页对 `trigger_config.kind=TASK_TEMPLATE` 的行显示「📋 模板」徽标与「⏰ 定时」：
  可选「每隔 N 分钟」或「每天 HH:MM」，直接创建 `POST /schedules`。
  非模板规则行为不变。后端 7 项测试覆盖创建、跨租户拒绝、缺失/非模板、字段清洗与触发校验。
- 调度仍是至少一次语义（120s 抢占租约），执行超过租约时可能重复建任务；任务与事件里带
  `template_id` / `schedule_id` 便于对账。

### ⚡ 数据层第三批迁移

- companies / experts / governance / memory / organization / reflection / token-stats / factory
  八页迁入 `useResource`：账号级与公司级 key 分开，写操作后 `refresh()` 或按前缀 `invalidate`；
  组织变更同时失效 `agents:`，专家雇佣/组队同时失效 `factory:`。
- `useResource` 覆盖页数达到 25；全站 JS gzip 641.06 KB（预算 680 KB，用量 94.3%），
  后续页面迁移需同步关注拆包。

### 📐 原型（待确认，未改代码）

- `apps/web/public/prototype/inbox-deliverable.html`：任务收件箱 / 交付单 / 导航收敛（42 → 12）
  三合一静态原型，供确认信息架构后再落代码。

## [5.14.0] 界面双模式：经典固定左栏 / 极客三栏工作台 — 2026-10-30

### 🎛 首次选择 + 随时切换

- 首次进入系统先二选一（`ModeChooser`）：两套界面功能完全相同，只是用法不同。选择存在
  `localStorage.aieos_ui_mode`；之后可从 ⌘K、经典侧栏底部、极客左栏底部的「⇄」随时切换。
- **经典模式**恢复 v5.12 的形态：`ClassicLayout` 提供固定左侧菜单（分组折叠、⌘K 搜索、
  最近访问、模式切换、退出登录），功能页整页打开，不做 URL 改写。
- **极客模式**保持 v5.13 的 `/workspace` 单外壳：功能页开在右栏标签，对话与左栏不中断。
- 切换时自动落点：经典 → 极客把当前页作为右栏标签带回工作台；极客 → 经典把 `?app=`
  指向的页面整页打开。
- `/` 按模式分流（经典 → `/dashboard`，极客 → `/workspace`），登录后先回 `/` 再分流。
- 经典模式下 `/workspace` 依然可用，但只保留「对话 + 产出」：左栏由 `ClassicLayout`
  提供，不再叠一列极客导航；不显示标签与「＋ 打开页面」，也不恢复历史标签。
- `openApp()` 按模式分派：经典走路由跳转（`registerAppNavigator`），极客走右栏标签
  （`registerAppOpener`）；内嵌 iframe 两种模式都剥掉外壳。
- 验证：`./tools/check.sh --fast` 全绿、后端 324 项用例通过；浏览器走查覆盖
  首次强制选择、两种模式导航、双向切换落点、刷新持久化、极客直达页改写与经典三栏页。

### 🧭 极客模式重排：按「会话」组织，而不是按「页面」

- **左栏**改成统一会话流：一个列表同时装单聊、项目群、会议、作战，按「进行中 → 待处理 →
  已完成」排序，顶部筛选（全部 / 单聊 / 群聊 / 会议 / 作战）、员工头像、6 个功能磁贴与
  「全部页面与操作」浮层；底部保留模式切换、收起与退出。
- **中栏**变成四种会话形态共用一个对话框：单聊（原有流式接口）、群聊（项目群聊）、
  会议（开始 / 暂停 / 继续 / 结束并生成纪要，进行中 4s 轮询）、作战（过程 + 分工 + 群聊，
  6s 轮询）。同一套输入框，只是收件人不同；`⌘N` 仍可换人新开单聊。
- **右栏**打开功能页时扩到窗口一半并给左栏让位（自动折叠、关闭页面后还原），
  `Esc` 依次退出展开态与页面；普通拖动仍是 360–760。
- 会议 / 群聊 / 作战的完整视图保留下沉入口：会话头「会议中心」「作战室全屏」，
  左栏浮层里的原页面照常可开。
- 经典模式不受影响：隐藏形态切换条与会议 / 拉群按钮，仍是「经典左栏 + 单聊 + 产出」。

### 💬 群聊不再绑定项目：自由拉群 + 内置分组

- 左栏新增「群聊」区：两个内置分组「业务群聊 📣」「产品研发群聊 🛠」（来自
  `content/chat_groups/*.json`），以及任意自建群——「＋ 新建」起名、勾人（1–12 位），
  随时改名、换人、删除。
- 内置分组缺人时，**点开就按预设生成员工**：姓名 / 职责来自分组配置，绑定专家库人设；
  没有对应专家模板时先用职责描述兜底。生成后就是普通员工，可继续培训、升级、调岗；
  重复点开幂等，不会重复创建。
- 群消息复用 `group_chat_messages`（`scope="group"`）：发言顺序、知识库与搜索工具和项目群聊
  完全一致；会话头「发起会议」默认拉本群成员，「群成员」直接打开编辑。
- 新接口：`GET /chat-groups`、`POST /chat-groups`、`PATCH|DELETE /chat-groups/{id}`、
  `POST /chat-groups/preset/{pid}/ensure`；新增 `chat_groups` 表，启动时自动建表。
- 后端 5 项单测覆盖内置补齐幂等、同名复用、自建群增删改与租户隔离；浏览器走查在
  `/tmp` 数据库副本上验证「点开自动生成 → 5 位成员就位 → 新建 / 编辑 / 删除自建群」，
  未触碰真实库。

### 🔐 安装第 4 步：给 AI 员工接上大模型

- 向导从 3 步扩到 4 步：建完公司后直接「选服务商 → 粘 API Key」，一次完成
  **登记模型 → 保存密钥 → 连通测试 → 绑定全部员工**，四段进度条逐步点亮；
  失败可原地重试，也可「跳过，先进入」。预设覆盖 Agnes（国内可用）/ DeepSeek /
  OpenAI / Anthropic，只填 Key 即可。
- **模型从列表里选，不用手敲**：填好地址与 Key 后点「读取模型」，直接调用服务商的
  模型列表接口，下拉选择即可（读不到时保留「手动填写」兜底）。同时新增「自定义 API」
  预设：任何 OpenAI 兼容接口（vLLM / Ollama / OneAPI / 自建网关）都能接，本机无鉴权
  服务可把 Key 留空。`POST /models/providers/list-models` 增加 `provider_type`，
  Anthropic 自动改用 `x-api-key` 头。
- 新接口 `POST /models/env-key`：把密钥写进本机 `apps/api/.env`（权限 600、
  原子替换、保留其它配置、同步进程环境立即生效）。仅超级管理员可用，变量名限定
  `OPENAI_COMPAT_API_KEY` / `ANTHROPIC_API_KEY` / `GEMINI_API_KEY`，响应只回掩码，
  密钥不上传、不入库、不回显。
- 新接口 `POST /models/{id}/bind-agents`：把模型整批绑给公司现有员工（默认只补
  未绑定，可整体替换），替代逐个设首选模型。
- 工作台在「没有模型」时显示提示条（可关闭，按公司记忆），点「去接入」直达模型页；
  模型页与旧版 `OnboardingWizard` 同样支持界面填 Key、一键「绑定全员」。
- 修复：删除仍被员工绑定的模型会因外键约束 500，现在先解绑再删（员工回到未绑定），
  并新增回归测试；`.env` 临时文件清理失败改为落日志。
- **自建模型的报错不再只有一句状态码**：上游 4xx/5xx 的响应体会脱敏（密钥打码）、
  截断后附进错误消息，能直接看到是「模型名不存在」「上下文超限」还是「参数不支持」；
  同时遇到「tools / max_tokens / temperature / stream_options 不被支持」这类 400 时
  自动去掉该参数重试（其余 400 不瞎重试），自建 vLLM / Ollama / 网关的兼容性明显变好。
- **401 会直接说清下一步**：没带 Key 提示去「模型配置」补 Key；带了 Key 仍被拒，
  提示 Key 可能失效或站点不匹配（Agnes 国际站 `.com` 与国内站 `.cn` 的 Key 不通用）。
  不再让用户对着 "Token not provided" 反复怀疑自己的配置。
- **429 / 额度不足也各说各的**：限流提示"稍后重试或换模型"，账户欠费提示
  "去控制台充值，这不是 Key 配错"；429 的退避从 0.5s 起步改为 2s→4s，并优先采用
  上游 `Retry-After`（封顶 8s），实测 Agnes 3.0-flash 间歇性限流时能自动穿过去。
- 连通测试的 `max_tokens` 从 10 提到 64：推理型模型（如 agnes-2.5-flash）会把
  前 10 个 token 全用在 `reasoning_content`，导致"✅ 连通正常"但回复为空。
- 修复：`AIEOS_ENV_FILE` 之前**完全没生效**，`load_env()` 始终只读 `apps/api/.env`。
  演示/便携环境因此加载了真实 `.env`、取到空 Key，上游报 401；现在设置了
  `AIEOS_ENV_FILE` 就只加载它，真实 `.env` 不会再混入隔离环境。
- 修复：登记模型时 `api_key_env` 留空会让模型行"不指向任何密钥"（.env 里配了也取不到）。
  现在留空自动落到 provider 默认变量（`OPENAI_COMPAT_API_KEY` 等），PATCH 同理。
- 修复：**界面上换了 Key 却不生效**。`settings.*` 是进程启动时的一次性快照，
  `resolved_api_key()` 又优先读快照，于是用户在「模型配置」保存新 Key 后，后端仍用
  旧 Key 发请求，上游一路 401 Invalid token，只有重启才恢复——而界面已经提示
  "已立即生效"。现在运行期的 `os.environ` 优先于启动快照，保存后下一个请求即用新 Key。
- 验证：新增 14 项后端单测（权限、密钥形态、.env 无损与 600、绑定/替换/删除、
  租户隔离、列表接口的 provider 鉴权头）与 9 项网关/环境用例（错误原文可见且密钥打码、
  tools / max_tokens / temperature / stream_options 降级、401 两种提示、env 文件隔离、
  默认变量名），后端全量
  395 项用例通过，并用真实 socket 假网关复现「不支持 tools + 上下文超限」验证降级链路；
  浏览器从零走查安装 →
  自定义 API 读取模型列表 → 下拉选中 → 发消息收到回复 → 删模型看提示条 →
  回模型页补 Key，`tsc` / `eslint` 无告警。走查用演示库与 `AIEOS_ENV_FILE` 隔离，
  未触碰真实库与真实 `.env`。

### 🌐 联网搜索恢复：Agent 真的能查到今天的天气了

- **根因不是没接线，而是搜索后端的选择**：`web_search` 工具早就接进了单聊、群聊与任务链，
  但后端用的是 `html.duckduckgo.com` 的 **GET** 请求——该端点对 GET 一律返回 202 反爬空页；
  `cn.bing.com` 301 回首页、`www.bing.com` 对中文查询丢词（"北京今天天气"只按"北京"返回）、
  360 与 m.baidu 连发几次就跳安全验证。于是"网络明明是通的，Agent 却说搜不到"。
- 改为 **DuckDuckGo HTML 版 POST** 为主后端：同一端点换 POST 即返回 10 条真实结果，
  中英文与时效性都正常；偶发 202 按 2s/4s 退避重试（实测第 2–3 次即恢复），
  再依次降级 Brave → Bing RSS → ddgs。实际使用的后端写进 `backend` 字段，便于排障。
- 新增进程内结果缓存（10 分钟、最多 64 条）与最小请求间隔 0.8s：同一会话反复问同一问题
  不再重复打免费后端，也降低被限流的概率。
- 单聊工具补上 `web_fetch`（搜索摘要只有标题时，抓权威页面正文核对数字），
  工具调用统一带 `company_id/agent_id` 进审计日志；提示词要求"最多搜 2 次，
  摘要没数字就抓正文"，并在人设里注入当前日期——此前模型不知道今天几号，
  会搜"北京天气 2026年7月"这种过期查询。
- 修复流式聊天的两个收尾问题：工具轮次用尽仍没吐正文时，去掉 tools 再请求一次，
  强制模型基于已拿到的结果作答（此前前端只看到状态提示和空回复）；
  工具轮次预算 2 → 3，覆盖"搜索 → 抓正文 → 作答"。
- 修复 `web_fetch` 在聊天中必现的 `database is locked`：工具用独立会话写审计日志，
  而聊天会话此时还握着未提交的用量记录，SQLite 只允许一个写者。现在执行工具前先把
  本会话待提交写入落盘。
- 验证：新增 6 项搜索解析/降级/缓存单测；全量后端用例通过；演示环境用「产品经理」
  Agent 实测"今天北京天气怎么样"——搜索命中 `duckduckgo`（查询自动带 2026-10-31），
  抓取 weather.com.cn 与 tianqi.com 正文后给出 6–14℃、湿度 30%、AQI 66 的实时答复。

## [5.13.0] 单界面工作台：所有功能开在右栏标签里 — 2026-10-30

### ✨ 新首页 `/workspace`

- 三栏：左「数字员工 / 作战会话 / 常用页面」、中「一句话指挥」（附件 / 技能 / 选员工）、
  右「浏览器 / 产物 / 轨迹」。数据全部走现有接口，没有新增后端模型。
- 两侧栏宽度可拖动（7px 分隔线）：指针拖拽 + 方向键微调 + 双击/回车复位；
  左 208–380、右 360–760、中间始终 ≥420；刷新后保留（`localStorage`）。
- `⌘B` 收左栏 / `⌘J` 收右栏 / `⌘N` 新对话（选人）；窄屏 <900px 两侧改为抽屉。
- 右栏：浏览器 = 员工视角正文阅读（`/browser-agent/fetch`）；产物 = 交付文件 + 成果库；
  轨迹 = 该员工近期任务 + `/tasks/{tid}/trace` 时间线（含成本与失败原因）。

### 🚚 路由与桌面壳

- 首页改为 `/workspace`；原指挥中心搬到 `/dashboard`，侧边栏与 ⌘K 均可达。
- 作战会话以标签形式开在右栏（`/warroom/{sid}`）；需要沉浸时点「展开」占满右栏。
- mac 桌面壳改 `titleBarStyle: hiddenInset` + 红绿灯留位；页面顶部让出 34px 并铺拖拽区。

### 🧩 单一界面：所有功能开在右栏标签里

- `/workspace` 成为唯一外壳。侧边栏导航、⌘K、页面内互相跳转的入口全部改为
  `openApp()`——功能页以**标签**形式开在右栏，对话与左栏原地不动，不再整页跳走。
- 右栏标签：「产出」固定第一个，其余按打开顺序追加、可关闭；关掉当前标签落到左边那个（和浏览器一致）。
- 标签内容用 iframe 承载（`?embed=1`，内层隐藏侧边栏/标题栏，Toast/确认框/⌘K 保留）：
  切换标签不丢列表筛选与滚动位置；内层页面自己跳转时标签标题跟着变，但不影响对话。
- 去重按标签**当前地址**：标签跳进详情页后再点左栏同名页面，会新开一个列表标签（不再被带回详情）。
- 焦点在内嵌页面里时，`⌘K`/`⌘B`/`⌘J`/`⌘N` 由内层转给外壳处理（否则会被页面吞掉）。
- 直接访问 `/tasks`、`/knowledge` 等旧地址一律客户端改写到 `/workspace?app=%2Ftasks`，
  地址栏始终只有一个界面；刷新/分享链接都能还原出同一组标签（`?app=` + `localStorage`）。
- 「展开」把功能页吃满中间与右侧（对话留在后台不卸载，Esc / ⌘J / 「回到对话」退出）；
  窄屏上面板本就是整屏，自动隐藏「展开」，顶部只留「回到对话」。
- 原来的全局侧边栏外壳（`components/Layout.tsx`）随之删除，避免两套导航并存。
- `lib/nav.ts` 补登 `/`（跳转别名，`hidden`），导航覆盖用例继续按原断言生效。

### 🌊 聊天流式输出

- 网关新增 `BaseModelProvider.stream()` / `OpenAICompatProvider.stream()` / `ModelGateway.stream_with_agent_models()`：
  OpenAI 兼容 SSE 逐字下发；服务端不支持 `stream_options` 时自动降级；**首个 token 之前**才做重试与换模型，
  吐字之后出错直接上报，避免两个模型的文本拼在一起。
- 新端点 `POST /agents/{id}/chat/stream`（`text/event-stream`）：`status` / `delta` / `done` / `error` 四类帧；
  生成器自带 session，老板消息先落库，中途失败也不会丢掉这条指令。
- 文档指令标记 `[GENERATE_DOC]` 用前缀保留过滤器挡住，流里不会漏出半截系统指令；落库与历史仍是干净正文。
- 前端 `streamPost()`（`lib/api.ts`）复用统一鉴权/401 刷新/超时收口；工作台对话与员工详情对话都改为逐字渲染，
  带闪烁光标、状态行、停止按钮与「本轮未保存到历史」的如实提示。

## [5.12.0] 一人公司主线打通：一句话 → 全公司干活 → 拿到结果 — 2026-10-29

> 目标形态：老板说一句，公司各个岗位自己把活干完，老板直接拿结果。
> 本轮带着这个目标重读代码，发现主链路上有四处**功能上是断的**——
> 派活不启动、任务没工具、Office 产出物打不开、简报时区偏 8 小时——
> 先把它们接上，再补「一句话直达」入口。检查：295 个后端用例 + e2e 38 项全绿。

### 👑 一句话直达（新入口）

- `POST /command/run`：一次请求完成「理解 + 执行」，返回 `plan`（让老板看到 AI 怎么理解的）
  与执行结果。`auto_run=true`（默认）。
- DIRECT 路径：把 `DispatchIn(auto_start=True)` 传下去，任务创建即入队执行。
- PROJECT 路径：作战室 `kickoff` 跳过逐人宣告（成员任务直接标 acknowledged）
  与负责人校对，直接进入 `confirmed` 并派发全员后台执行。
- 指挥台前端：新增「⚡ 一句话直达」按钮，原「下达」改名「先看方案」保留两步式；
  直达同样复用轮询（全终态 / 无存活任务 / 10 分钟硬上限即停）。
- `GET /command/report-templates`：对外暴露报告骨架，前端与 Agent 共用同一份定义。

### 🚀 派活真的会开工

- 新增 `app/task_runner.py`：`start_task`（CAS TODO→RUNNING + 投递）/ `dispatch_auto_queue`
  （按 URGENT/HIGH/NORMAL 拉起，单拍最多 8 个）。
- `Task.auto_start`（可空 Boolean，默认 false）：`ensure_columns` 幂等补列，老库升级无需手工迁移。
- worker 池满（`QUEUE_FULL`）：任务退回 TODO 且**保留** `auto_start`，调度循环下一拍再拉起——
  不再出现「RUNNING 但没有 worker 在跑」的僵尸行。
- 调度循环每拍（10s）独立 try/except 调用自动排队，单次失败不影响其它定时任务。
- `POST /task-dispatch/dispatch` 增加 `auto_start`，返回 `started_count` / `queued_count` /
  每条任务状态；前端「创建后立即开工」默认勾选。

### 🔧 Agent 真正具备取数能力

- 新增 `app/tools.py`：统一工具注册表（`web_search` / `web_fetch`）、唯一执行入口、文本形式
  `<tool_call>` 兜底解析。网络类工具复用 `fetch_page_text` 的 SSRF 闸门与审计日志。
- `run_agent_on_task` 改为工具循环：最多 4 轮、12 次调用，结果截断 6000 字符回填；
  工具失败转结构化 error 让模型换源，不拖垮任务；`metadata_json.tools_used` 留痕。
- `build_context` 注入工具纪律提示（先搜索再作答、标注来源、error 要换源）。
- 能力包工具名对齐真实实现（`web_browsing` → `web_search/web_fetch`；
  `computer_use` → 真实的 `computer_*` 工具名）。
- 任务详情 / 摘要接口返回 `tools_used`，前端执行记录显示「🔧 使用工具」。

### 📊 专业报表与报告

- 新增 `app/report_templates.py`：5 类骨架（行业研究 / 经营分析 / 周报 / 项目复盘 / 方案书），
  按标题+描述关键词命中；自动注入章节结构、数据表规则、「暂缺（建议采集方式）」占位与
  `[GENERATE_DOC]` 产出规范。
- `doc_generator` 重写：
  - Word：真实表格（`Table Grid`）、h1–h4、有序/无序列表、`**加粗**`、
    Normal 字体（中文 eastAsia=宋体）、页边距；
  - Excel：按 `##` 小节拆多 Sheet，数值/百分比还原为数字类型，表头样式、冻结首行、自动筛选；
  - PPT：按 `##` 分页（h1 不再重复封面也不再产生空页）、原生表格；
  - `[GENERATE_DOC]` 标记中的 `\\n` 还原为真实换行。
- 战报/作战室交付物：word/ppt/excel 走真实渲染并登记 `Asset`；LLM 失败时**不落文件**、
  任务标 `failed`（不再把「执行失败」写成交付物）。

### 🕘 简报与收口

- 晨间简报：数据库时间戳是 naive UTC，原实现用本地 `datetime.now()` 比较，
  UTC+8 下"昨日/今日/今日成本"整体偏 8 小时；改为本地自然日 0 点换算 UTC。
- 修掉 `company.created_by` 引用不存在字段导致的 500（非属主访问简报直接崩）。
- 自动化 `call_webhook` 补 SSRF：出站前校验 URL，连接建立后复核真实对端（防 DNS rebinding）。
- 模板建公司（`create_from_template`）补公司归属校验；任务派活目标一律按 `company_id` 过滤。

### 🧪 测试

- 新增 `test_doc_generator.py` / `test_auto_dispatch.py` / `test_agent_tools.py` /
  `test_command_run.py` / `test_briefing.py`，并在 `test_outbound_security.py`
  增补自动化 webhook 的 SSRF 用例；后端 261 → **295**。

## [5.11.0] 按技术方向复审后的第二轮加固 — 2026-10-28

> 来源：按技术方向重做的复审（`docs/审查报告_AI-EOS_2026-10-28.md`，54 项发现 / 10 个 P0）。
> 本轮把其中的 P0 与第二批 P1 全部落地；每一项都跑了对应门禁（261 个后端用例 + e2e 38 项）。
> 升级前请先备份 `data/`（新增的 `event_log.company_id` 由启动时幂等补列）。

### 🚪 安全：越权读清零 + 出站加固

- 15 个「双租户探针确认真的能读到」的端点（专家库、协作消息/协作图、定时任务、
  Agent 记忆、复盘概览、治理自主档位/概览/自改进案例、技能列表、技能成长树、
  应用别名规则、自动化列表、考核周期创建）全部补上归属校验，越权统一 **404**。
- 新增 `require_company_scope()` 路由级门禁（`app/core/permissions.py`）：
  请求带 `company_id` 即校验归属，不带则跳过——挂到 router 上，新增端点默认被覆盖。
  接入 experts / collaboration / memory / reflection / governance / skills /
  skill_growth / automation / review / schedules / command 共 11 个 router；
  app-alias 逐端点加 `assert_company_owned`（`/platform` 不读租户数据，不挂）。
- 静态越权读债务登记表：**52 → 1**（仅剩 `/app-alias/platform` 这一条扫描误报，
  已有行为用例钉住）。护栏同时学会识别 route 级门禁与 `assert_agent_owned` /
  `_assert_selectable_company` 两种既有守卫，不再误报。
- 事件日志补 `company_id` 列与严格读过滤（NULL 不返回）；导出上限 5000 行。
- 模型配置禁止引用服务端敏感环境变量：写入侧（create/update/test/list-models）
  校验之外，`settings.resolved_api_key` 在**读取侧**再兜一层——升级前落库的
  `api_key_env=JWT_SECRET` 历史行，运行时同样取不到值。
- 出站：外部 Agent / 网页抓取 / webhook 关闭自动重定向、逐跳校验对端地址；
  云元数据地址在任何场景（含本地模型）都拒绝。
- Computer Agent：`type_text/click/key/run_shell/run_write/run_app` 纳入高危能力，
  走审批；`execute_nl` 带 `company_id` 并限制在存储根目录内。
- 上传（S9）：1MB 块流式落盘 + `Settings.max_upload_mb`（默认 25）+ 扩展名白名单 +
  服务端生成文件名 + 落盘路径根内校验；超限 413 且不留半成品。
- 技能插件（S8）：删除 `sys.path.insert`，改为唯一模块名加载；manifest 必填
  name/version/module，入口只允许点分模块名且必须位于插件根内；插件根不可写时告警。
  （签名/发布者校验仍未实现，已在代码注释中明确标注。）

### ⚙️ 并发与数据层正确性

- SQLite：WAL + `foreign_keys=ON` + `busy_timeout=15s` + `connect_args timeout=15`，
  每个连接复用 PRAGMA；后台执行器加信号量（`AIEOS_JOB_WORKERS`）与 `QUEUE_FULL` 背压。
- 审批决策、任务/工作流/会议状态迁移改 **CAS**（`UPDATE ... WHERE status=:old` +
  `rowcount`），新增 `runtime_locks` 租约表（TTL），副作用只发生一次。
- 删除路径去掉 `DELETE ... USING` 与 `randomblob()` 等 PG/SQLite 专属写法；
  批量接口修 savepoint 误用；事务拆短，避免长事务持锁。

### ⚡ 性能

- 首页合并为 `/dashboard/{company_id}/bootstrap`：一次返回仪表盘 + 部门概览 +
  运行中任务（`limit 20`，不含 description/result）；计数全部改 `GROUP BY`；
  前端串行 3 请求 → 1 请求，轮询 5s → 15s。
- 新增 `/tasks/{id}/summary` 供轮询：不返回 `result` 全文，execution 输出截断 2KB，
  事件只回最近 100 条；前端首屏与操作后走全量，轮询走 summary。
- 作战室新增 `/war-room/sessions/{sid}/snapshot`：session + team + 聊天增量
  （`since_id`，默认最近 100 条），替代每 3 秒 5 个串行请求；`/agents`、`/companies` 只拉一次。
- RAG：SQL 侧按查询 token 收窄候选集（`limit top_k*20`）后再精排，无命中时回退到
  有界窗口；知识块不再每次全表拉进 Python。技能 scope 批量查询。
- 群聊：显式 `max_tokens=512`、单次 90s、按公司 `Semaphore(4)` 限流、任务描述截断。

### 🖥 Electron / 移动端

- 更新包：URL 白名单 + basename + 强制 sha256 + 解压目录根内校验；`will-navigate` /
  `will-redirect` 拦截；`sandbox: true`；spawn 子进程加 `error` 监听；表单窗口改 `data:` URL。
- 移动端：`/dashboard/{companyId}` 路径修正；审批改为真实端点
  （`GET /intervention/requests` / `POST .../decide`）；Android 增加
  `network_security_config`（targetSdk 34 禁止明文）。

### 🧪 测试与门禁

- 新增：`test_tenancy_read_attacks`（29）、`test_outbound_security`（11）、
  `test_concurrency_guards`、`test_sqlite_concurrency`、`test_delete_portability`、
  `test_events_isolation`、`test_computer_gate`、`test_upload_limits`（8）、
  `test_read_views`（4）、`test_lifespan`（2）。
- 覆盖率门禁进 CI：`--cov=app/api/routes --cov=app/core`，实测 35%，阈值 30。
- `lifespan` 首次被真实执行（此前 ASGITransport 直连，启动副作用从未跑过）。
- 后端用例 **171 → 261**，e2e 38/38 通过。

## [5.10.0] 安全与正确性加固 — 2026-10-27

> 来源：一份五视角独立检测（`docs/检测报告_AI-EOS_2026-10-27.md`）。
> 本轮只做**已复现**的缺陷修复，不加新功能——三个 P0 都是"看起来正常、实际从未生效"的类别。
> 升级前请先备份 `data/`（老库会自动补列，见下文）。

### 🔐 P0-1 JWT 空密钥/公开密钥可伪造 token（最严重）

`jwt.encode(payload, "", "HS256")` 在**空密钥**下照样能签出合法 token，而 `get_current_user`
用同一个空密钥校验通过。`.env.example` 里 `JWT_SECRET=` 是空的，桌面/便携版又不设
`AIEOS_ENV=production`，于是旧的安全闸门形同不存在——实测用空密钥签出的 token
可直接读到他人公司的员工与数据。

- `jwt_secret` 默认值改为空串**并触发密钥引导**：`secrets.token_urlsafe(32)`，
  按 `AIEOS_JWT_SECRET_FILE` → SQLite 同目录 → `storage/` → `apps/api/data/` 顺序落盘（0600、`O_EXCL` 防并发互踢）；
  目录不可写则降级为进程内随机密钥 + 大声 WARNING。**任何路径都不会退回空密钥或公开常量。**
- 生产环境（`AIEOS_ENV=production`）**不自动生成**：仍用公开/空密钥直接拒绝启动。
  自动生成会把"忘记配密钥"这件事悄无声息地糊过去，生产必须炸。
- 桌面壳自己生成并注入密钥（`userData/jwt-secret.key`），不再依赖后端可写。
- 便携启动脚本导出 `AIEOS_JWT_SECRET_FILE` 并提示备份。
- 复验：用 `""` / `aieos-portable-secret` / `change-me` / `secret` / `ai-eos` 伪造 token → **5/5 全部 401**（改造前 `""` 与公开常量均可通过）。

### 🚪 P0-2 跨租户越权（读面为主）

写路径原本有归属校验，**读路径几乎没有**：带着别人的 `company_id` 就能列出对方成果、
知识库文档、审批策略与白名单。

- `deps.py` 新增 `is_company_owned`（批量跳过用，不抛异常）、`assert_kb_doc_owned`。
- `kb2 / artifacts / intervention / tasks / orchestrate` 读写全部补 `assert_company_owned`，
  越权统一 **404 而非 403**（403 会告诉攻击者"这个 ID 存在"，等于租户枚举）。
- 新增护栏测试 `test_every_company_id_endpoint_has_an_ownership_guard`：AST 扫描 GET 端点，
  认「本文件内一跳校验辅助函数」；存量债务登记为 `_READ_DEBT_ALLOWLIST`（69 条，
  **修一处删一处，新增未登记的越权读直接红**）。
- **但静态护栏挡不住"伪校验"**（校验的是调用方自己传进来的 `company_id`，代码里确实"有校验"，
  扫描器看到的是绿灯）。所以再加一份运行时攻击用例 `tests/test_tenancy_attacks.py`（9 例）：
  真实 HTTP 打过去，断 404 且断属主仍可用（防止"用 404 把功能一起锁死"的假修复）。
  并做了反向验证——临时摘掉 `list_artifacts` 的归属校验，用例立刻红（200=泄露），确认后还原。
- 复验：31 项跨租户攻击实测 **31/31 全部拦截**，且正常属主 0 误伤。

### 🗄 P0-3 SQLite 上没有 `NOW()`：发布功能从来没成功过

`POST /artifacts/{id}/publish` 里 `SET published_at = NOW()`——SQLite 无此函数，
必抛 `no such function: NOW`。更根本的是 **`Artifact` 模型压根没有 `published_at` 列**，
所以"发布"这一步在 SQLite（即桌面/便携主形态）上从未生效过。

- 模型补 `published_at` 列 + Alembic 迁移 `a1c7e9c0ffee`；
  启动 `ensure_columns` 对 SQLite/PG 均幂等补列，老库无需手工迁移。
- 写入改绑定参数，统一 **naive UTC**，与 `created_at` / `_now()` 可比。
- 审批策略读取失败改为 **fail-closed**（默认需要审批）——读不到策略不能等于放行交付物出内网。
- 复验：`submit → review approve → publish` 全链路 200，`published_at` 真实落库，重复调用幂等不覆盖。

### 🖥 桌面版默认只监听 127.0.0.1

此前无条件 `--host 0.0.0.0`，同一 WiFi 下任何人都能打到你的后端端口。

- 默认绑定 `127.0.0.1`；新增「局域网共享」开关（开→绑 `0.0.0.0` 并重启后端，关→回到仅本机）。
- `docs / redoc / openapi.json` 只在 `DEBUG=true` 挂载：关 DEBUG 后路由根本不存在（实测 404），
  不再把 312KB 接口清单交给任何扫到端口的人。
- `/metrics/prometheus` 加中间件守卫：配了 `METRICS_TOKEN` 走常量时间比较，未配则**仅回环地址**可抓。
- mDNS 广播与共享开关联动：只监听回环时不再对外宣告一个连不上的服务。

### ⚙️ 后端正确性（本轮一并修的已复现项）

- **会议纪要**：LLM 抽取路径存在死代码/重复取模型，无模型时曾会崩 closing 流程；改为带日志的关键词兜底。
- **预算暂停恢复**：`status == "PAUSED"` 大小写不匹配，超预算后**点提高上限也恢复不了**；改 `func.lower(...)`，兼容历史两种写法。
- **日预算窗口**：`TokenUsage.created_at` 实为 naive UTC，而窗口边界用了 aware datetime → 比较异常导致当日统计失真；统一 naive UTC。
- **工作流**：Agent 节点失败 `rollback` 后隐式返回 `None`，被当成"等待人工"永远挂着；现在标 FAILED 并对称写回错误。
- **阻塞 IO**：`skills.py` 三处 `urlopen` → `httpx.AsyncClient` + 并发抓取（单仓失败只记日志）；`auth.py` 登录限流改 `redis.asyncio`，Redis 不可用降级内存计数。
- **静默异常清零**：`clients / command / experts / performance_engine / mdns_advertise / main / config` 的 `except: pass` 全部补 `logger.warning/error`；`redis_url` 那句"未实际使用"的注释已失效并更正。

### 🎨 前端

- 收口最后一批裸 fetch（`pair`、`UpdateBanner`），下载与轮询走统一层。
- 任务详情页竞态：快速切换任务时旧请求会覆盖新数据 → 加序列号守卫 + 卸载时 `AbortController`。
- `lib/api.ts`：登录入口的 401 不再触发"刷新—重试—再 401"风暴；支持外部 `signal` 取消与 `silent`。
- 错误不再被 `.catch(()=>{})` 吃掉，统一 Toast 播报。

### 🧨 版本字符串六个地方六个值

实测 `pyproject=0.1.0`、`desktop=5.9.0`、`web=5.0.0`、`mobile=4.1.0`、mDNS 广播 `5.4`、
FastAPI docs `0.1.0`——用户报障时截图里的版本号对不上任何一次发布。
新增 `apps/api/app/version.py` 作为后端唯一来源，其余位置从这里取，
并由 `test_version_strings_are_consistent` 跨端钉死（改乱会立刻红）。
五端版本统一到 **5.10.0**（5.3–5.9 仅发布于 GitHub Release，CHANGELOG 未记录，属文档缺口）。

### 🛡 CI 门禁

新增 `.github/workflows/ci.yml`：backend（pytest + compileall，**显式不起 Redis**——
`test_auth` 断言内存回退字典，起了会让限流失效）/ frontend（tsc + vitest）/ contract（OpenAPI、
mapper、`check_api_contract.py --strict`）三个 job，`concurrency` 取消旧任务，无部署步骤。
`tools/check_api_contract.py` 补 `retry`/`takeover` 动作，并把"路径中段变量"从宽松匹配
改成还原真实字面量后**逐条要求后端路由存在**（拼错动作名不再报绿）。

### ✅ 验证

| 项目 | 改造前 | 改造后 |
|---|---|---|
| `uv run pytest` | 159 例，**3 失败** | **171 passed, 1 skipped, 0 失败** |
| 跨租户攻击实测 | 读路径普遍可越权 | **31/31 拦截**，属主 0 误伤 |
| 伪造 token（公开/空密钥） | 可通过 | **5/5 拒绝（401）** |
| `npx tsc --noEmit` | — | 0 错误 |
| `npm run test`（vitest） | — | 39/39 通过 |
| 契约门禁 `--strict` | 3 处红 | 通过 |
| `./tools/check.sh`（含端到端主链路真进程试跑） | — | **全部通过，e2e 38/38** |

### ⚠️ 已知限制与待决策

- **`alembic upgrade head` 只支持 PostgreSQL**：链路里 `23e9b97bb927` 在 SQLite 上会抛
  `No support for ALTER of constraints`（历史遗留，非本轮引入）。桌面/便携形态走
  `create_all + ensure_columns`，不受影响；`服务器一键部署.sh` 已容忍该失败。
  但 `docs/08_DEPLOYMENT_GUIDE.md` 让 SQLite 用户直接跑 alembic，属文档错误。
- **Mac 代码签名私钥已提交进仓库**（`packaging/mac-signing/*.p12(.b64)`，口令 `aieos2026`，
  且脚本会把它装进系统信任根）。这是不可逆操作（改写 git 历史 / 轮换证书），
  **需要 maintainer 决策**，本轮未改动。
- 注册接口仍匿名开放；绑定收口后风险面已从"整个局域网"缩到"本机 + 显式开启共享"。
- 读端点归属债务 69 条为**基线登记**，非"已修完"。

## [5.2.0] 客户端自动升级 — 2026-10-18

> 各端客户端启动后自动从「你自己的服务器」检查并升级，不再需要手动找 Release 重新下载。

### 后端：更新通道 API（apps/api/app/api/routes/clients.py）
- `GET /clients/update/check?platform=&arch=&current=`：与 `storage/clients/manifest.json` 比对版本号，返回是否有新版 + 安装包下载 URL + 大小 + 更新说明；manifest 未部署时优雅返回 `has_update=false`（客户端静默，零影响）
- `GET /clients/update/download/{filename}`：下发 `storage/clients/` 下的安装包（防目录穿越）

### 桌面壳（Electron）
- 启动后自动检查更新：新版本 → 底部浮出升级横幅（新特性说明 + 进度条）
- 一键升级（按平台自动应用）：
  - **Windows**：NSIS 静默安装（`/S`）→ 自动重启，全程无感
  - **macOS**：自动打开 dmg 安装器并引导替换 /Applications（替换系统目录需要管理员权限，无法完全静默）
  - **Linux**：AppImage 场景原地替换并自动重启；权限不足时引导手动替换
- 菜单「查看 → 检查更新」手动触发；下载/安装/重启进度事件实时回推前端

### Android（Expo）
- 服务连接页新增「自动升级」区：检查更新 → 发现新版 → 下载 APK（带进度）→ 系统分享面板选择「安装」（react-native-share）

### 发布工具（tools/publish_client_updates.py）
- 一条命令从 GitHub Release 拉 5 个主安装包 → 写入服务器更新目录 + 生成 manifest.json
- 凭证自动读 `~/Documents/张阗/.github_token`（或 `GH_TOKEN`）
- 用法：`python3 tools/publish_client_updates.py [--release 标签] [--dir 目录]`

### 验证
- 后端 check/download API 全链路通（真实 manifest + 真实安装包）
- web / mobile / desktop 三方类型与语法检查全过

## [5.1.0] 多平台客户端（Mac / Windows / Linux / Android）— 2026-10-18

> 把 AI-EOS 打成「直接安装」的客户端安装包。iOS / 鸿蒙本迭代不做。

### 桌面客户端（Electron，desktop/）
- 新品牌图标（深色玻璃拟态 + 渐变轨道环 + AI/EOS 字标 + 呼吸点），多尺寸 ico/png
- 主进程重构：服务器地址可配置（`userData/settings.json`，可指向本机便携 / 远程自托管）
  - **打包安装场景**：app 内无仓库文件时不再强拉服务——探测不到服务显示「服务未检测到」引导页（玻璃拟态，可重试 / 换服务器 / 打开数据目录）
  - **开发场景**（仓库内 `npm start`）保留原行为：复用已监听端口，否则自拉起后端(8001)+前端(8000)
- 产物：`AI-EOS-5.1.0.dmg`（Intel）/ `AI-EOS-5.1.0-arm64.dmg`（Apple Silicon）/ 同名 zip ×2
- 验证：electron-builder 双架构构建 0 错、SMOKE 冒烟 0 错、打包 .app 实跑正常加载运行中的服务

### Android 客户端（Expo，mobile/）
- 老板视角 App（KPI / 一句话 / 签字门 / 连接服务器），V4.1 起已有
- 本次：`versionCode` 补齐、真图标替换 1×1 占位、版本 → 5.1.0
- 自签 p12 keystore（`packaging/keystore/ai-eos.p12`，100 年有效期）→ gradle `assembleRelease` 出发布版 APK

### 客户端流水线（.github/workflows/clients.yml）
- 4 平台并行：macos-latest(dmg+zip) / windows-latest(NSIS 安装器 + 绿色版) / ubuntu-latest(AppImage + deb + Android APK)
- 汇总 job 自动发 GitHub Release，挂全部安装包（tag `clients-*` 或手动 dispatch 触发）
- 说明：安装包均为内部自签（未做 Apple/Windows 公证）——分发给自己的设备时按系统提示放行即可

## [5.0.3] 附件口补全（建团队 / 冷启动 / 部门 / 项目）— 2026-10-18

> 延续 V5.0.2 全系统附件口：把剩余输入表单的📎接通，并修掉一处「附件口在 UI 但 payload 漏发」的假成功。

### 附件口补全
- **建团队（TeamGenModal）/ 冷启动向导（OnboardingWizard）**：建公司入口接📎挂资料，`team_generator.GenerateIn` 收 `attachments` 并落库到新公司
- **组织架构页（部门）**：新建/编辑接📎；`DepartmentPatchIn` 加 `attachments`，编辑时载入已有附件、PATCH 带回，避免把附件覆盖成空
- **项目页**：建项目表单早已有📎口但创建 payload 漏发 `attachments`（用户挂了文件却不入库，属假成功）——`projects` 表 + ORM + `ProjectIn` + 前端 payload 全链路补上

### 验证
- 前端 `tsc` 0 错、`next build` 0 错
- 后端：projects 带附件创建 + 读回 200；team-generator preview 收 attachments 200；测试数据已清理

## [5.0.2] 全系统附件口 + 级联删除修复 — 2026-10-17

> 输入表单全系统接通📎附件口（创建时真实落库、列表回传）；修删公司 500、外部接入注册 422 两个 P0。

### 全系统输入表单接通 📎 附件口
- 数据层：`companies / departments / automation_definitions / automation_schedules / training_sessions / external_agent_connections / agent_factory_jobs` 7 张表统一加 `attachments JSON` 列
- ORM：7 个模型类 + 7 个路由入参模型（`CompanyIn`/`DepartmentIn`/`DefinitionIn`/`ScheduleIn`/`SessionIn`/`ConnectionIn`/`BlueprintIn`/`AgentIn`）接收 `attachments`，创建时落库、列表端点回传
- `Agent` 无附件列 → 走已有 `memory_config` JSON 列存引用（零 DDL）
- 前端 6 页接入通用 `AttachmentPicker`（真实上传）：数字员工 / 自动化规则 / 调度 / 训练 / 外部接入 / 蓝图画布

### 修 P0：删公司 500（级联缺口）
- 根因：指向 `companies.id` 的 9 张子表外键全为 `NO ACTION`，删公司时子行残留 → `IntegrityError`；且 `Company.departments` 缺级联，ORM 把子行 `company_id` 置 NULL 撞 NOT NULL
- 数据库层 10 张子表外键改 `ON DELETE CASCADE` + ORM `ForeignKey(ondelete="CASCADE")` + `Company.departments` 加 `cascade="all, delete-orphan"`
- 实测：删带部门的公司 `500 → 204`，子行父行全清

### 修契约错位：外部接入注册 422
- 前端发 `provider`，后端 `ConnectionIn` 必填 `provider_type`（claude/codex/hermes 实际为 API/LOCAL/REMOTE 枚举）→ 字段名对齐，注册 `422 → 201`

### 验证
- 前端 `tsc --noEmit` 0 错误；后端全文件 import OK
- 7 类资源「带附件创建 → 读回 attachments 落库 → 级联删除」全链路回归通过，测试数据已清理

## [5.0.0] 专家 × 组织形态版 — 2026-10-17

> 大版本：新增数据模型（agent 绑定专家）+ 跨模块能力（专家库管理 / 数字员工联动 / 行业组织形态一键落地）。

### 专家库补全到 302（缺口 40）
- 起初 vendor 导入漏抓上游子目录，缺 40 个专家（游戏系 Unity/Unreal/Godot/Roblox/Blender、工程深水区 RAG/隐私/WebAssembly/支付/实时协同、安全审计、测试自动化等），非表面的「10 个」
- `tools/rewrite_experts.py` 后台补齐 40 位，全部过 10-gram 相似度门（最高 0.0150 < 0.02），概念简报仅喂「职业名+部门+emoji」
- 专家库 262 → **302**，`import-original` 全量入库，前端可见 302 位

### 专家库三大操作能力（编辑 / 复制 / 手动新建）
- `PUT /experts/{id}` 编辑专家：改 姓名/emoji/部门/简介/system_prompt，重提交付物章节
- `POST /experts/{id}/copy` 复制专家：生成「名称（副本）」，继承人设章节
- `POST /experts/manual` 手动新建专家：不依赖 302 预设，凭空定义人设（`source_slug=custom-*` 与预设隔离），**表单必带 📎 附件口**
- 前端：详情弹窗加「✏️ 编辑」「📋 复制」；页头「＋ 手动添加专家」弹窗（玻璃拟态 + 附件选择器）

### 数字员工 × 专家库打通
- `agents` 表新增 `expert_template_id` 列（关联 expert_templates.id）
- 「招新员工」：📚 从专家库选择（可搜索 302 位）→ 自动回填姓名/岗位/部门/人设；也可纯手动，两者共存
- 「批量添加」双模式：✍️ 手动输入 / 📚 专家库勾选（多选一键批量生成，带 expert_template_id）
- 后端 `create_agent` / `batch_create` 绑定专家时自动取 system_prompt 作人设 + 雇佣计数

### 行业组织形态预设 + 一键引入
- `content/org_presets/*.json`：内置 8 行业（互联网/制造/零售电商/医疗/教育/金融/建筑地产/政府），共 56 岗位，每岗位推荐对应专家
- 新增 `org_presets.py` 路由：`GET /org-presets` 列表 · `GET /org-presets/{id}` 预览 · `POST /org-presets/{id}/apply` 一键落地（部门树 + 配备专家的数字员工）
- 支持 dry_run 预览、按部门勾选引入、幂等（已存在自动跳过）、统一模型绑定
- 前端组织架构页：🏢 组织形态 选择器 → 预览弹窗（部门可勾选 + 已存在数提示）→ 一键引入

### 文案清理（展示层去原创字样，合规底裤保留）
- 删专家库「📜 原创声明横幅」+「AI-EOS 原创版」「AI-EOS 原创」展示文案（登录页/副标题同步改「专家 × 组织形态版」）
- **保留** `docs/EXPERTS_STATEMENT.md`（法务/审计看）· `vendor/.../LICENSE`（MIT 合规）· DB 的 source/license 字段（数据层留痕，不展示）

### 版本标识
- package.json / 登录页 / 侧边栏 → V5.0 · 专家 × 组织形态版

## [4.2.0] 专家全原创版 — 2026-10-17

### 桌面客户端（V4.0-P1）
- 新增 `desktop/`：Electron 壳（main.cjs + preload.cjs）把便携后端(FastAPI/SQLite@8001) + Web 前端(Next.js@8000) 装进原生窗口，不重写任何 UI
- 主进程平台感知：dev 场景复用已监听端口，双击便携场景自拉起子进程（uvicorn + next start）并等待就绪
- 数据单文件 `data/aieos.db`；API 端口烘焙一致性检查（.next 不对就自动重建）
- electron-builder 三平台打包配置（mac dmg/zip、win nsis/portable、linux AppImage/deb）
- 修复：Electron 关闭路径递归死锁（will-quit preventDefault 死循环）；冒烟测试模式 `AI_EOS_DESKTOP_SMOKE=1`

### 移动端（V4.1 · 老板视角）
- 新增 `mobile/`：Expo Router + React Native 老板随身 App（KPI / 一句话下达 / 签字门 / 服务连接 四 Tab）
- 一句话下达含📎附件口，方案必须老板"确认执行"才真跑（延续可视化确认点哲学）
- 共用与桌面壳/后端同一套 API（`/auth` `/command` `/experts` `/dashboard`）；token 走 expo-secure-store 加密持久化
- Metro 打包已验证：900 modules → 2.5MB HBC，TypeScript 类型检查归零
- 签字门真审批 / 呼吸灯动画 / 附件选图 留 V4.2

## [4.2.0] 专家全原创版 — 2026-10-17

### 专家库内容 100% 原创重写（商业化合规）
- 全部 262 位专家人设由 AI-EOS 团队 LLM 独立完整重写，存 `content/experts/`（source=ai-eos-original，license=AI-EOS 原创）
- 合规红线：生成时只喂「职业名+部门+emoji」概念层，**绝不喂上游正文**；生成后 10-gram 相似度门对上游原文硬拦截（全量最高 0.0199 < 0.02）
- 新增 `POST /experts/import-original`：按 source_slug 原地替换旧 MIT 记录 + 自动下架未覆盖的上游记录
- 三重留痕：`docs/EXPERTS_STATEMENT.md`（完整声明）· 专家库页面原创声明横幅 · 每条记录 source/license 字段
- vendor 瘦身：上游 266 份原文移入 `vendor/agency-agents-zh-archive/`（本地留档，.gitignore 不进仓库），主目录仅留 LICENSE 等 5 个合规文件
- 批量生成器 `tools/rewrite_experts.py`（并发 + 429 退避 + 断点续跑 + 相似度门）

### 专家库「完整展示」升级（参考上游一页一份档案形式）
- 详情弹窗从「截断简介 + raw_md 堆叠」升级为**章节化人设档案**：8 大章节（身份记忆/核心使命/关键规则/交付物/工作流程/沟通风格/成功指标/进阶能力）结构化卡片，前 2 节默认展开
- 章节标题按类型着色（使命=青 / 规则=琥珀 / 交付物=翠绿 / 指标=绿 …），保留 Portal 居中弹窗 + 玻璃拟态设计体系
- 卡片「AI-EOS 原创」绿色标识，替代旧的「MIT 许可」字样

### 版本标识
- 登录页 / 侧边栏 / package.json → V4.2 · 专家全原创版

## [4.0.0] 一句话指挥台 — 2026-10-17

### 一句话指挥台（Command Deck）
- 新增 `POST /command/plan`：一句话 → LLM 判断走 DIRECT（直接派活）/ EXPERT（专家库组队）/ PROJECT（作战室立项），方案不落库
- 新增 `POST /command/execute`：老板确认后执行，三路全部复用既有引擎（task_dispatch / experts / war_room）
- 新增 `GET /command/report/{ref}`：执行摘要 + 单文件 HTML 报告（可直接发群分享，含 token 费用）
- 新增 `POST /command/notify`：报告推钉钉/飞书/企微 webhook（按域名自适应消息体）
- 前端「📣 一句话指挥台」页（指挥中枢分组）：一句话输入 + 📎附件口 + 方案确认弹窗（含预计耗时/分工/理由，可改可拒）+ 执行结果控制台 + 报告分享 + 群推送
- 修复：Postgres 模式 tasks.progress / current_stage、agent_executions.completed_at 缺列（补 tools/ensure_columns.py 一键补列脚本）
- 登录页/侧边栏版本标识 → V4.0 · 一句话指挥版

## [3.0.0] 专家融合版 — 2026-10-17

### 专家库融合（agency-agents-zh · MIT）
- 新增 `expert_templates` 表 + 导入器（vendor/agency-agents-zh，265 位专家）
- 专家 API：`/experts/import`（幂等导入）、列表/详情/部门统计、
  `POST /experts/search`（P1 自然语言找专家）、`POST /experts/team`（P2 一句话组队）
- 专家 → 工厂蓝图：`POST /experts/{id}/to-blueprint`，走既有 审查→验收→上岗 确认链
- 前端「📚 专家库」页（组织与智能体分组）：部门筛选、卡片、详情弹窗、雇佣、找专家、组队
- 登录页/侧边栏版本标识 → V3.0 · 专家融合版

## [2026-10-14] — 五视角代码审查整改升级

> 依据五视角审查报告的 Top 10 + P0/P1/P2 整改项。升级前已全量备份
> （`~/Documents/张阗/ai-eos-backup-20261014`）。Mac 无 shell，所有改动为静态编辑，
> Python 文件已过 `py_compile` 语法校验；**上线前请在 Mac 上跑一遍 `pytest` 回归**。

### 任务执行链路（重度用户 P0）

- 后台引擎泄漏修复：`_with_fresh_engine` 的 finally 里 `await engine.dispose()`
- 超时按任务类型拆分：会议 600s，任务/编排/工作流读 `AIEOS_TASK_TIMEOUT_S`（默认 7200s）；
  超时先写事件告知用户再标 FAILED
- 新增 `POST /tasks/{tid}/retry`：FAILED→PLANNING→RUNNING 并重新提交后台工作；
  前端任务详情 FAILED 状态加「🔁 重试」按钮
- `resume_execution` 改为真重跑（从 checkpoint 恢复，旧执行标 `superseded`）；
  暂停按钮旁提示「暂停将在当前步骤完成后生效」
- lifespan 新增 `recover_stuck_tasks()`：RUNNING/PAUSED 超 30 分钟（`AIEOS_STUCK_TASK_MINUTES` 可配）
  无心跳标 FAILED 并写事件
- Task 模型新增 `progress`（Float，默认 0.0）与 `current_stage`（String，可空）；
  前端任务列表/详情加进度条
- `schedules.py` pause/resume 补归属校验；`departments_batch.py` cascade 改为真递归删除（已核查外键安全）
- `automation_engine.py` 新增 `run_workflow` / `call_webhook` / `update_task` 三个真实动作
- 编排链路补「产出即归档」：CEO 汇总后调 `save_artifact`（此前只写 `parent.result`）

### 模型网关与计量（程序员/运营 P0）

- `record_token_usage` 只 add 不 commit，提交权交还调用方（已审计全部调用方外层均会 commit）
- `usage_ctx` 改为上下文管理器（退出 finally reset）；training / factory / meeting / chat 补齐 agent / company 维度
- failover 每次失败记 `logger.warning`，最终异常附全部失败摘要；失败调用记入 token 流水（`success=False`），
  `model_calls.success_rate` 口径修正
- 简易熔断/冷却：单模型连续失败 5 次冷却 5 分钟，429 直接短路；猴子补丁转正为类方法
- `PROVIDERS` 支持 `plugins/providers/*.py` 动态加载（`register()` 约定，单文件失败隔离）
- `knowledge_engine.retrieve` 加 `limit` 分页（默认 200，SQL 层截断）

### 架构与安全（程序员 P0/P1）

- `plugins/skills/` 真动态加载（`manifest.json` + `importlib`，单 manifest 失败只记 warning）
- `test_code_quality.py` 静默吞异常检测升级为 AST 级（行尾注释 `pass` 不再逃逸；
  `agent_runtime.py:64-65` 已同步修复为 `logger.warning(..., exc_info=True)`）
- `app/sandbox.py` 改名 `app/computer_workspace.py` 消歧（import 已全仓同步）
- 新建 `app/engines/` 包：新引擎公约 + 渐进迁移文档（现有引擎暂不搬移）
- RefreshToken：`POST /auth/refresh`（轮换，响应 `{access_token, refresh_token}`）+ `POST /auth/logout`（吊销），
  库里只存 SHA-256 哈希；附 Alembic 迁移
- 登录限流 Redis 优先（无 Redis 回退内存 dict，对外行为不变）
- `ensure_columns` 只在 sqlite 方言下运行（PostgreSQL 交给 Alembic）

### 前端与向导（小白 P0/P1）

- 全局快捷键：`n` 新建任务、`/` 聚焦搜索、`j/k` 列表导航、空格多选、`?` 帮助面板
  （`lib/shortcuts.ts` + `GlobalShortcuts.tsx`）
- 术语统一：「＋ 招新员工」、「人设与职责设定」、「OpenAI 兼容地址」、「首选模型」；⌘K 占位与 keywords 对齐
- 向导步骤 2：每个模型预设加「Key 从哪来」一句话指引；`.env` 指引重写为小白 4 步操作（含便携包无 `.env` 情况）
- 建员工自动绑定公司首选模型（`governance.create_from_template`）；新增免 Key 纯前端演示模式（零 DB 写入、全程标注「演示」）
- `lib/api.ts`：401 自动调 `/auth/refresh` 换 token 重试一次；登录页存 `refresh_token`
- vitest：`vitest.config.ts` + `lib/api.test.ts` + `lib/validate.test.ts`（静态正确，未运行）

### 交付运维（运营 P0/P1）

- **LICENSE**：BSL 1.1（BUSL-1.1）——Licensor eos.hyipf.com（2026-10-14 晚由
  山东阗言密语寅卯辰文化传播有限公司变更）；免费层：个人学习/研究、非商业用途、
  团队规模不超过 3 人的团队内部使用；超 3 人使用或云端/公网/SaaS 部署须商业授权
  （登记 eos@hyipf.com）；Change Date 2030-10-14（满 4 年转 Apache-2.0）；
  对外称 source-available；`pyproject.toml` / `package.json` 同步标识
- 打包说明：账号三处收敛为「首次使用请在登录页注册」（删弱口令节）；`rm -f data/aieos.db` 改为先备份再重建；
  Python 要求更正为 3.12+
- `start-mac.sh`：`cd` 到 `apps/api`、自动装依赖、日志 `>>` 追加、启动前快照（留 5 份）；
  `start-win.bat` 纯 batch 重写（补 `sqlite+aiosqlite:///` 前缀，修 npm 未加 call 的 bug）
- 根目录 `.env.example`（中文注释模板）；`/health/db` 异常返 HTTP 503；日志按日轮转（`AIEOS_LOG_DIR` 可配，保留 30 天）
- `GET /metrics/prometheus`（手写 exposition，无新依赖）；`GET /events/export`（csv/json 流式）；
  成果删除 + zip 打包下载；资产改相对路径（兼容旧绝对路径）；kb 上传与截屏移出 /tmp
- `docker-compose.yml` 生产化：api/web 服务与 Dockerfile、`restart: unless-stopped`、healthcheck、
  PG 密码走 `docker/.env`、redis AOF 持久化卷、端口只绑 127.0.0.1
- 新建 `docs/BACKUP_RUNBOOK.md`、`docs/UPGRADE_SOP.md`、`docs/DATA_FLOW.md`（默认网关 `https://api.agnes-ai.cn/v1` 数据流向告知）

### 已知局限（本次未覆盖）

- Mac 无 shell：未运行任何测试；vitest / tsc / next build 均未验证。**跑 `pytest` 时不要起本地 Redis**，
  否则 `test_auth.py` 对 `_login_failures` 内存 dict 的断言会失败（限流走 Redis 通道时）
- 429 熔断语义实现为「短路 break 不再试后续模型」（同 provider 限流假设）；如需「跳过中间只试最后一个 fallback」可再调
- 演示模式为纯前端模拟（未落库、未调模型）；如需真实演示任务落库需后端配合新增演示执行路径
- `resume_execution` 由「状态翻转」改为「真重跑」，属预期内的语义变更；若旧测试断言旧行为需同步更新
- `start-mac.sh` 仍绑 `0.0.0.0`（有局域网访问需求，未改）；`start-linux.sh` 未动

## [未发布]

### 已新增（端到端试跑 · 无需真实密钥）

- **端到端主链路试跑** `tools/e2e_smoke.py`：起真实 uvicorn 进程 + 临时 SQLite +
  本地假 OpenAI 兼容模型服务，跑通「注册 → 登录 → 建公司 → 注册模型 → 建员工 → 绑模型 →
  建项目 → 建任务 → 启动 → 轮询到完成 → 断言成果入库 / 文档落盘 / token 记账 / 模型确实被调用」，
  **38 项断言全部通过**，已并入 `tools/check.sh` 作为第 7 道关卡
- 因为 `.env` 的模型密钥为空，起一个本地假模型即可让真实进程跑完整条链路 ——
  **整条主链路的验证不需要任何真实密钥**
- **试跑抓出并修复 4 个真实缺陷**：
  - 模型自检只看 provider 默认环境变量、不看模型自己的 `api_key_env` → 可用模型被误报「缺密钥」
  - `POST /projects` 只认 query 参数，前端传 body → 422，项目创建不了
  - 任务失败的证据写在未提交事务里，被上层 rollback 冲掉 → 失败现场查不到
  - `AgentExecution.completed_at` 运行时在写，但表上没有这一列 → 执行耗时永远是空

### 已修复（权限层 · 12 处跨租户越权）

- **修掉 12 处跨租户越权（IDOR）**：10 个完全没有归属校验的 DELETE 端点 +
  2 处「伪校验」（拿记录里的 `company_id` 与**调用方自己传进来的** `company_id` 比对 ——
  攻击者把自己公司 ID 传进去就永远通过，比完全不校验更危险，因为它会让 review 的人放心）
- **新增权限层** `app/core/permissions.py`：Capability（查看 / 操作 / 管理 / 破坏性 / 管理公司）
  × Role 矩阵；越权返回 **404 而非 403**（403 会暴露「此 ID 存在但你没权限」，等于帮攻击者枚举租户）
- **静态扫描护栏**：新增无归属校验的 DELETE 端点会直接让测试变红（已反向验证）
- **诚实边界**：目前只有 OWNER 角色真正可达；ADMIN / MEMBER / VIEWER 是已声明但尚未接入的语义，
  需先建「公司成员」表再接入，本轮未假装完成

### 已新增（度量体系）

- **`GET /metrics/overview`**：任务 / 执行 / 模型调用三级成功率、平均耗时与延迟、
  失败原因归一化（脱敏 UUID）、按员工归因、按天吞吐；前端「绩效中心」已接入
- **关键设计：无样本返回 `null` 而非 `0`**。「还没跑过任务」显示「—」，
  「跑了 2 个失败 1 个」显示 50% —— 二者绝不能混为一谈，测试里专门断言了这一点
- 修掉 `GET /dashboard/{cid}` 用 **200 + `{error}`** 表示失败（HTTP 层完全看不出出错了）
- **数据库 schema 自动补列** `app/db/migrations.py`：`create_all` 只建表不加列，
  升级后新字段会静默缺失，现在启动时自动补齐（跳过非空列并告警，绝不删列改列）

### 已修复（工程质量改造 · 详见 UPGRADE_NOTES.md）

- **修复 9 处未定义变量**导致的静默失效。其中「文档风格 + 公文写作」整块能力此前
  100% 不执行，但接口仍返回 200；三个接口（文档风格删除、技能删除、训练撤销）必然 500
- **修复 11 处前后端接口契约错配**。最严重的是人工介入页：路径错（404）+ 缺鉴权头（401）
  + 无视响应无条件提示「已批准」，用户以为成功而实际什么都没发生
- **治理 28 处静默吞异常**（`except: pass`）。保留「失败不阻塞主流程」的原意，
  但每处失败都留下可检索、可归因的日志
- **修复密钥加载链路**：此前全项目 0 处 `load_dotenv()`，只在 `.env` 配密钥会取到空串，
  导致调用模型报 401
- **修复任意文件读取**：`FileReaderSkill` 无路径限制，可读取 `.env` 并取走 `JWT_SECRET`
- **修复命令注入**：`real_run_write` 未过滤 `;` `|` `>` 与换行却用 shell 语义执行
- **补齐 4 个模块的跨租户归属校验（IDOR）**，含「可替他人公司授权本机 shell 能力」
- **新增生产环境安全门禁**：`JWT_SECRET` 仍为占位值或 `DEBUG=true` 时拒绝启动

### 已修复（消费者体验收敛）

- **统一二次确认，替代原生 `confirm`**：浏览器可被设置为"不再显示此类对话框"，
  之后 `confirm()` 恒返回 `true` —— 二次确认会退化成一次点击就执行破坏性操作
- **为 3 处原本完全没有确认的破坏性操作补上护栏**：批量删除公司（连同项目/员工/成果/日志）、
  执行离职交接（真实改写归属且不可撤销）、删除经验教训与记忆
- **14 处原生 `alert` 改为非阻塞 Toast**；其中会议页的上传失败原先被空 `catch {}`
  静默吞掉，用户会误以为"传上去了但没生效"
- **修掉两处"点了按钮没反应"**：新建 Agent 时姓名为空直接 return、模型配置格式错误
  只返回技术性 422

### 已新增（消费者体验收敛）

- **`⌘K` 全局跳转**：35 个页面按标题、说明、英文名、拼音、口语说法全量可搜，
  支持「最近访问」与全程键盘操作；内部页面改为"藏起来但搜得到"
- **导航单一事实源 `lib/nav.ts`**：侧边栏与命令面板共用一份定义，新增页面只改一处
- **统一确认框 `components/ConfirmDialog.tsx`**：主题化、逐条列出影响面、
  不可逆操作要求逐字输入确认词、Esc/Enter/Tab 全键盘可用、关闭后焦点归位
- **表单校验体系** `lib/validate.ts` + `lib/useFormValidation.ts` + `components/Field.tsx`：
  默认不打断用户、提交时一次性列出全部问题、字段级 `aria` 关联
- **6 条体验护栏测试**：禁止原生弹窗、每个页面必须有入口、导航不得有死链、
  导航必须带说明与检索词（已反向验证：注入违规探针会让门禁变红）
- **测试体系重建**：113 → **158** 个离线用例（原 29 个「测试」文件里 28 个无法运行）
  - 覆盖认证与限流、账号枚举防护、跨租户隔离、沙箱与命令注入、接口契约、
    schema 迁移、度量、权限与代码质量与体验护栏

### 已新增（前端数据层与类型统一）

- **30+ 处裸 `fetch` 全部收口到 `lib/api.ts`**：此前各页面自己拼
  `NEXT_PUBLIC_API_URL` 与 `aieos_token`，导致没有超时（后端卡住页面永久转圈）、
  没有错误规范化（422 显示 `[object Object]`），且**绕过统一层 = 同时绕过契约校验**
- **收口后挖出 6 个此前从未生效的功能**：
  - 员工换模型：前端发 PATCH，后端是 `PUT /agents/{id}/switch-model` → 从来没生效过
  - 外部接入启用/停用：前端 `PUT /external-agents/{id}/ENABLED`，
    后端只有 `POST .../enable|disable`，且原来连鉴权头都没带
  - 4 处下载链接点了必然 401 —— 下载接口要 Bearer token，而 `<a href>` 不会带它；
    改为取回 blob 再本地保存（新增 `fetchBlobURL` / `downloadURL` / `downloadText`）
  - 日志导出 `window.open(接口地址)` 打开的是 401 的 JSON
  - 多处 `createObjectURL` 后立刻 revoke，下载有概率失败
  - 契约扫描被模板里的三元表达式骗过 4 次（引号提前终止捕获）→ 已加「无法静态解析」判定
- **`lib/types.ts`** 领域类型 + **`lib/useResource.ts`** 轻量数据层
  （并发去重 / 失效重取 / 乐观更新，约 100 行，**不引第三方依赖**）
- 全站 `any` 从 136 处降到仅剩 `catch` 子句（TS 无法给 catch 标注类型）与 LLM 自由结构字段
- **新增护栏**：禁止裸 fetch / 硬编码地址 / 手工取 token（已反向验证：植入探针立刻变红）

### 已新增（工程质量改造）

- **交付前自检门禁** `tools/check.sh`：一条命令跑完静态检查 / 导入 / mapper /
  接口契约 / 类型检查 / lint / 测试，任一失败即非零退出
- **接口契约校验工具** `tools/check_api_contract.py`（已接入门禁）
- **文件访问沙箱** `app/core/sandbox.py`：默认拒绝 + 路径解析 + 敏感文件拦截
- **集中日志** `app/core/logging_config.py` + 全局异常处理器
- **环境加载器** `app/core/env_loader.py`
- **本机能力服务层** `app/services/computer_service.py`：权限校验 / 执行 / 审计收敛为唯一实现
- **可复用归属校验** `assert_company_owned` / `assert_agent_owned`
- **前端全局反馈**：Toast、三态布局、`error.tsx` / `loading.tsx` / `not-found.tsx` /
  `global-error.tsx`、`prefers-reduced-motion` 支持
- **新用户三步引导向导** + 模型可用性自检接口（只报告密钥是否存在，不返回密钥本身）
- **前端 API 层重构**：180s 超时、可区分的 `ApiError`、校验错误规范化、泛型响应
- **版本控制**：此前无 `.git`，现已有基线快照与按问题切分的提交历史

### 已变更（文档校订）

- README / DEVELOPMENT / INTRODUCTION 的数字按实测校正：
  Phase 1 → **Phase 3**，38 → **354 端点**，18 → **94 张表**，约 2400 行 → **21,554 行**
- 删除 README / INTRODUCTION 中「测试账号 `boss@aieos.dev` / `Passw0rd!`」的说明 ——
  该账号在整个代码库中**从未被创建**，属历史遗留；现改为引导用户自行注册
- 修正 `CHANGELOG.md` 与 `DEVELOPMENT_STATUS.md` 中的字面 `\n` 转义符（此前渲染为单行）

---

## [0.1.0] — 2026-08-23

### 新增（Phase 1 MVP 全量交付）

组织架构 / Agent 管理 / 模型网关 / 技能插件 / 任务引擎 / Agent Runtime /
多 Agent 协同 / 协作消息 / 资产中心 / 事件日志 / 暂停恢复取消 / 人工介入 / 实时看板

> 上述为历史交付记录。实际代码在后续开发中已推进至 Phase 2、Phase 3，
> 但本文档此前未同步更新 —— 这也是本次改造中「文档与实现脱节」问题的来源之一。
