"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { notify } from "@/components/Toast";
import { confirmAction } from "@/components/ConfirmDialog";

/* ============================================================
   员工离职中心：多选离职员工 → 选择继承人 → 资产继承 → 执行
   ============================================================ */

type Agent = {
  id: string; name: string; role: string | null; status: string;
  department_id: string | null;
  memory_config?: { capabilities?: string[]; backend?: string } | null;
};
type Dept = { id: string; name: string };
type Detail = {
  agent_id: string; name: string; role: string | null;
  skills: number; assets: number; memories: number; open_tasks: number;
};

const AVATAR_COLORS = ["#6366f1,#8b5cf6", "#22d3ee,#3b82f6", "#f59e0b,#ef4444", "#10b981,#14b8a6", "#ec4899,#f43f5e"];

export default function OffboardingPage() {
  const router = useRouter();
  const [agents, setAgents] = useState<Agent[]>([]);
  const [depts, setDepts] = useState<Dept[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [deptFilter, setDeptFilter] = useState("");
  const [search, setSearch] = useState("");

  // 继承设置
  const [successorId, setSuccessorId] = useState("");
  const [transferSkills, setTransferSkills] = useState(true);
  const [transferAssets, setTransferAssets] = useState(true);
  const [transferMemories, setTransferMemories] = useState(true);
  const [reassignTasks, setReassignTasks] = useState(true);

  // 预览 / 执行
  const [preview, setPreview] = useState<{ successor: any; details: Detail[] } | null>(null);
  const [executing, setExecuting] = useState(false);
  const [result, setResult] = useState<{ offboarded: any[]; offboarded_count: number; failed: any[] } | null>(null);

  const load = useCallback(async () => {
    const cs: any[] = await api.get("/companies");
    if (!cs.length) return;
    const cid = cs[0].id;
    setDepts(await api.get(`/companies/${cid}/departments`));
    setAgents(await api.get(`/agents?company_id=${cid}`));
  }, []);

  useEffect(() => { load(); }, [load]);

  const filtered = useMemo(() => {
    return agents.filter((a) => {
      if (deptFilter && a.department_id !== deptFilter) return false;
      if (search && !(a.name.includes(search) || (a.role || "").includes(search))) return false;
      return true;
    });
  }, [agents, deptFilter, search]);

  function toggle(id: string) {
    setSelected((prev) => {
      const n = new Set(prev); if (n.has(id)) n.delete(id); else n.add(id); return n;
    });
  }

  async function doPreview() {
    if (!selected.size || !successorId) return;
    try {
      const r = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/agents/offboarding/preview`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("aieos_token")}`,
        },
        body: JSON.stringify({
          agent_ids: Array.from(selected),
          successor_id: successorId,
          transfer_skills: transferSkills,
          transfer_assets: transferAssets,
          transfer_memories: transferMemories,
          reassign_open_tasks: reassignTasks,
        }),
      });
      setPreview(await r.json());
    } catch (e: any) { notify("error", `交接预演失败：${e.message || String(e)}`); }
  }

  async function doExecute() {
    if (!preview) return;
    // 这一步会真实改写记忆与资产的归属，且不可一键撤销。
    // 原实现点一下就执行、没有任何确认——属于"看起来有流程、其实没有护栏"。
    const ok = await confirmAction({
      title: `确认执行 ${selected.size} 名员工的离职交接？`,
      description: "交接会真实改写数据归属，无法一键撤销，请先确认预演结果无误。",
      bullets: [
        transferSkills ? "技能将转交给接手人" : "技能不转移（将随员工失效）",
        transferMemories ? "记忆将转交给接手人" : "记忆不转移",
        transferAssets ? "资产将转交给接手人" : "资产不转移",
        reassignTasks ? "未完成任务将改派给接手人" : "未完成任务不改派",
      ],
      confirmText: "执行交接",
    });
    if (!ok) return;
    setExecuting(true);
    try {
      const r = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/agents/offboarding/execute`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("aieos_token")}`,
        },
        body: JSON.stringify({
          agent_ids: Array.from(selected),
          successor_id: successorId,
          transfer_skills: transferSkills,
          transfer_assets: transferAssets,
          transfer_memories: transferMemories,
          reassign_open_tasks: reassignTasks,
        }),
      });
      const data = await r.json();
      setResult(data);
      setPreview(null);
      await load();
    } catch (e: any) { notify("error", `交接预演失败：${e.message || String(e)}`); }
    setExecuting(false);
  }

  const successor = agents.find((a) => a.id === successorId);
  const totalSkills = preview?.details.reduce((s, d) => s + d.skills, 0) ?? 0;
  const totalAssets = preview?.details.reduce((s, d) => s + d.assets, 0) ?? 0;
  const totalMems = preview?.details.reduce((s, d) => s + d.memories, 0) ?? 0;
  const totalTasks = preview?.details.reduce((s, d) => s + d.open_tasks, 0) ?? 0;

  /* ---------- 执行完成结果页 ---------- */
  if (result) {
    return (
      <div className="p-6 max-w-2xl mx-auto animate-fade-up">
        <div className="card text-center py-10">
          <div className="text-5xl mb-4">✅</div>
          <h1 className="text-xl font-bold mb-2">离职手续已完成</h1>
          <p className="text-sm mb-6" style={{ color: "var(--muted)" }}>
            {result.offboarded_count} 名员工已办理离职，资产已移交给「{agents.find((a) => a.id === successorId)?.name || successorId}」
          </p>
          {result.offboarded.length > 0 && (
            <div className="flex flex-wrap gap-2 justify-center mb-4">
              {result.offboarded.map((o) => (
                <span key={o.id} className="text-xs px-3 py-1.5 rounded-lg"
                  style={{ background: "var(--surface-2)" }}>
                  {o.name}
                </span>
              ))}
            </div>
          )}
          {result.failed.length > 0 && (
            <div className="card card-static mt-4 text-left">
              <p className="text-xs text-red-300 mb-2">以下员工处理失败：</p>
              {result.failed.map((f) => (
                <p key={f.id} className="text-xs" style={{ color: "var(--muted)" }}>{f.reason}</p>
              ))}
            </div>
          )}
          <div className="flex gap-2 justify-center mt-6">
            <button onClick={() => router.push("/agents")} className="btn-primary">返回数字员工</button>
            <button onClick={() => { setResult(null); setSelected(new Set()); }} className="btn-ghost">继续办理离职</button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="p-6 max-w-[1200px] mx-auto animate-fade-up">
      {/* 页头 */}
      <div className="mb-5">
        <button onClick={() => router.push("/agents")} className="btn-ghost text-xs mb-3">← 返回数字员工</button>
        <h1 className="text-2xl font-bold tracking-tight">员工离职中心</h1>
        <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
          选择离职员工 → 指定继承人 → 技能、资料、记忆、任务完整交接
        </p>
      </div>

      {/* 步骤指示 */}
      <div className="flex items-center gap-2 mb-6 text-xs">
        {[
          { n: 1, label: "选择离职员工", active: true },
          { n: 2, label: "指定继承人与交接项", active: !!selected.size },
          { n: 3, label: "确认执行", active: !!preview },
        ].map((s, i) => (
          <div key={s.n} className="flex items-center gap-2">
            {i > 0 && <div className="w-8 h-px" style={{ background: "var(--border)" }} />}
            <div className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full ${s.active ? "" : "opacity-40"}`}
              style={{ background: s.active ? "rgba(99,102,241,0.15)" : "var(--surface-2)", color: s.active ? "#a5b4fc" : "var(--muted)" }}>
              <span className="w-4 h-4 rounded-full flex items-center justify-center text-[9px] font-bold"
                style={{ background: s.active ? "#6366f1" : "var(--border)", color: "#fff" }}>{s.n}</span>
              {s.label}
            </div>
          </div>
        ))}
      </div>

      {/* 步骤1：选择离职员工 */}
      <div className="card mb-4">
        <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
          <h2 className="text-sm font-semibold">① 选择要办理离职的员工（已选 <b className="text-indigo-300">{selected.size}</b> 人）</h2>
          <div className="flex gap-2">
            <input placeholder="搜索姓名/岗位…" value={search} onChange={(e) => setSearch(e.target.value)}
              className="input w-44 !py-1.5 text-xs" />
            <select value={deptFilter} onChange={(e) => setDeptFilter(e.target.value)} className="input w-auto !py-1.5 text-xs">
              <option value="">全部部门</option>
              {depts.map((d) => (<option key={d.id} value={d.id}>{d.name}</option>))}
            </select>
          </div>
        </div>
        <div className="grid grid-cols-4 gap-2 max-h-[320px] overflow-y-auto p-1">
          {filtered.map((a, i) => {
            const on = selected.has(a.id);
            const grad = AVATAR_COLORS[i % AVATAR_COLORS.length];
            return (
              <button key={a.id} type="button" onClick={() => toggle(a.id)}
                className={`flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-left transition-all border ${
                  on ? "border-red-500/60 bg-red-500/10" : "hover:bg-white/[0.04]"}`}
                style={on ? {} : { borderColor: "var(--border)" }}>
                <div className="w-8 h-8 rounded-lg flex items-center justify-center text-white text-sm font-bold shrink-0"
                  style={{ background: `linear-gradient(135deg, ${grad})` }}>
                  {a.name.slice(0, 1)}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="text-xs font-medium truncate">{a.name}</div>
                  <div className="text-[10px] truncate" style={{ color: "var(--muted)" }}>{a.role || "未设岗位"}</div>
                </div>
                {on && <span className="text-red-400 shrink-0">✓</span>}
              </button>
            );
          })}
        </div>
        {!filtered.length && (
          <p className="text-xs text-center py-6" style={{ color: "var(--muted)" }}>无匹配员工</p>
        )}
      </div>

      {/* 步骤2：继承设置 */}
      {selected.size > 0 && (
        <div className="card mb-4 space-y-3 animate-fade-up">
          <h2 className="text-sm font-semibold">② 指定继承人与交接内容</h2>

          {/* 继承人选择 */}
          <div className="flex items-center gap-3 flex-wrap">
            <span className="text-xs shrink-0" style={{ color: "var(--muted)" }}>所有资产移交给：</span>
            <select value={successorId} onChange={(e) => { setSuccessorId(e.target.value); setPreview(null); }}
              className="input w-72 !py-2">
              <option value="">— 请选择继承员工 —</option>
              {agents.filter((a) => !selected.has(a.id)).map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}{a.role ? ` · ${a.role}` : ""}
                </option>
              ))}
            </select>
            {successor && (
              <span className="inline-flex items-center gap-2 text-xs px-3 py-1.5 rounded-lg bg-emerald-500/10 border border-emerald-500/30 text-emerald-300">
                ↳ 继承人：{successor.name}
              </span>
            )}
          </div>

          {/* 交接选项 */}
          <div className="grid grid-cols-2 gap-2">
            {([
              ["🧠 记忆传承", "离职员工的长期记忆复制给继承人（标注来源），业务经验不丢失", transferMemories, setTransferMemories],
              ["📚 技能传承", "继承人不具备的技能自动补齐", transferSkills, setTransferSkills],
              ["📄 成果移交", "经手的全部文档/成果所有权转移给继承人", transferAssets, setTransferAssets],
              ["📋 任务改派", "未完成的任务改派给继承人继续跟进", reassignTasks, setReassignTasks],
            ] as const).map(([label, desc, val, setter]) => (
              <button key={label} type="button"
                onClick={() => { setter(!val); setPreview(null); }}
                className={`text-left px-3.5 py-3 rounded-xl transition-all border ${
                  val ? "border-indigo-500/50 bg-indigo-500/10" : "hover:bg-white/[0.04]"}`}
                style={val ? {} : { borderColor: "var(--border)", opacity: 0.65 }}>
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">{label}</span>
                  <span className={`w-8 h-4.5 rounded-full relative transition-colors ${val ? "bg-indigo-500" : "bg-white/[0.2]"}`}
                    style={{ height: 18, width: 32 }}>
                    <span className="absolute top-0.5 w-3.5 h-3.5 rounded-full bg-white transition-all"
                      style={{ left: val ? 16 : 2 }} />
                  </span>
                </div>
                <p className="text-[11px] mt-1 opacity-70" style={{ color: "var(--muted)" }}>{desc}</p>
              </button>
            ))}
          </div>

          <div className="flex items-center gap-3 pt-1">
            <button onClick={doPreview} disabled={!successorId}
              className="btn-primary disabled:opacity-40">
              🔍 预览交接清单
            </button>
            {!successorId && selected.size > 0 && (
              <p className="text-xs text-amber-300">请先选择继承员工</p>
            )}
          </div>
        </div>
      )}

      {/* 步骤3：预览确认 */}
      {preview && (
        <div className="card mb-4 animate-fade-up" style={{ borderColor: "rgba(99,102,241,0.4)" }}>
          <h2 className="text-sm font-semibold mb-3">③ 交接清单确认</h2>

          {/* 汇总 */}
          <div className="grid grid-cols-5 gap-2 mb-4">
            {[
              ["离职员工", `${preview.details.length} 人`],
              ["传承技能", `${totalSkills} 项`],
              ["移交成果", `${totalAssets} 份`],
              ["传承记忆", `${totalMems} 条`],
              ["改派任务", `${totalTasks} 个`],
            ].map(([k, v]) => (
              <div key={k} className="px-3 py-2.5 rounded-xl text-center" style={{ background: "var(--surface-2)" }}>
                <div className="text-lg font-bold text-indigo-300">{v}</div>
                <div className="text-[10px] mt-0.5" style={{ color: "var(--muted)" }}>{k}</div>
              </div>
            ))}
          </div>

          {/* 明细表 */}
          <div className="rounded-xl overflow-hidden border" style={{ borderColor: "var(--border)" }}>
            <table className="w-full text-xs">
              <thead>
                <tr style={{ background: "var(--surface-2)" }}>
                  {["离职员工", "岗位", "技能", "成果", "记忆", "未完成任务"].map((h) => (
                    <th key={h} className="text-left px-3 py-2 font-medium" style={{ color: "var(--muted)" }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {preview.details.map((d) => (
                  <tr key={d.agent_id} className="border-t" style={{ borderColor: "var(--border)" }}>
                    <td className="px-3 py-2 font-medium">{d.name}</td>
                    <td className="px-3 py-2" style={{ color: "var(--muted)" }}>{d.role || "-"}</td>
                    <td className="px-3 py-2">{transferSkills ? `${d.skills} → ${successor?.name}` : "不移交"}</td>
                    <td className="px-3 py-2">{transferAssets ? `${d.assets} → ${successor?.name}` : "不移交"}</td>
                    <td className="px-3 py-2">{transferMemories ? `${d.memories} → 复制` : "不移交"}</td>
                    <td className="px-3 py-2">{reassignTasks ? `${d.open_tasks} → 改派` : "保留原状"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="flex items-center gap-3 mt-4">
            <button onClick={doExecute} disabled={executing}
              className="disabled:opacity-40 px-5 py-2 rounded-lg text-sm font-medium text-white transition-all"
              style={{
                background: "linear-gradient(135deg,#ef4444,#f97316)",
                boxShadow: "0 2px 12px -2px rgba(239,68,68,0.5)",
              }}>
              {executing ? "办理中…" : `确认离职并删除 ${preview.details.length} 名员工`}
            </button>
            <button onClick={() => setPreview(null)} className="btn-ghost">返回调整</button>
            <p className="text-[11px]" style={{ color: "var(--muted)" }}>
              ⚠️ 删除不可恢复 · 继承关系将记入审计日志
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
