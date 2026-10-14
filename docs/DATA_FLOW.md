# AI-EOS 数据流向告知

> 您的数据去哪了，一张图说清。

## 1. 默认模型网关

`apps/api/app/core/config.py` 的默认配置：

```
OPENAI_COMPAT_BASE_URL=https://api.agnes-ai.cn/v1
```

这是一个**第三方 relay（中转）网关**，不是 AI-EOS 自己的模型服务。

## 2. 数据流向

```
您的输入（任务描述、对话、知识库检索片段、上传的文档内容）
        │
        ▼
AI-EOS 后端（本机 / 您的服务器）
        │  经 OPENAI_COMPAT_BASE_URL 转发（需 OPENAI_COMPAT_API_KEY）
        ▼
https://api.agnes-ai.cn/v1  ← 第三方网关（agnes-ai）
        │  再转发给其上游模型服务商
        ▼
模型输出返回原路
```

- **会离开您机器的数据**：发给模型的提示词（含任务/对话/知识库片段/文档内容）、模型返回的内容。
- **留在本地的数据**：账号密码（bcrypt 哈希）、业务数据库（`data/` 或 PostgreSQL）、
  文件产出物（`storage/`）、API Key（只写本地 `.env`，不上传任何地方）。
- **网关能看到**：您的 API Key、请求与返回的文本内容（relay 的固有属性）。

## 3. 合规建议

1. **涉密内容**：不要用默认网关，换成您自己的网关地址（见下）。
2. **企业使用**：建议私有化部署 + 自有模型网关，并与法务确认数据出境要求。
3. **个人试用**：默认网关开箱即用，但请勿输入身份证号、银行卡等敏感信息。

## 4. 如何切换网关

在 `apps/api/.env` 里改两行，重启后端即可：

```ini
OPENAI_COMPAT_BASE_URL=https://您自己的网关地址/v1
OPENAI_COMPAT_API_KEY=您的密钥
```

也支持 Anthropic / Gemini 直连（`ANTHROPIC_API_KEY` / `GEMINI_API_KEY`），
或在前端「模型配置」页按向导登记。模板见仓库根 `.env.example`。
