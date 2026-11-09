"""创作中心：图片 / 视频生成。

模型目录早就支持 image/video 分类与配置，但产品一直没有生成路径——
用户把图片/视频模型接进来之后「配而不用」。本路由补上最后一公里：

* 图片：OpenAI 兼容 `POST {base_url}/images/generations`，同步等结果；
* 视频：`POST {base_url}/videos` 建异步任务，前端轮询
  `GET /media/videos/{id}`，后端替它去上游查进度，完成即下载收尾。

安全与模型测试同口径：敏感变量名拒绝、base_url 过出站闸门、密钥走
api_key_resolver（公司密钥库 → 环境变量），明文不进日志、不进响应。
结果下载到本地存储：上游链接可能过期，资产应该攥在公司自己手里。
"""
import base64
import logging
import re
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.routes.assets import storage_root
from app.api.routes.models_api import (_reject_sensitive_env,
                                       _validate_model_base_url)
from app.core.http_client import http_client
from app.core.net_guard import OutboundURLRejected, validate_outbound_url_async
from app.core.permissions import Capability, ensure_capability
from app.core.uploads import ensure_within
from app.db.session import get_db
from app.models.agent import Model
from app.models.media import MediaGeneration
from app.services import api_key_resolver
from app.services.event_system import log_event

router = APIRouter(prefix="/media", tags=["media"])
logger = logging.getLogger(__name__)

_IMAGE_TIMEOUT = httpx.Timeout(240.0, connect=15.0)
_DOWNLOAD_TIMEOUT = httpx.Timeout(180.0, connect=15.0)
_MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024  # 视频上限 200MB，防上游异常撑爆磁盘
_SIZE_RE = re.compile(r"^\d{3,4}x\d{3,4}$")


class ImageGenerateIn(BaseModel):
    company_id: str
    model_id: str                       # models 表的行 id
    prompt: str = Field(min_length=1, max_length=4000)
    size: str = "1024x1024"
    n: int = Field(default=1, ge=1, le=4)


class VideoGenerateIn(BaseModel):
    company_id: str
    model_id: str
    prompt: str = Field(min_length=1, max_length=4000)
    seconds: str = "5"                  # 上游要求字符串 "4"–"12"
    size: str = "720P"
    aspect_ratio: str = "16:9"


class MediaGenerationOut(BaseModel):
    id: str
    company_id: str
    kind: str
    model_name: str
    prompt: str
    status: str
    params: dict = {}
    source_url: str | None = None
    file_url: str | None = None
    progress: int | None = None
    error: str | None = None
    created_at: str | None = None


class MediaListOut(BaseModel):
    items: list[MediaGenerationOut]


def _out(row: MediaGeneration, progress: int | None = None) -> dict:
    return {
        "id": row.id,
        "company_id": row.company_id,
        "kind": row.kind,
        "model_name": row.model_name,
        "prompt": row.prompt,
        "status": row.status,
        "params": row.params or {},
        "source_url": row.source_url,
        "file_url": f"/media/files/{row.id}" if row.local_path else None,
        "progress": progress,
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def _load_model_for_generation(
        db: AsyncSession, user, company_id: str, model_row_id: str,
        kind: str) -> tuple[Model, str]:
    """权限 + 类型 + 安全闸门 + 密钥解析，返回 (模型行, 明文密钥)。

    明文密钥只在调用栈里传递，绝不写日志/响应/落库。
    """
    await ensure_capability(db, user.id, company_id, Capability.OPERATE)
    m = await db.get(Model, model_row_id)
    if m is None:
        raise HTTPException(404, "模型配置不存在。")
    if m.company_id is not None and m.company_id != company_id:
        raise HTTPException(404, "模型配置不存在。")
    if (m.model_type or "text") != kind:
        raise HTTPException(
            400, f"该模型不是{('图片' if kind == 'image' else '视频')}模型，"
            "请到模型配置选择对应类型的模型。")
    if m.enabled is False:
        raise HTTPException(409, "该模型已被停用，请先在模型配置启用。")
    if not m.base_url:
        raise HTTPException(400, "该模型没有配置 Base URL，无法生成。")
    _reject_sensitive_env(m.api_key_env)
    await _validate_model_base_url(m.base_url)
    resolved = await api_key_resolver.resolve_for_companies(
        db, [company_id], m.api_key_env, m.provider_type)
    if not resolved.present:
        raise HTTPException(
            400, "该模型还没有可用密钥：请在模型配置里填写密钥，"
            "或在服务端配置对应的环境变量。")
    return m, resolved.value


def _upstream_error_message(resp: httpx.Response) -> str:
    """上游报错原文截断，绝不回显请求头（密钥在 Authorization 里）。"""
    try:
        body = resp.json()
        if isinstance(body, dict):
            err = body.get("error") or body
            if isinstance(err, dict):
                return str(err.get("message") or err)[:500]
            return str(err)[:500]
    except ValueError:  # noqa: silent-except — 上游错误体非 JSON 时回退原文，属有意降级
        pass
    return resp.text[:500]


async def _validate_result_url(url: str) -> None:
    """结果下载地址与 base_url 同闸门：允许私网（本地模型服务），拒元数据。"""
    try:
        await validate_outbound_url_async(url, allow_private=True)
    except OutboundURLRejected as e:
        raise HTTPException(502, f"上游返回的结果地址不安全：{e}")


async def _download_result(client: httpx.AsyncClient, url: str,
                           row: MediaGeneration,
                           default_ext: str) -> str:
    """把上游结果下载到 storage/media/，返回相对路径。"""
    await _validate_result_url(url)
    async with client.stream("GET", url, timeout=_DOWNLOAD_TIMEOUT,
                             follow_redirects=True) as r:
        if r.status_code != 200:
            raise HTTPException(502, f"下载生成结果失败（HTTP {r.status_code}）。")
        ctype = r.headers.get("content-type", "").split(";")[0].strip()
        ext = {".png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
               "video/mp4": ".mp4", "video/webm": ".webm"}.get(
                   ctype, default_ext)
        chunks: list[bytes] = []
        total = 0
        async for chunk in r.aiter_bytes():
            total += len(chunk)
            if total > _MAX_DOWNLOAD_BYTES:
                raise HTTPException(502, "生成结果超过 200MB 上限，已放弃下载。")
            chunks.append(chunk)
    root = storage_root() / "media"
    root.mkdir(parents=True, exist_ok=True)
    dest = ensure_within(root, root / f"{row.id}{ext}")
    dest.write_bytes(b"".join(chunks))
    return f"media/{row.id}{ext}"


def _media_file_path(row: MediaGeneration) -> Path:
    root = storage_root()
    rel = row.local_path or ""
    return ensure_within(root, root / rel)


async def _fail_row(db: AsyncSession, row: MediaGeneration,
                    message: str) -> None:
    row.status = "failed"
    row.error = message[:1000]
    await db.commit()


@router.post("/images", response_model=MediaGenerationOut)
async def generate_image(data: ImageGenerateIn,
                         db: AsyncSession = Depends(get_db),
                         user=Depends(get_current_user)):
    """文生图：同步等上游出图，下载到本地后返回。"""
    if not _SIZE_RE.match(data.size):
        raise HTTPException(400, "size 格式应为 宽x高，如 1024x1024。")
    m, key = await _load_model_for_generation(
        db, user, data.company_id, data.model_id, "image")
    row = MediaGeneration(
        company_id=data.company_id, created_by=user.id,
        model_row_id=m.id, model_name=m.name or m.model_id,
        kind="image", prompt=data.prompt, status="processing",
        params={"size": data.size, "n": data.n})
    db.add(row)
    await db.flush()

    base = (m.base_url or "").rstrip("/")
    headers = {"Authorization": f"Bearer {key}"}
    body = {"model": m.model_id, "prompt": data.prompt,
            "size": data.size, "n": data.n, "response_format": "url"}
    try:
        async with http_client() as client:
            r = await client.post(f"{base}/images/generations",
                                  json=body, headers=headers,
                                  timeout=_IMAGE_TIMEOUT)
            if r.status_code != 200:
                msg = _upstream_error_message(r)
                await _fail_row(db, row, f"上游 HTTP {r.status_code}: {msg}")
                raise HTTPException(502, f"图片生成失败：{msg}")
            d = r.json()
            items = d.get("data") or []
            if not items:
                await _fail_row(db, row, "上游没有返回图片数据。")
                raise HTTPException(502, "图片生成失败：上游没有返回图片数据。")
            first = items[0]
            url = first.get("url") or ""
            if url:
                row.source_url = url
                row.local_path = await _download_result(
                    client, url, row, ".png")
            elif first.get("b64_json"):
                root = storage_root() / "media"
                root.mkdir(parents=True, exist_ok=True)
                dest = ensure_within(root, root / f"{row.id}.png")
                dest.write_bytes(base64.b64decode(first["b64_json"]))
                row.local_path = f"media/{row.id}.png"
            else:
                await _fail_row(db, row, "上游返回的图片数据为空。")
                raise HTTPException(502, "图片生成失败：上游返回的图片数据为空。")
    except httpx.HTTPError as e:
        await _fail_row(db, row, f"上游连接失败: {type(e).__name__}")
        raise HTTPException(502, "图片生成失败：无法连接上游服务，请稍后重试。")

    row.status = "completed"
    await log_event(db, "media.image_generated", "media", row.id,
                    "user", user.id,
                    {"model": m.model_id, "size": data.size},
                    company_id=data.company_id)
    await db.commit()
    return _out(row)


@router.post("/videos", response_model=MediaGenerationOut)
async def generate_video(data: VideoGenerateIn,
                         db: AsyncSession = Depends(get_db),
                         user=Depends(get_current_user)):
    """文生视频：上游是异步任务，这里只负责建任务；进度走 GET /media/videos/{id}。"""
    if data.seconds not in {str(i) for i in range(4, 13)}:
        raise HTTPException(400, "seconds 只支持 4–12 秒。")
    if data.aspect_ratio not in {"16:9", "9:16", "1:1"}:
        raise HTTPException(400, "aspect_ratio 只支持 16:9 / 9:16 / 1:1。")
    m, key = await _load_model_for_generation(
        db, user, data.company_id, data.model_id, "video")
    base = (m.base_url or "").rstrip("/")
    headers = {"Authorization": f"Bearer {key}"}
    body = {"model": m.model_id, "prompt": data.prompt, "mode": "text",
            "seconds": data.seconds, "size": data.size,
            "aspect_ratio": data.aspect_ratio}
    try:
        async with http_client() as client:
            r = await client.post(f"{base}/videos", json=body,
                                  headers=headers,
                                  timeout=httpx.Timeout(90.0, connect=15.0))
    except httpx.HTTPError as e:
        logger.warning("视频任务创建连接失败 model=%s: %s", m.model_id,
                       type(e).__name__)
        raise HTTPException(502, "视频任务创建失败：无法连接上游服务。")
    if r.status_code == 503:
        raise HTTPException(
            503, "上游视频队列已满，请稍后重试（本次未创建任务、不计费）。")
    if r.status_code != 200:
        raise HTTPException(502, f"视频任务创建失败：{_upstream_error_message(r)}")
    d = r.json()
    video_id = d.get("video_id") or d.get("id") or d.get("task_id")
    if not video_id:
        raise HTTPException(502, "视频任务创建失败：上游没有返回任务号。")

    row = MediaGeneration(
        company_id=data.company_id, created_by=user.id,
        model_row_id=m.id, model_name=m.name or m.model_id,
        kind="video", prompt=data.prompt, status="processing",
        params={"seconds": data.seconds, "size": data.size,
                "aspect_ratio": data.aspect_ratio},
        upstream_video_id=str(video_id))
    db.add(row)
    await db.flush()
    await log_event(db, "media.video_submitted", "media", row.id,
                    "user", user.id, {"model": m.model_id},
                    company_id=data.company_id)
    await db.commit()
    return _out(row)


def _video_poll_urls(m: Model, video_id: str,
                     style: str | None) -> list[tuple[str, str]]:
    """候选轮询地址：OpenAI 标准优先，404 再试 Agnes 专用查询。

    返回 [(style, url)]；style 命中后写回 params，后续轮询不再试错。
    """
    base = (m.base_url or "").rstrip("/")
    standard = f"{base}/videos/{video_id}"
    root = base[:-3] if base.endswith("/v1") else base
    agnes = f"{root}/agnesapi?" + urlencode(
        {"video_id": video_id, "model_name": m.model_id})
    if style == "agnes":
        return [("agnes", agnes)]
    if style == "standard":
        return [("standard", standard)]
    return [("standard", standard), ("agnes", agnes)]


@router.get("/videos/{gid}", response_model=MediaGenerationOut)
async def poll_video(gid: str,
                     db: AsyncSession = Depends(get_db),
                     user=Depends(get_current_user)):
    """轮询视频任务；上游完成即下载收尾，前端只需读到终态。"""
    row = await db.get(MediaGeneration, gid)
    if row is None or row.kind != "video":
        raise HTTPException(404, "生成记录不存在。")
    await ensure_capability(db, user.id, row.company_id, Capability.VIEW)
    if row.status in ("completed", "failed"):
        return _out(row)

    m = await db.get(Model, row.model_row_id) if row.model_row_id else None
    if m is None:
        await _fail_row(db, row, "模型配置已被删除，无法继续查询任务。")
        return _out(row)
    resolved = await api_key_resolver.resolve_for_companies(
        db, [row.company_id], m.api_key_env, m.provider_type)
    if not resolved.present:
        await _fail_row(db, row, "模型密钥已不可用，无法继续查询任务。")
        return _out(row)

    headers = {"Authorization": f"Bearer {resolved.value}"}
    style = (row.params or {}).get("poll")
    progress: int | None = None
    async with http_client() as client:
        for cand_style, url in _video_poll_urls(m, row.upstream_video_id or "",
                                                style):
            try:
                r = await client.get(url, headers=headers,
                                     timeout=httpx.Timeout(30.0, connect=10.0),
                                     follow_redirects=True)
            except httpx.HTTPError:
                raise HTTPException(502, "查询上游任务失败：连接中断，请稍后重试。")
            if r.status_code == 404 and cand_style == "standard":
                continue  # 换下一种查询形式
            if r.status_code != 200:
                raise HTTPException(
                    502, f"查询上游任务失败：{_upstream_error_message(r)}")
            d = r.json()
            status = str(d.get("status") or "").lower()
            progress = d.get("progress")
            if cand_style != style:
                row.params = {**(row.params or {}), "poll": cand_style}
            if status == "completed":
                video_url = d.get("url") or ""
                if not video_url:
                    await _fail_row(db, row, "上游标记完成但没有返回视频地址。")
                    return _out(row)
                row.source_url = video_url
                try:
                    row.local_path = await _download_result(
                        client, video_url, row, ".mp4")
                except HTTPException as e:
                    await _fail_row(db, row, str(e.detail))
                    return _out(row)
                row.status = "completed"
                await log_event(db, "media.video_generated", "media", row.id,
                                "user", user.id, {"model": m.model_id},
                                company_id=row.company_id)
                await db.commit()
                return _out(row, progress=100)
            if status in ("failed", "cancelled", "canceled"):
                await _fail_row(db, row, str(d.get("error") or "上游任务失败。"))
                return _out(row)
            await db.commit()  # 记住命中的查询形式
            return _out(row, progress=progress)
    raise HTTPException(502, "上游任务查询地址均不可用（404）。")


@router.get("/generations", response_model=MediaListOut)
async def list_generations(company_id: str,
                           kind: str | None = Query(default=None),
                           limit: int = Query(default=50, ge=1, le=200),
                           db: AsyncSession = Depends(get_db),
                           user=Depends(get_current_user)):
    await ensure_capability(db, user.id, company_id, Capability.VIEW)
    stmt = select(MediaGeneration).where(
        MediaGeneration.company_id == company_id)
    if kind in ("image", "video"):
        stmt = stmt.where(MediaGeneration.kind == kind)
    rows = (await db.execute(
        stmt.order_by(MediaGeneration.created_at.desc()).limit(limit))).scalars().all()
    return {"items": [_out(r) for r in rows]}


@router.get("/files/{gid}")
async def get_media_file(gid: str,
                         db: AsyncSession = Depends(get_db),
                         user=Depends(get_current_user)):
    row = await db.get(MediaGeneration, gid)
    if row is None or not row.local_path:
        raise HTTPException(404, "文件不存在。")
    await ensure_capability(db, user.id, row.company_id, Capability.VIEW)
    path = _media_file_path(row)
    if not path.is_file():
        raise HTTPException(404, "文件已丢失（可能清理过存储目录）。")
    return FileResponse(path)


@router.delete("/generations/{gid}", status_code=204)
async def delete_generation(gid: str,
                            db: AsyncSession = Depends(get_db),
                            user=Depends(get_current_user)):
    row = await db.get(MediaGeneration, gid)
    if row is None:
        raise HTTPException(404, "生成记录不存在。")
    await ensure_capability(db, user.id, row.company_id, Capability.MANAGE)
    if row.local_path:
        path = _media_file_path(row)
        if path.is_file():
            path.unlink()
    await db.delete(row)
    await db.commit()