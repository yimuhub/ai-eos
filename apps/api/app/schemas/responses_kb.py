"""知识库域的 API 响应模型（A3 长尾：``/knowledge`` + ``/kb2``）。

分工同 `app/schemas/responses_ops.py`：按模块分文件，避免并行改动撞车，
所以路由请 `from app.schemas.responses_kb import ...`，不要往 `__init__` 里加。

## 字段来自哪里

两个路由的返回都是**函数体里现拼的 dict**，因此逐字段对着源码写，并用
`tests/test_kb_contract.py` 在临时 SQLite 库上把端点真跑一遍对账
（断言的是"真实返回的键集合 ⊇ 模型声明的键"，方向锁死，改漏立刻红）。

## 两处刻意保留的宽松

* `KBUploadOut` 的两个分支键集不同（转换成功带 `chunks/original_saved/size`，
  失败带 `note`），所以除 `id`/`converted` 外全部可选，路由带
  `response_model_exclude_unset=True`：没走到的分支不会凭空多出 `null` 键。
* `KBMetaOut` 不建：`/kb2/agents/{id}/rag` 的 `meta` 两个分支结构不同
  （命中缓存只有 `profile/cached`，未命中多 `incremental/used_chars/budget`），
  写成 `dict` 才不会把以后新增的统计项裁掉。
* `KBSearchOut.results[*].content` 是**整块原文**（不截断），前端预览要用；
  而 `KBDetailOut.chunks[*].content` 在端点里已截到 200 字，两者别混。

全部模型 `extra="allow"`：字段判断错时多余键原样透出，不会被序列化静默裁掉。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


def _free() -> ConfigDict:
    return ConfigDict(extra="allow")


# ---------------------------------------------------------------- /knowledge


class KnowledgeDocCreatedOut(BaseModel):
    """`POST /knowledge/documents`：入库结果（`status` 已是 `.value`）。"""

    model_config = _free()

    id: str
    title: str
    status: str
    chunk_count: int


class KnowledgeDocItemOut(BaseModel):
    """`GET /knowledge/documents` 的一行；`created_at` 在端点里已 `isoformat()`。"""

    model_config = _free()

    id: str
    title: str
    source_type: str
    status: str
    chunk_count: int
    project_id: str | None
    scope: str
    agent_id: str | None
    created_at: str


class KnowledgeChunkBriefOut(BaseModel):
    model_config = _free()

    seq: int
    content: str


class KnowledgeDocDetailOut(BaseModel):
    """`GET /knowledge/documents/{doc_id}`：正文 + 分块预览。"""

    model_config = _free()

    id: str
    title: str
    content: str
    status: str
    chunk_count: int
    chunks: list[KnowledgeChunkBriefOut]


class KnowledgeDeletedOut(BaseModel):
    """`DELETE /knowledge/documents/{doc_id}`。

    只有 `detail`：这个键同时是前端错误信封的字段名，历史上就用它当成功标记，
    改不动——留着并把类型锁成 str，比"顺手统一成 {ok:true}"更安全。
    """

    model_config = _free()

    detail: str


class KnowledgeHitOut(BaseModel):
    """`retrieve()` 的一条命中（bigram 重合度打分）。"""

    model_config = _free()

    chunk_id: str
    document_id: str
    seq: int
    content: str
    score: float


class KnowledgeSearchOut(BaseModel):
    model_config = _free()

    query: str
    results: list[KnowledgeHitOut]


class KnowledgeRagOut(BaseModel):
    """`GET /knowledge/rag`：可直接塞进 prompt 的上下文。"""

    model_config = _free()

    query: str
    context: str


# ---------------------------------------------------------------- /kb2


class KBDocOut(BaseModel):
    """`_doc_out()`：私人库与公用库列表共用同一形状。"""

    model_config = _free()

    id: str
    title: str
    scope: str
    category: str | None
    chunk_count: int
    summary: str
    has_original: bool
    original_name: str | None
    skill_template: str | None
    created_at: str


class KBIndexedOut(BaseModel):
    """建文档成功的回执：新文档 id + 切片数。

    `POST /kb2/agents/{id}/documents` 以前返回 `null`——前端只能整表重取；
    现在与 `/kb2/global/documents` 用同一个模型。
    """

    model_config = _free()

    id: str
    chunks: int


class KBUploadOut(BaseModel):
    """`POST /kb2/agents/{id}/upload`：双格式存储的两条分支。"""

    model_config = _free()

    id: str
    converted: bool
    note: str | None = None
    chunks: int | None = None
    original_saved: str | None = None
    size: int | None = None


class KBMutationOut(BaseModel):
    """只回"改成了"的写端点（PATCH/DELETE 文档）。"""

    model_config = _free()

    ok: bool


class KBCopiedItemOut(BaseModel):
    model_config = _free()

    source: str
    new_id: str
    title: str


class KBCopyOut(BaseModel):
    """`POST /kb2/copy` 与 `POST /kb2/agents/{id}/sync-from-global`（后者直接复用前者）。

    `count` 可能小于请求的 `doc_ids` 长度：越权或不存在的源文档按"跳过"处理，
    这是设计行为，所以两个字段都得给前端读。
    """

    model_config = _free()

    copied: list[KBCopiedItemOut]
    count: int


class KBTemplateOut(BaseModel):
    """`GET /kb2/templates`：目录项（不含 docs 正文）。"""

    model_config = _free()

    key: str
    name: str
    icon: str
    doc_count: int


class KBImportedItemOut(BaseModel):
    model_config = _free()

    id: str
    title: str


class KBImportOut(BaseModel):
    model_config = _free()

    template: str
    imported: list[KBImportedItemOut]
    count: int


class KBAgentConfigOut(BaseModel):
    """`PUT /kb2/agents/{id}/config`：回显 Agent 的 memory_config。

    自由结构（各模块往里塞键），保持 `dict` 而不是展开成固定字段。
    """

    model_config = _free()

    memory_config: dict


class KBCostProfileOut(BaseModel):
    model_config = _free()

    top_k: int
    budget_chars: int
    compress: str
    summary_first: bool


class KBRagPreviewOut(BaseModel):
    """`GET /kb2/agents/{id}/rag`：成本优化检索预览（调试用）。"""

    model_config = _free()

    context_text: str
    from_cache: bool
    hits: int
    tokens_saved_est: int
    meta: dict
