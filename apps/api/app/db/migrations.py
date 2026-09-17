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

* 只自动补**可空列**。非空且无默认值的列会被跳过并告警——
  这类改动需要人工决策（老行填什么值），不该由启动流程替用户判断。
* 不删除、不改类型、不改约束。它不是迁移框架，只是「让升级不炸」。
"""
from __future__ import annotations

from sqlalchemy import inspect, text

from app.core.logging_config import get_logger
from app.db.base import Base

logger = get_logger(__name__)


async def ensure_columns(engine) -> list[str]:
    """补齐模型里新增、但数据库里还缺的可空列。返回补出的列名列表。"""
    added: list[str] = []
    dialect = engine.dialect

    async with engine.begin() as conn:
        existing_tables = set(
            await conn.run_sync(lambda c: inspect(c).get_table_names()))

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
    """只有可空列才能安全地在线补上。"""
    return bool(column.nullable) and not column.primary_key


def _add_column_ddl(table_name: str, column, dialect) -> str:
    type_sql = column.type.compile(dialect=dialect)
    return f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" {type_sql}'
