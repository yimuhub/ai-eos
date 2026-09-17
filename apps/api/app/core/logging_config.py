"""集中式日志配置。

设计目标（对应"让每个失败都出声"原则）：
1. 应用内所有模块通过 logging.getLogger(__name__) 输出，统一格式与级别。
2. 默认级别 INFO；通过环境变量 AIEOS_LOG_LEVEL 可调（DEBUG/INFO/WARNING/ERROR）。
3. 生产环境（AIEOS_ENV=production）默认只输出 WARNING 以上，避免刷屏；
   开发环境输出 INFO，便于观察智能体执行链路。
4. 第三方库（uvicorn/asyncio/sqlalchemy.engine）降噪到 WARNING，
   避免掩盖业务日志。

注意：本模块只负责配置，不负责业务逻辑。调用 setup_logging() 应尽早执行
（main.py 导入期），确保后续模块的 logger 都被覆盖。
"""
from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False

_LOG_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
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


def setup_logging(force: bool = False) -> None:
    """配置根 logger。幂等；force=True 可强制重配（测试用）。"""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))

    root = logging.getLogger()
    # 清掉可能已存在的 handler，避免重复输出
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(_resolve_level())

    for name, level in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(level)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """业务模块统一入口。首次调用会自动完成配置，避免漏配时丢日志。"""
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name)
