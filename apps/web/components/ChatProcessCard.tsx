"use client";

/**
 * 对话过程卡片：把"正在思考 / 用了哪个模型 / 调了什么工具 / 结果如何"
 * 一次摊开。单聊气泡上方用 Card，输入框上方用 Bar 做即时回执，
 * 历史消息用同一个 Card（live=false）回放。
 */

import { useEffect, useState } from "react";
import {
  formatDuration,
  processActivity,
  processDuration,
  processIdleMs,
  type ChatProcess,
  type ProcStep,
} from "@/lib/chatProcess";

const STUCK_MS = 20_000;

function useTick(active: boolean) {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [active]);
}

function stepIcon(step: ProcStep) {
  if (step.status === "running") return "◌";
  if (step.status === "error") return "⚠";
  if (step.kind === "tool") return "⚙";
  if (step.kind === "model") return "◇";
  if (step.kind === "action") return "✓";
  return "·";
}

function stepColor(step: ProcStep): string {
  if (step.status === "error") return "#fca5a5";
  if (step.status === "running") return "#a5b4fc";
  return "var(--muted)";
}

function headline(proc: ChatProcess, name: string): string {
  if (proc.status === "running") {
    const acting = proc.steps.some(
      (s) => s.kind === "tool" && s.status === "running");
    return acting ? `${name} 正在处理` : `${name} 正在思考中`;
  }
  if (proc.status === "error") return `${name} 处理失败`;
  if (proc.status === "stopped") return "已停止";
  return `${name} 的处理过程`;
}

export function ChatProcessCard({
  process,
  agentName = "AI",
  live = false,
  className = "",
}: {
  process: ChatProcess;
  agentName?: string | null;
  live?: boolean;
  className?: string;
}) {
  const name = agentName?.trim() || "AI";
  const [open, setOpen] = useState(live);
  useTick(live);
  useEffect(() => {
    if (live) setOpen(true);
  }, [live]);

  const now = Date.now();
  const duration = processDuration(process, now);
  const idle = processIdleMs(process, now);
  const stuck = live && idle >= STUCK_MS;
  const activity = processActivity(process);
  const steps = process.steps;

  return (
    <div
      className={`rounded-xl px-2.5 py-2 text-[11px] ${className}`}
      style={{
        background: "rgba(99,102,241,0.07)",
        border: "1px solid rgba(99,102,241,0.22)",
      }}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1.5 text-left"
        aria-expanded={open}
      >
        <span
          className={live ? "pulse-dot" : ""}
          style={{ color: process.status === "error" ? "#f87171" : "#818cf8" }}
          aria-hidden
        >
          {process.status === "error" ? "⚠"
            : process.status === "stopped" ? "■"
              : live ? "●" : "✓"}
        </span>
        <span className="font-medium" style={{ color: "var(--foreground)" }}>
          {headline(process, name)}
        </span>
        <span style={{ color: "var(--muted)" }}>· {formatDuration(duration)}</span>
        <span className="ml-auto text-[10px]" style={{ color: "#818cf8" }}>
          {open ? "收起" : `过程${steps.length ? ` · ${steps.length} 步` : ""}`}
        </span>
      </button>

      {live && (
        <p
          className={`mt-1 truncate ${stuck ? "" : "pulse-dot"}`}
          style={{ color: stuck ? "#fbbf24" : "var(--muted)" }}
          title={activity}
        >
          {stuck
            ? `已 ${formatDuration(idle)} 没有新进展，可能卡住了 · ${activity}`
            : activity}
        </p>
      )}

      {open && (
        <div className="mt-1.5 space-y-1.5">
          {!!steps.length && (
            <ol className="space-y-1">
              {steps.map((s) => (
                <li key={s.id} className="flex gap-1.5">
                  <span
                    className={`w-3 shrink-0 text-center ${s.status === "running" ? "pulse-dot" : ""}`}
                    style={{ color: stepColor(s) }}
                    aria-hidden
                  >
                    {stepIcon(s)}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline gap-1.5">
                      <span
                        className="truncate"
                        style={{ color: stepColor(s) }}
                        title={s.label}
                      >
                        {s.label}
                      </span>
                      {s.status === "running" && (
                        <span className="shrink-0 text-[10px]" style={{ color: "#818cf8" }}>
                          进行中
                        </span>
                      )}
                      {typeof s.durationMs === "number" && (
                        <span className="shrink-0 text-[10px]" style={{ color: "var(--muted)" }}>
                          {formatDuration(s.durationMs)}
                        </span>
                      )}
                    </div>
                    {s.detail && (
                      <p
                        className="truncate text-[10px]"
                        style={{ color: "var(--muted)" }}
                        title={s.detail}
                      >
                        {s.detail}
                      </p>
                    )}
                  </div>
                </li>
              ))}
            </ol>
          )}

          {process.reasoning && (
            <details
              open={live}
              className="rounded-lg px-2 py-1"
              style={{ background: "rgba(255,255,255,0.03)" }}
            >
              <summary
                className="cursor-pointer text-[10px]"
                style={{ color: "#a5b4fc" }}
              >
                思考过程 · {process.reasoning.length} 字
              </summary>
              <div
                className="mt-1 max-h-44 overflow-y-auto whitespace-pre-wrap text-[10px] leading-relaxed"
                style={{ color: "var(--muted)" }}
              >
                {process.reasoning}
              </div>
            </details>
          )}

          {process.error && (
            <p className="text-[10px]" style={{ color: "#fca5a5" }}>
              {process.error}
            </p>
          )}

          {!steps.length && !process.reasoning && !process.error && (
            <p className="text-[10px]" style={{ color: "var(--muted)" }}>
              已收到，正在准备…
            </p>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * 同步接口（群聊等）的等待提示：至少让老板看到已等多久，
 * 而不是一个永远转圈、不知道是慢还是卡住的 spinner。
 */
export function BusyHint({
  startedAt,
  label,
  slowAfterMs = 30_000,
}: {
  startedAt: number | null;
  label: string;
  slowAfterMs?: number;
}) {
  useTick(startedAt != null);
  if (startedAt == null) return null;
  const elapsed = Math.max(0, Date.now() - startedAt);
  const slow = elapsed >= slowAfterMs;
  return (
    <div
      className="flex flex-wrap items-center gap-2 text-xs"
      style={{ color: slow ? "#fbbf24" : "var(--muted)" }}
    >
      <span
        className="h-3 w-3 rounded-full border-2 animate-spin"
        style={{ borderColor: "var(--border)", borderTopColor: "var(--accent)" }}
        aria-hidden
      />
      <span>{label} · {formatDuration(elapsed)}</span>
      {slow && <span>仍在等待服务端返回，不是界面卡死</span>}
    </div>
  );
}

/** 输入框上方的即时回执：不滚动也能看到"正在思考 + 已用时"。 */
export function ChatProcessBar({
  process,
  agentName = "AI",
}: {
  process: ChatProcess | null | undefined;
  agentName?: string | null;
}) {
  const live = process?.status === "running";
  useTick(!!live);
  if (!process || !live) return null;
  const name = agentName?.trim() || "AI";
  const now = Date.now();
  const idle = processIdleMs(process, now);
  const stuck = idle >= STUCK_MS;
  return (
    <div
      className="flex items-center gap-1.5 border-b px-3.5 py-1.5 text-[10px]"
      style={{
        borderColor: "rgba(99,102,241,0.25)",
        background: "rgba(99,102,241,0.08)",
        color: "#c7d2fe",
      }}
      title={processActivity(process)}
    >
      <span className="pulse-dot" style={{ color: "#818cf8" }} aria-hidden>●</span>
      <span className="shrink-0 font-medium">
        {stuck ? `${name} 可能卡住了` : `${name} 正在思考中`}
      </span>
      <span className="shrink-0" style={{ color: "var(--muted)" }}>
        · {formatDuration(processDuration(process, now))}
      </span>
      <span className="truncate" style={{ color: "var(--muted)" }}>
        {stuck
          ? `${formatDuration(idle)} 没有新进展 · ${processActivity(process)}`
          : processActivity(process)}
      </span>
    </div>
  );
}
