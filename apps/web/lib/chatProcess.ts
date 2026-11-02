/**
 * 对话过程（思考 / 模型 / 工具）的共享状态。
 *
 * 后端 SSE 除正文外还会下发：status、model、model_error、reasoning、
 * tool_start、tool_end；消息 meta.process 里还会留一份精简版供历史回放。
 * 这里统一归约成一份前端好渲染的 ChatProcess，三个单聊界面共用，
 * 避免每处各写一套 if/else 后行为漂移。
 */

export type ProcStepKind = "stage" | "model" | "model_error" | "tool" | "action";

export type ProcStep = {
  id: string;
  kind: ProcStepKind;
  label: string;
  detail?: string;
  status: "running" | "done" | "error";
  durationMs?: number;
};

export type ChatProcess = {
  status: "running" | "done" | "error" | "stopped";
  startedAt: number;
  endedAt?: number;
  /** 历史回放用：落库时已经算好的总耗时 */
  durationMs?: number;
  /** 最后一次收到过程事件的时间，用来判断"是否卡住了" */
  updatedAt: number;
  steps: ProcStep[];
  reasoning: string;
  model?: string;
  error?: string;
};

export type ChatProcessEvent = { type: string; [key: string]: unknown };

const MAX_LIVE_STEPS = 60;

export function newChatProcess(now = Date.now()): ChatProcess {
  return {
    status: "running",
    startedAt: now,
    updatedAt: now,
    steps: [],
    reasoning: "",
  };
}

export function isProcessEvent(type: string): boolean {
  return type === "status" || type === "model" || type === "model_error"
    || type === "reasoning" || type === "tool_start" || type === "tool_end";
}

function pushStep(next: ChatProcess, step: ProcStep) {
  if (next.steps.length >= MAX_LIVE_STEPS) return;
  next.steps = [...next.steps, step];
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/**
 * 归约一条流式事件。未知事件（delta/done/error）也会顺手维护
 * "正在生成回答" 与结束态，调用方不需要再写一套状态判断。
 */
export function applyChatProcessEvent(
  proc: ChatProcess,
  ev: ChatProcessEvent,
  now = Date.now(),
): ChatProcess {
  const next: ChatProcess = {
    ...proc,
    steps: proc.steps.slice(),
    updatedAt: now,
  };
  const seq = `${now}-${next.steps.length}`;

  switch (ev.type) {
    case "status": {
      const text = str(ev.text).trim();
      if (!text) break;
      const last = next.steps[next.steps.length - 1];
      // 同一句"正在调用工具（第 N 轮）"重复到达时不再堆叠
      if (last && last.kind === "stage" && last.label === text) break;
      pushStep(next, { id: `s-${seq}`, kind: "stage", label: text, status: "done" });
      break;
    }
    case "model": {
      const name = str(ev.model_name) || str(ev.model_id) || "模型";
      const attempt = Number(ev.attempt || 1);
      next.model = name;
      pushStep(next, {
        id: `m-${seq}`,
        kind: "model",
        label: `调用模型 · ${name}`,
        detail: attempt > 1 ? `第 ${attempt} 顺位（已降级）` : undefined,
        status: "done",
      });
      break;
    }
    case "model_error": {
      const name = str(ev.model_name) || str(ev.model_id) || "模型";
      pushStep(next, {
        id: `e-${seq}`,
        kind: "model_error",
        label: `${name} 调用失败`,
        detail: str(ev.message) || undefined,
        status: "error",
      });
      break;
    }
    case "reasoning": {
      const piece = str(ev.text);
      if (piece) next.reasoning = (next.reasoning + piece).slice(-24000);
      break;
    }
    case "tool_start": {
      pushStep(next, {
        id: str(ev.id) || `t-${seq}`,
        kind: "tool",
        label: str(ev.label) || str(ev.name) || "调用工具",
        detail: str(ev.args) || undefined,
        status: "running",
      });
      break;
    }
    case "tool_end": {
      const id = str(ev.id);
      const summary = str(ev.summary);
      const duration = Number(ev.duration_ms);
      next.steps = next.steps.map((s) => {
        if (s.id !== id) return s;
        return {
          ...s,
          status: ev.ok === false ? "error" : "done",
          durationMs: Number.isFinite(duration) && duration >= 0 ? duration : s.durationMs,
          detail: [s.detail, summary].filter(Boolean).join(" · ") || undefined,
        };
      });
      break;
    }
    case "delta": {
      // 首次出现正文才加"生成回答"这一步；工具轮次之间的空 delta 不重复加
      if (!next.steps.some((s) => s.kind === "stage" && s.label === "正在生成回答…")) {
        pushStep(next, {
          id: `s-${seq}`,
          kind: "stage",
          label: "正在生成回答…",
          status: "done",
        });
      }
      break;
    }
    case "done": {
      const model = str(ev.model);
      if (model) next.model = model;
      next.status = "done";
      next.endedAt = now;
      break;
    }
    case "error": {
      next.status = "error";
      next.endedAt = now;
      next.error = str(ev.message) || next.error || "处理失败";
      break;
    }
    case "stopped": {
      next.status = "stopped";
      next.endedAt = now;
      break;
    }
    default:
      return proc;
  }
  return next;
}

/** 历史消息 meta → 可回放的过程；没有则返回 null。 */
export function processFromMeta(meta: unknown): ChatProcess | null {
  const raw = (meta as { process?: unknown } | null | undefined)?.process;
  if (!raw || typeof raw !== "object") return null;
  const p = raw as {
    duration_ms?: unknown;
    reasoning?: unknown;
    model?: unknown;
    steps?: unknown;
  };
  const steps: ProcStep[] = Array.isArray(p.steps)
    ? p.steps.flatMap((item, i) => {
      if (!item || typeof item !== "object") return [];
      const s = item as {
        kind?: unknown; label?: unknown; detail?: unknown;
        status?: unknown; duration_ms?: unknown;
      };
      const label = str(s.label);
      if (!label) return [];
      return [{
        id: `h-${i}`,
        kind: (str(s.kind) || "stage") as ProcStepKind,
        label,
        detail: str(s.detail) || undefined,
        status: s.status === "error" ? "error" : "done",
        durationMs: typeof s.duration_ms === "number" ? s.duration_ms : undefined,
      }];
    })
    : [];
  const durationMs = typeof p.duration_ms === "number" ? p.duration_ms : undefined;
  const reasoning = str(p.reasoning);
  if (!steps.length && !reasoning && durationMs == null) return null;
  return {
    status: "done",
    startedAt: 0,
    endedAt: durationMs ?? 0,
    durationMs,
    updatedAt: 0,
    steps,
    reasoning,
    model: str(p.model) || undefined,
  };
}

export function processDuration(proc: ChatProcess, now = Date.now()): number {
  if (typeof proc.durationMs === "number") return proc.durationMs;
  const end = proc.endedAt ?? now;
  return Math.max(0, end - proc.startedAt);
}

export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return "0s";
  const total = Math.round(ms / 1000);
  if (total < 1) return "<1s";
  if (total < 60) return `${total}s`;
  const m = Math.floor(total / 60);
  const s = total % 60;
  return s ? `${m}m ${s}s` : `${m}m`;
}

/** 当前正在做什么：优先取还在 running 的工具，其次取最后一步。 */
export function processActivity(proc: ChatProcess): string {
  const running = [...proc.steps].reverse().find((s) => s.status === "running");
  const last = running || proc.steps[proc.steps.length - 1];
  if (!last) return "正在准备…";
  return last.detail ? `${last.label} · ${last.detail}` : last.label;
}

/** 没有新事件的时间：用来提示"可能卡住"，而不是让界面无限转圈。 */
export function processIdleMs(proc: ChatProcess, now = Date.now()): number {
  if (proc.status !== "running") return 0;
  return Math.max(0, now - proc.updatedAt);
}
