"""客户端自动升级通道（V5.2）

把「最新版安装包 + manifest」托管在你自己的服务器上（私有仓库的
GitHub Release 匿名下载不了，更新源走自己的 FastAPI 最稳）。

- GET /clients/update/check?platform=darwin&arch=arm64&current=5.1.0
  → 与服务器 data/clients/manifest.json 比对，返回是否有新版 + 下载地址
- GET /clients/update/download/{filename}
  → 下发 storage/clients/ 下的安装包（防目录穿越）

manifest 由 `tools/publish_client_updates.py` 生成（从 GitHub Release
拉安装包写入服务器）。没有 manifest 时 check 优雅返回 has_update=false，
前端不显示升级条，不影响任何现有功能。
"""
import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.api.routes.assets import storage_root

router = APIRouter(prefix="/clients", tags=["clients"])


def clients_dir() -> Path:
    p = storage_root() / "clients"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _ver_tuple(v: str | None):
    if not v:
        return None
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def _cmp_version(a: str | None, b: str | None) -> int:
    """版本比较（补零对齐，避免 5.2 与 5.2.0 被误判成不同版本）。返回 -1/0/1。"""
    ta, tb = _ver_tuple(a) or (0,), _ver_tuple(b) or (0,)
    n = max(len(ta), len(tb))
    ta += (0,) * (n - len(ta))
    tb += (0,) * (n - len(tb))
    return (ta > tb) - (ta < tb)


class UpdateInfo(BaseModel):
    has_update: bool
    current: str | None = None
    latest: str | None = None
    notes: str | None = None
    file: str | None = None
    url: str | None = None
    size_mb: float | None = None
    sha256: str | None = None
    message: str | None = None


def _platform_key(platform: str, arch: str) -> str:
    # win32 / android 的 manifest 键不带 arch（Win 更新通道只发 x64 安装包；
    # 拼出 "win32-x64" 会永远匹配不到 manifest → Windows 端收不到更新）
    if platform in ("android", "win32"):
        return platform
    return f"{platform}-{arch or 'x64'}"


@router.get("/update/check", response_model=UpdateInfo)
def check_update(platform: str, arch: str = "", current: str | None = None):
    """检查客户端更新。platform ∈ darwin/win32/linux/android"""
    mfile = clients_dir() / "manifest.json"
    if not mfile.exists():
        return UpdateInfo(
            has_update=False, current=current,
            message="服务器未部署更新通道（先运行 tools/publish_client_updates.py）")
    try:
        m = json.loads(mfile.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return UpdateInfo(has_update=False, current=current,
                         message="manifest.json 损坏，请重新运行发布工具")
    latest = m.get("latest")
    files = m.get("files")
    if not isinstance(files, dict):
        files = {}
    entry = files.get(_platform_key(platform, arch))
    fname = (entry or {}).get("file") if isinstance(entry, dict) else None
    if not fname or not latest:
        return UpdateInfo(has_update=False, current=current, latest=latest,
                          message="该平台暂无更新包")
    safe = Path(fname).name  # 与 download 端点同样的消毒：manifest 里的 ../ 不能逃逸
    fpath = clients_dir() / safe
    if not fpath.is_file():
        return UpdateInfo(has_update=False, current=current, latest=latest,
                          message=f"更新文件缺失：{safe}")
    has = current is None or _cmp_version(latest, current) > 0
    return UpdateInfo(
        has_update=has, current=current, latest=latest, notes=m.get("notes"),
        file=safe,
        url=f"/clients/update/download/{safe}",
        size_mb=round(fpath.stat().st_size / 1048576, 1),
        sha256=(entry or {}).get("sha256"))


@router.get("/update/download/{filename}")
def download_update(filename: str):
    safe = Path(filename).name  # 防目录穿越
    if safe in ("", ".", ".."):  # 裸 '.'/'..' 的 .name 仍是自身 → 挡掉目录
        raise HTTPException(404, "更新文件不存在")
    fpath = clients_dir() / safe
    if not fpath.is_file():
        raise HTTPException(404, "更新文件不存在")
    return FileResponse(fpath, filename=safe, media_type="application/octet-stream")


# ---------- 局域网共享（V5.3）：手机扫码接入 ----------
import socket

from fastapi import Depends

from app.api.deps import get_current_user


def _detect_lan_ip() -> str:
    """探测本机对外的局域网 IPv4。

    ① UDP "connect" 8.8.8.8：不实际发包，只是让内核选出「出去会用的网卡」，
    拿到其源 IP（多网卡机器上比 gethostname 靠谱）；
    ② 兜底：枚举本机所有 IPv4，挑第一个私网段（10/172.16-31/192.168）；
    ③ 都失败返回 127.0.0.1（前端会提示「无法确定局域网地址」）。
    全程无新依赖、不阻塞（超时 0.5s）。
    """
    def _is_lan(ip: str) -> bool:
        return ip.startswith(("10.", "192.168.")) or ip.startswith("172.")

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and ip != "127.0.0.1" and (ip == "0.0.0.0" or _is_lan(ip)):
            return "127.0.0.1" if ip == "0.0.0.0" else ip
    except OSError:
        pass
    try:
        for family, _mode, _ttl, _socktype, sockaddr in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if family != socket.AF_INET:
                continue
            ip = sockaddr[0]  # AF_INET 的 sockaddr = (ip, port)
            if isinstance(ip, str) and _is_lan(ip):
                return ip
    except OSError:
        pass
    return "127.0.0.1"


@router.get("/lan", response_model=None)
async def lan_share_info(user=Depends(get_current_user)):
    """返回本机局域网接入信息（📡 局域网共享页的数据源）。

    前端拿 lan_ip + 自己的 window.location.port 拼扫码地址；
    需要鉴权：这是内部网络拓扑信息，不能对整个局域网裸奔。
    """
    ip = _detect_lan_ip()
    return {
        "lan_ip": ip,
        "reachable": ip != "127.0.0.1",
        # 手机浏览器扫到的入口 = 前端端口（桌面壳 8000 / dev 3000）；
        # 后端 API 端口仅供壳与 App 直连（App 的「更换服务器」页填写）。
        "note": "手机需与本机在同一局域网；扫码进入 Web 版，Android App 在「服务器」页填写本地址",
    }
