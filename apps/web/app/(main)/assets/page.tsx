"use client";
import { useMemo, useRef, useState } from "react";
import { api, API_BASE, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { notify } from "@/components/Toast";
import { ASSET_TYPE_ZH, zh } from "@/lib/i18n";
import type { Asset } from "@/lib/types";

export default function AssetsPage() {
  const cid = useActiveCompanyId();
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const { data, refresh: load } = useResource(
    cid ? `assets:${cid}` : null,
    () => api.get<Asset[]>(`/assets?company_id=${cid}`),
  );
  const assets = useMemo(() => data ?? [], [data]);

  async function upload(file: File) {
    if (!cid) return;
    setBusy(true);
    try {
      const form = new FormData();
      form.append("file", file);
      // 原来这里有两处硬编码：地址写死 http://localhost:8000（换成别的部署就是坏链），
      // 且不看返回值——上传失败页面上什么都没有。
      await api.upload(`/assets/upload?company_id=${cid}`, form);
      notify("success", `已上传 ${file.name}`);
      await load();
    } catch (e) {
      notify("error", `上传失败：${describeError(e, "上传失败")}`);
    }
    setBusy(false);
  }

  async function preview(id: string) {
    try {
      const d = await api.get<{ content?: unknown }>(`/assets/${id}`);
      // 文本类素材用 Toast 展示（原来用 alert：超长内容会被系统弹窗截断且无法复制）
      // 二进制素材走下载，交给浏览器处理
      if (d.content !== undefined) {
        const text = String(d.content);
        notify("info", text.length > 2000 ? `${text.slice(0, 2000)}…（已截断，共 ${text.length} 字）` : text);
      } else {
        // 用 API_BASE 拼下载地址，不再写死 localhost
        window.open(`${API_BASE}/assets/${id}/download`, "_blank");
      }
    } catch (e) {
      notify("error", `读取素材失败：${describeError(e, "读取失败")}`);
    }
  }

  return (
    <div className="p-6 max-w-[1400px] mx-auto animate-fade-up">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-bold tracking-tight">成果资产中心</h1>
        <div>
          <input ref={fileRef} type="file" hidden
            onChange={(e) => { const f = e.target.files?.[0]; if (f) void upload(f); e.target.value = ""; }} />
          <button onClick={() => fileRef.current?.click()} disabled={!cid || busy}
            className="btn-primary disabled:opacity-40">
            {busy ? "上传中…" : "⬆ 上传文件"}
          </button>
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
              <td className="text-xs text-[var(--muted)]">{((a.size ?? 0) / 1024).toFixed(1)} KB</td>
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
