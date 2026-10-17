"""Health check endpoints."""
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.logging_config import get_logger
from app.db.session import engine

logger = get_logger(__name__)

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok", "service": "ai-eos-api"}


@router.get("/health/db")
async def health_db():
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}
    except Exception as e:
        # 健康检查失败必须返回 503：探针/负载均衡按状态码判断，
        # 200 + body error 会被误判为健康。
        logger.warning("/health/db 异常: %s", e, exc_info=True)
        return JSONResponse(status_code=503,
                            content={"status": "error", "database": str(e)})
