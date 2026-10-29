"""出站 URL 安全闸门（SSRF 防护）。

两个使用面，策略不同，必须显式选择：

- `allow_private=False`（默认）：面向**用户提供的外部目标**（网页抓取、webhook）。
  只允许 http(s)，解析后必须是公网地址；回环 / 私网 / 链路本地 / 云元数据
  一律拒绝。DNS 解析一次、连接后对端 IP 再校一次由调用方负责（见
  `assert_peer_is_safe`）。
- `allow_private=True`：面向**管理员自己配置的模型服务**（本地 llama.cpp /
  Ollama 常监听 192.168.x）。允许私网，但仍拒绝云元数据地址，因为那是
  云主机上最值钱的内网目标，且没有正当理由被当成模型服务。

`check_url_security`（browser_agent 原有实现）保留，本模块不取代它，
只补齐「逐跳重定向复检」与「对端 IP 复检」这两处缺口。
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# 云厂商元数据服务：任何场景都不允许
_METADATA_HOSTS = {
    "169.254.169.254",          # AWS / GCP / Azure / OpenStack
    "metadata.google.internal",
    "metadata.tencentyun.com",
    "100.100.100.200",          # 阿里云
}
_METADATA_NETS = [ipaddress.ip_network("169.254.0.0/16")]

_BLOCKED_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",        # CGNAT
        "127.0.0.0/8",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",        # benchmark
        "224.0.0.0/4",          # multicast
        "240.0.0.0/4",          # reserved
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
]


class OutboundURLRejected(ValueError):
    """出站地址不安全。调用方应转成 4xx，不要 5xx。"""


def _ip_is_blocked(ip: ipaddress._BaseAddress, allow_private: bool) -> bool:
    for net in _METADATA_NETS:
        if ip.version == net.version and ip in net:
            return True
    if allow_private:
        return False
    for net in _BLOCKED_NETS:
        if ip.version == net.version and ip in net:
            return True
    return False


def _resolve_ips(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as e:
        raise OutboundURLRejected(f"无法解析主机 {host}：{e}") from e
    return [info[4][0] for info in infos]


def assert_peer_is_safe(peer_ip: str, *, allow_private: bool = False) -> None:
    """连接建立后复核真实对端地址，堵 DNS rebinding。"""
    try:
        ip = ipaddress.ip_address(peer_ip)
    except ValueError as e:
        raise OutboundURLRejected(f"对端地址不可解析：{peer_ip}") from e
    if _ip_is_blocked(ip, allow_private):
        raise OutboundURLRejected(f"对端地址不允许访问：{peer_ip}")


def validate_outbound_url(url: str, *, allow_private: bool = False) -> str:
    """校验一个出站 URL，返回规范化后的 URL。不安全则抛 OutboundURLRejected。"""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https"):
        raise OutboundURLRejected("只允许 http/https 地址")
    host = parsed.hostname
    if not host:
        raise OutboundURLRejected("地址缺少主机名")
    if host.lower() in _METADATA_HOSTS:
        raise OutboundURLRejected("拒绝访问云元数据地址")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    for ip_str in _resolve_ips(host, port):
        assert_peer_is_safe(ip_str, allow_private=allow_private)
    return url


async def validate_outbound_url_async(url: str, *, allow_private: bool = False) -> str:
    """异步版本：DNS 解析丢线程，避免阻塞事件循环。"""
    import anyio

    return await anyio.to_thread.run_sync(
        lambda: validate_outbound_url(url, allow_private=allow_private))
