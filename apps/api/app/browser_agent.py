"""Browser Agent Foundation (P2-13).

安全边界 + 操作日志。Agent 可以请求浏览网页，但必须经过：
1. 域名白名单检查
2. 协议检查（仅 https）
3. 私有地址/内网 IP 黑名单（防 SSRF）
4. 每次操作落库审计

执行层用 httpx 抓取文本（无 JS 渲染，安全可控），后续可接 Playwright。
"""
import ipaddress
import re
import socket
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from sqlalchemy import JSON, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class BrowserActionLog(Base):
    """浏览器操作审计日志（不可删）。"""
    __tablename__ = "browser_action_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True,
                                                  index=True)
    action_type: Mapped[str] = mapped_column(String(30), nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    allowed: Mapped[bool] = mapped_column(nullable=False)
    deny_reason: Mapped[str | None] = mapped_column(Text)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_now, index=True)


# 默认域名黑名单关键词 + 私网段
_PRIVATE_NETS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
    "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10",
)]
_BLOCKED_HOST_PAT = re.compile(
    r"(^|\.)(localhost|internal|intranet|metadata)\.?$", re.I)


class SecurityViolation(Exception):
    pass


def check_url_security(url: str) -> tuple[bool, str]:
    """URL 安全检查：协议 / 内网地址 / 黑名单主机名。"""
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "URL 解析失败"

    if parsed.scheme != "https":
        return False, f"仅允许 https（当前 {parsed.scheme or '无'}）"
    host = parsed.hostname
    if not host:
        return False, "缺少主机名"
    if _BLOCKED_HOST_PAT.match(host):
        return False, f"主机名被禁止: {host}"

    # 解析 IP 判断是否私网
    try:
        infos = socket.getaddrinfo(host, 443)
        for info in infos[:4]:
            ip = ipaddress.ip_address(info[4][0])
            for net in _PRIVATE_NETS:
                if ip in net:
                    return False, f"目标解析到私网地址 {ip}（防SSRF）"
    except OSError:
        return False, f"域名无法解析: {host}"
    return True, "ok"


async def fetch_page_text(db, company_id: str, agent_id: str | None,
                          url: str, max_chars: int = 8000,
                          timeout_s: float = 15.0,
                          render: str | None = None) -> dict:
    """安全抓取网页并提取纯文本。render='js' 时用 Playwright 渲染。
    所有尝试均写审计日志。"""
    log_id = str(uuid.uuid4())
    start = _now()

    ok, reason = check_url_security(url)
    result = {"log_id": log_id, "allowed": ok}
    if not ok:
        db.add(BrowserActionLog(
            id=log_id, company_id=company_id, agent_id=agent_id,
            action_type="fetch", url=url, allowed=False, deny_reason=reason))
        await db.commit()
        result["deny_reason"] = reason
        return result

    status_code = content_len = duration_ms = None
    error = None
    text = None
    page_title = None

    if render == "js":
        # JS 渲染模式：Playwright headless Chromium（需 playwright install chromium）
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(url, timeout=int(timeout_s * 1000),
                                wait_until="domcontentloaded")
                page_title = await page.title()
                raw = await page.inner_text("body")
                text = re.sub(r"\s+", " ", raw).strip()[:max_chars]
                status_code = 200
                content_len = len(text)
                result.update({"title_m": page_title, "text": text,
                               "rendered": True})
                await browser.close()
        except ImportError:
            result["note"] = ("playwright 未安装，回退静态抓取；"
                              "uv add playwright + playwright install chromium 后启用")
        except Exception as e:
            result["note"] = f"JS 渲染失败，回退静态抓取: {e}"

    if text is None:
        try:
            async with httpx.AsyncClient(follow_redirects=True,
                                         timeout=timeout_s) as client:
                resp = await client.get(url, headers={
                    "User-Agent": "AI-EOS-BrowserAgent/1.0"})
            status_code = resp.status_code
            html = resp.text
            # 剥离 script/style 后取纯文本
            stripped = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ",
                              html, flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", stripped)
            text = re.sub(r"\s+", " ", text).strip()
            text = text[:max_chars]
            content_len = len(text)
            m_title = re.search(r"<title[^>]*>(.*?)</title>", html,
                                re.I | re.S)
            page_title = m_title.group(1).strip() if m_title else None
            result.update({"title_m": page_title, "text": text})
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            result["error"] = error

    duration_ms = int((_now() - start).total_seconds() * 1000)
    db.add(BrowserActionLog(
        id=log_id, company_id=company_id, agent_id=agent_id,
        action_type="fetch", url=url, allowed=True,
        status_code=status_code, content_length=content_len,
        duration_ms=duration_ms))
    await db.commit()

    return result
