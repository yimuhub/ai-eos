"use client";
import type { AppTab } from "@/lib/apps";
import { embedURL } from "@/lib/apps";
import WorkspaceOutput, { type OutTab } from "./WorkspaceOutput";
import type { WsAgent } from "@/lib/workspace";

/**
 * 右栏：产出 + 已打开的功能页面。
 *
 * 「产出」固定第一个，其余标签由左栏 / ⌘K 打开，可关闭。
 * 页面用 iframe 承载：40 多个页面各自维护状态、路由、弹窗，改造它们的代价远高于隔离，
 * 而 iframe 天然把状态留在标签里——切回产出再切回来，列表筛选和滚动位置都还在。
 */

const OUT_ID = "out";

export default function WorkspacePanel({
  outTab,
  onOutTab,
  agent,
  cid,
  apps,
  active,
  onActivate,
  onClose,
  onOpenPicker,
  onCollapse,
  focus,
  onToggleFocus,
  onFrame,
  compact,
}: {
  outTab: OutTab;
  onOutTab: (t: OutTab) => void;
  agent: WsAgent | null;
  cid: string;
  apps: AppTab[];
  active: string;
  onActivate: (id: string) => void;
  onClose: (id: string) => void;
  onOpenPicker: () => void;
  onCollapse: () => void;
  focus: boolean;
  onToggleFocus: () => void;
  onFrame: (id: string, el: HTMLIFrameElement | null) => void;
  /** 窄屏上面板本身就是整屏，「展开」无从谈起 */
  compact: boolean;
}) {
  return (
    <div className="flex h-full min-w-0 flex-col" style={{ background: "var(--surface)" }}>
      <div className="flex items-center gap-1 px-2 shrink-0"
        style={{ height: 46, borderBottom: "1px solid var(--border)" }}>
        <div className="flex items-center gap-1 min-w-0 overflow-x-auto ws-scroll">
          <PaneTab active={active === OUT_ID} onClick={() => onActivate(OUT_ID)}>
            <span aria-hidden>▣</span>产出
          </PaneTab>
          {apps.map((t) => (
            <PaneTab key={t.id} active={active === t.id} onClick={() => onActivate(t.id)}
              onClose={() => onClose(t.id)} closeLabel={`关闭 ${t.label}`}>
              <span aria-hidden>{t.icon}</span>
              <span className="max-w-[132px] truncate">{t.label}</span>
            </PaneTab>
          ))}
          <button type="button" onClick={onOpenPicker} title="打开页面（⌘K）" aria-label="打开页面"
            className="w-7 h-7 rounded-lg flex items-center justify-center text-xs shrink-0 hover:bg-white/[0.06]"
            style={{ color: "var(--muted)" }}>
            ＋
          </button>
        </div>

        <div className="ml-auto flex items-center gap-1 shrink-0 pl-2"
          style={{ background: "var(--surface)" }}>
          {!compact && (
            <button type="button" onClick={onToggleFocus} title={focus ? "回到对话" : "展开工作区"}
              className="flex items-center gap-1.5 h-7 px-2 rounded-lg text-[11px] hover:bg-white/[0.06]"
              style={{ color: focus ? "#c7c8ff" : "var(--muted)", border: "1px solid transparent" }}>
              <span aria-hidden>{focus ? "⇤" : "⤢"}</span>
              <span className="hidden sm:inline">{focus ? "回到对话" : "展开"}</span>
            </button>
          )}
          <button type="button" onClick={onCollapse} title={compact ? "回到对话" : "收起右栏 ⌘J"}
            aria-label={compact ? "回到对话" : "收起右栏"}
            className="w-7 h-7 rounded-lg flex items-center justify-center hover:bg-white/[0.06]"
            style={{ color: "var(--muted)" }}>
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"
              strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16" />
            </svg>
          </button>
        </div>
      </div>

      <div className="relative flex-1 min-h-0">
        <div className={active === OUT_ID ? "h-full" : "hidden"}>
          <WorkspaceOutput tab={outTab} onTab={onOutTab} agent={agent} cid={cid} />
        </div>
        {apps.map((t) => (
          <div key={t.id} className={active === t.id ? "h-full" : "hidden"}>
            <iframe
              ref={(el) => onFrame(t.id, el)}
              src={embedURL(t.src)}
              title={t.label}
              className="w-full h-full border-0"
              style={{ background: "var(--background)" }}
            />
          </div>
        ))}
      </div>
    </div>
  );
}

function PaneTab({ active, onClick, onClose, closeLabel, children }: {
  active: boolean;
  onClick: () => void;
  onClose?: () => void;
  closeLabel?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center h-7 rounded-lg shrink-0"
      style={active
        ? { background: "rgba(99,102,241,0.16)", border: "1px solid rgba(99,102,241,0.32)" }
        : { border: "1px solid transparent" }}>
      <button type="button" onClick={onClick} aria-current={active ? "page" : undefined}
        className={`flex items-center gap-1.5 h-full text-[11px] rounded-lg ${
          onClose ? "pl-2.5 pr-1" : "px-2.5"
        }`}
        style={{ color: active ? "#fff" : "var(--muted)" }}>
        {children}
      </button>
      {onClose && (
        <button type="button" onClick={onClose} aria-label={closeLabel} title={closeLabel}
          className="w-5 h-5 mr-1 rounded flex items-center justify-center text-[10px] hover:bg-white/15"
          style={{ color: "#6b6c7a" }}>
          ✕
        </button>
      )}
    </div>
  );
}
