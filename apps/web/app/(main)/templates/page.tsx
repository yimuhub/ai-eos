"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Company } from "@/lib/types";

type Template = { key: string; name: string; builtin: boolean; departments?: number; agents?: number };

export default function TemplatesPage() {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [cid, setCid] = useState("");
  const [companyName, setCompanyName] = useState("我的新公司");
  const [selected, setSelected] = useState("");
  const [msg, setMsg] = useState("");

  useEffect(() => {
    api.get<Company[]>("/companies").then((cs) => setCid(cs[0]?.id));
    api.get("/governance/templates").then(setTemplates);
  }, []);

  const create = async (key: string) => {
    setSelected(key); setMsg("");
    try {
      const r = await api.post(`/governance/create-company-from-template?owner_company_id=${cid}`,
        { template_key: key, company_name: companyName });
      setMsg(`✅ 公司结构创建成功：${r.created_agents.map((a: any) => a.name).join("、")}`);
    } catch (e) { setMsg(String(e)); }
  };

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      <h1 className="text-2xl font-bold mb-4">🏢 企业模板 — 一键创建 AI 公司</h1>

      <div className="border border-[var(--border)] rounded p-4 mb-4 card">
        <label className="text-xs text-[var(--muted)]">公司名称</label>
        <input value={companyName} onChange={(e) => setCompanyName(e.target.value)}
          className="w-full border rounded px-2 py-1 text-sm mt-1" />
      </div>

      <div className="grid grid-cols-2 gap-3">
        {templates.map((t) => (
          <div key={t.key} className={`border rounded p-4 cursor-pointer hover:border-blue-400 ${selected === t.key ? "border-blue-600" : ""}`}
            onClick={() => setSelected(t.key)}>
            <b>{t.name}</b>
            {t.builtin && <span className="ml-2 text-xs bg-blue-900/50 text-blue-300 px-1.5 rounded">内置</span>}
            <div className="text-xs text-[var(--muted)] mt-1">{t.departments || "?"} 部门 · {t.agents || "?"} 个Agent</div>
            <button onClick={(e) => { e.stopPropagation(); create(t.key); }}
              className="mt-2 px-3 py-1 bg-blue-600 text-white rounded text-xs w-full">
              使用此模板创建
            </button>
          </div>
        ))}
      </div>
      {msg && <p className="mt-4 text-sm text-green-700">{msg}</p>}
    </div>
  );
}
