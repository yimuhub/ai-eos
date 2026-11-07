"""统一的出站 HTTP 客户端入口。

为什么不允许直接 `httpx.AsyncClient(...)`：

1. httpx 默认 `trust_env=True`，会读系统 HTTP(S)_PROXY / NO_PROXY。
   httpx 0.28 解析 `NO_PROXY=[::1]` 之类的条目会抛
   `InvalidURL: Invalid port: ':1]'`——表现为"浏览器能打开、后端调不通"，
   而且报错信息和网络完全无关，排障时极具误导性。
2. 要不要走代理必须是**部署显式决定**：需要代理的机器设
   `AIEOS_HTTP_PROXY=http://host:port`，而不是被某台开发机的环境变量
   悄悄改变行为。

需要代理时设 AIEOS_HTTP_PROXY；无论是否设置，都不再读 HTTPS_PROXY /
NO_PROXY。`tests/test_http_client_env.py` 会拦住绕开本模块的直连构造。
"""
from __future__ import annotations

import os

import httpx

PROXY_ENV = "AIEOS_HTTP_PROXY"


def http_client(**kwargs) -> httpx.AsyncClient:
    """构造出站 AsyncClient：默认不读环境变量，代理只认 AIEOS_HTTP_PROXY。"""
    kwargs.setdefault("trust_env", False)
    proxy = os.getenv(PROXY_ENV, "").strip()
    if proxy and "proxy" not in kwargs and "mounts" not in kwargs:
        kwargs["proxy"] = proxy
    return httpx.AsyncClient(**kwargs)
