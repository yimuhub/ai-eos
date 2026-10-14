# API 文档

Base URL: `http://localhost:8000` | 认证: `Authorization: Bearer <JWT>`（除 auth 外全部需要）

交互式文档: http://localhost:8000/docs

## 认证 /auth
- POST /auth/register {email, username, password}
- POST /auth/login {email, password} -> {access_token}
- GET  /auth/me
- POST /auth/logout

## 组织 /companies /departments
- GET|POST /companies, GET|PATCH|DELETE /companies/{cid}
- GET /companies/{cid}/departments
- POST /departments?cid={cid}, PATCH|DELETE /departments/{did}

## Agent /agents
- GET /agents?company_id&department_id&status（含 model_configs 汇总）
- POST /agents {company_id, name, role, department_id, system_prompt}
- PATCH /agents/{aid}（name/role/status/department_id/manager_agent_id）
- DELETE /agents/{aid}
- POST /agents/{aid}/models {role: primary|secondary|fallback, model_id}

## 模型中心 /models
- CRUD /models；provider_type: openai_compat | anthropic | gemini
- POST /models/{mid}/test —— 连通性实测

## 技能中心 /skills
- GET /skills（自动播种内置技能：text_processing / file_reader / web_search）
- POST /skills/{slug}/install {agent_id} / uninstall

## 任务 /tasks
- GET|POST /tasks?company_id&status&agent_id
- GET /tasks/{tid} —— 含 subtasks/executions/events/interventions 全景
- POST /tasks/{tid}/start —— 分配了 agent 才能启动，后台执行
- POST /tasks/{tid}/pause | resume | cancel?reason=
- 非法状态跳转返回 409

## 多Agent协同
- POST /orchestrate {company_id, title, description, coordinator_agent_id} -> 202 + task_id
  CEO 拆解(2-4子任务) -> 并行执行 -> 汇总报告写入 parent.result + 自动入资产库

## 协作消息
- GET|POST /conversations; GET /conversations/{cid}(含messages)
- POST /conversations/{cid}/messages —— user 发言后参与者 Agent 自动回复

## 资产中心 /assets
- GET /assets?company_id&type
- POST /assets/upload?company_id= (multipart)
- GET /assets/{aid}（文本类返回 content）/ GET /assets/{aid}/download

## 日志与看板
- GET /events?event_type&limit
- GET /dashboard/{company_id} —— Agent统计/任务统计/活跃任务/最近事件与成果
