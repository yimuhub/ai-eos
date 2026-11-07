"""IM 群机器人出站通知（C4）：飞书 / 钉钉 / Slack / 通用 Webhook。

## 配置

全部走环境变量，不进入 `app.core.config.settings`（避免每次改配置都动代码）：

* ``AIEOS_IM_WEBHOOK_URL``     —— 为空则整条通道关闭（no-op）
* ``AIEOS_IM_WEBHOOK_TYPE``    —— ``feishu|dingtalk|slack|generic``，默认 generic
* ``AIEOS_IM_WEBHOOK_SECRET``  —— 可选，飞书/钉钉加签

环境变量在**每次调用时**读取，测试可以直接 monkeypatch.setenv。

## 事务边界：提交后才发

`queue_notification()` 只把消息 append 到 ``db.info["im_outbox"]``，不写库、
不联网。模块级监听 `Session` 的 ``after_commit`` 才真正派发，``after_rollback``
清空——业务事务回滚时绝不会误发"任务完成"这类消息。

## 失败姿态

IM 是锦上添花：URL 非法、DNS 失败、对端 500、超时，全部只记 warning。
发送用 ``http_client(timeout=5)``（统一出站入口，不读系统代理变量），且有明确的 SSRF 防护：
只允许 http/https，拒绝本机、链路本地、内网/保留地址与单标签主机名。

重试/死信不在本模块范围内（队列已具备，后续可接）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import os
import time
from urllib.parse import quote_plus, urlsplit

from app.core.http_client import http_client
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.logging_config import get_logger

logger = get_logger(__name__)

ENV_URL = "AIEOS_IM_WEBHOOK_URL"
ENV_TYPE = "AIEOS_IM_WEBHOOK_TYPE"
ENV_SECRET = "AIEOS_IM_WEBHOOK_SECRET"

KINDS = ("feishu", "dingtalk", "slack", "generic")
OUTBOX_KEY = "im_outbox"

# 云厂商元数据/内部服务常用主机名，字面量直接拒绝
_BLOCKED_HOSTS = {
    "localhost",
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
    "instance-data.ec2.internal",
}

# ipaddress 未标记为 private 但仍属内网/云元数据（如阿里云 100.100.100.200）
_BLOCKED_NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    "100.64.0.0/10",   # 运营商级 NAT / 阿里云元数据
    "198.18.0.0/15",   # 基准测试保留段
    "fec0::/10",       # 已废弃的站点本地地址
))


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def _config() -> tuple[str, str, str]:
    kind = (_env(ENV_TYPE, "generic")).lower()
    if kind not in KINDS:
        kind = "generic"
    return _env(ENV_URL), kind, _env(ENV_SECRET)


def validate_url(url: str) -> str:
    """校验出站地址；不安全时抛 ValueError（调用方降级为 warning）。"""
    if not url or not isinstance(url, str):
        raise ValueError("IM webhook URL is empty")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"IM webhook URL scheme not allowed: {parts.scheme!r}")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("IM webhook URL has no host")

    if host in _BLOCKED_HOSTS or host.endswith(".localhost") \
            or host.endswith(".local") or host.endswith(".internal"):
        raise ValueError(f"IM webhook host not allowed: {host}")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # 数字形态（十进制/十六进制）可能被解析器当成 IP，一律拒绝
        if host.isdigit() or host.startswith("0x"):
            raise ValueError(f"IM webhook host not allowed: {host}")
        # 单标签主机名只可能指向内网（public DNS 名必然带点）
        if "." not in host:
            raise ValueError(f"IM webhook host not allowed: {host}")
        return url

    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
            or getattr(ip, "is_site_local", False)
            or any(ip in net for net in _BLOCKED_NETWORKS
                   if net.version == ip.version)):
        raise ValueError(f"IM webhook address not allowed: {host}")
    return url


def _text(title: str | None, body: str | None, link: str | None) -> str:
    """统一的人读文本：标题用【】包裹，正文与链接各占一行。"""
    head = f"【{title}】" if title else ""
    tail = f"\n{link}" if link else ""
    return f"{head}{body or ''}{tail}"


def _feishu_sign(timestamp: str, secret: str) -> str:
    """飞书自定义机器人：key=f"{timestamp}\\n{secret}"、msg="" 的 HMAC-SHA256。"""
    key = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(key, b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _dingtalk_sign(timestamp: str, secret: str) -> str:
    """钉钉自定义机器人：key=secret，msg=f"{timestamp}\\n{secret}"。"""
    msg = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest()
    return quote_plus(base64.b64encode(digest).decode("utf-8"))


def build_payload(kind: str, title: str | None, body: str | None,
                  link: str | None, timestamp: int | str | None = None
                  ) -> tuple[str, dict]:
    """按配置渠道构造 (url, json_body)。

    `kind` 是事件类型（如 task.completed），会写进 generic 的 ``type``；
    渠道格式由 ``AIEOS_IM_WEBHOOK_TYPE`` 决定。未配置该变量且 `kind`
    恰好是渠道名时，按渠道名处理（兼容直接调用）。
    """
    url, configured_kind, secret = _config()
    url = validate_url(url)
    raw_type = _env(ENV_TYPE).lower()
    if raw_type in KINDS:
        channel = raw_type
    elif (kind or "").lower() in KINDS:
        channel = kind.lower()
    else:
        channel = "generic"
    if channel not in KINDS:
        channel = configured_kind
    ts = str(timestamp if timestamp is not None else int(time.time()))
    text = _text(title, body, link)

    if channel == "slack":
        return url, {"text": text}
    if channel == "feishu":
        payload: dict = {"msg_type": "text", "content": {"text": text}}
        if secret:
            payload["timestamp"] = ts
            payload["sign"] = _feishu_sign(ts, secret)
        return url, payload
    if channel == "dingtalk":
        if secret:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}timestamp={ts}&sign={_dingtalk_sign(ts, secret)}"
        return url, {"msgtype": "text", "text": {"content": text}}

    return url, {"type": kind, "title": title, "body": body, "link": link}


async def _send_one(url: str, payload: dict) -> None:
    """POST 一次；任何异常只记 warning，绝不向上抛。"""
    try:
        async with http_client(timeout=5) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.warning("[im_webhook] 发送失败 status=%s url=%s",
                               resp.status_code, _safe_url(url))
    except Exception as exc:  # noqa: BLE001 — 外发失败不影响主业务
        logger.warning("[im_webhook] 发送异常 url=%s: %s",
                       _safe_url(url), exc)


def _safe_url(url: str) -> str:
    """日志里不打印 query（钉钉签名在 query 中）。"""
    return url.split("?", 1)[0]


async def _dispatch(items: list[dict]) -> None:
    """逐条外发；渠道格式由当前配置决定，单条失败不影响后续。"""
    for item in items:
        try:
            url, payload = build_payload(
                item.get("kind", "generic"), item.get("title"),
                item.get("body"), item.get("link"), item.get("timestamp"))
        except Exception as exc:  # noqa: BLE001 — 配置错误不能反噬业务
            logger.warning("[im_webhook] 跳过无效通知 kind=%s: %s",
                           item.get("kind"), exc)
            continue
        await _send_one(url, payload)


def queue_notification(db: AsyncSession, *, kind: str, title: str | None,
                       body: str | None = None,
                       link: str | None = None) -> bool:
    """把一条 IM 消息挂到当前 session，提交后异步派发。

    未配置 URL（或配置非法）时直接 no-op；不写库、不联网、不抛异常。
    返回是否入队。
    """
    url, _, _ = _config()
    if not url:
        return False
    try:
        validate_url(url)
    except ValueError as exc:
        logger.warning("[im_webhook] 配置的 URL 不可用，跳过外发: %s", exc)
        return False
    db.info.setdefault(OUTBOX_KEY, []).append({
        "kind": kind, "title": title, "body": body, "link": link,
        "timestamp": int(time.time()),
    })
    return True


def _take_outbox(session: Session) -> list[dict]:
    return session.info.pop(OUTBOX_KEY, None) or []


# 事件循环对 task 只持弱引用：不留强引用可能被 GC 掉，通知就静默丢了
_PENDING_TASKS: set[asyncio.Task] = set()


def _on_after_commit(session: Session) -> None:
    items = _take_outbox(session)
    if not items:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 同步脚本/线程里没有事件循环：放弃本次外发
        logger.warning("[im_webhook] 无运行中的事件循环，丢弃 %d 条通知",
                       len(items))
        return
    task = loop.create_task(_dispatch(items))
    _PENDING_TASKS.add(task)
    task.add_done_callback(_PENDING_TASKS.discard)


def _on_after_rollback(session: Session) -> None:
    _take_outbox(session)


# 模块级只挂一次；重复 import / reload 也不会重复监听
if not event.contains(Session, "after_commit", _on_after_commit):
    event.listen(Session, "after_commit", _on_after_commit)
if not event.contains(Session, "after_rollback", _on_after_rollback):
    event.listen(Session, "after_rollback", _on_after_rollback)
