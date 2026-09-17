"""冒烟测试：应用能否真正启动、核心基础能力是否在位。

这一组用例的价值不在于"测出了什么 bug"，而在于：改造前项目连
"应用能否导入"都没有自动化保障——9 处未定义变量长期存在而无人发现，
正是因为缺少这一层最低成本的护栏。
"""
from __future__ import annotations

from app.main import app as asgi_app


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_openapi_schema_builds():
    """OpenAPI 能生成 = 所有路由的依赖、类型、模型注解都能解析。

    改造前 agent_runtime.py 引用了未导入的 AssetType，这类问题
    不会在 OpenAPI 生成时暴露（它只解析签名），所以本用例只做基础保障，
    真正的"未定义名"由 test_code_quality.py 用静态分析兜住。
    """
    schema = asgi_app.openapi()
    assert len(schema["paths"]) > 100, "路由数骤降，可能有大片路由未注册"


async def test_no_unhandled_500_on_unknown_route(client):
    """未知路径应为 404，而不是被兜底处理器变成 500。"""
    r = await client.get("/definitely-not-a-real-route-xyz")
    assert r.status_code == 404


async def test_all_sqlalchemy_mappers_configure():
    """配置 mapper = 所有 relationship 的字符串引用都能解析。

    改造前 models/agent.py 里的 `Department` 前向引用没声明，
    这类问题会在这里被拦住。
    """
    from sqlalchemy.orm import configure_mappers

    configure_mappers()  # 配置失败会抛异常
