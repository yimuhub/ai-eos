"""测试夹具：完全离线、零外部依赖、可重复运行。

## 设计原则

1. **不碰真实数据库**。改造前的 28 个"测试"直接连真实库、硬编码真实 UUID，
   既不可重复运行，也有误伤生产数据的风险。这里每个测试会话用独立的
   临时 SQLite 文件，会话结束即丢弃。
2. **不需要起服务器**。用 `httpx.ASGITransport` 直连 ASGI 应用，
   在进程内完成请求，因此没有端口占用、没有启动等待、可并行。
3. **不需要模型密钥**。核心链路（认证、租户隔离、契约、安全边界）
   全部可在无密钥下验证；真正要调 LLM 的用例用 `requires_llm` 标记，
   没有密钥时自动跳过而不是失败。
4. **也支持 Postgres**（CI 的 `postgres` job）。设置
   `AIEOS_TEST_DATABASE_URL`（或非 sqlite 的 `DATABASE_URL`）即改用该库，
   每个测试进程会先建一个一次性库，跑完 DROP，避免并行互相清库。
   SQLite 仍是默认路径，两者断言完全一致；只在 SQLite 上有意义的
   3 个模块在 PG 下会自动 skip（见 `_SQLITE_ONLY_MODULES`）。

## 关键顺序约束

环境变量必须在**导入任何 `app.*` 模块之前**设置，因为
`app.core.config.settings` 与 `app.db.session.engine` 都在导入期实例化。
本文件顶部的 env 设置块因此刻意排在所有 app 导入之前。
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile
import uuid
from types import SimpleNamespace

import pytest

# ---------------------------------------------------------------------------
# 1) 路径：让 tests/ 能 import app.*（无需安装包）
# ---------------------------------------------------------------------------
API_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

# ---------------------------------------------------------------------------
# 2) 环境隔离（必须早于 app 导入）
#
# 默认仍然是"每个测试会话一个临时 SQLite 文件"。CI 另有一个 Postgres job：
# 它把 `AIEOS_TEST_DATABASE_URL` 指到 service 容器，本文件据此改用 PG。
#
# 为什么只认非 SQLite 的继承值：`DATABASE_URL` 在开发机/CI 上常常指向
# 真实库（apps/api/data/aieos.db）。如果无脑继承，一次 `pytest` 就会把
# 真实数据删光——所以继承的 `DATABASE_URL` 只有在**不是 sqlite**时才被采用，
# SQLite 一律走临时目录，这条安全边界不允许被环境变量绕过。
# ---------------------------------------------------------------------------
_TMP_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="aieos_tests_"))

_EXTERNAL_URL = (os.environ.get("AIEOS_TEST_DATABASE_URL") or "").strip()
if not _EXTERNAL_URL:
    _inherited = (os.environ.get("DATABASE_URL") or "").strip()
    if _inherited and not _inherited.startswith("sqlite"):
        _EXTERNAL_URL = _inherited

# 会话结束时需要丢弃的、由本进程新建的 PG 库（见 _provision_postgres）
_PROVISIONED_PG: dict[str, str] = {}


def _admin_dsn(url: str) -> str:
    """把业务库 URL 改成连维护库 `postgres` 的 asyncpg DSN。"""
    from sqlalchemy.engine import make_url

    admin = make_url(url).set(drivername="postgresql", database="postgres")
    return admin.render_as_string(hide_password=False)


def _pg_admin_exec(dsn: str, *statements: str) -> None:
    """在独立事件循环里连一次维护库执行 DDL（建库/删库用）。"""
    import asyncpg

    async def _run() -> None:
        conn = await asyncpg.connect(dsn, timeout=10)
        try:
            for sql in statements:
                await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(_run())


def _provision_postgres(url: str) -> str:
    """为当前测试进程准备一个**独立**的 PG 库，避免并行跑测试时互相清库。

    做法：连维护库建一个带 pid + 随机后缀的库（`create_all` 会建出全部表），
    会话结束再 DROP。建库需要 CREATEDB 权限；没有权限时退化为
    "就用给定的库 + 开头先 drop_all 清空"（照样能跑，但两个进程同时跑
    会互相干扰，这时请在 URL 里自己给每个进程不同的库）。
    """
    name = f"aieos_test_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        _pg_admin_exec(_admin_dsn(url), f'CREATE DATABASE "{name}"')
    except Exception as exc:  # noqa: BLE001 — 权限/并发等都可能，降级即可
        print(f"[conftest] 无法创建独立测试库（{type(exc).__name__}: {exc}），"
              f"改用给定库并先清空", file=sys.stderr)
        return url

    from sqlalchemy.engine import make_url

    _PROVISIONED_PG["name"] = name
    return make_url(url).set(database=name).render_as_string(hide_password=False)


async def _drop_provisioned_pg() -> None:
    """会话收尾：删掉本进程建的临时库（尽力而为，失败不掩盖测试结果）。"""
    name = _PROVISIONED_PG.get("name")
    admin = _PROVISIONED_PG.get("admin")
    if not name or not admin:
        return
    _PROVISIONED_PG.clear()
    try:
        # 放到线程里跑：它自建事件循环，不能和正在收尾的 session loop 抢
        await asyncio.to_thread(
            _pg_admin_exec, admin, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    except Exception as exc:  # noqa: BLE001
        print(f"[conftest] 临时测试库 {name} 未能删除：{type(exc).__name__}",
              file=sys.stderr)


if _EXTERNAL_URL:
    if _EXTERNAL_URL.startswith("postgres"):
        _PROVISIONED_PG["admin"] = _admin_dsn(_EXTERNAL_URL)
        os.environ["DATABASE_URL"] = _provision_postgres(_EXTERNAL_URL)
    else:
        # 显式指定的其它方言（如某人显式传了 sqlite URL）原样使用。
        os.environ["DATABASE_URL"] = _EXTERNAL_URL
else:
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_ROOT / 'test.db'}"
os.environ["JWT_SECRET"] = "test-only-secret-not-for-production"
os.environ["AIEOS_ENV"] = "test"
# 测试默认安静，排查问题时设 AIEOS_LOG_LEVEL=DEBUG 即可看到全部失败细节
os.environ.setdefault("AIEOS_LOG_LEVEL", "ERROR")
# SQLAlchemy echo 跟随 settings.debug，必须关掉否则刷屏
os.environ["DEBUG"] = "false"
os.environ["STORAGE_DIR"] = str(_TMP_ROOT / "storage")
# 清掉可能存在的模型密钥，确保"无密钥"路径被真实覆盖
for _k in ("OPENAI_COMPAT_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
    os.environ.pop(_k, None)

# ---------------------------------------------------------------------------
# 3) 导入应用（此刻配置已定型）
# ---------------------------------------------------------------------------
import httpx  # noqa: E402

import app.models  # noqa: E402,F401 — 必须先导入，把全部表注册到 Base.metadata
from app.db.base import Base  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.main import app as asgi_app  # noqa: E402

# 默认测试账号口令：满足"≥8 位且含字母与数字"
DEFAULT_PASSWORD = "Test1234pass"

# 测试库方言：SQLite 之外的分支（PG job）用得到
TEST_DB_DIALECT = engine.dialect.name


@pytest.fixture(scope="session")
async def _schema():
    """会话级：建一次表。比每个用例 drop/create 94 张表快得多。

    SQLite 每次都是全新临时文件，直接 create_all。
    PG 可能是"独占的新库"（正常路径）或"复用的库"（无 CREATEDB 权限时的
    降级路径）；后者必须先把残留表清掉，否则测试会撞上上一次运行的脏数据。
    """
    if TEST_DB_DIALECT != "sqlite":
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.drop_all(c))
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c))
    yield
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.drop_all(c))
    await engine.dispose()
    await _drop_provisioned_pg()


# 只在 SQLite 上有意义的**单个用例**（不是整目录豁免）：直接开 sqlite3 连接 /
# 查 sqlite_master / 断言 PRAGMA。PG job 下按 nodeid 精确跳过；同文件里
# 与方言无关的用例照常在 PG 上跑。这些用例在 backend job 的 SQLite 门禁里
# 全部照跑，断言一个字都没改。
_SQLITE_ONLY_TESTS = {
    "test_sqlite_concurrency.py::test_main_engine_enables_busy_timeout_and_foreign_keys":
        "断言 PRAGMA busy_timeout/foreign_keys 与 WAL，PG 无对应物",
    "test_sqlite_concurrency.py::test_background_factory_applies_same_pragmas":
        "断言后台 engine 也挂了 SQLite PRAGMA 监听器",
    "test_concurrency_guards.py::test_war_room_kickoff_commits_before_background_submit":
        "用 sqlite3.connect 开独立连接读库验证已提交，asyncpg 下没有对应写法",
    "test_concurrency_guards.py::test_workflow_cancel_stops_further_progress":
        "末尾断言 select(WorkflowStep) 的返回顺序，但该查询没有 ORDER BY："
        "SQLite 按 rowid 返回插入顺序，PG 在时间戳相同时不保证顺序——"
        "这是测试对方言返回顺序的隐含依赖（已在 PG 首跑中确认），不是产品行为差异",
    "test_lifespan.py::test_lifespan_runs_startup_and_shutdown":
        "用 sqlite_master 统计表数量，PG 需改查 information_schema",
}


def pytest_collection_modifyitems(config, items):
    """PG 下跳过 SQLite 专属用例，并明确报"跳过原因"。"""
    if TEST_DB_DIALECT == "sqlite":
        return
    for item in items:
        why = _SQLITE_ONLY_TESTS.get(f"{item.path.name}::{item.name}")
        if why:
            item.add_marker(pytest.mark.skip(
                reason=f"仅在 SQLite 下有意义（当前方言 {TEST_DB_DIALECT}）：{why}"))


@pytest.fixture(autouse=True)
async def _clean_tables(_schema):
    """用例级：跑完清空数据，保证用例之间互不影响、顺序无关。

    为什么不用事务回滚：被测代码内部会自己 commit（真实业务语义就是提交），
    回滚式隔离会与业务提交冲突。直接删行更贴近真实运行方式。
    """
    yield
    async with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())


@pytest.fixture
async def client(_schema):
    """进程内 ASGI 客户端——不监听端口，不发真实网络请求。"""
    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=30.0
    ) as c:
        yield c


@pytest.fixture
def make_user(client):
    """注册并登录一个用户，返回 {id, email, username, password, headers}。"""

    async def _make(email: str | None = None, username: str | None = None,
                    password: str = DEFAULT_PASSWORD) -> dict:
        suffix = uuid.uuid4().hex[:8]
        email = email or f"user_{suffix}@example.com"
        username = username or f"user_{suffix}"
        r = await client.post("/auth/register", json={
            "email": email, "username": username, "password": password})
        assert r.status_code == 201, f"注册失败: {r.status_code} {r.text}"
        r = await client.post("/auth/login", json={
            "identity": username, "password": password})
        assert r.status_code == 200, f"登录失败: {r.status_code} {r.text}"
        body = r.json()
        return {
            "id": body["user"]["id"], "email": email, "username": username,
            "password": password,
            "headers": {"Authorization": f"Bearer {body['access_token']}"},
        }

    return _make


@pytest.fixture
def make_company(client, make_user):
    """给某个用户建一家公司，返回公司 dict。"""

    async def _make(user: dict, name: str | None = None) -> dict:
        r = await client.post("/companies", json={
            "name": name or f"公司-{uuid.uuid4().hex[:6]}"},
            headers=user["headers"])
        assert r.status_code == 201, f"建公司失败: {r.status_code} {r.text}"
        return r.json()

    return _make


@pytest.fixture
async def tenants(make_user, make_company) -> SimpleNamespace:
    """两个互不相干的租户，各自拥有一家公司。

    放在 conftest 而不是某个测试模块里：租户隔离与安全边界的用例
    都要用它，而"只有本模块能用的夹具"会诱导后来者复制粘贴出第二份。
    """
    alice = await make_user()
    bob = await make_user()
    alice_co = await make_company(alice, name="A 公司")
    bob_co = await make_company(bob, name="B 公司")
    return SimpleNamespace(alice=alice, alice_co=alice_co,
                           bob=bob, bob_co=bob_co)


def has_llm_key() -> bool:
    """是否存在任一可用的模型密钥（供 requires_llm 用例做前置判断）。"""
    from app.core.config import settings

    return bool(
        settings.openai_compat_api_key
        or settings.anthropic_api_key
        or settings.gemini_api_key
        or os.environ.get("OPENAI_COMPAT_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
    )


def pytest_configure(config):
    """注册 requires_llm 标记。

    用例侧用的是 `requires_llm` **夹具**（无密钥时自动 skip）；这里额外把同名标记
    注册上，是为了让命令行筛选也能用：

        pytest tests/ -m "not requires_llm"     # CI 里显式排除需真实模型的用例

    两者并存不冲突：夹具负责"无密钥时跳过"，标记负责"按需筛选"。
    """
    config.addinivalue_line(
        "markers",
        "requires_llm: 该用例需要真实模型密钥；无密钥时由 requires_llm 夹具自动跳过",
    )


@pytest.fixture
def requires_llm():
    """声明"本用例需要真实模型"。无密钥时跳过而非失败。"""
    if not has_llm_key():
        pytest.skip("未配置模型密钥，跳过需要真实 LLM 的端到端用例")
