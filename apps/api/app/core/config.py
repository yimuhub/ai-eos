"""Application settings via environment variables.

改造要点：
1. env_file 使用**绝对路径**（由 env_loader.API_ROOT 推导），
   不再依赖启动时的工作目录。
2. 导入本模块时先把 .env 注入 os.environ，使 `settings.x` 与
   `os.environ["X"]` 两条读取路径看到同一份配置。
3. 新增 model_api_keys 显式映射，供 model_gateway 统一取密钥。
4. 新增安全姿态自检：默认密钥/默认 debug 在**生产环境直接拒绝启动**，
   在开发环境则大声警告。原因见 `_DEFAULT_JWT_SECRET` 注释。
"""
from __future__ import annotations

import os

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.env_loader import API_ROOT, load_env

# 尽早注入：本模块被导入即完成，早于任何业务代码读取环境变量
_ENV_FILES_LOADED = load_env()

_ENV_FILE = API_ROOT / ".env"

# ⚠️ 仅供本地直开使用的占位密钥。
#
# 保留它，是为了让"克隆下来就能跑"成立（用户不必先造一个密钥）。
# 但它同时意味着：**如果生产环境忘记配置 JWT_SECRET，所有人都能用这个
# 公开值签发任意用户的 token**，等于没有鉴权。
# 因此下面的 _enforce_security_posture 会在生产环境直接拒绝启动，
# 把"忘记配置"从"静默失守"变成"显式失败"。
_DEFAULT_JWT_SECRET = "aieos-portable-secret"


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

    jwt_secret: str = _DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440
    jwt_refresh_expire_days: int = 30  # refresh token 有效期（可吊销，存 DB）

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
    def using_default_jwt_secret(self) -> bool:
        """是否仍在使用公开的占位密钥。"""
        return self.jwt_secret == _DEFAULT_JWT_SECRET

    @property
    def test_page_enabled(self) -> bool:
        """全功能测试台是否开放。显式配置优先，否则跟随 debug。"""
        if self.enable_full_function_test_page is not None:
            return self.enable_full_function_test_page
        return self.debug

    @model_validator(mode="after")
    def _enforce_security_posture(self) -> "Settings":
        """生产环境的安全底线检查。

        这里只做"明显不能上生产"的拦截，不做过度校验：
        - 生产环境 + 默认 JWT 密钥 → 拒绝启动（否则任何人可伪造 token）
        - 生产环境 + debug 未显式关闭 → 拒绝启动（debug 会暴露堆栈与测试台）
        - 开发环境 → 只警告，保证"克隆下来就能跑"
        """
        if not self.is_production:
            return self

        problems: list[str] = []
        if self.using_default_jwt_secret:
            problems.append(
                "JWT_SECRET 仍为默认值。该值公开可见，任何人都能据此签发出"
                "任意用户的 token，等于没有鉴权。请在 apps/api/.env 中设置"
                "一个随机长字符串（如 `openssl rand -hex 32` 的输出）。"
            )
        if self.debug:
            problems.append(
                "DEBUG 仍为 True。生产环境请设置 DEBUG=false，"
                "否则会暴露详细堆栈，并可能开放全功能测试台。"
            )
        if problems:
            raise ValueError(
                "生产环境（AIEOS_ENV=production）安全配置未通过：\n  - "
                + "\n  - ".join(problems)
            )
        return self

    def security_warnings(self) -> list[str]:
        """启动时打印的告警（非致命）。让"能跑"和"安全"之间的差距被看见。"""
        warnings: list[str] = []
        if self.using_default_jwt_secret:
            warnings.append(
                "JWT_SECRET 使用默认占位值——仅适用于本机试用，"
                "切勿用于部署。生成方式：openssl rand -hex 32"
            )
        if self.debug:
            warnings.append("DEBUG=True（开发模式）。部署前请设 DEBUG=false。")
        if self.test_page_enabled:
            warnings.append(
                "全功能测试台处于开放状态（enable_full_function_test_page 未设为 false，"
                "当前跟随 DEBUG）。该页面可触发危险能力，请勿在公网暴露。"
            )
        return warnings

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
