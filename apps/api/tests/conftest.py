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

## 关键顺序约束

环境变量必须在**导入任何 `app.*` 模块之前**设置，因为
`app.core.config.settings` 与 `app.db.session.engine` 都在导入期实例化。
本文件顶部的 env 设置块因此刻意排在所有 app 导入之前。
"""
from __future__ import annotations

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
# ---------------------------------------------------------------------------
_TMP_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="aieos_tests_"))

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


@pytest.fixture(scope="session")
async def _schema():
    """会话级：建一次表。比每个用例 drop/create 94 张表快得多。"""
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c))
    yield
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.drop_all(c))
    await engine.dispose()


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


@pytest.fixture
def requires_llm():
    """声明"本用例需要真实模型"。无密钥时跳过而非失败。"""
    if not has_llm_key():
        pytest.skip("未配置模型密钥，跳过需要真实 LLM 的端到端用例")
