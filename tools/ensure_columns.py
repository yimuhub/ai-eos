"""一次性诊断 + 补列脚本（Postgres 模式）。

对比 Base.metadata 与实际表结构，给缺失的列加上去：
  - 模型列有默认值 → ADD COLUMN <name> <type> DEFAULT <default>
  - 否则 → ADD COLUMN <name> <type> NULL
幂等：已有列自动跳过。用完可删。
"""
import asyncio

from sqlalchemy import inspect, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.types import (BigInteger, Boolean, Date, DateTime, Float,
                              Integer, SmallInteger, Text)

from app.db.base import Base
from app.db.session import engine

import app.models  # noqa: F401 注册全部模型

_SQL_TYPES = {
    Boolean: "BOOLEAN",
    BigInteger: "BIGINT",
    Date: "DATE",
    Float: "DOUBLE PRECISION",
    Integer: "INTEGER",
    SmallInteger: "SMALLINT",
    Text: "TEXT",
    JSONB: "JSONB",
}


def _col_sql(col):
    t = col.type
    # 复合/特化类型逐个匹配
    sql = None
    for py, s in _SQL_TYPES.items():
        if isinstance(t, py):
            sql = s
            break
    if sql is None:
        # 其他（Enum 字符串、UUID、String(n)、DateTime tz、JSON、Interval...）
        try:
            s = t.compile(dialect=engine.dialect).string.upper()
            s = s.replace("TIMESTAMP WITHOUT TIME ZONE", "TIMESTAMP").replace("TIMESTAMPTZ", "TIMESTAMP")
            if s.startswith("VARCHAR"):
                s = "TEXT"
            if s.startswith("UUID"):
                s = "UUID"
            if s in ("JSON", "JSONB"):
                s = "JSONB"
            sql = s
        except Exception:
            sql = "JSONB"
    parts = [f'"{col.name}"', sql]
    d = col.default.arg if hasattr(col.default, "arg") else None
    if isinstance(d, str):
        parts.append(f"DEFAULT '{d}'")
    elif isinstance(d, (int, float)) and not isinstance(d, bool):
        parts.append(f"DEFAULT {d}")
    elif d is True or d is False:
        parts.append(f"DEFAULT {str(d).upper()}")
    elif col.nullable:
        parts.append("NULL")
    else:
        parts.append("NOT NULL")
    return " ".join(parts)


async def main():
    # 第一步：新表交给 create_all（幂等，已有表不动）
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c))
    # 第二步：老表补缺失列。表名/列清单都必须在 run_sync 内取（避免 greenlet）
    added = []
    async with engine.connect() as conn:
        existing_tables = await conn.run_sync(
            lambda c: set(inspect(c).get_table_names()))

        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            cols = await conn.run_sync(
                lambda c, n=table.name: {x["name"] for x in inspect(c).get_columns(n)})
            for col in table.columns:
                if col.name in cols:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN {_col_sql(col)}'
                print(f"  [补列] {table.name}.{col.name} → {ddl.split('ADD COLUMN')[1].strip()}")
                try:
                    await conn.execute(text(ddl))
                    added.append(f"{table.name}.{col.name}")
                except Exception as e:
                    print(f"    失败: {str(e)[:100]}")
        await conn.commit()
    print(f"\n✅ 共补齐 {len(added)} 列: {added or '无'}")


asyncio.run(main())
