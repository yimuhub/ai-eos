"""Application settings via environment variables."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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


settings = Settings()
