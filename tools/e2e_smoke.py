#!/usr/bin/env python3
"""AI-EOS 端到端主链路试跑（离线、可重复、零真实密钥）。

## 为什么需要它

此前所有验证都是"零件级"的：单元测试、类型检查、接口契约。
它们能证明"每个零件没问题"，但证明不了"整条链路能跑通"——
而用户真正关心的是后者：我建了公司、配了模型、招了员工、下了目标，
到底能不能拿到结果？

本脚本用**真实进程 + 真实 HTTP + 真实数据库**把主链路跑一遍：

    注册 → 登录 → 建公司 → 配模型 → 建 Agent → 绑定模型
         → 建项目 → 下达任务 → 执行 → 产出成果 → 文档落盘 → 计费落库

模型侧使用本地假服务（OpenAI 兼容协议），因此**不需要任何真实密钥**，
也可以在 CI 里反复跑。假模型返回固定内容（内含文档生成标记），
所以"成果是否入库""文档是否真的落盘"都是可断言的硬事实。

## 安全约束

* 默认强制使用**临时 SQLite 数据库**与**临时存储目录**，
  绝不触碰 `apps/api/.env` 里配置的真实数据库（用户数据）。
* 显式设置 `DATABASE_URL` 时改用该库（CI 的 Postgres job 就是这么跑的）。
  这是**明确的选择**：调用方必须给一个可丢弃的库，脚本不替你判断。
  为防手滑，指向仓库内真实 SQLite 文件（`apps/api/data/*.db`）的 URL
  会被直接拒绝。
* 不读取、不打印任何真实密钥（外部库 URL 打印前会抹掉口令）。

## 用法

    python3 tools/e2e_smoke.py                # 跑一遍，临时目录自动清理
    python3 tools/e2e_smoke.py --keep         # 保留临时目录，便于排查
    python3 tools/e2e_smoke.py --verbose      # 同时把 API 服务端日志打到终端

    # 用外部数据库（Postgres）：必须是可丢弃的库
    DATABASE_URL=postgresql+asyncpg://u:p@127.0.0.1:5432/aieos_e2e \
        python3 tools/e2e_smoke.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
API_DIR = REPO_ROOT / "apps" / "api"
VENV_PY = API_DIR / ".venv" / "bin" / "python"

DOC_TITLE = "E2E 试跑报告"
MOCK_KEY_ENV = "E2E_MOCK_MODEL_KEY"
STARTUP_TIMEOUT = 90.0
TASK_TIMEOUT = 120.0
# 外部库（CI 的 Postgres service 刚起来时可能还没接受连接）：
# 有限重试，绝不死等——30 次 × 0.5 秒 = 最多 15 秒。
DB_WAIT_ATTEMPTS = 30
DB_WAIT_INTERVAL = 0.5

# 匹配 scheme://user:password@ ，只用于把口令替换成 ***
_CREDENTIAL_RE = re.compile(r"([a-zA-Z0-9+.\-]+://)([^:/@\s]+):([^@/\s]+)@")

# --------------------------------------------------------------------------
# 假模型服务（OpenAI 兼容）
# --------------------------------------------------------------------------

_calls: list[dict] = []
_calls_lock = threading.Lock()


def _mock_content() -> str:
    """固定回复。内含文档标记，用来验证"标记 → 文件落盘"这条链路。"""
    spec = {
        "type": "md",
        "title": DOC_TITLE,
        "content": "# 试跑结论\n\n- 主链路连通\n- 本文件由端到端试跑的假模型输出渲染而成\n",
    }
    return (
        "## 任务已完成\n\n"
        "以下内容由端到端试跑使用的**本地假模型服务**返回，用于验证三件事：\n\n"
        "1. 模型网关是否真的把请求发到了数据库里配置的 `base_url`；\n"
        "2. Agent 运行时是否把返回内容写回任务结果与成果中心；\n"
        "3. 输出中的文档标记是否真的被渲染成文件并登记为资产。\n\n"
        "### 结论\n"
        "- 主链路（建公司 → 配模型 → 招人 → 下目标 → 执行 → 产出）已跑通。\n\n"
        "[GENERATE_DOC]\n"
        + json.dumps(spec, ensure_ascii=False)
        + "\n[/GENERATE_DOC]\n"
    )


class _MockLLMHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # 有些客户端会先探 /models
        self._send_json({"object": "list", "data": [
            {"id": "e2e-mock-model", "object": "model", "owned_by": "e2e"},
        ]})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            payload = {}

        with _calls_lock:
            _calls.append({
                "path": self.path,
                "model": payload.get("model"),
                "messages": len(payload.get("messages") or []),
                "auth": bool(self.headers.get("Authorization")),
            })

        content = _mock_content()
        self._send_json({
            "id": "chatcmpl-e2e",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model") or "e2e-mock-model",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 256, "completion_tokens": 128,
                      "total_tokens": 384},
        })

    def log_message(self, *_args):  # 静音，避免污染报告
        pass


def start_mock_llm() -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockLLMHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


# --------------------------------------------------------------------------
# 极简 HTTP 客户端（只用标准库，避免对 venv 的依赖）
# --------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body


class Client:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.token: str | None = None

    def raw(self, method: str, path: str, body=None, auth: bool = True,
            timeout: float = 60.0):
        url = self.base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if auth and self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8")
                status = resp.status
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            status = e.code
        if status >= 400:
            raise ApiError(status, text)
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def get(self, path, **kw):
        return self.raw("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.raw("POST", path, body=body, **kw)


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------

class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, bool(ok), detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f"  — {detail}" if detail else ""), flush=True)
        return bool(ok)

    def section(self, title: str) -> None:
        print(f"\n▶ {title}", flush=True)

    @property
    def failed(self) -> list[tuple[str, bool, str]]:
        return [r for r in self.rows if not r[1]]

    def summary(self) -> bool:
        total = len(self.rows)
        bad = len(self.failed)
        print("\n" + "=" * 68)
        if bad:
            print(f"端到端试跑未通过：{total - bad}/{total} 项通过，{bad} 项失败")
            for name, _ok, detail in self.failed:
                print(f"  · {name}  {detail}")
            print("=" * 68)
            return False
        print(f"端到端试跑通过：{total}/{total} 项全部通过")
        print("=" * 68)
        return True


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for_api(client: Client, proc: subprocess.Popen, log_path: Path,
                 timeout: float = STARTUP_TIMEOUT) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            data = client.get("/health", auth=False, timeout=3)
            if isinstance(data, dict) and data.get("status") == "ok":
                return True
        except Exception:
            time.sleep(0.4)
    return False


def tail(path: Path, lines: int = 40) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(无日志)"
    return "\n".join(content[-lines:]) if content else "(日志为空)"


# --------------------------------------------------------------------------
# 数据库目标：默认临时 SQLite；有 DATABASE_URL 时用它（CI 的 PG job）
# --------------------------------------------------------------------------

def redact_url(url: str) -> str:
    """打印用：抹掉 URL 里的口令，避免把凭据写进 CI 日志。"""
    return _CREDENTIAL_RE.sub(r"\1\2:***@", url)


def normalize_async_url(url: str) -> str:
    """把 postgres://、postgresql:// 统一成 asyncpg 驱动形式。"""
    if url.startswith("postgresql+asyncpg://") or url.startswith("sqlite+"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://"):]
    return url


def resolve_database_url(tmp: Path) -> tuple[str, bool, str]:
    """返回 (给被测服务的 DATABASE_URL, 是否外部库, 人类可读说明)。

    * 没有 `DATABASE_URL` → 临时 SQLite 文件（原行为，绝不碰真实库）。
    * 有 `DATABASE_URL`   → 用它；但指向仓库内真实 SQLite 文件时直接拒绝。
    """
    real_db = (API_DIR / "data" / "aieos.db").resolve()

    raw = (os.environ.get("DATABASE_URL") or "").strip()
    if not raw:
        db_path = tmp / "e2e.db"
        return f"sqlite+aiosqlite:///{db_path}", False, f"临时 SQLite：{db_path}"

    url = normalize_async_url(raw)
    if url.startswith("sqlite"):
        # 外部 SQLite 只允许放在临时目录：落到仓库 data/ 下一律拒绝
        path_part = url.split(":///", 1)[-1].split("?", 1)[0]
        target = Path(path_part).expanduser().resolve() if path_part else None
        if target == real_db or (target and (API_DIR / "data") in target.parents):
            raise SystemExit(
                f"拒绝使用 DATABASE_URL={redact_url(url)}："
                f"它会写入真实数据库（{real_db}）。"
                "要用外部库请指向可丢弃的临时库，PG 用 "
                "postgresql+asyncpg://...")

    return url, True, f"外部数据库：{redact_url(url)}"


_DB_PROBE = r"""
import asyncio
import sys

import asyncpg

dsn, attempts, interval = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])


async def main() -> int:
    last = None
    for i in range(attempts):
        try:
            conn = await asyncpg.connect(dsn, timeout=3)
            try:
                await conn.execute("SELECT 1")
            finally:
                await conn.close()
            return 0
        except Exception as exc:  # noqa: BLE001 — 未就绪的各种原因都重试
            last = exc
            await asyncio.sleep(interval)
    print(f"数据库未就绪（{type(last).__name__}: {last}）", file=sys.stderr)
    return 1


sys.exit(asyncio.run(main()))
"""


def wait_for_database(url: str) -> tuple[bool, str]:
    """外部库就绪探测：有限重试，绝不死等。

    用 venv 里的 asyncpg 真连一次（只 SELECT 1，不建表、不写数据），
    这样 CI 的 Postgres service "端口已开但还在启动" 的阶段也能等到。
    """
    if url.startswith("sqlite"):
        return True, "SQLite 无需等待"
    dsn = url.replace("+asyncpg", "") if url.startswith("postgresql") else url
    proc = subprocess.run(
        [str(VENV_PY), "-c", _DB_PROBE, dsn,
         str(DB_WAIT_ATTEMPTS), str(DB_WAIT_INTERVAL)],
        capture_output=True, text=True, timeout=DB_WAIT_ATTEMPTS * DB_WAIT_INTERVAL + 30,
    )
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return proc.returncode == 0, (detail[-1] if detail else "")


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def run(keep: bool, verbose: bool) -> int:
    if not VENV_PY.is_file():
        print(f"找不到后端虚拟环境：{VENV_PY}\n"
              f"请先在 apps/api 下创建 venv 并安装依赖。", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="aieos_e2e_"))
    storage = tmp / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    api_log = tmp / "api.log"

    # 默认临时 SQLite；CI 的 PG job 通过 DATABASE_URL 换成外部库
    database_url, external_db, db_label = resolve_database_url(tmp)

    # 外部库可能刚被容器拉起：有限重试等它就绪（最多 15 秒），绝不死等
    if external_db:
        db_ready, db_detail = wait_for_database(database_url)
        if not db_ready:
            print(f"外部数据库在 {DB_WAIT_ATTEMPTS} 次重试内未就绪：{db_label}"
                  + (f"\n{db_detail}" if db_detail else ""), file=sys.stderr)
            if not keep:
                shutil.rmtree(tmp, ignore_errors=True)
            return 2

    mock_server, mock_port = start_mock_llm()
    api_port = free_port()
    base = f"http://127.0.0.1:{api_port}"

    env = dict(os.environ)
    env.update({
        # —— 隔离：绝不碰真实库/真实存储 ——
        "DATABASE_URL": database_url,
        "STORAGE_DIR": str(storage),
        "JWT_SECRET": secrets.token_hex(32),
        "AIEOS_ENV": "development",
        "DEBUG": "true",
        "ENABLE_FULL_FUNCTION_TEST_PAGE": "false",
        # 假模型密钥（只存在于本进程，用完即弃）
        MOCK_KEY_ENV: "e2e-mock-key-not-a-real-secret",
        "PYTHONUNBUFFERED": "1",
    })

    log_handle = open(api_log, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(VENV_PY), "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(api_port),
         "--log-level", "info"],
        cwd=str(API_DIR), env=env,
        stdout=log_handle, stderr=subprocess.STDOUT,
    )

    if verbose:
        threading.Thread(target=_tee_log, args=(api_log,), daemon=True).start()

    report = Report()
    exit_code = 1
    client = Client(base)
    ctx: dict = {}

    print("AI-EOS 端到端主链路试跑")
    print(f"临时目录：{tmp}")
    print(f"数据库：{db_label}")
    print(f"假模型服务：http://127.0.0.1:{mock_port}/v1   API 服务：{base}")

    try:
        report.section("服务启动")
        ok = wait_for_api(client, proc, api_log)
        if not report.check("API 服务在超时内就绪（/health）", ok,
                            "" if ok else f"进程退出码={proc.poll()}"):
            print("\n--- 服务端日志（尾部）---\n" + tail(api_log))
            return 1

        suffix = secrets.token_hex(3)
        email = f"e2e_{suffix}@example.com"
        username = f"e2e_{suffix}"
        password = "E2ePassw0rd"

        report.section("账号")
        created = client.post("/auth/register", {
            "email": email, "username": username, "password": password,
            "display_name": "试跑用户",
        }, auth=False)
        report.check("注册新账号", bool(created and created.get("id")))

        login = client.post("/auth/login",
                            {"identity": email, "password": password}, auth=False)
        client.token = (login or {}).get("access_token")
        report.check("登录并取得访问令牌", bool(client.token))

        me = client.get("/auth/me")
        report.check("令牌可用于受保护接口（/auth/me）",
                     (me or {}).get("username") == username)

        report.section("公司")
        company = client.post("/companies", {"name": "端到端试跑公司",
                                             "description": "由 e2e_smoke.py 创建"})
        company_id = (company or {}).get("id")
        report.check("创建 AI 公司", bool(company_id))
        ctx["company_id"] = company_id

        report.section("模型配置")
        model = client.post("/models", {
            "name": "试跑假模型",
            "provider_type": "openai_compat",
            "base_url": f"http://127.0.0.1:{mock_port}/v1",
            "api_key_env": MOCK_KEY_ENV,
            "model_id": "e2e-mock-model",
        })
        model_id = (model or {}).get("id")
        report.check("登记模型配置（指向本地假服务）", bool(model_id))

        diag = client.get("/models/diagnostics") or {}
        report.check("模型自检判定「可用」", bool(diag.get("ready")),
                     f"ready={diag.get('ready')}；"
                     f"提示={diag.get('hint') or '无'}")
        report.check("自检认出了本模型（口径与运行时一致）",
                     model_id in (diag.get("usable_model_ids") or []),
                     f"可用模型 {len(diag.get('usable_model_ids') or [])} 个")
        report.check("逐 provider 明细不与总体判定矛盾",
                     _diag_consistent(diag),
                     _diag_detail(diag.get("providers")))
        report.check("自检不泄露密钥内容（只返回布尔）",
                     _no_secret_leak(diag),
                     "响应中未出现密钥明文")

        report.section("组织与人员")
        agent = client.post("/agents", {
            "company_id": company_id, "name": "试跑分析员",
            "role": "战略分析师",
            "capabilities": ["persistent_memory"],
        })
        agent_id = (agent or {}).get("id")
        report.check("创建 AI 员工", bool(agent_id))

        client.post(f"/agents/{agent_id}/models",
                    {"role": "primary", "model_id": model_id})
        bound = client.get(f"/agents/{agent_id}/models") or []
        report.check("模型已绑定到该员工且可回读",
                     any(m.get("model_id") == model_id for m in bound),
                     f"{len(bound)} 条绑定")

        report.section("项目与目标")
        project = client.post("/projects", {
            "company_id": company_id,
            "name": "端到端试跑项目",
            "description": "验证主链路是否贯通",
        })
        project_id = (project or {}).get("id")
        report.check("创建项目（company_id 走请求体）", bool(project_id))

        legacy = client.post(f"/projects?company_id={company_id}", {
            "name": "端到端试跑项目（旧式查询参数）",
        })
        report.check("创建项目（company_id 走查询参数，向后兼容）",
                     bool((legacy or {}).get("id")))

        task = client.post("/tasks", {
            "company_id": company_id,
            "title": f"出具一份{DOC_TITLE}",
            "description": "请输出本期经营分析结论，并生成一份 Markdown 报告。",
            "assigned_agent_id": agent_id,
        })
        task_id = (task or {}).get("id")
        report.check("下达任务并指派给该员工", bool(task_id))

        started = client.post(f"/tasks/{task_id}/start")
        report.check("启动任务执行", bool(started))

        report.section("执行与产出")
        final = _poll_task(client, task_id, TASK_TIMEOUT)
        status = (final or {}).get("status")
        report.check("任务在超时内进入 COMPLETED",
                     status == "COMPLETED", f"最终状态={status}")

        result_text = (final or {}).get("result") or ""
        report.check("任务结果已写回（非空）",
                     len(result_text) > 30,
                     f"{len(result_text)} 字符")

        execution_ok, exec_detail = _check_executions(client, task_id)
        report.check("执行记录与检查点已落库", execution_ok, exec_detail)

        assets = client.get(f"/assets?company_id={company_id}") or []
        report.check("成果已进入资产中心", len(assets) >= 2,
                     f"{len(assets)} 个资产")

        doc_assets = [a for a in assets if str(a.get("name", "")).endswith(".md")]
        doc_files = [a for a in doc_assets if Path(a.get("path", "")).is_file()
                     and Path(a["path"]).stat().st_size > 0]
        report.check("文档标记被渲染为真实文件并落盘",
                     bool(doc_files),
                     "; ".join(f"{Path(a['path']).name}"
                               f"({Path(a['path']).stat().st_size}B)"
                               for a in doc_files[:3]) or "未找到渲染出的文件")

        events = client.get("/events?event_type=doc.generated&limit=50") or []
        matched = [e for e in events if (e.get("payload") or {}).get("task_id") == task_id
                   or e.get("entity_id") == task_id]
        report.check("文档生成事件已留痕（可观测）", bool(matched),
                     f"事件 {len(matched)} 条")

        report.section("计费与可观测性")
        stats = client.get(f"/tokens/stats/summary?company_id={company_id}") or {}
        report.check("Token 用量已计入流水",
                     int(stats.get("total_tokens") or 0) > 0,
                     f"{stats.get('total_tokens')} tokens / "
                     f"{stats.get('calls')} 次调用")
        report.check("模型调用延迟已记录",
                     int(stats.get("avg_latency_ms") or 0) >= 0,
                     f"平均 {stats.get('avg_latency_ms')} ms")

        with _calls_lock:
            llm_calls = list(_calls)
        report.check("假模型服务确实收到了请求（链路真的走通）",
                     len(llm_calls) > 0,
                     f"{len(llm_calls)} 次请求，均带 Authorization="
                     f"{all(c['auth'] for c in llm_calls)}")

        logs = client.get(f"/tasks/{task_id}/logs") or {}
        report.check("任务日志可查（事件流）",
                     int(logs.get("count") or 0) >= 0,
                     f"{logs.get('count')} 条")

        report.section("质量度量")
        metrics = client.get(f"/metrics/overview?company_id={company_id}") or {}
        m_tasks = metrics.get("tasks") or {}
        report.check("度量：成功任务被计入且成功率为 100%",
                     m_tasks.get("success_rate") == 1.0,
                     f"完成={m_tasks.get('completed')} 失败={m_tasks.get('failed')} "
                     f"成功率={m_tasks.get('success_rate')}")
        m_calls = metrics.get("model_calls") or {}
        report.check("度量：模型调用成功率与延迟",
                     m_calls.get("success_rate") == 1.0
                     and m_calls.get("avg_latency_ms") is not None,
                     f"{m_calls.get('total')} 次调用 / "
                     f"成功率={m_calls.get('success_rate')} / "
                     f"平均 {m_calls.get('avg_latency_ms')} ms")
        m_exec = metrics.get("executions") or {}
        report.check("度量：执行耗时被测出（依赖执行记录起止时间）",
                     m_exec.get("avg_duration_seconds") is not None,
                     f"平均 {m_exec.get('avg_duration_seconds')} 秒")

        report.section("失败路径（负面验证）")
        bad_agent = client.post("/agents", {
            "company_id": company_id, "name": "未配模型的员工"})
        bad_agent_id = (bad_agent or {}).get("id")
        bad_task = client.post("/tasks", {
            "company_id": company_id, "title": "注定失败的任务",
            "assigned_agent_id": bad_agent_id})
        bad_task_id = (bad_task or {}).get("id")
        report.check("可以为一个未配模型的员工下达任务", bool(bad_task_id))

        client.post(f"/tasks/{bad_task_id}/start")
        bad_final = _poll_task(client, bad_task_id, 60)
        report.check("无模型任务在超时内判定为 FAILED",
                     (bad_final or {}).get("status") == "FAILED",
                     f"状态={(bad_final or {}).get('status')}")
        report.check("失败任务没有留下'半成品'结果",
                     not (bad_final or {}).get("result"),
                     "result 为空（符合预期）")

        bad_detail = client.get(f"/tasks/{bad_task_id}") or {}
        bad_execs = [e for e in (bad_detail.get("executions") or [])
                     if e.get("status") == "failed"]
        bad_error = (bad_execs[0].get("error") if bad_execs else "") or ""
        report.check("失败原因已落库（用户点开就能看到为什么）",
                     bool(bad_error), (bad_error or "无")[:70])
        report.check("失败原因可操作（点明缺失的是模型配置）",
                     "模型" in bad_error, (bad_error or "")[:70])

        bad_logs = client.get(f"/tasks/{bad_task_id}/logs") or {}
        report.check("任务时间线包含执行记录（而非只有事件流）",
                     any(l.get("kind") == "execution"
                         for l in (bad_logs.get("logs") or [])),
                     f"{bad_logs.get('count')} 条日志")

        metrics2 = client.get(f"/metrics/overview?company_id={company_id}") or {}
        report.check("度量如实反映一半成功一半失败（成功率 0.5）",
                     (metrics2.get("tasks") or {}).get("success_rate") == 0.5,
                     f"成功率={(metrics2.get('tasks') or {}).get('success_rate')}")
        report.check("度量指出失败原因与责任员工",
                     bool(metrics2.get("failure_reasons"))
                     and bool(metrics2.get("failure_by_agent")),
                     (metrics2.get("failure_reasons") or [{}])[0].get("reason", "无")[:60])

        report.section("租户隔离（负面验证）")
        other = Client(base)
        other.post("/auth/register", {
            "email": f"other_{suffix}@example.com",
            "username": f"other_{suffix}", "password": password,
        }, auth=False)
        other_login = other.post("/auth/login", {
            "identity": f"other_{suffix}@example.com", "password": password,
        }, auth=False)
        other.token = (other_login or {}).get("access_token")
        denied = False
        try:
            other.get(f"/tasks/{task_id}")
        except ApiError as e:
            denied = e.status == 404
        report.check("另一个账号读取不到本账号的任务（404 而非 403 泄露）",
                     denied)

        exit_code = 0 if report.summary() else 1
        return exit_code

    except ApiError as e:
        print(f"\n接口调用失败：{e}", file=sys.stderr)
        print("\n--- 服务端日志（尾部）---\n" + tail(api_log))
        return 1
    except Exception as e:  # noqa: BLE001 — 顶层兜底，必须让人看见
        import traceback
        traceback.print_exc()
        print(f"\n试跑异常终止：{e}", file=sys.stderr)
        print("\n--- 服务端日志（尾部）---\n" + tail(api_log))
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_handle.close()
        mock_server.shutdown()
        mock_server.server_close()
        if keep:
            print(f"\n临时目录已保留：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def _tee_log(path: Path, interval: float = 0.5) -> None:
    seen = 0
    while True:
        time.sleep(interval)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines[seen:]:
            print(f"    [api] {line}", flush=True)
        seen = len(lines)


def _poll_task(client: Client, task_id: str, timeout: float) -> dict | None:
    deadline = time.time() + timeout
    last: dict | None = None
    while time.time() < deadline:
        try:
            last = client.get(f"/tasks/{task_id}")
        except ApiError:
            return last
        status = (last or {}).get("status")
        if status in ("COMPLETED", "FAILED", "CANCELLED"):
            return last
        time.sleep(1.0)
    return last


def _check_executions(client: Client, task_id: str) -> tuple[bool, str]:
    try:
        rows = client.get(f"/tasks/{task_id}/executions") or []
    except ApiError as e:
        return False, f"读取执行记录失败：{e}"
    if not rows:
        return False, "没有执行记录"
    ok = any(r.get("status") == "completed" for r in rows)
    detail = ", ".join(f"{r.get('status')}" for r in rows[:3])
    return ok, f"{len(rows)} 条执行记录（{detail}）"


def _diag_detail(providers) -> str:
    if not providers:
        return "自检未返回 provider 信息"
    parts = [f"{p.get('provider_type')}={'就绪' if p.get('key_present') else '缺密钥'}"
             f"({p.get('models_ready', '?')}/{p.get('model_count', '?')})"
             for p in providers[:3]]
    return " / ".join(parts)


def _diag_consistent(diag) -> bool:
    """总体判定与逐 provider 明细必须自洽。

    这里守护的正是本次试跑抓到的那类缺陷：总体说"可用"，
    明细却报"缺密钥"——用户会以为配置错了，实际能跑。
    """
    if not isinstance(diag, dict):
        return False
    if not diag.get("ready"):
        return True
    return any(p.get("key_present") for p in (diag.get("providers") or []))


def _no_secret_leak(diag) -> bool:
    """自检响应里不应出现任何看起来像密钥的长随机串。"""
    text = json.dumps(diag, ensure_ascii=False, default=str)
    marker = "e2e-mock-key-not-a-real-secret"
    return marker not in text


def main() -> int:
    parser = argparse.ArgumentParser(description="AI-EOS 端到端主链路试跑")
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    parser.add_argument("--verbose", action="store_true", help="打印 API 服务端日志")
    args = parser.parse_args()
    return run(keep=args.keep, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
