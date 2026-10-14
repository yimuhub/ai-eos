# AI Developer Constitution

## Rule 1 任何功能开发前：先理解架构 → 再创建模块 → 再写代码。禁止直接随意修改核心代码。
## Rule 2 所有模块必须 Modular / Plugin-based / API-first。
## Rule 3 禁止把 LLM Provider 写死，必须通过 Provider Adapter 调用。
## Rule 4 Agent 必须独立：独立 ID / 配置 / Memory / Skills / Model。
## Rule 5 所有任务必须可恢复：保存 Task State / Execution State / Checkpoint。
## Rule 6 所有 Agent 行为必须记录：Task / Input / Output / Tool / Model / Cost / Time / Error。
## Rule 7 所有 AI 自动化行为必须支持 Pause / Resume / Cancel。
## Rule 8 所有 Agent 通信必须经过统一 Message Protocol。
