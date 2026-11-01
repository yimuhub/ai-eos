"""B5「公司大脑」：租户隔离的全局检索（关键词 + 本地语义重排）。

``mode=keyword`` 保持纯 SQL LIKE 召回；``mode=hybrid``（默认）先按公司
取最近 50 条候选，再用 :mod:`app.services.semantic_search` 的同义词扩展
+ BM25 本地重排——不调用模型、不发网络请求，真 embedding 留 TODO。

设计约束（都是安全边界，不是风格偏好）：

* 先 ``assert_company_owned`` 再查询——无权限与他司一律 404，
  与「公司不存在」不可区分，避免用状态码探测租户；
* 每条 SQL 都强制 ``company_id ==``，绝不依赖调用方只传自己的 id；
* LIKE 使用 ``lower(col) LIKE lower(:kw) ESCAPE '\\'``，并转义
  ``%`` / ``_`` / ``\\``，避免 ``a%a`` 这类输入被当通配符放大成全表；
* 每类最多 5 条、总计最多 20 条，避免命令面板一次拖回整库。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_owned, get_current_user, get_db
from app.models.asset import Asset
from app.models.knowledge import KBDocument
from app.models.memory import AgentMemory
from app.models.task import Task
from app.models.user import User
from app.schemas.responses import SearchOut
from app.services.semantic_search import rank

router = APIRouter(prefix="/search", tags=["search"])

MIN_QUERY_LEN = 2
MAX_QUERY_LEN = 100
PER_TYPE_LIMIT = 5
TOTAL_LIMIT = 20
SNIPPET_LEN = 120
# hybrid 先取最近 N 条做本地重排；上限固定，避免命令面板一次拖回整库。
HYBRID_CANDIDATE_LIMIT = 50
MODE_CHOICES = ("keyword", "hybrid")

# (type, label, href) —— href 只指向现有前端页面。
GROUP_SPECS: tuple[tuple[str, str, str], ...] = (
    ("task", "任务", "/tasks"),
    ("memory", "记忆", "/memory"),
    ("knowledge", "知识库", "/knowledge"),
    ("asset", "资产", "/assets"),
)
_GROUP_BY_TYPE = {spec[0]: spec for spec in GROUP_SPECS}


def _escape_like(keyword: str) -> str:
    """转义 LIKE 通配符：反斜杠必须最先替换，否则会把后加的转义再转一遍。"""
    return (
        keyword.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _like_pattern(keyword: str):
    """`lower(:kw)` 绑定参数；SQL 侧为 lower(col) LIKE lower(:kw) ESCAPE '\\'。"""
    return func.lower(f"%{_escape_like(keyword)}%")


def _matches(keyword: str, *columns):
    return or_(*[
        func.lower(column).like(_like_pattern(keyword), escape="\\")
        for column in columns
    ])


def _window(text: str, index: int) -> str:
    """截取命中词附近的片段；保留上下文，避免只看到关键词本身。"""
    if len(text) <= SNIPPET_LEN:
        return text
    start = max(0, index - 20)
    end = min(len(text), start + SNIPPET_LEN)
    start = max(0, end - SNIPPET_LEN)
    out = text[start:end]
    if start > 0:
        out = "…" + out
    if end < len(text):
        out += "…"
    return out


def _snippet(keyword: str, *texts: str | None) -> str:
    """优先取真正包含关键词的字段做片段，其次退化为第一个非空字段。"""
    flat: list[str] = []
    for text in texts:
        if text:
            # 折叠换行/多空白，前端一行展示不会出现空洞
            flat.append(" ".join(str(text).split()))
    lowered = keyword.lower()
    for text in flat:
        index = text.lower().find(lowered)
        if index >= 0:
            return _window(text, index)
    return _window(flat[0], 0) if flat else ""


def _short(text: str, limit: int = 48) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _task_item(row: Task, keyword: str) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "snippet": _snippet(keyword, row.description, row.title),
        "href": "/tasks",
    }


def _memory_item(row: AgentMemory, keyword: str) -> dict:
    return {
        "id": row.id,
        "title": _short(row.content),
        "snippet": _snippet(keyword, row.content),
        "href": "/memory",
    }


def _knowledge_item(row: KBDocument, keyword: str) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "snippet": _snippet(keyword, row.content, row.summary, row.title),
        "href": "/knowledge",
    }


def _asset_item(row: Asset, keyword: str) -> dict:
    kind = row.type.value if row.type else "文件"
    mime = row.mime_type or "未知类型"
    return {
        "id": row.id,
        "title": row.name,
        "snippet": f"{kind} · {mime}",
        "href": "/assets",
    }


def _ranked_items(rows, keyword: str, doc_builder, item_builder) -> list[dict]:
    """对候选行做本地重排，并给每项补上 ``score``。

    ``rows`` 已经是 SQL 过滤后的本公司候选（顺序即同分时的原始顺序）；
    这里只做打分与截断，不接触数据库，因此不会扩大租户可见范围。
    """
    docs = [doc_builder(row) for row in rows]
    items: list[dict] = []
    for index, score in rank(keyword, docs):
        item = item_builder(rows[index], keyword)
        item["score"] = round(score, 4)
        items.append(item)
        if len(items) >= PER_TYPE_LIMIT:
            break
    return items


async def _search_tasks(db: AsyncSession, company_id: str,
                        keyword: str, mode: str = "keyword") -> list[dict]:
    if mode == "hybrid":
        rows = (await db.execute(
            select(Task)
            .where(Task.company_id == company_id)
            .order_by(Task.created_at.desc(), Task.id.desc())
            .limit(HYBRID_CANDIDATE_LIMIT)
        )).scalars().all()
        return _ranked_items(
            rows, keyword,
            lambda row: (row.title or "", row.description or ""),
            _task_item,
        )
    rows = await db.execute(
        select(Task)
        .where(
            Task.company_id == company_id,
            _matches(keyword, Task.title, Task.description),
        )
        .order_by(Task.created_at.desc())
        .limit(PER_TYPE_LIMIT)
    )
    return [_task_item(row, keyword) for row in rows.scalars().all()]


async def _search_memories(db: AsyncSession, company_id: str,
                           keyword: str, mode: str = "keyword") -> list[dict]:
    if mode == "hybrid":
        rows = (await db.execute(
            select(AgentMemory)
            .where(AgentMemory.company_id == company_id)
            .order_by(AgentMemory.created_at.desc(), AgentMemory.id.desc())
            .limit(HYBRID_CANDIDATE_LIMIT)
        )).scalars().all()
        return _ranked_items(
            rows, keyword,
            lambda row: (_short(row.content), row.content or ""),
            _memory_item,
        )
    rows = await db.execute(
        select(AgentMemory)
        .where(
            AgentMemory.company_id == company_id,
            _matches(keyword, AgentMemory.content),
        )
        .order_by(AgentMemory.created_at.desc())
        .limit(PER_TYPE_LIMIT)
    )
    return [_memory_item(row, keyword) for row in rows.scalars().all()]


async def _search_knowledge(db: AsyncSession, company_id: str,
                            keyword: str, mode: str = "keyword") -> list[dict]:
    if mode == "hybrid":
        rows = (await db.execute(
            select(KBDocument)
            .where(KBDocument.company_id == company_id)
            .order_by(KBDocument.created_at.desc(), KBDocument.id.desc())
            .limit(HYBRID_CANDIDATE_LIMIT)
        )).scalars().all()
        return _ranked_items(
            rows, keyword,
            lambda row: (
                row.title or "",
                f"{row.content or ''} {row.summary or ''}".strip(),
            ),
            _knowledge_item,
        )
    rows = await db.execute(
        select(KBDocument)
        .where(
            KBDocument.company_id == company_id,
            _matches(keyword, KBDocument.title, KBDocument.content,
                     KBDocument.summary),
        )
        .order_by(KBDocument.created_at.desc())
        .limit(PER_TYPE_LIMIT)
    )
    return [_knowledge_item(row, keyword) for row in rows.scalars().all()]


async def _search_assets(db: AsyncSession, company_id: str,
                         keyword: str, mode: str = "keyword") -> list[dict]:
    if mode == "hybrid":
        rows = (await db.execute(
            select(Asset)
            .where(Asset.company_id == company_id)
            .order_by(Asset.created_at.desc(), Asset.id.desc())
            .limit(HYBRID_CANDIDATE_LIMIT)
        )).scalars().all()
        # 资产只有名称可检索，正文留空，避免用类型/扩展名造出假命中。
        return _ranked_items(
            rows, keyword,
            lambda row: (row.name or "", ""),
            _asset_item,
        )
    rows = await db.execute(
        select(Asset)
        .where(
            Asset.company_id == company_id,
            _matches(keyword, Asset.name),
        )
        .order_by(Asset.created_at.desc())
        .limit(PER_TYPE_LIMIT)
    )
    return [_asset_item(row, keyword) for row in rows.scalars().all()]


_SEARCHERS = {
    "task": _search_tasks,
    "memory": _search_memories,
    "knowledge": _search_knowledge,
    "asset": _search_assets,
}


@router.get("", response_model=SearchOut, response_model_exclude_none=True)
async def search_company(
    company_id: str = Query(...),
    q: str = Query(...),
    types: str | None = Query(None),
    mode: str = Query("hybrid"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """检索本公司的任务 / 记忆 / 知识文档 / 资产。

    ``mode=keyword`` 走原有 SQL LIKE；``mode=hybrid``（默认）在最近 50 条
    候选上做同义词 + BM25 本地重排，不调用任何模型。``types`` 省略时检索
    全部；只接受 ``task,memory,knowledge,asset``，未知类型返回 400（调用方
    拼错时早失败，而不是静默少搜一类）。
    """
    # 先鉴权再查数据：他司 / 不存在都返回同一个 404，避免租户探测。
    await assert_company_owned(db, user.id, company_id)

    mode = (mode or "").strip().lower()
    if mode not in MODE_CHOICES:
        raise HTTPException(400, f"未知的检索模式：{mode or '(空)'}")

    keyword = (q or "").strip()
    if not (MIN_QUERY_LEN <= len(keyword) <= MAX_QUERY_LEN):
        raise HTTPException(
            400,
            f"q 去除空白后需为 {MIN_QUERY_LEN}–{MAX_QUERY_LEN} 个字符",
        )

    requested = [part.strip() for part in (types or "").split(",") if part.strip()]
    unknown = [part for part in requested if part not in _GROUP_BY_TYPE]
    if unknown:
        raise HTTPException(400, f"未知的检索类型：{','.join(unknown)}")
    selected = [spec for spec in GROUP_SPECS
                if not requested or spec[0] in requested]

    groups: list[dict] = []
    total = 0
    for type_name, label, _href in selected:
        if total >= TOTAL_LIMIT:
            break
        searcher = _SEARCHERS[type_name]
        items = await searcher(db, company_id, keyword, mode)
        if not items:
            continue
        items = items[: min(PER_TYPE_LIMIT, TOTAL_LIMIT - total)]
        total += len(items)
        groups.append({"type": type_name, "label": label, "items": items})

    # 空结果是正常业务态，返回空 groups（200），不是 404。
    return {"query": keyword, "groups": groups}
