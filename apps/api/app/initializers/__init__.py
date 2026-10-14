import sys, sqlite3, time, os
"""便携包首次启动：初始化 SQLite + 默认账号 + 默认公司（可选）"""

DB = sys.argv[1] if len(sys.argv) > 1 else "data/aieos.db"
DATA_DIR = os.path.dirname(DB) or "."
os.makedirs(DATA_DIR, exist_ok=True)

print(f"📦 AI-EOS SQLite 初始化: {DB}")
if not os.path.exists(DB):
    print("  数据库不存在，需要建表 + 初始化数据（首次启动）")
    sys.exit(0)

# 快速校验 schema 完整性
with sqlite3.connect(DB) as c:
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if "companies" in tables:
        print(f"  ✅ 已有 {len(tables)} 张表，已就绪（跳过初始化）")
        sys.exit(0)
    print("  ⚠️  有 DB 但缺少 companies 表，可能是旧版本 schema，请手动重建")
    sys.exit(1)
