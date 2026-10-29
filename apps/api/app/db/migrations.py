"""便携式数据库的自动列补齐。

## 为什么需要

产品定位是「克隆下来就能跑」：SQLite 模式下启动时用
`Base.metadata.create_all` 自动建表。但 `create_all` **只建缺失的表，
不会给已存在的表补新列**。

于是升级后新增的字段在老数据库上根本不存在：一读就报
`no such column: xxx`，而用户什么都没做错，只是换了新版本。
对「直接就能用」来说，这是一个必须消掉的摩擦。

本模块做一件很小的事：比对模型定义与真实表结构，把**缺失的可空列**
用 `ALTER TABLE ... ADD COLUMN` 补上。幂等、无额外依赖、不动已有数据。

## 边界

* 只自动补**可空列**，以及非空但"老行有唯一正确答案"的 JSON 集合列
  （`default=list/dict` → 补 '[]'/'{}'）。其余非空列会被跳过并告警——
  这类改动需要人工决策（老行填什么值），不该由启动流程替用户判断。
* 不删除、不改类型、不改约束。它不是迁移框架，只是「让升级不炸」。
* **只在 SQLite 方言下运行**。PostgreSQL 的结构变更统一走 Alembic
  迁移，避免两套机制双轨打架（同一列被两边各加一次）。
"""
from __future__ import annotations

from sqlalchemy import JSON, inspect, text

from app.core.logging_config import get_logger
from app.db.base import Base

logger = get_logger(__name__)


async def ensure_columns(engine) -> list[str]:
    """补齐模型里新增、但数据库里还缺的可空列。返回补出的列名列表。"""
    # 双轨熔断：PostgreSQL 的 schema 演进走 Alembic；这个函数只服务
    # SQLite 便携形态。两边同时跑会在 PG 上打架（列被各加一次/锁表）。
    # 例外（V5.6.1）：PG 上"仅补缺失的可空列"是幂等且安全的
    # （ADD COLUMN IF NOT EXISTS，不碰已有列/不锁数据表），
    # 保证发版后老库升级不报 "no such column"。
    is_pg = engine.dialect.name == "postgresql"
    if not is_pg and engine.dialect.name != "sqlite":
        logger.info("[migrate] 跳过自动补列（方言=%s）", engine.dialect.name)
        return []
    added: list[str] = []
    dialect = engine.dialect

    async with engine.begin() as conn:
        existing_tables = set(
            await conn.run_sync(lambda c: inspect(c).get_table_names()))
        if is_pg:
            from sqlalchemy import text as _text
            for table in Base.metadata.sorted_tables:
                if table.name not in existing_tables:
                    continue
                for column in table.columns:
                    if not _auto_addable(column):
                        continue
                    # PG 幂等补列：仅补真正缺失的
                    have = (await conn.execute(_text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name=:t AND column_name=:c"),
                        {"t": table.name, "c": column.name})).first()
                    if have:
                        continue
                    ddl = f'ALTER TABLE {table.name} ADD COLUMN IF NOT EXISTS ' \
                          f'"{column.name}" {column.type.compile(dialect=dialect)}' \
                          f'{_default_clause(column)}'
                    await conn.execute(_text(ddl))
                    added.append(f"{table.name}.{column.name}")
                    logger.info("[migrate] PG 幂等补列 %s.%s", table.name, column.name)
        else:
            for table in Base.metadata.sorted_tables:
                if table.name not in existing_tables:
                    continue  # 新表交给 create_all，这里只管给老表加列
                present = await conn.run_sync(
                    lambda c, name=table.name:
                    {col["name"] for col in inspect(c).get_columns(name)})
                for column in table.columns:
                    if column.name in present:
                        continue
                    if not _auto_addable(column):
                        logger.warning(
                            "模型新增了非空列 %s.%s，无法自动补齐；"
                            "老数据需要人工决定填充值，请手工迁移后再启用该字段",
                            table.name, column.name)
                        continue
                    await conn.execute(text(_add_column_ddl(table.name, column, dialect)))
                    added.append(f"{table.name}.{column.name}")
                    logger.info("[migrate] 已补齐列 %s.%s", table.name, column.name)

    if added:
        logger.info("[migrate] 本次共补齐 %d 个列：%s",
                    len(added), ", ".join(added))
    return added


def _auto_addable(column) -> bool:
    """可空列，或"老行能机械填默认值"的非空列（见 _default_clause）。"""
    return not column.primary_key and (
        bool(column.nullable) or _default_clause(column) != "")


def _default_literal(column) -> str | None:
    """非空列里可以安全机械补值的子集：JSON 集合列的 list/dict 默认值。

    形如 `Mapped[list] = mapped_column(JSON, default=list)` 的列（附件列表
    之类）语义上"没有就是空集合"，老行填 '[]' 是唯一正确答案；其余非空列
    仍要人来决定老行该填什么，继续跳过并告警。
    """
    if column.nullable or column.primary_key:
        return None
    if not isinstance(column.type, JSON):
        return None
    arg = getattr(column.default, "arg", None)
    # SQLAlchemy 会把 `default=list` 包一层（arg.__wrapped__ 才是内置 list），
    # 直接 `arg is list` 永远不成立
    inner = getattr(arg, "__wrapped__", arg)
    if inner is list:
        return "'[]'"
    if inner is dict:
        return "'{}'"
    return None


def _default_clause(column) -> str:
    literal = _default_literal(column)
    return f" DEFAULT {literal}" if literal else ""


def _add_column_ddl(table_name: str, column, dialect) -> str:
    type_sql = column.type.compile(dialect=dialect)
    return (f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" {type_sql}'
            f'{_default_clause(column)}')
