"""Web Push（VAPID）服务（C6）。

## 配置

只走环境变量，**不写任何默认私钥**，也不把密钥放进 settings：

* ``AIEOS_VAPID_PRIVATE_KEY`` —— VAPID 私钥（PEM 文本或 py-vapid 支持的编码串）
* ``AIEOS_VAPID_PUBLIC_KEY``  —— 对应的公钥（前端 base64url 使用）
* ``AIEOS_VAPID_SUBJECT``     —— 默认 ``mailto:admin@localhost``

环境变量在**每次调用时**读取，测试可以直接 monkeypatch.setenv；
日志只打印订阅 id/异常类型，绝不回显私钥、公钥或 endpoint（endpoint 本身
就是设备的推送地址，泄漏等于把通知投递给第三方）。

## 依赖是可选的

``pywebpush`` 缺失时模块仍可 import：``push_configured()`` 照常按 env 判断，
但 ``push_available()`` 为 False、``send_to_user()`` 直接返回零计数。
接口层据此返回 ``dependency_missing``，而不是让整个后端起不来。

## 事务边界

站内通知的 ``notify()`` 只 ``db.add()``，推送同样**不能在事务里发**：
``queue_push()`` 把消息挂到 ``db.info["push_outbox"]``，模块级监听
``Session.after_commit`` 才起后台任务真正发送；``after_rollback`` 清空。
这样业务回滚时不会误发推送，发送失败也不会回滚通知。

## 失败姿态

推送是锦上添花：任何异常只记 warning；只有推送服务明确说
404/410（订阅已失效/设备已注销）才删除订阅行，其余 4xx/5xx 保留，
避免一次临时故障把用户的设备静默解绑。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.logging_config import get_logger
from app.models.push_subscription import PushSubscription

try:  # 可选依赖：没装也不能影响后端启动
    from pywebpush import WebPushException, webpush

    PYWEBPUSH_AVAILABLE = True
    _PUSH_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # noqa: BLE001 — ImportError 之外还有依赖内部报错的可能
    webpush = None  # type: ignore[assignment]
    WebPushException = Exception  # type: ignore[assignment,misc]
    PYWEBPUSH_AVAILABLE = False
    _PUSH_IMPORT_ERROR = exc

logger = get_logger(__name__)

if _PUSH_IMPORT_ERROR is not None:
    logger.warning("pywebpush 不可用，Web Push 推送已停用：%s", _PUSH_IMPORT_ERROR)

ENV_PRIVATE = "AIEOS_VAPID_PRIVATE_KEY"
ENV_PUBLIC = "AIEOS_VAPID_PUBLIC_KEY"
ENV_SUBJECT = "AIEOS_VAPID_SUBJECT"
DEFAULT_SUBJECT = "mailto:admin@localhost"

OUTBOX_KEY = "push_outbox"

TITLE_MAX = 200
BODY_MAX = 1000
URL_MAX = 500

# 事件循环对 task 只持弱引用，不留强引用可能被 GC 掉
_PENDING_TASKS: set[asyncio.Task] = set()

_STATUS_RE = re.compile(r"\b(404|410)\b")


def _env(name: str) -> str:
    """读环境变量；PEM 常用 ``\\n`` 单行写法，这里还原成真实换行。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return ""
    return raw.replace("\\n", "\n")


def vapid_private_key() -> str | None:
    """私钥明文；只给发送函数用，调用方不得打印。"""
    return _env(ENV_PRIVATE) or None


def vapid_public_key() -> str | None:
    public = _env(ENV_PUBLIC)
    if not public:
        return None
    key = public.replace("\n", "").strip()
    if "@" in key or "," in key:
        # BASE64URL 前导的 subject（部分教程把 `mailto:...;BASE64` 一起粘进来）
        key = key.rsplit(",", 1)[-1].rsplit(";", 1)[-1]
    return key


def vapid_subject() -> str:
    return _env(ENV_SUBJECT) or DEFAULT_SUBJECT


def validate_endpoint(endpoint: str) -> str:
    """订阅地址必须是 https 公网地址。

    endpoint 由浏览器提供，但服务端会主动 POST 到它；不校验就成了一条
    SSRF 通道（内网 https 服务、云元数据等）。复用 IM webhook 的
    ``validate_url``（拒绝本机/内网/单标签主机），并额外要求 https
    ——浏览器推送服务没有 http 端点。
    """
    from urllib.parse import urlsplit

    from app.services.im_webhook import validate_url

    parts = urlsplit(endpoint or "")
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("push endpoint must be an https URL")
    return validate_url(endpoint)


def push_configured() -> bool:
    """私钥 + 公钥都存在才算配置好（依赖是否安装不影响本判断）。"""
    return bool(vapid_private_key() and vapid_public_key())


def push_available() -> bool:
    """真正能发送：配置齐全且 pywebpush 可用。"""
    return push_configured() and PYWEBPUSH_AVAILABLE


def push_status() -> dict:
    """给前端/接口的状态快照；未配置时 public_key 必须为 None。"""
    configured = push_configured()
    if not configured:
        reason = "not_configured"
    elif not PYWEBPUSH_AVAILABLE:
        reason = "dependency_missing"
    else:
        reason = None
    return {
        "configured": configured,
        "available": configured and PYWEBPUSH_AVAILABLE,
        "public_key": vapid_public_key() if configured else None,
        "reason": reason,
    }


def _payload(title: str, body: str | None, url: str | None) -> dict:
    return {
        "title": (title or "")[:TITLE_MAX],
        "body": (body or "")[:BODY_MAX],
        "url": (url or "/m")[:URL_MAX],
    }


def _send_sync(subscription: PushSubscription, payload: dict) -> None:
    """阻塞发送（在 to_thread 里跑）；成功返回，失败抛 WebPushException。"""
    if webpush is None:  # pragma: no cover — push_available 已挡住
        raise RuntimeError("pywebpush is not installed")
    webpush(
        subscription_info={
            "endpoint": subscription.endpoint,
            "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
        },
        data=json.dumps(payload, ensure_ascii=False),
        vapid_private_key=vapid_private_key(),
        vapid_claims={"sub": vapid_subject()},
        timeout=10,
        ttl=60,
    )


def _gone(exc: Exception) -> bool:
    """推送服务是否明确表示订阅已失效（404/410）。"""
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code in (404, 410)
    match = _STATUS_RE.search(str(exc)[:200])
    return bool(match)


async def send_to_user(
    db: AsyncSession,
    user_id: str,
    title: str,
    body: str | None = None,
    url: str | None = None,
) -> dict:
    """给该用户所有订阅尽力发送；绝不抛异常。

    404/410 删除订阅行（设备已注销），其余错误保留订阅只记 warning。
    返回 ``{"sent", "failed", "removed"}``；未配置/依赖缺失时全为 0。
    """
    if not push_available():
        return {"sent": 0, "failed": 0, "removed": 0}

    try:
        rows = list((await db.execute(select(PushSubscription).where(
            PushSubscription.user_id == user_id))).scalars().all())
    except Exception as exc:  # noqa: BLE001 — 查询失败也不能反噬业务
        logger.warning("[web_push] 读取订阅失败 user=%s: %s", user_id, exc)
        return {"sent": 0, "failed": 0, "removed": 0}

    payload = _payload(title, body, url)
    sent = failed = removed = 0
    changed = False
    for row in rows:
        try:
            await asyncio.to_thread(_send_sync, row, payload)
            row.last_used_at = datetime.now(timezone.utc).replace(tzinfo=None)
            sent += 1
            changed = True
        except Exception as exc:  # noqa: BLE001 — 单台设备失败不影响其它设备
            failed += 1
            if _gone(exc):
                await db.delete(row)
                removed += 1
                changed = True
                logger.info("[web_push] 订阅已失效，删除 subscription=%s", row.id)
            else:
                # 只记订阅 id 与异常类型：endpoint/密钥不进日志
                logger.warning("[web_push] 发送失败 subscription=%s: %s",
                               row.id, type(exc).__name__)

    if changed:
        try:
            await db.commit()
        except Exception as exc:  # noqa: BLE001 — 清理失败不影响发送结果
            logger.warning("[web_push] 更新订阅状态失败 user=%s: %s",
                           user_id, exc)
    return {"sent": sent, "failed": failed, "removed": removed}


def queue_push(
    db: AsyncSession,
    *,
    user_id: str,
    title: str,
    body: str | None = None,
    url: str | None = None,
) -> bool:
    """把一条推送挂到当前 session，提交成功后才发送。

    未配置/依赖缺失时直接 no-op（返回 False），不写库、不联网、不抛异常。
    """
    if not push_available() or not user_id:
        return False
    db.info.setdefault(OUTBOX_KEY, []).append({
        "user_id": user_id, "title": title, "body": body, "url": url,
    })
    return True


def _take_outbox(session: Session) -> list[dict]:
    return session.info.pop(OUTBOX_KEY, None) or []


async def _dispatch(items: list[dict]) -> None:
    """提交后真正发送：每次用独立 session，逐条尽力而为。"""
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        for item in items:
            try:
                await send_to_user(
                    session, item["user_id"], item.get("title") or "",
                    item.get("body"), item.get("url"))
            except Exception as exc:  # noqa: BLE001 — 后台任务绝不冒泡
                logger.warning("[web_push] 派发失败 user=%s: %s",
                               item.get("user_id"), type(exc).__name__)


def _on_after_commit(session: Session) -> None:
    items = _take_outbox(session)
    if not items:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 同步脚本/线程里没有事件循环：放弃本次外发
        logger.warning("[web_push] 无运行中的事件循环，丢弃 %d 条推送", len(items))
        return
    task = loop.create_task(_dispatch(items))
    _PENDING_TASKS.add(task)
    task.add_done_callback(_PENDING_TASKS.discard)


def _on_after_rollback(session: Session) -> None:
    _take_outbox(session)


# 与 IM webhook 相同：模块级只挂一次，重复 import 不会重复监听
if not event.contains(Session, "after_commit", _on_after_commit):
    event.listen(Session, "after_commit", _on_after_commit)
if not event.contains(Session, "after_rollback", _on_after_rollback):
    event.listen(Session, "after_rollback", _on_after_rollback)
