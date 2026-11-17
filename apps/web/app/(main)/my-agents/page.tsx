"use client";
import { useEffect, useState } from "react";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { notify } from "@/components/Toast";

type Binding = { id: string; user_id: string; agent_id: string;
  relation: string; agent_name: string; agent_role?: string };
type AgentRow = { id: string; name: string; role?: string };

const RELATIONS = [
  { value: "assistant", label: "随身助理" },
  { value: "subordinate", label: "下属" },
  { value: "consultant", label: "顾问" },
];

export default function MyAgentsPage() {
  const cid = useActiveCompanyId();
  const [picked, setPicked] = useState<Record<string, string>>({});

  const mine = useResource(
    cid ? `my-agents:${cid}` : null,
    async () => {
      const me = await api.get<{ id: string }>("/auth/me");
      const b = await api.get<{ bindings: Binding[] }>(
        `/member-agents?company_id=${cid}&user_id=${me.id}`);
      const all = await api.get<AgentRow[]>(`/agents?company_id=${cid}`);
      const agents = Array.isArray(all) ? all : [];
      return { cid: cid as string, meId: me.id as string,
               bindings: b?.bindings ?? [], agents };
    },
  );
  const { data: mineData, error: error_, refresh } = mine;
  const active = mineData && mineData.cid === cid ? mineData : null;

  useEffect(() => {
    if (error_) notify("error", error_ || "加载失败");
  }, [error_]);

  const save = async () => {
    if (!cid || !active) return;
    try {
      await api.put(`/member-agents?company_id=${cid}`, {
        user_id: active.meId,
        bindings: Object.entries(picked)
          .filter(([, rel]) => rel)
          .map(([agent_id, relation]) => ({ agent_id, relation })),
      });
      notify("success", "绑定已更新");
      await refresh();
    } catch (e) {
      notify("error", describeError(e, "保存失败"));
    }
  };

  const boundIds = new Set((active?.bindings ?? []).map((b) => b.agent_id));

  return (
    <div className="mx-auto max-w-4xl px-4 py-6 text-[13px] leading-relaxed">
      <div className="mb-1 text-[15px] font-semibold">我的员工</div>
      <p className="mb-4 text-white/50">
        你名下的数字员工：随身助理跟你自己，下属替你干活，顾问按需咨询。每个自然人可以拥有多个 Agent。
      </p>
      {!cid && <div className="text-white/40">先选择公司。</div>}

      {active && (
        <>
          <div className="mb-4 rounded-lg border border-white/10 bg-white/5 p-4">
            <div className="mb-2 font-medium">当前绑定（{active.bindings.length}）</div>
            {active.bindings.map((b) => (
              <div key={b.id} className="flex items-center justify-between py-1">
                <span>{b.agent_name}（{b.agent_role || "员工"}）</span>
                <span className="text-white/50">
                  {RELATIONS.find((r) => r.value === b.relation)?.label || b.relation}
                </span>
              </div>
            ))}
            {!active.bindings.length && <div className="text-white/40">还没有绑定</div>}
          </div>

          <div className="rounded-lg border border-white/10 bg-white/5 p-4">
            <div className="mb-2 font-medium">设置绑定</div>
            <div className="grid gap-1.5">
              {active.agents.map((a) => (
                <div key={a.id} className="flex items-center justify-between rounded-md bg-black/20 px-3 py-1.5">
                  <span>{a.name}{boundIds.has(a.id) && <span className="ml-2 text-[11px] text-emerald-300">已绑定</span>}</span>
                  <select value={picked[a.id] || ""}
                          onChange={(e) => setPicked({ ...picked, [a.id]: e.target.value })}
                          className="rounded-md bg-black/40 px-2 py-1">
                    <option value="">不绑定</option>
                    {RELATIONS.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
                  </select>
                </div>
              ))}
            </div>
            <button onClick={save}
                    className="mt-3 rounded-md bg-emerald-600/80 px-3 py-1.5 hover:bg-emerald-600">
              保存绑定
            </button>
          </div>
        </>
      )}
    </div>
  );
}

