"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, describeError, downloadURL, fetchBlobURL } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { formatLocalStamp } from "@/lib/remoteDevices";
import { notify } from "@/components/Toast";
import {
  autonomyBreakdown,
  executionStatusLabel,
  executionStatusTone,
  failedChecks,
  formatAutonomy,
  formatCheckValue,
  isTerminalStatus,
  MANUAL_REASONS,
  MANUAL_REASON_LABELS,
  summarizeGates,
  type AutonomyLedger,
  type GateRecord,
  type ManualReason,
} from "@/lib/evidence";
import type { Company } from "@/lib/types";

type Node = {
  node_key: string; node_type: string; name: string;
  config: Record<string, any>; position_x: number; position_y: number;
};
type Edge = { from_node: string; to_node: string; label?: string | null };
type Agent = { id: string; name: string };
type WorkflowInput = {
  name: string;
  type: "text" | "number" | "boolean" | "json" | "file";
  required: boolean;
  default?: unknown;
  description?: string;
};

type FeedbackItem = {
  id: string; workflow_id: string; success: boolean; score: number;
  lessons: string[]; note: string; source: string; created_at: string;
};

type WorkflowMeta = {
  id: string; name: string; version: number; source: string; status: string;
  description?: string | null; project_id?: string | null; company_id?: string;
  score: number | null; run_count: number; success_count: number;
  failure_count: number; reuse_key: string; previous_workflow_id: string;
  inputs?: WorkflowInput[];
  feedback: FeedbackItem[];
};

/** GET /executions/{id} 的响应里与证据有关的部分（steps 只取需要的字段） */
type ExecutionDetail = {
  id: string; status: string; error: string | null;
  steps: { node_key: string; node_name: string; node_type: string;
           status: string; stale_at?: string | null }[];
  gates: GateRecord[];
};


/** 与后端 NodeType 一一对应；新增类型时必须同步这张清单。 */
const WORKFLOW_NODE_TYPES = [
  { type: "START", label: "开始", color: "bg-green-700" },
  { type: "END", label: "结束", color: "bg-red-800" },
  { type: "TASK", label: "任务占位", color: "bg-slate-600" },
  { type: "AGENT", label: "Agent执行", color: "bg-blue-700" },
  { type: "CONDITION", label: "条件判断", color: "bg-yellow-700" },
  { type: "PARALLEL", label: "并行", color: "bg-purple-700" },
  { type: "MERGE", label: "汇聚", color: "bg-purple-900" },
  { type: "WAIT", label: "等待", color: "bg-gray-600" },
  { type: "APPROVAL", label: "人工审批", color: "bg-orange-700" },
  { type: "HUMAN_INPUT", label: "人工输入", color: "bg-amber-700" },
  { type: "SUB_WORKFLOW", label: "子工作流", color: "bg-indigo-700" },
  { type: "HTTP", label: "HTTP请求", color: "bg-cyan-700" },
  { type: "TOOL", label: "工具调用", color: "bg-teal-700" },
  { type: "TRANSFORM", label: "数据转换", color: "bg-fuchsia-700" },
] as const;

const NODE_TYPES = WORKFLOW_NODE_TYPES;
const NODE_TYPE_NOTES: Record<string, string> = {
  TASK: "占位节点：运行时直接通过，不执行任务，也不会等待。",
  SUB_WORKFLOW: "异步子流程：启动子执行并立即返回子执行 ID，不等待子流程完成。",
};
const NODE_TYPE_SHORT: Record<string, string> = {
  TASK: "占位 · 直接通过",
  SUB_WORKFLOW: "异步 · 不等待",
};
const DEFAULT_CONFIG: Record<string, Record<string, unknown>> = {
  WAIT: { seconds: 5 },
  HTTP: { method: "GET", timeout_seconds: 15, headers: {} },
  TOOL: { arguments: {} },
  TRANSFORM: { set: {} },
};
const JSON_FIELDS: Record<string, string[]> = {
  HTTP: ["headers"], TOOL: ["arguments"], TRANSFORM: ["set"],
};
const SENSITIVE_HEADER = /^(authorization|proxy-authorization|x-api-key|api-key|cookie|set-cookie)$/i;
const INPUT_CLASS = "min-h-11 w-full bg-white/[0.04] border border-[var(--border)] rounded-md px-3 py-2 text-sm";
const FIELD_CLASS = "grid gap-1 text-[11px] text-[var(--muted)]";
const INPUT_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;

function inputDefaultText(value: unknown): string {
  if (value == null) return "";
  return JSON.stringify(value);
}

function validateInputValue(item: WorkflowInput, value: unknown): string | null {
  if (item.type === "text") {
    return typeof value === "string" && value.length <= 20_000 ? null : "必须是字符串";
  }
  if (item.type === "file") {
    return typeof value === "string" && value.trim().length > 0 && value.length <= 1_000
      ? null : "必须是非空文件引用";
  }
  if (item.type === "number") {
    return typeof value === "number" && Number.isFinite(value) ? null : "必须是有限数字";
  }
  if (item.type === "boolean") return typeof value === "boolean" ? null : "必须是布尔值";
  try {
    return JSON.stringify(value).length <= 64_000 ? null : "JSON 过大";
  } catch {
    return "必须是合法 JSON";
  }
}

function resolveRunInputs(definitions: WorkflowInput[], drafts: Record<string, string>) {
  const values: Record<string, unknown> = {};
  for (const item of definitions) {
    const draft = drafts[item.name] ?? "";
    if (!draft.trim()) {
      if (item.required) throw new Error(`请填写运行入参「${item.name}」`);
      continue;
    }
    let value: unknown = draft;
    if (item.type === "number") {
      if (!/^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(draft.trim())) {
        throw new Error(`运行入参「${item.name}」必须是数字`);
      }
      value = Number(draft);
    } else if (item.type === "boolean") {
      if (draft !== "true" && draft !== "false") {
        throw new Error(`运行入参「${item.name}」必须是布尔值`);
      }
      value = draft === "true";
    } else if (item.type === "json") {
      try {
        value = JSON.parse(draft);
      } catch {
        throw new Error(`运行入参「${item.name}」不是合法 JSON`);
      }
    }
    const error = validateInputValue(item, value);
    if (error) throw new Error(`运行入参「${item.name}」${error}`);
    values[item.name] = value;
  }
  return values;
}

const W = 170, H = 64;

type JsonParseResult = { value?: Record<string, unknown>; error?: string };

/** JSON 配置只收对象；保留草稿给用户改，但绝不把解析失败静默写进节点。 */
function parseJsonObject(text: string): JsonParseResult {
  try {
    const value: unknown = JSON.parse(text);
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return { error: "必须是 JSON 对象，例如 {}" };
    }
    return { value: value as Record<string, unknown> };
  } catch {
    return { error: "JSON 格式不正确" };
  }
}

/** HTTP 只允许非敏感请求头；Authorization 等凭据只能引用公司保险库。 */
function parseHttpHeaders(text: string): JsonParseResult {
  const parsed = parseJsonObject(text);
  if (parsed.error || !parsed.value) return parsed;
  const sensitive = Object.keys(parsed.value).find((key) => SENSITIVE_HEADER.test(key.trim()));
  if (sensitive) {
    return { error: `请求头 ${sensitive} 属于凭据；请删除并在 auth_secret_ref 中填写公司密钥引用名` };
  }
  return parsed;
}

function jsonText(value: unknown): string {
  if (value == null) return "{}";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

/** 客户端坐标 → 画布内容坐标；滚动后节点与连线仍使用同一坐标系。 */
function canvasPoint(
  canvas: Pick<HTMLDivElement, "getBoundingClientRect" | "scrollLeft" | "scrollTop" | "clientLeft" | "clientTop">,
  clientX: number,
  clientY: number,
) {
  const rect = canvas.getBoundingClientRect();
  return {
    x: clientX - rect.left - canvas.clientLeft + canvas.scrollLeft,
    y: clientY - rect.top - canvas.clientTop + canvas.scrollTop,
  };
}

function JsonObjectEditor({ label, value, onChange, onValidityChange, validate, hint }: {
  label: string;
  value: unknown;
  onChange: (value: Record<string, unknown>) => void;
  onValidityChange: (valid: boolean) => void;
  validate?: (text: string) => JsonParseResult;
  hint?: string;
}) {
  const [draft, setDraft] = useState(() => jsonText(value));
  const result = (validate || parseJsonObject)(draft);
  return (
    <label className={`${FIELD_CLASS} ${hint ? "md:col-span-2" : ""}`}>
      <span className="font-medium text-[var(--foreground)]">{label}</span>
      <textarea
        aria-label={label}
        value={draft}
        onChange={(e) => {
          const text = e.target.value;
          setDraft(text);
          const next = (validate || parseJsonObject)(text);
          onValidityChange(!next.error);
          if (!next.error && next.value) onChange(next.value);
        }}
        rows={4}
        spellCheck={false}
        aria-invalid={!!result.error}
        className={`${INPUT_CLASS} resize-y font-mono text-xs ${result.error ? "border-red-500" : ""}`}
      />
      {hint && <span>{hint}</span>}
      {result.error && <span role="alert" className="text-red-400">{result.error}</span>}
    </label>
  );
}

export default function WorkflowBuilderPage() {
  const canvasRef = useRef<HTMLDivElement>(null);
  const activeCompanyId = useActiveCompanyId();
  const contextResolved = typeof activeCompanyId === "string" && activeCompanyId.length > 0;
  // 公司列表只用于把激活 ID 显示成名称；保存目标始终是 hook 返回的激活公司。
  const { data: companyData } = useResource<Company[]>(
    "companies:all",
    () => api.get<Company[]>("/companies"),
  );
  const activeCompany = companyData?.find((company) => company.id === activeCompanyId);
  const [nodes, setNodes] = useState<Node[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  // Agent 下拉候选：账号级列表，交给统一数据层（缓存 + 回窗重取）
  const { data: agentData } = useResource<Agent[]>(
    "agents:all",
    () => api.get<Agent[]>("/agents"),
  );
  const agents = useMemo<Agent[]>(() => agentData ?? [], [agentData]);
  const [wfName, setName] = useState("新工作流");
  const [wfDescription, setWfDescription] = useState("");
  const [wfProjectId, setWfProjectId] = useState<string | null>(null);
  const [inputs, setInputs] = useState<WorkflowInput[]>([]);
  const [inputDefaultDrafts, setInputDefaultDrafts] = useState<Record<number, string>>({});
  const [runDrafts, setRunDrafts] = useState<Record<string, string>>({});
  const [runInputError, setRunInputError] = useState("");
  const [savedId, setSavedId] = useState<string | null>(null);
  const [workflowCompanyId, setWorkflowCompanyId] = useState<string | null>(null);
  const contextMatchesWorkflow = !workflowCompanyId || workflowCompanyId === activeCompanyId;
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const [runStatus, setRunStatus] = useState<string>("");
  const [jsonValidity, setJsonValidity] = useState<Record<string, boolean>>({});
  const [editorVersion, setEditorVersion] = useState(0);
  const dragRef = useRef<{ key: string; pointerId: number; dx: number; dy: number } | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);  // 工作流运行轮询句柄
  const linkRef = useRef<{ from: string; pointerId: number | null; x: number; y: number } | null>(null);
  const [linkPreview, setLinkPreview] = useState<{ x1: number; y1: number; x2: number; y2: number } | null>(null);

  /* 从 CEO 指挥台跳转过来时看的是服务端已落库的流程（含版本/评分） */
  const [meta, setMeta] = useState<WorkflowMeta | null>(null);
  const [fbScore, setFbScore] = useState(80);
  const [fbLessons, setFbLessons] = useState("");
  const [fbBusy, setFbBusy] = useState(false);
  const [fbError, setFbError] = useState("");
  const [optimized, setOptimized] = useState<{ id: string; version: number } | null>(null);

  /* 运行证据（M4 前端）：跑完后保留执行详情，而不是只留一行状态文字 */
  const [execution, setExecution] = useState<ExecutionDetail | null>(null);
  const [autonomy, setAutonomy] = useState<AutonomyLedger | null>(null);
  const [reportBusy, setReportBusy] = useState(false);
  const [manual, setManual] = useState<{ reason: ManualReason; minutes: number; note: string }>(
    { reason: "judgment", minutes: 10, note: "" });
  const [manualBusy, setManualBusy] = useState(false);
  const [reworkNode, setReworkNode] = useState("");
  const [reworkText, setReworkText] = useState("");
  const [reworkBusy, setReworkBusy] = useState(false);

  const loadWorkflow = useCallback(async (id: string) => {
    try {
      const wf = await api.get<WorkflowMeta & { nodes: Node[]; edges: Edge[] }>(`/workflows/${id}`);
      setName(wf.name);
      setWfDescription(wf.description || "");
      setWfProjectId(wf.project_id || null);
      setInputs(wf.inputs || []);
      setInputDefaultDrafts(Object.fromEntries((wf.inputs || []).map((item, index) => [index, inputDefaultText(item.default)])));
      setRunDrafts({});
      setRunInputError("");
      setNodes((wf.nodes || []).map((n) => ({
        node_key: n.node_key, node_type: n.node_type, name: n.name,
        config: n.config || {}, position_x: n.position_x || 0, position_y: n.position_y || 0,
      })));
      setEdges(wf.edges || []);
      setSavedId(wf.id);
      setWorkflowCompanyId(wf.company_id || null);
      setMeta(wf);
      setSelected(null);
      setOptimized(null);
      setFbError("");
      setJsonValidity({});
      setEditorVersion((version) => version + 1);
    } catch (e) {
      notify("error", describeError(e, "载入工作流失败"));
    }
  }, []);

  const refreshMeta = useCallback(async (id: string) => {
    try {
      setMeta(await api.get<WorkflowMeta>(`/workflows/${id}`));
    } catch {
      // 元数据刷新失败不影响已经展示的图；下次载入会纠正
    }
  }, []);

  useEffect(() => {
    const open = new URLSearchParams(window.location.search).get("open");
    if (open) void loadWorkflow(open);
  }, [loadWorkflow]);

  async function submitFeedback() {
    if (!savedId || fbBusy) return;
    setFbBusy(true);
    setFbError("");
    try {
      const lessons = fbLessons.split("\n").map((s) => s.trim()).filter(Boolean);
      const res = await api.post<{ workflow: { id: string; version: number }; optimized_from: string }>(
        `/workflows/${savedId}/feedback`,
        { score: fbScore, lessons, note: "工作流编排页人工复盘", optimize: true },
      );
      setFbLessons("");
      await refreshMeta(savedId);
      if (res.optimized_from) {
        setOptimized({ id: res.workflow.id, version: res.workflow.version });
        notify("success", `反馈已记录；低于 80 分，已生成 v${res.workflow.version}`);
      } else {
        setOptimized(null);
        notify("success", "反馈已记录");
      }
    } catch (e) {
      setFbError(describeError(e, "提交反馈失败"));
    } finally {
      setFbBusy(false);
    }
  }


  function addNode(type: string) {
    const key = `n${Date.now().toString(36)}${Math.floor(Math.random() * 100)}`;
    setNodes((ns) => [...ns, {
      node_key: key, node_type: type,
      name: NODE_TYPES.find((t) => t.type === type)?.label || type,
      config: { ...(DEFAULT_CONFIG[type] || {}) },
      position_x: 40 + Math.random() * 320, position_y: 40 + Math.random() * 220,
    }]);
    setSelected(key);
  }

  function clearPointer() {
    dragRef.current = null;
    linkRef.current = null;
    setLinkPreview(null);
  }

  function completeLink(toKey: string) {
    const link = linkRef.current;
    if (!link) return;
    if (toKey !== link.from) {
      setEdges((es) => es.some((edge) => edge.from_node === link.from && edge.to_node === toKey)
        ? es : [...es, { from_node: link.from, to_node: toKey }]);
    }
    clearPointer();
  }

  /** Pointer Events 统一鼠标/触控笔/触摸；捕获保证指针移出节点后仍收到事件。 */
  function onNodePointerDown(e: React.PointerEvent<HTMLDivElement>, key: string) {
    if (!e.isPrimary || e.button !== 0) return;
    e.stopPropagation();
    e.preventDefault();
    const canvas = canvasRef.current;
    if (!canvas) return;
    const point = canvasPoint(canvas, e.clientX, e.clientY);
    const node = nodes.find((item) => item.node_key === key);
    if (!node) return;
    setSelected(key);
    dragRef.current = {
      key, pointerId: e.pointerId,
      dx: point.x - node.position_x, dy: point.y - node.position_y,
    };
    e.currentTarget.setPointerCapture(e.pointerId);
  }

  function onPortPointerDown(e: React.PointerEvent<HTMLButtonElement>, key: string) {
    if (!e.isPrimary || e.button !== 0) return;
    e.stopPropagation();
    e.preventDefault();
    const canvas = canvasRef.current;
    const node = nodes.find((item) => item.node_key === key);
    if (!canvas || !node) return;
    const x = node.position_x + W, y = node.position_y + H / 2;
    linkRef.current = { from: key, pointerId: e.pointerId, x, y };
    setLinkPreview({ x1: x, y1: y, x2: x, y2: y });
    e.currentTarget.setPointerCapture(e.pointerId);
  }

  function onCanvasPointerMove(e: React.PointerEvent<HTMLDivElement>) {
    const canvas = canvasRef.current;
    if (!canvas) return;
    if (linkRef.current?.pointerId === e.pointerId) {
      const point = canvasPoint(canvas, e.clientX, e.clientY);
      setLinkPreview({ x1: linkRef.current.x, y1: linkRef.current.y, x2: point.x, y2: point.y });
      return;
    }
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== e.pointerId) return;
    const point = canvasPoint(canvas, e.clientX, e.clientY);
    const x = Math.max(0, point.x - drag.dx);
    const y = Math.max(0, point.y - drag.dy);
    setNodes((ns) => ns.map((n) =>
      n.node_key === drag.key ? { ...n, position_x: x, position_y: y } : n));
  }

  /** 松开：若在某个节点上释放则完成连线。 */
  function onCanvasPointerUp(e: React.PointerEvent<HTMLDivElement>) {
    if (linkRef.current?.pointerId === e.pointerId) {
      const nodeEl = document.elementFromPoint(e.clientX, e.clientY)
        ?.closest("[data-node-key]") as HTMLElement | null;
      const toKey = nodeEl?.dataset.nodeKey;
      if (toKey) completeLink(toKey);
      else clearPointer();
      return;
    }
    if (dragRef.current?.pointerId === e.pointerId) clearPointer();
  }

  function deleteNode(key: string) {
    setNodes((ns) => ns.filter((n) => n.node_key !== key));
    setEdges((es) => es.filter((e) => e.from_node !== key && e.to_node !== key));
    setJsonValidity((current) => Object.fromEntries(
      Object.entries(current).filter(([field]) => !field.startsWith(`${key}:`))));
    setSelected(null);
  }

  const sel = nodes.find((n) => n.node_key === selected) || null;
  const hasInvalidJson = Object.values(jsonValidity).includes(false) || nodes.some((node) =>
    (JSON_FIELDS[node.node_type] || []).some((field) => {
      const value = node.config?.[field];
      if (value == null) return false;
      if (typeof value !== "object" || Array.isArray(value)) return true;
      const result = field === "headers" && node.node_type === "HTTP"
        ? parseHttpHeaders(JSON.stringify(value)) : parseJsonObject(JSON.stringify(value));
      return !!result.error;
    }));
  const inputErrors = useMemo(() => {
    const errors: Record<number, string> = {};
    const names = new Set<string>();
    inputs.forEach((item, index) => {
      if (!INPUT_NAME_RE.test(item.name)) errors[index] = "名称须以字母或下划线开头，仅含字母、数字、下划线，最长 64 字";
      else if (names.has(item.name)) errors[index] = "输入名称不能重复";
      names.add(item.name);
      const draft = inputDefaultDrafts[index] ?? inputDefaultText(item.default);
      if (!draft.trim()) return;
      try {
        const value = JSON.parse(draft);
        const error = validateInputValue(item, value);
        if (error) errors[index] = `默认值${error}`;
      } catch {
        errors[index] = "默认值必须是合法 JSON；字符串请加引号";
      }
    });
    return errors;
  }, [inputs, inputDefaultDrafts]);
  const hasInvalidInputs = Object.keys(inputErrors).length > 0;
  const gateInfo = useMemo(() => summarizeGates(execution?.gates), [execution]);
  const reworkable = (execution?.steps ?? []).filter(
    (s) => !["START", "END"].includes(s.node_type));

  // selected 指向的节点已被删除时自动清除
  useEffect(() => {
    if (selected && !nodes.some((n) => n.node_key === selected)) {
      setSelected(null);
    }
  }, [nodes, selected]);

  function updateSelConfig(patch: Record<string, any>) {
    setNodes((ns) => ns.map((n) =>
      n.node_key === selected ? { ...n, config: { ...n.config, ...patch } } : n));
  }

  async function save() {
    if (!contextResolved || !contextMatchesWorkflow || saving || hasInvalidJson || hasInvalidInputs) return;
    setSaving(true);
    try {
      const body = {
        name: wfName,
        description: wfDescription || null,
        project_id: wfProjectId,
        inputs: inputs.map(({ name, type, required, default: defaultValue, description }) => ({
          name, type, required,
          ...(defaultValue !== undefined ? { default: defaultValue } : {}),
          ...(description ? { description } : {}),
        })),
        nodes: nodes.map(({ node_key, node_type, name, config, position_x, position_y }) =>
          ({ node_key, node_type, name, config, position_x, position_y })),
        edges,
      };
      // 编辑已保存的工作流（savedId 存在）改为 PUT 原地更新、版本 +1。
      // 新建只使用已校验的激活公司 ID，绝不回退到 companies[0]。
      const res = savedId
        ? await api.put<{ id: string; name: string; version: number }>(`/workflows/${savedId}`, body)
        : await api.post<{ id: string; name: string; version?: number }>(
          `/workflows?company_id=${encodeURIComponent(activeCompanyId!)}`, body);
      setSavedId(res.id);
      setWorkflowCompanyId(activeCompanyId);
      notify("success", res.version ? `已保存：${res.name}（v${res.version}）` : `已保存：${res.name}`);
      void refreshMeta(res.id);
    } catch (e) {
      notify("error", describeError(e, "保存失败"));
    } finally {
      setSaving(false);
    }
  }

  /** 拉一次执行详情 + 自主率账本。轮询与返工后都走这里，避免两套解析。 */
  const loadEvidence = useCallback(async (eid: string) => {
    const ex = await api.get<ExecutionDetail>(`/executions/${eid}`);
    setExecution(ex);
    setRunStatus(`${executionStatusLabel(ex.status)}（${ex.steps.length} 步）`);
    // 自主率只在跑完后取：跑一半的分母还在变，提前展示会误导
    if (isTerminalStatus(ex.status)) {
      try {
        setAutonomy(await api.get<AutonomyLedger>(`/executions/${eid}/autonomy`));
      } catch {
        setAutonomy(null);   // 账本失败不影响主流程，页面显示"—"
      }
    }
    return ex;
  }, []);

  /** 开始/重启轮询。抽出来是因为"整轮重跑""改稿"之后同样要接着盯。 */
  const pollExecution = useCallback((eid: string) => {
    if (pollRef.current) clearInterval(pollRef.current);
    let fails = 0;
    setRunStatus("运行中…");
    pollRef.current = setInterval(async () => {
      try {
        const ex = await loadEvidence(eid);
        fails = 0;
        if (isTerminalStatus(ex.status)) {
          if (pollRef.current) clearInterval(pollRef.current);
          pollRef.current = null;
        }
      } catch {
        // 轮询失败不崩：连续 3 次失败自动停止并提示（否则每 5s 一条未处理 rejection）
        if (++fails >= 3) {
          if (pollRef.current) clearInterval(pollRef.current);
          pollRef.current = null;
          setRunStatus("轮询失败，已停止（刷新页面可查看结果）");
        }
      }
    }, 5000);
  }, [loadEvidence]);

  async function run() {
    if (!contextResolved || !contextMatchesWorkflow || !savedId || running || hasInvalidInputs) return;
    let resolved: Record<string, unknown>;
    try {
      resolved = resolveRunInputs(inputs, runDrafts);
      setRunInputError("");
    } catch (error) {
      setRunInputError((error as Error).message);
      return;
    }
    setRunning(true);
    try {
      const res = await api.post<{ execution_id: string }>(
        `/workflows/${savedId}/run`, { inputs: resolved });
      setAutonomy(null);
      pollExecution(res.execution_id);
    } catch (e) {
      notify("error", describeError(e, "启动失败"));
    } finally {
      setRunning(false);
    }
  }

  /** 下载单文件报告：必须走 blob（window.open 带不上 Bearer token）。 */
  async function downloadReport() {
    if (!execution || reportBusy) return;
    setReportBusy(true);
    try {
      const url = await fetchBlobURL(`/executions/${execution.id}/report.html`);
      downloadURL(url, `report-${execution.id.slice(0, 8)}.html`);
    } catch (e) {
      notify("error", describeError(e, "下载报告失败"));
    } finally {
      setReportBusy(false);
    }
  }

  async function addManualLog() {
    if (!execution || manualBusy) return;
    setManualBusy(true);
    try {
      await api.post(`/executions/${execution.id}/manual-log`, {
        reason: manual.reason, minutes: manual.minutes, note: manual.note || null });
      setManual((m) => ({ ...m, note: "" }));
      setAutonomy(await api.get<AutonomyLedger>(`/executions/${execution.id}/autonomy`));
      notify("success", "已记一条人工介入，自主率随之更新");
    } catch (e) {
      notify("error", describeError(e, "记录失败"));
    } finally {
      setManualBusy(false);
    }
  }

  /** 返工：node_key 为空 = 整轮重跑；给了 feedback = 原稿改稿。 */
  async function rework(feedback?: string) {
    if (!execution || reworkBusy) return;
    const nodeKey = reworkNode || undefined;
    if (feedback !== undefined && !nodeKey) {
      notify("error", "带意见改稿需要先选中一个节点");
      return;
    }
    setReworkBusy(true);
    try {
      await api.post(`/executions/${execution.id}/rework`, {
        node_key: nodeKey,
        mode: feedback !== undefined ? "revise" : "rerun",
        feedback: feedback ?? null,
      });
      setReworkText("");
      notify("success", feedback !== undefined ? "已交回该员工改稿" : "已重新开跑");
      pollExecution(execution.id);
    } catch (e) {
      notify("error", describeError(e, "返工失败"));
    } finally {
      setReworkBusy(false);
    }
  }
  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  return (
    <div className="p-3 sm:p-4 flex flex-col h-full">
      <div className="flex flex-wrap items-center gap-3 mb-3" role="toolbar" aria-label="工作流操作">
        <input value={wfName} onChange={(e) => setName(e.target.value)} aria-label="工作流名称"
          className={`${INPUT_CLASS} sm:w-48`} />
        <div role="status" aria-live="polite" className="text-xs text-[var(--muted)] min-w-0 basis-full sm:basis-auto">
          保存目标：<b className="text-[var(--foreground)]">
            {activeCompanyId === undefined ? "正在确认公司…" : activeCompany?.name || (contextResolved ? activeCompanyId : "未选择公司")}
          </b>
          {!contextResolved && activeCompanyId !== undefined && <span> · 请先创建或选择公司</span>}
          {!contextMatchesWorkflow && <span className="text-red-400"> · 当前流程属于其他公司，请切回对应公司后编辑或运行</span>}
        </div>
        <button type="button" onClick={save} disabled={!contextResolved || !contextMatchesWorkflow || saving || hasInvalidJson || hasInvalidInputs}
          className="min-h-11 bg-green-600 hover:bg-green-500 disabled:opacity-40 px-4 rounded-md text-sm">
          {saving ? "保存中…" : "保存"}
        </button>
        <button type="button" onClick={run} disabled={!contextResolved || !contextMatchesWorkflow || !savedId || running || hasInvalidInputs}
          className="min-h-11 bg-blue-600 hover:bg-blue-500 disabled:opacity-40 px-4 rounded-md text-sm">▶ 运行</button>
        {hasInvalidJson && <span className="text-xs text-red-400">请先修正节点 JSON 配置</span>}
        {hasInvalidInputs && <span className="text-xs text-red-400">请先修正运行入参声明</span>}
        {runInputError && <span role="alert" className="text-xs text-red-400">{runInputError}</span>}
        {runStatus && <span className="text-xs text-[var(--muted)]">{runStatus}</span>}
      </div>

      {meta && (
        <div className="flex items-center gap-2 flex-wrap text-[11px] text-[var(--muted)] mb-2">
          <span className="px-1.5 py-0.5 rounded"
            style={{ background: "rgba(34,211,238,0.14)", color: "#a5f3fc" }}>
            v{meta.version}
          </span>
          <span>{meta.source === "ceo" ? "CEO 自动生成" : "手工编排"}</span>
          <span>状态 {meta.status}</span>
          <span>评分 {meta.score == null ? "—" : meta.score.toFixed(1)}</span>
          <span>跑过 {meta.run_count} 次 · 成功 {meta.success_count} · 失败 {meta.failure_count}</span>
          {meta.reuse_key && <span>复用键「{meta.reuse_key}」</span>}
          {meta.previous_workflow_id && <span>由上一版反馈升级</span>}
        </div>
      )}

      <section aria-label="工作流运行入参" className="card-lg p-3 mb-3 grid gap-4 lg:grid-cols-2">
        <div className="min-w-0">
          <div className="flex items-center justify-between gap-2 mb-2">
            <div>
              <h2 className="text-sm font-medium">入参声明</h2>
              <p className="text-[11px] text-[var(--muted)]">节点配置中用 {"{{input.名称}}"} 引用；默认值按 JSON 填写。</p>
            </div>
            <button type="button" disabled={inputs.length >= 50}
              onClick={() => setInputs((items) => [...items, { name: "", type: "text", required: false }])}
              className="min-h-11 shrink-0 px-3 rounded-md bg-white/[0.08] text-xs disabled:opacity-40">＋添加入参</button>
          </div>
          {inputs.length === 0 && <p className="text-xs text-[var(--muted)]">当前流程无需外部入参。</p>}
          <div className="space-y-3">
            {inputs.map((item, index) => (
              <div key={index} className="rounded-md border border-[var(--border)] p-2 grid gap-2 sm:grid-cols-2">
                <label className={FIELD_CLASS}>
                  <span>名称</span>
                  <input aria-label={`入参 ${index + 1} 名称`} value={item.name}
                    onChange={(e) => setInputs((items) => items.map((current, i) =>
                      i === index ? { ...current, name: e.target.value } : current))}
                    className={INPUT_CLASS} placeholder="subject" />
                </label>
                <label className={FIELD_CLASS}>
                  <span>类型</span>
                  <select aria-label={`入参 ${index + 1} 类型`} value={item.type}
                    onChange={(e) => setInputs((items) => items.map((current, i) =>
                      i === index ? { ...current, type: e.target.value as WorkflowInput["type"] } : current))}
                    className={INPUT_CLASS}>
                    <option value="text">文本</option><option value="number">数字</option>
                    <option value="boolean">布尔</option><option value="json">JSON</option><option value="file">文件引用</option>
                  </select>
                </label>
                <label className={`${FIELD_CLASS} sm:col-span-2`}>
                  <span>默认值 JSON（可留空）</span>
                  <input aria-label={`入参 ${index + 1} 默认值`} value={inputDefaultDrafts[index] ?? ""}
                    onChange={(e) => {
                      const text = e.target.value;
                      setInputDefaultDrafts((drafts) => ({ ...drafts, [index]: text }));
                      let value: unknown;
                      try { value = text.trim() ? JSON.parse(text) : undefined; } catch { value = undefined; }
                      setInputs((items) => items.map((current, i) =>
                        i === index ? { ...current, default: value } : current));
                    }}
                    className={INPUT_CLASS} placeholder='例如 "待评审需求"、2、false 或 {"topic":"AI"}' />
                </label>
                <label className={`${FIELD_CLASS} sm:col-span-2`}>
                  <span>说明</span>
                  <input aria-label={`入参 ${index + 1} 说明`} value={item.description || ""}
                    onChange={(e) => setInputs((items) => items.map((current, i) =>
                      i === index ? { ...current, description: e.target.value } : current))}
                    className={INPUT_CLASS} />
                </label>
                {inputErrors[index] && <p role="alert" className="sm:col-span-2 text-xs text-red-400">{inputErrors[index]}</p>}
                <div className="sm:col-span-2 flex items-center justify-between gap-3">
                  <label className="min-h-11 flex items-center gap-2 text-xs">
                    <input type="checkbox" checked={item.required}
                      onChange={(e) => setInputs((items) => items.map((current, i) =>
                        i === index ? { ...current, required: e.target.checked } : current))} />
                    运行时必填
                  </label>
                  <button type="button" aria-label={`删除入参 ${index + 1}`}
                    onClick={() => {
                      const remaining = inputs.filter((_, i) => i !== index);
                      setInputs(remaining);
                      setInputDefaultDrafts(Object.fromEntries(remaining.map((input, i) => {
                        const oldIndex = i >= index ? i + 1 : i;
                        return [i, inputDefaultDrafts[oldIndex] ?? inputDefaultText(input.default)];
                      })));
                      setRunDrafts(Object.fromEntries(remaining.map((input) => [input.name, runDrafts[input.name] ?? ""])));
                    }}
                    className="min-h-11 px-3 rounded-md text-xs text-red-300 hover:bg-red-500/10">删除</button>
                </div>
              </div>
            ))}
          </div>
        </div>
        <div className="min-w-0">
          <h2 className="text-sm font-medium mb-1">本次运行</h2>
          <p className="text-[11px] text-[var(--muted)] mb-2">留空的可选入参会使用已保存的默认值。</p>
          {inputs.length === 0 ? (
            <p className="text-xs text-[var(--muted)]">直接运行已保存的流程。</p>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2">
              {inputs.map((item, index) => (
                <label key={index} className={`${FIELD_CLASS} ${item.type === "json" ? "sm:col-span-2" : ""}`}>
                  <span>{item.name || `未命名入参 ${index + 1}`}{item.required ? " *" : ""}</span>
                  {item.type === "boolean" ? (
                    <select aria-label={`运行入参 ${item.name || index + 1}`} value={runDrafts[item.name] ?? ""}
                      onChange={(e) => setRunDrafts((drafts) => ({ ...drafts, [item.name]: e.target.value }))}
                      className={INPUT_CLASS}>
                      <option value="">使用默认值</option><option value="true">true</option><option value="false">false</option>
                    </select>
                  ) : (
                    <input aria-label={`运行入参 ${item.name || index + 1}`} type={item.type === "number" ? "number" : "text"}
                      step="any" value={runDrafts[item.name] ?? ""}
                      onChange={(e) => setRunDrafts((drafts) => ({ ...drafts, [item.name]: e.target.value }))}
                      className={INPUT_CLASS} placeholder={item.description || (item.type === "json" ? "合法 JSON" : "可留空")} />
                  )}
                </label>
              ))}
            </div>
          )}
        </div>
      </section>

      <div className="flex gap-2 mb-2 flex-wrap" role="toolbar" aria-label="添加节点">
        {NODE_TYPES.map((t) => (
          <button key={t.type} type="button" onClick={() => addNode(t.type)}
            aria-label={`添加${t.label}节点`} title={NODE_TYPE_NOTES[t.type]}
            className={`min-h-11 touch-manipulation ${t.color} hover:opacity-80 px-3 py-2 rounded-md text-xs`}>＋{t.label}</button>
        ))}
        <span className="text-[10px] text-[var(--muted)] self-center ml-2">
          拖动节点移动｜从节点<b className="text-yellow-400">右侧大圆点</b>拖到另一节点连线｜单击选中编辑
        </span>
      </div>

      <div ref={canvasRef} onPointerMove={onCanvasPointerMove} onPointerUp={onCanvasPointerUp}
        onPointerCancel={clearPointer} onLostPointerCapture={clearPointer}
        className="relative flex-1 bg-gray-950 border border-[var(--border)] rounded-lg overflow-auto min-h-[420px] overscroll-contain"
        onClick={() => setSelected(null)}>

        <svg className="absolute left-0 top-0 pointer-events-none"
          width={Math.max(1, ...nodes.map((n) => n.position_x + W + 80))}
          height={Math.max(1, ...nodes.map((n) => n.position_y + H + 80))}
          style={{ minWidth: "100%", minHeight: "100%" }}>
          <defs><marker id="arrow" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
            <path d="M0,0 L8,3 L0,6 z" fill="#6b7280" />
          </marker></defs>
          {edges.map((e, i) => {
            const a = nodes.find((n) => n.node_key === e.from_node);
            const b = nodes.find((n) => n.node_key === e.to_node);
            if (!a || !b) return null;
            const x1 = a.position_x + W, y1 = a.position_y + H / 2;
            const x2 = b.position_x, y2 = b.position_y + H / 2;
            return (
              <g key={i}>
                <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="#6b7280" strokeWidth="1.5"
                  markerEnd="url(#arrow)" />
                {e.label && <text x={(x1 + x2) / 2} y={(y1 + y2) / 2 - 5} fill="#fbbf24" fontSize="11">{e.label}</text>}
              </g>
            );
          })}
          {/* 连线预览 */}
          {linkPreview && (
            <line {...linkPreview} stroke="#fbbf24" strokeWidth="2" strokeDasharray="5,3" />
          )}
        </svg>

        {nodes.map((n) => {
          const color = NODE_TYPES.find((t) => t.type === n.node_type)?.color || "bg-white/[0.15]";
          return (
            <div key={n.node_key} data-node-key={n.node_key} role="group" tabIndex={0}
              data-selected={selected === n.node_key}
              aria-label={`${n.name}，${n.node_type}${NODE_TYPE_NOTES[n.node_type] ? `，${NODE_TYPE_NOTES[n.node_type]}` : ""}`}
              onPointerDown={(e) => onNodePointerDown(e, n.node_key)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  if (linkRef.current?.pointerId == null) completeLink(n.node_key);
                  else setSelected(n.node_key);
                }
              }}
              onClick={(e) => {
                e.stopPropagation();
                if (linkRef.current?.pointerId == null) completeLink(n.node_key);
                else setSelected(n.node_key);
              }}
              style={{ left: n.position_x, top: n.position_y, width: W, minHeight: H, touchAction: "none" }}
              className={`absolute ${color} ${selected === n.node_key ? "ring-2 ring-white" : ""}
                rounded-md px-3 py-2 cursor-move select-none shadow-lg focus:outline-none focus-visible:ring-2 focus-visible:ring-white`}>
              <div className="text-xs font-medium truncate">{n.name}</div>
              <div className="text-[9px] opacity-80">{NODE_TYPE_SHORT[n.node_type] || n.node_type}</div>
              {/* 44px 触控命中区，视觉圆点保持小巧。 */}
              <button type="button" aria-label={`从${n.name}开始连线`}
                onPointerDown={(e) => onPortPointerDown(e, n.node_key)}
                onKeyDown={(e) => {
                  if (e.key !== "Enter" && e.key !== " ") return;
                  e.preventDefault();
                  e.stopPropagation();
                  const x = n.position_x + W, y = n.position_y + H / 2;
                  linkRef.current = { from: n.node_key, pointerId: null, x, y };
                  setLinkPreview({ x1: x, y1: y, x2: x, y2: y });
                }}
                className="absolute -right-[22px] top-1/2 -translate-y-1/2 w-11 h-11 grid place-items-center bg-transparent cursor-crosshair touch-manipulation">
                <span className="w-3.5 h-3.5 bg-yellow-400 rounded-full border border-black/60 pointer-events-none" />
              </button>
            </div>
          );
        })}
      </div>

      {/* 运行证据：闸门结果 + 自主率 + 报告下载 + 返工。
          没有这层，前面三批机制只存在于数据库里，老板看不到。 */}
      {execution && (
        <div className="mt-3 card-lg p-3 space-y-2">
          <div className="flex items-center gap-2 flex-wrap text-[11px]">
            <span className="text-xs text-[var(--muted)]">本次运行</span>
            <span className="px-1.5 py-0.5 rounded tabular-nums"
              style={{
                background: executionStatusTone(execution.status) === "ok" ? "rgba(52,211,153,0.14)"
                  : executionStatusTone(execution.status) === "bad" ? "rgba(248,113,113,0.14)"
                  : "rgba(255,255,255,0.06)",
                color: executionStatusTone(execution.status) === "ok" ? "#6ee7b7"
                  : executionStatusTone(execution.status) === "bad" ? "#fca5a5" : "var(--muted)",
              }}>
              {executionStatusLabel(execution.status)}
            </span>
            <span className="text-[var(--muted)]">#{execution.id.slice(0, 8)} · {execution.steps.length} 步</span>
            <span className="text-[var(--muted)]">自主率 <b className="text-[var(--foreground)]">{formatAutonomy(autonomy)}</b></span>
            <button type="button" onClick={() => void downloadReport()} disabled={reportBusy}
              className="btn-primary !py-1 !px-3 text-xs disabled:opacity-40">
              {reportBusy ? "生成中…" : "⬇ 下载报告"}
            </button>
            {isTerminalStatus(execution.status) && (
              <button type="button" onClick={() => void rework()} disabled={reworkBusy}
                className="px-2 py-1 rounded text-xs bg-white/[0.08] hover:bg-white/[0.14] disabled:opacity-40">
                整轮重跑
              </button>
            )}
          </div>

          {execution.error && (
            <div className="text-[11px]" style={{ color: "#fca5a5" }}>{execution.error}</div>
          )}

          <div className="text-[10px] text-[var(--muted)]">{autonomyBreakdown(autonomy)}</div>

          {gateInfo.configured ? (
            <div className="space-y-1">
              <div className="text-[10px] text-[var(--muted)]">
                闸门 {gateInfo.rounds} 轮 · 未通过节点 {gateInfo.failedNodes.length} · 返工 {gateInfo.reworked.length}
                {gateInfo.degraded > 0 ? ` · 降级放行 ${gateInfo.degraded}` : ""}
              </div>
              {execution.gates.map((g, i) => {
                const bad = failedChecks(g);
                return (
                  <div key={`${g.node_key}-${i}`} className="text-[10px] rounded-md px-2 py-1"
                    style={{ background: "rgba(255,255,255,0.03)", border: "1px solid var(--border)" }}>
                    <span className="font-medium">{g.node_name || g.node_key}</span>
                    <span className="text-[var(--muted)]"> · 第 {g.gate_round ?? 1} 轮 · {g.stage || "gate"}</span>
                    <span style={{ color: g.passed ? "#6ee7b7" : "#fca5a5" }}>
                      {" "}{g.passed ? "通过" : "未通过"}
                    </span>
                    {g.degraded && <span className="text-[var(--muted)]">（降级）</span>}
                    {typeof g.score === "number" && (
                      <span className="text-[var(--muted)]"> · 评审 {g.score.toFixed(2)}</span>
                    )}
                    {bad.length > 0 && (
                      <div className="text-[var(--muted)]">
                        {bad.map((c) => (
                          <div key={c.rule}>
                            × {c.rule}：实际 {formatCheckValue(c.actual)} / 要求 {formatCheckValue(c.expected)}
                            {c.detail ? `（${c.detail}）` : ""}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="text-[10px] text-[var(--muted)]">该流程没有配置验收闸门（可在节点 config.gate 里声明）</div>
          )}

          {isTerminalStatus(execution.status) && (
            <div className="flex items-center gap-2 flex-wrap pt-1"
              style={{ borderTop: "1px dashed var(--border)" }}>
              <span className="text-[10px] text-[var(--muted)]">记一条人工介入</span>
              <select value={manual.reason}
                onChange={(e) => setManual((m) => ({ ...m, reason: e.target.value as ManualReason }))}
                className="bg-white/[0.04] border border-[var(--border)] rounded px-1.5 py-0.5 text-[10px]">
                {MANUAL_REASONS.map((r) => (
                  <option key={r} value={r}>{MANUAL_REASON_LABELS[r]}</option>
                ))}
              </select>
              <input type="number" min={0} max={1440} value={manual.minutes}
                onChange={(e) => setManual((m) => ({ ...m, minutes: Number(e.target.value) }))}
                className="bg-white/[0.04] border border-[var(--border)] rounded px-1.5 py-0.5 text-[10px] w-16"
                aria-label="人工介入分钟" />
              <span className="text-[10px] text-[var(--muted)]">分钟</span>
              <input value={manual.note}
                onChange={(e) => setManual((m) => ({ ...m, note: e.target.value }))}
                placeholder="做了什么（可空）"
                className="bg-white/[0.04] border border-[var(--border)] rounded px-1.5 py-0.5 text-[10px] flex-1 min-w-[140px]" />
              <button type="button" onClick={() => void addManualLog()} disabled={manualBusy}
                className="px-2 py-1 rounded text-[10px] bg-white/[0.08] hover:bg-white/[0.14] disabled:opacity-40">
                {manualBusy ? "记录中…" : "记录"}
              </button>
            </div>
          )}

          {isTerminalStatus(execution.status) && (
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-[10px] text-[var(--muted)]">交回改稿</span>
              <select value={reworkNode}
                onChange={(e) => setReworkNode(e.target.value)}
                className="bg-white/[0.04] border border-[var(--border)] rounded px-1.5 py-0.5 text-[10px] max-w-[180px]">
                <option value="">选节点（留空=整轮重跑）</option>
                {reworkable.map((s) => (
                  <option key={s.node_key} value={s.node_key}>{s.node_name || s.node_key}</option>
                ))}
              </select>
              <input value={reworkText}
                onChange={(e) => setReworkText(e.target.value)}
                placeholder="只说哪里不对、改成什么（必填才能改稿）"
                className="bg-white/[0.04] border border-[var(--border)] rounded px-1.5 py-0.5 text-[10px] flex-1 min-w-[200px]" />
              <button type="button" onClick={() => void rework(reworkText.trim())}
                disabled={reworkBusy || !reworkText.trim() || !reworkNode}
                className="px-2 py-1 rounded text-[10px] bg-amber-700 hover:bg-amber-600 disabled:opacity-40">
                {reworkBusy ? "提交中…" : "交回该员工改"}
              </button>
            </div>
          )}
        </div>
      )}

      {/* 反馈：低于 80 分自动生成下一版，把经验注入执行指令 */}
      {savedId && (
        <div className="mt-3 card-lg p-3 space-y-2">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs text-[var(--muted)]">流程反馈</span>
            <input
              type="range"
              min={0}
              max={100}
              step={5}
              value={fbScore}
              onChange={(e) => setFbScore(Number(e.target.value))}
              className="w-36"
              aria-label="工作流评分"
            />
            <span className="tabular-nums text-xs w-8">{fbScore}</span>
            <input
              value={fbLessons}
              onChange={(e) => setFbLessons(e.target.value)}
              placeholder="经验教训，一行一条（可空）"
              className="input !py-1 text-xs flex-1 min-w-[180px]"
            />
            <button
              type="button"
              onClick={submitFeedback}
              disabled={fbBusy}
              className="btn-primary !py-1 !px-3 text-xs disabled:opacity-40 shrink-0"
            >
              {fbBusy ? "提交中…" : "提交反馈"}
            </button>
            <span className="text-[10px] text-[var(--muted)]">低于 80 分自动升一版</span>
          </div>

          {optimized && (
            <div className="text-[11px] rounded-md px-2 py-1 flex items-center gap-2 flex-wrap"
              style={{ background: "rgba(52,211,153,0.1)", border: "1px solid rgba(52,211,153,0.35)", color: "#6ee7b7" }}>
              已根据反馈生成 v{optimized.version}。
              <button
                type="button"
                onClick={() => void loadWorkflow(optimized.id)}
                className="underline"
              >
                载入新版
              </button>
            </div>
          )}
          {fbError && (
            <div className="text-[11px]" style={{ color: "#fca5a5" }}>{fbError}</div>
          )}

          {meta?.feedback && meta.feedback.length > 0 && (
            <div className="space-y-1">
              {meta.feedback.slice(0, 3).map((f) => (
                <div key={f.id} className="text-[10px] rounded-md px-2 py-1"
                  style={{ background: "rgba(255,255,255,0.03)", border: "1px solid var(--border)" }}>
                  <span style={{ color: f.success ? "#6ee7b7" : "#fca5a5" }}>{f.score.toFixed(0)} 分</span>
                  <span className="text-[var(--muted)]">
                    {" "}· {f.source === "manual" ? "人工反馈" : "CEO 自动评分"} · {formatLocalStamp(f.created_at)}
                  </span>
                  {f.lessons.length > 0 && (
                    <span className="text-[var(--foreground)]"> — {f.lessons.join("；")}</span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* 响应式属性面板：窄屏单列，宽屏分组；所有操作至少 44px 高。 */}
      {sel && (
        <aside aria-label="节点属性" className="mt-3 card-lg p-3 space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-[var(--muted)]">属性 · {sel.node_type}</span>
            {NODE_TYPE_NOTES[sel.node_type] && (
              <span role="note" className="text-[11px] rounded-md px-2 py-1 bg-amber-500/10 text-amber-200">
                {NODE_TYPE_NOTES[sel.node_type]}
              </span>
            )}
            <button type="button" onClick={() => setSelected(null)} aria-label="关闭属性面板"
              className="ml-auto min-h-11 min-w-11 px-3 rounded-md bg-white/[0.08] text-xs">关闭</button>
            {!["START", "END"].includes(sel.node_type) && (
              <button type="button" onClick={() => deleteNode(sel.node_key)}
                className="min-h-11 px-3 rounded-md bg-red-800 text-xs">删除节点</button>
            )}
          </div>

          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
            <label className={`${FIELD_CLASS} md:col-span-2 xl:col-span-1`}>
              <span className="font-medium text-[var(--foreground)]">节点名称</span>
              <input value={sel.name} onChange={(e) => setNodes((ns) => ns.map((n) =>
                n.node_key === selected ? { ...n, name: e.target.value } : n))} className={INPUT_CLASS} />
            </label>

            {sel.node_type === "TASK" && (
              <label className={`${FIELD_CLASS} md:col-span-2`}>
                <span className="font-medium text-[var(--foreground)]">占位备注</span>
                <textarea value={sel.config?.note || ""} rows={2}
                  onChange={(e) => updateSelConfig({ note: e.target.value })}
                  placeholder="仅用于说明流程意图，不会执行任务" className={INPUT_CLASS} />
              </label>
            )}
            {sel.node_type === "HUMAN_INPUT" && (
              <label className={`${FIELD_CLASS} md:col-span-2`}>
                <span className="font-medium text-[var(--foreground)]">输入提示</span>
                <textarea value={sel.config?.prompt || ""} rows={2}
                  onChange={(e) => updateSelConfig({ prompt: e.target.value })}
                  placeholder="运行到此节点时向人工展示的问题" className={INPUT_CLASS} />
              </label>
            )}
            {sel.node_type === "SUB_WORKFLOW" && (
              <label className={FIELD_CLASS}>
                <span className="font-medium text-[var(--foreground)]">子工作流 ID</span>
                <input value={sel.config?.workflow_id || ""}
                  onChange={(e) => updateSelConfig({ workflow_id: e.target.value })}
                  placeholder="workflow_id" className={INPUT_CLASS} />
                <span>启动后立即继续父流程，不等待子工作流结果。</span>
              </label>
            )}
            {sel.node_type === "AGENT" && (
              <>
                <label className={FIELD_CLASS}>
                  <span className="font-medium text-[var(--foreground)]">执行 Agent</span>
                  <select value={sel.config?.agent_id || ""}
                    onChange={(e) => updateSelConfig({ agent_id: e.target.value })} className={INPUT_CLASS}>
                    <option value="">选择执行 Agent</option>
                    {agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                  </select>
                </label>
                <label className={`${FIELD_CLASS} md:col-span-2`}>
                  <span className="font-medium text-[var(--foreground)]">Agent 指令</span>
                  <textarea value={sel.config?.instruction || ""} rows={2}
                    onChange={(e) => updateSelConfig({ instruction: e.target.value })}
                    placeholder="给 Agent 的指令" className={INPUT_CLASS} />
                </label>
              </>
            )}
            {sel.node_type === "CONDITION" && (
              <label className={`${FIELD_CLASS} md:col-span-2`}>
                <span className="font-medium text-[var(--foreground)]">条件关键词</span>
                <input value={sel.config?.keyword || ""}
                  onChange={(e) => updateSelConfig({ keyword: e.target.value })}
                  placeholder="输出包含关键词走 yes 分支，否则 no" className={INPUT_CLASS} />
              </label>
            )}
            {sel.node_type === "WAIT" && (
              <label className={FIELD_CLASS}>
                <span className="font-medium text-[var(--foreground)]">等待秒数（0–60）</span>
                <input type="number" min={0} max={60} value={sel.config?.seconds ?? 5}
                  onChange={(e) => updateSelConfig({ seconds: Number(e.target.value) })} className={INPUT_CLASS} />
              </label>
            )}
            {sel.node_type === "APPROVAL" && (
              <label className={FIELD_CLASS}>
                <span className="font-medium text-[var(--foreground)]">审批提示</span>
                <input value={sel.config?.approver_hint || ""}
                  onChange={(e) => updateSelConfig({ approver_hint: e.target.value })}
                  placeholder="需要审批的内容" className={INPUT_CLASS} />
              </label>
            )}
            {sel.node_type === "HTTP" && (
              <>
                <label className={`${FIELD_CLASS} md:col-span-2`}>
                  <span className="font-medium text-[var(--foreground)]">请求 URL</span>
                  <input type="url" value={sel.config?.url || ""}
                    onChange={(e) => updateSelConfig({ url: e.target.value })}
                    placeholder="https://public-api.example.com/path" className={INPUT_CLASS} />
                  <span>仅允许公网地址；回环、私网与云元数据地址会被后端拒绝。</span>
                </label>
                <label className={FIELD_CLASS}>
                  <span className="font-medium text-[var(--foreground)]">方法</span>
                  <select value={sel.config?.method || "GET"}
                    onChange={(e) => updateSelConfig({ method: e.target.value })} className={INPUT_CLASS}>
                    {["GET", "POST", "PUT", "PATCH", "DELETE"].map((method) =>
                      <option key={method} value={method}>{method}</option>)}
                  </select>
                </label>
                <label className={FIELD_CLASS}>
                  <span className="font-medium text-[var(--foreground)]">超时（秒，1–120）</span>
                  <input type="number" min={1} max={120} value={sel.config?.timeout_seconds ?? 15}
                    onChange={(e) => updateSelConfig({ timeout_seconds: Number(e.target.value) })} className={INPUT_CLASS} />
                </label>
                <JsonObjectEditor key={`${sel.node_key}:headers:${editorVersion}`} label="非敏感请求头 JSON"
                  value={sel.config?.headers} validate={parseHttpHeaders}
                  hint='对象格式，如 {"Accept":"application/json"}；Authorization、API Key、Cookie 等禁止明文。'
                  onValidityChange={(valid) => setJsonValidity((current) => ({ ...current, [`${sel.node_key}:headers`]: valid }))}
                  onChange={(headers) => updateSelConfig({ headers })} />
                <label className={`${FIELD_CLASS} md:col-span-2`}>
                  <span className="font-medium text-[var(--foreground)]">请求体</span>
                  <textarea value={typeof sel.config?.body === "string" ? sel.config.body
                    : sel.config?.body == null ? "" : JSON.stringify(sel.config.body, null, 2)}
                    rows={3} spellCheck={false} onChange={(e) => updateSelConfig({ body: e.target.value })}
                    placeholder='JSON 字符串或纯文本，如 {"query":"hello"}' className={`${INPUT_CLASS} font-mono text-xs`} />
                </label>
                <label className={`${FIELD_CLASS} md:col-span-2`}>
                  <span className="font-medium text-[var(--foreground)]">auth_secret_ref（公司密钥引用名）</span>
                  <input value={sel.config?.auth_secret_ref || ""} autoComplete="off"
                    onChange={(e) => updateSelConfig({ auth_secret_ref: e.target.value })}
                    placeholder="例如 openai_api_key（不是密钥明文）" className={INPUT_CLASS} />
                  <span>只保存保险库中的引用名；不要粘贴 Token、API Key 或 Cookie。</span>
                </label>
              </>
            )}
            {sel.node_type === "TOOL" && (
              <>
                <label className={FIELD_CLASS}>
                  <span className="font-medium text-[var(--foreground)]">工具名</span>
                  <input value={sel.config?.tool_name || ""}
                    onChange={(e) => updateSelConfig({ tool_name: e.target.value })}
                    placeholder="tool_name" className={INPUT_CLASS} />
                </label>
                <JsonObjectEditor key={`${sel.node_key}:arguments:${editorVersion}`} label="工具参数 arguments JSON"
                  value={sel.config?.arguments} hint='对象格式，如 {"url":"https://example.com"}'
                  onValidityChange={(valid) => setJsonValidity((current) => ({ ...current, [`${sel.node_key}:arguments`]: valid }))}
                  onChange={(args) => updateSelConfig({ arguments: args })} />
              </>
            )}
            {sel.node_type === "TRANSFORM" && (
              <JsonObjectEditor key={`${sel.node_key}:set:${editorVersion}`} label="输出映射 set JSON"
                value={sel.config?.set}
                hint={'对象格式；值可用 "{{节点key.字段}}" 或 "{{input.参数}}" 引用上游数据。'}
                onValidityChange={(valid) => setJsonValidity((current) => ({ ...current, [`${sel.node_key}:set`]: valid }))}
                onChange={(set) => updateSelConfig({ set })} />
            )}
          </div>

          {/* 条件节点出边标签编辑 */}
          {sel.node_type === "CONDITION" && edges.some((ed) => ed.from_node === sel.node_key) && (
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-4">
              {edges.map((e, i) => e.from_node === sel.node_key ? (
                <label key={i} className={`${FIELD_CLASS} sm:flex-row sm:items-center`}>
                  <span>→ {nodes.find((n) => n.node_key === e.to_node)?.name}</span>
                  <input aria-label={`到${nodes.find((n) => n.node_key === e.to_node)?.name}的分支标签`}
                    placeholder="yes/no" value={e.label || ""}
                    onChange={(ev) => setEdges((es) => es.map((x, j) =>
                      j === i ? { ...x, label: ev.target.value } : x))} className={INPUT_CLASS} />
                </label>
              ) : null)}
            </div>
          )}
        </aside>
      )}
    </div>
  );
}
