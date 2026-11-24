// @vitest-environment jsdom
import { createElement } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.hoisted(() => ({
  get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn(),
}));
const notifyMock = vi.hoisted(() => vi.fn());
const contextMock = vi.hoisted(() => ({
  companyId: "co-active" as string | null | undefined,
  companies: [
    { id: "co-other", name: "其他公司" },
    { id: "co-active", name: "当前公司" },
  ],
}));

vi.mock("@/lib/api", () => ({
  api: apiMock,
  describeError: (error: unknown, fallback = "操作失败") =>
    (error as { message?: string })?.message || fallback,
  downloadURL: vi.fn(),
  fetchBlobURL: vi.fn(),
}));
vi.mock("@/lib/useResource", () => ({
  useActiveCompanyId: () => contextMock.companyId,
  useResource: (key: string | null) => ({
    data: key === "companies:all" ? contextMock.companies : key ? [] : null,
    error: null,
    loading: false,
    refresh: vi.fn(),
    mutate: vi.fn(),
  }),
}));
vi.mock("@/components/Toast", () => ({ notify: notifyMock }));

import WorkflowBuilderPage from "@/app/(main)/workflow-builder/page";

// 独立于页面实现的后端 NodeType 清单，避免用同一份常量自证。
const NODE_DEFINITIONS = [
  { type: "START", label: "开始" },
  { type: "END", label: "结束" },
  { type: "TASK", label: "任务占位" },
  { type: "AGENT", label: "Agent执行" },
  { type: "CONDITION", label: "条件判断" },
  { type: "PARALLEL", label: "并行" },
  { type: "MERGE", label: "汇聚" },
  { type: "WAIT", label: "等待" },
  { type: "APPROVAL", label: "人工审批" },
  { type: "HUMAN_INPUT", label: "人工输入" },
  { type: "SUB_WORKFLOW", label: "子工作流" },
  { type: "HTTP", label: "HTTP请求" },
  { type: "TOOL", label: "工具调用" },
  { type: "TRANSFORM", label: "数据转换" },
] as const;

const META = {
  id: "wf-saved", name: "测试工作流", version: 1, source: "manual", status: "DRAFT",
  score: null, run_count: 0, success_count: 0, failure_count: 0,
  reuse_key: "", previous_workflow_id: "", feedback: [], nodes: [], edges: [],
};

function mountPage() {
  return render(createElement(WorkflowBuilderPage));
}

async function addNode(user: ReturnType<typeof userEvent.setup>, type: string) {
  const definition = NODE_DEFINITIONS.find((item) => item.type === type);
  if (!definition) throw new Error(`unknown test node type ${type}`);
  await user.click(screen.getByRole("button", { name: `添加${definition.label}节点` }));
}

async function savePayload(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(apiMock.post).toHaveBeenCalled());
  const [url, body] = apiMock.post.mock.calls[0];
  return { url: url as string, body: body as {
    nodes: Array<Record<string, any>>;
    edges: unknown[];
    inputs?: Array<Record<string, unknown>>;
  } };
}

beforeEach(() => {
  vi.clearAllMocks();
  contextMock.companyId = "co-active";
  window.history.replaceState({}, "", "/workflow-builder");
  Object.defineProperty(HTMLElement.prototype, "setPointerCapture", {
    configurable: true, value: vi.fn(),
  });
  Object.defineProperty(HTMLElement.prototype, "releasePointerCapture", {
    configurable: true, value: vi.fn(),
  });
  Object.defineProperty(document, "elementFromPoint", {
    configurable: true, value: vi.fn(() => null),
  });
  apiMock.get.mockImplementation(async (url: string) => {
    if (url === "/companies") return contextMock.companies;
    if (url === "/agents") return [];
    if (url.startsWith("/workflows/")) return META;
    throw new Error(`unexpected GET ${url}`);
  });
  apiMock.post.mockResolvedValue({ id: "wf-saved", name: "测试工作流", version: 1 });
  apiMock.put.mockResolvedValue({ id: "wf-saved", name: "测试工作流", version: 2 });
});

describe("工作流编排 Stage B", () => {
  it("工具栏暴露后端全部 14 种节点", () => {
    mountPage();
    const toolbar = screen.getByRole("toolbar", { name: "添加节点" });
    expect(within(toolbar).getAllByRole("button").map((button) => button.getAttribute("aria-label")))
      .toEqual(NODE_DEFINITIONS.map((item) => `添加${item.label}节点`));
  });

  it("公司上下文未解析时禁用保存/运行，解析后只保存到激活公司", async () => {
    contextMock.companyId = undefined;
    const view = mountPage();
    const user = userEvent.setup();
    expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "▶ 运行" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole("status").textContent).toContain("正在确认公司");

    contextMock.companyId = null;
    view.rerender(createElement(WorkflowBuilderPage));
    expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole("status").textContent).toContain("未选择公司");

    contextMock.companyId = "co-active";
    view.rerender(createElement(WorkflowBuilderPage));
    expect(screen.getByRole("status").textContent).toContain("当前公司");
    expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(false);
    const { url } = await savePayload(user);
    expect(url).toBe("/workflows?company_id=co-active");
    // 公司名单由 useResource 注入；保存处理器本身不再额外请求 /companies。
    expect(apiMock.get.mock.calls.filter(([called]) => called === "/companies")).toHaveLength(0);
    expect(contextMock.companies[0].id).toBe("co-other");
  });

  it("保存后可运行；再次保存走 PUT，不新建重复流程", async () => {
    const view = mountPage();
    const user = userEvent.setup();
    await addNode(user, "START");
    await addNode(user, "END");
    await savePayload(user);
    const run = screen.getByRole("button", { name: "▶ 运行" }) as HTMLButtonElement;
    expect(run.disabled).toBe(false);
    contextMock.companyId = undefined;
    view.rerender(createElement(WorkflowBuilderPage));
    expect(run.disabled).toBe(true);
    expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(true);
    contextMock.companyId = "co-active";
    view.rerender(createElement(WorkflowBuilderPage));
    expect(run.disabled).toBe(false);
    apiMock.post.mockResolvedValueOnce({ execution_id: "ex-1" });
    await user.click(run);
    await waitFor(() => expect(apiMock.post).toHaveBeenCalledWith(
      "/workflows/wf-saved/run", { inputs: {} },
    ));

    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(apiMock.put).toHaveBeenCalledWith(
      "/workflows/wf-saved", expect.objectContaining({ name: "新工作流" }),
    ));
  });

  it("TASK/SUB_WORKFLOW 明示非等待语义，并保存新增节点的配置字段", async () => {
    mountPage();
    const user = userEvent.setup();
    await addNode(user, "TASK");
    expect(screen.getByRole("note").textContent).toContain("不执行任务");
    fireEvent.change(screen.getByLabelText("占位备注"), { target: { value: "结构说明" } });

    await addNode(user, "HUMAN_INPUT");
    fireEvent.change(screen.getByLabelText("输入提示"), { target: { value: "请确认范围" } });
    await addNode(user, "SUB_WORKFLOW");
    expect(screen.getByRole("note").textContent).toContain("不等待子流程完成");
    fireEvent.change(screen.getByRole("textbox", { name: /子工作流 ID/ }), { target: { value: "wf-child" } });

    await addNode(user, "HTTP");
    fireEvent.change(screen.getByRole("textbox", { name: /请求 URL/ }), {
      target: { value: "https://api.example.com/run" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: "方法" }), { target: { value: "POST" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: /超时/ }), { target: { value: "30" } });
    fireEvent.change(screen.getByLabelText("非敏感请求头 JSON"), {
      target: { value: '{"Accept":"application/json"}' },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "请求体" }), {
      target: { value: '{"query":"hello"}' },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /auth_secret_ref/ }), {
      target: { value: "example_service_key" },
    });

    await addNode(user, "TOOL");
    fireEvent.change(screen.getByLabelText("工具名"), { target: { value: "web.search" } });
    fireEvent.change(screen.getByLabelText("工具参数 arguments JSON"), {
      target: { value: '{"query":"hello"}' },
    });
    await addNode(user, "TRANSFORM");
    fireEvent.change(screen.getByLabelText("输出映射 set JSON"), {
      target: { value: '{"summary":"{{agent.output}}"}' },
    });

    const { body } = await savePayload(user);
    const config = (type: string) => body.nodes.find((node) => node.node_type === type)?.config;
    expect(config("TASK")).toEqual({ note: "结构说明" });
    expect(config("HUMAN_INPUT")).toEqual({ prompt: "请确认范围" });
    expect(config("SUB_WORKFLOW")).toEqual({ workflow_id: "wf-child" });
    expect(config("HTTP")).toEqual(expect.objectContaining({
      url: "https://api.example.com/run", method: "POST", timeout_seconds: 30,
      headers: { Accept: "application/json" }, body: '{"query":"hello"}',
      auth_secret_ref: "example_service_key",
    }));
    expect(config("TOOL")).toEqual({ tool_name: "web.search", arguments: { query: "hello" } });
    expect(config("TRANSFORM")).toEqual({ set: { summary: "{{agent.output}}" } });
  });

  it("拒绝无效 JSON 和明文 HTTP 凭据头，不静默保存", async () => {
    mountPage();
    const user = userEvent.setup();
    await addNode(user, "HTTP");
    const headers = screen.getByLabelText("非敏感请求头 JSON");
    const save = screen.getByRole("button", { name: "保存" }) as HTMLButtonElement;

    for (const invalid of ["{", "[]", "null", "1", '"text"']) {
      fireEvent.change(headers, { target: { value: invalid } });
      expect(screen.getByRole("alert").textContent).toBeTruthy();
      expect(save.disabled).toBe(true);
    }
    for (const header of ["Authorization", " authorization ", "X-API-Key", "Cookie", "Proxy-Authorization"]) {
      fireEvent.change(headers, { target: { value: JSON.stringify({ [header]: "raw-token" }) } });
      expect(screen.getByRole("alert").textContent).toContain("auth_secret_ref");
      expect(save.disabled).toBe(true);
    }
    expect(apiMock.post).not.toHaveBeenCalled();

    fireEvent.change(headers, { target: { value: '{"Accept":"application/json"}' } });
    expect(save.disabled).toBe(false);
    const { body } = await savePayload(user);
    expect(body.nodes[0].config.headers).toEqual({ Accept: "application/json" });
    expect(JSON.stringify(body)).not.toContain("raw-token");
  });

  it("声明并严格转换运行入参，缺失或非法值不会启动", async () => {
    mountPage();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "＋添加入参" }));
    fireEvent.change(screen.getByLabelText("入参 1 名称"), { target: { value: "subject" } });
    fireEvent.change(screen.getByLabelText("入参 1 类型"), { target: { value: "text" } });
    await user.click(screen.getByRole("checkbox", { name: "运行时必填" }));

    await user.click(screen.getByRole("button", { name: "＋添加入参" }));
    fireEvent.change(screen.getByLabelText("入参 2 名称"), { target: { value: "round" } });
    fireEvent.change(screen.getByLabelText("入参 2 类型"), { target: { value: "number" } });
    fireEvent.change(screen.getByLabelText("入参 2 默认值"), { target: { value: "2" } });

    await user.click(screen.getByRole("button", { name: "＋添加入参" }));
    fireEvent.change(screen.getByLabelText("入参 3 名称"), { target: { value: "approved" } });
    fireEvent.change(screen.getByLabelText("入参 3 类型"), { target: { value: "boolean" } });

    await user.click(screen.getByRole("button", { name: "＋添加入参" }));
    fireEvent.change(screen.getByLabelText("入参 4 名称"), { target: { value: "context" } });
    fireEvent.change(screen.getByLabelText("入参 4 类型"), { target: { value: "json" } });

    const { body } = await savePayload(user);
    expect(body.inputs).toEqual([
      { name: "subject", type: "text", required: true },
      { name: "round", type: "number", required: false, default: 2 },
      { name: "approved", type: "boolean", required: false },
      { name: "context", type: "json", required: false },
    ]);

    await user.click(screen.getByRole("button", { name: "▶ 运行" }));
    expect(screen.getByRole("alert").textContent).toContain("请填写运行入参「subject」");
    expect(apiMock.post).toHaveBeenCalledTimes(1);

    fireEvent.change(screen.getByLabelText("运行入参 subject"), { target: { value: "发布需求" } });
    fireEvent.change(screen.getByLabelText("运行入参 context"), { target: { value: "{" } });
    await user.click(screen.getByRole("button", { name: "▶ 运行" }));
    expect(screen.getByRole("alert").textContent).toContain("不是合法 JSON");
    expect(apiMock.post).toHaveBeenCalledTimes(1);

    fireEvent.change(screen.getByLabelText("运行入参 round"), { target: { value: "3.5" } });
    fireEvent.change(screen.getByLabelText("运行入参 approved"), { target: { value: "false" } });
    fireEvent.change(screen.getByLabelText("运行入参 context"), { target: { value: '{"topic":"AI"}' } });
    apiMock.post.mockResolvedValueOnce({ execution_id: "ex-inputs" });
    await user.click(screen.getByRole("button", { name: "▶ 运行" }));
    await waitFor(() => expect(apiMock.post).toHaveBeenLastCalledWith("/workflows/wf-saved/run", {
      inputs: { subject: "发布需求", round: 3.5, approved: false, context: { topic: "AI" } },
    }));
  });

  it("拒绝重复入参名和错误类型的默认值", async () => {
    mountPage();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "＋添加入参" }));
    fireEvent.change(screen.getByLabelText("入参 1 名称"), { target: { value: "count" } });
    fireEvent.change(screen.getByLabelText("入参 1 类型"), { target: { value: "number" } });
    fireEvent.change(screen.getByLabelText("入参 1 默认值"), { target: { value: "true" } });
    expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole("alert").textContent).toContain("默认值必须是有限数字");

    fireEvent.change(screen.getByLabelText("入参 1 默认值"), { target: { value: "{" } });
    expect(screen.getByRole("alert").textContent).toContain("默认值必须是合法 JSON");
    fireEvent.change(screen.getByLabelText("入参 1 默认值"), { target: { value: "1" } });
    await user.click(screen.getByRole("button", { name: "＋添加入参" }));
    fireEvent.change(screen.getByLabelText("入参 2 名称"), { target: { value: "count" } });
    expect(screen.getByRole("alert").textContent).toContain("输入名称不能重复");
    expect(apiMock.post).not.toHaveBeenCalled();
  });

  it("载入已有流程时保留项目归属和入参声明，更新不再清空关联", async () => {
    window.history.replaceState({}, "", "/workflow-builder?open=wf-saved");
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/companies") return contextMock.companies;
      if (url === "/agents") return [];
      if (url === "/workflows/wf-saved") return {
        ...META, description: "已有说明", project_id: "project-a", company_id: "co-active",
        inputs: [{ name: "subject", type: "text", required: true, default: "需求" }],
      };
      throw new Error(`unexpected GET ${url}`);
    });
    const view = mountPage();
    const user = userEvent.setup();
    expect(await screen.findByLabelText("运行入参 subject")).toBeTruthy();
    contextMock.companyId = "co-other";
    view.rerender(createElement(WorkflowBuilderPage));
    expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "▶ 运行" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole("status").textContent).toContain("属于其他公司");
    contextMock.companyId = "co-active";
    view.rerender(createElement(WorkflowBuilderPage));
    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(apiMock.put).toHaveBeenCalledWith("/workflows/wf-saved", expect.objectContaining({
      description: "已有说明", project_id: "project-a",
      inputs: [{ name: "subject", type: "text", required: true, default: "需求" }],
    })));
  });

  it("滚动画布后触摸拖动仍保持鼠标位移，连线预览使用内容坐标", async () => {
    mountPage();
    const user = userEvent.setup();
    await addNode(user, "START");
    await addNode(user, "END");
    const start = screen.getByRole("group", { name: /^开始，START/ }) as HTMLElement;
    const end = screen.getByRole("group", { name: /^结束，END/ }) as HTMLElement;
    const canvas = start.parentElement as HTMLElement;
    Object.defineProperty(canvas, "clientLeft", { configurable: true, value: 2 });
    Object.defineProperty(canvas, "clientTop", { configurable: true, value: 3 });
    canvas.scrollLeft = 80;
    canvas.scrollTop = 30;
    const initialX = Number.parseFloat(start.style.left);
    const initialY = Number.parseFloat(start.style.top);

    fireEvent.pointerDown(start, {
      pointerId: 7, pointerType: "touch", isPrimary: true, button: 0, clientX: 100, clientY: 100,
    });
    fireEvent.pointerMove(canvas, {
      pointerId: 7, pointerType: "touch", isPrimary: true, clientX: 140, clientY: 130,
    });
    expect(Number.parseFloat(start.style.left)).toBeCloseTo(initialX + 40, 3);
    expect(Number.parseFloat(start.style.top)).toBeCloseTo(initialY + 30, 3);
    fireEvent.pointerUp(canvas, { pointerId: 7, pointerType: "touch", isPrimary: true });

    fireEvent.pointerDown(start, {
      pointerId: 8, pointerType: "mouse", isPrimary: true, button: 0, clientX: 100, clientY: 100,
    });
    fireEvent.pointerMove(canvas, {
      pointerId: 8, pointerType: "mouse", isPrimary: true, clientX: 110, clientY: 120,
    });
    expect(Number.parseFloat(start.style.left)).toBeCloseTo(initialX + 50, 3);
    expect(Number.parseFloat(start.style.top)).toBeCloseTo(initialY + 50, 3);
    fireEvent.pointerUp(canvas, { pointerId: 8, pointerType: "mouse", isPrimary: true });

    const port = screen.getByRole("button", { name: "从开始开始连线" });
    fireEvent.pointerDown(port, {
      pointerId: 9, pointerType: "touch", isPrimary: true, button: 0, clientX: 150, clientY: 100,
    });
    fireEvent.pointerMove(canvas, {
      pointerId: 9, pointerType: "touch", isPrimary: true, clientX: 200, clientY: 180,
    });
    const preview = canvas.querySelector("line[stroke-dasharray]") as SVGLineElement;
    expect(preview.getAttribute("x2")).toBe("278");
    expect(preview.getAttribute("y2")).toBe("207");
    vi.mocked(document.elementFromPoint).mockReturnValue(end);
    fireEvent.pointerUp(canvas, {
      pointerId: 9, pointerType: "touch", isPrimary: true, clientX: 200, clientY: 180,
    });
    expect(canvas.querySelector("line[stroke-dasharray]")).toBeNull();

    const { body } = await savePayload(user);
    expect(body.edges).toEqual([{
      from_node: start.dataset.nodeKey,
      to_node: end.dataset.nodeKey,
    }]);
  });
});
