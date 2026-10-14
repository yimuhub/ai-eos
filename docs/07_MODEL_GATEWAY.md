# Model Gateway

架构: Agent -> ModelGateway -> ProviderAdapter -> LLM

- BaseModelProvider: chat(messages, tools, config) 唯一接口
- OpenAICompatProvider: 兼容 OpenAI/DeepSeek/Qwen/agnes/llama.cpp（已实装验证）
- Anthropic/Gemini: Skeleton 就位，填 key 即可用
- 故障切换: chat_with_agent_models 按 primary->secondary->fallback 顺序尝试
- API Key 解析: models.api_key_env 指向环境变量，避免明文入库
- 实测: agnes-2.5-flash 连通（POST /models/{mid}/test 返回 OK）
