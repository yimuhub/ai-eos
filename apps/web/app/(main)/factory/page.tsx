"use client";
import { useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { api, describeError } from "@/lib/api";
import { invalidate, useActiveCompanyId, useResource } from "@/lib/useResource";
import AttachmentPicker, { Attachment } from "@/components/AttachmentPicker";

type Blueprint = {
      name?: string; role?: string; responsibilities?: string[];
      required_skills?: string[]; knowledge_scope?: string[]; system_prompt?: string;
    };
    type TrainingInfo = { agent_id: string; training: string[]; blueprint: Blueprint };
    type FactoryJob = { id: string; goal: string; status: string; agent_id?: string;
                        created_at?: string; blueprint?: Blueprint };

export default function FactoryPage() {
  // 激活公司可订阅：切公司后工厂任务自动跟着换
  const cid = useActiveCompanyId();
  const { data: jobData, error: loadError, loading, refresh: refreshJobs } = useResource<FactoryJob[]>(
    cid ? `factory:jobs:${cid}` : null,
    () => api.get<FactoryJob[]>(`/factory/jobs?company_id=${cid}`),
  );
  const jobs = useMemo<FactoryJob[]>(() => jobData ?? [], [jobData]);
  const [goal, setGoal] = useState("");
  const [blueprintAttachments, setBlueprintAttachments] = useState<Attachment[]>([]); // 📎 蓝图参考附件
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [confirmJob, setConfirmJob] = useState<string | null>(null);   // 批准前确认
  const [reviewOpen, setReviewOpen] = useState<string | null>(null);   // 验收面板 job id
  const [reviewData, setReviewData] = useState<any>(null);
  const [reviewNotes, setReviewNotes] = useState("");
  const [testAnswers, setTestAnswers] = useState<Record<number, string>>({});
  const [asking, setAsking] = useState<number | null>(null);

  async function createBlueprint() {
    if (!goal || !cid) return;
    setBusy(true);
    setMsg("⏳ AI 正在设计组织蓝图（约需 30~90 秒），请勿关闭页面…");
    try {
      const d = await api.post(`/factory/blueprint?company_id=${cid}`, {
        goal,
        attachments: blueprintAttachments.length ? blueprintAttachments : undefined, // 📎
      });
      setMsg("✅ 蓝图已生成，请查看详情后批准创建");
      refreshJobs();
      setGoal("");
      setBlueprintAttachments([]);
      void d;
    } catch (e: any) {
      setMsg(e.message || String(e));
    }
    setBusy(false);
  }

  async function approve(jobId: string) {
    try {
      await api.post(`/factory/jobs/${jobId}/approve`);
      setMsg("✅ 已批准，开始创建");
      invalidate("agents:"); // 批准会建出员工，员工名录缓存需失效
      refreshJobs();
    } catch (e: any) {
      setMsg(e.message || String(e));
    }
  }

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      <div className="mb-4">
          <h1 className="text-2xl font-bold tracking-tight">Agent 工厂</h1>
          <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>能力缺口 → 蓝图生成 → 新员工上岗</p>
        </div>
      {/* 生成蓝图 */}
      <div className="border border-[var(--border)] rounded p-4 mb-6 card space-y-2">
        <h3 className="font-medium text-[var(--foreground)]">输入目标，AI 生成蓝图</h3>
        <textarea value={goal} onChange={(e) => setGoal(e.target.value)} placeholder="例如：创建一位擅长抖音短视频内容创作的 Agent"
          className="input" />
        <AttachmentPicker value={blueprintAttachments} onChange={setBlueprintAttachments}
          label="📎 参考材料（岗位 JD / 业务规范 / 参考资料）" />
        <button onClick={createBlueprint} disabled={busy || !goal}
          className="btn-primary disabled:opacity-40">{busy ? "设计中…" : "生成蓝图"}</button>
      </div>
      {msg && <p className="text-sm text-blue-300 mb-4">{msg}</p>}
      {/* 任务列表 */}
      <div className="space-y-2">
        {jobs.map((j) => {
          const bp = j.blueprint || {};
          const steps = [
            { k: "BLUEPRINT", label: "蓝图生成" },
            { k: "TESTING", label: "岗前培训/测试" },
            { k: "ACTIVE", label: "正式上岗" },
          ];
          const stepIdx = steps.findIndex((x) => x.k === j.status);
          return (
          <div key={j.id} className="border border-[var(--border)] rounded-xl p-4 card space-y-3">
            <div className="flex justify-between items-start gap-3">
              <div className="min-w-0">
                <div className="text-sm font-semibold text-[var(--foreground)]">{bp.name || j.goal.slice(0, 30)}</div>
                <div className="text-xs text-[var(--muted)] mt-0.5">{j.goal}</div>
              </div>
              {j.status === "BLUEPRINT" && (
                <button onClick={() => setConfirmJob(j.id)}
                  className="px-3 py-1.5 bg-green-700 hover:bg-green-600 text-white rounded-lg text-xs shrink-0">审查蓝图并批准</button>
              )}
              {j.status === "TESTING" && (
                <button onClick={async () => {
                  setReviewOpen(j.id); setReviewData(null);
                  // 失败时给一个显式的 error 标记，而不是静默留空——
                  // 否则弹窗里"什么都没有"，用户分不清"没培训记录"和"接口挂了"
                  const review = await api.get(`/factory/jobs/${j.id}/training-detail`)
                    .catch(() => ({ error: true }));
                  setReviewData(review);
                }}
                  className="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg text-xs shrink-0">📋 培训验收</button>
              )}
              {(j.status === "ACTIVE") && (
                <span className="text-[11px] px-2 py-1 rounded-full bg-emerald-500/15 text-emerald-300 shrink-0">✅ 已上岗</span>
              )}
            </div>

            {/* 状态时间线 */}
            <div className="flex items-center gap-1.5 text-[10px]">
              {steps.map((st, i) => {
                const done = j.status === "ACTIVE" ? true : i <= (stepIdx === -1 ? 0 : stepIdx);
                const cur = st.k === j.status;
                return (
                  <div key={st.k} className="flex items-center gap-1.5">
                    <span className={`px-2 py-0.5 rounded-full ${cur ? "bg-indigo-500 text-white" :
                      done ? "bg-emerald-500/15 text-emerald-300" : "bg-white/[0.04] text-[var(--muted)]"}`}>
                      {done && !cur ? "✓ " : ""}{st.label}
                    </span>
                    {i < steps.length - 1 && <span className="text-gray-700">→</span>}
                  </div>
                );
              })}
            </div>

            {/* 蓝图详情：职责 / 技能 / 知识域 可视化 */}
            {bp.role && (
              <div className="grid md:grid-cols-3 gap-2 text-xs">
                <div className="rounded-lg p-2 bg-white/[0.03]">
                  <p className="text-[10px] text-cyan-300 mb-1">📋 职责</p>
                  {(bp.responsibilities || []).slice(0, 3).map((r2, i2) => (
                    <p key={i2} className="text-[var(--foreground)] truncate">· {r2}</p>
                  ))}
                </div>
                <div className="rounded-lg p-2 bg-white/[0.03]">
                  <p className="text-[10px] text-pink-300 mb-1">💎 蓝图专属技能（已注册+安装）</p>
                  <div className="flex flex-wrap gap-1">
                    {(bp.required_skills || []).slice(0, 3).map((sk, i2) => (
                      <span key={i2} className="px-1.5 py-0.5 rounded bg-pink-500/10 text-pink-300">{sk}</span>
                    ))}
                  </div>
                </div>
                <div className="rounded-lg p-2 bg-white/[0.03]">
                  <p className="text-[10px] text-amber-300 mb-1">📚 知识领域</p>
                  <p className="text-[var(--foreground)] truncate">{(bp.knowledge_scope || []).join("、") || "—"}</p>
                </div>
              </div>
            )}

            {j.agent_id && (
              <div className="text-[11px] text-[var(--muted)]">
                Agent ID: {j.agent_id.slice(0, 8)} ·
                <a href={`/agents/${j.agent_id}`} className="text-indigo-400 hover:text-indigo-300 ml-1">查看员工档案 →</a>
              </div>
            )}
          </div>
          );
        })}
        {!loading && !jobs.length && <p className="text-sm text-[var(--muted)]">暂无工厂任务</p>}
        {loadError && (
          <p className="text-sm text-red-300">加载工厂任务失败：{loadError}
            <button onClick={() => refreshJobs()} className="ml-2 underline">重试</button>
          </p>
        )}
      </div>
    {/* 批准前蓝图确认弹窗 */}
      {confirmJob && (() => {
        const j = jobs.find((x) => x.id === confirmJob);
        if (!j) return null;
        const bp = j.blueprint || {};
        return createPortal(
          <div className="fixed inset-0 z-50 flex items-center justify-center px-4 overflow-y-auto py-8"
            style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(6px)" }}
            onClick={() => setConfirmJob(null)}>
            <div className="w-full max-w-2xl rounded-2xl p-5 space-y-3 max-h-[90vh] overflow-y-auto animate-fade-up"
              style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid rgba(99,102,241,0.35)" }}
              onClick={(e) => e.stopPropagation()}>
              <h3 className="font-semibold text-sm">📋 蓝图审查 —— {bp.name || j.goal.slice(0, 24)}</h3>
              <p className="text-[11px]" style={{ color: "var(--muted)" }}>
                确认后系统将：按蓝图创建员工 → 自动注册专属技能并安装 → 执行岗前培训（学习点写入长期记忆）→ 进入测试期
              </p>
              <div className="space-y-2 text-xs">
                <p><span className="text-cyan-300">岗位：</span>{bp.role}</p>
                <div>
                  <p className="text-pink-300 mb-1">专属技能（将自动注册+安装）</p>
                  <div className="flex flex-wrap gap-1">
                    {(bp.required_skills || []).map((sk, i) => (
                      <span key={i} className="px-1.5 py-0.5 rounded bg-pink-500/10 text-pink-300">{sk}</span>))}
                  </div>
                </div>
                <div>
                  <p className="text-amber-300 mb-1">职责</p>
                  {(bp.responsibilities || []).map((r2, i) => (
                    <p key={i} style={{ color: "var(--muted)" }}>· {r2}</p>))}
                </div>
              </div>
              <div className="flex gap-2 justify-end pt-2 border-t" style={{ borderColor: "var(--border)" }}>
                <button onClick={() => setConfirmJob(null)} className="btn-ghost text-xs">取消</button>
                <button onClick={async () => {
                  await approve(confirmJob);
                  setConfirmJob(null);
                }} className="btn-primary text-xs">✅ 确认批准，开始岗前培训</button>
              </div>
            </div>
          </div>,
          document.body);
      })()}

      {/* 培训验收面板 */}
      {reviewOpen && (() => {
        const j = jobs.find((x) => x.id === reviewOpen);
        if (!j) return null;
        const sessions = reviewData?.sessions || [];
        const testTasks = reviewData?.test_tasks || [];
        return createPortal(
          <div className="fixed inset-0 z-50 flex items-center justify-center px-4 overflow-y-auto py-8"
            style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(6px)" }}
            onClick={() => setReviewOpen(null)}>
            <div className="w-full max-w-3xl rounded-2xl p-5 space-y-4 max-h-[90vh] overflow-y-auto animate-fade-up"
              style={{ background: "linear-gradient(180deg,#15151f,#101018)", border: "1px solid rgba(99,102,241,0.35)" }}
              onClick={(e) => e.stopPropagation()}>
              <div>
                <h3 className="font-semibold text-sm">🎓 培训验收 —— {j.blueprint?.name || j.goal.slice(0, 24)}</h3>
                <p className="text-[11px]" style={{ color: "var(--muted)" }}>
                  审查培训产出的学习点 → 回答验收问题 → 决定是否让该员工上岗
                </p>
              </div>

              {!reviewData ? (
                <p className="text-xs text-center py-8" style={{ color: "var(--muted)" }}>加载中…</p>
              ) : (
                <>
                  {/* 学习点审查 */}
                  {sessions.map((sess: any) => (
                    <div key={sess.id} className="rounded-xl p-3 space-y-2" style={{ background: "var(--surface-2)" }}>
                      <p className="text-xs font-semibold">{sess.title}
                        <span className="ml-2 text-[10px] text-emerald-300">{sess.status}</span></p>
                      {sess.summary && <p className="text-[11px]" style={{ color: "var(--muted)" }}>{sess.summary}</p>}
                      <div className="space-y-1">
                        {(sess.learning_points || []).map((lp: any, i: number) => (
                          <div key={i} className="flex gap-2 items-start text-xs">
                            <span className="text-emerald-400 shrink-0">✓</span>
                            <span style={{ color: "var(--foreground)" }}>{lp.content}</span>
                          </div>
                        ))}
                        {!(sess.learning_points || []).length && (
                          <p className="text-[11px]" style={{ color: "var(--muted)" }}>（无学习点）</p>)}
                      </div>
                    </div>
                  ))}

                  {/* 验收测试题：一键让 Agent 真实作答 */}
                  <div className="space-y-3">
                    <p className="text-xs font-semibold">📝 验收考试 —— 点击按钮，让该员工现场作答（调用其培训记忆+知识库），你根据回答质量决定是否通过</p>
                    {testTasks.map((t: string, i: number) => {
                      const ans = testAnswers[i];
                      return (
                      <div key={i} className="rounded-xl p-3 space-y-2" style={{ background: "var(--surface-2)" }}>
                        <div className="flex items-start justify-between gap-2">
                          <p className="text-xs flex-1" style={{ color: "#a5b4fc" }}>Q{i + 1}. {t}</p>
                          <button disabled={asking !== null}
                            onClick={async () => {
                              setAsking(i);
                              try {
                                const d = await api.post<{ answer?: string }>(
                                  `/factory/jobs/${reviewOpen}/ask`, { question: t });
                                setTestAnswers((prev) => ({ ...prev, [i]: d.answer || "(无回答)" }));
                              } catch (e) {
                                setTestAnswers((prev) => ({ ...prev, [i]: `❌ ${describeError(e, "作答失败")}` }));
                              }
                              setAsking(null);
                            }}
                            className={`shrink-0 text-[11px] px-2.5 py-1 rounded-lg transition-colors ${ans ? "bg-gray-700 text-[var(--muted)]" : "btn-primary !py-1 !px-2.5"}`}
                            title="让该 Agent 真实回答此题">
                            {asking === i ? "⏳ 作答中…" : ans ? "🔄 重答" : "🎓 让 TA 回答"}
                          </button>
                        </div>
                        {ans && (
                          <div className="rounded-lg p-2.5 border-l-2 animate-fade-up"
                            style={{ borderColor: "#6366f1", background: "rgba(99,102,241,0.06)" }}>
                            <p className="text-[10px] text-indigo-300 mb-1">
                              🤖 {(j.blueprint || {}).name || "该员工"} 的现场回答：
                            </p>
                            <textarea value={ans}
                              onChange={(e) => setTestAnswers((prev) => ({ ...prev, [i]: e.target.value }))}
                              rows={Math.min(8, Math.max(3, Math.ceil(ans.length / 60)))}
                              className="w-full bg-transparent text-xs resize-none outline-none"
                              style={{ color: "var(--foreground)" }} />
                          </div>
                        )}
                      </div>
                      );
                    })}
                  </div>

                  <textarea value={reviewNotes} onChange={(e) => setReviewNotes(e.target.value)}
                    rows={2} placeholder="验收备注（可选）…"
                    className="input w-full !py-2 text-xs resize-none" />

                  <div className="flex gap-2 justify-end pt-2 border-t" style={{ borderColor: "var(--border)" }}>
                    <button onClick={() => setReviewOpen(null)} className="btn-ghost text-xs">稍后再定</button>
                    <button onClick={async () => {
                      try {
                        // 原先不看返回就提示"已驳回"——验收动作没生效也会报喜
                        await api.post(`/factory/jobs/${reviewOpen}/review-training`,
                          { approve: false, notes: reviewNotes || "验收未通过，需重新培训" });
                        setMsg("❌ 已驳回，员工保持禁用状态（培训记忆已清除）");
                        setReviewOpen(null); invalidate("agents:"); refreshJobs();
                      } catch (e) {
                        setMsg(`❌ ${describeError(e, "驳回失败")}`);
                      }
                    }} className="text-xs py-1.5 px-3 rounded-lg text-red-300 hover:bg-red-500/15 transition-colors"
                      style={{ border: "1px solid rgba(248,113,113,0.35)" }}>
                      ✕ 驳回（重新培训）
                    </button>
                    <button onClick={async () => {
                      try {
                        await api.post(`/factory/jobs/${reviewOpen}/review-training`,
                          { approve: true, notes: reviewNotes });
                        setMsg("🎉 验收通过！员工已正式上岗（ACTIVE）");
                        setReviewOpen(null); invalidate("agents:"); refreshJobs();
                      } catch (e) {
                        setMsg(`❌ ${describeError(e, "验收失败")}`);
                      }
                    }} className="btn-primary text-xs">
                      ✅ 验收通过，正式上岗
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>,
          document.body);
      })()}
    </div>
  );
}
