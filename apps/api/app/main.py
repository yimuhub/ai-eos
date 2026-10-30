"""AI-EOS API entrypoint."""
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import (access, agents, artifacts, assets, auth, browser_agent, group_chat, chat_groups, growth_center, doc_styles,
                            agent_chat, delivery, departments_batch, kb2, offboarding, task_dispatch,
                            test_console,
                            career,
                            clients, pairing, diagnostics, briefing, budgets,
                            mcp, mailboxes, worklogs,
                            org_presets,
                            companies, collaboration, computer_agent, context,
                            dashboard, events, health, intervention, knowledge,
                            learning, meetings, memory, messages, metrics, models_api,
                            orchestrate, performance, projects, reflection,
                            factory, external_agents, review, sandbox,
                            automation, governance,
                            schedules, skill_growth,
                            app_alias, skills, tasks, team_builder, training, workflows, war_room, team_generator, company_manage,
                            tokens, experts, command, org_presets, prometheus_metrics, setup,
                            members)
from app.core.config import settings
from app.core.logging_config import get_logger, setup_logging
from app.version import __version__

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
    else:
        # PostgreSQL 的**新表**同样幂等补齐（create_all 只建缺失表，
        # 不改已存在的表——补列仍由 Alembic 管，双轨不打架）
        try:
            async with engine.begin() as conn:
                await conn.run_sync(lambda c: Base.metadata.create_all(c))
            logger.info("[startup] postgres 缺失表已幂等补齐")
        except Exception as _pg_e:  # noqa: BLE001
            logger.warning("[startup] postgres create_all 检查失败（不影响启动）：%s", _pg_e)

    # 补列：create_all 只建缺失的表，**不会给已存在的表补新列**。
    # 升级后老数据库缺少新增字段会直接报 "no such column"，
    # 而用户只是换了版本、什么都没做错。这里幂等地把可空列补上。
    from app.db.migrations import ensure_columns
    _added = await ensure_columns(engine)
    if _added:
        logger.info("[startup] 自动补齐 %d 个新增列", len(_added))

    # 超管回填（P5）：老库没有 is_superadmin 概念，若不标记一个超管，
    # 安装向导会把已有数据的安装误判成"全新安装"。必须放在 ensure_columns
    # 之后（列可能是上一步补出来的）。失败只记日志，绝不阻断启动。
    try:
        from app.db.migrations import backfill_superadmin
        await backfill_superadmin(engine)
    except Exception as _bf_e:  # noqa: BLE001 — 回填失败不能拖住服务
        logger.warning("[startup] 超管回填失败（不影响启动）：%s", _bf_e)

    from app.scheduler_engine import start_scheduler, stop_scheduler
    start_scheduler()
    # mDNS 局域网广播（V5.4）：零依赖降级，失败不影响启动
    try:
        from app import mdns_advertise
        mdns_advertise.start()
    except Exception as e:
        logger.warning("[startup] mDNS 广播未启动（不影响服务）：%s", e)
    # 启动时恢复卡在 RUNNING 的会议（自愈）
    try:
        import app.background_jobs as background_jobs
        background_jobs.recover_stuck_meetings()
        # 启动时恢复卡在 RUNNING/PAUSED 的任务（自愈）：
        # 重启后不再留下永远"执行中"的僵尸任务行
        background_jobs.recover_stuck_tasks()
    except Exception as e:
        # 自愈失败要让运维看得见，但不能阻止服务启动
        logger.error("[startup] 恢复卡住的会议/任务失败：%s", e, exc_info=True)
    yield
    stop_scheduler()
    try:
        from app import mdns_advertise
        mdns_advertise.stop()
    except Exception as e:
        # 关停阶段的失败同样要出声：mDNS 组播套接字没关掉会残留广播，
        # 但绝不能因为清理失败而吞掉其它关停步骤或静默退出
        logger.warning("[shutdown] mDNS 广播停止失败（服务已在退出，仅记录）：%s",
                       e, exc_info=True)


# docs/redoc/openapi 只在 DEBUG=True 时挂载：匿名 GET /openapi.json 过去会把
# 312KB 的完整 API 面（路径、字段、模型名）交给任何扫到端口的人，等于给
# 攻击者一份接口清单。关掉后路由根本不存在（404），不是靠鉴权兜底。
# 注意：app.openapi() 仍可按需生成 schema，"API 面完整性"的护栏测试不受影响。
app = FastAPI(
    title=settings.app_name,
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    openapi_url="/openapi.json" if settings.debug else None,
)

app.add_middleware(
    CORSMiddleware,
    # 白名单 = 本地回环 + 局域网常见地址。用 regex 放开 LAN IP（如 192.168.x.x:3000）：
    # 前端若经局域网 IP 访问，Origin 不在白名单时带 Authorization 的 POST/PUT 预检
    # 会被浏览器拦截，fetch 抛网络异常，前端只看到「无法连接后端服务」——误导性极强。
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}
# 运维端点：暴露指标/接口面，默认不给匿名公网/局域网访客看
_METRICS_PATH = "/metrics/prometheus"


def _is_loopback_client(request) -> bool:
    """请求是否来自本机回环地址。

    只看 socket 层的 request.client.host——X-Forwarded-For 是客户端可伪造的，
    用它判定"是否本机"等于没有判定。反代场景请显式配置 METRICS_TOKEN。
    """
    client = getattr(request, "client", None)
    host = (getattr(client, "host", "") or "").strip()
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def _metrics_token_ok(request) -> bool:
    """是否带上了正确的 metrics token（Authorization: Bearer 或 ?token=）。"""
    expected = (settings.metrics_token or "").strip()
    if not expected:
        return False
    supplied = ""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    else:
        # 兼容 Prometheus 常用的 bearer_token 抓取配置之外的 ?token= 写法
        supplied = request.query_params.get("token", "")
    return bool(supplied) and secrets.compare_digest(supplied, expected)


@app.middleware("http")
async def guard_ops_endpoints(request, call_next):
    """收紧匿名可访问的元信息端点。

    - DEBUG=True（开发/便携默认）：保持现状，文档与指标全开，不破坏开发体验。
    - DEBUG=False：
      * /docs、/redoc、/openapi.json 已在 FastAPI 构造时关掉（路由根本不存在）
      * /metrics/prometheus：配了 METRICS_TOKEN 就按 token 校验；
        没配则只允许回环地址，其他来源一律 404（不暴露"这里有个端点"）。
    """
    if request.url.path == _METRICS_PATH and not settings.debug:
        if (settings.metrics_token or "").strip():
            if not _metrics_token_ok(request):
                return JSONResponse(status_code=401,
                                    content={"detail": "metrics 需要有效的 METRICS_TOKEN"},
                                    headers={"WWW-Authenticate": "Bearer"})
        elif not _is_loopback_client(request):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
    return await call_next(request)


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

for r in (health, auth, companies, agents, agent_chat, models_api, skills, tasks, messages, kb2, delivery, group_chat, chat_groups, doc_styles, growth_center,
          assets, events, dashboard, metrics, orchestrate, projects, workflows, meetings,
          collaboration, knowledge, memory, context, artifacts, intervention,
          performance, team_builder, browser_agent, computer_agent, app_alias,
          schedules, learning, training, reflection, skill_growth, career,
          review, factory, external_agents, sandbox, automation, governance,
          offboarding, departments_batch, task_dispatch, test_console, war_room, team_generator, company_manage, tokens, experts, command, org_presets,
          access, prometheus_metrics, clients, pairing, diagnostics, briefing,
          budgets, mcp, mailboxes, worklogs, setup, members):
    app.include_router(r.router)
