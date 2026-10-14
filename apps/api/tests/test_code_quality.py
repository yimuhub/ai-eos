"""代码质量护栏：把两类"长期隐身"的缺陷钉死在自动化里。

## 为什么这两类

改造前的 9 处 `NameError`（如 `agent_runtime.py` 用了未导入的 `AssetType`）
和 24 处 `except Exception: pass` 共同构成一个恶性组合：

- 未定义变量让整块功能**永远不执行**；
- 静默捕获让这次失败**不留任何痕迹**。

两者叠加的结果是：接口返回 200、页面显示成功、功能从未生效。
这类缺陷不会被"能启动/能编译/能打开"任何一道关卡拦住，
只有静态分析 + 显式规则能拦。

## 关于阈值

阈值不是"零容忍洁癖"，而是本项目的既定纪律：
新增代码请一并满足；若确有正当理由需要静默捕获，
请在 `# noqa: silent-except` 上写明原因，而不是降低整个门禁。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

API_ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_DIR = API_ROOT / "app"

# pyflakes 的"未定义名"是最接近线上事故的一档问题，必须为零
_FATAL_PYFLAKES = re.compile(r"undefined name|syntax error|syntaxerror", re.I)

# 允许静默捕获的显式豁免标记
_SILENT_EXEMPT_MARKER = "noqa: silent-except"


def _python_files() -> list[pathlib.Path]:
    return sorted(p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def test_every_module_parses():
    """语法层面：任何一个文件写错，都应该在测试里第一时间暴露。"""
    bad = []
    for path in _python_files():
        try:
            ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            bad.append(f"{path.relative_to(API_ROOT)}:{exc.lineno} {exc.msg}")
    assert not bad, "存在语法错误：\n" + "\n".join(bad)


def test_no_undefined_names():
    """回归护栏：9 处未定义变量曾让整块功能静默失效。

    用 pyflakes 而不是"肉眼看"——后者在 2 万行代码上不可持续。
    """
    pyflakes = pytest.importorskip(
        "pyflakes", reason="未安装 pyflakes（dev 依赖），跳过静态检查")
    from pyflakes.api import checkPath
    from pyflakes.reporter import Reporter

    import io

    out, err = io.StringIO(), io.StringIO()
    reporter = Reporter(out, err)
    for path in _python_files():
        checkPath(str(path), reporter)

    messages = (out.getvalue() + err.getvalue()).splitlines()
    fatal = [m for m in messages if _FATAL_PYFLAKES.search(m)]
    assert not fatal, ("存在未定义名/语法错误（这类问题会让整块功能静默失效）：\n"
                       + "\n".join(fatal))


def _is_docstring(node: ast.stmt) -> bool:
    """字符串表达式语句（docstring 位）——语义上不是"处理逻辑"。"""
    return (isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str))


def _handler_source(handler: ast.ExceptHandler, lines: list[str]) -> str:
    """取 except 处理器的源码文本（用于查找豁免标记）。"""
    start = handler.lineno - 1
    end = getattr(handler, "end_lineno", None) or handler.lineno
    return "\n".join(lines[start:end])


_LOGGING_ATTRS = {"debug", "info", "warning", "error", "exception",
                  "critical", "log"}


def _handler_logs(handler: ast.ExceptHandler) -> bool:
    """处理器内是否有 logging 调用（logger.xxx / logging.xxx）。"""
    for node in ast.walk(handler):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _LOGGING_ATTRS:
            owner = func.value
            if isinstance(owner, ast.Name) and "log" in owner.id.lower():
                return True
    return False


def _silent_exception_sites() -> list[str]:
    """AST 级检测：`except` 处理器的 body 仅为 `pass` 且无 logging 调用即违规。

    升级原因：老实现逐行比对 `lines[cursor].strip() == "pass"`，
    行尾带注释的 `pass  # xxx` 直接逃逸（真实实例见 agent_runtime.py，
    注释写着"记忆召回失败不阻塞任务"却没有任何日志）。
    AST 只看语义不看注释文本——注释再也藏不住 `pass`。
    """
    hits: list[str] = []
    for path in _python_files():
        lines = path.read_text(encoding="utf-8").splitlines()
        try:
            tree = ast.parse("\n".join(lines))
        except SyntaxError:
            continue  # 语法错误由 test_every_module_parses 负责报告
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            body = [n for n in node.body if not _is_docstring(n)]
            if len(body) != 1 or not isinstance(body[0], ast.Pass):
                continue
            if _handler_logs(node):
                continue
            if _SILENT_EXEMPT_MARKER in _handler_source(node, lines):
                continue
            hits.append(f"{path.relative_to(API_ROOT)}:{node.lineno}")
    return hits


def test_no_silent_exception_swallowing():
    """失败必须出声：不允许 `except: pass` 把异常整个吞掉。

    降级仍然是允许的（记忆召回失败不该阻断任务），
    但必须落一条日志——否则"功能没生效"永远查不出来。
    """
    hits = _silent_exception_sites()
    assert not hits, (
        "存在静默吞掉异常的代码（会让功能失效且无任何痕迹）：\n  "
        + "\n  ".join(hits)
        + "\n\n请改为 logger.warning/error 记录；若确属有意为之，"
          "请在该处注明 `# noqa: silent-except` 并写明理由。"
    )


# ============================================================ 基础设施在位

@pytest.mark.parametrize("relative_path,why", [
    ("app/core/logging_config.py", "集中日志：失败出声的载体"),
    ("app/core/env_loader.py", "环境变量注入：修掉'密钥读成空串'"),
    ("app/core/sandbox.py", "文件访问沙箱：阻断任意文件读取"),
    ("app/services/computer_service.py", "本机能力权限门禁的唯一实现"),
])
def test_required_infrastructure_present(relative_path, why):
    assert (API_ROOT / relative_path).is_file(), f"缺少基础设施：{relative_path}（{why}）"


def test_logging_is_configured_on_import():
    """导入 app.main 时就必须配好日志，不能等第一次 logger 调用。"""
    import logging

    import app.main  # noqa: F401

    root = logging.getLogger()
    assert root.handlers, "根 logger 没有 handler，日志会丢失"
    assert root.level != logging.NOTSET


def test_global_exception_handler_registered():
    """未捕获异常必须有兜底：进日志 + 返回结构化 JSON。"""
    from app.main import app as asgi_app

    assert Exception in asgi_app.exception_handlers, "未注册全局异常处理器"


def test_env_loader_does_not_override_real_env():
    """进程环境变量优先级必须高于 .env，否则容器/CI 注入会被本地文件覆盖。"""
    import os

    from app.core.env_loader import load_env

    os.environ["AIEOS_TEST_PROBE_KEY"] = "from-process"
    load_env(override=False)
    assert os.environ["AIEOS_TEST_PROBE_KEY"] == "from-process"
    os.environ.pop("AIEOS_TEST_PROBE_KEY", None)


def test_provider_key_resolution_uses_unified_entry():
    """密钥读取必须走 settings.resolved_api_key，不能再散落 os.environ.get。"""
    import os

    from app.core.config import settings

    os.environ["AIEOS_TEST_KEY_ENV"] = "secret-value"
    try:
        assert settings.resolved_api_key("AIEOS_TEST_KEY_ENV") == "secret-value"
        # 环境变量名给了但取不到 → 回退到 provider 默认键
        assert settings.resolved_api_key("AIEOS_MISSING_KEY_ENV",
                                         provider="gemini") == settings.gemini_api_key
    finally:
        os.environ.pop("AIEOS_TEST_KEY_ENV", None)



# ---------------------------------------------------------------------------
# 生产环境安全姿态：占位密钥 / debug 未关 → 必须拒绝启动
#
# 这几条守的是一个具体事故：部署时忘了配 JWT_SECRET，代码里那个公开的
# 占位值就成了实际签名密钥 —— 任何人都能伪造任意用户的 token。
# 目的是把"忘记配置"从"静默失守"变成"显式失败"。
#
# 注意：构造时传 `_env_file=None`，否则仓库里的 apps/api/.env 会先一步
# 填上值，测试就测不到"没配"这条路径了（这正是本文件第一次写错的地方）。
# ---------------------------------------------------------------------------

_PLACEHOLDER_SECRET = "aieos-portable-secret"


def _build_settings(monkeypatch, **env):
    """在隔离环境下构造 Settings：屏蔽 .env 文件，只认显式给的变量。"""
    keys = ("AIEOS_ENV", "JWT_SECRET", "DEBUG", "JWT_EXPIRE_MINUTES",
            "DATABASE_URL", "OPENAI_COMPAT_API_KEY")
    for k in keys:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    import app.core.config as cfg

    # _env_file=None 关掉 .env 加载；不这样做，仓库里的 .env 会掩盖测试意图
    return cfg.Settings(_env_file=None)


def test_production_rejects_placeholder_jwt_secret(monkeypatch):
    """生产环境 + 占位密钥 → 拒绝启动（否则任何人可伪造 token）。"""
    import pytest

    with pytest.raises(Exception) as exc:
        _build_settings(monkeypatch, AIEOS_ENV="production",
                        JWT_SECRET=_PLACEHOLDER_SECRET, DEBUG="false")
    assert "JWT_SECRET" in str(exc.value)


def test_production_rejects_debug_true(monkeypatch):
    """生产环境 + DEBUG=true → 拒绝启动（暴露堆栈、可能开放测试台）。"""
    import pytest

    with pytest.raises(Exception) as exc:
        _build_settings(monkeypatch, AIEOS_ENV="production",
                        JWT_SECRET="x" * 64, DEBUG="true")
    assert "DEBUG" in str(exc.value)


def test_production_accepts_hardened_config(monkeypatch):
    """生产环境 + 强密钥 + debug 关闭 → 放行，且不再有告警。"""
    s = _build_settings(monkeypatch, AIEOS_ENV="production",
                        JWT_SECRET="a" * 64, DEBUG="false")
    assert s.is_production is True
    assert s.using_default_jwt_secret is False
    assert s.security_warnings() == [], s.security_warnings()


def test_development_starts_but_warns(monkeypatch):
    """开发环境必须放行（否则"克隆下来就能跑"不成立），但要给出告警。"""
    s = _build_settings(monkeypatch, AIEOS_ENV="development")
    assert s.is_production is False
    assert s.using_default_jwt_secret is True
    warnings = s.security_warnings()
    assert any("JWT_SECRET" in w for w in warnings), f"默认密钥未告警：{warnings}"
    assert any("DEBUG" in w for w in warnings), f"debug 未告警：{warnings}"


# ---------------------------------------------------------------------------
# 前端体验护栏
#
# 这一节守的是"用起来顺不顺"这类**不会被编译器和 lint 拦住**的问题。
# ESLint 不会因为"用了 window.confirm"报错，TypeScript 也不会因为
# "新页面没进导航"报错，但它们会实实在在地让产品显得不专业：
#
#   - 浏览器原生 confirm 可以被用户在设置里关掉，之后恒返回 true，
#     "二次确认"直接退化成一次点击就执行破坏性操作；
#   - 原生 alert 会阻塞主线程、样式无法定制，在深色产品里像系统故障；
#   - 一个没人能点到的新页面 = 白写。
#
# 所以把这三条写成测试。规则本身很简单，价值在于**它们不会随时间腐化**。
# ---------------------------------------------------------------------------

WEB_ROOT = API_ROOT.parent / "web"


def _web_sources() -> list[pathlib.Path]:
    """前端自有源码（排除 node_modules / 构建产物 / 类型声明）。"""
    roots = [WEB_ROOT / "app", WEB_ROOT / "components", WEB_ROOT / "lib"]
    files: list[pathlib.Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.suffix in (".ts", ".tsx") and "node_modules" not in p.parts and ".next" not in p.parts:
                files.append(p)
    return sorted(files)


def _strip_comments(src: str) -> str:
    """去掉注释，避免把"文档里提到 alert() 的说明文字"误判成真实调用。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)^\s*//.*$", "", src)
    return src


def test_no_native_blocking_dialogs():
    """禁止 window.confirm / alert / prompt。

    统一走 `confirmAction()`（主题化、可列出影响面、不可逆操作要求输入确认词）
    与 `notify()`（非阻塞、可堆叠、带 role=alert）。
    """
    # (?<![A-Za-z0-9_]) 让 confirmAction( 不会被当成 confirm(
    pattern = re.compile(r"(?<![A-Za-z0-9_$])(confirm|alert|prompt)\s*\(")
    hits: list[str] = []
    for path in _web_sources():
        body = _strip_comments(path.read_text(encoding="utf-8"))
        for idx, line in enumerate(body.splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(WEB_ROOT)}:{idx}  {line.strip()[:90]}")
    assert not hits, (
        "前端仍在使用原生阻塞式弹窗（确认框可被用户关闭，等于没有二次确认）：\n  "
        + "\n  ".join(hits)
        + "\n\n请改用 components/ConfirmDialog.tsx 的 confirmAction() 与 components/Toast 的 notify()。"
    )


def test_required_frontend_ux_infrastructure_present():
    """统一确认框 / 全局跳转 / 校验规则 / 字段容器，缺一个都会让体验退回改造前。"""
    for rel, why in [
        ("components/ConfirmDialog.tsx", "统一二次确认（替代可被关闭的原生 confirm）"),
        ("components/CommandPalette.tsx", "⌘K 全局跳转（页面越多越必要）"),
        ("components/Field.tsx", "字段容器：标签 / 必填 / 内联错误"),
        ("lib/nav.ts", "导航单一事实源（侧边栏与命令面板共用）"),
        ("lib/validate.ts", "统一校验规则"),
        ("lib/useFormValidation.ts", "表单校验状态管理"),
    ]:
        assert (WEB_ROOT / rel).is_file(), f"缺少 {rel}（{why}）"


def _nav_hrefs() -> set[str]:
    """从 lib/nav.ts 里取出所有导航目标。"""
    src = (WEB_ROOT / "lib" / "nav.ts").read_text(encoding="utf-8")
    return set(re.findall(r'href:\s*"([^"]+)"', src))


def _frontend_pages() -> set[str]:
    """实际存在的可访问页面路由（排除动态路由 `[id]`）。"""
    routes: set[str] = set()
    main = WEB_ROOT / "app" / "(main)"
    for page in main.rglob("page.tsx"):
        rel = page.parent.relative_to(main)
        if any(seg.startswith("[") for seg in rel.parts):
            continue  # 详情页需要具体 id，天然不在导航里
        routes.add("/" if str(rel) == "." else "/" + str(rel).replace("\\", "/"))
    return routes


def test_navigation_covers_every_page():
    """每个页面都必须能从导航或 ⌘K 到达。

    这条守的是一个很常见的退化：新加了页面但忘了挂入口，
    结果功能存在、却没人找得到（改造前 34 个页面平铺在侧边栏，
    反过来又有页面永远点不到）。
    """
    missing = _frontend_pages() - _nav_hrefs()
    assert not missing, (
        "以下页面既不在侧边栏、也不在 ⌘K 可搜范围内（等于没有入口）：\n  "
        + "\n  ".join(sorted(missing))
        + "\n\n请在 apps/web/lib/nav.ts 中登记（若属内部页面，放入 HIDDEN_ITEMS）。"
    )


def test_navigation_has_no_dead_links():
    """导航里登记的目标必须真实存在，避免点进去 404。"""
    dead = _nav_hrefs() - _frontend_pages()
    assert not dead, (
        "导航指向了不存在的页面：\n  " + "\n  ".join(sorted(dead))
        + "\n\n页面被重命名或删除时，请同步更新 apps/web/lib/nav.ts。"
    )


def test_nav_items_have_description_and_keywords():
    """每一条导航都必须带说明与检索词。

    说明是给新用户的教学材料，检索词决定他能不能用自己习惯的说法找到页面；
    少了任何一个，⌘K 的价值就只剩"精确知道名字才搜得到"。
    """
    src = (WEB_ROOT / "lib" / "nav.ts").read_text(encoding="utf-8")
    entries = re.findall(r'\{\s*href:\s*"[^"]+",\s*label:\s*"[^"]+",\s*icon:\s*"[^"]*",\s*desc:', src)
    total = len(re.findall(r'href:\s*"/', src))
    assert total > 0, "未解析到任何导航项（lib/nav.ts 结构可能已变更）"
    assert len(entries) == total, (
        f"有 {total - len(entries)} 条导航缺少 desc（命令面板将无法向用户说明这个页面能做什么）"
    )
    assert src.count("keywords:") >= total - 1, "导航项应尽量都带检索关键词"

