"""Schema authority: fresh migration, strict adoption and safe failure boundaries."""
from __future__ import annotations

import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.migrations import prepare_database, verify_database

pytestmark = pytest.mark.asyncio
API_ROOT = pathlib.Path(__file__).resolve().parent.parent


def alembic(db_path: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_url=sqlite:///{db_path}", *args],
        cwd=API_ROOT, capture_output=True, text=True, timeout=90)


@asynccontextmanager
async def database(tmp: pathlib.Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp / 'schema.db'}")
    try:
        yield engine
    finally:
        await engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


async def revision(engine) -> str | None:
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
        if "alembic_version" not in tables:
            return None
        return (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()


async def test_fresh_database_upgrades_and_verifies_without_create_all():
    with tempfile.TemporaryDirectory(prefix="aieos_fresh_migrate_") as raw:
        async with database(pathlib.Path(raw)) as engine:
            assert await revision(engine) is None
            assert await prepare_database(engine) == "0040"
            assert await prepare_database(engine) == "0040"
            assert await verify_database(engine) == "0040"
            async with engine.connect() as conn:
                tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
            assert {"users", "companies", "billing_webhook_events"} <= set(tables)


async def test_equivalent_unversioned_legacy_database_is_stamped_without_data_loss():
    from app.db.migrations import load_metadata

    with tempfile.TemporaryDirectory(prefix="aieos_adopt_migrate_") as raw:
        async with database(pathlib.Path(raw)) as engine:
            async with engine.begin() as conn:
                await conn.run_sync(lambda c: load_metadata().create_all(c))
                await conn.execute(text(
                    "INSERT INTO companies (id, owner_id, name, attachments) "
                    "VALUES ('legacy-company', 'legacy-owner', '原有公司', '[]')"))
            assert await revision(engine) is None
            assert await prepare_database(engine) == "0040"
            assert await revision(engine) == "0040"
            async with engine.connect() as conn:
                assert (await conn.execute(text(
                    "SELECT name FROM companies WHERE id='legacy-company'"))).scalar() == "原有公司"


async def test_incomplete_unversioned_legacy_database_is_rejected_unchanged():
    with tempfile.TemporaryDirectory(prefix="aieos_partial_migrate_") as raw:
        async with database(pathlib.Path(raw)) as engine:
            async with engine.begin() as conn:
                await conn.execute(text(
                    "CREATE TABLE agent_executions (id VARCHAR(36) PRIMARY KEY, output TEXT)"))
                await conn.execute(text(
                    "INSERT INTO agent_executions (id, output) VALUES ('e1', '保留')"))
            with pytest.raises(RuntimeError, match="拒绝自动改表或 stamp"):
                await prepare_database(engine)
            assert await revision(engine) is None
            async with engine.connect() as conn:
                columns = await conn.run_sync(
                    lambda c: {col["name"] for col in inspect(c).get_columns("agent_executions")})
                assert columns == {"id", "output"}
                assert (await conn.execute(text(
                    "SELECT output FROM agent_executions WHERE id='e1'"))).scalar() == "保留"


async def test_retired_migration_chain_is_rejected_without_stamping():
    with tempfile.TemporaryDirectory(prefix="aieos_retired_migrate_") as raw:
        path = pathlib.Path(raw) / "schema.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            conn.execute("INSERT INTO alembic_version VALUES ('23e9b97bb927')")
        async with database(pathlib.Path(raw)) as engine:
            with pytest.raises(RuntimeError, match="已停用的 Alembic 迁移链"):
                await prepare_database(engine)
            assert await revision(engine) == "23e9b97bb927"


async def test_versioned_database_upgrades_to_head_and_detects_drift():
    with tempfile.TemporaryDirectory(prefix="aieos_versioned_migrate_") as raw:
        path = pathlib.Path(raw) / "schema.db"
        result = alembic(path, "upgrade", "0039")
        assert result.returncode == 0, result.stdout + result.stderr
        async with database(pathlib.Path(raw)) as engine:
            assert await revision(engine) == "0039"
            assert await prepare_database(engine) == "0040"
            async with engine.begin() as conn:
                await conn.execute(text(
                    "CREATE TABLE unexpected_table (id VARCHAR(36) PRIMARY KEY)"))
            with pytest.raises(RuntimeError, match="数据库结构与模型漂移"):
                await verify_database(engine)


async def test_duplicate_artifact_versions_stop_migration_without_deleting_history():
    with tempfile.TemporaryDirectory(prefix="aieos_duplicate_migrate_") as raw:
        path = pathlib.Path(raw) / "schema.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            conn.execute("INSERT INTO alembic_version VALUES ('0037')")
            conn.execute("CREATE TABLE artifact_versions ("
                         "id VARCHAR(36) PRIMARY KEY, artifact_id VARCHAR(36) NOT NULL, "
                         "version INTEGER NOT NULL, content TEXT NOT NULL DEFAULT '')")
            conn.execute("INSERT INTO artifact_versions (id, artifact_id, version) "
                         "VALUES ('v1', 'a1', 1), ('v2', 'a1', 1)")
        async with database(pathlib.Path(raw)) as engine:
            with pytest.raises(RuntimeError, match="重复版本号.*a1:v1"):
                await prepare_database(engine)
            assert await revision(engine) == "0037"
            async with engine.connect() as conn:
                assert (await conn.execute(text(
                    "SELECT COUNT(*) FROM artifact_versions WHERE artifact_id='a1'"))).scalar() == 2


async def test_postgres_startup_verification_never_upgrades(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="aieos_verify_only_") as raw:
        async with database(pathlib.Path(raw)) as engine:
            async with engine.begin() as conn:
                await conn.execute(text("CREATE TABLE placeholder (id INTEGER)"))
            called = False

            def forbidden(*args, **kwargs):
                nonlocal called
                called = True
                raise AssertionError("verification must not mutate schema")

            monkeypatch.setattr("app.db.migrations._run_alembic", forbidden)
            with pytest.raises(RuntimeError, match="数据库迁移版本"):
                await verify_database(engine)
            assert not called


async def test_stripe_migration_upgrade_downgrade_preserves_offline_orders():
    with tempfile.TemporaryDirectory(prefix="aieos_stripe_migrate_") as raw:
        path = pathlib.Path(raw) / "stripe.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE subscription_orders (id VARCHAR(36) PRIMARY KEY)")
            conn.execute("INSERT INTO subscription_orders (id) VALUES ('legacy-order')")
        result = alembic(path, "stamp", "0038")
        assert result.returncode == 0, result.stdout + result.stderr
        result = alembic(path, "upgrade", "0039")
        assert result.returncode == 0, result.stdout + result.stderr
        with sqlite3.connect(path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(subscription_orders)")}
            assert {"payment_method", "stripe_checkout_session_id", "amount", "currency",
                    "checkout_operation_id"} <= columns
            assert conn.execute(
                "SELECT payment_method FROM subscription_orders WHERE id='legacy-order'"
            ).fetchone() == ("offline",)
            indexes = {row[1]: row[2] for row in conn.execute(
                "PRAGMA index_list(subscription_orders)").fetchall()}
            assert indexes["ix_subscription_orders_stripe_checkout_session_id"] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM billing_webhook_events").fetchone() == (0,)
        result = alembic(path, "downgrade", "0038")
        assert result.returncode == 0, result.stdout + result.stderr
        with sqlite3.connect(path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(subscription_orders)")}
            assert "payment_method" not in columns
            assert conn.execute("SELECT id FROM subscription_orders").fetchone() == ("legacy-order",)
            assert conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='billing_webhook_events'"
            ).fetchone() == (0,)
