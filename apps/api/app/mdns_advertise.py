"""mDNS 局域网设备发现（V5.4）：让同网设备「自动找到」本机的 AI-EOS。

学 Home Assistant 的 zeroconf 发现模型：服务起来即在局域网广播
`_aieos._tcp.local.`（ai-eos.local:port），手机 App / 未来的 Web 发现页
不用手敲 IP。

边界与降级：
- zeroconf 没装 / 无可用网卡 / 广播失败 → 跳过并记 WARNING 日志，服务照常启动
  （mDNS 只是锦上添花，局域网共享的主体是 /clients/lan + 二维码）
- 环境变量 AI_EOS_MDNS=0 可强制关闭（部分多播受限的网络会刷屏告警）
- 广播线程 daemon，不拦退出
"""
import os
import socket

from app.core.logging_config import get_logger
from app.version import __version__ as _ADVERTISE_VERSION

logger = get_logger(__name__)

_TYPE = "_aieos._tcp"
_DOMAIN = "local."
_service = None


def _service_name() -> str:
    """多实例同网时按端口区分：ai-eos-8001.local。"""
    port = str(_local_port())
    return f"ai-eos-{port}.{_TYPE}." + _DOMAIN


def _local_port() -> int:
    # database_url 不携带服务端口；uvicorn 的 --port 通过环境变量传入
    return int(os.environ.get("AIEOS_HTTP_PORT") or _guess_port())


def _guess_port() -> int:
    # 便携壳 8001 / dev 8000：看哪个在监听
    for p in (8001, 8000):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("0.0.0.0", p))
            s.close()
            # 能 bind 说明没被占 → 不是我们的端口，继续
        except OSError:
            return p  # 被占用 = 正监听中
    return 8000


def start() -> None:
    """在应用 lifespan 启动后调用。任何异常都不抛。"""
    global _service
    if os.environ.get("AI_EOS_MDNS") == "0":
        logger.info("[mdns] 已禁用（AI_EOS_MDNS=0）")
        return
    try:
        from zeroconf import Zeroconf, ServiceInfo
    except ImportError:
        logger.warning("[mdns] zeroconf 未安装，跳过局域网广播（pip install zeroconf）")
        return
    try:
        zc = Zeroconf()
        info = ServiceInfo(
            _TYPE + "." + _DOMAIN,
            _service_name(),
            addresses=_lan_addresses(),
            port=_local_port(),
            properties={"version": _ADVERTISE_VERSION, "name": "AI-EOS", "type": "tcp"},
        )
        zc.register_service(info)
        _service = zc
        logger.info("[mdns] 已广播 %s（同网设备可自动发现）", _service_name())
    except Exception as e:
        logger.warning("[mdns] 广播失败（不影响服务）：%s", e)
        _service = None


def _lan_addresses() -> list:
    addrs = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        addrs.append(socket.inet_aton(ip))
    except OSError as e:
        # 降级不阻断（下面回退 127.0.0.1），但留痕：这会导致广播只带本机地址，
        # 同网设备发现到的地址不可达
        logger.warning("[mdns] 局域网地址探测失败，回退 127.0.0.1 广播：%s", e)
    if not addrs:
        addrs.append(socket.inet_aton("127.0.0.1"))
    return addrs


def stop() -> None:
    global _service
    if _service is not None:
        try:
            _service.close()
        except Exception as e:
            # 关闭失败不阻断退出流程（daemon 场景），但要留痕方便排查资源泄漏
            logger.warning("[mdns] Zeroconf 关闭失败：%s: %s", type(e).__name__, e)
        _service = None
