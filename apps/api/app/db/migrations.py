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

* 只自动补**可空列**，以及非空但"老行有唯一正确答案"的列
  （JSON 集合列 → 补 '[]'/'{}'；版本/计数等语义确定的列见 `_SAFE_BACKFILL`
  白名单）。其余非空列会被跳过并告警——这类改动需要人工决策（老行填什么值），
  不该由启动流程替用户判断。
* 不删除、不改类型、不改约束。它不是迁移框架，只是「让升级不炸」。
* **只在 SQLite 方言下运行**。PostgreSQL 的结构变更统一走 Alembic
  迁移，避免两套机制双轨打架（同一列被两边各加一次）。
"""
from __future__ import annotations

from sqlalchemy import JSON, inspect, text

from app.core.logging_config import get_logger
from app.db.base import Base

logger = get_logger(__name__)

# 本次（P5）新增列/新表所依赖的索引：(表, 列, 索引名)。
#
# 为什么要显式列出来：`create_all` 只给**新建**的表建索引；
# 表已存在时它连索引都不会补，于是"给老表加一列 + 想按它过滤"会退化成全表扫。
# 名字用 SQLAlchemy `index=True` 的默认格式，这样 create_all 建过的库上
# `CREATE INDEX IF NOT EXISTS` 是纯 no-op，不会出现同名不同定义的两套索引。
_NEW_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("users", "is_superadmin", "ix_users_is_superadmin"),
    ("agents", "visibility", "ix_agents_visibility"),
    ("agents", "created_by_user_id", "ix_agents_created_by_user_id"),
    ("departments", "data_domain_code", "ix_departments_data_domain_code"),
    ("company_members", "company_id", "ix_company_members_company_id"),
    ("company_members", "user_id", "ix_company_members_user_id"),
    ("department_members", "department_id", "ix_department_members_department_id"),
    ("department_members", "user_id", "ix_department_members_user_id"),
    # CEO 统筹：老库给 tasks 补 mission_id 时索引不会自动建
    ("tasks", "mission_id", "ix_tasks_mission_id"),
    # CEO 自动招人：老库补 agents.group_id；agent_groups 是新表，create_all 会建索引
    ("agents", "group_id", "ix_agents_group_id"),
    ("agent_groups", "company_id", "ix_agent_groups_company_id"),
    ("agent_groups", "department_id", "ix_agent_groups_department_id"),
    # CEO 工作流：老库补版本/复用键/统筹关联；workflow_feedback 是新表
    ("workflow_definitions", "owner_agent_id",
     "ix_workflow_definitions_owner_agent_id"),
    ("workflow_definitions", "reuse_key",
     "ix_workflow_definitions_reuse_key"),
    ("ceo_missions", "workflow_id", "ix_ceo_missions_workflow_id"),
)


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

        # 索引：与补列同一事务、同一幂等语义（CREATE INDEX IF NOT EXISTS，
        # sqlite / pg 均支持）。只处理"表与列都真实存在"的项——
        # 新表交给 create_all，不在这里顺手建半张表。
        await _ensure_indexes(conn, existing_tables)

    if added:
        logger.info("[migrate] 本次共补齐 %d 个列：%s",
                    len(added), ", ".join(added))
    return added


async def _ensure_indexes(conn, existing_tables: set[str]) -> list[str]:
    """幂等补索引。返回本次执行过 DDL 的索引名（含已存在的，仅供日志）。

    刻意**不**并入 `ensure_columns` 的返回值：那个列表的语义是
    "本次补出了哪些列"，把它混进索引名会让"第二次启动应为空"的断言失真。
    """
    touched: list[str] = []
    for table_name, column_name, index_name in _NEW_INDEXES:
        if table_name not in existing_tables:
            continue
        present = await conn.run_sync(
            lambda c, name=table_name:
            {col["name"] for col in inspect(c).get_columns(name)})
        if column_name not in present:
            continue
        await conn.execute(text(
            f'CREATE INDEX IF NOT EXISTS "{index_name}" '
            f'ON "{table_name}" ("{column_name}")'))
        touched.append(index_name)
    return touched


async def backfill_superadmin(engine) -> str | None:
    """升级到"有超管"的版本时，把历史安装标记出一个超管。

    背景：安装向导（P5）之前没有"平台超管"这一概念，所有公司都是
    owner 直接用。新版本用 `users.is_superadmin` 判定"是否已初始化"，
    若老库一个超管都没有，界面会误以为"这是全新安装"并要求重新建公司。

    规则（与契约一致）：不存在 is_superadmin=True 的用户 → 取**最早**的
    `companies.owner_id` 标记为超管。返回被提升的用户 id；无变更返回 None。

    * 幂等：已有超管直接返回 None；重复启动不会改动任何数据。
    * 不抛异常：启动流程不该被一次回填失败拖住，失败只记一条 warning。
    * 不新增任何列/表：只做一次 UPDATE。
    * **调用顺序**：必须在 `ensure_columns(engine)` 之后调用——老库的
      `users.is_superadmin` 列是那一步补出来的；列还不存在时本函数会安全跳过
      （返回 None），不会为了回填自己去 ALTER。
    """
    try:
        async with engine.begin() as conn:
            tables = set(await conn.run_sync(
                lambda c: inspect(c).get_table_names()))
            if "users" not in tables or "companies" not in tables:
                return None  # 全新库 / 尚未建表：create_all 之后会有新一轮启动
            user_columns = await conn.run_sync(
                lambda c: {col["name"] for col in inspect(c).get_columns("users")})
            if "is_superadmin" not in user_columns:
                return None  # 补列没跑（非 sqlite/pg）：交给对应迁移机制

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

            user_id = row[0]
            updated = await conn.execute(text(
                "UPDATE users SET is_superadmin = :v WHERE id = :uid"),
                {"v": True, "uid": user_id})
            if not updated.rowcount:
                return None  # owner_id 指向的用户已不存在（脏数据），不猜
            logger.info("[migrate] 历史安装无超管，已把最早的 owner(%s) 提升为超管",
                        user_id)
            return user_id
    except Exception as exc:  # noqa: BLE001 — 回填失败绝不能阻塞启动
        logger.warning("[migrate] 超管回填跳过（不影响启动）：%s", exc)
        return None


def _auto_addable(column) -> bool:
    """可空列，或"老行能机械填默认值"的非空列（见 _default_clause）。"""
    return not column.primary_key and (
        bool(column.nullable) or _default_clause(column) != "")


# 非空标量列的补值白名单：只收「老行填什么没有歧义」的字段。
#
# 为什么不按 `column.default` 一刀切：默认值是给**新行**用的，
# 老行可能处于一个当时还没有该概念的状态，机械填进去反而制造错误事实
# （比如给历史订单补上 status='PENDING'）。这里逐个确认语义后才放行。
_SAFE_BACKFILL: dict[tuple[str, str], str] = {
    # 工作流版本化（0008）：历史流程都是第一版、非 CEO 生成、从未跑过
    ("workflow_definitions", "version"): "1",
    ("workflow_definitions", "source"): "'manual'",
    ("workflow_definitions", "run_count"): "0",
    ("workflow_definitions", "success_count"): "0",
    ("workflow_definitions", "failure_count"): "0",
}


def _default_literal(column) -> str | None:
    """非空列里可以安全机械补值的子集。

    ① 白名单里的版本/计数列；
    ② JSON 集合列的 list/dict 默认值（"没有就是空集合"）。
    其余非空列仍要人来决定老行该填什么，继续跳过并告警。
    """
    if column.nullable or column.primary_key:
        return None
    table_name = getattr(getattr(column, "table", None), "name", "")
    allowed = _SAFE_BACKFILL.get((table_name, column.name))
    if allowed is not None:
        return allowed
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
