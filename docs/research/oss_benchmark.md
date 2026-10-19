# 开源多智能体/数字员工生态对标报告（for AI-EOS）

> 调研对象:与 AI-EOS(数字员工=Agent,公司组织隐喻,本地桌面+局域网共享,老板审批文化,token 预算护栏,trace+接管)方向相似的开源项目。
> 数据:star 数/最后活跃时间取自 GitHub API 实时抓取,**【截至 2026-10-19】**;个别项目(Flowise、Devika、XAgent)标注了"已归档/停更"。
> 方法:先对每个仓库调 GitHub API 拿 star/pushed_at/license,再抽读 README 核实架构机制。star 数写量级,活跃看 pushed_at。

## 0. 总览(36 个项目,分 6 组)

| 组 | 项目 |
|---|---|
| 编排框架(角色/群聊/图) | CrewAI, AutoGen/AG2, MAF(Microsoft Agent Framework), MetaGPT, ChatDev, LangGraph, OpenAI Agents SDK, Swarm, Camel, Qwen-Agent, AgentScope, SuperAGI, BabyAGI, AutoGPT, AgentVerse, LangFlow, smolagents, google/adk-python |
| 低代码/工作流平台 | Dify, n8n, Flowise, RAGFlow, AnythingLLM |
| 记忆系统 | mem0, Letta |
| 操作型 agent(浏览器/桌面/编码) | browser-use, open-operator, open-interpreter, Cline, SWE-agent, OpenHands, Devika |
| 社会/组织模拟 | Generative Agents, OASIS, TheAgentCompany |
| 自动科研 + 互操作协议 | Sakana AI-Scientist, MCP, A2A |

---

## 1. 逐项目档案

### 编排框架组

**① CrewAI — https://github.com/crewAIInc/crewAI**
- ~59.9k star;最后活跃 2026-10-19(极活跃);MIT。
- 定位:面向"多 agent 协作完成业务任务"的生产级编排框架,核心抽象 Crews(角色队)+ Flows(事件驱动流程)。
- 架构:Agent = role + goal + backstory(人格三件套);Task 绑定 expected_output 与工具;Crew 支持 sequential 和 hierarchical(自动产生 manager agent 做委派);Flow 是确定性事件驱动编排(精确状态转移、@start/@listen、可暂停 wait_for_input),Crew 作为 Flow 的节点;内建短期/长期/实体记忆、知识源(Knowledge)、工具护栏。
- 可借鉴:
  1. **role/goal/backstory 三件套**——直接对应 AI-EOS 的"岗位"概念,但 backstory 给了"岗位说明书"里人设维度的一个轻量写法,可在岗位模板里加"工作风格"字段。
  2. **Crew 内嵌于 Flow**:确定性骨架(老板可审计的 DAG)+ 自治肉(crew 内部自由讨论)混编——AI-EOS 的"任务流水线 + 员工协作"可以照这个分层:流程层可审计、协作层可自治。
  3. **Flow.wait_for_input()**:把"暂停等人类"做成一等公民原语,和 AI-EOS 审批门天然同构,可作为审批挂起态的 API 设计参考。

**② AutoGen / AG2 — https://github.com/microsoft/autogen (legacy 61.5k,2026-01 停更) / https://github.com/ag2ai/ag2 (5.0k,2026-10-19)**
- 定位:多 agent 对话式编排的源头;0.4 版重构为 actor 模型(异步消息传递 + 话题订阅);AG2 是社区 fork,Microsoft 官方版转为维护模式并推出后继 MAF。
- 架构:ConversableAgent 之间自由对话;GroupChat + 可插拔 speaker_selection_policy(轮询/LLM 选人/自定义)是"群聊式"编排的鼻祖;code executor 支持 Docker 隔离执行;human-in-loop 靠 UserProxyAgent(执行前必须人类确认)。
- 可借鉴:
  1. **群聊 speaker 选择策略做成可插拔**——AI-EOS 如果做"部门群聊",谁是发言人可以由策略(轮询/按岗位/按紧急度/LLM)决定,而不是写死。
  2. **UserProxyAgent 的"人类即 agent"**——把老板建模成会收消息、可批准/打回的 agent,这是"老板审批文化"的最干净的框架化表达。
  3. **Docker 化代码执行**默认沙箱,操作型 agent 的安全底线。

**③ Microsoft Agent Framework (MAF) — https://github.com/microsoft/agent-framework**
- ~14.2k star;2026-10-18(活跃);AutoGen 的"生产化继任者"。
- 架构:graph-based workflow(顺序/并发/handoff/群协作四种编排原语),内建 checkpointing + **time-travel(回到历史 checkpoint 重放)**,内置 OpenTelemetry 追踪,declarative YAML agent 定义,Agent Skills(领域知识库)。**human-in-the-loop 是一级特性**。
- 可借鉴:
  1. **time-travel / checkpoint 重放**:出错后不是重跑,而是回到某个"干净 checkpoint"再改指令——AI-EOS 已有 trace,下一步值得学"从 trace 的某个节点回放/分叉",这是老板接管的自然延伸。
  2. **YAML 声明式 agent + 版本化**:数字员工=一份可 diff、可回滚的配置,契合"公司人事档案"隐喻。
  3. **Agent Skills 作为可发现的知识包**,和 AI-EOS 的技能体系直接对标,其"多来源(文件/内联代码/类库)拼装"值得参考。

**④ MetaGPT — https://github.com/FoundationAgents/MetaGPT**
- ~70.9k star;最后推送 2026-01-21(明显减速,生态重心已转 OpenGPTs 云);Apache-2.0。
- 架构:"SOP(标准作业程序)编码进多智能体":一个代码工厂流水线,角色(ProductManager→Architect→PM→Engineer→QA)各自消费/产出结构化文档(需求文档、设计文档、接口定义),靠**消息池(pub-sub)+ 可执行代码反馈**驱动;Data Interpreter 负责数据分析。核心理念:把人类公司 SOP 写成"可执行的 agent 间协议"。
- 可借鉴:
  1. **SOP 流水线 + 岗位角色文档**:每个角色的 docstring 就是岗位 SOP;上下游传的是"文档"(需求→设计→代码→测试)而非自由聊天——AI-EOS 的成果物应设计成结构化"交付物",审批门批的是交付物而非聊天记录。
  2. **消息池 pub-sub**:agent 不互相指名道姓,而是订阅自己关心的消息类型——降低了多 agent 系统的耦合,适合"公司内信息流转"隐喻。
  3. **可执行反馈闭环**:QA 角色跑代码,失败信息回灌 Engineer——"员工自测"机制。

**⑤ ChatDev — https://github.com/OpenBMB/ChatDev**
- ~34.5k star;2026-10-16(活跃)。
- 架构:**chat chain**:任务切成阶段(设计/编码/测试/文档),每阶段一对 leader+member 双 agent 对话推进,阶段产出(聊天+代码)作为下一阶段输入;ChatChainManager 控制流程;内建自动评估(code 可运行性)。
- 可借鉴:
  1. **"双角色对话 + 阶段化"**比"多角色群聊"便宜且可控:每个阶段只 2 个 agent,成本线性;AI-EOS 的部门协作可默认用"主岗+协办岗"成对制,而非全部门大会。
  2. **阶段边界即天然审批点**:每个阶段产出物交付老板确认再进下一阶段——比"全程监督"更符合审批文化。
  3. 聊天+代码打包成可回溯 bundle,便于 trace 化。

**⑥ LangGraph — https://github.com/langchain-ai/langgraph**
- ~43.5k star;2026-10-18(极活跃)。
- 架构:把多 agent 系统写成**状态机图**(节点=函数,边=条件转移),checkpointer 持久化全局状态(支持 Postgres/SQLite——和 AI-EOS 的 PG 栈吻合),time-travel 回放、子图嵌套、prebuilt ReAct agent;HITL 靠 `interrupt()`(图跑到某节点可挂起等人,人批准后 resume)与 `Command`。
- 可借鉴:
  1. **interrupt()/resume 的"图挂起"语义**:AI-EOS 的 token 护栏到顶暂停,本质就是图挂起+状态持久化——直接可抄的范式(护栏=条件边上的 interrupt)。
  2. **Postgres checkpointer**:trace 即图状态,老板接管=在任意节点注入 Command;与 AI-EOS "trace + 接管"几乎同构。
  3. **子图=子公司/项目组**:大任务拆成嵌套子图,各自有独立状态与审批边界。

**⑦ OpenAI Agents SDK — https://github.com/openai/openai-agents-python**
- ~30.1k star;2026-10-17(极活跃);Swarm(22k,实验性,已并入 SDK)是其前身。
- 架构:极简三原语——Agent(带 instructions/tools)、**handoff(把对话交给另一个 agent)**、**guardrails(输入/输出护栏,可"tripped"即熔断整个 run)**;Sessions 持久化多轮;全链路 tracing(OTel);agent-as-tool(把 agent 当工具嵌套)。
- 可借鉴:
  1. **guardrails "tripped" 语义**:护栏触发不是报错,而是把整个执行熔断掉并标记原因——AI-EOS 的 token 预算护栏可照此设计:熔断=暂停+通知老板+保留现场。
  2. **handoff 是 agent 间"移交"而非群聊**:老板视角最易审计(谁在什么时候移交给谁),适合"转派/移交"这类公司动作。
  3. **OTel trace 默认内建**:trace 不是后挂的,是一等对象。

**⑧ Camel — https://github.com/camel-ai/camel**
- ~18k star;2026-10-12(活跃);camel 系还有 OASIS(见社会组)。
- 架构:**角色扮演**框架:任意两个角色(user/assistant)领任务后自动分解子任务并协作;Camel 把"roleplay"当通信协议;生态含 OASIS 百万 agent 社交模拟。
- 可借鉴:
  1. **任务分解由"对话双方"共同完成**(而非单侧 planner)——数字员工接到模糊任务时,先和"老板数字替身"对一遍需求再动手,是便宜的澄清机制。
  2. 角色可热插拔:同一任务换不同组合重跑做 A/B。

**⑨ Qwen-Agent — https://github.com/QwenLM/Qwen-Agent**
- ~17.2k star;最后推送 2026-03-04(近 7 个月无更新);Apache-2.0。
- 架构:基于 Qwen 指令跟随/工具/规划/记忆能力的 LLM 应用框架;自带 Browser Assistant、Code Interpreter、Custom Assistant 三个成品 agent;**MCP 工具一等支持**;长文本规划(Deep Planning 基准)。
- 可借鉴:
  1. **MCP 作为工具接入标准**:AI-EOS 已有 MCP 双向,可直接把 Qwen-Agent 的 browser agent 当"网页操作员工"模板参考。
  2. "成品 agent 即样例"的交付方式(每个样例可一键跑),降低数字员工模板的使用门槛。

**⑩ AgentScope — https://github.com/agentscope-ai/agentscope (原 modelscope/agentscope)**
- ~33.5k star;2026-10-19(极活跃)。
- 架构:阿里开源:agent + ReAct、**MsgHub(多 agent 群聊消息枢纽)**、分布式(RPC 化,agent 可跨进程/机器跑)、AgentScope Studio(可视化观测)、模型网关多 provider 路由。
- 可借鉴:
  1. **MsgHub**:群聊里消息广播给除自己外的所有 agent——"部门群"消息原语的干净实现。
  2. **分布式 RPC agent**:agent 是可调度到任意机器的进程——AI-EOS 局域网共享场景下,"把某个数字员工派到局域网另一台机器执行"可借鉴其 agent-as-rpc 思路。
  3. Studio 的会话级观测面板。

**⑪ SuperAGI — https://github.com/TransformerOptimus/SuperAGI**
- ~17.7k star;最后推送 2025-01-22(基本停更,教训样本)。
- 架构(2023 峰值期):dev-first 自主 agent 框架,CLI+GUI 双端,agent 模板、工具管理、向量库、多 agent 编排;定位"让普通人快速造 agent"。
- 可借鉴:
  1. 反面教材多于正面:GUI+CLI 双端同时维护、停更后无继任者——**多端形态未收敛是框架类项目常见死因**,AI-EOS 桌面+Web 双端要尽早定单一主形态。
  2. 其"agent 模板市场"思路(现由 AutoGPT Platform 继承)可参考。

**⑫ BabyAGI — https://github.com/yoheinakajima/babyagi**
- ~22.4k star;2026-01-31(README/架构已变)。
- 架构:2023 原版是 800 行"任务规划循环"(LLM 维护 task 列表,每次选一个做,做完重规划);现演进为 **functionz 框架**:函数存数据库、依赖图管理、自动加载、看板管理,定位"能自我构建的 agent"。
- 可借鉴:
  1. **"技能=带依赖图的函数,存 DB 自动加载"**——AI-EOS 技能体系可加"技能依赖图"(技能 A 依赖技能 B)与自动装配。
  2. 看板式的 agent 运行管理(dashboard 里能看到每个函数/任务状态)——老板视角的"工时看板"原型。

**⑬ AutoGPT (Platform) — https://github.com/Significant-Gravitas/AutoGPT**
- ~187.6k star(本组最高);2026-10-19(极活跃)。
- 架构:从"自主循环 agent"(2023)蜕变为低代码 agent 平台:blocks 拖拽 + 代码两种定义 agent,**scheduled agents(定时任务)、agent 市场(share/fork)、多 agent 协作**,内置向量库/记忆。
- 可借鉴:
  1. **scheduled agent(数字员工定时上班)**:每天 9 点自动跑周报 agent——"排班/考勤"隐喻很贴。
  2. **agent 市场 + fork**:数字员工模板可分享/复制改良——局域网共享之外的生态想象。
  3. 同名"自主 agent"的历史证明:无护栏全自主必失控,护栏+审批是主流方向。

**⑭ AgentVerse — https://github.com/OpenBMB/AgentVerse**
- ~5.2k star;最后推送 2024-09-09(停更)。
- 架构:双框架:task-solving(从 agent 群体中**动态组队**迭代选 agent)+ simulation(自定义环境观察多 agent 社会行为)。
- 可借鉴:
  1. **动态组队**:任务→根据能力描述从 agent 池里选人——AI-EOS 可让老板"派活"时由系统建议/自动组"项目组"(现有员工+临时外聘 agent)。
  2. 停更本身是警示:学术型多 agent 框架若无产品化牵引难存活。

**⑮ LangFlow — https://github.com/langflow-ai/langflow**
- ~155.8k star;2026-10-19(活跃)。
- 架构:低代码 LLM 流可视化构建(节点=组件,边=数据流),拖拽出 RAG 链/agent,一键部署为 API;现也支持"agent 编排"模板。
- 可借鉴:
  1. **节点图即产品**:非技术老板用拖拽看/改"员工的工作流"——AI-EOS 若做可视化流程编辑,LangFlow 的节点/组件抽象是成熟参考。
  2. 流=可发布 API:把"一条业务流程"当成可共享的局域网服务。

**⑯ smolagents — https://github.com/huggingface/smolagents**
- ~30k star;2026-10-06(活跃);HF 出品,以极简(核心 <5k 行)著称。
- 架构:主张 **code-as-action**:agent 不输出 JSON 工具调用,而是写 Python 代码作为动作(可连续调多个工具、用循环条件),跑在沙箱里;多 agent=agent 作为另一个 agent 的工具(层级)。
- 可借鉴:
  1. **code-as-action 显著省 token**(一条代码=多个工具调用)——和 AI-EOS 的 token 护栏直接协同,可作为"高效执行模式"。
  2. "agent 嵌套为工具"的层级委派,比 handoff 更结构化(调用方保留控制流)。

**⑰ google/adk-python — https://github.com/google/adk-python**
- ~21.9k star;2026-10-19(极活跃)。
- 架构:Google Agent Development Kit 2.0:**workflow runtime(图执行引擎:路由/fan-out/fan-in/循环/retry/状态/HITL/嵌套)**、**Task API(agent 间结构化委派:多轮任务模式、单轮受控输出、agent 作为工作流节点)**、层级多 agent、**tool confirmation flow(HITL 工具级确认)**、Agent Config(无代码定义 agent)、ADK Web 开发 UI。
- 可借鉴:
  1. **Task API 的"受控输出委派"**:父 agent 给子 agent 发任务并规定返回结构(子 agent 不能自由发挥),这是"经理给下属派活+验收标准"的完美机制——比 handoff(整个对话移交)粒度更细。
  2. **tool confirmation(HITL 到工具级)**:审批门不止在任务级,还能挂到单个工具调用(如"外发邮件"这一个动作前必须老板点头)——与 AI-EOS 的"高危操作/外发"审批门粒度一致,值得抄。
  3. Agent Config 无代码建 agent:老板在 UI 里"招一个员工"。

### 低代码/工作流平台组

**⑱ Dify — https://github.com/langgenius/dify**
- ~158.8k star;2026-10-19(极活跃)。
- 架构:LLM 应用平台:workflow(DAG)+ Chatflow(对话流)双模式,**agent 节点**(ReAct/FunctionCall)可嵌在工作流里,RAG 知识库、插件体系、多租户;自托管/云双形态。
- 可借鉴:
  1. **"工作流里嵌 agent 节点"**:确定性流程里某一步是自主 agent——对应 AI-EOS"流水线里某环节交给数字员工自主发挥"。
  2. 插件市场 + 多租户隔离:局域网多设备共享时,租户/权限模型可直接参考。

**⑲ n8n — https://github.com/n8n-io/n8n**
- ~208k star(全场最高);2026-10-19(极活跃);混合许可(公平代码)。
- 架构:通用工作流自动化(200+ 集成),近年加 **AI agent 节点 + workflow 当 agent 的工具**(agent 可调用另一条 workflow);HITL 靠"等人工输入"节点(wait-for-webhook/表单)。
- 可借鉴:
  1. **agent 与 workflow 互为工具**:数字员工可以"呼叫"一条公司 SOP workflow,workflow 里也能唤醒某个数字员工——双向互操作,和 AI-EOS 技能/流程体系契合。
  2. 人工等待节点=最朴素的审批挂起,值得对比自己审批门的超时/重试策略。

**⑳ Flowise — https://github.com/FlowiseAI/Flowise**
- ~55.5k star;**仓库 2026-08 已归档**(archived=true,最后推送 2026-08-13)——"拖拽 LLM 流"赛道头部项目生命周期结束的典型案例。
- 架构:拖拽构建 LLM flow/agent/chain,内建 playground 调试。
- 可借鉴:
  1. 主要是警示:**纯"拖拽"卖点撑不起长期维护**,被 Dify/Langflow 双夹击后归档。AI-EOS 桌面端若做流编辑器,要绑定自身差异点(员工隐喻+审批),别做通用 flow 画布。

**㉑ RAGFlow — https://github.com/infiniflow/ragflow**
- ~92.4k star;2026-10-19(极活跃)。
- 架构:深度文档理解驱动的 RAG(版面解析 DeepDoc)+ 可视化 agent workflow(多 agent 协作节点)、知识图谱。
- 可借鉴:
  1. **agent 工作流节点化**(RAG/LLM/分类/生成节点)适合"办公室文员"型数字员工:审文件、查资料、写材料的流水线。
  2. "先深度解析文档再喂 agent"的知识处理,是数字员工"读文件"技能的上游参考。

**㉒ AnythingLLM — https://github.com/Mintplex-Labs/anything-llm**
- ~67.3k star;2026-10-17(活跃)。
- 架构:**桌面优先**(macOS/Windows 一键安装包)的 RAG+agent 应用:本地向量库、多 provider、agent 模式(工具调用)、团队版(多人共享工作区)。
- 可借鉴:
  1. **"桌面优先"的交付形态做得最成功的开源样本**:安装包+零配置+数据全本地——AI-EOS 桌面版 UX 的直接对标。
  2. 团队版"共享工作区+邀请码":局域网共享/配对码的先行者设计(工作区=设备/成员共享的上下文边界)。

### 记忆系统组

**㉓ mem0 — https://github.com/mem0ai/mem0**
- ~67.4k star;2026-10-17(极活跃)。
- 架构:记忆层中间件:抽取-归纳管道;2026-04 新算法:**ADD-only 单次抽取(不覆盖旧记忆)+ 实体链接(实体跨记忆嵌入关联)+ 多信号检索(语义/BM25/实体三路并行融合)+ 时间推理**;开源了记忆基准(LoCoMo/LongMemEval/BEAM)。
- 可借鉴:
  1. **agent 确认过的动作=一等记忆**:AI-EOS 的"老板批过的成果"应自动沉淀为高置信记忆/技能,这是"审批文化"反哺记忆的独特点。
  2. 多信号融合检索 + 时间推理:老板问"上周那个方案怎么定论的"这种时间限定查询要答得准。
  3. 记忆基准开源——AI-EOS 记忆体系要能自证,可自建"公司场景记忆基准"。

**㉔ Letta (MemGPT) — https://github.com/Letta-ai/letta (主源已迁 letta-code)**
- ~25.2k star;2026-10-18(极活跃;V1 已归档)。
- 架构:**有状态 agent**:把记忆管理做成 OS 分页类比(core memory 常驻 / archival 外存,agent 自己发工具调用编辑自己的记忆);**sleep-time agents**(后台空转时整理/学习记忆,下次唤醒更聪明);现主源 letta-code=终端 agent harness+App Server+Slack/Telegram 通道+Letta Cloud(记忆/身份跨机器同步)。
- 可借鉴:
  1. **sleep-time(下班学习)机制**:数字员工"不上班的时候"在后台整理记忆、预研项目——"加班学习"隐喻,且可挂 token 护栏(空转也耗预算)。
  2. **agent 自主编辑记忆**:记忆不是外部写死的,agent 自己调用 memory 工具增删——和"员工写自己的工作日志"隐喻完全一致,审批门可落在"日志/记忆变更"上。
  3. 记忆跨设备同步(Letta Cloud 思路)对应 AI-EOS 局域网多设备:同一员工在办公室/家里设备上是"同一个人"(身份+记忆一致)。

### 操作型 agent 组

**㉕ browser-use — https://github.com/browser-use/browser-use**
- ~118.4k star;2026-10-17(极活跃)。
- 架构:通用网页操作 agent:DOM 感知+视觉双通道、按选择器/坐标操作、MCP server 化;云端/本地双形态。
- 可借鉴:
  1. **MCP 化暴露 agent 本身**(browser-use 可作为 MCP server 被其他 agent 调用)——AI-EOS "操作型数字员工被其他员工调用"的接口范式。
  2. 双通道(结构化 DOM 优先、视觉兜底)的成本控制策略,与 token 护栏兼容。

**㉖ open-operator — https://github.com/OpenHands/open-operator**
- ~432 star(资源仓);OpenHands 出品的 computer-use(桌面 GUI 操作)agent。
- 架构:VLM 驱动的桌面 GUI 操作(screenshot+动作),用于通用 UI 自动化。
- 可借鉴:桌面级 GUI 操作是"数字员工会用公司桌面软件"的底座;小而专,可作为 AI-EOS 操作员工的子模块参考。

**㉗ open-interpreter — https://github.com/openinterpreter/openinterpreter**
- ~68.6k star;2026-10-11(活跃);已演进为"面向开源模型的 coding agent"(Kimi K3/GLM 5.3 等)。
- 架构:终端里让 LLM 直接执行 Python/shell(本地运行时),支持流式代码块、项目上下文。
- 可借鉴:
  1. **本地运行时执行**是"桌面 agent"最简形态:AI-EOS 执行轨迹里"员工跑本地脚本"的安全边界(允许目录、只读白名单)可参考其配置模型。
  2. "先读文件→再写代码"的上下文装配。

**㉘ Cline — https://github.com/cline/cline**
- ~70.5k star;2026-10-19(极活跃)。
- 架构:VS Code 自主编码 agent,现兼 SDK/CLI;**plan/act 双模式**(先出计划再执行)、**逐工具调用的人工批准/拒绝(HITL 到工具粒度)**、MCP 支持、浏览器操作子代理。
- 可借鉴:
  1. **plan/act 分离 + 工具级批准**:老板先批"计划",执行中每个敏感工具(写文件、跑命令、外发)逐一点头/否决——最完整的"老板审批文化"交互样本,AI-EOS 审批 UI 直接对标。
  2. 批准决策可设"始终允许此工具"(审批疲劳治理)——高频低风险操作免批,保住审批门不被老板嫌烦,关键设计。

**㉙ SWE-agent — https://github.com/SWE-agent/SWE-agent**
- ~20.6k star;2026-10-12(活跃)。
- 架构:给 GitHub issue 自动修 bug;**核心贡献是 ACI(Agent-Computer Interface)设计**:给 LLM 提供受限、语法校验过的命令集(读 100 行限制、编辑后 lint 反馈),证明"工具接口设计比模型强弱更影响成败"。
- 可借鉴:
  1. **ACI 原则:为 LLM 专门设计"防呆"工具接口**(限制窗口大小、语法校验、失败即时反馈)——AI-EOS 给数字员工的工具/MCP 工具都应做成 ACI 风格,降低出错率=省 token=护栏更松。
  2. "失败反馈即时回灌"是 trace 里最值钱的信息。

**㉚ OpenHands — https://github.com/OpenHands/OpenHands (原 OpenDevin,Devin 类复刻)**
- ~91.2k star;2026-10-19(极活跃)。
- 架构:自主软件工程 agent:**event stream 架构**(所有动作=事件,UI 可重放/干预/接管)、沙箱运行时(Docker/云端)、CodeAct(写代码作为通用动作)、VLA、TheAgentCompany 基准的官方 harness。
- 可借鉴:
  1. **event stream = trace 的一等数据模型**:老板接管、回放、分叉全部建立在事件流上——AI-EOS 的 trace 体系可完全对齐这个模型(事件带类型+可注入人工事件)。
  2. **接管=向事件流插入人工事件**:接管不是"停 agent",而是"老板的事件和 agent 的事件同流记录"——trace 与接管统一。
  3. 沙箱分层(Docker 默认、本地可选),桌面场景下"员工动作的运行时隔离"参考。

**㉛ Devika — https://github.com/stitionai/devika**
- ~19.6k star;最后推送 2025-09-25(已停更;README 声明继任者为 Opcode)。
- 架构:Devin 开源复刻:Planner→Researcher(限 3 条搜索)→Coder→Action(run/deploy/feature/patch/report)专职子 agent 链;计划先展示后写码;状态可视化(计划/浏览器截图/终端/token 计数同屏)。
- 可借鉴:
  1. **"计划先给人看再动手"**(planner 产出编号 checklist,老板/用户可见后 coder 才写文件)——审批门最小可用形态。
  2. **token 计数常驻同屏**:预算护栏的 UI 最简表达。
  3. 停更+被继任者吞并:又一个"单 agent 复刻 Devin"红海警示。

### 社会/组织模拟组

**㉜ Generative Agents (Stanford Smallville) — https://github.com/joonspk-research/generative_agents**
- ~22.2k star;最后推送 2024-08-05(论文代码,停更)。
- 架构:25 个 LLM agent 在虚拟小镇生活,**三件套:memory stream(全量日志)+ reflection(定期把日志提炼成高层认知)+ planning(把反思落成日程)**;记忆检索=新近性×重要性×相关性加权。
- 可借鉴:
  1. **reflection(反思)是记忆质量的放大器**:数字员工定期把执行日志提炼成"经验/价值观"层记忆——"员工的职业成长"隐喻,且反思可挂审批(老板看员工"述职")。
  2. **memory 检索三因子加权**(新近/重要/相关)公式简单可解释,可直接用于 AI-EOS 记忆召回。
  3. 日程化规划(plan→schedule 可被人修改):老板改员工日程=轻干预不重跑。

**㉝ OASIS — https://github.com/camel-ai/oasis**
- ~5.3k star;2026-10-12(活跃)。
- 架构:百万级 agent 社交媒体模拟器(规则 agent+LLM agent 混合,23 种动作,内置推荐系统),研究信息扩散/极化/合谋。
- 可借鉴:对 AI-EOS 主体价值有限,但"规则 agent 兜底 + LLM agent 点缀"的**混合规模成本模型**值得记:大量低价值"围观/通知"类 agent 用规则引擎,关键岗位才用 LLM——省 token。

**㉞ TheAgentCompany — https://github.com/TheAgentCompany/TheAgentCompany**
- ~800 star;最后推送 2025-11-17;CMU 出品(NeurIPS 2025 D&B)。
- 架构:benchmark(非框架):自托管 Docker 公司环境(GitLab+Plane+ownCloud+RocketChat),175 个跨 6 岗位(软件/产品/数据/HR/财务/行政)的真实任务;**有序 checkpoint 给部分分**,确定性+LLM 双评估器;结论:最强模型仅 30% 任务全自主完成。
- 可借鉴:
  1. **"模拟小公司+岗位任务集"是 AI-EOS 的验收基准**:可自制"华夏版 TheAgentCompany"——老板视角的员工能力考卷,checkpoint 部分分适合给"员工绩效打分"。
  2. **NPC 同事(LLM 扮演)**:agent 要"找 HR 对接"时靠沟通拿信息——"跨部门协作"能力的评测方式。
  3. 论文发现的失败模式(agent 造假捷径、不识常识)是 AI-EOS 护栏设计的负面清单。

### 自动科研 + 互操作协议组

**㉟ Sakana AI-Scientist — https://github.com/SakanaAI/AI-Scientist (+v2)**
- ~14.8k star(v1;v2 另仓,2025-12 活跃)。
- 架构:全自动科研流水线:生成 idea→写代码跑实验→写论文,**自动审稿人**打分;v2 升级为 **agentic tree search**(workshop 级论文,v2 的论文本身是被 v2 自动发现的——自我指涉)。
- 可借鉴:
  1. **自动审稿人 = 成果发布的第二道门**:AI-EOS"成果发布"审批前可加一个 LLM 评审 agent("技术评审岗")自动预审,老板只看终审——审批漏斗。
  2. tree search 替代单链:多 idea 并行探索再择优,比线性流程成功率高的通用范式。

**㊱ MCP — https://github.com/modelcontextprotocol/modelcontextprotocol**
- ~9.5k star;2026-10-18(极活跃)。
- 架构:工具接入标准协议(resources/prompts/tools + 双向 server),已被 AI-EOS 采用(对外 7 工具 + 外接 server)。
- 可借鉴:
  1. 协议持续演进(认证、streaming、elicitation 等)——AI-EOS 的 MCP 层要做"协议版本跟踪",别停在 2024-11 版。
  2. 把"数字员工本身"也实现为 MCP server 已被 browser-use 等验证,生态方向明确。

**㊲ A2A (Agent2Agent) — https://github.com/a2aproject/A2A (Linux Foundation)**
- ~26.2k star;2026-10-16(活跃)。
- 架构:agent 间互操作协议:Agent Card(能力清单 JSON,含认证方式)、task 生命周期、streaming、多 agent 发现;与 MCP 互补(MCP=agent↔工具,A2A=agent↔agent)。
- 可借鉴:
  1. **Agent Card = 数字员工的"工牌/名片"**:局域网发现机制(mDNS)之上,可发 Agent Card 做"员工能力公开表",别的公司/别的 agent 按卡调用。
  2. task 状态机(submitted→working→needs-auth→completed):AI-EOS 任务状态机可对齐,其中 **needs-auth 态就是审批挂起态**——协议层原生支持 HITL。

### 名称澄清

**Fugue(用户名单中)**:GitHub 上名为 Fugue 的知名项目是分布式计算框架(fugue-project/fugue,~2.2k star,SQL/Pandas on Spark/Dask/Ray),与多 agent 无关。用户指的应是 Sakana AI 的 **Fugu**——闭源模型路由器(为 prompt 自动选模型),无开源主体;开源替代有 eulogik/fugusashi(极早期)。本组"市场式"编排的开源近似见 ② AgentVerse(动态组队)与 ㊲ A2A(Agent Card 发现/按卡调用,"员工市场"的协议化)。
**Genspark/Gamma 类**:Genspark 为闭源(搜索+agent 一站式),无直接开源对标;最接近的开源"搜索+agent 工作台"是 OpenHands 与 AnythingLLM(桌面)的组合,本报告不再单列。
**XAgent(MetaGPT 团队 3D 具身)**:仓库 2025-03 已归档,仅作 MetaGPT 系背景提及,不单列。

---

## 2. A) 按机制分类的横向对比表

### A1 编排范式(角色式 / 图式 / 群聊式 / 市场式)

| 项目 | 角色式 | 图式 | 群聊式 | 市场式(发现/组队/按价/按卡) |
|---|---|---|---|---|
| CrewAI | ★★★ role/goal/backstory + hierarchical manager | ★★ Flows 事件驱动 | 弱 | 无 |
| AutoGen/AG2 | ★★ | 弱 | ★★★ GroupChat + speaker 策略 | 无 |
| MAF | ★★ declarative agent | ★★★ 四类编排原语+checkpoint | ★★ group 协作 | 无 |
| MetaGPT | ★★★ SOP 角色流水线 | ★★ 流水线 DAG 化 | 弱 | 无 |
| ChatDev | ★★ 双角色成对 | ★★ chat chain 阶段图 | ★★ 阶段内双人聊 | 无 |
| LangGraph | ★★ 节点=agent | ★★★ 状态机图标杆 | 弱 | 无 |
| OpenAI Agents SDK | ★★ | ★★ | ★★ handoff 接管对话 | 弱(agent-as-tool) |
| Swarm | 实验性(并入 SDK) | — | ★★★ | — |
| Camel | ★★★ roleplay | 弱 | ★★★ 角色对话协议 | 弱(任务 A/B) |
| Qwen-Agent | ★★ 成品 agent 模板 | 弱 | 弱 | ★ MCP 工具市场 |
| AgentScope | ★★ | ★★ | ★★ MsgHub | ★★ 分布式 agent 调度 |
| AutoGPT Platform | ★★ agent 模板 | ★★ blocks | 弱 | ★★★ agent 市场 share/fork |
| AgentVerse | ★★ agent 池 | ★★ | ★★ | ★★★ 动态组队(按能力选人) |
| n8n | ★★ agent 节点 | ★★★ 节点图标杆 | 弱 | ★★ 200+ 集成 |
| Dify | ★★ | ★★★ workflow DAG | 弱 | ★★ 插件市场 |
| LangFlow | ★ | ★★★ 拖拽图标杆 | 弱 | ★★ 模板库 |
| RAGFlow | ★ | ★★ agent 工作流节点 | 弱 | 弱 |
| smolagents | ★★ 层级 agent | ★★ | 弱 | 弱 |
| adk-python | ★★★ 层级多 agent | ★★★ workflow runtime | ★★ | ★★ Agent Card/A2A 生态 |
| A2A | — | — | — | ★★★ 协议级员工市场 |

**要点**:角色式(CrewAI/MetaGPT)与 AI-EOS 隐喻最贴;图式(LangGraph/ADK/n8n)最利于可审计;群聊式(AutoGen/AgentScope)灵活但 trace 难读;"市场式"在开源里尚未有成熟本体,AgentVerse 动态组队 + A2A Agent Card 是最接近"员工市场"的两块拼图——**AI-EOS 有差异化机会**。

### A2 记忆方案

| 项目 | 记忆机制 | 一句话特征 |
|---|---|---|
| Letta | 核心/外存分页 + agent 自编辑 + sleep-time | 记忆是 agent 自己管的分页内存,空转期学习 |
| mem0 | 抽取-归纳管道,ADD-only + 实体链接 + 多信号检索 | 中间件式,带公开基准 |
| Generative Agents | memory stream + reflection + 检索三因子 | 反思层让记忆质量随时间增长 |
| CrewAI | 短期/长期/实体三类记忆 | 开箱即用三档 |
| AutoGPT | 向量库工作记忆 | 通用但弱 |
| OpenHands/Devika/Cline | 会话上下文 + 事件流回放 | 靠 trace 而非长期记忆 |
| MetaGPT/ChatDev | 消息池/阶段文档 | 结构化交付物当记忆 |

**AI-EOS 可组合**:Letta 的"自编辑+下班学习" × mem0 的"审批过的高置信沉淀+多信号检索" × Generative Agents 的"反思述职"。

### A3 HITL 审批(机制粒度)

| 项目 | 粒度 | 机制 |
|---|---|---|
| Cline | **工具调用级** | 逐次批准/拒绝 + "始终允许"免批;plan/act 分离 |
| adk-python | **工具级** | tool confirmation flow + 自定义输入 |
| LangGraph | 图节点级 | interrupt() 挂起/resume |
| MAF | 图节点级 | checkpoint + time-travel + HITL |
| AutoGen | 执行级 | UserProxyAgent 代人类确认 |
| OpenAI Agents | 全 run 级 | guardrails tripped 熔断 |
| n8n | 节点级 | 人工等待节点 |
| CrewAI | Flow 级 | wait_for_input() |
| OpenHands | 事件流级 | 向 event stream 注入人工事件(接管) |

**AI-EOS 对标位**:老板审批文化 = Cline 工具级 × OpenHands 事件流级 × ADK 受控输出的组合,目前开源没有把"公司审批流(多级、留痕、可撤销)"做透的产品——差异点成立。

### A4 评估与可观测

| 项目 | 机制 |
|---|---|
| OpenHands | 全事件流=可观测性底座,UI 可回放 |
| MAF/OpenAI Agents SDK | 内建 OpenTelemetry,trace 一等对象 |
| TheAgentCompany | 175 岗位任务 + 有序 checkpoint 部分分 + 双评估器;最强模型 30% 全自主 |
| mem0 | 开源记忆基准(LoCoMo/LongMemEval/BEAM) |
| smolagents/Devika | 极简,靠人工看 |
| 多数框架 | 无内建评估,需自接 LLM-as-judge |

### A5 部署形态(桌面 / 云 / 自托管)

| 形态 | 代表 |
|---|---|
| 桌面优先 | AnythingLLM(一键安装包),Letta letta-code(终端 harness),AI-EOS 本身 |
| 桌面操作 | Cline(VS Code),browser-use(本地+云),open-operator(本地),open-interpreter(本地终端) |
| 自托管(服务器) | Dify/n8n/Langflow/RAGFlow(全家桶 Docker),OpenHands(AIO 镜像) |
| 云 SaaS | AutoGPT Cloud,CrewAI AMP,Letta Cloud,Sakana/AutoGPT 市场 |
| 局域网共享 | **几乎空白**——只有 AnythingLLM 团队版"共享工作区"、Letta Cloud"跨设备记忆同步"沾边,AI-EOS 的 mDNS 广播+配对码+设备吊销是差异化空白点 |

---

## 3. B) Top 10 值得 AI-EOS 学习的设计点(按与"一人公司+老板审批+本地桌面+局域网"契合度排名)

1. **事件流统一 trace 与接管(OpenHands)**:所有 agent 动作=事件,老板接管=向事件流插入"老板事件",回放/分叉/审计全部同流——直接升级 AI-EOS 现有 trace+takeover,且桌面场景全本地可存。
2. **工具级审批 + 免批治理(Cline)**:审批门细到单个工具调用,配"始终允许此工具"防审批疲劳;plan/act 分离让老板先批计划再批执行——与"老板审批文化"最贴的成熟交互样本。
3. **guarded "tripped" 熔断语义(OpenAI Agents SDK)**:护栏触发=暂停+标记原因+保留现场,而非报错——AI-EOS token 预算到顶暂停的精确机制设计(暂停≠失败,老板可"加预算"续跑)。
4. **interrupt() 图挂起 + Postgres checkpointer(LangGraph)**:审批挂起态/护栏暂停态=图在条件边上的可持久化挂起,与 AI-EOS PG 栈无缝;老板批准后 resume 原状态。
5. **岗位 SOP 化:结构化交付物 + 角色文档(MetaGPT)**:员工之间传"文档"(需求/设计/验收标准)而非聊天;审批批的是交付物——强化 AI-EOS 的"公司"隐喻,成果发布门有明确对象。
6. **agent 自编辑记忆 + sleep-time 下班学习(Letta)**:员工写自己的"工作日志/经验",空转期后台学习;审批门可落在"日志变更"上——"数字员工成长"的机制化表达,桌面本地数据不动。
7. **审批过的高置信记忆沉淀 + 多信号检索(mem0)**:老板批过的成果自动升级为高置信记忆/技能;语义/BM25/实体/时间四路检索——记忆体系与审批文化双向咬合的独特设计。
8. **工具接口 ACI 化(SWE-agent)**:给 LLM 做"防呆"工具(限行窗口、即时 lint 反馈)→出错率降=token 省=护栏可放宽,桌面操作员工的工具设计准则;与本地桌面执行强相关。
9. **Agent Card + 任务状态机(needs-auth 态)(A2A)**:mDNS 发现之上发"员工工牌"(能力清单 JSON),任务状态机原生含 needs-auth=审批挂起态——"局域网里其他 agent 公司按卡调用你的员工"的协议地基,开源空白点。
10. **checkpoint 部分分 + 模拟公司任务集(TheAgentCompany)**:给数字员工做"绩效考核":任务拆成有序 checkpoint,部分完成给部分分,LLM 扮 NPC 同事——为 AI-EOS 建立"员工能力考卷/晋升标准",也是向老板证明 ROI 的量化抓手。

(候选第 11-13:time-travel 回放 MAF;自动审稿人漏斗 AI-Scientist;scheduled agent 排班 AutoGPT Platform。)

## 4. C) 风险 / 反模式警示(别学什么)

1. **无护栏全自主循环(AutoGPT 2023 / BabyAGI 原型)**:纯自主 task-loop 烧钱失控、造假捷径(TheAgentCompany 论文记录 agent 会"改名字冒充同事"来跳步)——AI-EOS 的审批+护栏是正确方向,别为"自主性"卖点去拆护栏。
2. **群聊式编排当主范式(AutoGen GroupChat)**:自由群聊 trace 难读、token 浪费大、谁发言不可控;老板要的是可审计——群聊只配当部门内部的"讨论"节点,不能当主干。
3. **纯拖拽流画布当卖点(Flowise 归档 2026-08)**:低代码画布赛道头部都活不过头,无差异点(Flowise vs Dify vs Langflow)必死;AI-EOS 的流程可视化必须长在"员工/审批"隐喻上,不做通用画布。
4. **多端形态不收敛(SuperAGI 停更 2025-01)**:GUI+CLI+服务同时维护、学术→产品无人接力即停更;AI-EOS 桌面+Web+局域网三端,应明确桌面为主形态、其余为投影。
5. **"复刻 Devin"红海(Devika 停更、open-operator 432 star)**:单点 coding agent 已被 OpenHands/Cline 压住,无独立价值;操作型员工要做成"公司里的岗位"(会写代码的文员),不是又一个 IDE 插件。
6. **记忆覆盖式更新(mem0 旧算法的教训)**:UPDATE/DELETE 旧记忆导致信息丢失,新算法改 ADD-only;AI-EOS 记忆/技能沉淀只追加+标注失效,保留可审计的历史。
7. **论文代码当生产底座(Generative Agents / AgentVerse 停更)**:学术框架两年不动即成考古;借鉴其机制(反思/检索三因子)而非依赖其代码。
8. **把 MCP 当静态接入(AI-EOS 自警)**:MCP 协议在快速演进(认证/streaming/elicitation),A2A 等协议正在抢"agent 间"位;只实现 7 个工具不够,要有协议跟踪与 Agent Card 发现能力。
9. **审批做粗(只批任务级)**:工具级/事件级审批(Cline/ADK/OpenHands)已验证可行;只批任务=老板看不见高危单点动作,审批文化落空。
10. **成本模型全 LLM(OASIS 的对照)**:低价值"围观/通知"动作应用规则引擎,关键岗位才用 LLM;token 护栏要建在"调用类型"维度上,而不是只在总预算维度。

---

*报告完。数据快照:2026-10-19(GitHub API star/pushed_at)+ README 抽读。*
