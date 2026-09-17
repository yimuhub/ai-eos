"""App Alias: 本机已装应用扫描 + 自然语言别名规则 CRUD + 启动应用。"""
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_current_user
from app.app_registry import scan_apps, get_platform, build_launch_command, match_app
from app.models.app_alias import AppAliasRule
from app.models.user import User

router = APIRouter(prefix="/app-alias", tags=["app_alias"])


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _id():
    import uuid
    return str(uuid.uuid4())


class AliasIn(BaseModel):
    alias: str
    app_name: str
    app_path: str | None = None


class AliasOut(BaseModel):
    id: str
    company_id: str
    alias: str
    app_name: str
    app_path: str | None
    created_at: datetime


class MatchOut(BaseModel):
    matched: bool
    name: str | None
    path: str | None
    via: str
    platform: str


def _user_dep(user: Annotated[User, Depends(get_current_user)]):
    return user


@router.get("/platform")
async def get_platform_info(company_id: str = Query(...)):
    return {"os": get_platform()["os"], "os_name": get_platform()["os_name"],
            "os_version": get_platform()["os_version"], "machine": get_platform()["machine"]}


@router.get("/installed")
async def list_installed(company_id: str = Query(...),
                         user: Annotated[User, Depends(get_current_user)] = None,
                         db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(AppAliasRule).where(AppAliasRule.company_id == company_id)
    )).scalars().all()
    installed = scan_apps()
    rule_paths = {r.app_name.lower(): r.app_path for r in rows if r.app_path}
    out = []
    for a in installed:
        path = rule_paths.get(a["name"].lower(), a["path"])
        out.append({"name": a["name"], "path": path,
                    "source": a.get("source", ""), "terminal": a.get("terminal")})
    return {"platform": get_platform(), "installed": out}


@router.get("/rules", response_model=list[AliasOut])
async def list_rules(company_id: str = Query(...),
                     user: Annotated[User, Depends(get_current_user)] = None,
                     db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(AppAliasRule).where(AppAliasRule.company_id == company_id)
        .order_by(AppAliasRule.created_at.desc())
    )).scalars().all()
    return rows


@router.post("/rules", response_model=AliasOut)
async def create_rule(data: AliasIn, company_id: str = Query(...),
                      user: Annotated[User, Depends(get_current_user)] = None,
                      db: AsyncSession = Depends(get_db)):
    alias = data.alias.strip().lower()
    if not alias:
        raise HTTPException(400, "口语词不能为空")
    existing = (await db.execute(select(AppAliasRule).where(
        AppAliasRule.company_id == company_id,
        AppAliasRule.alias == alias,
    ))).scalar_one_or_none()
    if existing:
        raise HTTPException(400, f"口语词「{data.alias}」已存在")
    rule = AppAliasRule(
        id=_id(),
        company_id=company_id,
        alias=alias,
        app_name=data.app_name.strip(),
        app_path=data.app_path.strip() if data.app_path else None,
        created_by=user.id if user else None,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return rule


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: str, company_id: str = Query(...),
                      user: Annotated[User, Depends(get_current_user)] = None,
                      db: AsyncSession = Depends(get_db)):
    r = await db.get(AppAliasRule, rule_id)
    if not r or r.company_id != company_id:
        raise HTTPException(404, "not found")
    await db.delete(r)
    await db.commit()
    return {"ok": True}


@router.post("/match", response_model=MatchOut)
async def match_intent(data: dict, company_id: str = Query(...),
                       user: Annotated[User, Depends(get_current_user)] = None,
                       db: AsyncSession = Depends(get_db)):
    text = (data.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "empty text")
    rules = (await db.execute(select(AppAliasRule).where(
        AppAliasRule.company_id == company_id,
    ))).scalars().all()
    installed = scan_apps()
    resolved = match_app(text,
                         custom_rules=[{"alias": r.alias, "app_name": r.app_name,
                                        "app_path": r.app_path} for r in rules],
                         installed=installed)
    if not resolved:
        return MatchOut(matched=False, name=None, path=None,
                        via="", platform=get_platform()["os"])
    return MatchOut(matched=True, name=resolved["name"], path=resolved.get("path"),
                    via=resolved["via"], platform=get_platform()["os"])


@router.post("/run")
async def run_app(data: dict, company_id: str = Query(...),
                  user: Annotated[User, Depends(get_current_user)] = None,
                  db: AsyncSession = Depends(get_db)):
    text = (data.get("text") or "").strip()
    rule_id = data.get("rule_id")
    if not text and not rule_id:
        raise HTTPException(400, "empty request")
    if rule_id:
        r = await db.get(AppAliasRule, rule_id)
        if not r or r.company_id != company_id:
            raise HTTPException(404, "rule not found")
        name, path, via = r.app_name, r.app_path, "自定义规则"
    else:
        rules = (await db.execute(select(AppAliasRule).where(
            AppAliasRule.company_id == company_id,
        ))).scalars().all()
        installed = scan_apps()
        resolved = match_app(text,
                             custom_rules=[{"alias": r.alias, "app_name": r.app_name,
                                            "app_path": r.app_path} for r in rules],
                             installed=installed)
        if not resolved:
            raise HTTPException(404, f"未找到对应应用: {text}")
        name, path, via = resolved["name"], resolved.get("path"), resolved["via"]
    cmd, _ = build_launch_command(path, name)
    import asyncio
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), timeout=10)
        ok = proc.returncode == 0
        msg = f"✅ 已打开 {name}" if ok else f"❌ 打不开 {name}: {err.decode()[:120]}"
    except asyncio.TimeoutError:
        msg = f"⏳ 正在启动 {name}（超时，可能已启动）"
        ok = True
    except Exception as e:
        msg = f"❌ 执行失败: {type(e).__name__}: {e}"
        ok = False
    return {"ok": ok, "name": name, "cmd": cmd, "via": via, "message": msg}
