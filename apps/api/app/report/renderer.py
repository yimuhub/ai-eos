"""单文件 HTML 执行报告：断网双击即可打开。

约束（客户演示场景）：**零外链**——不引 CDN、不引字体、不放远程图片，
CSS 全部内联，产出文本一律 HTML 转义后放进 `<pre>`。这样把文件发到群里，
对方不用装任何东西、也不用联网就能看。

输入是纯数据结构，不碰 ORM，便于单测。
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone

_CSS = """
:root { color-scheme: light; }
body { margin: 0; background: #f6f7f9; color: #1f2328;
       font: 14px/1.6 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }
.wrap { max-width: 1080px; margin: 0 auto; padding: 28px 20px 60px; }
h1 { font-size: 22px; margin: 0 0 6px; }
h2 { font-size: 16px; margin: 30px 0 10px; padding-left: 9px;
     border-left: 3px solid #2f6feb; }
.meta { color: #57606a; font-size: 12.5px; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 9px;
         font-size: 12px; background: #eaeef2; margin-right: 6px; }
.badge.ok { background: #dafbe1; color: #116329; }
.badge.bad { background: #ffebe9; color: #a40e26; }
.badge.warn { background: #fff8c5; color: #7d4e00; }
table { width: 100%; border-collapse: collapse; background: #fff;
        border: 1px solid #d0d7de; border-radius: 6px; overflow: hidden; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eaeef2;
         vertical-align: top; }
th { background: #f6f8fa; font-weight: 600; font-size: 12.5px; color: #57606a; }
tr:last-child td { border-bottom: 0; }
pre { white-space: pre-wrap; word-break: break-word; margin: 0;
      background: #f6f8fa; border: 1px solid #eaeef2; border-radius: 6px;
      padding: 10px 12px; font-size: 12.5px; }
.step { background: #fff; border: 1px solid #d0d7de; border-radius: 8px;
        padding: 14px 16px; margin-bottom: 12px; }
.step h3 { font-size: 14.5px; margin: 0 0 6px; }
.kv { color: #57606a; font-size: 12px; margin-bottom: 8px; }
.empty { color: #8c959f; font-style: italic; }
.foot { margin-top: 36px; color: #8c959f; font-size: 12px; }
"""


def _e(v) -> str:
    return html.escape(str(v), quote=True)


def _text_of(output) -> str:
    """从步骤产出里取"给人看的那段文本"。"""
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        for key in ("output", "content", "text", "result", "human_input"):
            v = output.get(key)
            if isinstance(v, str) and v.strip():
                return v
            if isinstance(v, (dict, list)) and v:
                return json.dumps(v, ensure_ascii=False, indent=2)
        if output.get("sub_execution"):
            return f"子流程执行：{output['sub_execution']}"
        if output.get("finished"):
            return ""
    return json.dumps(output, ensure_ascii=False, indent=2)


def _fmt_ts(v) -> str:
    if not v:
        return "—"
    return str(v).replace("T", " ")[:19]


def _seconds(start, end):
    if not start or not end:
        return None
    try:
        a = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        return max(0.0, (b - a).total_seconds())
    except ValueError:
        return None


def _gate_rows(gates: list[dict]) -> str:
    if not gates:
        return '<p class="empty">本次执行没有验收闸门记录（节点未配置 gate）。</p>'
    rows = []
    for g in gates:
        failed = [c for c in (g.get("checks") or []) if not c.get("passed")]
        detail = "；".join(
            f"{c.get('rule') or c.get('name') or '规则'}：期望 {c.get('expected')}，"
            f"实际 {c.get('actual')}" for c in failed[:6]) or "—"
        state = "通过" if g.get("passed") else "未通过"
        cls = "ok" if g.get("passed") else "bad"
        if g.get("degraded"):
            state += "（评审降级）"
            cls = "warn"
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td><span class=\"badge {}\">"
            "{}</span></td><td>{}</td><td>{}</td></tr>".format(
                _e(g.get("node_name") or g.get("node_key")),
                _e(g.get("attempt", 1)), _e(g.get("gate_round", 0)),
                _e(g.get("stage", "")), cls, _e(state),
                _e(g.get("score") if g.get("score") is not None else "—"),
                _e(detail)))
    return ("<table><tr><th>节点</th><th>尝试</th><th>轮次</th><th>阶段</th>"
            "<th>结论</th><th>评分</th><th>未过项（期望 / 实际）</th></tr>"
            + "".join(rows) + "</table>")


def _autonomy_line(autonomy: dict | None) -> str:
    if not autonomy:
        return ""
    rate = autonomy.get("autonomy_rate")
    pct = "—" if rate is None else f"{rate * 100:.0f}%"
    return (
        "<h2>AI 自主率</h2>"
        "<p class=\"meta\">口径：AI 完成步骤 ÷（AI 完成步骤 + 人工节点 + 手记介入），"
        "按次数不按工作量。</p>"
        "<p><span class=\"badge\">AI 步骤 {}</span>"
        "<span class=\"badge\">人工节点 {}</span>"
        "<span class=\"badge\">手记介入 {}</span>"
        "<span class=\"badge ok\">自主率 {}</span></p>".format(
            _e(autonomy.get("ai_steps", 0)),
            _e(autonomy.get("human_steps", 0)),
            _e(autonomy.get("manual_logs", 0)), _e(pct)))


def render_execution_report(*, workflow_name: str, execution: dict,
                            steps: list[dict], gates: list[dict] | None = None,
                            autonomy: dict | None = None) -> str:
    gates = gates or []
    status = str(execution.get("status") or "")
    cls = "ok" if status == "COMPLETED" else (
        "bad" if status in ("FAILED", "CANCELLED") else "warn")

    started = min((s.get("started_at") for s in steps if s.get("started_at")),
                  default=None)
    ended = max((s.get("completed_at") for s in steps if s.get("completed_at")),
                default=None)
    attempts = sum(int(s.get("attempt") or 1) for s in steps)
    rewrites = sum(1 for s in steps if s.get("stale_at"))
    failed_gates = sum(1 for g in gates if not g.get("passed"))

    cards = []
    # 只展示每个节点的最后一版；历史 attempt 在时间线里体现
    latest = {}
    for s in steps:
        key = s.get("node_key")
        if key not in latest or int(s.get("attempt") or 1) >= int(latest[key].get("attempt") or 1):
            latest[key] = s
    for s in steps:
        key = s.get("node_key")
        if latest.get(key) is not s:
            continue
        body = _text_of(s.get("output_data"))
        note = ""
        if not body.strip() and s.get("status") == "COMPLETED":
            note = '<p class="empty">本步无文本产出（可能是编排/等待节点，或产出未落文本）。</p>'
        cards.append(
            "<div class=\"step\"><h3>{name} <span class=\"meta\">· {key}</span></h3>"
            "<div class=\"kv\">类型 {ntype} ｜ 尝试 {att} ｜ 状态 {st} ｜ 耗时 {dur}</div>"
            "{note}<pre>{body}</pre></div>".format(
                name=_e(s.get("node_name") or key), key=_e(key),
                ntype=_e(s.get("node_type")), att=_e(s.get("attempt", 1)),
                st=_e(s.get("status")),
                dur=_e("—" if _seconds(s.get("started_at"), s.get("completed_at")) is None
                       else f"{_seconds(s.get('started_at'), s.get('completed_at')):.0f}s"),
                note=note, body=_e(body) if body.strip() else ""))

    timeline_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            _e(s.get("node_name") or s.get("node_key")), _e(s.get("node_type")),
            _e(s.get("attempt", 1)), _e(s.get("status")),
            _e(_fmt_ts(s.get("started_at"))), _e(_fmt_ts(s.get("completed_at"))),
            _e("是" if s.get("stale_at") else "否"))
        for s in steps)

    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(workflow_name)} · 执行报告</title>
<style>{_CSS}</style></head>
<body><div class="wrap">
<h1>{_e(workflow_name)} · 执行报告</h1>
<div class="meta">执行 {_e(str(execution.get("id") or "")[:8])} ｜
开始 {_e(_fmt_ts(started))} ｜ 结束 {_e(_fmt_ts(ended))} ｜
生成于 {_e(now)}（本机时间）</div>
<p><span class="badge {cls}">{_e(status)}</span>
<span class="badge">步骤 {_e(len(steps))}</span>
<span class="badge">执行次数 {_e(attempts)}</span>
<span class="badge">返工作废 {_e(rewrites)}</span>
<span class="badge {"bad" if failed_gates else "ok"}">闸门未过 {_e(failed_gates)}</span></p>
{f'<p class="badge bad">失败原因：{_e(execution.get("error"))}</p>' if execution.get("error") else ""}

<h2>专家分工时间线</h2>
<table><tr><th>节点</th><th>类型</th><th>尝试</th><th>状态</th>
<th>开始</th><th>完成</th><th>已作废</th></tr>{timeline_rows}</table>

<h2>验收结果</h2>
{_gate_rows(gates)}

{_autonomy_line(autonomy)}

<h2>各步产出（最后一版）</h2>
{"".join(cards) if cards else '<p class="empty">没有步骤记录。</p>'}

<div class="foot">本报告为单文件、全内联样式，无任何外部依赖；断网双击即可打开。
数据只来自本次执行记录，未做美化性删改。</div>
</div></body></html>"""
