"""自由群聊：老板拉任意员工成组；内置「业务群聊 / 产品研发群聊」。

与项目群聊的区别：成员不依赖项目，可随时增删。消息仍走 group_chat_messages
（scope="group"），所以发言逻辑、知识库、搜索工具与项目群聊完全一致。

内置分组来自 content/chat_groups/*.json：第一次点开时缺哪位员工就生成哪位
（绑定专家库人设），生成后就是普通员工，照常培训、升级、调岗。
"""
import json
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_company_capability, get_current_user, get_db
from app.core.permissions import Capability
from app.models.agent import Agent, AgentStatus
from app.models.chat_group import ChatGroup
from app.models.expert_template import ExpertTemplate
from app.services.event_system import log_event

router = APIRouter(prefix="/chat-groups", tags=["chat-groups"])

MAX_MEMBERS = 12


def _groups_dir() -> str:
    cur = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        cand = os.path.join(cur, "content", "chat_groups")
        if os.path.isdir(cand):
            return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "..", "..", "..", "..", "content", "chat_groups")


def _load_preset(pid: str) -> dict:
    path = os.path.join(_groups_dir(), f"{pid}.json")
    if not os.path.isfile(path):
        raise HTTPException(404, f"群聊分组 {pid} 不存在")
    return json.load(open(path, encoding="utf-8"))


def _presets() -> list[dict]:
    d = _groups_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for f in sorted(os.listdir(d)):
        if f.endswith(".json"):
            out.append(json.load(open(os.path.join(d, f), encoding="utf-8")))
    return out


async def _owned_company(db: AsyncSession, user_id: str, company_id: str,
                         capability: Capability = Capability.VIEW):
    """公司门禁：先由 resolve_role 确认关系再放行（越权 404）。

    company_id 来自查询参数，不能只信参数本身。
    """
    return await assert_company_capability(db, user_id, company_id, capability)


async def _agents_by_ids(db: AsyncSession, company_id: str, ids: list[str]) -> list[Agent]:
    """按传入顺序返回员工，跳过已删除/不属于本公司的。"""
    if not ids:
        return []
    rows = (await db.execute(select(Agent).where(
        Agent.id.in_(ids), Agent.company_id == company_id))).scalars().all()
    by_id = {str(a.id): a for a in rows}
    return [by_id[i] for i in ids if i in by_id]


def _member(a: Agent) -> dict:
    return {"id": str(a.id), "name": a.name, "role": a.role}


async def _stats(db: AsyncSession, company_id: str, ids: list[str]) -> dict[str, dict]:
    """每个群的消息数与最后发言时间——左栏排序用。"""
    if not ids:
        return {}
    from app.api.routes.group_chat import GroupChatMessage
    rows = (await db.execute(select(
        GroupChatMessage.scope_id,
        func.count(GroupChatMessage.id),
        func.max(GroupChatMessage.created_at),
    ).where(
        GroupChatMessage.company_id == company_id,
        GroupChatMessage.scope == "group",
        GroupChatMessage.scope_id.in_(ids),
    ).group_by(GroupChatMessage.scope_id))).all()
    out = {}
    for scope_id, count, last in rows:
        out[str(scope_id)] = {
            "message_count": int(count or 0),
            "last_at": last.isoformat() if last else None,
        }
    return out


async def _serialize(db: AsyncSession, company_id: str, group: ChatGroup) -> dict:
    ids = [str(i) for i in (group.agent_ids or [])]
    members = await _agents_by_ids(db, company_id, ids)
    st = (await _stats(db, company_id, [str(group.id)])).get(str(group.id), {})
    return {
        "id": str(group.id),
        "name": group.name,
        "kind": group.kind,
        "preset_id": group.preset_id,
        "emoji": None,
        "hint": None,
        "agent_ids": [str(a.id) for a in members],
        "members": [_member(a) for a in members],
        "missing": [],
        "message_count": st.get("message_count", 0),
        "last_at": st.get("last_at"),
        "created_at": group.created_at.isoformat() if group.created_at else None,
    }


@router.get("")
async def list_groups(company_id: str = Query(...),
                      db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)):
    """内置分组（未创建时给虚拟条目，并标出缺哪些员工）+ 自建分组。"""
    await _owned_company(db, user.id, company_id)
    rows = (await db.execute(select(ChatGroup).where(
        ChatGroup.company_id == company_id))).scalars().all()
    by_preset = {g.preset_id: g for g in rows if g.preset_id}
    custom = [g for g in rows if not g.preset_id]

    agents = (await db.execute(select(Agent).where(
        Agent.company_id == company_id))).scalars().all()
    by_name = {a.name: a for a in agents}

    stats = await _stats(db, company_id, [str(g.id) for g in rows])

    out = []
    for p in _presets():
        existing = by_preset.get(p["id"])
        if existing:
            item = await _serialize(db, company_id, existing)
            item["emoji"] = p.get("emoji")
            item["hint"] = p.get("hint")
            out.append(item)
            continue
        # 还没创建：已存在的同名员工先算进成员，其余标成 missing
        members, missing = [], []
        for a in p.get("agents", []):
            hit = by_name.get(a.get("name") or "")
            if hit:
                members.append(_member(hit))
            elif a.get("name"):
                missing.append({"name": a["name"], "role": a.get("role")})
        out.append({
            "id": f"preset:{p['id']}",
            "name": p.get("name") or p["id"],
            "kind": "preset",
            "preset_id": p["id"],
            "emoji": p.get("emoji"),
            "hint": p.get("hint"),
            "agent_ids": [m["id"] for m in members],
            "members": members,
            "missing": missing,
            "message_count": 0,
            "last_at": None,
            "created_at": None,
        })

    for g in custom:
        item = await _serialize(db, company_id, g)
        st = stats.get(str(g.id), {})
        item["message_count"] = st.get("message_count", 0)
        item["last_at"] = st.get("last_at")
        out.append(item)
    return {"groups": out}


class EnsureIn(BaseModel):
    company_id: str


async def _expert_map(db: AsyncSession, company_id: str, slugs: list[str]) -> dict[str, ExpertTemplate]:
    """slug → 专家模板。DB 里的 source_slug 可能带部门前缀，按「完全相等或以 -slug 结尾」匹配。"""
    if not slugs:
        return {}
    rows = (await db.execute(select(ExpertTemplate).where(
        ExpertTemplate.company_id == company_id,
        ExpertTemplate.enabled.is_(True)))).scalars().all()
    out: dict[str, ExpertTemplate] = {}
    for slug in slugs:
        for r in rows:
            if r.source_slug == slug or (r.source_slug or "").endswith("-" + slug):
                out[slug] = r
                break
    return out


@router.post("/preset/{pid}/ensure")
async def ensure_preset(pid: str, data: EnsureIn,
                        db: AsyncSession = Depends(get_db),
                        user=Depends(get_current_user)):
    """创建/返回内置分组；缺的员工按专家库人设自动生成（幂等）。"""
    await _owned_company(db, user.id, data.company_id, Capability.OPERATE)
    preset = _load_preset(pid)
    roster = preset.get("agents", [])
    if not roster:
        raise HTTPException(400, "该分组没有成员配置")

    group = (await db.execute(select(ChatGroup).where(
        ChatGroup.company_id == data.company_id,
        ChatGroup.preset_id == pid))).scalars().first()

    agents = (await db.execute(select(Agent).where(
        Agent.company_id == data.company_id))).scalars().all()
    by_name = {a.name: a for a in agents}
    slugs = [a.get("expert_slug") for a in roster if a.get("expert_slug")]
    experts = await _expert_map(db, data.company_id, slugs)

    created, skipped = [], []
    for a in roster:
        name = (a.get("name") or "").strip()
        if not name:
            continue
        hit = by_name.get(name)
        if hit:
            skipped.append({"id": str(hit.id), "name": hit.name})
            continue
        tpl = experts.get(a.get("expert_slug") or "")
        agent = Agent(
            company_id=data.company_id,
            name=name,
            role=a.get("role"),
            description=a.get("duties") or (tpl.description if tpl else None),
            system_prompt=(tpl.system_prompt if tpl else None),
            expert_template_id=tpl.id if tpl else None,
            memory_config={"group_preset": pid},
            status=AgentStatus.IDLE,
        )
        db.add(agent)
        await db.flush()
        by_name[name] = agent
        created.append({"id": str(agent.id), "name": agent.name, "role": agent.role})
        await log_event(db, "agent.created", "agent", str(agent.id), "user", user.id,
                        {"name": agent.name, "source": f"chat_group:{pid}"})

    member_ids = [str(by_name[a["name"]].id) for a in roster
                  if a.get("name") and a["name"] in by_name]
    if group is None:
        group = ChatGroup(
            id=str(uuid.uuid4()),
            company_id=data.company_id,
            name=preset.get("name") or pid,
            kind="preset",
            preset_id=pid,
            agent_ids=member_ids,
            created_by=user.id,
        )
        db.add(group)
    else:
        group.agent_ids = member_ids
    await db.commit()
    await db.refresh(group)

    item = await _serialize(db, data.company_id, group)
    item["emoji"] = preset.get("emoji")
    item["hint"] = preset.get("hint")
    return {"group": item, "created": created, "reused": skipped}


class GroupIn(BaseModel):
    company_id: str
    name: str = Field(min_length=1, max_length=60)
    agent_ids: list[str] = Field(min_length=1, max_length=MAX_MEMBERS)


class GroupPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    agent_ids: list[str] | None = Field(default=None, min_length=1, max_length=MAX_MEMBERS)


@router.post("", status_code=201)
async def create_group(data: GroupIn,
                       db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    await _owned_company(db, user.id, data.company_id, Capability.OPERATE)
    members = await _agents_by_ids(db, data.company_id, data.agent_ids)
    if len(members) != len(set(data.agent_ids)):
        raise HTTPException(400, "有员工不存在或不属于当前公司")
    group = ChatGroup(
        id=str(uuid.uuid4()),
        company_id=data.company_id,
        name=data.name.strip(),
        kind="custom",
        agent_ids=[str(a.id) for a in members],
        created_by=user.id,
    )
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return await _serialize(db, data.company_id, group)


@router.patch("/{gid}")
async def update_group(gid: str, data: GroupPatch,
                       db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    group = await db.get(ChatGroup, gid)
    if not group:
        raise HTTPException(404, "群聊不存在")
    await _owned_company(db, user.id, group.company_id, Capability.OPERATE)
    if data.name is not None:
        group.name = data.name.strip()
    if data.agent_ids is not None:
        members = await _agents_by_ids(db, group.company_id, data.agent_ids)
        if len(members) != len(set(data.agent_ids)):
            raise HTTPException(400, "有员工不存在或不属于当前公司")
        group.agent_ids = [str(a.id) for a in members]
    await db.commit()
    await db.refresh(group)
    return await _serialize(db, group.company_id, group)


@router.delete("/{gid}", status_code=204)
async def delete_group(gid: str,
                       db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    group = await db.get(ChatGroup, gid)
    if not group:
        raise HTTPException(404, "群聊不存在")
    # 解散群聊属于组织级删除：MANAGE（公司管理员可做），不走 DESTRUCTIVE。
    await _owned_company(db, user.id, group.company_id, Capability.MANAGE)
    from app.api.routes.group_chat import GroupChatMessage
    from sqlalchemy import delete
    await db.execute(delete(GroupChatMessage).where(
        GroupChatMessage.scope == "group", GroupChatMessage.scope_id == gid))
    await db.delete(group)
    await db.commit()
    return None
