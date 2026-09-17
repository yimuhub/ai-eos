"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, getToken } from "@/lib/api";
import { notify } from "@/components/Toast";
import { ASSET_TYPE_ZH, zh } from "@/lib/i18n";

type Asset = { id: string; name: string; type: string; size: number; task_id: string | null; agent_id: string | null };

export default function AssetsPage() {
  const [assets, setAssets] = useState<Asset[]>([]);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    const cs: any[] = await api.get("/companies");
    if (!cs.length) return;
    setAssets(await api.get(`/assets?company_id=${cs[0].id}`));
  }, []);
  useEffect(() => { load(); }, [load]);

  async function upload(file: File) {
    const cs: any[] = await api.get("/companies");
    const form = new FormData();
    form.append("file", file);
    await fetch(`http://localhost:8000/assets/upload?company_id=${cs[0].id}`, {
      method: "POST",
      headers: { Authorization: `Bearer ${getToken()}` },
      body: form,
    });
    load();
  }

  async function preview(id: string) {
    const d = await api.get(`/assets/${id}`);
    // 文本类素材用 Toast 展示（原来用 alert：超长内容会被系统弹窗截断且无法复制）
    // 二进制素材走下载，交给浏览器处理
    if (d.content !== undefined) {
      const text = String(d.content);
      notify("info", text.length > 2000 ? `${text.slice(0, 2000)}…（已截断，共 ${text.length} 字）` : text);
    } else {
      window.open(`http://localhost:8000/assets/${id}/download`, "_blank");
    }
  }

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-bold tracking-tight">成果资产中心</h1>
        <div>
          <input ref={fileRef} type="file" hidden onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
          <button onClick={() => fileRef.current?.click()} className="btn-primary">⬆ 上传文件</button>
        </div>
      </div>
      <table className="w-full text-sm">
        <thead><tr className="text-left text-xs text-[var(--muted)] border-b border-[var(--border)]">
          <th className="py-2">名称</th><th>类型</th><th>大小</th><th>来源</th><th></th>
        </tr></thead>
        <tbody>
          {assets.map((a) => (
            <tr key={a.id} className="border-b border-[var(--border)]">
              <td className="py-2.5">📄 {a.name}</td>
              <td><span className="text-xs bg-white/[0.05] px-2 py-0.5 rounded">{zh(ASSET_TYPE_ZH, a.type)}</span></td>
              <td className="text-xs text-[var(--muted)]">{(a.size / 1024).toFixed(1)} KB</td>
              <td className="text-xs text-[var(--muted)]">{a.agent_id ? `🤖 ${a.agent_id.slice(0, 8)}…` : "👤 用户"}</td>
              <td><button onClick={() => preview(a.id)} className="text-xs text-blue-400 hover:underline">查看</button></td>
            </tr>
          ))}
          {!assets.length && <tr><td colSpan={5} className="py-6 text-center text-xs text-gray-600">暂无资产。Agent 完成任务后，成果将自动保存到这里。</td></tr>}
        </tbody>
      </table>
    </div>
  );
}
