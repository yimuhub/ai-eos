"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "@/lib/api";
import AttachmentPicker, { type Attachment } from "@/components/AttachmentPicker";

/* ============================================================
   AI 会议中心：部门化选人 + 自定义会议类型 + 无主题闲聊
   UI 对标成长中心/复盘中心（深空玻璃卡 / Portal 弹窗 / 渐变头像）
   ============================================================ */

const AVATAR_COLORS = ["#6366f1,#8b5cf6", "#22d3ee,#3b82f6", "#f59e0b,#ef4444",
  "#10b981,#14b8a6", "#ec4899,#f43f5e", "#a78bfa,#6366f1"];

type Agent = { id: string; name: string; role?: string | null };
type DeptGroup = { dept_id: string; dept_name: string; agents: Agent[] };
type MeetingMsg = { speaker: string; speaker_type: string; content: string; phase?: string };
type Meeting = {
  id: string; title: string; status: string; topic?: string;
  meeting_type?: string;
  minutes?: string; summary?: string; context_material?: string;
  participants: { agent_id: string; name?: string; spoke_count: number }[];
  messages: MeetingMsg[];
  decisions: { id: string; content: string; status: string }[];
  action_items: { id: string; content: string; assignee?: string; status: string; task_id?: string }[];
};

const STATUS_ZH: Record<string, string> = {
  SCHEDULED: "待开始", RUNNING: "进行中", PAUSED: "已暂停",
  COMPLETED: "已结束", CANCELLED: "已取消",
};
const TYPE_ZH: Record<string, string> = {
  FREE_CHAT: "💬 无主题",
  DAILY_STANDUP: "每日站会", DEPARTMENT_MEETING: "部门会议",
  PROJECT_MEETING: "项目会议", STRATEGY_MEETING: "战略会议",
  REVIEW_MEETING: "评审会", RETROSPECTIVE: "复盘会", EMERGENCY_MEETING: "紧急会议",
};
const typeLabel = (t?: string) =>
  t ? (TYPE_ZH[t] || `🏷 ${t}`) : "项目会议";

export default function MeetingsPage() {
  const [mounted, setMounted] = useState(false);
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [depts, setDepts] = useState<DeptGroup[]>([]);
  const [projects, setProjects] = useState<{id: string; name: string}[]>([]);
  const [companyId, setCid] = useState("");
  const [detail, setDetail] = useState<Meeting | null>(null);
  const [sayText, setSayText] = useState("");
  const [meetingId, setMeetingId] = useState(""); // 当前会议 id
  // 创建表单
  const [showCreate, setShowCreate] = useState(false);
  const [title, setTitle] = useState("");
  const [topic, setTopic] = useState("");
  const [material, setMaterial] = useState("");
  const [mtype, setMtype] = useState("PROJECT_MEETING");
  const [customType, setCustomType] = useState("");
  const customRef = useRef<HTMLInputElement>(null);
  // 部门化选人：Set<agentId>
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [projectId, setProjectId] = useState("");
  const [deptOpen, setDeptOpen] = useState<Set<string>>(new Set());
  const [createAttach, setCreateAttach] = useState<Attachment[]>([]);
  const [meetingAttach, setMeetingAttach] = useState<string[]>([]);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => { setMounted(true); }, []);

  const loadMeetings = useCallback(async (cid: string) => {
    const list = await api.get(`/meetings?company_id=${cid}`);
    setMeetings(list as any);
  }, []);

  useEffect(() => {
    api.get("/companies").then(async (cs: any[]) => {
      if (!cs.length) return;
      setCid(cs[0].id);
      loadMeetings(cs[0].id);
      // 部门分组（复用成长中心的聚合接口）
      api.get(`/growth-center/departments-overview?company_id=${cs[0].id}`)
        .then((d: any) => setDepts(d.departments || [])).catch(() => {});
      api.get(`/projects?company_id=${cs[0].id}`).then((ps: any[]) => setProjects(ps)).catch(() => {});
    }).catch(() => {});
  }, [loadMeetings]);

  /* ---------- 选人逻辑（部门为单位） ---------- */
  const deptAgentIds = (g: DeptGroup) => g.agents.map((a) => a.id);
  const allAgentIds = depts.flatMap((g) => g.agents.map((a) => a.id));
  const isAllPicked = allAgentIds.length > 0 && allAgentIds.every((id) => picked.has(id));
  const toggleAll = () => {
    setPicked(isAllPicked ? new Set() : new Set(allAgentIds));
  };
  const isDeptAllPicked = (g: DeptGroup) => deptAgentIds(g).every((id) => picked.has(id));
  const toggleDept = (g: DeptGroup) => {
    setPicked((prev) => {
      const n = new Set(prev);
      if (isDeptAllPicked(g)) deptAgentIds(g).forEach((id) => n.delete(id));
      else deptAgentIds(g).forEach((id) => n.add(id));
      return n;
    });
  };
  const toggleAgent = (id: string) => {
    setPicked((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id); else n.add(id);
      return n;
    });
  };

  /* ---------- 详情弹窗 ---------- */
  const openDetail = useCallback((mid: string) => {
    setMeetingId(mid);
    const fetchIt = async () => {
      const d = await api.get(`/meetings/${mid}`);
      setDetail(d as any);
    };
    fetchIt();
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(fetchIt, 6000);
  }, []);
  const closeDetail = () => {
    setDetail(null);
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  };
  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  async function create() {
    const finalType = mtype === "__custom__" ? customType.trim() : mtype;
    if (!title || !picked.size || !finalType) {
      alert("请填写标题、选择参会人员" + (mtype === "__custom__" ? "并输入自定义类型名" : ""));
      return;
    }
    const res: any = await api.post("/meetings", {
      company_id: companyId, project_id: projectId || null, title,
      topic: mtype === "FREE_CHAT" ? "自由交流 · 无固定议题" : topic,
      context_material: material,
      meeting_type: finalType, participant_ids: Array.from(picked),
    });
    setTitle(""); setTopic(""); setMaterial(""); setPicked(new Set()); setShowCreate(false);
    await loadMeetings(companyId);
    openDetail(res.id);
  }

  async function act(path: string) {
    try {
      await api.post(`/meetings/${detail!.id}${path}`);
      if (path === "/end") {
        // 结束需要后台生成纪要，轮询等待 COMPLETED
        const waitDone = async () => {
          for (let i = 0; i < 20; i++) {
            await new Promise(r => setTimeout(r, 1500));
            const d = await api.get(`/meetings/${detail!.id}`);
            if (d.status === "COMPLETED") { setDetail(d); return; }
          }
          openDetail(detail!.id);
        };
        waitDone();
      } else {
        setTimeout(() => openDetail(detail!.id), 800);
      }
    } catch (e: any) {
      alert("❌ " + (e?.message || String(e)));
    }
  }

  async function say() {
    if (!sayText.trim()) return;
    await api.post(`/meetings/${detail!.id}/say`, { content: sayText });
    setSayText("");
    setTimeout(() => openDetail(detail!.id), 500);
  }

  async function convertItems() {
    const res: any = await api.post(`/meetings/${detail!.id}/action-items/convert`);
    alert(`已创建 ${res.converted_task_ids.length} 个任务`);
    openDetail(detail!.id);
  }

  const statusBadge = (s: string) => (
    <span className={`text-[10px] px-2 py-0.5 rounded-full shrink-0 ${
      s === "RUNNING" ? "bg-emerald-500/15 text-emerald-300 pulse-dot" :
      s === "COMPLETED" ? "bg-white/[0.06] text-[var(--muted)]" :
      s === "PAUSED" ? "bg-amber-500/15 text-amber-300" :
      "bg-indigo-500/15 text-indigo-300"}`}>
      {STATUS_ZH[s]}
    </span>
  );

  /* ---------- 详情弹窗 JSX ---------- */
  const detailModal = mounted && detail ? createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center px-4 overflow-y-auto py-8"
      style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(6px)" }}
      onClick={closeDetail}>
      <div className="w-full max-w-3xl rounded-2xl p-5 space-y-4 max-h-[90vh] overflow-y-auto animate-fade-up"
        style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid rgba(99,102,241,0.35)" }}
        onClick={(e) => e.stopPropagation()}>
        {/* 头部 */}
        <div className="flex items-center justify-between pb-3 border-b" style={{ borderColor: "var(--border)" }}>
          <div className="flex items-center gap-3">
            <div className="w-12 h-12 rounded-2xl flex items-center justify-center text-xl"
              style={{ background: "linear-gradient(135deg,#6366f1,#8b5cf6)" }}>
              🎙️
            </div>
            <div>
              <h3 className="font-semibold text-base">{detail.title}</h3>
              <p className="text-xs mt-0.5 flex items-center gap-2" style={{ color: "var(--muted)" }}>
                <span>{typeLabel(detail.meeting_type)}</span>·
                <span>{detail.topic || "无议题"}</span>
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {statusBadge(detail.status)}
            <button onClick={closeDetail} className="btn-ghost !px-2 !py-0.5 text-xs">✕</button>
          </div>
        </div>

        {/* 操作按钮 */}
        <div className="flex gap-1.5 flex-wrap">
          {detail.status === "SCHEDULED" &&
            <button onClick={() => act("/start")} className="px-3 py-1.5 rounded-lg text-xs font-medium bg-emerald-600 hover:bg-emerald-500 text-white transition-colors">▶ 开始会议</button>}
          {detail.status === "RUNNING" && <>
            <button onClick={() => act("/pause")} className="px-3 py-1.5 rounded-lg text-xs font-medium bg-amber-600 hover:bg-amber-500 text-white transition-colors">⏸ 暂停</button>
            <button onClick={() => act("/end")} className="px-3 py-1.5 rounded-lg text-xs font-medium bg-red-700 hover:bg-red-600 text-white transition-colors">■ 提前结束</button>
          </>}
          {detail.status === "PAUSED" &&
            <button onClick={() => act("/resume")} className="px-3 py-1.5 rounded-lg text-xs font-medium bg-emerald-600 hover:bg-emerald-500 text-white transition-colors">▶ 继续</button>}
          {detail.status === "COMPLETED" && detail.action_items.some((i) => i.status === "PENDING") &&
            <button onClick={convertItems} className="px-3 py-1.5 rounded-lg text-xs font-medium bg-purple-600 hover:bg-purple-500 text-white transition-colors">📌 行动项转任务</button>}
        </div>

        {/* 参会人 */}
        <div className="flex gap-1.5 flex-wrap">
          {detail.participants.map((p) => (
            <span key={p.agent_id} className="text-[11px] px-2.5 py-1 rounded-full"
              style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
              {p.name} · 发言{p.spoke_count}
            </span>
          ))}
        </div>

        {/* 实时消息流 */}
        <div className="rounded-xl p-3 space-y-2 max-h-72 overflow-y-auto"
          style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
          {detail.messages.map((m, i) => (
            <div key={i} className="text-sm leading-relaxed">
              <span className={`mr-1.5 font-semibold ${m.speaker_type === "user" ? "text-emerald-300" : "text-indigo-300"}`}>
                {m.speaker}:
              </span>
              <span style={{ color: "var(--foreground)" }}>{m.content}</span>
            </div>
          ))}
          {!detail.messages.length && (
            <p className="text-xs py-4 text-center" style={{ color: "var(--muted)" }}>
              尚未发言。点击「▶ 开始会议」启动。
            </p>
          )}
        </div>

        {/* 会议附件 */}
        {detail.status === "RUNNING" && (
          <div className="rounded-xl p-3 space-y-2"
            style={{ background: "rgba(99,102,241,0.05)", border: "1px solid rgba(99,102,241,0.2)" }}>
            <div className="flex items-center gap-2 flex-wrap">
              <label className="btn-ghost text-xs cursor-pointer">📎 上传会议附件</label>
              <input type="file" multiple accept=".pdf,.docx,.txt,.md,.csv,.json,.xlsx,.pptx"
                className="hidden"
                onChange={async (e) => {
                  const f = e.target.files?.[0];
                  if (!f || !meetingId) return;
                  const fd = new FormData(); fd.append("file", f);
                  try {
                    const resp = await fetch(
                      `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/meetings/${meetingId}/upload`,
                      { method: "POST",
                        headers: { Authorization: `Bearer ${localStorage.getItem("aieos_token")}` },
                        body: fd });
                    const d = await resp.json();
                    if (d.ok) { alert(`✅ 已上传 ${f.name}`); openDetail(meetingId); }
                  } catch {}
                  e.target.value = "";
                }} />
              <span className="text-[11px]" style={{ color: "var(--muted)" }}>支持 pdf/docx/txt/md → 自动追加会议材料</span>
            </div>
          </div>
        )}

        {/* 老板插话 */}
        {["RUNNING", "PAUSED"].includes(detail.status) && (
          <div className="flex gap-2">
            <input value={sayText} onChange={(e) => setSayText(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && say()}
              placeholder="作为老板插话…（Agent 下一轮会看到）"
              className="input flex-1 !py-2 text-sm" />
            <button onClick={say} className="btn-primary text-xs px-4">发送</button>
          </div>
        )}

        {/* 决策 */}
        {detail.decisions.length > 0 && (
          <div className="rounded-xl p-3" style={{ background: "rgba(16,185,129,0.06)", border: "1px solid rgba(16,185,129,0.25)" }}>
            <p className="text-xs font-semibold mb-1.5 text-emerald-300">✅ 决策</p>
            {detail.decisions.map((d) => (
              <p key={d.id} className="text-sm leading-relaxed">· {d.content}</p>
            ))}
          </div>
        )}

        {/* 行动项 */}
        {detail.action_items.length > 0 && (
          <div className="rounded-xl p-3" style={{ background: "rgba(168,85,247,0.06)", border: "1px solid rgba(168,85,247,0.25)" }}>
            <p className="text-xs font-semibold mb-1.5 text-purple-300">📌 行动项</p>
            {detail.action_items.map((a) => (
              <p key={a.id} className="text-sm leading-relaxed">
                · {a.content}
                <span className="ml-1.5 text-[11px]" style={{ color: "var(--muted)" }}>
                  → {a.assignee || "未指派"} [{a.status === "CONVERTED" ? "已转任务" : a.status === "DISMISSED" ? "已忽略" : "待转任务"}]
                </span>
              </p>
            ))}
          </div>
        )}

        {/* 会议材料 */}
        {detail.minutes || detail.context_material && (
          <details className="rounded-xl p-3" style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
            <summary className="text-xs cursor-pointer font-semibold text-indigo-300">📄 会议材料 & 纪要</summary>
            {detail.context_material && (
              <pre className="text-xs whitespace-pre-wrap mt-2 max-h-40 overflow-y-auto"
                style={{ color: "var(--foreground)" }}>{detail.context_material}</pre>
            )}
            {detail.minutes && <pre className="text-xs whitespace-pre-wrap mt-2 max-h-60 overflow-y-auto"
              style={{ color: "var(--foreground)" }}>{detail.minutes}</pre>}
          </details>
        )}
      </div>
    </div>, document.body
  ) : null;

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      {/* 页头 */}
      <div className="relative overflow-hidden rounded-2xl mb-5 p-6"
        style={{ background: "linear-gradient(135deg, rgba(99,102,241,0.12) 0%, rgba(139,92,246,0.08) 100%)", border: "1px solid rgba(99,102,241,0.25)" }}>
        <div className="flex items-center gap-4">
          <div className="w-12 h-12 rounded-full flex items-center justify-center text-2xl"
            style={{ background: "linear-gradient(135deg,#6366f1,#8b5cf6)" }}>🎙️</div>
          <div>
            <h1 className="text-xl font-bold text-[var(--foreground)]">AI 会议中心</h1>
            <p className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>
              部门会议 · 无主题畅聊 · 决策与行动项
            </p>
          </div>
          <button onClick={() => setShowCreate(!showCreate)}
            className="ml-auto btn-primary text-sm px-5 py-2.5">
            {showCreate ? "✕ 收起创建面板" : "＋ 发起会议"}
          </button>
        </div>
      </div>

      {/* 创建面板 */}
      {showCreate && (
        <div className="card card-static p-5 space-y-3 mb-6 animate-fade-up">
          <div className="flex gap-2 flex-wrap">
            <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="会议标题"
              className="input w-56" />
            <select value={mtype} onChange={(e) => setMtype(e.target.value)}
              className="input w-44">
              {Object.entries(TYPE_ZH).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              <option value="__custom__">🏷 自定义类型…</option>
            </select>
            {mtype === "__custom__" && (
              <input ref={customRef} value={customType} onChange={(e) => setCustomType(e.target.value)}
                placeholder="输入自定义类型名，如：产品共创会"
                className="input flex-1 min-w-[200px]" />
            )}
            {mtype !== "FREE_CHAT" && mtype !== "__custom__" && (
              <input value={topic} onChange={(e) => setTopic(e.target.value)} placeholder="议题"
                className="input flex-1 min-w-[200px]" />
            )}
          </div>
          {mtype === "FREE_CHAT" && (
            <p className="text-xs text-indigo-300">💬 无主题模式：不设议题，与会者自由交流（替代原协作消息）。</p>
          )}
          <select value={projectId} onChange={(e) => setProjectId(e.target.value)}
            className="input w-48">
            <option value="">不关联项目（公司级会议）</option>
            {projects.map((pr) => <option key={pr.id} value={pr.id}>{pr.name}</option>)}
          </select>
          <textarea value={material} onChange={(e) => setMaterial(e.target.value)}
            placeholder="会议材料 / 背景（可选，也可用下方附件上传）"
            className="input h-16 resize-none" />
          <div className="flex items-center gap-2 flex-wrap">
            <AttachmentPicker value={createAttach} onChange={setCreateAttach} label="添加会议附件" />
          </div>

          {/* 部门化选人 */}
          <div>
            <p className="text-xs font-semibold mb-2 flex items-center gap-2" style={{ color: "var(--muted)" }}>
              👥 选择与会人员（按部门 · 已选 {picked.size} 人）
              {picked.size > 0 && (
                <button onClick={() => setPicked(new Set())}
                  className="text-red-300 hover:bg-red-500/10 px-2 py-0.5 rounded-md text-[11px]">清空</button>
              )}
            </p>
            <div className="space-y-1.5 max-h-72 overflow-y-auto pr-1">
              {/* 全体会议：一键全选 */}
              <div className="rounded-xl overflow-hidden shrink-0"
                style={{ background: isAllPicked ? "rgba(245,158,11,0.12)" : "rgba(255,255,255,0.03)", border: "1px solid rgba(245,158,11,0.35)" }}>
                <button onClick={toggleAll}
                  className="w-full flex items-center gap-3 px-3 py-2.5 text-left hover:bg-white/[0.04] transition-colors">
                  <span className={`w-4 h-4 rounded flex items-center justify-center text-[10px] shrink-0 transition-colors ${isAllPicked ? "bg-amber-500" : "border border-white/30"}`}>
                    {isAllPicked && "✓"}
                  </span>
                  <span className="w-8 h-8 rounded-lg flex items-center justify-center text-base shrink-0"
                    style={{ background: "linear-gradient(135deg,#f59e0b,#ef4444)" }}>🌍</span>
                  <span className="flex-1 min-w-0">
                    <span className="text-sm font-semibold block">全体会议</span>
                    <span className="text-[11px]" style={{ color: "var(--muted)" }}>
                      {isAllPicked ? `全员参与 · ${allAgentIds.length} 人` : `一键选择所有人员（${allAgentIds.length} 人）`}
                    </span>
                  </span>
                  {isAllPicked && (
                    <span className="text-[10px] px-2 py-0.5 rounded-full bg-amber-500/20 text-amber-300 shrink-0">全体参会</span>
                  )}
                </button>
              </div>
              {depts.map((g) => {
                const all = isDeptAllPicked(g);
                const some = !all && g.agents.some((a) => picked.has(a.id));
                const open = deptOpen.has(g.dept_id);
                const grad = AVATAR_COLORS[Math.abs(
                  g.dept_name.split("").reduce((a, c) => a + c.charCodeAt(0), 0)) % AVATAR_COLORS.length];
                return (
                  <div key={g.dept_id} className="rounded-xl overflow-hidden"
                    style={{ background: "var(--surface-2)", border: all ? "1px solid rgba(99,102,241,0.45)" : "1px solid var(--border)" }}>
                    {/* 部门行：整部门选择 */}
                    <div className="flex items-center gap-2 px-3 py-2">
                      <button onClick={() => toggleDept(g)}
                        className={`w-4 h-4 rounded border flex items-center justify-center text-[9px] shrink-0 transition-all ${
                          all ? "bg-indigo-500 border-indigo-500 text-white"
                            : some ? "border-indigo-400 text-indigo-300" : "border-gray-600"}`}
                        title={all ? "取消整部门" : "选择整部门"}>
                        {all ? "✓" : some ? "−" : ""}
                      </button>
                      <span className="w-6 h-6 rounded-lg flex items-center justify-center text-xs shrink-0"
                        style={{ background: `linear-gradient(135deg,${grad})` }}>🏢</span>
                      <button onClick={() => setDeptOpen((prev) => {
                        const n = new Set(prev);
                        if (n.has(g.dept_id)) n.delete(g.dept_id); else n.add(g.dept_id);
                        return n;
                      })}
                        className="flex-1 text-left text-sm font-medium flex items-center gap-2">
                        {g.dept_name}
                        <span className="text-[10px]" style={{ color: "var(--muted)" }}>{g.agents.length} 人</span>
                        <span className={`text-indigo-300 text-[10px] transition-transform inline-block ${open ? "rotate-90" : ""}`}>▸</span>
                      </button>
                      <span className="text-[10px]" style={{ color: all ? "#818cf8" : "var(--muted)" }}>
                        {all ? "全员参会" : some ? "部分参会" : ""}
                      </span>
                    </div>
                    {/* 成员行 */}
                    {open && (
                      <div className="px-3 pb-2 flex flex-wrap gap-1.5 animate-fade-up">
                        {g.agents.map((a) => {
                          const on = picked.has(a.id);
                          return (
                            <button key={a.id} onClick={() => toggleAgent(a.id)}
                              className={`px-2.5 py-1 rounded-lg text-[11px] border transition-all ${
                                on ? "border-indigo-500/50 bg-indigo-500/15 text-indigo-200"
                                  : "hover:bg-white/[0.04]"}`}
                              style={on ? {} : { borderColor: "rgba(99,102,241,0.2)", color: "var(--muted)" }}>
                              {on ? "✓ " : ""}{a.name}
                            </button>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              })}
              {!depts.length && <p className="text-xs py-3 text-center" style={{ color: "var(--muted)" }}>暂无部门数据</p>}
            </div>
          </div>

          <button onClick={create} disabled={!title || !picked.size}
            className="btn-primary disabled:opacity-40">
            🎙️ 创建会议（{picked.size} 名与会者）
          </button>
        </div>
      )}

      {/* 会议列表 */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        {meetings.map((m, i) => {
          const cg = AVATAR_COLORS[i % AVATAR_COLORS.length];
          const running = m.status === "RUNNING";
          return (
            <button key={m.id} onClick={() => openDetail(m.id)}
              className="card group relative overflow-hidden cursor-pointer text-left">
              <div className="absolute top-0 left-0 w-full h-[2px] opacity-60 group-hover:opacity-100 transition-opacity"
                style={{ background: `linear-gradient(90deg, ${cg.split(",")[0]}, transparent)` }} />
              <div className="flex items-start gap-3">
                <div className="w-10 h-10 rounded-xl flex items-center justify-center text-lg shrink-0"
                  style={{ background: `linear-gradient(135deg, ${cg})` }}>
                  {m.meeting_type === "FREE_CHAT" ? "💬" : "🎙️"}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="font-medium text-sm truncate">{m.title}</div>
                  <div className="text-xs mt-0.5 truncate" style={{ color: "var(--muted)" }}>
                    {typeLabel(m.meeting_type as string)} · {m.topic || "无议题"}
                  </div>
                </div>
                {statusBadge(m.status)}
              </div>
              <div className="mt-2.5 flex items-center gap-2 text-[11px]" style={{ color: "var(--muted)" }}>
                <span>👥 {m.participants?.length ?? 0} 人</span>
                <span>💬 {m.messages?.length ?? 0} 条发言</span>
                {(m.decisions?.length ?? 0) > 0 && <span className="text-emerald-300">✅ {m.decisions.length} 决策</span>}
                {(m.action_items?.length ?? 0) > 0 && <span className="text-purple-300">📌 {m.action_items.length} 行动项</span>}
              </div>
            </button>
          );
        })}
      </div>
      {!meetings.length && (
        <div className="card card-static text-center py-16 text-sm" style={{ color: "var(--muted)" }}>
          还没有会议 · 点击右上角「＋ 发起会议」开始
        </div>
      )}

      {detail && detailModal}
    </div>
  );
}
