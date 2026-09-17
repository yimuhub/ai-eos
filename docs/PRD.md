# AI-EOS 总产品需求文档（PRD）v1.0

> 本文档是 AI-EOS 的"宪法"。任何开发者 / AI 编程 Agent 在修改代码前必须先阅读本文档与《ARCHITECTURE.md》。

## 一、产品定位

AI-EOS 是一套 **AI 数字员工操作系统（AI Digital Workforce OS）**：

- 不是聊天机器人，也不是简单的 Agent 编排工具
- 核心是**一支会成长、可治理、能持续产出**的 AI 数字员工团队
- 用户角色 = 公司老板 / 部门负责人，而非"提示词工程师"

## 二、目标用户与核心场景

| 用户 | 场景 |
|---|---|
| 中小企业主 | 一句话成立 AI 部门/公司，承接业务 |
| 文旅策划师 | 组建策划团队，完成景区策划全案 |
| 知识工作者 | 培养专属数字员工，长期积累经验 |

## 三、功能架构（三期已交付）

```
P1 基础层：公司/部门/Agent、任务引擎、模型网关(多模型降级)、技能插件、
          人工介入、执行日志、编排(CEO拆解→并行→汇总)
P2 协同层：项目管理、工作流引擎+可视化编辑器、AI会议(真实LLM)、协同协议2.0、
          知识库RAG、记忆系统、Project Context、成果版本流、自动化L0-4、绩效
P3 成长层：Learning Center、Training、Reflection、Skill Tree、Career、
          晋升转岗、考核、Agent Factory 2.0、外部Agent接入、Computer Runtime、
          Sandbox、高级自动化、自我改进治理、Marketplace、企业模板、自治L0-5
```

## 四、核心原则（不可违背）

1. **Human Always Has Final Control**
   - 所有 Learning / Improvement / Promotion / Transfer / Skill Upgrade 必须走：
     `Proposal → Evaluation → Approval → Versioning → 可 Rollback`
2. **Identity 不可被训练修改**；Core Safety Rules / Human Priority Rules 受保护
3. **禁止虚假能力声明**：技能等级必须有 Evidence；Automated 考核必须基于真实数据
4. **一切可追溯**：Growth Event / Action Log / Execution Log 全量落库，append-only
5. **防失控三件套**：配额限制、深度/速率限制、全局 Emergency Stop + Checkpoint 恢复

## 五、验收基线

- P1：CEO 编排端到端（拆解→并行→汇总）
- P2：全生命周期集成测试 16/16
- P3：数字员工全生命周期集成测试 27/27
- 回归要求：每次改动后运行 `tests/` 下全部 E2E + 对应集成测试

## 六、非目标（当前版本不做）

- 公开商业 Marketplace（仅内部包系统）
- 真实键鼠控制（Sandbox Provider 已预留接口，默认 MOCK）
- 多租户计费

详细需求分解见各期任务包文档。
