"""只有文本模型能当员工的对话模型。

图片/视频/音频/向量模型走的是别的接口（如 /v1/images/generations），把它们
绑成员工的首选模型后，每一轮对话都是上游 400，而响应体往往只有一句
Bad Request——用户看到的是「员工全都不会说话」，排查方向还全错。
这里钉住三件事：绑定入口拒、自动挑模型时不选、被拒的切换不能顺手删掉旧绑定。
"""
from __future__ import annotations

import uuid

from sqlalchemy import or_, select

from app.db.session import AsyncSessionLocal
from app.model_discovery import (chat_model_condition, is_chat_model,
                                 type_label)
from app.models.agent import (Agent, AgentModelConfig, Model,
                              ModelProviderType, ModelType)

VALID_URL = "http://127.0.0.1:11434/v1"


async def _model(company_id: str, model_type: str | None) -> dict:
    """直接落一行模型：POST /models 不接受 model_type（类型由拉取/配置写入）。"""
    mid = uuid.uuid4().hex
    async with AsyncSessionLocal() as s:
        s.add(Model(id=mid, company_id=company_id,
                    name=f"{type_label(model_type)}模型-{mid[:5]}",
                    provider_type=ModelProviderType.OPENAI_COMPAT,
                    base_url=VALID_URL, api_key_env="OPENAI_COMPAT_API_KEY",
                    model_id=f"m-{mid[:5]}", model_type=model_type))
        await s.commit()
    return {"id": mid, "name": f"{type_label(model_type)}模型-{mid[:5]}"}


async def _agent(company_id: str) -> str:
    aid = uuid.uuid4().hex
    async with AsyncSessionLocal() as s:
        s.add(Agent(id=aid, company_id=company_id, name=f"员工-{aid[:5]}"))
        await s.commit()
    return aid


def test_only_text_models_count_as_chat_models():
    assert is_chat_model(None)                    # 升级前登记的只有聊天模型
    assert is_chat_model(ModelType.TEXT.value)
    for t in (ModelType.IMAGE, ModelType.VIDEO, ModelType.AUDIO,
              ModelType.EMBEDDING, ModelType.OTHER):
        assert not is_chat_model(t.value), t.value


async def test_chat_model_condition_skips_non_text_rows(tenants):
    co = tenants.alice_co["id"]
    text = await _model(co, ModelType.TEXT.value)
    legacy = await _model(co, None)
    image = await _model(co, ModelType.IMAGE.value)
    embedding = await _model(co, ModelType.EMBEDDING.value)
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(Model.id).where(or_(Model.company_id == co,
                                       Model.company_id.is_(None)),
                                   chat_model_condition()))).scalars().all()
    assert {text["id"], legacy["id"]} <= set(rows)
    assert image["id"] not in rows and embedding["id"] not in rows


async def test_batch_bind_refuses_non_chat_model(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    image = await _model(co["id"], ModelType.IMAGE.value)
    text = await _model(co["id"], ModelType.TEXT.value)
    await _agent(co["id"])

    r = await client.post(f"/models/{image['id']}/bind-agents",
                          json={"company_id": co["id"]}, headers=alice["headers"])
    assert r.status_code == 400, r.text
    assert "图片模型" in r.text and "对话模型" in r.text

    r = await client.post(f"/models/{text['id']}/bind-agents",
                          json={"company_id": co["id"]}, headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["bound"] == 1


async def test_single_bind_refuses_non_chat_model(client, tenants):
    alice, co = tenants.alice, tenants.alice_co
    image = await _model(co["id"], ModelType.IMAGE.value)
    aid = await _agent(co["id"])

    r = await client.post(f"/agents/{aid}/models",
                          json={"role": "primary", "model_id": image["id"]},
                          headers=alice["headers"])
    assert r.status_code == 400, r.text
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(AgentModelConfig).where(
            AgentModelConfig.agent_id == aid))).scalars().all()
    assert rows == []


async def test_rejected_switch_keeps_the_existing_binding(client, tenants):
    """被拒的切换必须原样保留旧绑定——否则一次误点就把员工变成「没有模型」。"""
    alice, co = tenants.alice, tenants.alice_co
    text = await _model(co["id"], ModelType.TEXT.value)
    image = await _model(co["id"], ModelType.IMAGE.value)
    aid = await _agent(co["id"])
    async with AsyncSessionLocal() as s:
        s.add(AgentModelConfig(agent_id=aid, role="primary",
                               model_id=text["id"]))
        await s.commit()

    r = await client.put(f"/agents/{aid}/switch-model",
                         json={"model_id": image["id"]}, headers=alice["headers"])
    assert r.status_code == 400, r.text
    async with AsyncSessionLocal() as s:
        binding = (await s.execute(select(AgentModelConfig).where(
            AgentModelConfig.agent_id == aid,
            AgentModelConfig.role == "primary"))).scalars().first()
    assert binding is not None and binding.model_id == text["id"]
