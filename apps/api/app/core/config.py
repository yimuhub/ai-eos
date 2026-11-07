"""Application settings via environment variables.

改造要点：
1. env_file 使用**绝对路径**（由 env_loader.API_ROOT 推导），
   不再依赖启动时的工作目录。
2. 导入本模块时先把 .env 注入 os.environ，使 `settings.x` 与
   `os.environ["X"]` 两条读取路径看到同一份配置。
3. 新增 model_api_keys 显式映射，供 model_gateway 统一取密钥。
4. 安全姿态自检**不再只限生产**：JWT_SECRET 缺失/为空/等于公开占位值时，
   首次启动自动生成并落盘一个随机密钥（详见 `_bootstrap_jwt_secret`）。
   空密钥过去能让 `jwt.encode(payload, "")` 签出可通过鉴权的 token，
   等于整个鉴权体系失守，因此这里把它变成"不可能出现的状态"。
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from pydantic import PrivateAttr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.env_loader import API_ROOT, env_file_path, load_env

# 尽早注入：本模块被导入即完成，早于任何业务代码读取环境变量
_ENV_FILES_LOADED = load_env()

_ENV_FILE = env_file_path()

# ⚠️ 仅供本地直开使用的占位密钥。
#
# 它出现在仓库里 = 公开信息。任何环境（生产、便携、桌面）只要还在用它，
# 任何人都能签发任意用户的 token。因此它不再作为 `jwt_secret` 的默认值
# 使用，只作为**"这个值是公开的、必须被替换掉"的指纹**保留。
_DEFAULT_JWT_SECRET = "aieos-portable-secret"

# 兼容旧文档/脚本里出现过的"默认值"写法
_INSECURE_SECRETS = frozenset({_DEFAULT_JWT_SECRET, "change-me", "secret", "changeme"})

# 模型配置（数据库里的用户可写字段）永远不允许引用的服务端敏感变量。
# 写入侧（API 校验）与读取侧（resolved_api_key）共用这一份名单：
# 升级前落库的历史行、或绕过校验直接改库的行，运行时同样取不到值。
_SENSITIVE_ENV_NAMES = frozenset({
    "JWT_SECRET", "SECRET_KEY", "DATABASE_URL", "REDIS_URL", "METRICS_TOKEN",
})
_SENSITIVE_ENV_SUFFIXES = ("_SECRET", "_PASSWORD")


def is_sensitive_env_name(name: str | None) -> bool:
    upper = (name or "").strip().upper()
    return bool(upper) and (upper in _SENSITIVE_ENV_NAMES
                            or upper.endswith(_SENSITIVE_ENV_SUFFIXES))

_SECRET_FILE_NAME = "jwt_secret.key"
_SECRET_FILE_ENV_VAR = "AIEOS_JWT_SECRET_FILE"

_config_logger = logging.getLogger("app.core.config")


def _is_insecure_jwt_secret(value: str | None) -> bool:
    """密钥是否"不可用于鉴权"：空、纯空白，或等于仓库里公开的占位值。"""
    if not value or not value.strip():
        return True
    return value.strip() in _INSECURE_SECRETS


def _secret_file_candidates(database_url: str = "", storage_dir: str = "") -> list[Path]:
    """密钥落盘位置的候选列表，按优先级排列。

    1. AIEOS_JWT_SECRET_FILE 显式指定（桌面壳/容器用它注入可写路径）
    2. SQLite 数据库同级目录（便携版：数据在哪，密钥就在哪）
    3. storage_dir / DATA_DIR 同级
    4. apps/api/data
    """
    out: list[Path] = []

    def add(p) -> None:
        if not p or not str(p).strip():
            return
        try:
            path = Path(str(p).strip()).expanduser()
            if not path.is_absolute():
                path = (API_ROOT / path).resolve()
            # 用户给了个目录（AIEOS_JWT_SECRET_FILE=/tmp/）→ 自动落到目录下的文件名
            # is_dir() 对权限类错误会往外抛，这里一并兜住：候选路径不值得为它崩
            if path.is_dir():
                path = path / _SECRET_FILE_NAME
        except Exception:  # noqa: BLE001 — 路径不可解析/无权限，换下一个候选即可
            _config_logger.warning("JWT 密钥候选路径不可用，已跳过：%r",
                                   p, exc_info=True)
            return
        if path not in out:
            out.append(path)

    add(os.getenv(_SECRET_FILE_ENV_VAR, ""))
    db_file = _sqlite_path_from_url(database_url)
    if db_file:
        add(Path(db_file).parent / _SECRET_FILE_NAME)
    for base in (storage_dir, os.getenv("DATA_DIR", "")):
        if base:
            add(Path(base) / _SECRET_FILE_NAME)

    add(API_ROOT / "data" / _SECRET_FILE_NAME)
    return out


def _sqlite_path_from_url(url: str) -> str:
    """从 sqlite+aiosqlite:///path 里抠出文件路径；非 SQLite 返回 ""。"""
    if not url or "sqlite" not in url:
        return ""
    marker = "sqlite:///"
    idx = url.find(marker)
    if idx < 0:
        return ""
    raw = url[idx + len(marker):]
    raw = raw.split("?")[0].strip()
    if not raw or raw in (":memory:", ":memory"):
        return ""
    return raw


def _bootstrap_jwt_secret(database_url: str = "", storage_dir: str = "") -> tuple[str, str]:
    """为空/默认密钥生成一个随机值，并尽量持久化。

    返回 (密钥, 来源说明)：
    - persisted:<path> —— 已落盘，重启后登录态保持不变
    - memory           —— 目录不可写，只用进程内随机密钥

    两条路都**不会**退回空密钥或公开常量。
    """
    for candidate in _secret_file_candidates(database_url, storage_dir):
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            reusable = _read_secret_file(candidate)
            if reusable:
                return reusable, f"persisted:{candidate}"
            token = secrets.token_urlsafe(32)
            # O_EXCL：并发启动时只有一个进程能"赢"，输的那个回头读对方的成果，
            # 保证多进程/多次重启拿到的是同一个密钥（登录态不会互踢）
            try:
                fd = os.open(str(candidate),
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                reusable = _read_secret_file(candidate)
                if reusable:
                    return reusable, f"persisted:{candidate}"
                raise
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token + "\n")
            try:
                os.chmod(candidate, 0o600)
            except OSError as exc:
                # 落盘已成功，仅权限没收紧（某些挂载点/文件系统不支持 chmod）：
                # 密钥仍可用，但必须让"它不是 0600"这件事被看见。
                _config_logger.warning(
                    "[security] JWT 密钥文件 %s 无法设为 0600（%s），请确认其权限",
                    candidate, exc,
                )
            return token, f"persisted:{candidate}"
        except OSError as exc:
            _config_logger.warning(
                "[security] JWT 密钥文件 %s 不可用（%s），尝试下一个位置",
                candidate, exc,
            )
    return secrets.token_urlsafe(32), "memory"


def _read_secret_file(path: Path) -> str:
    """读取已存在的密钥文件；空/公开值/读不动都视为"没有可用密钥"。"""
    try:
        if not path.is_file():
            return ""
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return "" if _is_insecure_jwt_secret(value) else value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE.is_file() else None,
        extra="ignore",
    )

    app_name: str = "AI-EOS API"
    debug: bool = True
    # 逐条打印 SQL 与参数，由 SQL_ECHO 单独控制，不再跟随 DEBUG。
    # 跟随 DEBUG 有两个后果：默认真实库下调度器每 15 秒轮询一次，日志被
    # SELECT 灌到上百 MB；而想安静的人只能去改 settings.debug，那会连带
    # 关掉 /docs 与 /metrics（曾有两处路由就这么干过）。
    sql_echo: bool = False
    # 全功能测试台开关：生产环境设 false（默认随 debug）
    enable_full_function_test_page: bool | None = None

    # 便携打包模式：默认 SQLite（数据文件在 data/ 下），可用环境变量切回 PG
    database_url: str = "sqlite+aiosqlite:///./data/aieos.db"
    redis_url: str = "redis://localhost:6379/0"  # 登录限流用；连不上时自动降级为进程内计数

    # 默认空——空值不再意味着"用占位密钥运行"，而是触发下面的随机密钥生成。
    # 用 _DEFAULT_JWT_SECRET 当默认值会让"忘记配置"退化成"公开密钥"。
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440
    jwt_refresh_expire_days: int = 30  # refresh token 有效期（可吊销，存 DB）

    # /metrics/prometheus 的访问令牌；留空时该端点只允许回环地址访问
    metrics_token: str = ""

    # 密钥来源（configured / persisted:<path> / memory），仅用于启动日志
    _jwt_secret_source: str = PrivateAttr(default="configured")

    storage_dir: str = "./storage"
    # 单文件上传上限（MB）：assets 上传与知识库原文上传共用。
    # 流式落盘时按此上限边写边计数，超出即删除半成品并返回 413。
    max_upload_mb: int = 25
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
    def jwt_secret_source(self) -> str:
        """密钥来源说明：configured / persisted:<path> / memory。"""
        return self._jwt_secret_source

    @property
    def using_default_jwt_secret(self) -> bool:
        """当前密钥是否等于仓库里公开的占位值。

        正常情况下 _ensure_usable_jwt_secret 永远不会让它为 True；
        保留这个判定是为了把"配置里显式写了公开常量"变成生产环境的致命错误。
        """
        return self.jwt_secret.strip() in _INSECURE_SECRETS

    @property
    def jwt_secret_is_generated(self) -> bool:
        """密钥是否由本次启动自动生成（用户未配置）。"""
        return self._jwt_secret_source != "configured"

    @property
    def test_page_enabled(self) -> bool:
        """全功能测试台是否开放。显式配置优先，否则跟随 debug。"""
        if self.enable_full_function_test_page is not None:
            return self.enable_full_function_test_page
        return self.debug

    @model_validator(mode="after")
    def _ensure_usable_jwt_secret(self) -> "Settings":
        """无条件保证 jwt_secret 是一个可用密钥。

        背景：`jwt.encode(payload, "", "HS256")` 在空密钥下照样能签出 token，
        而 get_current_user 用同一个空密钥校验通过——以前只有
        `AIEOS_ENV=production` 才拦，便携版/桌面版不设该变量，等于全线裸奔。

        这里不直接崩溃（会把所有还在用空密钥的老便携版打挂），而是：
        1. 首次启动生成 secrets.token_urlsafe(32) 并落盘（默认 data/jwt_secret.key）
        2. 落盘失败（只读目录/权限）→ 降级为进程内随机密钥 + 大声 WARNING
        3. 绝不退回空密钥或公开常量
        """
        if not _is_insecure_jwt_secret(self.jwt_secret):
            self._jwt_secret_source = "configured"
            return self

        # 生产环境**不**自动生成：随机密钥会随进程消失，且让"忘记配置密钥"
        # 这件事悄无声息地过去。留原值，交给 _enforce_security_posture 拒绝启动。
        if self.is_production:
            return self

        token, source = _bootstrap_jwt_secret(self.database_url, self.storage_dir)
        self.jwt_secret = token
        self._jwt_secret_source = source
        if source == "memory":
            _config_logger.warning(
                "[security] 未能写入 JWT 密钥文件（目录不可写），已改用进程内随机密钥。"
                "服务重启后所有登录态失效，需要重新登录。"
            )
        else:
            _config_logger.warning(
                "[security] JWT_SECRET 未配置或不可用，已为你生成随机密钥并保存到 %s。"
                "请备份该文件；文件丢失会导致所有登录态失效。",
                source.split(":", 1)[1],
            )
        return self

    @model_validator(mode="after")
    def _enforce_security_posture(self) -> "Settings":
        """生产环境的安全底线检查。

        这里只做"明显不能上生产"的拦截，不做过度校验：
        - 生产环境 + 密钥是公开常量 → 拒绝启动（否则任何人可伪造 token）
        - 生产环境 + debug 未显式关闭 → 拒绝启动（debug 会暴露堆栈与测试台）
        - 开发环境 → 只警告，保证"克隆下来就能跑"
        """
        if not self.is_production:
            return self

        problems: list[str] = []
        if _is_insecure_jwt_secret(self.jwt_secret) or self.using_default_jwt_secret:
            problems.append(
                "JWT_SECRET 为空或仍是公开占位值。该值公开可见，任何人都能据此签发出"
                "任意用户的 token，等于没有鉴权。请在 apps/api/.env 中设置"
                "一个随机长字符串（如 `openssl rand -hex 32` 的输出）。"
            )
        if self.debug:
            problems.append(
                "DEBUG 仍为 True。生产环境请设置 DEBUG=false，"
                "否则会暴露详细堆栈与 API 文档，并可能开放全功能测试台。"
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
        if self.jwt_secret_is_generated:
            if self._jwt_secret_source == "memory":
                warnings.append(
                    "JWT_SECRET 未配置且密钥文件不可写，本次使用进程内随机密钥——"
                    "重启后所有登录态失效。请设置 JWT_SECRET 或 "
                    "AIEOS_JWT_SECRET_FILE 指向可写路径。"
                )
            else:
                warnings.append(
                    f"JWT_SECRET 未配置，已自动生成并保存到 "
                    f"{self._jwt_secret_source.split(':', 1)[1]}"
                    "——请备份该文件，丢失会导致所有登录态失效。"
                    "正式部署请显式配置 JWT_SECRET。"
                )
        if self.using_default_jwt_secret:
            warnings.append(
                "JWT_SECRET 使用默认占位值——仅适用于本机试用，"
                "切勿用于部署。生成方式：openssl rand -hex 32"
            )
        # METRICS_TOKEN 留空不是"待修复的安全问题"——回环 only 正是单机桌面版的
        # 正确默认值，把它放进本列表会让"配置已加固"永远无法收敛到零告警。
        if self.debug:
            warnings.append("DEBUG=True（开发模式）。部署前请设 DEBUG=false。")
        if self.sql_echo:
            warnings.append(
                "SQL_ECHO=True：每条语句连同绑定参数原样写进日志——"
                "里面可能有密钥、token 和用户数据。只适合本地临时排查，"
                "部署前请设 SQL_ECHO=false。"
            )
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

        敏感变量名在这里**直接忽略**（不取、不回退），作为 API 校验之外的
        兜底：历史遗留的 model 行（升级前写入、或直接改库）即使
        `api_key_env=JWT_SECRET`，运行时也拿不到服务端密钥。

        **os.environ 必须优先于 self 上的同名字段**：`self.*` 是导入期的一次性
        快照，而 `/models/env-key` 在运行期才改写 os.environ。若先读快照，
        用户在「模型配置」里换过的 Key 会被启动时的旧 Key 顶掉，
        表现为一直 401 Invalid token，直到重启后端——而界面却提示"已立即生效"。
        """
        if env_var_name and not is_sensitive_env_name(env_var_name):
            value = os.environ.get(env_var_name, "")
            if value:
                return value
        default_env = {
            "openai_compat": "OPENAI_COMPAT_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "gemini": "GEMINI_API_KEY",
        }.get(provider, "")
        if default_env:
            value = os.environ.get(default_env, "")
            if value:
                return value
        return {
            "openai_compat": self.openai_compat_api_key,
            "anthropic": self.anthropic_api_key,
            "gemini": self.gemini_api_key,
        }.get(provider, "")


settings = Settings()
