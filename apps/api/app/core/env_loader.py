"""环境变量加载：把 .env 真实注入 os.environ。

## 为什么需要这个模块

改造前项目存在一个静默故障链：
- `core/config.py` 用 pydantic-settings 的 `env_file=".env"`，看起来"读了 .env"；
- 但 `model_gateway._resolve_api_key()` 读的是 `os.environ.get(...)`；
- pydantic-settings 只把值装进 `Settings` 对象，**从不写入 os.environ**；
- 结果：只在 .env 里配密钥 → os.environ 里没有 → 取到空串 → 调用模型 401。

而且 `env_file=".env"` 是相对当前工作目录解析的：
从仓库根目录启动就找不到 apps/api/.env，从 apps/api 启动才找得到。

本模块用**文件自身位置**推导 .env 路径，与启动目录无关，并把结果写入
os.environ，使两条读取路径（Settings 与 os.environ）看到同一份配置。

## 使用方式

在应用入口最早期调用一次（main.py 导入期），后续所有模块无论用
`settings.xxx` 还是 `os.environ[...]` 都能取到同一个值。
"""
from __future__ import annotations

import os
from pathlib import Path

# app/core/env_loader.py → app/core → app → apps/api
API_ROOT = Path(__file__).resolve().parent.parent.parent

# 候选 .env 位置，按优先级排列（后者不覆盖前者已设置的值）
_CANDIDATE_ENV_FILES = (
    API_ROOT / ".env",            # apps/api/.env
    API_ROOT.parent.parent / ".env",  # 仓库根 .env
)


def load_env(override: bool = False) -> list[str]:
    """把 .env 载入 os.environ。返回实际加载的文件路径列表。

    override=False 时不会覆盖已存在的真实环境变量——这是有意为之：
    容器/CI 注入的环境变量优先级高于本地 .env 文件。

    依赖缺失或文件不存在都不抛异常（便携包允许纯环境变量运行），
    但会通过返回值让调用方知道"到底加载了没有"。
    """
    loaded: list[str] = []
    try:
        from dotenv import load_dotenv
    except ImportError:
        # 不静默：明确告知缺少依赖，否则密钥问题会被误判为"配置写错了"
        print("[env] python-dotenv 未安装，跳过 .env 加载；"
              "请使用真实环境变量或安装 python-dotenv")
        return loaded

    for path in _CANDIDATE_ENV_FILES:
        if path.is_file():
            load_dotenv(dotenv_path=path, override=override)
            loaded.append(str(path))
    if not loaded:
        print(f"[env] 未找到 .env 文件（已检查 {[str(p) for p in _CANDIDATE_ENV_FILES]}），"
              "将只使用进程环境变量")
    return loaded


def get_secret(name: str, default: str = "") -> str:
    """读取密钥类配置的统一入口。

    顺序：os.environ → 空串。集中在一处便于将来接入密钥管理服务
    （如 Vault / KMS）时只改这一个函数。
    """
    value = os.environ.get(name)
    return value if value else default
