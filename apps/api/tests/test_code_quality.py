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
