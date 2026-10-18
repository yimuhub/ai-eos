"""系统诊断（V5.4）：一键体检 + 修复建议（学 self-hosted-ai 的 make doctor）

GET /diagnostics（鉴权）：跑一组检查项，每项返回
{ name, ok, detail, fix }。桌面壳「查看 → 🩺 系统诊断」直达 /diagnostics 页。
所有检查都是只读、快速（<2s），失败项不抛异常——诊断的目的就是
在坏的时候还能告诉你哪坏了。
"""
import shutil
import socket
import time
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.routes.clients import _detect_lan_ip, clients_dir
from app.core.config import settings
from app.db.session import get_db
from app.models.user import User

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


@router.get("")
async def run_diagnostics(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, fix: str = ""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "fix": fix})

    # 1) 数据库连通 + 写入健康
    t0 = time.time()
    try:
        await db.execute(text("SELECT 1"))
        ms = int((time.time() - t0) * 1000)
        add("数据库连接", True, f"响应 {ms}ms（{settings.database_url.split('://')[0]}）")
    except Exception as e:
        add("数据库连接", False, f"查询失败：{type(e).__name__}",
            "Postgres 容器没跑？colima start && docker start ai-eos-postgres")

    # 2) 存储目录 + 磁盘剩余
    try:
        from app.api.routes.assets import storage_root
        root = storage_root()
        root.mkdir(parents=True, exist_ok=True)
        du = shutil.disk_usage(str(root))
        free_gb = round(du.free / 1073741824, 1)
        ok = free_gb > 1
        add("磁盘空间", ok, f"存储目录 {root}，剩余 {free_gb}GB",
            "剩余不足 1GB：清理 storage/ 旧交付物或释放磁盘" if not ok else "")
    except Exception as e:
        add("磁盘空间", False, f"检查失败：{e}")

    # 3) 局域网可达性（手机扫码接入的前提）
    lan = _detect_lan_ip()
    add("局域网共享", lan != "127.0.0.1",
        f"局域网地址 {lan}" + ("" if lan != "127.0.0.1" else "（未能探测到，可能纯离线网络）"),
        "确认本机连了 WiFi/有线网；手机需同一局域网" if lan == "127.0.0.1" else "")

    # 4) 更新通道 manifest 状态
    try:
        mdir = clients_dir()
        mf = mdir / "manifest.json"
        if mf.exists():
            import json
            m = json.loads(mf.read_text())
            nfiles = len(m.get("files", {})) if isinstance(m, dict) else 0
            add("自动升级通道", True,
                f"manifest 最新版 {m.get('latest', '?')}，{nfiles} 个平台文件")
        else:
            add("自动升级通道", False, "未部署 manifest.json",
                "跑 tools/publish_client_updates.py（--from-local 或 --release）发布一次")
    except Exception as e:
        add("自动升级通道", False, f"manifest 损坏或不可读：{e}",
            "重跑发布工具；旧版备份在 .bak-* 文件里")

    # 5) 卡住的会议/任务自愈检查（后台僵尸）
    try:
        stuck_m = (await db.execute(text(
            "SELECT COUNT(*) FROM meetings WHERE status = 'RUNNING'"))).scalar() or 0
        stuck_t = (await db.execute(text(
            "SELECT COUNT(*) FROM tasks WHERE status = 'RUNNING'"))).scalar() or 0
        total = stuck_m + stuck_t
        add("执行状态健康", total == 0,
            f"运行中会议 {stuck_m} / 任务 {stuck_t}" if total
            else "无卡住的执行（重启时自动自愈）",
            f"{total} 个卡在执行中：重启后端触发自愈，或在「会议中心/任务」手动停止"
            if total else "")
    except Exception as e:
        add("执行状态健康", True, f"统计失败（不影响服务）：{e}")

    # 6) 本机时间（证书/过期判断的基础，留档供前端对照）
    add("本机时间", True, datetime.now().strftime("%Y-%m-%d %H:%M"))

    return {
        "ok": all(c["ok"] for c in checks),
        "checked_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "checks": checks,
    }
