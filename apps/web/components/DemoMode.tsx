"use client";

// 免 Key 演示模式。
//
// ## 为什么是纯前端模拟
//
// 小白在没有 API Key 时走完向导，只能看到一群接不了任务的空壳员工。
// 这个演示用 1 个演示员工 + 1 个示例任务做载体，任务执行走预设模板 /
// 关键词规则回复——**全程不调用任何真实模型、不写数据库、不消耗 Token**。
// 每处回复都明确标注「演示」，避免用户误以为是真实能力。
//
// 保持最小可用：不新增后端接口，不污染真实数据。

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  DEMO_DELIVERABLE_MARKDOWN,
  DEMO_TASK_TITLE,
  demoDeliverableFileName,
  demoDeliverableMarkdown,
} from "@/lib/demoDeliverable";

type Msg = { from: "agent" | "user"; text: string };
type DemoStage = "idle" | "planning" | "writing" | "done";
type CopyState = "idle" | "copied" | "failed";

const DEMO_AGENT_NAME = "演示小助手";

const STAGE_TEXT: Record<Exclude<DemoStage, "idle">, string> = {
  planning: "🧠 规划中…（模板：朋友圈文案三段式：钩子 → 卖点 → 行动号召）",
  writing: "✍️ 撰写中…（规则：每段不超过 2 行，结尾带话题标签）",
  done: "✅ 演示完成",
};

/** 关键词 → 模板回复。命中不了就走默认模板。 */
function templateReply(input: string): string {
  const t = input.trim();
  if (/你好|您好|^hi$|hello/i.test(t))
    return "你好呀！我是演示小助手。这整段对话都是预设模板回复，没有调用任何真实模型——你可以随便问，看看交互长什么样。";
  if (/任务|做什么|干活/.test(t))
    return `当前示例任务是「${DEMO_TASK_TITLE}」。点上方的「开始演示任务」，你会看到规划 → 撰写 → 交付的完整走位（全是模板演出）。`;
  if (/模型|密钥|key|api/i.test(t))
    return "演示模式不需要模型和密钥。等你配好真实模型，员工才会真正『动脑』——向导第 2 步会手把手教你。";
  if (/谢谢|感谢/.test(t))
    return "不客气！准备好就关掉演示，继续走完三步向导，组建你真正的团队吧。";
  if (/价格|费用|多少钱/.test(t))
    return "演示不花一分钱。真实使用时的花费取决于你选的模型供应商，Token 统计页能看到每一笔。";
  return `（🎭 演示模板回复）收到：「${t.slice(0, 40)}」。真实模式下，这里会由员工绑定的大模型来回答。`;
}

export default function DemoMode({ onClose }: { onClose: () => void }) {
  const [mounted, setMounted] = useState(false);
  const [msgs, setMsgs] = useState<Msg[]>([
    {
      from: "agent",
      text: "我是演示小助手（示例员工）。下面有一条示例任务，点「开始演示任务」看我走完它——全程模板演出，不调用真实模型。",
    },
  ]);
  const [input, setInput] = useState("");
  const [stage, setStage] = useState<DemoStage>("idle");
  const [progress, setProgress] = useState(0);
  const [copyState, setCopyState] = useState<CopyState>("idle");
  const endRef = useRef<HTMLDivElement>(null);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);

  useEffect(() => { setMounted(true); }, []);
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [msgs, stage]);
  useEffect(() => () => { timers.current.forEach(clearTimeout); }, []);

  function later(ms: number, fn: () => void) {
    timers.current.push(setTimeout(fn, ms));
  }

  function startDemo() {
    if (stage !== "idle") return;
    setCopyState("idle");
    setStage("planning");
    setProgress(15);
    later(1400, () => { setStage("writing"); setProgress(60); });
    later(3000, () => {
      setStage("done");
      setProgress(100);
      setMsgs((prev) => [...prev, {
        from: "agent",
        text: `任务执行完毕，交付物如下（🎭 演示模板生成）：\n\n${DEMO_DELIVERABLE_MARKDOWN}`,
      }]);
    });
  }

  /** 纯前端下载：Blob → objectURL → 临时 <a download>，不经过后端、不写库。 */
  function downloadDeliverable() {
    const now = new Date();
    const blob = new Blob([demoDeliverableMarkdown(now)], {
      type: "text/markdown;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = demoDeliverableFileName(now);
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  async function copyDeliverable() {
    try {
      await navigator.clipboard.writeText(demoDeliverableMarkdown(new Date()));
      setCopyState("copied");
      later(2000, () => setCopyState("idle"));
    } catch {
      setCopyState("failed");
    }
  }

  function restartDemo() {
    setStage("idle");
    setProgress(0);
    setCopyState("idle");
  }

  function send() {
    const t = input.trim();
    if (!t) return;
    setInput("");
    setMsgs((prev) => [...prev, { from: "user", text: t }]);
    later(500, () => {
      setMsgs((prev) => [...prev, { from: "agent", text: templateReply(t) }]);
    });
  }

  if (!mounted) return null;

  return createPortal(
    <div className="fixed inset-0 z-[9990] flex items-center justify-center px-4 overflow-y-auto py-8"
      style={{ background: "rgba(4,4,8,0.65)", backdropFilter: "blur(4px)" }}
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}
      role="dialog" aria-modal="true" aria-label="免密钥演示模式">
      <div className="w-full max-w-lg rounded-2xl overflow-hidden animate-fade-up"
        style={{ background: "linear-gradient(180deg,#15151f,#101018)",
                 border: "1px solid rgba(245,158,11,0.45)" }}
        onMouseDown={(e) => e.stopPropagation()}>
        {/* 头部：明确标注演示 */}
        <div className="px-5 py-4 border-b flex items-center gap-3"
          style={{ borderColor: "var(--border)" }}>
          <span className="w-10 h-10 rounded-xl flex items-center justify-center text-lg shrink-0"
            style={{ background: "linear-gradient(135deg,#f59e0b,#ef4444)" }}>🎭</span>
          <div className="flex-1 min-w-0">
            <h2 className="font-bold text-sm">
              演示模式
              <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded"
                style={{ background: "rgba(245,158,11,0.18)", color: "#fcd34d" }}>
                免密钥 · 模板回复
              </span>
            </h2>
            <p className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>
              所有回复均为预设模板，未调用任何真实模型，不消耗 Token，不写入数据库。
            </p>
          </div>
          <button onClick={onClose} className="btn-ghost !px-2 !py-1 text-xs">✕</button>
        </div>

        <div className="px-5 py-4 space-y-3 max-h-[70vh] overflow-y-auto">
          {/* 演示员工 + 示例任务卡片 */}
          <div className="rounded-xl p-3 space-y-2" style={{ background: "var(--surface-2)" }}>
            <div className="flex items-center gap-2">
              <span className="w-7 h-7 rounded-lg flex items-center justify-center text-white text-xs font-bold"
                style={{ background: "linear-gradient(135deg,#f59e0b,#ef4444)" }}>
                演
              </span>
              <div>
                <div className="text-xs font-medium">{DEMO_AGENT_NAME}</div>
                <div className="text-[10px]" style={{ color: "var(--muted)" }}>示例员工（演示专用）</div>
              </div>
              <span className="ml-auto text-[10px] px-1.5 py-0.5 rounded"
                style={{ background: "rgba(245,158,11,0.15)", color: "#fcd34d" }}>🎭 演示</span>
            </div>
            <div className="text-xs rounded-lg px-2.5 py-2"
              style={{ background: "rgba(255,255,255,0.03)" }}>
              📋 {DEMO_TASK_TITLE}
            </div>
            {stage === "idle" ? (
              <button onClick={startDemo} className="btn-primary w-full !py-2 text-xs">
                ▶ 开始演示任务
              </button>
            ) : (
              <div>
                <p className="text-[11px] mb-1.5" style={{ color: "var(--muted)" }}>
                  {STAGE_TEXT[stage]}
                </p>
                <div className="h-1.5 rounded-full overflow-hidden"
                  style={{ background: "rgba(255,255,255,0.06)" }}>
                  <div className="h-full rounded-full transition-all duration-700"
                    style={{ width: `${progress}%`,
                             background: "linear-gradient(90deg,#f59e0b,#ef4444)" }} />
                </div>
                {stage === "done" && (
                  <div className="mt-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <button onClick={downloadDeliverable}
                        className="btn-primary flex-1 !py-1.5 text-[11px] whitespace-nowrap">
                        ⬇ 下载交付物 (.md)
                      </button>
                      <button onClick={copyDeliverable}
                        className="btn-ghost flex-1 !py-1.5 text-[11px] whitespace-nowrap">
                        📋 复制全文
                      </button>
                      <button onClick={restartDemo}
                        className="btn-ghost flex-1 !py-1.5 text-[11px] whitespace-nowrap">
                        ↺ 再演示一次
                      </button>
                    </div>
                    {copyState !== "idle" && (
                      <p className="text-[10px] mt-1.5"
                        style={{ color: copyState === "copied" ? "#86efac" : "#fca5a5" }}
                        role="status" aria-live="polite">
                        {copyState === "copied" ? "已复制" : "复制失败，请手动选择文本"}
                      </p>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* 对话区 */}
          <div className="space-y-2">
            {msgs.map((m, i) => (
              <div key={i} className={`flex ${m.from === "user" ? "justify-end" : "justify-start"}`}>
                <div className="max-w-[85%] rounded-2xl px-3 py-2 text-xs whitespace-pre-wrap leading-relaxed"
                  style={m.from === "user"
                    ? { background: "linear-gradient(135deg,#4f46e5,#7c3aed)", color: "#fff" }
                    : { background: "var(--surface-2)", color: "var(--foreground)" }}>
                  {m.from === "agent" && (
                    <span className="text-[9px] px-1 py-0.5 rounded mr-1.5 align-middle"
                      style={{ background: "rgba(245,158,11,0.18)", color: "#fcd34d" }}>
                      🎭 演示
                    </span>
                  )}
                  {m.text}
                </div>
              </div>
            ))}
            <div ref={endRef} />
          </div>

          <div className="flex items-center gap-2">
            <input value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && send()}
              placeholder="随便问一句，试试模板回复…"
              className="input flex-1 !py-2 text-xs" />
            <button onClick={send} disabled={!input.trim()}
              className="btn-primary !px-3 !py-2 text-xs shrink-0 disabled:opacity-40">发送</button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}
