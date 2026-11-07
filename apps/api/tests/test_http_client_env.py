"""出站 HTTP 必须走 app.core.http_client，且不被开发机代理环境左右。

回归对象：httpx 默认 `trust_env=True`，会读系统的 HTTP(S)_PROXY / NO_PROXY。
某台开发机的 `NO_PROXY=[::1]` 会让 httpx 0.28 在建客户端时直接抛
`InvalidURL: Invalid port: ':1]'`——症状是"浏览器能打开、后端调不通"，
报错还和网络毫无关系，排障成本极高。

因此两道护栏：
1. 本模块构造的客户端不受系统代理变量影响，代理只认 AIEOS_HTTP_PROXY；
2. 全仓不允许绕开本模块直接 `httpx.AsyncClient(...)` / `httpx.Client(...)`。
"""
from __future__ import annotations

import ast
import pathlib

import httpx
import pytest

from app.core.http_client import PROXY_ENV, http_client

API_ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_DIR = API_ROOT / "app"
CLIENT_MODULE = APP_DIR / "core" / "http_client.py"

_OS_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                  "http_proxy", "https_proxy", "all_proxy", "no_proxy")


@pytest.fixture
def env_isolated(monkeypatch):
    for name in _OS_PROXY_VARS + (PROXY_ENV,):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_os_proxy_env_really_breaks_plain_httpx(env_isolated):
    """先证明这段环境确实能让裸 httpx 崩掉，后面的断言才有意义。"""
    env_isolated.setenv("NO_PROXY", "[::1]")
    with pytest.raises(httpx.InvalidURL):
        httpx.AsyncClient()


async def test_default_client_ignores_os_proxy_env(env_isolated):
    env_isolated.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    env_isolated.setenv("NO_PROXY", "[::1]")
    async with http_client() as client:
        assert client.trust_env is False
        assert not client._mounts, "不应根据系统变量生成代理挂载"


async def test_explicit_proxy_env_is_honoured(env_isolated):
    env_isolated.setenv(PROXY_ENV, "http://127.0.0.1:9")
    async with http_client() as client:
        assert client.trust_env is False
        assert client._mounts, "设了 AIEOS_HTTP_PROXY 就应当走代理"


def _raw_client_constructions() -> list[str]:
    hits: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if path == CLIENT_MODULE or "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Attribute)
                    and func.attr in {"AsyncClient", "Client"}
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "httpx"):
                hits.append(f"{path.relative_to(API_ROOT)}:{node.lineno}")
    return hits


def test_no_module_constructs_httpx_clients_directly():
    hits = _raw_client_constructions()
    assert not hits, (
        "出站请求请改用 app.core.http_client.http_client()，"
        "否则会重新被开发机的代理环境干扰：" + ", ".join(hits))
