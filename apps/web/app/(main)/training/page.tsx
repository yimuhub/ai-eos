"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, getCompanies } from "@/lib/api";
import { notify } from "@/components/Toast";
import { confirmAction } from "@/components/ConfirmDialog";

// ## 改造说明
//
// 本页此前 3 个接口全部对不上后端，整页不可用：
//
// 1. 列表 `GET /training/sessions?company_id=&status=APPLIED`
//    后端该接口要求的是 **agent_id**（且原未声明 status 参数），→ **422**。
// 2. 学习点 `GET /training/points?session_id=` —— 后端**没有这个接口** → **404**。
// 3. 应用/拒绝 `POST /training/points/{pid}/approve` —— 同样**不存在** → **404**。
//
// 后端真实的审批粒度是**整个训练会话**（会把该会话的全部提案一起应用），
// 学习点只是展示用的提取结果。因此这里改为：
//   选 Agent → 列会话 → 看提取结果与提案 → 批准/拒绝整个会话 → 可回滚。
// 同时把"按 company 取会话"改为"按 agent 取"，与后端模型一致。

type Agent = { id: string; name: string; role?: string };

type Session = {
  id: string;
  title: string;
  type: string;
  status: string;
  summary?: string;
  created_at?: string;
};

type LearningPoint = { content: string; category?: string };
type Proposal = { id: string; change_type: string; payload?: any; status: string };
type Application = { id: string; applied_changes?: any; rolled_back: boolean };

type SessionDetail = Session & {
  raw_input?: string;
  learning_points: LearningPoint[];
  proposals: Proposal[];
  applications: Application[];
};

const TYPE_ZH: Record<string, string> = {
  CONVERSATION: "对话训练",
  DOCUMENT: "文档学习",
  EXAMPLE: "范例示范",
  FEEDBACK: "反馈纠正",
  TASK: "任务复盘",
  SKILL: "技能传授",
  MENTOR: "导师带教",
};

const STATUS_ZH: Record<string, { label: string; color: string }> = {
  PENDING: { label: "提取中", color: "var(--muted)" },
  EXTRACTED: { label: "待确认", color: "var(--warning)" },
  APPROVED: { label: "已批准", color: "var(--accent-2)" },
  REJECTED: { label: "已拒绝", color: "var(--muted)" },
  APPLIED: { label: "已生效", color: "var(--success)" },
};

const CHANGE_TYPE_ZH: Record<string, string> = {
  knowledge_update: "知识库更新",
  skill_improvement: "技能改进",
  preference_update: "偏好更新",
  capability_update: "能力更新",
};

export default function TrainingPage() {
  const [cid, setCid] = useState<string>("");
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agentId, setAgentId] = useState<string>("");
  const [sessions, setSessions] = useState<Session[]>([]);
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [trainType, setTrainType] = useState("CONVERSATION");

  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [deciding, setDeciding] = useState(false);

  // ---------- 初始化：公司 → Agent 列表 ----------
  useEffect(() => {
    (async () => {
      try {
        const cs = await getCompanies<any>();
        const c = cs?.[0]?.id;
        if (!c) {
          setLoading(false);
          return;
        }
        setCid(c);
        const list = await api.get<Agent[]>("/agents");
        setAgents(list || []);
        if (list?.length) setAgentId(list[0].id);
      } catch (e) {
        notify("error", describe(e, "加载公司/员工列表失败"));
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const loadSessions = useCallback(async (aid: string) => {
    if (!aid) {
      setSessions([]);
      return;
    }
    try {
      setSessions(await api.get<Session[]>(`/training/sessions?agent_id=${aid}`));
    } catch (e) {
      notify("error", describe(e, "加载训练会话失败"));
      setSessions([]);
    }
  }, []);

  useEffect(() => {
    setSelected(null);
    setDetail(null);
    loadSessions(agentId);
  }, [agentId, loadSessions]);

  const openSession = useCallback(async (sid: string) => {
    setSelected(sid);
    try {
      setDetail(await api.get<SessionDetail>(`/training/sessions/${sid}`));
    } catch (e) {
      setDetail(null);
      notify("error", describe(e, "加载训练详情失败"));
    }
  }, []);

  // ---------- 发起训练 ----------
  async function startNew() {
    if (!title.trim() || !content.trim() || !agentId) {
      notify("warning", "请先选择员工，并填写训练标题与内容");
      return;
    }
    setBusy(true);
    try {
      const r = await api.post<{ id: string; points: number; summary?: string }>(
        `/training/sessions?agent_id=${agentId}`,
        { training_type: trainType, title: title.trim(), raw_input: content },
      );
      notify("success", `训练完成：提取 ${r?.points ?? 0} 个学习点，待你确认`);
      setTitle("");
      setContent("");
      await loadSessions(agentId);
      if (r?.id) await openSession(r.id);
    } catch (e) {
      notify("error", describe(e, "发起训练失败"));
    } finally {
      setBusy(false);
    }
  }

  // ---------- 会话级批准 / 拒绝 ----------
  async function decide(action: "approve" | "reject") {
    if (!selected) return;
    setDeciding(true);
    try {
      const r = await api.post<{ status: string; applied_count?: number }>(
        `/training/sessions/${selected}/${action}`,
      );
      notify(
        "success",
        action === "approve"
          ? `已批准并生效${r?.applied_count != null ? `（应用 ${r.applied_count} 项变更）` : ""}`
          : "已拒绝该次训练",
      );
      await loadSessions(agentId);
      await openSession(selected);
    } catch (e) {
      notify("error", describe(e, action === "approve" ? "批准失败" : "拒绝失败"));
    } finally {
      setDeciding(false);
    }
  }

  // ---------- 回滚一次已应用的训练 ----------
  async function rollback(appId: string) {
    if (!(await confirmAction({
      title: "撤销这次训练应用？",
      description: "该员工的档案会恢复到训练前的状态，训练期间学到的内容将不再生效。",
      confirmText: "撤销",
    }))) return;
    try {
      await api.post(`/training/applications/${appId}/rollback`);
      notify("success", "已撤销该次训练应用");
      if (selected) await openSession(selected);
    } catch (e) {
      notify("error", describe(e, "撤销失败"));
    }
  }

  const pending = detail?.status === "EXTRACTED";

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up space-y-4">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">🎓 训练中心</h1>
        <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
          教给你的数字员工新知识，确认后才会真正写入它的档案与知识库。
        </p>
      </div>

      {!cid && !loading && (
        <div className="card card-static text-sm" style={{ color: "var(--muted)" }}>
          你还没有创建 AI 公司，请先到「公司管理」创建后再来训练员工。
        </div>
      )}

      {cid && (
        <div className="card card-static p-3 flex items-center gap-3 flex-wrap">
          <label className="text-sm" style={{ color: "var(--muted)" }}>训练对象</label>
          <select
            value={agentId}
            onChange={(e) => setAgentId(e.target.value)}
            className="input max-w-[260px]"
          >
            {agents.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}{a.role ? ` · ${a.role}` : ""}
              </option>
            ))}
          </select>
          {!agents.length && (
            <span className="text-xs" style={{ color: "var(--muted)" }}>
              还没有员工，请先到「Agent 团队」招募。
            </span>
          )}
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-[280px_1fr] gap-4">
        {/* ---------- 会话列表 ---------- */}
        <aside className="card card-static p-3">
          <h3 className="font-medium mb-2 text-[var(--foreground)]">训练会话</h3>
          {!sessions.length ? (
            <p className="text-xs" style={{ color: "var(--muted)" }}>
              该员工还没有训练记录。
            </p>
          ) : (
            sessions.map((s) => {
              const st = STATUS_ZH[s.status] || { label: s.status, color: "var(--muted)" };
              const active = selected === s.id;
              return (
                <button
                  key={s.id}
                  onClick={() => openSession(s.id)}
                  className="w-full text-left p-2 mb-1 rounded-lg border transition-all"
                  style={{
                    borderColor: active ? "var(--accent)" : "var(--border)",
                    background: active ? "rgba(99,102,241,0.12)" : "transparent",
                  }}
                >
                  <div className="text-sm text-[var(--foreground)]">{s.title}</div>
                  <div className="text-xs mt-1 flex items-center gap-2">
                    <span style={{ color: "var(--muted)" }}>
                      {TYPE_ZH[s.type] || s.type}
                    </span>
                    <span style={{ color: st.color }}>· {st.label}</span>
                  </div>
                </button>
              );
            })
          )}
        </aside>

        {/* ---------- 内容区 ---------- */}
        <div className="space-y-3">
          {/* 发起新训练 */}
          <div className="card card-static p-3 space-y-2">
            <h3 className="font-medium text-[var(--foreground)]">发起新训练</h3>
            <div className="flex gap-2 flex-wrap">
              <input
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="训练标题（如：抖音内容创作规范）"
                className="input flex-1 min-w-[220px]"
              />
              <select
                value={trainType}
                onChange={(e) => setTrainType(e.target.value)}
                className="input max-w-[160px]"
              >
                {Object.entries(TYPE_ZH).map(([k, v]) => (
                  <option key={k} value={k}>{v}</option>
                ))}
              </select>
            </div>
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              placeholder="要教给它的内容、规范、案例……"
              className="input h-24"
            />
            <div className="flex items-center gap-3">
              <button
                onClick={startNew}
                disabled={busy || !title.trim() || !content.trim() || !agentId}
                className="btn-primary disabled:opacity-40"
              >
                {busy ? "AI 正在提取学习点…" : "发起训练"}
              </button>
              <span className="text-xs" style={{ color: "var(--muted)" }}>
                提取结果需要你确认后才会生效
              </span>
            </div>
          </div>

          {/* 训练详情 */}
          {!selected ? (
            <div className="card card-static text-sm" style={{ color: "var(--muted)" }}>
              从左侧选择一个训练会话查看提取结果与变更提案。
            </div>
          ) : !detail ? (
            <div className="card card-static text-sm" style={{ color: "var(--muted)" }}>
              正在加载训练详情…
            </div>
          ) : (
            <>
              {/* 概要 + 决策 */}
              <div className="card card-static p-4 space-y-3">
                <div className="flex items-start justify-between gap-3 flex-wrap">
                  <div>
                    <h3 className="font-medium text-[var(--foreground)]">{detail.title}</h3>
                    <p className="text-xs mt-1" style={{ color: "var(--muted)" }}>
                      {TYPE_ZH[detail.type] || detail.type}
                      {" · "}
                      {(STATUS_ZH[detail.status] || { label: detail.status }).label}
                      {detail.created_at && ` · ${detail.created_at.slice(0, 16).replace("T", " ")}`}
                    </p>
                  </div>
                  {pending && (
                    <div className="flex gap-2">
                      <button
                        onClick={() => decide("approve")}
                        disabled={deciding}
                        className="px-3 py-1.5 rounded-lg text-xs text-white disabled:opacity-50"
                        style={{ background: "#15803d" }}
                      >
                        {deciding ? "处理中…" : "批准并应用"}
                      </button>
                      <button
                        onClick={() => decide("reject")}
                        disabled={deciding}
                        className="btn-ghost text-xs disabled:opacity-50"
                      >
                        拒绝
                      </button>
                    </div>
                  )}
                </div>

                {detail.summary && (
                  <p className="text-sm" style={{ color: "var(--foreground)" }}>
                    {detail.summary}
                  </p>
                )}

                {pending && (
                  <p className="text-xs rounded-lg px-3 py-2"
                    style={{ background: "rgba(251,191,36,0.1)", color: "var(--warning)" }}>
                    批准后会把下列 {detail.proposals.length} 项变更写入该员工的档案与知识库。
                  </p>
                )}
              </div>

              {/* 学习点 */}
              <div className="card card-static p-4">
                <h4 className="font-medium mb-2 text-[var(--foreground)]">
                  提取到的学习点（{detail.learning_points.length}）
                </h4>
                {!detail.learning_points.length ? (
                  <p className="text-xs" style={{ color: "var(--muted)" }}>
                    没有提取到学习点，可能是训练材料信息量不足。
                  </p>
                ) : (
                  <ul className="space-y-1.5">
                    {detail.learning_points.map((p, i) => (
                      <li key={i} className="text-sm flex items-start gap-2 text-[var(--foreground)]">
                        <span style={{ color: "var(--muted)" }}>•</span>
                        <span className="flex-1">{p.content}</span>
                        {p.category && (
                          <span className="badge text-xs shrink-0">{p.category}</span>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              {/* 变更提案 */}
              <div className="card card-static p-4">
                <h4 className="font-medium mb-2 text-[var(--foreground)]">
                  变更提案（{detail.proposals.length}）
                </h4>
                {!detail.proposals.length ? (
                  <p className="text-xs" style={{ color: "var(--muted)" }}>无提案。</p>
                ) : (
                  detail.proposals.map((p) => (
                    <div key={p.id} className="rounded-lg border p-2.5 mb-2 text-sm"
                      style={{ borderColor: "var(--border)" }}>
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-[var(--foreground)]">
                          {CHANGE_TYPE_ZH[p.change_type] || p.change_type}
                        </span>
                        <span className="text-xs" style={{ color: "var(--muted)" }}>{p.status}</span>
                      </div>
                      {p.payload && Object.keys(p.payload).length > 0 && (
                        <pre className="text-xs mt-1.5 rounded p-2 overflow-auto max-h-40"
                          style={{ background: "rgba(0,0,0,0.25)", color: "var(--muted)" }}>
                          {JSON.stringify(p.payload, null, 2)}
                        </pre>
                      )}
                    </div>
                  ))
                )}
              </div>

              {/* 已应用记录 + 回滚 */}
              {detail.applications.length > 0 && (
                <div className="card card-static p-4">
                  <h4 className="font-medium mb-2 text-[var(--foreground)]">应用记录</h4>
                  {detail.applications.map((a) => (
                    <div key={a.id} className="flex items-center justify-between gap-3 py-1.5 text-sm"
                      style={{ borderTop: "1px solid var(--border)" }}>
                      <span style={{ color: "var(--muted)" }}>
                        {a.rolled_back ? "已撤销" : "已生效"}
                      </span>
                      {!a.rolled_back && (
                        <button onClick={() => rollback(a.id)} className="btn-ghost text-xs">
                          撤销这次训练
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function describe(e: unknown, fallback: string): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message;
  return fallback;
}
