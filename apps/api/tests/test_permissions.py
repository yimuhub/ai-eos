"""权限模型的守卫：谁可以做什么，以及"新增的危险端点不能没有守卫"。

## 这一组测试为什么值得单独存在

跨租户越权（IDOR）有个很坏的性质：**它在单租户使用下完全看不出来**。
所有功能都正常，只有第二个账号出现时，数据才会串。

改造前审计发现并修掉了 career 模块的越权；这次端到端试跑又发现
模型配置（`/models`）的列表/改/删/测试**完全没有归属校验**，
以及两个急停接口能被任何登录用户调用来影响他人公司。
同类问题反复出现，说明靠"每次审计时人肉看一遍"是不够的。

因此这里放三层护栏：

* 行为层：对已知的高危端点，用第二个租户账号实际调用，要求 404。
* 静态层（写）：扫描源码，**任何新增的 DELETE 端点都必须带归属校验痕迹**，
  否则门禁直接变红，迫使作者在合并前处理。
* 静态层（读）：**任何从 Query / Pydantic body 取 company_id 的端点（GET 也算）**
  都必须有归属校验痕迹，否则视为越权读债务；见
  test_every_company_id_endpoint_has_an_ownership_guard。它用"基线登记表"式
  断言——已知违规先固化进 _READ_DEBT_ALLOWLIST，只拦"新增的、没登记的"越权读，
  这样今天就能进 CI，同时把技术债显式记账，而不是假装通过。

静态层是启发式的（匹配关键字），它不证明"逻辑正确"，
但能可靠地拦住"新加了一个删除接口，忘了写校验"这个最常见的失误。
"""
from __future__ import annotations

import ast
import pathlib
import re

import app

# 不写模块级 pytestmark：pyproject 里已设 asyncio_mode=auto，
# 而本文件混有同步测试（源码扫描、矩阵语义），加标记只会产生
# "sync 函数被标为 asyncio" 的告警，掩盖真正的问题。

ROUTE_DIR = pathlib.Path(app.__file__).resolve().parent / "api" / "routes"

# 只要出现任意一个，就认为这个删除端点"声明了归属校验"
GUARD_MARKERS = (
    "ensure_capability",   # 统一权限层
    "require_capability",
    "_owned",              # _owned_company / _owned_department / _project_owned
    "_verify_",            # _verify_agent_access
    "owner_id",            # 就地比较归属（历史写法）
    "Capability",
)


# ---------------------------------------------------------------------------
# 静态层：新增 DELETE 端点必须带守卫
# ---------------------------------------------------------------------------

_TRIPLE_QUOTED = re.compile(r'"""(?:.|\n)*?"""|\'\'\'(?:.|\n)*?\'\'\'')


def _strip_noise(src: str) -> str:
    """去掉注释与文档字符串。

    不做这一步的话，一句"仅所有者可删除"的注释就能让门禁误判为已守卫——
    注释不是校验。
    """
    src = _TRIPLE_QUOTED.sub("", src)
    return "\n".join(line.split("#", 1)[0] for line in src.splitlines())


def _delete_handler_blocks(
        route_dir: pathlib.Path = ROUTE_DIR) -> list[tuple[str, int, str]]:
    """产出 (文件名, 起始行号, 代码块) —— 每个 DELETE 端点一块。

    route_dir 可注入：反向验证用例要拿一个临时目录（故意放违规样本）来
    调用**被测扫描器本身**，而不是在测试里重写一遍正则。
    """
    blocks: list[tuple[str, int, str]] = []
    for path in sorted(route_dir.glob("*.py")):
        src = _strip_noise(path.read_text(encoding="utf-8"))
        for m in re.finditer(r"@router\.delete\(", src):
            nxt = src.find("\n@router.", m.end())
            block = src[m.start(): nxt if nxt != -1 else len(src)]
            line_no = src[:m.start()].count("\n") + 1
            blocks.append((path.name, line_no, block))
    return blocks


def test_every_delete_endpoint_has_an_ownership_guard():
    """每个删除端点都必须带归属校验——这是防越权的最后一道自动防线。"""
    blocks = _delete_handler_blocks()
    # 空输入假绿：正则失效时 blocks=[]，下面的 unguarded=[] 会"通过"。
    assert blocks, "没有扫描到任何 DELETE 端点——扫描器失效（空输入假绿）"
    unguarded = [
        f"{name}:{line}"
        for name, line, block in blocks
        if not any(marker in block for marker in GUARD_MARKERS)
    ]
    assert not unguarded, (
        "以下删除端点没有任何归属校验痕迹，请补上 ensure_capability / "
        f"_owned_* / _verify_* 或 _owned 系列检查：{unguarded}"
    )


def test_guard_scanner_actually_detects_violations(tmp_path):
    """反向验证删除扫描器：直接调用被测函数，违规样本必须红、守卫样本必须绿。

    M3 修正：老用例在测试里自己重写了一遍 `@router.delete(` 正则，
    真扫描器返回 `[]` 它照样绿。现在把临时目录注进**被测函数**，
    扫描器一旦失效（正则截断/空目录）这条用例至少有一半会失败。
    """
    (tmp_path / "bad.py").write_text(
        '@router.delete("/x/{id}")\n'
        'async def bad(yid: str, db=Depends(get_db)):\n'
        '    """仅所有者可删除（这句注释不算校验）"""\n'
        "    await db.delete(await db.get(Thing, yid))\n"
        "\n"
        '@router.get("/y")\n'
        "async def other():\n"
        "    return {}\n",
        encoding="utf-8")
    (tmp_path / "good.py").write_text(
        '@router.delete("/z/{id}")\n'
        'async def good(zid: str, db=Depends(get_db), user=Depends(get_current_user)):\n'
        "    await assert_company_owned(db, user.id, zid)\n"
        "    return {'ok': True}\n",
        encoding="utf-8")

    blocks = _delete_handler_blocks(tmp_path)
    assert len(blocks) == 2, f"扫描器没认出两个 DELETE 端点：{blocks}"

    flagged = {
        name for name, _line, block in blocks
        if not any(marker in block for marker in GUARD_MARKERS)
    }
    assert flagged == {"bad.py"}, (
        "删除扫描器判定有误：bad.py 无守卫应报警、good.py 有 assert_company_owned "
        f"应放行，实得 {flagged}"
    )


# ---------------------------------------------------------------------------
# 静态层（读）：任何从 Query / body 取 company_id 的端点都要有归属校验（GET 也查）
# ---------------------------------------------------------------------------
#
# 越权"读"和越权"删"一样是 IDOR，但 DELETE 有守卫、GET/POST 却没有：
#   GET  /artifacts?company_id=<他人>、GET /kb2/global/documents?company_id=<他人>
#   只要把 company_id 当查询条件、不校验调用者是否拥有该公司，任何登录用户都能读。
#   模型配置、知识库、账单统计此前都被这样看过。
#
# 判定标准：端点从 Query 或 Pydantic body 里取 company_id（调用者可自由指定），
# 且函数体内没有任何归属校验痕迹。痕迹词表复用删除守卫那一套，另外补上本次
# 读端点收口实际用到的 assert_company_owned / resolve_role，以及 P5 收口后
# 路由改用的 build_ctx / assert_company_capability / ensure_agent_access。
#
# 这是"基线登记表"：第一天必然是红的（本次并行修复只覆盖了最严重的几处），
# 所以已知违规先固化进 _READ_DEBT_ALLOWLIST，测试只拦"新增的、没登记的"越权读——
# 今天就能进 CI，同时把技术债显式记账，而不是靠削弱断言假装通过。
#
# 已知启发式边界（宁可少报、不制造假债务）：
#   * 校验常下沉到同文件的 _owned(...) / _company_of(...) 辅助函数——本扫描做一跳
#     传递：handler 调用了"体内确在校验归属"的本地函数即视为已守卫。
#   * 只看同一文件内的辅助；跨文件的校验辅助（少数）会被漏判为违规，属可接受的
#     保守误报，收口后从 allowlist 删条目即可。
#   * 词表里的 owner_id 按题面口径接受，故"输出里带 owner_id 但没真校验"的极端写法
#     会被判为已守卫；这类端点本 allowlist 未登记，属已知的扫描盲区。

# 出现任意一个即认为端点"声明了归属校验"。
READ_GUARD_MARKERS = (
    "assert_company_owned",   # 依赖层：按 company_id 直接校验归属
    "assert_agent_owned",     # 按 agent_id 反查公司
    "ensure_capability",      # 统一权限层
    "require_capability",
    # P5 收口后的新写法：build_ctx 在 resolve_role 拿到 None（与该公司的
    # 任何角色都无关）时直接 404，是完整的租户门禁，不是"只查了个公司名"。
    "build_ctx(",
    # assert_company_owned 的能力版：公司存在 + 角色具备 VIEW/OPERATE/...
    "assert_company_capability(",
    # 按 Agent 反查其公司，再叠加角色与行级范围，不通过统一 404
    "ensure_agent_access(",
    "selectable_company",     # browser-agent 的 _assert_selectable_company
    "_owned_",                # _owned_company / _owned_department ...
    "owner_id",               # 就地比较归属
    "resolve_role",           # 解析调用者在该公司的角色
)
# 认定"同文件某函数是校验辅助"时只认主动校验词；光出现 owner_id 不够
# （序列化输出也可能带 owner_id），必须伴随 raise 才算真在校验。
_ACTIVE_CHECK = ("assert_company_owned", "assert_agent_owned",
                 "ensure_capability", "require_capability",
                 "selectable_company", "_owned_", "resolve_role")
_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


# --- 债务登记表：修一处删一处，禁止新增条目 ---------------------------------
# 每条 = "文件名 METHOD 完整路径  # 一句说明"。allowlist 只应越来越短。
_READ_DEBT_ALLOWLIST = frozenset({
    # app_alias：本机应用别名，越权会读到/拉起他人公司的应用
    # GET /app-alias/platform 只返回本机 OS 元信息、不读任何租户数据（静态扫描
    # 按参数名误报）。行为用例 test_app_alias_platform_carries_no_tenant_data
    # 钉住"响应与 company_id 无关"；其余端点均已在体内加 assert_company_owned。
    "app_alias.py GET /app-alias/platform",            # 误报：不读租户数据
    # automation：自动化规则
    # browser_agent：浏览器 Agent（已由 test_tenancy_read_attacks 行为覆盖）
    # collaboration：跨 Agent 协作消息（内容敏感）
    # command：一句话指挥台
    # experts：专家库（本次收口重点之一）
    # external_agents：外部 Agent 接入（已由 test_tenancy_read_attacks 行为覆盖）
    # factory：工厂 / 蓝图（按 job_id 的端点已补归属校验）
    # governance：治理与自主运行（急停/恢复类已收口，其余待补）
    # knowledge：知识库（创建/列表/详情/检索/RAG 已补归属校验）
    # memory：Agent 记忆（隐私）
    # reflection / review
    # sandbox：沙箱工作区（工作区/会话/动作已按反查公司校验）
    # schedules：定时任务
    # skill_growth / skills
    # tokens：用量与账单（已改为服务端推导可访问公司集合 / 校验传入公司）
    # training：训练材料（upload-material 经 _agent() 做了归属校验）
})  # 当前共 1 条（唯一剩余项是静态扫描误报，已有行为用例钉住）

# 可接受的债务上限：只减不增。本轮把 router 级 require_company_scope 接入
# 后，52 条真实越权读全部收口（含 15 个行为探针确认过的泄露），
# 只剩 1 条静态扫描误报。这个数只能继续往下调，不能放宽。
_READ_DEBT_TARGET = 1


def _router_prefix(tree: ast.Module) -> str:
    """取 router = APIRouter(prefix=...) 的前缀；无则空串。"""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Name) and t.id == "router"
                        and isinstance(node.value, ast.Call)):
                    for kw in node.value.keywords:
                        if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                            return str(kw.value.value)
    return ""


def _models_with_company_id(tree: ast.Module) -> set[str]:
    """列出定义了 company_id 字段的 Pydantic 模型名（body 可能夹带 company_id）。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for st in node.body:
                if (isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name)
                        and st.target.id == "company_id"):
                    names.add(node.name)
    return names


def _router_scopes_company(tree: ast.Module) -> bool:
    """router 是否挂了 router 级租户门禁 require_company_scope。

    这种写法一次覆盖整组端点（含以后新增的），扫描器必须认它，
    否则收口后登记表会永远删不掉对应条目。
    """
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "APIRouter"):
            for kw in node.value.keywords:
                if kw.arg == "dependencies":
                    return "require_company_scope" in ast.unparse(kw.value)
    return False


def _is_query_default(d) -> bool:
    return (isinstance(d, ast.Call) and isinstance(d.func, ast.Name)
            and d.func.id == "Query")


def _scan_route_source(name: str, src: str) -> set[str]:
    """扫描单个路由源码，返回未受归属守卫的 company_id 端点标识集合。

    标识 = f"{文件名} {METHOD} {完整路径}"。拆成纯函数便于反向自检测试。
    """
    tree = ast.parse(src)
    lines = src.splitlines()
    prefix = _router_prefix(tree)
    co_models = _models_with_company_id(tree)
    router_scoped = _router_scopes_company(tree)

    def body_of(node) -> str:
        return _strip_noise("\n".join(lines[node.lineno - 1:node.end_lineno]))

    # 一跳传递：先找出"体内确在校验归属"的本地辅助函数名
    guard_helpers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            b = body_of(node)
            if any(m in b for m in _ACTIVE_CHECK) or ("owner_id" in b and "raise" in b):
                guard_helpers.add(node.name)

    vios: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        method = rpath = None
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and isinstance(dec.func.value, ast.Name)
                    and dec.func.value.id == "router"
                    and dec.func.attr in _HTTP_METHODS):
                method = dec.func.attr.upper()
                if dec.args and isinstance(dec.args[0], ast.Constant):
                    rpath = str(dec.args[0].value)
        if method is None:
            continue
        full = (prefix + (rpath or "")) or "/"

        a = node.args
        pos_defaults = [None] * ((len(a.posonlyargs) + len(a.args))
                                 - len(a.defaults)) + list(a.defaults)
        takes_query = False
        takes_body = False
        for i, arg in enumerate(list(a.posonlyargs) + list(a.args)):
            d = pos_defaults[i]
            if arg.arg == "company_id" and d is not None and _is_query_default(d):
                takes_query = True
        for arg, d in zip(a.kwonlyargs, a.kw_defaults):
            if arg.arg == "company_id" and d is not None and _is_query_default(d):
                takes_query = True

        body = body_of(node)
        uses_co_attr = ".company_id" in body
        for arg in (list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)):
            ann = arg.annotation
            aname = (ann.id if isinstance(ann, ast.Name)
                     else ann.value if isinstance(ann, ast.Constant) else None)
            if aname in co_models and arg.arg != "company_id" and uses_co_attr:
                takes_body = True
        if not (takes_query or takes_body):
            continue

        guarded = any(m in body for m in READ_GUARD_MARKERS)
        if not guarded:
            guarded = any(h != node.name and f"{h}(" in body for h in guard_helpers)
        if not guarded and takes_query:
            # router 级门禁只覆盖 query/path 里的 company_id；body 夹带的
            # company_id 仍需端点自己校验（路由依赖看不到请求体）。
            guarded = router_scoped
        if not guarded:
            vios.add(f"{name} {method} {full}")
    return vios


def _company_id_endpoint_violations() -> tuple[set[str], list[str]]:
    violations: set[str] = set()
    skipped: list[str] = []
    for path in sorted(ROUTE_DIR.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        try:
            violations |= _scan_route_source(path.name, src)
        except SyntaxError:
            # 别的 agent 可能正把这个文件改到一半；语法错误由
            # test_every_module_parses 负责报错，本护栏绝不在解析层崩溃。
            skipped.append(path.name)
    return violations, skipped


def test_every_company_id_endpoint_has_an_ownership_guard():
    """读端点越权护栏（基线登记表式）：新增的、未登记的越权读端点 → 直接失败。

    这是债务登记表，不是免死金牌：
      * _READ_DEBT_ALLOWLIST 里每条都是已知的、尚未收口的越权读，附一句说明；
      * 修一处 → 从 allowlist 删一行；allowlist 只应越来越短；
      * 新增端点必须自带校验，**禁止往 allowlist 加条目来把它变绿**。

    M3 修正：老实现用 `assert violations` 证明"扫描器没空转"，后果是
    **债务清零那天门禁反而变红**（违反了登记表"只减不增"的语义）。
    现在改成：扫描器的有效性由反向用例
    test_read_guard_scanner_detects_violation_and_honors_local_helper 证明；
    这里则要求"登记表里的每条都仍然是真实违规"（stale 必须为空）——
    这同时兜住两类假绿：
      * 扫描器失效返回 0：所有登记条目都会变成 stale → 红；
      * 某人把入口修好了却懒得删登记 → 也红，强制登记表跟着缩小。
    """
    violations, skipped = _company_id_endpoint_violations()

    new = violations - _READ_DEBT_ALLOWLIST
    assert not new, (
        f"以下端点从 Query/body 取了 company_id 却没有任何归属校验，且不在债务登记表里"
        f"——这是新增的越权读，禁止直接合并。请就地补 assert_company_owned / "
        f"assert_company_capability / build_ctx 或 _owned_* 校验，"
        f"而不是把它加进 allowlist：\n  "
        + "\n  ".join(sorted(new))
    )

    stale = _READ_DEBT_ALLOWLIST - violations
    assert not stale, (
        "以下 allowlist 条目已不再被扫描为违规。这通常意味着入口已收口——"
        "请删掉这些条目；若一条都没变少却出现了大量 stale，则说明扫描器失效"
        "（空输入假绿）：\n  " + "\n  ".join(sorted(stale))
    )
    if skipped:
        print(f"\n[提示] 跳过语法错误文件（交由 test_every_module_parses 报错）：{skipped}")

    print(f"\n[越权读债务] 当前真实违规 {len(violations)} 处 / "
          f"登记表 {len(_READ_DEBT_ALLOWLIST)} 条。")


def test_read_debt_allowlist_only_shrinks():
    """登记表长度只能下降：新增一条都不允许。

    M1/M3：`assert violations` 式的"必须有债务"会把清零当失败；这里反过来
    盯住长度上限，逼着后来者用行为用例证明修复，而不是偷偷加白名单。
    """
    assert len(_READ_DEBT_ALLOWLIST) <= _READ_DEBT_TARGET, (
        f"越权读债务登记表从 {_READ_DEBT_TARGET} 条涨到了 "
        f"{len(_READ_DEBT_ALLOWLIST)} 条——禁止新增，请补归属校验。"
    )


def test_read_guard_scanner_detects_violation_and_honors_local_helper():
    """反向验证读扫描器本身：一段该报警、一段不该报警——两头都要对。

    只验证"能报警"会漏掉另一类失效：把已下沉校验到 _owned 辅助的端点也当违规，
    那样 allowlist 会塞满假债务、登记表就不可信了。
    """
    sample = (
        'from fastapi import APIRouter, Depends, Query\n'
        'router = APIRouter(prefix="/w")\n'
        '\n'
        '@router.get("/bad")\n'
        'async def bad(company_id: str = Query(...), db=Depends(get_db), '
        'user=Depends(get_current_user)):\n'
        '    return (await db.execute(select(T).where(T.company_id == company_id))).all()\n'
        '\n'
        '@router.get("/safe")\n'
        'async def safe(company_id: str = Query(...), db=Depends(get_db), '
        'user=Depends(get_current_user)):\n'
        '    await _owned(db, user.id, company_id)\n'
        '    return {"ok": True}\n'
        '\n'
        'async def _owned(db, user_id, company_id):\n'
        '    c = await db.get(Company, company_id)\n'
        '    if not c or c.owner_id != user_id:\n'
        '        raise HTTPException(404, "no")\n'
        '    return c\n'
    )
    vios = _scan_route_source("w.py", sample)
    assert vios == {"w.py GET /w/bad"}, (
        f"读扫描器判定有误：/bad 应报警、/safe 应因调用本地 _owned 校验而放行。实得 {vios}"
    )


# ---------------------------------------------------------------------------
# 行为层：跨租户调用必须被拒
# ---------------------------------------------------------------------------

async def test_company_delete_cross_tenant(client, tenants):
    r = await client.delete(f"/companies/{tenants.bob_co['id']}",
                            headers=tenants.alice["headers"])
    assert r.status_code == 404, f"越权删除公司竟然成功：{r.status_code}"


async def test_department_delete_cross_tenant(client, tenants):
    r = await client.post("/departments",
                          params={"cid": tenants.bob_co["id"]},
                          json={"name": "B 部门"},
                          headers=tenants.bob["headers"])
    assert r.status_code == 201, r.text
    did = r.json()["id"]

    r = await client.delete(f"/departments/{did}",
                            headers=tenants.alice["headers"])
    assert r.status_code == 404


async def test_agent_delete_cross_tenant(client, tenants):
    r = await client.post("/agents", json={
        "company_id": tenants.bob_co["id"], "name": "B 的员工"},
        headers=tenants.bob["headers"])
    assert r.status_code == 201, r.text
    aid = r.json()["id"]

    r = await client.delete(f"/agents/{aid}", headers=tenants.alice["headers"])
    assert r.status_code == 404


async def test_project_delete_cross_tenant(client, tenants):
    r = await client.post("/projects", json={
        "company_id": tenants.bob_co["id"], "name": "B 的项目"},
        headers=tenants.bob["headers"])
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    r = await client.delete(f"/projects/{pid}", headers=tenants.alice["headers"])
    assert r.status_code == 404


async def test_emergency_stop_cross_tenant(client, tenants):
    """急停是最典型的"影响他人"接口：原先既不校验归属，还会跨公司生效。

    M3 修正：老用例打的是 `/automation/emergency-stop`——后端根本没有这个
    路由（只有 `/automations/…`），404 恒真，和归属校验毫无关系。现在先
    用 OpenAPI 断言路由真实存在，再发请求，避免"路由写错也算通过"。
    """
    from app.main import app as asgi_app

    openapi_paths = asgi_app.openapi()["paths"]
    for path in ("/automations/emergency-stop", "/governance/emergency-stop",
                 "/governance/emergency-resume"):
        assert path in openapi_paths, f"用例打的是不存在的路由：{path}（404 假绿）"
        r = await client.post(path,
                              params={"company_id": tenants.bob_co["id"]},
                              headers=tenants.alice["headers"])
        assert r.status_code == 404, \
            f"{path} 允许越权调用他人公司：{r.status_code}"


# ---------------------------------------------------------------------------
# 模型配置的归属（本次修复的核心漏洞）
# ---------------------------------------------------------------------------

_MODEL_BODY = {
    "name": "归属测试模型",
    "provider_type": "openai_compat",
    "base_url": "http://127.0.0.1:9/v1",
    "api_key_env": "NOT_A_REAL_KEY_ENV",
    "model_id": "x",
}


async def _make_model(client, user, *, company_id: str | None = None) -> str:
    body = dict(_MODEL_BODY)
    if company_id:
        body["company_id"] = company_id
    r = await client.post("/models", json=body, headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_model_list_is_tenant_scoped(client, tenants):
    """A 的列表里不能出现 B 的模型配置（含 base_url 与密钥变量名）。"""
    b_model = await _make_model(client, tenants.bob,
                                company_id=tenants.bob_co["id"])

    r = await client.get("/models", headers=tenants.alice["headers"])
    ids = [m["id"] for m in r.json()]
    assert b_model not in ids, "跨租户泄露了模型配置"


async def test_model_cannot_be_created_under_other_company(client, tenants):
    r = await client.post("/models", json={
        **_MODEL_BODY, "company_id": tenants.bob_co["id"]},
        headers=tenants.alice["headers"])
    assert r.status_code == 404


async def test_model_update_and_delete_cross_tenant(client, tenants):
    b_model = await _make_model(client, tenants.bob,
                                company_id=tenants.bob_co["id"])

    r = await client.patch(f"/models/{b_model}", json={"name": "被改名"},
                           headers=tenants.alice["headers"])
    assert r.status_code == 404, "越权改模型配置竟然成功"

    r = await client.delete(f"/models/{b_model}",
                            headers=tenants.alice["headers"])
    assert r.status_code == 404, "越权删模型配置竟然成功"


async def test_unowned_model_can_be_claimed_but_not_silently_deleted(client,
                                                                    make_user,
                                                                    make_company):
    """未归属（升级前遗留）的配置：只能认领，不能直接删。

    这是"不破坏升级前安装"与"关闭越权口子"之间的折中：
    无主配置可能正被别人公司的员工使用，直接删掉会造成看不见的破坏。
    """
    user = await make_user()
    co = await make_company(user)
    mid = await _make_model(client, user, company_id=None)

    listed = (await client.get("/models", headers=user["headers"])).json()
    row = [m for m in listed if m["id"] == mid][0]
    assert row["unowned"] is True, "未归属的配置必须被标记出来"

    r = await client.delete(f"/models/{mid}", headers=user["headers"])
    assert r.status_code == 409, "未归属配置应要求先认领，而不是直接删除"

    r = await client.patch(f"/models/{mid}", json={"company_id": co["id"]},
                           headers=user["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["claimed"] is True

    r = await client.delete(f"/models/{mid}", headers=user["headers"])
    assert r.status_code == 204, "认领后应能正常删除"


# ---------------------------------------------------------------------------
# 矩阵本身
# ---------------------------------------------------------------------------

def test_role_matrix_semantics():
    """矩阵语义：越权的不该被悄悄允许。"""
    from app.core.permissions import Capability, Role, capabilities_of, role_has

    assert capabilities_of(Role.OWNER) == frozenset(Capability), "所有者应具备全部能力"
    assert not role_has(Role.VIEWER, Capability.OPERATE), "只读角色不能写"
    assert not role_has(Role.VIEWER, Capability.DESTRUCTIVE)
    assert not role_has(Role.MEMBER, Capability.DESTRUCTIVE), "普通成员不能删公司"
    assert role_has(Role.MEMBER, Capability.OPERATE)
    assert role_has(Role.ADMIN, Capability.DESTRUCTIVE)
    assert not role_has(Role.ADMIN, Capability.ADMIN), "管理员不该拥有所有权级能力"
    assert not role_has(None, Capability.VIEW), "无角色必须什么都不行"


async def test_access_matrix_endpoint_requires_login(client):
    r = await client.get("/access/matrix")
    assert r.status_code in (401, 403), "权限矩阵不应匿名可读"


async def test_access_matrix_is_exposed_for_frontend(client, make_user):
    """前端要据此禁用/隐藏操作，因此必须能读到自己角色的能力集合。"""
    user = await make_user()
    r = await client.get("/access/matrix", headers=user["headers"])
    assert r.status_code == 200, r.text
    rows = {row["role"]: row["capabilities"] for row in r.json()["matrix"]}
    assert set(rows) == {"owner", "admin", "member", "viewer"}
    assert "destructive" in rows["owner"]
    assert rows["viewer"] == ["view"]


async def test_resolve_role_returns_none_for_foreign_company(client, tenants):
    from app.db.session import AsyncSessionLocal
    from app.core.permissions import resolve_role

    async with AsyncSessionLocal() as db:
        assert await resolve_role(db, tenants.alice["id"],
                                  tenants.bob_co["id"]) is None
        assert await resolve_role(db, tenants.alice["id"],
                                  tenants.alice_co["id"]) is not None
        assert await resolve_role(db, tenants.alice["id"], None) is None


# ---------------------------------------------------------------------------
# 静态护栏扫出来的那一批：逐个用行为验证兜住
# ---------------------------------------------------------------------------

async def test_billing_config_delete_cross_tenant(client, tenants):
    """计费标准挂在模型上：B 的模型 → B 的计费标准 → A 不能删。

    回归守卫：`DELETE /tokens/pricing/{id}` 原先按 id 直接删，
    任何登录用户都能抹掉别人公司的计费标准，成本口径随之失真。
    """
    b_model = await _make_model(client, tenants.bob,
                                company_id=tenants.bob_co["id"])
    r = await client.post("/tokens/pricing",
                          json={"model_id": b_model, "billing_mode": "USAGE",
                                "input_price_per_m": 1.0},
                          headers=tenants.bob["headers"])
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    r = await client.delete(f"/tokens/pricing/{pid}",
                            headers=tenants.alice["headers"])
    assert r.status_code == 404, "越权删除他人计费标准竟然成功"

    r = await client.delete(f"/tokens/pricing/{pid}",
                            headers=tenants.bob["headers"])
    assert r.status_code == 200, "所有者应能删除自己的计费标准"


async def test_knowledge_document_delete_cross_tenant(client, tenants):
    """知识库文档：B 上传的文档，A 不能删。

    回归守卫：`DELETE /kb2/documents/{id}` 与 `/knowledge/documents/{id}`
    原先都只按 id 取文档就删，没有归属校验。

    M3 修正：老用例调 `POST /kb2/documents` —— 该路由不存在（只有
    `/kb2/global/documents` 与 `/knowledge/documents`），于是永久 skip，
    成了全套件唯一的"1 skipped"。现在用真实创建接口，且创建失败直接红。
    """
    r = await client.post(
        f"/knowledge/documents?company_id={tenants.bob_co['id']}",
        json={"title": "B 的知识库文档", "content": "机密内容"},
        headers=tenants.bob["headers"])
    assert r.status_code == 200, f"B 建不了自己的知识库文档：{r.text[:160]}"
    doc_id = r.json()["id"]

    for path in (f"/kb2/documents/{doc_id}",
                 f"/knowledge/documents/{doc_id}"):
        r = await client.delete(path, headers=tenants.alice["headers"])
        assert r.status_code == 404, f"{path} 越权删除他人知识库文档竟然成功"

    # 属主仍然能删：防止"用不可用换安全"
    r = await client.delete(f"/knowledge/documents/{doc_id}",
                            headers=tenants.bob["headers"])
    assert r.status_code == 200, f"所有者删不了自己的文档：{r.text[:160]}"
