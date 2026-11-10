"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { useResource } from "@/lib/useResource";
import { notify } from "@/components/Toast";
import AgentChat from "@/components/AgentChat";
import InfoEditPanel from "@/components/InfoEditPanel";

/* ============================================================
   Agent 详情中心：对话为主界面 + 员工信息 + 切换驱动模型。

   修复（死代码清理）：此前的"员工信息总览 / 成长 / 训练 / 技能 / 复盘 /
   考核 / 职业"六组面板从未渲染——tab 状态初始化为 "" 后再无任何 setter，
   "📚 综合档案"按钮实际跳转独立的 /agents/{id}/profile 页。约 180 行
   不可达 UI（连同只为它们服务的数据源与函数）一并移除；真实档案页在
   /agents/{id}/profile，功能不受影响。
   ============================================================ */

type Agent = { id: string; name: string; role?: string; status?: string };
type ModelRow = { id: string; name: string; provider_type: string; model_id: string; base_url?: string };
type MenuKey = "none" | "info";

export default function AgentDetailPage() {
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const urlId = params?.id || "";
  const [agentId, setAgentId] = useState<string>(urlId);
  const [menu, setMenu] = useState<MenuKey>("none");
  // 切换模型
  const [newModelId, setNewModelId] = useState("");
  const [switching, setSwitching] = useState(false);

  /* ---------- 数据（统一交给 useResource：去重、缓存、切公司/回窗重取） ---------- */
  const { data: agentRows, error: agentsError } = useResource<Agent[]>(
    "agents:all",
    () => api.get<Agent[]>("/agents"),
  );
  const agents = useMemo<Agent[]>(() => agentRows ?? [], [agentRows]);

  const aid = agentId || null;
  const { data: modelRows } = useResource<ModelRow[]>(
    "models:options",
    () => api.get<ModelRow[]>("/models"),
  );
  const allModels = useMemo<ModelRow[]>(() => modelRows ?? [], [modelRows]);
  // 当前绑定模型：驱动「切换驱动模型」下拉的初始值
  const { data: bindingRows, refresh: refreshBindings } = useResource<any[]>(
    aid ? `agent:model-bindings:${aid}` : null,
    () => api.get<any[]>(`/agents/${aid}/models`),
  );
  useEffect(() => {
    if (bindingRows?.length) setNewModelId(bindingRows[0].model_id);
  }, [bindingRows]);

  // 以 URL 为准；URL 无效时才回退到第一个
  useEffect(() => {
    if (!agentRows) return;
    const target = agentRows.some((x) => x.id === urlId) ? urlId : (agentRows[0]?.id || "");
    if (target) setAgentId(target);
    else notify("warning", "还没有数字员工，请先创建或生成团队");
  }, [agentRows, urlId]);

  // 原实现 catch(() => {}) 会把"加载失败"渲染成永远的空页面
  useEffect(() => {
    if (agentsError) notify("error", agentsError);
  }, [agentsError]);

  async function switchModel() {
    if (!agentId || !newModelId) return;
    setSwitching(true);
    try {
      // 后端实际注册的是 PUT /agents/{id}/switch-model，原代码用 PATCH
      // → 405 Method Not Allowed，切换模型永远失败（且错误提示只有一句 HTTP 405）。
      await api.put(`/agents/${agentId}/switch-model`, { model_id: newModelId });
      notify("success", `✅ 已切换到模型: ${allModels.find((m) => m.id === newModelId)?.name || newModelId}`);
      refreshBindings();
    } catch (e: any) {
      notify("error", `❌ ${e.message || String(e)}`);
    }
    setSwitching(false);
  }

  const agentName = agents.find((a) => a.id === agentId)?.name || "";

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      {!agentId ? (
        <p className="text-[var(--muted)]">请选择一个 Agent</p>
      ) : (
        <>
          {/* 顶部栏：左标题 + 右上角两个菜单按钮 */}
          <div className="flex items-center justify-between flex-wrap gap-3 mb-3">
            <h1 className="text-xl font-bold tracking-tight text-[var(--foreground)]">{agentName} · 工作对话</h1>
            <div className="flex gap-2">
              <button onClick={() => { setMenu(menu === "info" ? "none" : "info"); }}
                className={`px-3.5 py-1.5 rounded-full text-xs font-medium transition-all border ${
                  menu === "info" ? "border-indigo-500 bg-indigo-500/15 text-indigo-200"
                                  : "hover:bg-white/[0.05]"}`}
                style={menu === "info" ? {} : { borderColor: "var(--border)", color: "var(--muted)" }}>
                👤 员工信息
              </button>
              <button onClick={() => router.push(`/agents/${agentId}/profile`)}
                className="px-3.5 py-1.5 rounded-full text-xs font-medium transition-all border hover:bg-white/[0.05]"
                style={{ borderColor: "var(--border)", color: "var(--muted)" }}>
                📚 综合档案 →
              </button>
            </div>
          </div>

          {/* 员工信息编辑弹窗（复用原列表页弹窗模式） */}
          {menu === "info" && agentId && (
            <InfoEditPanel agentId={agentId} onClose={() => setMenu("none")} />
          )}

          {/* 对话主界面 */}
          <AgentChat agentId={agentId} />

          {/* ---- 切换模型（固定在底部）---- */}
          <div className="mt-6 card card-static p-3">
            <h4 className="text-sm font-medium text-[var(--foreground)] mb-2">切换驱动模型</h4>
            <div className="flex gap-2 items-center">
              <select value={newModelId} onChange={(e) => setNewModelId(e.target.value)}
                className="flex-1 bg-white/[0.04] border border-[var(--border)] rounded px-3 py-1.5 text-sm text-[var(--foreground)]">
                <option value="">选择模型</option>
                {allModels.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name} ({m.provider_type} · {m.model_id})
                  </option>
                ))}
              </select>
              <button onClick={switchModel} disabled={switching || !newModelId}
                className="btn-primary disabled:opacity-40">
                {switching ? "切换中…" : "确认切换"}
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
