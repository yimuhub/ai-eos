"""macOS 无障碍树（AX）执行器：读界面结构，按元素引用操作。

## 为什么单独一层

`computer_real.py` 的感知只有 `screenshot`：Agent 拿到一张 PNG，再靠视觉模型
猜坐标点 (x, y)。分辨率一变、弹窗一挡就点错，而且每一步都要烧图片 token。
插件目录里 `agent-desktop` 写的是「直接读取界面结构……不依赖截图」，
`computer_policy.OBSERVE_ACTIONS` 也已经把 `list_windows` /
`accessibility_snapshot` / `get_window` / `find_element` 列为只读动作——
但执行器根本不存在，调用会撞上「未知能力」。本模块补的就是这一段。

全部走系统自带的 `System Events`（AppleScript），无新增依赖、无网络：
- 一次属性批量取（`get role of elems`）= 一个 Apple Event，
  所以整棵树的代价是"节点数"级别，不是"节点数 × 属性数"；
- 操作按**元素引用**（`AXPress` / `set value`）而不是键鼠模拟，
  不抢焦点、不依赖前台窗口，也不会把文字敲进终端。

信任边界：界面上的文字是**外部输入**（网页、弹窗都可能写着"点这个按钮"），
所以快照里的 title/value 只作为数据回传给模型，不当作指令执行；
写操作（点击、赋值）一律要过 `computer_policy` 的三档判定。
"""
from __future__ import annotations

import asyncio
import platform
import re
from urllib.parse import quote, unquote

# 一次快照的默认上限：节点数与层级。上限存在的意义不是省时间，
# 而是防止一棵大应用（几百个按钮的工具栏）把上下文和 Apple Event 队列打满。
# 实测代价：Finder 窗口 depth=3 / 26 个元素约 3.3s，每多一层翻几倍，
# 所以默认只到 depth=2——按钮和输入框基本都在这一层，深了反而更该先 find。
DEFAULT_MAX_NODES = 80
DEFAULT_MAX_DEPTH = 2
HARD_MAX_NODES = 300
HARD_MAX_DEPTH = 6
FIND_MAX_NODES = 120
FIND_MAX_DEPTH = 3

# 允许的点击动作：写死白名单，不接受前端/模型传任意 AX action 名。
PRESS_ACTIONS = {"AXPress", "AXPick"}

_FIELD_LIMITS = {"role": 60, "name": 160, "path": 80}
_APP_MAX_LEN = 80
_VALUE_MAX_LEN = 2000

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class AXError(ValueError):
    """参数不合法或本机环境不支持（辅助功能未授权 / 应用没有窗口）。"""


def _require_macos() -> None:
    """这套执行器只读 macOS 的无障碍树。

    非 macOS 上宁可明确报错，也不要出现"osascript 找不到"这种看不出原因的失败——
    远程设备/其它系统由 agent-desktop sidecar 形态承接（见插件库）。
    """
    if platform.system() != "Darwin":
        raise AXError(
            f"无障碍树执行器目前只支持 macOS（当前 {platform.system()}）；"
            "其它系统请在「插件库」安装 agent-desktop 后由设备执行")


def terminal_host_reason(app: str) -> str | None:
    """界面赋值等价于把这段文字交给目标应用；终端宿主上就是提交命令。"""
    # 延迟导入：computer_real 在模块级把本模块的执行器并进 REAL_EXECUTORS，
    # 顶层互相 import 会成环。
    from app.computer_real import is_terminal_app

    if is_terminal_app(app):
        return (f"硬拦截：目标应用 {app!r} 属于终端/Shell 宿主，"
                "界面赋值等价于把内容当作命令提交。请改用受审批的 run_shell。")
    return None


def _applescript_text(value: str) -> str:
    """把任意 Python 字符串安全地写成 AppleScript 字符串字面量。

    AppleScript 没有转义控制字符的机制，所以换行/制表符只能拆成 `return`、
    `tab` 常量再拼接——否则生成的脚本站不住。
    """
    pieces: list[str] = []
    buf = ""
    for ch in value:
        if ch == '"':
            buf += '\\"'
        elif ch == "\\":
            buf += "\\\\"
        elif ch == "\n":
            pieces.append(f'"{buf}" & return')
            buf = ""
        elif ch == "\t":
            pieces.append(f'"{buf}" & tab')
            buf = ""
        else:
            buf += ch
    pieces.append(f'"{buf}"')
    return " & ".join(pieces)


def _q(value: object) -> str:
    """任意标量 → AppleScript 文本表达式（数字直接用字面量）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return _applescript_text(str(value if value is not None else ""))


def _clean_app_name(raw: object) -> str:
    app = str(raw or "").strip()
    if not app:
        raise AXError("app 不能为空（进程名，如 Finder、Safari）")
    if len(app) > _APP_MAX_LEN:
        raise AXError(f"应用名过长（>{_APP_MAX_LEN} 字）")
    if _CONTROL_CHARS.search(app):
        raise AXError("应用名含控制字符，已拒绝")
    return app


def build_ref(app: str, window: int, path: list[int]) -> str:
    """元素引用 = 进程名 | 窗口序号 | 逐层 UI elements 下标（1 起）。

    用路径而不是自增 id：路径由快照生成，点击时按同一条路径重新解析，
    界面没变就还是同一个元素；应用名百分号编码，避免名字里的 `|` 拆坏字段。
    """
    return f"{quote(app, safe='')}|{window}|{'.'.join(str(i) for i in path)}"


def parse_ref(ref: object) -> tuple[str, int, list[int]]:
    text = str(ref or "").strip()
    if not text:
        raise AXError("ref 不能为空，先取 accessibility_snapshot 或 find_element")
    parts = text.split("|")
    if len(parts) != 3:
        raise AXError(f"ref 格式应为 应用|窗口|路径，收到 {ref!r}")
    app = _clean_app_name(unquote(parts[0]))
    try:
        window = int(parts[1])
    except ValueError as exc:
        raise AXError(f"ref 里的窗口序号不是整数：{parts[1]!r}") from exc
    if not 1 <= window <= 50:
        raise AXError("ref 里的窗口序号越界（1–50）")
    raw_path = parts[2].split(".") if parts[2] else []
    path: list[int] = []
    for token in raw_path:
        try:
            idx = int(token)
        except ValueError as exc:
            raise AXError(f"ref 里的层级下标不是整数：{token!r}") from exc
        if not 1 <= idx <= 300:
            raise AXError("ref 里的层级下标越界（1–300）")
        path.append(idx)
    if len(path) > HARD_MAX_DEPTH:
        raise AXError(f"ref 层级过深（>{HARD_MAX_DEPTH}）")
    if not path:
        raise AXError("ref 缺少元素层级，指向的是窗口本身")
    return app, window, path


def element_expr(app: str, window: int, path: list[int]) -> str:
    """路径 → AppleScript 元素表达式（在 System Events 的 tell 块里用）。"""
    expr = f'window {int(window)} of process {_q(app)}'
    for idx in path:
        expr = f"item {int(idx)} of (UI elements of {expr})"
    return expr


# AppleScript 里到处是 `{}`（空列表、记录），str.format 会把它们当成占位符，
# 所以这里用 __TOKEN__ 手工替换，而不是 format。
#
# 递归写法踩过两个坑，都实测过：
# 1. `set es to (UI elements of w)` 之后再 `get role of es` 会报 -1728
#    ——变量里是**已求值的引用列表**，复数 get 只对"现场拼出来的 specifier"有效。
#    所以每一层都写全 `get {role, name, position, size} of (UI elements of parentRef)`，
#    一次事件拿一整层的四类属性（这才是"不截图"能跑在几十毫秒级的原因）。
# 2. `global` 声明必须写在 handler 定义**之前**，否则 handler 里改全局变量直接
#    运行时报"变量未定义"。
#
# 元素只有一个时 AppleScript 会把复数结果摊平（`{role,name,pos,size}` 的每个分量
# 变成标量），所以统一过 asList / asPairList 归一化；某类元素（如工具栏分隔条）
# 拿不到 position 时整层复数 get 会失败，降级成只取 {role, name}，不毁掉快照。
_DUMP_TEMPLATE = """
global gLines, gCount, gMaxNodes, gMaxDepth

on walk(parentRef, prefix, depth)
    if gCount ≥ gMaxNodes then return
    set roles to {}
    set titles to {}
    set poss to {}
    set szs to {}
    set es to {}
    tell application "System Events"
        try
            set props to (get {role, name, position, size} of (UI elements of parentRef))
            set roles to my asList(item 1 of props)
            set titles to my asList(item 2 of props)
            set poss to my asPairList(item 3 of props)
            set szs to my asPairList(item 4 of props)
        on error
            try
                set props to (get {role, name} of (UI elements of parentRef))
                set roles to my asList(item 1 of props)
                set titles to my asList(item 2 of props)
            on error
                return
            end try
        end try
        try
            set es to (UI elements of parentRef)
        end try
    end tell
    set n to (count of roles)
    repeat with i from 1 to n
        if gCount ≥ gMaxNodes then return
        set gCount to gCount + 1
        set pos to my nthPair(poss, i)
        set sz to my nthPair(szs, i)
        set gLines to gLines & prefix & i & tab & my nthText(roles, i) & tab & ¬
            (item 1 of pos) & tab & (item 2 of pos) & tab & (item 1 of sz) & tab & ¬
            (item 2 of sz) & tab & my nthText(titles, i) & linefeed
        if depth < gMaxDepth and (count of es) ≥ i then
            my walk((item i of es), prefix & i & ".", depth + 1)
        end if
    end repeat
end walk

on asList(v)
    if (class of v) is list then return v
    return {v}
end asList

on asPairList(v)
    if (class of v) is not list then return {}
    if (count of v) is 0 then return {}
    if (class of (item 1 of v)) is list then return v
    return {v}
end asPairList

on nthText(theList, i)
    if (count of theList) < i then return ""
    set v to (item i of theList)
    if v is missing value then return ""
    return (v as text)
end nthText

on nthPair(theList, i)
    if (count of theList) ≥ i then
        set v to (item i of theList)
        if (count of v) ≥ 2 then return {(item 1 of v), (item 2 of v)}
    end if
    return {-1, -1}
end nthPair

set gLines to ""
set gCount to 0
set gMaxNodes to __MAX_NODES__
set gMaxDepth to __MAX_DEPTH__
tell application "System Events"
    my walk(window __WIN__ of process __APP__, "", 1)
end tell
return gLines
"""


def _render_dump(app_expr: str, window: int, max_nodes: int, max_depth: int) -> str:
    return (_DUMP_TEMPLATE
            .replace("__APP__", app_expr)
            .replace("__WIN__", str(int(window)))
            .replace("__MAX_NODES__", str(int(max_nodes)))
            .replace("__MAX_DEPTH__", str(int(max_depth))))


def _parse_dump(text: str, app: str, window: int) -> list[dict]:
    rows: list[dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        raw_path, role, x, y, w, h = (f.strip() for f in fields[:6])
        title = "\t".join(fields[6:]).strip()
        try:
            path = [int(p) for p in raw_path.split(".") if p]
        except ValueError:
            continue
        if not path:
            continue
        try:
            ix, iy, iw, ih = (int(float(v)) for v in (x, y, w, h))
        except ValueError:
            ix = iy = iw = ih = -1
        row = {
            "ref": build_ref(app, window, path),
            "path": raw_path,
            "role": role[:_FIELD_LIMITS["role"]],
            "title": title[:_FIELD_LIMITS["name"]],
        }
        if ix >= 0:
            row.update({"x": ix, "y": iy, "width": iw, "height": ih,
                        "center": [ix + iw // 2, iy + ih // 2]})
        rows.append(row)
    return rows


async def _osascript(script: str, timeout: int = 20) -> tuple[bool, str, str]:
    _require_macos()
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise AXError(f"界面读取超时（>{timeout}s）：目标应用可能无响应，已放弃") from None
    if proc.returncode == 0:
        return True, out.decode(errors="replace"), ""
    return False, "", err.decode(errors="replace").strip()


def _permission_hint(stderr: str) -> str:
    lowered = stderr.lower()
    if "-1719" in stderr or "not allowed assistive" in lowered or "不允许" in stderr:
        return ("需要辅助功能权限：系统设置 → 隐私与安全性 → 辅助功能，"
                "为运行本服务的 App 勾选后重启服务")
    if "-1728" in stderr or "can’t get" in lowered or "can't get" in lowered:
        return ("找不到目标应用或窗口：先用 list_windows 核对进程名和窗口序号"
                f"（原始报错：{stderr[:160]}）")
    return stderr


async def _dump_tree(app: str, window: int, max_nodes: int, max_depth: int) -> list[dict]:
    script = _render_dump(_q(app), window, max_nodes, max_depth)
    ok, out, err = await _osascript(script, timeout=45)
    if not ok:
        raise AXError(_permission_hint(err))
    return _parse_dump(out, app, window)


async def ax_list_windows(params: dict) -> dict:
    """列出本机可见应用与它们的窗口（名称/序号/位置/尺寸）。"""
    only_app = str((params or {}).get("app") or "").strip()
    script = """
tell application "System Events"
    set procs to (every process whose background only is false)
    set report to {}
    repeat with p in procs
        set pname to (name of p)
        set winCount to (count of windows of p)
        if winCount is 0 then
            set end of report to pname & tab & "0"
        else
            repeat with k from 1 to winCount
                set w to (window k of p)
                set wname to ""
                try
                    set wname to (name of w)
                end try
                set wsize to {-1, -1}
                try
                    set wsize to (size of w)
                end try
                set wpos to {-1, -1}
                try
                    set wpos to (position of w)
                end try
                set end of report to pname & tab & k & tab & (item 1 of wpos) & "," & ¬
                    (item 2 of wpos) & tab & (item 1 of wsize) & "," & (item 2 of wsize) & tab & wname
            end repeat
        end if
    end repeat
end tell
set AppleScript's text item delimiters to linefeed
return report as text
"""
    if only_app:
        app = _clean_app_name(only_app)
        script = script.replace("(every process whose background only is false)",
                                f'(every process whose background only is false and name is {_q(app)})')
    ok, out, err = await _osascript(script)
    if not ok:
        return {"ok": False, "error": _permission_hint(err)}
    apps: dict[str, list] = {}
    for line in out.splitlines():
        fields = line.split("\t")
        name = fields[0].strip()
        if not name:
            continue
        if len(fields) < 2 or fields[1] == "0":
            apps.setdefault(name, [])
            continue
        pos = _pair(fields[2])
        size = _pair(fields[3])
        apps.setdefault(name, []).append({
            "window": int(fields[1]),
            "title": "\t".join(fields[4:])[:_FIELD_LIMITS["name"]] if len(fields) > 4 else "",
            "position": pos, "size": size})
    return {"ok": True, "app_count": len(apps),
            "apps": [{"app": k, "windows": v} for k, v in list(apps.items())[:60]]}


def _pair(text: str) -> list[int]:
    """AppleScript 里拼出来的 "x,y" → [x, y]；拿不到就是空。"""
    out: list[int] = []
    for token in str(text or "").split(",")[:2]:
        try:
            out.append(int(float(token.strip())))
        except ValueError:
            return []
    return out


def _int_param(params: dict, key: str, default: int, *, lo: int, hi: int) -> int:
    raw = (params or {}).get(key, default)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise AXError(f"{key} 必须是整数，收到 {raw!r}") from exc
    return max(lo, min(value, hi))


async def ax_accessibility_snapshot(params: dict) -> dict:
    """把某个窗口读成元素清单（ref / role / title / 位置），不看像素。"""
    p = params or {}
    if p.get("app"):
        app = _clean_app_name(p["app"])
    else:
        ok, front, err = await _osascript(
            'tell application "System Events" to get name of first '
            'application process whose frontmost is true')
        if not ok:
            return {"ok": False, "error": _permission_hint(err)}
        app = _clean_app_name(front.splitlines()[0] if front else "")
    window = _int_param(p, "window", 1, lo=1, hi=50)
    max_nodes = _int_param(p, "max_nodes", DEFAULT_MAX_NODES,
                           lo=10, hi=HARD_MAX_NODES)
    max_depth = _int_param(p, "max_depth", DEFAULT_MAX_DEPTH, lo=1, hi=HARD_MAX_DEPTH)
    elements = await _dump_tree(app, window, max_nodes, max_depth)
    return {"ok": True, "app": app, "window": window,
            "element_count": len(elements),
            "truncated": len(elements) >= max_nodes,
            "elements": elements}


async def ax_get_window(params: dict) -> dict:
    """单个窗口的概览：标题、位置、尺寸、元素总数与顶层分区。"""
    app = _clean_app_name((params or {}).get("app"))
    window = _int_param(params or {}, "window", 1, lo=1, hi=50)
    ok, out, err = await _osascript(f"""
tell application "System Events"
    set w to window {window} of process {_q(app)}
    set t to ""
    try
        set t to (name of w)
    end try
    set pos to {{-1, -1}}
    try
        set pos to (position of w)
    end try
    set sz to {{-1, -1}}
    try
        set sz to (size of w)
    end try
    set n to (count of UI elements of w)
    set focusRole to ""
    try
        set focusRole to (role of (first UI element of w whose focused is true))
    end try
    return t & tab & (item 1 of pos) & "," & (item 2 of pos) & tab & ¬
        (item 1 of sz) & "," & (item 2 of sz) & tab & n & tab & focusRole
end tell
""")
    if not ok:
        return {"ok": False, "error": _permission_hint(err)}
    fields = (out.strip().split("\t") + ["", "", "", "", ""])[:5]
    return {"ok": True, "app": app, "window": window, "title": fields[0],
            "position": _pair(fields[1]), "size": _pair(fields[2]),
            "top_level_count": int(fields[3] or 0),
            "focused_role": fields[4]}


async def ax_find_element(params: dict) -> dict:
    """按文字/角色在界面里找元素，返回 ref 与中心点——省掉"截图→猜坐标"。"""
    p = params or {}
    query = str(p.get("query") or "").strip()
    if not query:
        raise AXError("query 不能为空（要匹配的按钮/字段文字）")
    role = str(p.get("role") or "").strip()
    snapshot = await ax_accessibility_snapshot({
        "app": p.get("app"), "window": p.get("window", 1),
        "max_nodes": _int_param(p, "max_nodes", FIND_MAX_NODES,
                                lo=10, hi=HARD_MAX_NODES),
        "max_depth": _int_param(p, "max_depth", FIND_MAX_DEPTH,
                               lo=1, hi=HARD_MAX_DEPTH)})
    if not snapshot.get("ok"):
        return snapshot
    needle = query.lower()
    matches = [e for e in snapshot["elements"]
               if needle in (e["title"] or "").lower()
               or needle in (e["role"] or "").lower()]
    if role:
        matches = [e for e in matches if role.lower() in e["role"].lower()]
    return {"ok": True, "app": snapshot["app"], "window": snapshot["window"],
            "query": query[:100], "match_count": len(matches),
            "matches": matches[:20],
            "scanned": snapshot["element_count"],
            "note": "没有匹配时可以调大 max_depth，或先用 list_windows 确认窗口序号"}


async def ax_click_element(params: dict) -> dict:
    """按元素引用点击（AXPress），不移动鼠标、不依赖坐标与截图。"""
    p = params or {}
    app, window, path = parse_ref(p.get("ref"))
    action = str(p.get("action") or "AXPress")
    if action not in PRESS_ACTIONS:
        raise AXError(f"不支持的界面动作 {action!r}，只允许 {'、'.join(sorted(PRESS_ACTIONS))}")
    expr = element_expr(app, window, path)
    ok, _, err = await _osascript(
        f'tell application "System Events" to perform action {_q(action)} of {expr}')
    if not ok:
        return {"ok": False, "app": app, "window": window,
                "error": _permission_hint(err), "hint": "界面可能已经变了，重新取一次快照再点"}
    return {"ok": True, "app": app, "window": window, "action": action,
            "performed": True, "note": "按无障碍引用执行，未移动光标、未抢焦点"}


async def ax_set_value(params: dict) -> dict:
    """把文本直接写进输入框（AXValue），不走键盘事件。

    与 `type_text` 的区别：type_text 是全局 keystroke，前台是谁就敲给谁，
    所以必须查前台应用；set_value 明确指向某个元素，
    内容不会落到终端命令行上。
    """
    p = params or {}
    app, window, path = parse_ref(p.get("ref"))
    reason = terminal_host_reason(app)
    if reason:
        return {"ok": False, "blocked": True, "app": app, "error": reason}
    text = str(p.get("value", p.get("text", "")))
    if not text:
        raise AXError("value 不能为空")
    if len(text) > _VALUE_MAX_LEN:
        raise AXError(f"文本 {len(text)} 字超过 {_VALUE_MAX_LEN} 字上限，请分段写入")
    if _CONTROL_CHARS.search(text):
        raise AXError("文本含控制字符，已拒绝")
    expr = element_expr(app, window, path)
    ok, _, err = await _osascript(
        f'tell application "System Events" to set value of {expr} to {_q(text)}')
    if not ok:
        return {"ok": False, "app": app, "window": window,
                "error": _permission_hint(err),
                "hint": "这个元素可能不接受赋值（按钮/图标类），改点它或找它的输入子元素"}
    return {"ok": True, "app": app, "window": window, "written": text[:120],
            "chars": len(text), "method": "AXValue",
            "note": "直接赋值，未模拟键盘，未改变前台应用"}


AX_EXECUTORS = {
    "list_windows": ax_list_windows,
    "accessibility_snapshot": ax_accessibility_snapshot,
    "get_window": ax_get_window,
    "find_element": ax_find_element,
    "click_element": ax_click_element,
    "set_value": ax_set_value,
}
