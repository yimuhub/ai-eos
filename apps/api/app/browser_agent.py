"""Browser Agent Foundation (P2-13).

安全边界 + 操作日志。Agent 可以请求浏览网页，但必须经过：
1. 域名白名单检查
2. 协议检查（仅 https）
3. 私有地址/内网 IP 黑名单（防 SSRF）
4. 每次操作落库审计
5. **逐跳重定向复检**：关闭 httpx 自动跟随，最多 5 跳，每跳都过
   `core.net_guard.validate_outbound_url`（allow_private=False）；Playwright
   渲染分支用 route 拦截对每个请求先校验，导航后再校验最终 URL。

执行层用 httpx 抓取文本（无 JS 渲染，安全可控），可选 Playwright。
"""
import ipaddress
import re
import socket
import uuid
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx

from app.core.http_client import http_client

from sqlalchemy import JSON, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.net_guard import OutboundURLRejected, validate_outbound_url
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


_MAX_REDIRECTS = 5
_REDIRECT_CODES = {301, 302, 303, 307, 308}


class _UpstreamHTTPError(Exception):
    """上游返回非 2xx；只携带状态码与原因，绝不携带响应体。"""

    def __init__(self, status_code: int, reason: str):
        self.status_code = status_code
        self.reason = (reason or "")[:60]
        super().__init__(f"HTTP {status_code} {self.reason}".strip())


def _require_safe_hop(url: str) -> None:
    """每一跳（含重定向目标）都必须重新校验，且仅允许 https。"""
    scheme = urlparse(url).scheme
    if scheme != "https":
        raise OutboundURLRejected(f"仅允许 https（当前 {scheme or '无'}）")
    validate_outbound_url(url, allow_private=False)


async def _fetch_static(url: str, timeout_s: float) -> httpx.Response:
    """手动逐跳跟随重定向（最多 5 跳），每跳先过 SSRF 闸门。"""
    headers = {"User-Agent": "AI-EOS-BrowserAgent/1.0"}
    async with http_client(follow_redirects=False, timeout=timeout_s) as client:
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            _require_safe_hop(current)
            resp = await client.get(current, headers=headers)
            if resp.status_code in _REDIRECT_CODES:
                location = resp.headers.get("location")
                if location:
                    current = urljoin(str(resp.url), location)
                    continue
            return resp
    raise OutboundURLRejected("重定向次数超过上限（最多 5 跳）")


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
    text = None
    page_title = None

    if render == "js":
        # JS 渲染模式：Playwright headless Chromium（需 playwright install chromium）
        try:
            from playwright.async_api import async_playwright

            async def _guard_route(route):
                # 每个 http(s) 子请求（含重定向）发出前先校验，
                # 防止浏览器把私网内容带回；data:/blob: 等本地资源放行。
                req_url = route.request.url
                if urlparse(req_url).scheme in ("http", "https"):
                    try:
                        _require_safe_hop(req_url)
                    except OutboundURLRejected:
                        await route.abort()
                        return
                await route.continue_()

            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.route("**/*", _guard_route)
                await page.goto(url, timeout=int(timeout_s * 1000),
                                wait_until="domcontentloaded")
                # 导航结束后再复核最终 URL（防止重定向到私网/元数据）
                _require_safe_hop(page.url)
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
            result["note"] = f"JS 渲染失败，回退静态抓取: {type(e).__name__}"

    if text is None:
        try:
            resp = await _fetch_static(url, timeout_s)
            status_code = resp.status_code
            if status_code >= 400:
                # 不回显上游响应体，只给状态码 + 简短原因
                raise _UpstreamHTTPError(status_code, resp.reason_phrase)
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
        except _UpstreamHTTPError as e:
            result["error"] = str(e)[:200]
        except OutboundURLRejected as e:
            result["error"] = f"目标地址被拒绝：{e}"[:200]
        except Exception as e:
            # 只保留异常类型与截断信息，避免把上游 body 带出去
            result["error"] = f"{type(e).__name__}: {str(e)[:150]}"

    duration_ms = int((_now() - start).total_seconds() * 1000)
    db.add(BrowserActionLog(
        id=log_id, company_id=company_id, agent_id=agent_id,
        action_type="fetch", url=url, allowed=True,
        status_code=status_code, content_length=content_len,
        duration_ms=duration_ms))
    await db.commit()

    return result
