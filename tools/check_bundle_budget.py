#!/usr/bin/env python3
"""前端构建产物体积预算门禁（C7 性能预算的 CI 可验证部分）。

对 Next.js 构建产物（apps/web/.next）做 gzip 后体积统计，并与脚本内的
预算常量比较。只做「体积代理」，不测量真实网络耗时（首屏 1.5s 这类指标
需要浏览器环境，见 docs/性能预算.md）。

用法：
    python3 tools/check_bundle_budget.py --dir apps/web/.next
    python3 tools/check_bundle_budget.py --dir apps/web/.next --json
    python3 tools/check_bundle_budget.py --dir apps/web/.next --budget-kb 1  # 仅本地反向验证

退出码：
    0 —— 全部指标在预算内
    1 —— 有指标超出预算（打印 current / budget / 超出量）
    2 —— 构建目录或 manifest 缺失（通常是没有先 build），无法测量

无第三方依赖，只用标准库；gzip 压缩级别固定为 9，保证同一产物结果可复现。
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sys
import unicodedata

# ---------------------------------------------------------------------------
# 预算常量（单位：KB，gzip -9 后的字节数 / 1024）
#
# 基线来源：2026-10-31 在本机 `cd apps/web && npm run build`（Next 14.2.35，
# Node v24.19.0，默认 production 配置），解析 .next/app-build-manifest.json、
# build-manifest.json 与 static/ 下全部产物得到：
#     first_load_js_gzip_kb = 118.43   （首页 "/"：rootMainFiles + 根 layout +
#                                        (main) layout + (main)/page）
#     total_js_gzip_kb      = 610.52   （static/chunks 下全部 .js）
#     total_css_gzip_kb     =  12.27   （static/css 下全部 .css）
#     max_chunk_gzip_kb     =  52.40   （最大的单个 chunk：fd9d1056-*.js）
#
# 阈值规则：基线 × 1.10，向上取整到 10KB（留出 10% 给正常增量与不同
# Node/构建环境的微小差异，避免刚落地就必红）。
#   118.43 × 1.10 = 130.27  -> 140
#   610.52 × 1.10 = 671.57  -> 680
#    12.27 × 1.10 =  13.50  ->  20
#    52.40 × 1.10 =  57.64  ->  60
#
# 调整流程：先在目标机器上重新 build、用本脚本打印的真实值作为新基线，
# 再按同样规则改这里，并在 PR 说明里贴出前后对比。不要凭感觉下调。
# ---------------------------------------------------------------------------
DEFAULT_BUDGETS_KB = {
    "first_load_js_gzip_kb": 140.0,
    "total_js_gzip_kb": 680.0,
    "total_css_gzip_kb": 20.0,
    "max_chunk_gzip_kb": 60.0,
}

METRIC_LABELS = {
    "first_load_js_gzip_kb": "首屏 JS（首页 + 共享 chunk）",
    "total_js_gzip_kb": "全站 JS（所有 chunk）",
    "total_css_gzip_kb": "全站 CSS",
    "max_chunk_gzip_kb": "最大单 chunk",
}

# 首页在 app-build-manifest 里的候选 key（Next App Router：路由组会进 key）
HOME_PAGE_KEYS = ("/(main)/page", "/page", "/")


def gzip_kb(path: str) -> float:
    """返回文件 gzip -9 后的体积（KB）。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    return len(gzip.compress(raw, compresslevel=9)) / 1024.0


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def resolve_home_chain(pages: dict) -> tuple[str | None, list[str]]:
    """找到首页 page 条目及其 layout 链（根 layout + 路由组 layout）。

    返回 (page_key, layout_keys)。layout 链用前缀匹配：例如 page 为
    "/(main)/page" 时，"/layout" 与 "/(main)/layout" 都属于它的链路。
    """
    page_key = next((k for k in HOME_PAGE_KEYS if k in pages), None)
    if page_key is None:
        return None, []

    route = page_key[: -len("/page")]  # "/page" -> ""，"/(main)/page" -> "/(main)"
    layout_keys = []
    for key in pages:
        if not key.endswith("/layout"):
            continue
        prefix = key[: -len("/layout")]  # "/layout" -> ""，"/(main)/layout" -> "/(main)"
        if route == prefix or route.startswith(prefix + "/"):
            layout_keys.append(key)
    return page_key, layout_keys


def collect_first_load_js(root: str, app_manifest: dict, build_manifest: dict):
    """按 manifest 解析首页首屏 JS 集合（rootMainFiles + layout 链 + 页面）。"""
    pages = app_manifest.get("pages", {})
    page_key, layout_keys = resolve_home_chain(pages)

    files = list(build_manifest.get("rootMainFiles", []))
    chain_keys = layout_keys + ([page_key] if page_key else [])
    for key in chain_keys:
        for rel in pages.get(key, []):
            if rel.endswith(".js"):
                files.append(rel)

    # 去重并保持稳定顺序
    seen = set()
    ordered = []
    for rel in files:
        if rel not in seen:
            seen.add(rel)
            ordered.append(rel)
    return ordered, page_key, layout_keys


def sum_gzip_kb(root: str, rel_files: list[str]) -> tuple[float, list[str]]:
    """返回 (总 KB, 缺失文件列表)。缺失说明 manifest 与产物不一致。"""
    total = 0.0
    missing = []
    for rel in rel_files:
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            missing.append(rel)
            continue
        total += gzip_kb(path)
    return total, missing


def measure(root: str) -> dict:
    app_manifest_path = os.path.join(root, "app-build-manifest.json")
    build_manifest_path = os.path.join(root, "build-manifest.json")
    missing_manifests = [
        p for p in (app_manifest_path, build_manifest_path) if not os.path.isfile(p)
    ]
    if missing_manifests:
        raise FileNotFoundError(
            "缺少构建 manifest：" + ", ".join(os.path.basename(p) for p in missing_manifests)
        )

    app_manifest = load_json(app_manifest_path)
    build_manifest = load_json(build_manifest_path)

    first_files, home_page, layouts = collect_first_load_js(root, app_manifest, build_manifest)
    first_js_kb, first_missing = sum_gzip_kb(root, first_files)

    all_js = sorted(glob.glob(os.path.join(root, "static", "chunks", "**", "*.js"), recursive=True))
    all_css = sorted(glob.glob(os.path.join(root, "static", "css", "**", "*.css"), recursive=True))
    if not all_js:
        raise FileNotFoundError("static/chunks 下没有 .js 产物")
    # CSS 允许为 0（例如未来改成纯内联样式），此时 total_css 记 0 即可。

    js_sizes = {p: gzip_kb(p) for p in all_js}
    css_sizes = {p: gzip_kb(p) for p in all_css}

    metrics = {
        "first_load_js_gzip_kb": round(first_js_kb, 2),
        "total_js_gzip_kb": round(sum(js_sizes.values()), 2),
        "total_css_gzip_kb": round(sum(css_sizes.values()), 2),
        "max_chunk_gzip_kb": round(max(js_sizes.values()), 2),
    }

    max_chunk = max(js_sizes, key=js_sizes.get)
    return {
        "metrics": metrics,
        "details": {
            "home_page_entry": home_page,
            "home_layout_entries": layouts,
            "first_load_file_count": len(first_files),
            "js_file_count": len(all_js),
            "css_file_count": len(all_css),
            "max_chunk_file": os.path.relpath(max_chunk, root),
            "missing_first_load_files": first_missing,
        },
    }


def build_report(root: str, budgets: dict) -> dict:
    measured = measure(root)
    missing = measured["details"]["missing_first_load_files"]
    if missing:
        # manifest 指向不存在的 chunk，说明产物不完整；此时算出来的首屏体积
        # 会偏小，不能当作通过，直接按「无法测量」处理。
        raise FileNotFoundError(
            "manifest 引用的首屏文件不存在: " + ", ".join(missing[:5])
            + (" ..." if len(missing) > 5 else "")
        )
    metrics = measured["metrics"]
    over = []
    for name, budget in budgets.items():
        current = metrics[name]
        if current > budget:
            over.append(
                {
                    "metric": name,
                    "current_kb": current,
                    "budget_kb": budget,
                    "over_kb": round(current - budget, 2),
                }
            )
    return {
        "dir": os.path.abspath(root),
        "ok": not over,
        "metrics": metrics,
        "budgets": {k: round(v, 2) for k, v in budgets.items()},
        "over": over,
        **measured["details"],
    }


def _display_width(text: str) -> int:
    """终端显示宽度：CJK 宽字符按 2 列计算，避免表格错位。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int, align: str = "left") -> str:
    padding = " " * max(0, width - _display_width(text))
    return text + padding if align == "left" else padding + text


def print_table(report: dict) -> None:
    metrics = report["metrics"]
    budgets = report["budgets"]
    print("前端构建产物体积预算（gzip -9，单位 KB）")
    print(f"构建目录: {report['dir']}")
    print(f"首页条目: {report.get('home_page_entry')}  |  JS 文件数: {report['js_file_count']}"
          f"  |  CSS 文件数: {report['css_file_count']}")
    print("-" * 76)
    print(_pad("指标", 36) + _pad("当前", 10, "right") + _pad("预算", 10, "right")
          + _pad("用量", 10, "right"))
    print("-" * 76)
    for name in DEFAULT_BUDGETS_KB:
        current = metrics[name]
        budget = budgets[name]
        ratio = (current / budget * 100.0) if budget else 0.0
        print(_pad(name, 36) + _pad(f"{current:.2f}", 10, "right")
              + _pad(f"{budget:.2f}", 10, "right") + _pad(f"{ratio:.1f}%", 10, "right"))
    print("-" * 76)
    print(f"最大单 chunk: {report['max_chunk_file']}")
    for key in report.get("home_layout_entries", []):
        print(f"首页 layout 链: {key}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="检查 Next.js 构建产物的 gzip 体积是否超出预算（C7 性能预算门禁）。"
    )
    parser.add_argument(
        "--dir",
        default="apps/web/.next",
        help="Next.js 构建产物目录，默认 apps/web/.next（在仓库根目录执行时无需传）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（供脚本消费）")
    parser.add_argument(
        "--budget-kb",
        type=float,
        default=None,
        help="临时把所有指标的预算统一覆盖为该值（KB），仅用于本地反向验证，CI 不要传",
    )
    args = parser.parse_args(argv)

    root = args.dir
    if not os.path.isdir(root):
        msg = f"构建目录不存在: {os.path.abspath(root)}；请先执行 `cd apps/web && npm run build`"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
        else:
            print(f"[ERROR] {msg}", file=sys.stderr)
        return 2

    budgets = dict(DEFAULT_BUDGETS_KB)
    if args.budget_kb is not None:
        budgets = {name: args.budget_kb for name in budgets}

    try:
        report = build_report(root, budgets)
    except FileNotFoundError as exc:
        msg = f"构建产物不完整: {exc}；请先执行 `cd apps/web && npm run build`"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
        else:
            print(f"[ERROR] {msg}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_table(report)
        if report["over"]:
            print("\n[FAIL] 以下指标超出预算：")
            for item in report["over"]:
                label = METRIC_LABELS.get(item["metric"], item["metric"])
                print(
                    f"  - {item['metric']}（{label}）: "
                    f"current={item['current_kb']:.2f}KB "
                    f"budget={item['budget_kb']:.2f}KB "
                    f"超出={item['over_kb']:.2f}KB"
                )
        else:
            print("\n[OK] 全部体积指标在预算内。")

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
