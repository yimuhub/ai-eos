"""Alembic is the only schema authority; startup verifies or upgrades it safely."""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import inspect, make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.logging_config import get_logger
from app.db.base import Base

logger = get_logger(__name__)
API_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = API_ROOT / "alembic.ini"
VERSION_TABLE = "alembic_version"


def load_metadata():
    import app.models as _models  # noqa: F401
    import app.task_engine as _task_engine  # noqa: F401
    import app.api.routes.agent_chat as _agent_chat  # noqa: F401
    import app.api.routes.group_chat as _group_chat  # noqa: F401
    import app.api.routes.growth_center as _growth_center  # noqa: F401
    import app.api.routes.doc_styles as _doc_styles  # noqa: F401

    return Base.metadata


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))


def _head() -> str:
    heads = _script().get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"Alembic 必须只有一个 head，实际为 {heads}")
    return heads[0]


def _redact(output: str, url: str) -> str:
    output = output.replace(url, make_url(url).render_as_string(hide_password=True))
    parsed = make_url(url)
    if parsed.password:
        output = output.replace(parsed.password, "***")
    return output


def _run_alembic(url: str, action: str, revision: str = "head") -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = _async_url(url)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI),
         "-x", f"db_url={url}", action, revision],
        cwd=API_ROOT, env=env, capture_output=True, text=True, timeout=180)
    if result.returncode:
        details = _redact(result.stdout + result.stderr, url).strip()
        raise RuntimeError(f"Alembic {action} 失败（退出码 {result.returncode}）：{details}")


def _async_url(url: str) -> str:
    parsed = make_url(url)
    if parsed.get_dialect().is_async:
        return url
    driver = {"sqlite": "aiosqlite", "postgresql": "asyncpg"}.get(
        parsed.get_backend_name())
    if not driver:
        raise RuntimeError(f"不支持的数据库方言：{parsed.get_backend_name()}")
    return parsed.set(drivername=f"{parsed.get_backend_name()}+{driver}").render_as_string(
        hide_password=False)


async def _state(engine) -> tuple[list[str], set[str]]:
    async with engine.connect() as conn:
        tables = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
        heads = await conn.run_sync(
            lambda c: MigrationContext.configure(c).get_current_heads())
    return list(heads), tables - {VERSION_TABLE}


def _validate_known_revisions(heads: list[str]) -> None:
    if not heads:
        return
    try:
        _script().get_revisions(heads)
    except CommandError as exc:
        raise RuntimeError(
            "数据库来自已停用的 Alembic 迁移链，不能自动 stamp；"
            "请备份后按 docs/数据库迁移.md 人工采用当前迁移链") from exc


def _schema_diff(connection) -> list:
    context = MigrationContext.configure(
        connection, opts={"compare_type": True, "compare_server_default": False,
                          "include_schemas": False})
    return compare_metadata(context, load_metadata())


def _format_diff(diffs: list) -> str:
    return "; ".join(str(item) for item in diffs[:12])


async def verify_database(engine) -> str:
    heads, _ = await _state(engine)
    _validate_known_revisions(heads)
    expected = _head()
    if heads != [expected]:
        raise RuntimeError(
            f"数据库迁移版本为 {heads or '未登记'}，期望 {expected}；"
            "请先执行 python -m app.db.migrations upgrade")
    async with engine.connect() as conn:
        diffs = await conn.run_sync(_schema_diff)
    if diffs:
        raise RuntimeError("数据库结构与模型漂移：" + _format_diff(diffs))
    return expected


async def prepare_database(engine) -> str:
    """Portable startup: migrate empty/versioned SQLite, strictly adopt old SQLite."""
    heads, tables = await _state(engine)
    _validate_known_revisions(heads)
    url = engine.url.render_as_string(hide_password=False)
    if not heads and tables:
        async with engine.connect() as conn:
            diffs = await conn.run_sync(_schema_diff)
        if diffs:
            raise RuntimeError(
                "未登记版本号的老库与当前模型不完全一致，拒绝自动改表或 stamp；"
                "请备份并执行受控迁移。差异：" + _format_diff(diffs))
        await asyncio.to_thread(_run_alembic, url, "stamp", _head())
        logger.info("[startup] 老库结构校验通过，已登记 Alembic 基线 %s", _head())
    else:
        await asyncio.to_thread(_run_alembic, url, "upgrade", "head")
    return await verify_database(engine)


async def backfill_superadmin(engine) -> str | None:
    try:
        async with engine.begin() as conn:
            tables = set(await conn.run_sync(
                lambda c: inspect(c).get_table_names()))
            if "users" not in tables or "companies" not in tables:
                return None
            user_columns = await conn.run_sync(
                lambda c: {col["name"] for col in inspect(c).get_columns("users")})
            if "is_superadmin" not in user_columns:
                return None
            has_super = (await conn.execute(text(
                "SELECT id FROM users WHERE is_superadmin = :v LIMIT 1"),
                {"v": True})).first()
            if has_super is not None:
                return None
            row = (await conn.execute(text(
                "SELECT owner_id FROM companies WHERE owner_id IS NOT NULL "
                "ORDER BY created_at ASC, id ASC LIMIT 1"))).first()
            if row is None or not row[0]:
                return None
            updated = await conn.execute(text(
                "UPDATE users SET is_superadmin = :v WHERE id = :uid"),
                {"v": True, "uid": row[0]})
            if not updated.rowcount:
                return None
            logger.info("[migrate] 历史安装无超管，已把最早的 owner(%s) 提升为超管", row[0])
            return row[0]
    except Exception as exc:  # noqa: BLE001 — 回填失败不能阻塞启动
        logger.warning("[migrate] 超管回填跳过（不影响启动）：%s", exc)
        return None


async def _cli_async(action: str, url: str) -> str:
    engine = create_async_engine(_async_url(url))
    try:
        if action == "upgrade":
            return await prepare_database(engine)
        return await verify_database(engine)
    finally:
        await engine.dispose()


def main() -> None:
    from app.core.config import settings

    parser = argparse.ArgumentParser(description="AI-EOS Alembic schema runner")
    parser.add_argument("action", choices=("upgrade", "check", "current"))
    parser.add_argument("--url", default=os.environ.get("ALEMBIC_DATABASE_URL")
                        or settings.database_url)
    args = parser.parse_args()
    url = args.url
    try:
        if args.action == "current":
            async def current():
                engine = create_async_engine(_async_url(url))
                try:
                    heads, _ = await _state(engine)
                    return heads
                finally:
                    await engine.dispose()

            print(",".join(asyncio.run(current())) or "(empty)")
            return
        revision = asyncio.run(_cli_async(args.action, url))
        print(f"{args.action}: {revision}")
    except Exception as exc:
        print(_redact(str(exc), url), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
