"""Auth endpoints."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import get_db
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterIn(BaseModel):
    email: EmailStr
    username: str
    password: str
    display_name: str | None = None


class LoginIn(BaseModel):
    identity: str  # 支持邮箱或用户名
    password: str


@router.post("/register")
async def register(data: RegisterIn, db: AsyncSession = Depends(get_db)):
    exists = await db.execute(
        select(User).where((User.email == data.email) | (User.username == data.username))
    )
    if exists.scalar_one_or_none():
        raise HTTPException(409, "Email or username already registered")
    user = User(
        email=data.email,
        username=data.username,
        password_hash=hash_password(data.password),
        display_name=data.display_name or data.username,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return {"id": user.id, "email": user.email, "username": user.username}


@router.post("/login")
async def login(data: LoginIn, db: AsyncSession = Depends(get_db)):
    # 支持邮箱或用户名登录
    result = await db.execute(
        select(User).where((User.email == data.identity) | (User.username == data.identity))
    )
    user = result.scalar_one_or_none()
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(401, "Invalid credentials")
    return {
        "access_token": create_access_token(user.id),
        "token_type": "bearer",
        "user": {"id": user.id, "email": user.email, "username": user.username,
                 "display_name": user.display_name},
    }


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    return {"id": user.id, "email": user.email, "username": user.username,
            "display_name": user.display_name, "avatar_url": user.avatar_url}


@router.post("/logout")
async def logout():
    # JWT is stateless — client drops the token.
    return {"detail": "logged out"}
