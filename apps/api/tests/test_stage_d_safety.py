"""阶段 D 复审回归：测试库隔离、跨方言时间与有界成果打包。"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from app.db.session import AsyncSessionLocal
from app.db.types import CalendarDateTime
from app.models.artifact import ArtifactVersion
from app.models.evolution import EvolutionRecord
from app.models.interview import ExpertInterview, _now

API_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = API_ROOT.parent.parent


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
@pytest.mark.parametrize("aware", [False, True])
def test_calendar_datetime_preserves_wall_time_for_naive_and_aware_inputs(dialect, aware):
    value = datetime(2026, 11, 24, 0, 30)
    if aware:
        value = value.replace(tzinfo=timezone.utc)
    bound = CalendarDateTime().process_bind_param(value, SimpleNamespace(name=dialect))
    assert bound.hour == 0 and bound.minute == 30
    assert (bound.tzinfo is None) == (dialect == "sqlite")
    result = CalendarDateTime().process_result_value(bound, SimpleNamespace(name=dialect))
    assert result == datetime(2026, 11, 24, 0, 30)
    assert result.tzinfo is None


def test_evolution_period_columns_use_calendar_type():
    table = EvolutionRecord.__table__
    assert isinstance(table.c.period_start.type, CalendarDateTime)
    assert isinstance(table.c.period_end.type, CalendarDateTime)


def test_interview_timestamp_is_an_aware_utc_instant():
    value = _now()
    assert value.tzinfo is not None
    assert value.utcoffset().total_seconds() == 0
    assert ExpertInterview.__table__.c.created_at.type.timezone is True


async def test_interview_list_serializes_utc_with_z(client, tenants):
    response = await client.post(
        f"/interviews?company_id={tenants.alice_co['id']}",
        headers=tenants.alice["headers"],
        json={"transcript": "这是一段足够长的部门访谈记录，用于验证时间序列化。",
              "expert_template_id": "template-without-foreign-key"},
    )
    assert response.status_code == 201, response.text
    response = await client.get(
        f"/interviews?company_id={tenants.alice_co['id']}",
        headers=tenants.alice["headers"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["interviews"][0]["created_at"].endswith("Z")


@pytest.mark.parametrize("endpoint", ["create", "version"])
async def test_artifact_content_limit_is_enforced(client, tenants, endpoint):
    from app.api.routes.artifacts import MAX_ARTIFACT_CONTENT_CHARS

    headers = tenants.alice["headers"]
    company_id = tenants.alice_co["id"]
    content = "x" * (MAX_ARTIFACT_CONTENT_CHARS + 1)
    if endpoint == "create":
        response = await client.post(
            f"/artifacts?company_id={company_id}", headers=headers,
            json={"name": "超限成果", "content": content})
    else:
        created = await client.post(
            f"/artifacts?company_id={company_id}", headers=headers,
            json={"name": "正常成果", "content": "v1"})
        artifact_id = created.json()["id"]
        response = await client.post(
            f"/artifacts/{artifact_id}/versions", headers=headers,
            json={"content": content})
    assert response.status_code == 422, response.text


async def test_artifact_zip_closes_temporary_file_after_response(
        client, tenants, monkeypatch):
    original = tempfile.SpooledTemporaryFile
    created = []

    class TrackedSpool(original):
        def close(self):
            created.append(self)
            super().close()

    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", TrackedSpool)
    created_response = await client.post(
        f"/artifacts?company_id={tenants.alice_co['id']}",
        headers=tenants.alice["headers"],
        json={"name": "临时文件回收", "content": "正文"})
    response = await client.get(
        f"/artifacts/{created_response.json()['id']}/download.zip",
        headers=tenants.alice["headers"])
    assert response.status_code == 200
    assert response.content.startswith(b"PK")
    assert len(created) == 1 and created[0].closed


@pytest.mark.parametrize("limit_kind", ["versions", "bytes"])
async def test_artifact_zip_download_is_bounded(client, tenants, monkeypatch, limit_kind):
    from app.api.routes import artifacts

    headers = tenants.alice["headers"]
    created = await client.post(
        f"/artifacts?company_id={tenants.alice_co['id']}", headers=headers,
        json={"name": "有界归档", "content": "第一版正文"})
    artifact_id = created.json()["id"]
    async with AsyncSessionLocal() as db:
        db.add(ArtifactVersion(
            id="bounded-zip-extra-version", artifact_id=artifact_id,
            version=2, content="第二版正文", submitted_by=tenants.alice["id"]))
        await db.commit()
    if limit_kind == "versions":
        monkeypatch.setattr(artifacts, "MAX_ARTIFACT_ZIP_VERSIONS", 1)
    else:
        monkeypatch.setattr(artifacts, "MAX_ARTIFACT_ZIP_CONTENT_BYTES", 5)
    response = await client.get(
        f"/artifacts/{artifact_id}/download.zip", headers=headers)
    assert response.status_code == 413, response.text


def test_explicit_sqlite_test_url_is_copied_and_source_is_untouched(tmp_path):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as conn:
        conn.execute("PRAGMA user_version=7")
    original = source.read_bytes()
    env = dict(os.environ)
    env["AIEOS_TEST_DATABASE_URL"] = f"sqlite+aiosqlite:///{source}"
    script = """
import asyncio, json, runpy, shutil
ns = runpy.run_path('tests/conftest.py')
from app.db.session import engine
from app.db.migrations import prepare_database
asyncio.run(prepare_database(engine))
print('ISOLATED_DB=' + json.dumps(engine.url.database))
asyncio.run(engine.dispose())
shutil.rmtree(ns['_TMP_ROOT'], ignore_errors=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=API_ROOT, env=env,
        capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    isolated = next(line.removeprefix("ISOLATED_DB=") for line in result.stdout.splitlines()
                    if line.startswith("ISOLATED_DB="))
    assert Path(json.loads(isolated)) != source
    assert source.read_bytes() == original


def test_postgres_provision_failure_refuses_given_database():
    env = dict(os.environ)
    env["AIEOS_TEST_DATABASE_URL"] = (
        "postgresql+asyncpg://nobody:unused@127.0.0.1:5432/must_not_be_used")
    script = """
import asyncpg, runpy
async def denied(*args, **kwargs):
    raise PermissionError('CREATEDB denied')
asyncpg.connect = denied
runpy.run_path('tests/conftest.py')
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=API_ROOT, env=env,
        capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "拒绝使用给定库" in result.stderr
    assert "must_not_be_used" in result.stderr
    assert "nobody:unused@" not in result.stderr


def test_e2e_migration_cannot_be_redirected_by_alembic_url():
    source = (REPO_ROOT / "tools/e2e_smoke.py").read_text(encoding="utf-8")
    assert '"--url", database_url' in source
