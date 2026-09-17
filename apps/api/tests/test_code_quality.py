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


def _silent_exception_sites() -> list[str]:
    """找出 `except ...:` 之后紧跟 `pass`（中间只有注释）的位置。"""
    hits: list[str] = []
    for path in _python_files():
        lines = path.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines):
            if not re.match(r"^\s*except\b.*:\s*$", line):
                # 同行写法 `except X: pass`
                if re.match(r"^\s*except\b.*:\s*pass\s*(#.*)?$", line):
                    if _SILENT_EXEMPT_MARKER not in line:
                        hits.append(f"{path.relative_to(API_ROOT)}:{idx + 1}")
                continue
            cursor = idx + 1
            while cursor < len(lines) and not lines[cursor].strip():
                cursor += 1
            if cursor < len(lines) and lines[cursor].strip() == "pass":
                context = "\n".join(lines[idx:cursor + 1])
                if _SILENT_EXEMPT_MARKER not in context:
                    hits.append(f"{path.relative_to(API_ROOT)}:{idx + 1}")
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
