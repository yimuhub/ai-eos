"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, describeError, downloadURL, fetchBlobURL, isCancelled, streamPost } from "@/lib/api";
import { notify } from "@/components/Toast";
import { ChatProcessBar, ChatProcessCard } from "@/components/ChatProcessCard";
import {
  applyChatProcessEvent,
  isProcessEvent,
  newChatProcess,
  processFromMeta,
  type ChatProcess,
  type ChatProcessEvent,
} from "@/lib/chatProcess";

/* ============================================================
   AgentChat：老板↔员工对话主界面
   - 支持附件上传（图片/文字/PPT/Excel/Word/MD）→ 直接进私人知识库
   ============================================================ */

type Msg = {
  id?: string; role: string; content: string; created_at?: string; streaming?: boolean;
  /** 本轮过程（思考/模型/工具）；历史消息从 meta.process 还原 */
  process?: ChatProcess | null;
};
type HistoryMsg = Msg & { meta?: unknown };
type FolderFile = { id?: string; name: string; size?: number };

export default function AgentChat({
  agentId,
  onFileUploaded,
}: {
  agentId: string;
  onFileUploaded?: () => void;
}) {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadNote, setUploadNote] = useState("");
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [folderOpen, setFolderOpen] = useState(false);
  const [folderFiles, setFolderFiles] = useState<FolderFile[]>([]);
  const endRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => () => abortRef.current?.abort(), []);

  const load = useCallback(async () => {
    try {
      const rows = await api.get<HistoryMsg[]>(`/agents/${agentId}/chat/history?limit=50`);
      setMsgs(rows.map((m) => ({ ...m, process: processFromMeta(m.meta) })));
    } catch { /* 历史拉不到不影响发新消息，保持空列表 */ }
  }, [agentId]);

  useEffect(() => { setMsgs([]); load(); }, [load]);

  const loadFolder = useCallback(async () => {
    try {
      const d = await api.get<{ files?: FolderFile[] }>(`/delivery/agents/${agentId}/folder`);
      setFolderFiles(d.files || []);
    } catch { setFolderFiles([]); }
  }, [agentId]);
  useEffect(() => {
    if (folderOpen) loadFolder();
  }, [folderOpen, loadFolder]);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [msgs, busy]);

  async function uploadOne(file: File): Promise<string> {
    const fd = new FormData();
    fd.append("file", file);
    const d = await api.upload<{ converted?: boolean; chunks?: number }>(
      `/kb2/agents/${agentId}/upload`, fd);
    return d.converted
      ? `✅ ${file.name} → 已转换入库（${d.chunks} 块索引）+ 原始文件保留`
      : `⚠️ ${file.name} 无法提取文本，仅保留原始文件`;
  }

  async function handleFiles(files: FileList | null) {
    if (!files?.length) return;
    setUploading(true); setUploadNote("");
    const notes: string[] = [];
    for (const f of Array.from(files)) {
      try { notes.push(await uploadOne(f)); }
      catch (e) { notes.push(`❌ ${f.name}: ${describeError(e, "上传失败")}`); }
    }
    setUploadNote(notes.join("\n"));
    setUploading(false);
    setPendingFiles([]);
    onFileUploaded?.();
  }

  async function send() {
    const text = input.trim();
    if ((!text && !pendingFiles.length) || busy) return;

    // 先传附件
    if (pendingFiles.length) {
      await handleFiles(pendingFiles as unknown as FileList);
      if (!text) return;
    }

    setMsgs((p) => [...p, { role: "boss", content: text }]);
    setInput(""); setBusy(true);
    const ac = new AbortController();
    abortRef.current = ac;

    // 流式回复：先挂上过程卡（"正在思考"立即可见），再逐字往里填
    setMsgs((p) => [...p, {
      role: "agent", content: "", streaming: true, process: newChatProcess(),
    }]);
    const patchLast = (patch: (m: Msg) => Msg) => setMsgs((p) => {
      if (!p.length) return p;
      const next = p.slice();
      next[next.length - 1] = patch(next[next.length - 1]);
      return next;
    });
    let acc = "";
    let started = false;
    try {
      await streamPost(`/agents/${agentId}/chat/stream`, { message: text }, {
        signal: ac.signal,
        onEvent: (ev) => {
          if (ev.type === "delta" || ev.type === "done" || ev.type === "error"
            || isProcessEvent(ev.type)) {
            patchLast((m) => ({
              ...m,
              process: applyChatProcessEvent(
                m.process ?? newChatProcess(), ev as ChatProcessEvent),
            }));
          }
          if (ev.type === "delta") {
            started = true;
            acc += String(ev.text || "");
            patchLast((m) => ({ ...m, content: acc, streaming: true }));
          } else if (ev.type === "done") {
            acc = String(ev.reply ?? acc);
            patchLast((m) => ({ ...m, content: acc, streaming: false }));
          } else if (ev.type === "error") {
            // 已经吐出的字保留，错误另起一行，避免半截回复被整段覆盖
            patchLast((m) => ({
              ...m,
              content: m.content ? `${m.content}\n\n❌ ${ev.message}` : `❌ ${ev.message}`,
              streaming: false,
            }));
          }
        },
      });
    } catch (e) {
      if (isCancelled(e) || ac.signal.aborted) {
        // 主动停止：保留已收到的字，并说明本轮没落库
        patchLast((m) => ({
          ...m,
          content: m.content ? `${m.content}\n\n（已停止，这轮回复未保存到历史）` : "（已停止）",
          streaming: false,
          process: applyChatProcessEvent(m.process ?? newChatProcess(), { type: "stopped" }),
        }));
      } else {
        const message = describeError(e, "发送失败");
        patchLast((m) => ({
          ...m,
          content: started ? m.content : `❌ ${message}`,
          streaming: false,
          process: applyChatProcessEvent(
            m.process ?? newChatProcess(), { type: "error", message }),
        }));
      }
    }
    if (abortRef.current === ac) abortRef.current = null;
    setBusy(false);
  }

  const ACCEPT = ".txt,.md,.csv,.json,.pdf,.docx,.xlsx,.xls,.pptx,.ppt,image/*";

  return (
    <div className="card flex flex-col" style={{ height: "72vh", minHeight: 480 }}>
      <div className="flex items-center gap-2 pb-2.5 border-b" style={{ borderColor: "var(--border)" }}>
        <div className="w-8 h-8 rounded-xl flex items-center justify-center text-white text-sm"
          style={{ background: "linear-gradient(135deg,#6366f1,#a78bfa)" }}>💬</div>
        <div>
          <h3 className="font-semibold text-sm">单独对话</h3>
          <p className="text-[10px]" style={{ color: "var(--muted)" }}>
            问答 · 下达任务 · 安排其他员工 · 附件直接进私人知识库
          </p>
        </div>
        <button title="查看我的交付文件夹"
          onClick={() => setFolderOpen(!folderOpen)}
          className={`ml-auto w-8 h-8 rounded-lg flex items-center justify-center shrink-0 transition-colors ${
            folderOpen ? "bg-indigo-500/20 text-indigo-300" : "hover:bg-white/[0.06]"}`}
          style={folderOpen ? {} : { color: "var(--muted)" }}>📁</button>
      </div>

      {/* 交付文件夹面板 */}
      {folderOpen && (
        <div className="my-2 rounded-xl p-3 animate-fade-up overflow-y-auto max-h-44 space-y-1"
          style={{ background: "var(--surface-2)", border: "1px solid rgba(99,102,241,0.3)" }}>
          <p className="text-[11px] font-medium mb-1">📁 我的交付文件夹（{folderFiles.length}）</p>
          {folderFiles.map((f) => (
            <div key={f.id} className="flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-xs"
              style={{ background: "rgba(255,255,255,0.03)" }}>
              <span className="truncate flex-1">{f.name}</span>
              <button onClick={async () => {
                // 下载接口同样要 Bearer token，必须取回 blob 再触发保存
                try {
                  downloadURL(await fetchBlobURL(`/delivery/download/${f.id}`), f.name);
                } catch (e: any) {
                  notify("error", `下载失败：${describeError(e)}`);
                }
              }} className="text-indigo-300 shrink-0 text-[10px]">⬇</button>
            </div>
          ))}
          {!folderFiles.length && (
            <p className="text-[11px] text-center py-2" style={{ color: "var(--muted)" }}>
              还没有产出文件 · 对话中让我生成文档即可
            </p>
          )}
        </div>
      )}

      <ChatProcessBar process={msgs[msgs.length - 1]?.process} agentName="员工" />

      <div className="flex-1 overflow-y-auto py-3 space-y-2.5">
        {!msgs.length && (
          <p className="text-xs text-center py-8 leading-relaxed" style={{ color: "var(--muted)" }}>
            开始和这位员工对话。例如：<br />
            「今天进账多少钱？」「明天写一份周报」<br />
            「安排市场部明天做调研」 · 附件会自动存入 TA 的私人知识库
          </p>
        )}
        {msgs.map((m, i) => (
          <div key={m.id || i} className={`flex flex-col ${m.role === "boss" ? "items-end" : "items-start"}`}>
            <div className={`max-w-[85%] px-3 py-2 rounded-2xl text-xs leading-relaxed whitespace-pre-wrap ${
              m.role === "boss" ? "bg-indigo-600 text-white rounded-br-sm" : "rounded-bl-sm"}`}
              style={m.role !== "boss" ? { background: "var(--surface-2)", color: "var(--foreground)" } : {}}>
              {m.content}
              {m.streaming && <span className="ws-caret" aria-hidden />}
            </div>
            {m.role !== "boss" && m.process && (
              <div className="mt-1 w-full max-w-[92%]">
                <ChatProcessCard
                  process={m.process}
                  agentName="员工"
                  live={!!m.streaming}
                />
              </div>
            )}
          </div>
        ))}
        {uploading && (
          <div className="flex justify-start">
            <div className="px-3 py-2 rounded-2xl rounded-bl-sm text-xs pulse-dot"
              style={{ background: "var(--surface-2)" }}>正在解析附件并存入知识库…</div>
          </div>
        )}
        {busy && !msgs[msgs.length - 1]?.process
          && !(msgs[msgs.length - 1]?.streaming && msgs[msgs.length - 1]?.content) && (
          <div className="flex justify-start">
            <div className="px-3 py-2 rounded-2xl rounded-bl-sm text-xs pulse-dot"
              style={{ background: "var(--surface-2)" }}>对方正在输入…</div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      {/* 上传状态 */}
      {uploadNote && (
        <div className="mb-1.5 px-3 py-2 rounded-lg text-[11px] whitespace-pre-wrap"
          style={{ background: "rgba(99,102,241,0.08)", color: "#a5b4fc" }}
          onClick={() => setUploadNote("")}>
          {uploadNote}
        </div>
      )}

      {/* 待发送附件提示 */}
      {pendingFiles.length > 0 && (
        <div className="mb-1.5 flex flex-wrap gap-1.5 items-center">
          {Array.from(pendingFiles).map((f, i) => (
            <span key={i} className="text-[10px] px-2 py-0.5 rounded-full"
              style={{ background: "var(--surface-2)", color: "var(--muted)" }}>
              📎 {f.name} ✕
            </span>
          ))}
        </div>
      )}

      {/* 输入区 */}
      <div className="pt-2.5 border-t flex gap-2 items-center" style={{ borderColor: "var(--border)" }}>
        <input ref={fileRef} type="file" multiple
          accept=".txt,.md,.csv,.json,.pdf,.docx,.xlsx,.xls,.pptx,.ppt,image/*"
          className="hidden"
          onChange={(e) => {
            if (e.target.files?.length) {
              handleFiles(e.target.files);
            }
            e.target.value = "";
          }} />
        <button title="上传附件（图片/文字/Office/MD）→ 存入私人知识库"
          onClick={() => fileRef.current?.click()}
          className="w-8 h-8 rounded-lg flex items-center justify-center hover:bg-white/[0.06] shrink-0"
          style={{ color: "var(--muted)" }}>📎</button>
        <input value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && send()}
          placeholder="输入消息，Enter 发送…"
          className="input flex-1 !py-2 text-xs" />
        {busy ? (
          <button onClick={() => abortRef.current?.abort()}
            className="text-xs px-4 py-2 rounded-lg font-medium shrink-0"
            style={{ background: "rgba(248,113,113,0.14)", color: "#fca5a5", border: "1px solid rgba(248,113,113,0.35)" }}>
            停止
          </button>
        ) : (
          <button onClick={send} disabled={!input.trim() && !pendingFiles.length}
            className="btn-primary !py-2 !px-4 text-xs shrink-0 disabled:opacity-40">
            发送
          </button>
        )}
      </div>
    </div>
  );
}
