"""集中式日志配置。

设计目标（对应"让每个失败都出声"原则）：
1. 应用内所有模块通过 logging.getLogger(__name__) 输出，统一格式与级别。
2. 默认级别 INFO；通过环境变量 AIEOS_LOG_LEVEL 可调（DEBUG/INFO/WARNING/ERROR）。
3. 生产环境（AIEOS_ENV=production）默认只输出 WARNING 以上，避免刷屏；
   开发环境输出 INFO，便于观察智能体执行链路。
4. 第三方库（uvicorn/asyncio/sqlalchemy.engine）降噪到 WARNING，
   避免掩盖业务日志。
5. stdout 之外再写一份按日轮转的日志文件（TimedRotatingFileHandler，
   保留 30 天），目录由 AIEOS_LOG_DIR 配置，默认 logs/。
   运维排障不再依赖"启动器重定向的日志"。
6. 每条日志带 request_id（来自 app.core.request_context 的 contextvar），
   用户报错时可以用响应头 X-Request-ID 直接 grep 到整条链路；
   没有请求上下文时显示 "-"，不会报错。

注意：本模块只负责配置，不负责业务逻辑。调用 setup_logging() 应尽早执行
（main.py 导入期），确保后续模块的 logger 都被覆盖。
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from app.core.request_context import DEFAULT_REQUEST_ID, get_request_id

_CONFIGURED = False

# request_id 放在级别之后、logger 名之前：既不打乱既有的阅读顺序，
# 又能让同一次请求的多条日志一眼串起来（无请求上下文时显示 "-"）。
_LOG_FORMAT = ("%(asctime)s [%(levelname)-7s] [%(request_id)s] "
               "%(name)s: %(message)s")
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 这些库默认日志过于啰嗦，压到 WARNING，避免把业务日志顶出视野
_NOISY_LOGGERS = {
    "uvicorn.access": logging.WARNING,
    "asyncio": logging.WARNING,
    "sqlalchemy.engine": logging.WARNING,
    "sqlalchemy.pool": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "openai": logging.WARNING,
    "urllib3": logging.WARNING,
}


class RequestIdFilter(logging.Filter):
    """把当前请求上下文里的 request_id 注入每条 LogRecord。

    放在 handler 上而不是 logger 上：根 logger 的 handler 覆盖全部模块，
    第三方库的 logger 也不会有漏网的记录。没有请求上下文（启动期、后台
    线程、定时任务）时填 "-"，保证 `%(request_id)s` 永远不会 KeyError。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or DEFAULT_REQUEST_ID
        return True


def _make_formatter() -> logging.Formatter:
    """统一 formatter；defaults 兜底，即使记录绕过了 Filter 也不会炸。"""
    return logging.Formatter(
        _LOG_FORMAT, datefmt=_DATE_FORMAT,
        defaults={"request_id": DEFAULT_REQUEST_ID},
    )


def _resolve_level() -> int:
    raw = os.getenv("AIEOS_LOG_LEVEL", "").strip().upper()
    if raw:
        level = logging.getLevelName(raw)
        if isinstance(level, int):
            return level
        # 非法值不静默吞掉，明确提示后回退
        print(f"[logging] 无法识别的 AIEOS_LOG_LEVEL={raw!r}，回退默认级别",
              file=sys.stderr)
    env = os.getenv("AIEOS_ENV", "development").strip().lower()
    return logging.WARNING if env == "production" else logging.INFO


def _resolve_log_dir() -> Path | None:
    """日志文件目录：AIEOS_LOG_DIR 可配（建议绝对路径），默认 ./logs。

    相对路径以启动时的工作目录为基准。目录建不出来就退回纯 stdout，
    但要大声说出来，不能静默。
    """
    raw = os.getenv("AIEOS_LOG_DIR", "").strip()
    log_dir = Path(raw) if raw else Path("logs")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir
    except OSError as e:
        print(f"[logging] 无法创建日志目录 {log_dir}（{e}），仅输出到 stdout",
              file=sys.stderr)
        return None


def _make_file_handler() -> logging.Handler | None:
    """按日轮转的文件 handler（ai-eos.log，保留 30 天）。"""
    log_dir = _resolve_log_dir()
    if log_dir is None:
        return None
    handler = TimedRotatingFileHandler(
        str(log_dir / "ai-eos.log"),
        when="midnight", interval=1, backupCount=30, encoding="utf-8",
    )
    handler.setFormatter(_make_formatter())
    handler.addFilter(RequestIdFilter())
    return handler


def setup_logging(force: bool = False) -> None:
    """配置根 logger。幂等；force=True 可强制重配（测试用）。"""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    formatter = _make_formatter()

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    # 清掉可能已存在的 handler，避免重复输出；文件 handler 先 close 释放句柄
    for existing in list(root.handlers):
        root.removeHandler(existing)
        try:
            existing.close()
        except Exception as e:
            # 关闭旧 handler 失败不能静默吞掉：此时 logger 正在重配，打到 stderr
            print(f"[logging] 关闭旧 handler 失败：{e}", file=sys.stderr)
    root.addHandler(stdout_handler)

    # 按日轮转的文件日志：与 stdout 并存
    file_handler = _make_file_handler()
    if file_handler is not None:
        root.addHandler(file_handler)

    root.setLevel(_resolve_level())

    for name, level in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(level)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """业务模块统一入口。首次调用会自动完成配置，避免漏配时丢日志。"""
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name)
