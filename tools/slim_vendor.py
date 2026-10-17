#!/usr/bin/env python3
"""V4.2 vendor 瘦身：把上游专家原文移入 archive，主目录只留合规最小集。

⚠️ 执行时机：必须在 tools/rewrite_experts.py 全量生成【完成】之后跑。
原因：重写生成器做 10-gram 相似度门需要拿 vendor 原文当基线比对。
    提前瘦身后 similarity_gate 拿不到基线（会自动降级为 0.0 跳过），
    合规门就形同虚设。

保留（合规最小集）：LICENSE / README.md / EXECUTIVE-BRIEF.md / QUICKSTART.md /
SOURCE_NOTE.md（本说明）
移出（→ vendor/agency-agents-zh-archive/）：其余全部 *.md 专家原文
"""
import os, shutil, sys

BASE = os.path.expanduser("~/Documents/张阗/ai-eos")
VENDOR = os.path.join(BASE, "vendor", "agency-agents-zh")
ARCHIVE = os.path.join(BASE, "vendor", "agency-agents-zh-archive")

KEEP = {"LICENSE", "README.md", "EXECUTIVE-BRIEF.md", "QUICKSTART.md",
        "SOURCE_NOTE.md", "PKG-INFO.txt"}

def main():
    moved = kept = 0
    if not os.path.isdir(VENDOR):
        sys.exit("❌ vendor 目录不存在")
    os.makedirs(ARCHIVE, exist_ok=True)
    for f in sorted(os.listdir(VENDOR)):
        fp = os.path.join(VENDOR, f)
        if not os.path.isfile(fp):
            continue
        if f in KEEP:
            kept += 1
            continue
        if f.endswith(".md"):
            dst = os.path.join(ARCHIVE, f)
            shutil.move(fp, dst)
            moved += 1
    # 在 archive 里放一份来源索引
    idx = os.path.join(ARCHIVE, "INDEX.md")
    open(idx, "w", encoding="utf-8").write(
        "# 上游专家原文存档（合规留档）\n\n"
        "来源：jnMetaCode/agency-agents-zh（MIT）。\n"
        "这些 .md 是上游原文，已【不再】作为 AI-EOS 专家库内容来源——"
        "全部专家人设已由 AI-EOS 团队 LLM 独立重写（content/experts/）。\n"
        "保留本存档仅用于：① 相似度门生成期基线；② 上游版权追溯。\n"
        f"共 {moved} 份。\n")
    print(f"✅ 瘦身完成：主目录保留 {kept} 个合规文件，{moved} 份专家原文 → archive/")
    print("   下一步：重新执行 POST /experts/import-original 并推 GitHub")

if __name__ == "__main__":
    main()
