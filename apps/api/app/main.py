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
from app.core.logging_config import get_logger, setup_logging

# 尽早配置日志：保证后续所有模块（含第三方库降噪）都走统一格式，
# 也让"失败出声"从进程启动第一秒就生效。
setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 安全姿态告警：让"能跑"和"安全"之间的差距在启动日志里就被看见。
    # 生产环境的致命问题已在 Settings 校验时拒绝启动，这里只处理非致命的提醒。
    for _w in settings.security_warnings():
        logger.warning("安全提示：%s", _w)

    # 便携/SQLite 模式：启动时自动建表（幂等）
    from app.db.session import engine
    from app.db.base import Base
    import app.models  # noqa: F401 — 确保所有模型注册到 Base.metadata
    if settings.database_url.startswith("sqlite"):
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.create_all(c))
        logger.info("[startup] sqlite schema ensured")

    # 补列：create_all 只建缺失的表，**不会给已存在的表补新列**。
    # 升级后老数据库缺少新增字段会直接报 "no such column"，
    # 而用户只是换了版本、什么都没做错。这里幂等地把可空列补上。
    from app.db.migrations import ensure_columns
    _added = await ensure_columns(engine)
    if _added:
        logger.info("[startup] 自动补齐 %d 个新增列", len(_added))
    from app.scheduler_engine import start_scheduler, stop_scheduler
    start_scheduler()
    # 启动时恢复卡在 RUNNING 的会议（自愈）
    try:
        import app.background_jobs as background_jobs
        background_jobs.recover_stuck_meetings()
    except Exception as e:
        # 自愈失败要让运维看得见，但不能阻止服务启动
        logger.error("[startup] 恢复卡住的会议失败：%s", e, exc_info=True)
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


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request, exc):
    """兜底异常处理：任何未被捕获的异常都必须进日志（含堆栈），
    并以统一 JSON 结构返回，避免前端拿到裸 500 后无从判断。"""
    logger.error("未处理异常 %s %s: %s", request.method, request.url.path, exc,
                 exc_info=exc)
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=500,
        content={"detail": "服务器内部错误，请查看后端日志获取详细堆栈",
                 "error_type": type(exc).__name__},
    )

for r in (health, auth, companies, agents, agent_chat, models_api, skills, tasks, messages, kb2, delivery, group_chat, doc_styles, growth_center,
          assets, events, dashboard, orchestrate, projects, workflows, meetings,
          collaboration, knowledge, memory, context, artifacts, intervention,
          performance, team_builder, browser_agent, computer_agent, app_alias,
          schedules, learning, training, reflection, skill_growth, career,
          review, factory, external_agents, sandbox, automation, governance,
          offboarding, departments_batch, task_dispatch, test_console, war_room, team_generator, company_manage, tokens):
    app.include_router(r.router)
