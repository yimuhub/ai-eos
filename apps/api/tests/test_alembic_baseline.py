"""Alembic 基线护栏：空库 `upgrade head` 必须与 `create_all` 等价。

## 为什么要有这组用例

运行时仍然走 `create_all` + `ensure_columns`（便携部署，见
`docs/数据库迁移.md`）。Alembic 基线如果和模型漂移，就会出现
"SQLite 能跑、`alembic upgrade head` 出来的库少列/少索引"的分裂，
而这类问题只会在客户机上炸。这里把"基线 == 模型"钉成断言。

## 设计约束

* 全程只用 `tmp_path` 下的临时 SQLite，绝不碰 `apps/api/data/`。
* 用 Alembic Python API（Config + command.upgrade/downgrade），
  不 subprocess：不依赖 cwd，也能拿到真实异常栈。
* 显式传 `-x db_url=...`，URL 解析不依赖调用进程的 `.env` / 环境变量。
* 同步 `def` 用例：env.py 内部对 async 方言用 `asyncio.run()`，
  在已运行的事件循环里会直接报错；同步用例天然避开这一点。
"""
from __future__ import annotations

import configparser
import os
from pathlib import Path
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

import app.models  # noqa: F401 — 必须先把全部模型注册到 Base.metadata
from app.db.base import Base

API_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = API_ROOT / "alembic.ini"
MIGRATIONS_DIR = API_ROOT / "migrations"
BASELINE_REVISION = "0001"
ALEMBIC_VERSION_TABLE = "alembic_version"


def _alembic_config(db_path: Path) -> Config:
    """构造不依赖 cwd / ini 的 Config（URL 走 -x，与部署脚本同一条路径）。"""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    url = f"sqlite:///{db_path}"
    cfg.set_main_option("sqlalchemy.url", url)
    # context.get_x_argument() 读的就是 cmd_opts.x
    cfg.cmd_opts = SimpleNamespace(x=[f"db_url={url}"])
    return cfg


def _run(cfg: Config, action: str, revision: str) -> None:
    """执行 upgrade/downgrade，并在结束后还原 DATABASE_URL。

    env.py 会把解析出的目标 URL 写进 `os.environ["DATABASE_URL"]`，
    这是为了"迁移进程里顺手创建的 engine 也指向目标库"。测试进程里
    必须还原，否则会污染同会话的其他用例。
    """
    previous = os.environ.get("DATABASE_URL")
    try:
        getattr(command, action)(cfg, revision)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def _snapshot(db_path: Path) -> dict[str, dict[str, set[str]]]:
    """{表: {"columns": {...}, "indexes": {...}}}，忽略 alembic_version。"""
    insp = inspect(create_engine(f"sqlite:///{db_path}"))
    out: dict[str, dict[str, set[str]]] = {}
    for table in insp.get_table_names():
        if table == ALEMBIC_VERSION_TABLE:
            continue
        out[table] = {
            "columns": {col["name"] for col in insp.get_columns(table)},
            "indexes": {idx["name"] for idx in insp.get_indexes(table)},
        }
    return out


def _create_all_db(db_path: Path) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def test_alembic_baseline_matches_create_all(tmp_path: Path) -> None:
    alembic_db = tmp_path / "alembic.db"
    create_all_db = tmp_path / "create_all.db"

    _run(_alembic_config(alembic_db), "upgrade", "head")
    _create_all_db(create_all_db)

    migrated = _snapshot(alembic_db)
    created = _snapshot(create_all_db)

    assert set(migrated) == set(created), (
        "Alembic 基线缺表/多表："
        f"仅基线有={sorted(set(migrated) - set(created))}，"
        f"仅 create_all 有={sorted(set(created) - set(migrated))}"
    )

    column_diffs = {
        table: {
            "alembic_only": sorted(migrated[table]["columns"] - created[table]["columns"]),
            "create_all_only": sorted(created[table]["columns"] - migrated[table]["columns"]),
        }
        for table in sorted(migrated)
        if migrated[table]["columns"] != created[table]["columns"]
    }
    assert not column_diffs, f"列集合不一致（说明模型改了但基线没跟上）：{column_diffs}"

    index_diffs = {
        table: {
            "alembic_only": sorted(migrated[table]["indexes"] - created[table]["indexes"]),
            "create_all_only": sorted(created[table]["indexes"] - migrated[table]["indexes"]),
        }
        for table in sorted(migrated)
        if migrated[table]["indexes"] != created[table]["indexes"]
    }
    assert not index_diffs, f"索引集合不一致：{index_diffs}"


def test_alembic_downgrade_base_removes_business_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "downgrade.db"
    cfg = _alembic_config(db_path)

    _run(cfg, "upgrade", "head")
    assert set(_snapshot(db_path)), "upgrade 后应当有业务表"

    _run(cfg, "downgrade", "base")

    insp = inspect(create_engine(f"sqlite:///{db_path}"))
    leftovers = set(insp.get_table_names()) - {ALEMBIC_VERSION_TABLE}
    assert not leftovers, f"downgrade base 后仍有业务表残留：{sorted(leftovers)}"


def test_single_head_and_ini_points_to_migrations() -> None:
    """`alembic heads` 只能有一个；ini 的 script_location 必须指向 migrations/。

    两个 head 意味着分叉历史，`upgrade head` 会直接失败；
    ini 指回旧 `alembic/` 则会让部署脚本跑一套缺列的历史迁移。
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    script = ScriptDirectory.from_config(cfg)
    heads = list(script.get_heads())
    assert len(heads) == 1, f"期望单一 head（分叉会让 upgrade head 失败），实际 {heads}"
    # 基线必须仍在 head 的祖先链上：追加迁移可以，改基线/另起分叉不行。
    ancestry = {rev.revision for rev in script.iterate_revisions(heads[0], "base")}
    assert BASELINE_REVISION in ancestry, (
        f"head {heads[0]} 不在基线 {BASELINE_REVISION} 的演进链上")

    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ALEMBIC_INI, encoding="utf-8")
    configured = parser.get("alembic", "script_location")
    assert configured.replace("%(here)s", str(API_ROOT)) == str(MIGRATIONS_DIR)
