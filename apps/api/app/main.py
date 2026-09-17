"""AI-EOS API entrypoint."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import (agents, artifacts, assets, auth, browser_agent, group_chat, growth_center, doc_styles,
                            agent_chat, delivery, departments_batch, kb2, offboarding, task_dispatch,
                            test_console,
                            career,
                            companies, collaboration, computer_agent, context,
                            dashboard, events, health, intervention, knowledge,
                            learning, meetings, memory, messages, models_api,
                            orchestrate, performance, projects, reflection,
                            factory, external_agents, review, sandbox,
                            automation, governance,
                            schedules, skill_growth,
                            app_alias, skills, tasks, team_builder, training, workflows, war_room, team_generator, company_manage,
                            tokens)
from app.core.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 便携/SQLite 模式：启动时自动建表（幂等）
    if settings.database_url.startswith("sqlite"):
        from app.db.session import engine
        from app.db.base import Base
        import app.models  # noqa: F401 — 确保所有模型注册到 Base.metadata
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.create_all(c))
        print("[startup] sqlite schema ensured")
    from app.scheduler_engine import start_scheduler, stop_scheduler
    start_scheduler()
    # 启动时恢复卡在 RUNNING 的会议（自愈）
    try:
        import app.background_jobs as background_jobs
        background_jobs.recover_stuck_meetings()
    except Exception as e:
        print(f"[startup] recover stuck meetings failed: {e}")
    yield
    stop_scheduler()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:8000",
                   "http://127.0.0.1:3000", "http://127.0.0.1:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (health, auth, companies, agents, agent_chat, models_api, skills, tasks, messages, kb2, delivery, group_chat, doc_styles, growth_center,
          assets, events, dashboard, orchestrate, projects, workflows, meetings,
          collaboration, knowledge, memory, context, artifacts, intervention,
          performance, team_builder, browser_agent, computer_agent, app_alias,
          schedules, learning, training, reflection, skill_growth, career,
          review, factory, external_agents, sandbox, automation, governance,
          offboarding, departments_batch, task_dispatch, test_console, war_room, team_generator, company_manage, tokens):
    app.include_router(r.router)
