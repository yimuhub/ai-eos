"""响应模型护栏（A3 收口）。

## 为什么值得一条测试

A3 的目标是让 OpenAPI 的 2xx 响应不再是空 schema：外部集成方与前端类型
都靠它。2026-10-31 审计时全库只有 10/435 个操作声明了 `response_model`，
本轮把核心业务路由补齐到 90%。但「补过一次」不等于「以后都有」——
历史路由仍有大量裸 `dict`，新写的端点也很容易照抄旧写法。

因此这里锁两件事：
1. 核心业务路由（员工/任务/项目/交付/评审/调度/技能/学习/绩效/反思）的
   2xx JSON 操作中，声明 `response_model` 的比例不得低于 90%；
2. 全库已声明的数量与占比不得回退（只许上调，不许悄悄改回去）。

不要求一次补齐 471 个历史操作：那需要逐个核对真实返回结构，盲目加
`response_model` 反而会把多出来的字段在序列化时静默丢掉。这里用「棘轮」
防止倒退，新端点必须带模型。
"""
from __future__ import annotations

from fastapi.routing import APIRoute, _IncludedRouter

from app.main import app

# 核心业务路由：这些模块直接支撑「一句话 → 全公司干活 → 交付结果」主线。
CORE_MODULES = frozenset({
    "app.api.routes.agents",
    "app.api.routes.tasks",
    "app.api.routes.projects",
    "app.api.routes.delivery",
    "app.api.routes.review",
    "app.api.routes.schedules",
    "app.api.routes.skills",
    "app.api.routes.learning",
    "app.api.routes.performance",
    "app.api.routes.reflection",
})

CORE_MIN_RATIO = 0.90

# 2026-11-01 实测：112 / 471；同日补完知识库域（`/knowledge` + `/kb2` 共 20 条）
# 后为 202 / 461 = 43.8%。地板取整到 200 / 0.42，任何时候都不许下调。
BASELINE_TYPED = 200
BASELINE_MIN_RATIO = 0.42
MIN_TOTAL_OPERATIONS = 400


def _all_api_routes() -> list[APIRoute]:
    """展开 FastAPI 的惰性 include 包装，拿到真实 APIRoute。"""
    found: list[APIRoute] = []

    def walk(routes) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                found.append(route)
            elif isinstance(route, _IncludedRouter):
                walk(route.original_router.routes)
            elif hasattr(route, "routes"):
                walk(route.routes)

    walk(app.routes)
    return found


def _is_json_operation(route: APIRoute) -> bool:
    return route.status_code != 204


def test_core_business_routes_declare_response_models():
    missing: list[str] = []
    typed = total = 0
    for route in _all_api_routes():
        if route.endpoint.__module__ not in CORE_MODULES:
            continue
        if not _is_json_operation(route):
            continue
        total += 1
        if route.response_model is not None:
            typed += 1
        else:
            methods = ",".join(sorted(route.methods))
            missing.append(f"{methods} {route.path}  ({route.endpoint.__name__})")

    assert total > 0, "核心模块一条路由都没解析到，说明 include 结构变了"
    ratio = typed / total
    assert ratio >= CORE_MIN_RATIO, (
        f"核心业务路由 response_model 覆盖率 {typed}/{total} = {ratio:.0%}，"
        f"低于 {CORE_MIN_RATIO:.0%}。缺模型的端点：\n  " + "\n  ".join(missing)
    )


def test_response_model_coverage_never_regresses():
    routes = [r for r in _all_api_routes() if _is_json_operation(r)]
    typed = sum(1 for r in routes if r.response_model is not None)

    assert len(routes) >= MIN_TOTAL_OPERATIONS, (
        f"只解析到 {len(routes)} 个操作，OpenAPI/路由注册可能坏掉了"
    )
    assert typed >= BASELINE_TYPED, (
        f"声明了 response_model 的操作从 {BASELINE_TYPED} 降到了 {typed}；"
        "新增端点请一并声明响应模型"
    )
    ratio = typed / len(routes)
    assert ratio >= BASELINE_MIN_RATIO, (
        f"response_model 占比 {typed}/{len(routes)} = {ratio:.1%}，"
        f"低于基线 {BASELINE_MIN_RATIO:.0%}：新端点不能返回裸 dict"
    )
