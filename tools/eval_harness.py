#!/usr/bin/env python3
"""AI-EOS B6 质量评测器：用规则给「AI 员工交付质量」打分。

## 它解决什么问题

功能测试只能证明「任务能跑完」，证明不了「交付内容够不够用」。
本脚本把一组真实业务任务下发到现有主链路（建任务 → 启动 → 轮询 → 取产出），
再用**可机检的规则**给产出打分，让质量可以随版本回归，而不是靠感觉。

## 边界

* 只做规则评分（关键词/结构/引用/长度/禁用表达），**不调用第二个模型做 judge**。
* 不需要真实密钥也能验证评分器本身：`--self-test` 用内置好/中/差样本自测。
* 真实评测需要已启动的 API、登录态 token、公司 id，以及公司里有绑定模型的 AI 员工。
* 报告与终端输出**绝不包含 token**。

## 用法

    # 1) 只验证评分器（本机可跑，零网络）
    python3 tools/eval_harness.py --self-test

    # 2) 看看会跑哪些任务（不调接口）
    python3 tools/eval_harness.py --dry-run

    # 3) 真实评测（需要已配置模型的服务）
    python3 tools/eval_harness.py \
        --base-url http://127.0.0.1:8000 \
        --token "$AIEOS_TOKEN" \
        --company-id "$COMPANY_ID"

默认从 `tools/eval_tasks.seed.json` 读任务，报告写到仓库根目录的
`eval-report-YYYYmmdd-HHMMSS.json`。目标：任务通过率 ≥ 80%。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TASKS = REPO_ROOT / "tools" / "eval_tasks.seed.json"
VERSION_FILE = REPO_ROOT / "apps" / "api" / "app" / "version.py"
PYPROJECT_FILE = REPO_ROOT / "apps" / "api" / "pyproject.toml"
TARGET_PASS_RATE = 0.80

SUPPORTED_RULES = {
    "must_include",
    "forbidden",
    "min_chars",
    "min_sections",
    "citation_required",
}

SECTION_RE = re.compile(r"^#{1,6}[ \t]*\S.*$", re.MULTILINE)
FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
CITATION_RE = re.compile(
    r"\[\d{1,3}(?:\s*[,，]\s*\d{1,3})*\]"   # [1] / [1,2]
    r"|来源\s*[:：]"                          # 来源：
    r"|参考(?:资料|来源)"                      # 参考资料 / 参考来源
    r"|https?://",                            # 原文链接
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# 版本
# --------------------------------------------------------------------------

def read_version() -> str:
    """优先读后端唯一版本源 version.py，退而读 pyproject。"""
    try:
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']',
                      VERSION_FILE.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    except OSError:
        pass
    try:
        m = re.search(r'^version\s*=\s*["\']([^"\']+)["\']',
                      PYPROJECT_FILE.read_text(encoding="utf-8"), re.MULTILINE)
        if m:
            return m.group(1)
    except OSError:
        pass
    return "unknown"


# --------------------------------------------------------------------------
# 任务加载与校验
# --------------------------------------------------------------------------

class SeedError(ValueError):
    pass


def _require_str(obj: dict, key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SeedError(f"{where} 缺少非空字符串字段 `{key}`")
    return value


def validate_tasks(tasks: object) -> list[dict]:
    if not isinstance(tasks, list) or not tasks:
        raise SeedError("任务文件必须是非空 JSON 数组")
    seen: set[str] = set()
    for idx, task in enumerate(tasks):
        where = f"第 {idx + 1} 条任务"
        if not isinstance(task, dict):
            raise SeedError(f"{where} 必须是对象")
        tid = _require_str(task, "id", where)
        if tid in seen:
            raise SeedError(f"任务 id 重复：{tid}")
        seen.add(tid)
        _require_str(task, "title", where)
        _require_str(task, "prompt", where)
        _require_str(task, "category", where)

        expected = task.get("expected")
        if not isinstance(expected, dict) or not expected:
            raise SeedError(f"{where}（{tid}）缺少非空 `expected` 规则对象")
        unknown = set(expected) - SUPPORTED_RULES
        if unknown:
            raise SeedError(
                f"{where}（{tid}）含不支持的规则：{sorted(unknown)}；"
                f"支持：{sorted(SUPPORTED_RULES)}")
        used = 0
        for key in ("must_include", "forbidden"):
            if key in expected:
                items = expected[key]
                if (not isinstance(items, list) or not items
                        or not all(isinstance(x, str) and x.strip() for x in items)):
                    raise SeedError(f"{where}（{tid}）的 `{key}` 必须是非空字符串数组")
                used += 1
        for key in ("min_chars", "min_sections"):
            if key in expected:
                value = expected[key]
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise SeedError(f"{where}（{tid}）的 `{key}` 必须是正整数")
                used += 1
        if "citation_required" in expected:
            if not isinstance(expected["citation_required"], bool):
                raise SeedError(f"{where}（{tid}）的 `citation_required` 必须是布尔值")
            used += 1
        if used == 0:
            raise SeedError(f"{where}（{tid}）的 `expected` 至少要有一条可执行规则")
    return tasks


def load_tasks(path: Path) -> list[dict]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SeedError(f"读取任务文件失败：{path}（{exc}）") from exc
    except json.JSONDecodeError as exc:
        raise SeedError(f"任务文件不是合法 JSON：{path}（{exc}）") from exc
    if isinstance(raw, dict):  # 兼容 {"tasks": [...]} 形式
        raw = raw.get("tasks")
    return validate_tasks(raw)


# --------------------------------------------------------------------------
# 规则评分
# --------------------------------------------------------------------------

def _norm(text: str) -> str:
    """去掉所有空白并统一大小写，避免「作为 AI」漏掉「作为AI」。"""
    return re.sub(r"\s+", "", text).casefold()


def visible_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def count_sections(text: str) -> int:
    """Markdown 标题数（先去掉代码块，避免把示例代码里的 # 算进去）。"""
    without_code = FENCE_RE.sub("", text)
    return len(SECTION_RE.findall(without_code))


def has_citation(text: str) -> bool:
    return bool(CITATION_RE.search(text))


def score_output(text: str, expected: dict) -> dict:
    """按 expected 逐条打分。

    每条规则（must_include / forbidden 的每个词各算一条）等权；
    任务得分 = 通过条数 / 总条数 × 100。
    任务通过 = 所有规则全部通过（不是看平均分）。
    """
    text = text or ""
    normalized = _norm(text)
    checks: list[dict] = []

    def add(rule: str, ok: bool, detail: str) -> None:
        checks.append({"rule": rule, "passed": bool(ok), "detail": detail})

    for term in expected.get("must_include", []):
        ok = _norm(term) in normalized
        add(f"must_include:{term}", ok,
            f"包含「{term}」" if ok else f"缺少「{term}」")

    for term in expected.get("forbidden", []):
        hit = _norm(term) in normalized
        add(f"forbidden:{term}", not hit,
            f"未出现禁用表达「{term}」" if not hit else f"出现禁用表达「{term}」")

    if "min_chars" in expected:
        need = int(expected["min_chars"])
        got = visible_chars(text)
        add("min_chars", got >= need, f"可见字符 {got}/{need}")

    if "min_sections" in expected:
        need = int(expected["min_sections"])
        got = count_sections(text)
        add("min_sections", got >= need, f"Markdown 标题 {got}/{need} 个")

    if expected.get("citation_required"):
        ok = has_citation(text)
        add("citation_required", ok,
            "检测到引用特征" if ok else "未检测到引用（[1]/来源：/参考资料/http 链接）")

    total = len(checks)
    passed = sum(1 for c in checks if c["passed"])
    score = round(passed / total * 100, 1) if total else 0.0
    failures = [c["detail"] for c in checks if not c["passed"]]
    return {
        "score": score,
        "passed": bool(checks) and passed == total,
        "checks": checks,
        "failures": failures,
    }


# --------------------------------------------------------------------------
# 自测：内置好/中/差样本，验证排序与规则确实生效
# --------------------------------------------------------------------------

SELF_TEST_EXPECTED = {
    "must_include": ["结论", "风险"],
    "forbidden": ["我不确定"],
    "min_chars": 80,
    "min_sections": 2,
    "citation_required": True,
}


def _self_test_samples() -> dict[str, str]:
    good = (
        "# 调研结论\n\n"
        "本季度中小企业对 AI 员工的需求继续上升，付费意愿集中在客服与经营分析场景。"
        "综合三家公开数据，头部厂商的差异化主要在行业模板和交付服务上[1]。\n\n"
        "## 风险与建议\n\n"
        "需要关注模型成本波动与合规要求变化，建议先用一个部门做三个月试点，"
        "再按验收结果扩大范围。以上判断来自公开资料，具体数据可复核。\n"
    )
    mid = (
        "# 结论\n\n"
        "市场在增长，但竞争也在加剧，产品需要更聚焦中小企业场景。"
        "本段内容用于验证长度规则可以通过，但没有任何出处标注。\n\n"
        "## 下一步\n\n"
        "建议继续观察一个季度，再决定是否扩大投入；"
        "先用一个部门试点，按验收结果决定推广范围，整体判断保持谨慎。\n"
    )
    bad = (
        "我不确定这个市场怎么样，可能需要再查一下。"
        "作为AI，我没有办法给出可靠判断。"
    )
    return {"good": good, "mid": mid, "bad": bad}


def run_self_test() -> int:
    samples = _self_test_samples()
    results = {name: score_output(text, SELF_TEST_EXPECTED)
               for name, text in samples.items()}
    problems: list[str] = []

    def expect(cond: bool, msg: str) -> None:
        if not cond:
            problems.append(msg)

    good, mid, bad = results["good"], results["mid"], results["bad"]
    expect(good["score"] > mid["score"] > bad["score"],
           f"分数未按好>中>差排序：{good['score']} / {mid['score']} / {bad['score']}")
    expect(good["passed"], "好样本未通过全部规则")
    expect(not mid["passed"], "中样本不应被判为通过")
    expect(bad["score"] == 0.0, f"差样本得分应为 0，实际 {bad['score']}")
    expect(any("缺少「风险」" in f for f in mid["failures"]),
           "中样本未报出缺少「风险」")
    expect(any("引用" in f for f in mid["failures"]),
           "中样本未报出缺少引用")
    expect(any("禁用表达「我不确定」" in f for f in bad["failures"]),
           "差样本未命中禁用表达")
    expect(any("Markdown 标题" in f for f in bad["failures"]),
           "差样本未报出结构不足")
    expect(count_sections(samples["good"]) >= 2, "好样本的 Markdown 标题计数不正确")
    expect(has_citation(samples["good"]) and not has_citation(samples["mid"]),
           "引用识别不符合预期")

    print("评分器自测（内置好/中/差样本）")
    for name in ("good", "mid", "bad"):
        r = results[name]
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"  [{mark}] {name:<4} 分数={r['score']:>5.1f} "
              f"规则 {sum(c['passed'] for c in r['checks'])}/{len(r['checks'])}")
        for failure in r["failures"]:
            print(f"         · {failure}")
    print("-" * 60)
    if problems:
        print("自测失败：")
        for p in problems:
            print(f"  · {p}")
        return 1
    print("自测通过：分数排序正确，关键词/禁用/长度/结构/引用规则均生效。")
    return 0


# --------------------------------------------------------------------------
# 极简 HTTP 客户端（标准库；不打印 token）
# --------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class Client:
    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip("/")
        self._token = token

    def request(self, method: str, path: str, body: dict | None = None,
                timeout: float = 60.0):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8")
                status = resp.status
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            status = exc.code
        if status >= 400:
            raise ApiError(status, text)
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def get(self, path: str, timeout: float = 60.0):
        return self.request("GET", path, timeout=timeout)

    def post(self, path: str, body: dict | None = None, timeout: float = 60.0):
        return self.request("POST", path, body=body, timeout=timeout)


def discover_agent(client: Client, company_id: str) -> str:
    """优先选已绑定模型的员工；都没有绑定就退而选第一个。"""
    rows = client.get(f"/agents?company_id={urllib.parse.quote(company_id)}") or []
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("该公司下没有 AI 员工，请先创建员工并绑定模型")
    candidates = [str(a["id"]) for a in rows if isinstance(a, dict) and a.get("id")]
    if not candidates:
        raise RuntimeError("无法从 /agents 响应中解析员工 id")
    for agent_id in candidates[:5]:
        try:
            models = client.get(f"/agents/{agent_id}/models") or []
        except ApiError:
            continue
        if isinstance(models, list) and models:
            return agent_id
    return candidates[0]


# --------------------------------------------------------------------------
# 真实评测
# --------------------------------------------------------------------------

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


def _poll_task(client: Client, task_id: str, timeout_s: float) -> dict:
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = client.get(f"/tasks/{task_id}") or {}
        if last.get("status") in TERMINAL:
            return last
        time.sleep(min(2.0, max(0.2, deadline - time.monotonic())))
    return last


def _extract_output(detail: dict) -> str:
    text = detail.get("result") or ""
    if isinstance(text, str) and text.strip():
        return text
    executions = detail.get("executions") or []
    for execution in reversed(executions):
        output = execution.get("output") or ""
        if isinstance(output, str) and output.strip():
            return output
    return ""


def run_one_task(client: Client, company_id: str, agent_id: str,
                 task: dict, timeout_s: float) -> dict:
    started = time.monotonic()
    result = {
        "id": task["id"],
        "title": task["title"],
        "category": task["category"],
        "status": "error",
        "http_task_id": None,
        "duration_ms": 0,
        "output_chars": 0,
        "score": 0.0,
        "checks": [],
        "failures": [],
        "output_preview": "",
    }
    try:
        created = client.post("/tasks", {
            "company_id": company_id,
            "title": f"[质量评测] {task['id']} {task['title']}",
            "description": task["prompt"],
            "assigned_agent_id": agent_id,
        }) or {}
        task_id = created.get("id")
        if not task_id:
            raise RuntimeError("创建任务未返回 id")
        result["http_task_id"] = task_id
        client.post(f"/tasks/{task_id}/start")
        detail = _poll_task(client, task_id, timeout_s)
        text = _extract_output(detail)
        final_status = detail.get("status")
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        result["output_chars"] = visible_chars(text)
        result["output_preview"] = text[:240]

        if not text.strip():
            result["status"] = "error"
            result["failures"] = [
                f"任务未产出内容（最终状态={final_status}）"
            ]
            return result

        scored = score_output(text, task["expected"])
        result["score"] = scored["score"]
        result["checks"] = scored["checks"]
        result["failures"] = list(scored["failures"])
        if final_status != "COMPLETED":
            result["failures"].append(f"任务最终状态={final_status}")
        result["status"] = (
            "passed" if scored["passed"] and final_status == "COMPLETED"
            else "failed"
        )
        return result
    except (ApiError, RuntimeError, OSError) as exc:
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        result["failures"] = [str(exc)[:300]]
        return result


def evaluate(args: argparse.Namespace, tasks: list[dict], token: str) -> int:
    company_id = (args.company_id or "").strip()
    if not company_id:
        print("缺少 --company-id（或环境变量 AIEOS_EVAL_COMPANY_ID）", file=sys.stderr)
        return 2
    client = Client(args.base_url, token)
    try:
        agent_id = (args.agent_id or os.environ.get("AIEOS_EVAL_AGENT_ID")
                    or discover_agent(client, company_id))
    except (ApiError, RuntimeError, OSError) as exc:
        print(f"无法确定执行任务的 AI 员工：{exc}", file=sys.stderr)
        return 2

    version = read_version()
    out_path = Path(args.out) if args.out else (
        REPO_ROOT / f"eval-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")

    print(f"AI-EOS 质量评测 v{version}")
    print(f"服务：{args.base_url.rstrip('/')}  公司：{company_id}  员工：{agent_id}")
    print(f"任务：{len(tasks)} 条  单条超时：{args.timeout:g}s")
    print("-" * 68)

    results: list[dict] = []
    for idx, task in enumerate(tasks, 1):
        print(f"[{idx}/{len(tasks)}] {task['id']} …", end="", flush=True)
        row = run_one_task(client, company_id, agent_id, task, args.timeout)
        results.append(row)
        mark = {"passed": "PASS", "failed": "FAIL", "error": "ERR "}[row["status"]]
        print(f" {mark} 得分={row['score']:.1f} 用时={row['duration_ms'] / 1000:.1f}s")
        for failure in row["failures"][:4]:
            print(f"         · {failure}")

    total = len(results)
    passed = sum(1 for r in results if r["status"] == "passed")
    errors = sum(1 for r in results if r["status"] == "error")
    average = round(statistics.fmean(r["score"] for r in results), 1) if results else 0.0
    pass_rate = round(passed / total, 4) if total else 0.0
    target_met = pass_rate >= TARGET_PASS_RATE

    report = {
        "version": version,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base_url": args.base_url.rstrip("/"),
        "company_id": company_id,
        "agent_id": agent_id,
        "tasks_file": str(Path(args.tasks).resolve()),
        "summary": {
            "total": total,
            "passed": passed,
            "failed": total - passed - errors,
            "errors": errors,
            "pass_rate": pass_rate,
            "average_score": average,
            "target_pass_rate": TARGET_PASS_RATE,
            "target_met": target_met,
        },
        "results": results,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    print("-" * 68)
    print(f"通过 {passed}/{total}（成功率 {pass_rate:.1%}），平均分 {average:.1f}")
    print(f"目标 ≥ {TARGET_PASS_RATE:.0%}：{'达成' if target_met else '未达成'}")
    print(f"报告：{out_path}")
    return 0 if target_met else 1


def run_dry(tasks: list[dict], tasks_path: Path) -> int:
    print(f"dry-run：将执行 {len(tasks)} 条任务（来源 {tasks_path}），不调用任何接口。")
    for idx, task in enumerate(tasks, 1):
        rules = []
        expected = task["expected"]
        if expected.get("must_include"):
            rules.append("must_include=" + "/".join(expected["must_include"]))
        if expected.get("forbidden"):
            rules.append("forbidden=" + "/".join(expected["forbidden"]))
        if "min_chars" in expected:
            rules.append(f"min_chars={expected['min_chars']}")
        if "min_sections" in expected:
            rules.append(f"min_sections={expected['min_sections']}")
        if expected.get("citation_required"):
            rules.append("citation_required=true")
        print(f"  {idx:>2}. [{task['category']}] {task['id']} — {task['title']}")
        print(f"      规则：{'; '.join(rules)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="AI-EOS B6 质量评测（规则评分，无 LLM judge）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000",
                        help="API 地址（默认 http://127.0.0.1:8000）")
    parser.add_argument("--token", default=os.environ.get("AIEOS_EVAL_TOKEN", ""),
                        help="登录态 access token（也可用 AIEOS_EVAL_TOKEN；不会打印）")
    parser.add_argument("--company-id", default=os.environ.get("AIEOS_EVAL_COMPANY_ID", ""),
                        help="公司 id（也可用 AIEOS_EVAL_COMPANY_ID）")
    parser.add_argument("--agent-id", default="",
                        help="指定 AI 员工 id；默认自动选一个已绑定模型的员工")
    parser.add_argument("--tasks", default=str(DEFAULT_TASKS), help="任务种子 JSON 路径")
    parser.add_argument("--out", default="",
                        help="报告路径（默认仓库根目录 eval-report-YYYYmmdd-HHMMSS.json）")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="单条任务等待上限（秒，默认 300）")
    parser.add_argument("--self-test", action="store_true",
                        help="用内置好/中/差样本自测评分器，不联网")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将要执行的任务，不调用接口")
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()

    tasks_path = Path(args.tasks).expanduser().resolve()
    try:
        tasks = load_tasks(tasks_path)
    except SeedError as exc:
        print(f"任务文件校验失败：{exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        return run_dry(tasks, tasks_path)

    token = (args.token or "").strip()
    if not token:
        print("缺少 --token（或环境变量 AIEOS_EVAL_TOKEN）。"
              "真实评测需要已登录的 API 与可用模型。", file=sys.stderr)
        return 2
    return evaluate(args, tasks, token)


if __name__ == "__main__":
    sys.exit(main())
