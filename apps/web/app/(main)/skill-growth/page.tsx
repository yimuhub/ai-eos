"use client";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";

type Agent = { id: string; name: string };
type Skill = { skill: string; level: string; experience_count: number; evidence_count: number };

const LEVEL_ZH: Record<string, string> = { BEGINNER: "入门", JUNIOR: "初级", INTERMEDIATE: "中级", ADVANCED: "高级", EXPERT: "专家" };

export default function SkillGrowthPage() {
  // 激活公司可订阅：切公司后员工与技能树自动跟随，不再用 `/companies → cs[0]` 快照。
  const cid = useActiveCompanyId();
  // 员工列表与技能树互不依赖：并发拉取，任一失败退回空值（与原实现各自 catch 一致）。
  const { data: bundle } = useResource(
    cid ? `skill-growth:${cid}` : null,
    async () => {
      const [agents, nodes] = await Promise.allSettled([
        api.get<Agent[]>("/agents"),
        api.get<any[]>(`/skill-growth/tree?company_id=${cid}`),
      ]);
      return {
        cid,
        agents: agents.status === "fulfilled" ? agents.value || [] : [],
        nodes: nodes.status === "fulfilled" ? nodes.value || [] : [],
      };
    },
  );
  // 切公司时旧 bundle 先不渲染，避免技能树短暂混用上一家公司的数据。
  const active = bundle && bundle.cid === cid ? bundle : null;
  const agents = useMemo<Agent[]>(() => active?.agents ?? [], [active]);
  const nodes = useMemo<any[]>(() => active?.nodes ?? [], [active]);
  const [agentId, setAgentId] = useState("");
  const [msg, setMsg] = useState("");

  // 默认选中第一位员工；员工列表变化（换公司/增删）时修正选择。
  useEffect(() => {
    if (agents.length && !agents.some((a) => a.id === agentId)) setAgentId(agents[0].id);
    else if (!agents.length && agentId) setAgentId("");
  }, [agents, agentId]);

  // 技能矩阵按员工缓存：切换员工时先显示缓存，随后按新鲜度重取。
  const { data: matrix, refresh: refreshMatrix } = useResource<Skill[]>(
    agentId ? `skill-growth:matrix:${agentId}` : null,
    () => api.get<Skill[]>(`/skill-growth/agents/${agentId}/matrix`),
  );
  const skills = useMemo<Skill[]>(() => matrix ?? [], [matrix]);

  async function acquire(skillNode: any) {
    try {
      await api.post(`/skill-growth/agents/${agentId}/acquire`, {
        skill_node_id: skillNode.id,
        source_type: "assignment",
        evidence: [{ type: "assignment" }],
      });
      setMsg(`✅ ${skillNode.name} 已添加`);
      refreshMatrix();
    } catch (e: any) {
      setMsg(e.message || String(e));
    }
  }

  const available = nodes.filter((n) => !skills.some((s) => s.skill === n.name));

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      <h1 className="text-2xl font-bold mb-4 text-[var(--foreground)]">🌱 技能成长</h1>
      <div className="grid grid-cols-[260px_1fr] gap-4">
        <div className="card card-static p-3">
          <h3 className="font-medium mb-2 text-[var(--foreground)]">选择 Agent</h3>
          {agents.map((a) => (
            <button key={a.id} onClick={() => setAgentId(a.id)}
              className={`w-full text-left px-3 py-1.5 rounded text-sm mb-1 ${agentId === a.id ? "bg-blue-600 text-white" : "bg-white/[0.04] text-[var(--foreground)] hover:bg-white/[0.08]"}`}>
              {a.name}
            </button>
          ))}
        </div>
        <div className="space-y-4">
          <div className="card card-static p-3">
            <h3 className="font-medium mb-3 text-[var(--foreground)]">已掌握技能</h3>
            {skills.map((s, i) => (
              <div key={i} className="rounded-xl p-2.5 bg-white/[0.03] border border-[var(--border)] flex justify-between items-center text-sm text-[var(--foreground)] mb-1">
                <span><b className="text-[var(--foreground)]">{s.skill}</b> — {LEVEL_ZH[s.level] || s.level}</span>
                <span className="text-xs text-[var(--muted)]">证据{s.evidence_count} · 经验{s.experience_count}</span>
              </div>
            ))}
            {!skills.length && <p className="text-xs text-[var(--muted)]">暂无掌握技能</p>}
          </div>
          {available.length > 0 && (
            <div className="card card-static p-3">
              <h3 className="font-medium mb-2 text-[var(--foreground)]">可学习技能</h3>
              <div className="flex gap-2 flex-wrap">
                {available.map((n) => (
                  <button key={n.id} onClick={() => acquire(n)}
                    className="px-3 py-1 border border-[var(--border)] rounded text-xs text-[var(--foreground)] hover:bg-white/[0.04] hover:text-[var(--foreground)]">
                    + {n.name}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
      {msg && <p className="mt-3 text-sm text-blue-300">{msg}</p>}
    </div>
  );
}
