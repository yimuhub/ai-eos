"""Application settings via environment variables.

改造要点（修复"只在 .env 配密钥 → 取到空串"的问题）：
1. env_file 使用**绝对路径**（由 env_loader.API_ROOT 推导），
   不再依赖启动时的工作目录。
2. 导入本模块时先把 .env 注入 os.environ，使 `settings.x` 与
   `os.environ["X"]` 两条读取路径看到同一份配置。
3. 新增 model_api_keys 显式映射，供 model_gateway 统一取密钥。
"""
from __future__ import annotations

import os

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.env_loader import API_ROOT, load_env

# 尽早注入：本模块被导入即完成，早于任何业务代码读取环境变量
_ENV_FILES_LOADED = load_env()

_ENV_FILE = API_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE.is_file() else None,
        extra="ignore",
    )

    app_name: str = "AI-EOS API"
    debug: bool = True
    # 全功能测试台开关：生产环境设 false（默认随 debug）
    enable_full_function_test_page: bool | None = None

    # 便携打包模式：默认 SQLite（数据文件在 data/ 下），可用环境变量切回 PG
    database_url: str = "sqlite+aiosqlite:///./data/aieos.db"
    redis_url: str = "redis://localhost:6379/0"  # 未实际使用，保留兼容

    jwt_secret: str = "aieos-portable-secret"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440

    storage_dir: str = "./storage"
    doc_convert_model_id: str = ""   # 专用文档转换大模型（低成本）

    # ---- 模型服务连接（改造新增：集中声明，避免散落的 os.environ 读取）----
    openai_compat_api_key: str = ""
    openai_compat_base_url: str = "https://api.agnes-ai.cn/v1"
    openai_compat_model: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""

    @property
    def is_production(self) -> bool:
        return os.getenv("AIEOS_ENV", "development").strip().lower() == "production"

    @property
    def test_page_enabled(self) -> bool:
        """全功能测试台是否开放。显式配置优先，否则跟随 debug。"""
        if self.enable_full_function_test_page is not None:
            return self.enable_full_function_test_page
        return self.debug

    def resolved_api_key(self, env_var_name: str | None, provider: str = "") -> str:
        """按环境变量名取密钥；取不到时回退到 provider 默认键。

        统一入口，让"取不到密钥"这件事有一个可断言的位置，
        便于后续加健康检查/启动自检（不要再散落 os.environ.get）。
        """
        if env_var_name:
            value = os.environ.get(env_var_name, "")
            if value:
                return value
        fallback = {
            "openai_compat": self.openai_compat_api_key or os.environ.get(
                "OPENAI_COMPAT_API_KEY", ""),
            "anthropic": self.anthropic_api_key or os.environ.get(
                "ANTHROPIC_API_KEY", ""),
            "gemini": self.gemini_api_key or os.environ.get(
                "GEMINI_API_KEY", ""),
        }
        return fallback.get(provider, "")


settings = Settings()
