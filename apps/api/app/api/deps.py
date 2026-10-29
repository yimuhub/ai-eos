"""FastAPI dependencies: current user + 租户归属校验。"""
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.db.session import get_db
from app.models.company import Company
from app.models.user import User

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    user_id = decode_token(credentials.credentials)
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or disabled")
    return user


async def assert_company_owned(db: AsyncSession, user_id: str,
                               company_id: str) -> Company:
    """校验公司归属；不属于该用户则 404（不泄露资源是否存在）。

    改造新增：此前多个路由各自手写 `_owned`，语义略有出入，
    且部分写操作（如训练撤销）完全没有归属校验——知道 id 就能操作他人数据。
    统一到一个实现，新增端点直接复用，避免再次遗漏。
    """
    if not company_id:
        raise HTTPException(400, "company_id is required")
    company = await db.get(Company, company_id)
    if not company or company.owner_id != user_id:
        raise HTTPException(404, "Company not found")
    return company


async def is_company_owned(db: AsyncSession, user_id: str,
                           company_id: str) -> bool:
    """assert_company_owned 的无异常版本。

    用于批量场景（如复制文档）：对不属于调用者的行按"不存在"跳过，
    不抛 404，避免通过差异状态码泄露资源存在性。
    """
    if not company_id:
        return False
    company = await db.get(Company, company_id)
    return bool(company) and company.owner_id == user_id


async def assert_kb_doc_owned(db: AsyncSession, user_id: str, doc_id: str):
    """校验知识库文档归属（经由其所属公司）；不属于该用户则 404。

    kb2 的 documents 级端点（编辑/删除/复制）共用同一模式，
    收敛到这里，避免每处手写 _owned 出现语义出入。
    """
    from app.models.knowledge import KBDocument

    doc = await db.get(KBDocument, doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    await assert_company_owned(db, user_id, doc.company_id)
    return doc


async def assert_agent_owned(db: AsyncSession, user_id: str,
                             agent_id: str):
    """校验 Agent 归属（经由其所属公司）。返回 Agent 行。"""
    from app.models.agent import Agent

    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    await assert_company_owned(db, user_id, agent.company_id)
    return agent
