#!/usr/bin/env python3
"""发布客户端自动升级包（V5.2）

从 GitHub Release（yimu hub/ai-eos）拉取最新安装包，写入「你自己服务器」
的更新目录并生成 manifest.json —— 桌面壳 / Android 客户端启动时就会
从本机的 /clients/update/* 检查并升级到该版本。

用法（在服务器上跑）：
    python3 tools/publish_client_updates.py            # 写默认更新目录
    python3 tools/publish_client_updates.py --dir /自定义/storage/clients
    python3 tools/publish_client_updates.py --release clients-20261018  # 指定 release

凭证：环境变量 GH_TOKEN，或自动读 ~/Documents/张阗/.github_token（不再要用户给）。

目录约定与后端一致：默认目标 = backend storage_root()/clients，
storage_root 的解析逻辑直接复用 app.api.routes.assets，保证万无一失。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import urllib.request
from pathlib import Path

REPO = "yimuhub/ai-eos"
TOKEN_FILE = Path.home() / "Documents" / "张阗" / ".github_token"

# 各平台更新键 → 文件名匹配（发布 9 个资产中取 5 个主安装器）
PLATFORM_PATTERNS = {
    "darwin-arm64": lambda n: n.endswith("-arm64.dmg"),
    "darwin-x64": lambda n: n.endswith(".dmg") and "-arm64" not in n,
    "win32": lambda n: n.endswith("win-setup.exe"),
    "linux-x64": lambda n: n.endswith(".AppImage"),
    "android": lambda n: n.endswith(".apk"),
}


def get_token() -> str:
    import os
    t = os.environ.get("GH_TOKEN", "").strip()
    if t:
        return t
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    sys.exit("找不到 GitHub token：设 GH_TOKEN 或放 " + str(TOKEN_FILE))


def gh(url: str, token: str) -> bytes:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def resolve_release(token: str, tag: str | None):
    if tag:
        data = json.loads(gh(f"https://api.github.com/repos/{REPO}/releases/tags/{tag}", token))
    else:
        items = json.loads(gh(f"https://api.github.com/repos/{REPO}/releases?per_page=10", token))
        # 优先 clients-* 标签（客户端发布流），否则最新 release
        clients = [i for i in items if str(i.get("tag_name", "")).startswith("clients")]
        data = (clients or items)[0]
    assets = {a["name"]: a for a in data.get("assets", [])}
    return data, assets


def default_clients_dir() -> Path:
    """与后端 assets.storage_root()/clients 保持一致的解析。

    关键点：必须在 apps/api 目录下 import，env_loader 才会注入
    apps/api/.env（其中 STORAGE_DIR 绝对路径覆盖默认 ./storage）。
    从其它 CWD 跑会得到不同结果 —— 直接 os.chdir 到 apps/api。
    """
    api_dir = Path(__file__).resolve().parents[1] / "apps" / "api"
    cwd = os.getcwd()
    sys.path.insert(0, str(api_dir))
    os.chdir(api_dir)
    try:
        from app.api.routes.assets import storage_root  # type: ignore
        return Path(storage_root()) / "clients"
    except Exception:
        # 依赖缺失（比如用系统 python 跑、没有 pydantic_settings）→
        # 直接读 .env 的 STORAGE_DIR，保证与后端一致
        envf = api_dir / ".env"
        if envf.exists():
            for line in envf.read_text().splitlines():
                if line.startswith("STORAGE_DIR="):
                    return Path(line.split("=", 1)[1].strip()).resolve() / "clients"
        return api_dir / "storage" / "clients"
    finally:
        os.chdir(cwd)


def main():
    ap = argparse.ArgumentParser(description="发布客户端自动升级包")
    ap.add_argument("--release", help="GitHub Release 标签（默认：最新 clients-* release）")
    ap.add_argument("--from-local", help="不从 GitHub 下载，改用本地安装包目录（如 客户端交付_V5.1/）")
    ap.add_argument("--dir", help="更新目录（默认：后端 storage_root()/clients）")
    ap.add_argument("--latest", help="版本号（--from-local 时必填或自动从文件名提取）")
    ap.add_argument("--notes", help="更新说明（manifest.notes）")
    args = ap.parse_args()

    target = Path(args.dir) if args.dir else default_clients_dir()
    target.mkdir(parents=True, exist_ok=True)

    if args.from_local:
        src = Path(args.from_local)
        if not src.exists():
            sys.exit(f"本地目录不存在：{src}")
        names = [p.name for p in src.iterdir() if p.suffix in (".dmg", ".zip", ".exe", ".AppImage", ".deb", ".apk")]
        if not names:
            sys.exit(f"{src} 里没有安装包文件")
        versions = set()
        files: dict[str, dict] = {}
        total = 0
        for name in names:
            m = re.search(r"(\d+\.\d+\.\d+)", name)
            if m:
                versions.add(m.group(1))
            for key, match in PLATFORM_PATTERNS.items():
                if match(name):
                    shutil.copy2(src / name, target / name)
                    files[key] = {"file": name, "size_mb": round((src / name).stat().st_size / 1048576, 1)}
                    total += 1
                    print(f"  ⧉ 复制 {name} → {target.name}/")
        latest = args.latest or (max(versions) if versions else "0.0.0")
        manifest = {
            "latest": latest,
            "tag": f"local-{src.name}",
            "notes": args.notes or f"本地发布：{src.name}",
            "files": files,
        }
        (target / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✅ 已发布 {total} 个更新包 → {target}")
        print(f"   manifest.json：latest={latest}，平台键：{', '.join(files) or '(无)'}")
        return

    token = get_token()
    data, assets = resolve_release(token, args.release)
    tag = data.get("tag_name", "?")
    print(f"== 使用 GitHub Release：{tag}")

    # 从安装包文件名提取版本号（如 AI-EOS-5.2.0-arm64.dmg → 5.2.0）
    versions = set()
    for name in assets:
        m = re.search(r"(\d+\.\d+\.\d+)", name)
        if m:
            versions.add(m.group(1))
    latest = max(versions) if versions else "0.0.0"

    files: dict[str, dict] = {}
    total = 0
    for key, match in PLATFORM_PATTERNS.items():
        hit = [n for n in assets if match(n)]
        if not hit:
            print(f"  ⚠️  {key}：release 里没有匹配的安装包，跳过")
            continue
        name = hit[0]
        url = assets[name]["browser_download_url"]
        dest = target / name
        print(f"  ↓ 下载 {name} ({assets[name].get('size', 0) / 1048576:.0f} MB) → {dest.name}")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
        total += 1
        files[key] = {"file": name, "size_mb": round(dest.stat().st_size / 1048576, 1)}

    manifest = {
        "latest": latest,
        "tag": tag,
        "notes": (data.get("body") or "")[:2000],
        "files": files,
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n✅ 已发布 {total} 个更新包 → {target}")
    print(f"   manifest.json：latest={latest}，平台键：{', '.join(files) or '(无)'}")
    print(f"   客户端现在启动即可从 {target.parent.parent.name}/…/clients/update 检测到 v{latest}")


if __name__ == "__main__":
    main()
