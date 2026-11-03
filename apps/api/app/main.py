"""AI-EOS API entrypoint."""
import logging
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes import (access, agents, artifacts, assets, auth, browser_agent, group_chat, chat_groups, growth_center, doc_styles,
                            agent_chat, delivery, departments_batch, kb2, offboarding, task_dispatch,
                            test_console,
                            career,
                            ceo, ceo_companion, evolution, exams, experiences,
                            clients, pairing, diagnostics, briefing, budgets,
                            mcp, mailboxes, worklogs,
                            companies, company_search, collaboration,
                            computer_agent, context,
                            dashboard, events, health, intervention, knowledge,
                            learning, meetings, memory, messages, metrics, models_api,
                            model_providers_api,
                            orchestrate, performance, projects, reflection,
                            factory, external_agents, review, sandbox,
                            automation, governance,
                            schedules, skill_growth,
                            app_alias, skills, tasks, team_builder, training, workflows, war_room, team_generator, company_manage,
                            tokens, experts, command, org_presets, prometheus_metrics, setup,
                            org_synthesis, plugins, computer_policy,
                            members, licensing, subscription, byok)
from app.api.routes import notifications
from app.api.routes import thread_reads
# 收件箱挂在 /tasks 下，必须比 tasks 路由**先注册**——tasks 有 /{tid} 兜底，
# 后注册的 /tasks/inbox 会被它当成 tid 吃掉。
from app.api.routes import inbox as inbox_routes
from app.api.routes import queue as queue_routes
from app.core.config import settings
from app.core.errors import envelope, error_code_for_status
from app.core.logging_config import get_logger, setup_logging
from app.core.metrics import (
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_IN_FLIGHT,
    HTTP_REQUESTS_TOTAL,
)
from app.core.request_context import (
    REQUEST_ID_HEADER,
    get_request_id,
    is_valid_request_id,
    new_request_id,
    reset_request_id,
    set_request_id,
)
from app.services import license_service
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
    _ = app.models  # pyflakes 不认 noqa：显式引用一次，保留其注册副作用
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

    # 历史 CEO 自愈：升级前已任命的 CEO 也要改名、补齐专属技能包。
    # 失败只记日志，不阻断启动——技能是增强项，不该让服务起不来。
    try:
        from app.ceo_skill_bundle import ensure_ceo_bundles
        from app.db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as _ceo_session:
            _ceo_fix = await ensure_ceo_bundles(_ceo_session)
            await _ceo_session.commit()
        if _ceo_fix["agents"]:
            logger.info("[startup] CEO 自愈：%d 名 CEO，补齐 %d 项技能",
                        _ceo_fix["agents"], _ceo_fix["skills_added"])
    except Exception as _ceo_e:  # noqa: BLE001 — 自愈失败不能拖住服务
        logger.warning("[startup] CEO 技能包自愈失败（不影响启动）：%s", _ceo_e)

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

    # 持久队列自愈：回收过期租约（RUNNING 但进程已不在）并把没跑完的
    # task.execute 重新投递。幂等性来自两点而非"只启动一次"的假设：
    # 只投递仍为 QUEUED 的条目，且真正执行前还要 CAS 领取（抢不到就退出）。
    try:
        from app.services import job_queue
        _queue_recovered = await job_queue.recover_startup()
        if any(_queue_recovered.values()):
            logger.info("[startup] 队列自愈：%s", _queue_recovered)
    except Exception as e:  # noqa: BLE001 — 队列自愈失败不能拖住服务
        logger.error("[startup] 队列自愈失败（不影响启动）：%s", e, exc_info=True)
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
    # 浏览器跨源时默认读不到自定义响应头；把 request_id 暴露出去，
    # 前端报错弹窗才能带上可 grep 的标识交给运维。
    expose_headers=[REQUEST_ID_HEADER],
)


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}
# 运维端点：暴露指标/接口面，默认不给匿名公网/局域网访客看
_METRICS_PATH = "/metrics/prometheus"


def _request_id_for(request) -> str:
    """取当前请求的 request_id（唯一来源：request_id 中间件 + contextvar）。

    优先读 ``request.state``：请求结束后 contextvar 会被还原，而异常路径
    下处理器仍需要同一个 id；state 存在 scope 上，不受 reset 影响。
    中间件没跑到（极端情况）时退回 contextvar，至少不返回空串。
    """
    rid = (getattr(getattr(request, "state", None), "request_id", "") or "").strip()
    return rid or get_request_id()


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
                rid = _request_id_for(request)
                return JSONResponse(
                    status_code=401,
                    content=envelope("metrics 需要有效的 METRICS_TOKEN",
                                     "unauthorized", rid),
                    headers={"WWW-Authenticate": "Bearer",
                             REQUEST_ID_HEADER: rid})
        elif not _is_loopback_client(request):
            rid = _request_id_for(request)
            return JSONResponse(
                status_code=404,
                content=envelope("Not Found", "not_found", rid),
                headers={REQUEST_ID_HEADER: rid})
    return await call_next(request)


# 只读模式放行：License 失效后仍要能"自救"，也要能观测健康。
# 否则用户拿到新 License 却连 /license 上传、重新登录、重装都做不了。
_LICENSE_READ_ONLY_PREFIXES = (
    "/auth", "/license", "/setup", "/health", "/metrics",
    "/docs", "/redoc", "/openapi.json",
)
# 文档与 schema 可能带查询串/静态子路径，额外容忍 `-` 形式。
_LICENSE_READ_ONLY_DASH_PREFIXES = frozenset({"/docs", "/openapi.json"})
_LICENSE_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _license_read_only_allowed(path: str) -> bool:
    for prefix in _LICENSE_READ_ONLY_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
        if prefix in _LICENSE_READ_ONLY_DASH_PREFIXES \
                and path.startswith(prefix + "-"):
            return True
    return False


@app.middleware("http")
async def enforce_license_read_only(request, call_next):
    """License 过期/校验不通过时，全局拦截业务写请求（只读模式）。

    WHY 放在中间件而不是逐路由依赖：写端点有上百个，逐个补依赖必然漏；
    ASGI 层兜底保证"以后新增的写端点默认受限"。同时必须原样放行
    /auth、/license、/setup 等恢复路径——否则用户更新 License 后
    仍然无法登录/上传，只读模式就成了死锁。

    注册顺序：在 guard_ops_endpoints 之后、request_id 之前 add，
    因此这里返回的 403 仍然带 X-Request-ID；metrics 在最外层，
    403 也会被计入指标。GET/HEAD/OPTIONS 一律放行（CORS 预检不能被拦）。
    """
    if request.method not in _LICENSE_WRITE_METHODS:
        return await call_next(request)
    if _license_read_only_allowed(request.url.path):
        return await call_next(request)
    # 放在白名单之后：普通读请求与恢复路径不承担 License 校验开销。
    if not license_service.is_read_only():
        return await call_next(request)
    rid = _request_id_for(request)
    return JSONResponse(
        status_code=403,
        content=envelope(
            "License 已过期或无效，系统处于只读模式；请更新 License 后重试",
            "license_read_only", rid),
        headers={REQUEST_ID_HEADER: rid},
    )


@app.middleware("http")
async def request_id_middleware(request, call_next):
    """给每个请求一个可跨日志关联的 request_id。

    - 客户端带了合法的 X-Request-ID（8–128 位 [A-Za-z0-9._-]）就沿用：
      前端/网关的一次操作可以贯穿多层日志；
    - 否则生成 uuid4 hex；
    - 写入 contextvar 供日志 Filter 与 500 兜底读取，并回写响应头。

    注意注册顺序：本中间件最后 add（Starlette 插入到用户中间件最外层），
    所以 guard_ops_endpoints 提前返回的 401/404 同样带 X-Request-ID。
    异常路径下 contextvar 会在 finally 里还原，兜底处理器改从
    request.state 取同一个 id（scope 共享，不受 reset 影响）。
    """
    supplied = (request.headers.get(REQUEST_ID_HEADER) or "").strip()
    rid = supplied if is_valid_request_id(supplied) else new_request_id()
    request.state.request_id = rid
    token = set_request_id(rid)
    try:
        response = await call_next(request)
    finally:
        reset_request_id(token)
    response.headers[REQUEST_ID_HEADER] = rid
    return response


def _http_failure_level(status_code: int, route_path: str) -> int:
    """失败请求该记多响——只记"没人会注意到"的那两类。

    - 5xx：服务端自己的问题，必须 WARNING；
    - 404 且 route 没匹配上（`unmatched`）：前端调了后端根本不存在的地址。
      这类失败**不会**在任何业务日志里留痕，用户只会看到一句 "Not Found"，
      而运维端一片安静——契约破坏正是靠这里出声；
    - 其余 4xx（401 未登录、403 越权、422 输入错）多是正常交互，记 INFO
      足够，不抢视野。
    """
    if status_code >= 500:
        return logging.WARNING
    if status_code == 404 and route_path == "unmatched":
        return logging.WARNING
    return logging.INFO


@app.middleware("http")
async def http_metrics_middleware(request, call_next):
    """RED 指标：Rate / Errors / Duration，外加 in-flight 并发数。

    注册顺序：本中间件在 request_id 之后 add，Starlette 会把它放到用户
    中间件的最外层。因此 guard_ops_endpoints 提前返回的 401/404、路由
    不存在的 404、以及兜底处理器产生的 500 都会被计数——不会漏响应。

    path 标签必须低基数：优先取路由模板（`/agents/{aid}`），拿不到模板
    时统一归为 `unmatched`，绝不把真实 id 当标签。
    """
    method = request.method
    status_code = 500
    started = time.perf_counter()
    HTTP_REQUESTS_IN_FLIGHT.inc()
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    except BaseException:
        # 未处理异常最终由 exception_handler 变成 500；这里先按 500 记账，
        # 再把异常原样抛回去，不改变任何错误语义。
        status_code = 500
        raise
    finally:
        route = request.scope.get("route")
        path = getattr(route, "path", None) or "unmatched"
        duration = time.perf_counter() - started
        HTTP_REQUESTS_IN_FLIGHT.dec()
        HTTP_REQUESTS_TOTAL.inc(
            method=method, path=path, status=str(status_code))
        HTTP_REQUEST_DURATION_SECONDS.observe(
            duration, method=method, path=path)
        if status_code >= 400:
            # request_id 由更外层的 request_id_middleware 写进 state，
            # 这里取它而不是 contextvar：本中间件在 contextvar 作用域之外。
            logger.log(
                _http_failure_level(status_code, path),
                "HTTP 失败 %s %s -> %s（%.0fms，route=%s，request_id=%s）",
                method, request.url.path, status_code, duration * 1000, path,
                getattr(request.state, "request_id", "-") or "-",
            )


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request, exc: StarletteHTTPException):
    """HTTPException 统一信封：状态码与 detail 原样保留，补 code/message/request_id。

    兼容性优先级（不能倒退）：
    * ``status_code`` 不变——前端按状态码分支；
    * ``detail`` 原样透传（字符串/列表/对象都不改写）——现有前端读它；
    * 保留异常自带的 headers（如 401 的 WWW-Authenticate），并追加
      X-Request-ID，使错误响应与日志/正常响应同源可关联。
    """
    rid = _request_id_for(request)
    detail = exc.detail
    code = getattr(exc, "code", None) or error_code_for_status(
        exc.status_code, detail)
    extra = getattr(exc, "extra", None) or {}
    headers = dict(exc.headers or {})
    headers[REQUEST_ID_HEADER] = rid
    return JSONResponse(status_code=exc.status_code,
                        content=envelope(detail, code, rid, **extra),
                        headers=headers)


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request, exc: RequestValidationError):
    """422 参数校验：detail 保持 FastAPI 原始 errors 列表，另补统一字段。

    前端与部分测试直接按 ``detail[0]["loc"]`` 定位字段，因此这里绝不把
    detail 改写成一句话；``message`` 才是给人看的那一条。
    """
    rid = _request_id_for(request)
    # errors() 可能含 bytes / ValueError 等非 JSON 原生值，必须与 FastAPI
    # 默认处理器一样先做 jsonable 编码。
    detail = jsonable_encoder(exc.errors())
    return JSONResponse(
        status_code=422,
        content=envelope(detail, "validation_error", rid),
        headers={REQUEST_ID_HEADER: rid},
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request, exc):
    """兜底异常处理：任何未被捕获的异常都必须进日志（含堆栈），
    并以统一 JSON 结构返回，避免前端拿到裸 500 后无从判断。

    同时带回 request_id（响应头 + 响应体）：用户截图里的那串标识，
    就是日志里对应的那一次请求，排障不再靠时间戳猜。
    """
    rid = _request_id_for(request)
    token = set_request_id(rid)
    try:
        logger.error("未处理异常 %s %s: %s", request.method, request.url.path, exc,
                     exc_info=exc)
    finally:
        reset_request_id(token)
    detail = "服务器内部错误，请查看后端日志获取详细堆栈"
    return JSONResponse(
        status_code=500,
        headers={REQUEST_ID_HEADER: rid},
        content=envelope(detail, "internal_error", rid,
                         error_type=type(exc).__name__),
    )


# A3：给全部业务端点补一个 `/api/v1` 别名。
# 前端仍走无前缀路径（历史契约不变），但外部集成方可以用稳定版本前缀接入；
# 这里只重写 ASGI scope 的 path，不复制路由、不改变 OpenAPI 与鉴权逻辑。
_API_V1_PREFIX = "/api/v1"


@app.middleware("http")
async def api_v1_alias(request, call_next):
    path = request.scope.get("path", "")
    if path == _API_V1_PREFIX or path.startswith(_API_V1_PREFIX + "/"):
        stripped = path[len(_API_V1_PREFIX):] or "/"
        request.scope["path"] = stripped
        request.scope["raw_path"] = stripped.encode("utf-8")
        request.scope["root_path"] = (
            (request.scope.get("root_path") or "") + _API_V1_PREFIX)
    return await call_next(request)


for r in (health, auth, companies, company_search, agents, agent_chat, models_api, model_providers_api, skills, inbox_routes, tasks, messages, kb2, delivery, group_chat, chat_groups, doc_styles, growth_center,
          assets, events, dashboard, metrics, orchestrate, projects, workflows, meetings,
          collaboration, knowledge, memory, context, artifacts, intervention,
          performance, team_builder, browser_agent, computer_agent, app_alias,
          schedules, learning, training, reflection, skill_growth, career,
          review, factory, external_agents, sandbox, automation, governance,
          ceo, ceo_companion, evolution, exams, experiences,
          offboarding, departments_batch, task_dispatch, test_console, war_room, team_generator, company_manage, tokens, experts, command, org_presets,
          access, prometheus_metrics, clients, pairing, diagnostics, briefing,
          budgets, mcp, mailboxes, worklogs, setup, members, notifications,
          thread_reads, licensing, queue_routes, subscription, byok,
          org_synthesis, plugins, computer_policy):
    app.include_router(r.router)
