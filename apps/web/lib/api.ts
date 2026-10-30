// AI-EOS API client（多公司版：全局公司上下文自动注入）
//
// ## 改造要点
// 1. **超时**：此前 fetch 无超时，后端卡住时页面会永久转圈，
//    用户只能刷新。现统一 180s 上限（LLM 类接口耗时较长，故给足），
//    可按调用覆盖。
// 2. **错误规范化**：FastAPI 校验失败返回的是 `detail: [{loc, msg}]` 数组，
//    旧实现直接 `new Error(body.detail)` → 界面显示 "[object Object]"。
//    现在把数组/对象统一转成可读中文串。
// 3. **可区分的错误类型**：ApiError 带 status，页面可据此给出不同引导
//    （401 去登录、403 说清缺什么权限、404 说资源不存在、0 说网络/超时）。
// 4. **全局错误广播**：失败时派发 `aieos-api-error` 事件，
//    由全局 Toast 统一提示——避免"点了没反应"。
// 5. **类型化**：api.get<T>() 支持泛型，逐步替代 any。
// 6. **可取消 + 认证端点豁免跳转**：RequestOptions 支持外部 `signal`（与内部
//    超时组合，页面卸载/轮询竞态时可 abort 在途请求）；`/auth/login`、
//    `/auth/register`、`/auth/refresh` 的 401 只抛错给调用方就地内联提示，
//    不再触发全局"登录已过期"跳转（此前会冲掉登录页的错误渲染、丢失输入）。
//    `silent: true` 可关闭全局错误广播（更新检查等静默通道）。

/**
 * 后端基地址。**运行时**推导，不再编译期写死。
 *
 * 背景（v5.0.1 根治）：此前是 `process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"`
 * 的编译期常量——浏览器在**任何设备**打开页面时，`localhost` 都指向"该设备自己"，
 * 用局域网 IP / 手机 / 另一台电脑访问前端时，fetch 指向本机的 :8000（无服务）
 * → 网络层失败 → 报「无法连接后端服务」。
 *
 * 推导规则：
 * 1. 显式 `NEXT_PUBLIC_API_URL`（生产/特殊部署）→ 原样使用
 * 2. 浏览器环境 → 跟随**当前页面的 host**（你从哪台机器的 IP 打开前端，
 *    服务就跑在那台机器上）：
 *    - 便携版前端 :8000（后端 :8001）
 *    - 开发版前端 :3000（后端 :8000）
 * 3. SSR/构建期 → 回退 localhost:8000（仅 Next.js 服务端渲染用，不发请求）
 */
function resolveApiBase(): string {
  const envUrl = process.env.NEXT_PUBLIC_API_URL;
  if (envUrl) return envUrl;
  if (typeof window === "undefined") return "http://localhost:8000";
  const host = window.location.hostname || "localhost";
  const port = window.location.port === "8000" ? "8001" : "8000";
  return `${window.location.protocol}//${host}:${port}`;
}

const API = resolveApiBase();

/**
 * 后端基地址。页面需要"直接指向后端的 URL"时用它——
 * 例如下载链接、图片 src。这些没法走 fetch 包装，但**同样不能写死**：
 * 之前有页面硬编码 `http://localhost:8000`，部署到别处就成了坏链。
 */
export const API_BASE = API;

/** 默认超时：LLM 生成类接口可能需要 1-3 分钟，故给足上限但不再无限等待 */
const DEFAULT_TIMEOUT_MS = 180_000;

export class ApiError extends Error {
  status: number;
  detail: unknown;
  path: string;

  constructor(message: string, status: number, path: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.path = path;
    this.detail = detail;
  }

  /** 401：登录态失效 */
  get isAuth(): boolean { return this.status === 401; }
  /** 403：已登录但无权限（如本机操作能力未授权） */
  get isPermission(): boolean { return this.status === 403; }
  /** 404：资源不存在（也可能是不属于当前公司） */
  get isNotFound(): boolean { return this.status === 404; }
  /** 网络层失败或超时 */
  get isNetwork(): boolean { return this.status === 0; }
}

/** 把后端各种形状的错误体转成一句人话 */
export function normalizeErrorDetail(detail: unknown): string {
  if (detail == null || detail === "") return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item: any) => {
        if (typeof item === "string") return item;
        const loc = Array.isArray(item?.loc)
          ? item.loc.filter((x: unknown) => x !== "body" && x !== "query").join(".")
          : "";
        const msg = item?.msg || item?.message || JSON.stringify(item);
        return loc ? `${loc}：${msg}` : String(msg);
      })
      .join("；");
  }
  if (typeof detail === "object") {
    const obj = detail as any;
    if (typeof obj.message === "string") return obj.message;
    try { return JSON.stringify(detail); } catch { return String(detail); }
  }
  return String(detail);
}

/** 把任意异常转成一句可展示的中文，失败时给出兜底文案。
 *
 * 此前每个页面各自复制了一份同样的 `describe()`，改一处漏一处；
 * 统一到这里，页面只负责给出业务语境的兜底词（如"审批失败"）。
 */
export function describeError(e: unknown, fallback = "操作失败"): string {
  if (e instanceof ApiError) return e.message || fallback;
  if (e instanceof Error) return e.message || fallback;
  if (typeof e === "string" && e) return e;
  return fallback;
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("aieos_token");
}

export function setToken(token: string) {
  localStorage.setItem("aieos_token", token);
}

/** refresh_token 的本地存储 key（Worker C 新增 POST /auth/refresh 约定） */
const REFRESH_KEY = "aieos_refresh_token";

export function getRefreshToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(REFRESH_KEY);
}

export function setRefreshToken(token: string) {
  localStorage.setItem(REFRESH_KEY, token);
}

export function clearRefreshToken() {
  localStorage.removeItem(REFRESH_KEY);
}

export function clearToken() {
  localStorage.removeItem("aieos_token");
  clearRefreshToken();
}

/* ---------- 多公司上下文 ---------- */
export function getActiveCompanyId(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("aieos_active_cid");
}

export function setActiveCompanyId(cid: string) {
  localStorage.setItem("aieos_active_cid", cid);
  window.dispatchEvent(new CustomEvent("aieos-company-changed", { detail: cid }));
}

/** 不需要自动追加 company_id 的路径前缀 */
const COMPANY_SCOPE_SKIP = ["/companies", "/auth", "/upload", "/health", "/setup"];

/** 把路径中的 company_id=... 替换为当前激活公司（有激活值才替换） */
function applyCompanyScope(path: string): string {
  const cid = getActiveCompanyId();
  if (!cid) return path;
  if (path.includes("company_id=")) {
    // 占位符（如 company_id=x / company_id=）也一并纠正为真实公司
    return path.replace(/company_id=[^&]*/, `company_id=${cid}`);
  }
  if (COMPANY_SCOPE_SKIP.some((p) => path.startsWith(p))) return path;
  return path + (path.includes("?") ? "&" : "?") + `company_id=${cid}`;
}

export type RequestOptions = {
  /** 超时毫秒数，0 表示不限时（默认 180s） */
  timeoutMs?: number;
  /**
   * 外部取消信号：组件卸载 / 轮询竞态时用它 abort 在途请求。
   * 与内部超时正确组合——两者任一触发都能终止请求，且被调用方取消时
   * **不会**派发全局错误广播（取消不是故障）。
   */
  signal?: AbortSignal;
  /** 失败时不派发 `aieos-api-error` 全局提示（由调用方自己静默处理或就地提示） */
  silent?: boolean;
  /** 透传给 fetch 的缓存策略（如更新检查通道需要 no-store） */
  cache?: RequestCache;
  /**
   * 401 时不按"登录已过期"处理：不清 token、不整页跳 /login、不广播。
   *
   * 首次安装引导（/setup）专用：安装前系统里还没有任何账号，也就没有登录态，
   * 此时读 `/org-presets` 之类需要鉴权的接口必然 401——那是**预期状态**，
   * 若走默认的"过期 → window.location = /login"，会把用户正在填写的安装表单冲掉，
   * 再被登录页的 needs_setup 判定送回 /setup，形成来回跳转。
   * 调用方拿到 401 后自行降级即可。
   */
  noAuthRedirect?: boolean;
};

/**
 * 认证入口端点：它们的 401 含义是"凭据不对"，不是"登录已过期"。
 *
 * 此前登录页自己就用 `api.post("/auth/login")`，密码打错返回 401 后
 * 被全局"过期 → window.location 跳转"拦下：错误提示还没渲染整页就被刷走，
 * 用户输入的邮箱/密码全丢。这几个端点必须豁免全局跳转，
 * 让调用方（登录页）自己处理错误并就地显示内联提示。
 */
const AUTH_ENTRY_PATHS = ["/auth/login", "/auth/register", "/auth/refresh"];
function isAuthEntryPath(path: string): boolean {
  return AUTH_ENTRY_PATHS.some((p) => path.startsWith(p));
}

/** 全局错误广播：由 Toast 组件监听，统一提示失败原因 */
function broadcastError(err: ApiError) {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent("aieos-api-error", {
    detail: { message: err.message, status: err.status, path: err.path },
  }));
}

/**
 * 401 时尝试用 refresh_token 续期并重放原请求。
 *
 * Worker C 新增 `POST /auth/refresh`（请求体 {refresh_token}，
 * 响应约定 {access_token, refresh_token}）。
 *
 * - 成功：换上新 token，重放原请求一次（调用处传 _isRetry=true，
 *   新 token 若再 401 就直接走过期逻辑，不循环）。
 * - 失败/无 refresh_token：返回 null，调用处走原有的"登录已过期"逻辑。
 * - 并发 401：模块级 promise 去重，同一时刻只换一次。
 * - /auth/* 自身 401：不再套娃。
 */
let refreshPromise: Promise<boolean> | null = null;

async function tryRefreshAndReplay(
  path: string,
  options: RequestInit,
  config: RequestOptions,
): Promise<Response | null> {
  if (typeof window === "undefined") return null;
  if (path.startsWith("/auth/")) return null;
  const rt = getRefreshToken();
  if (!rt) return null;
  if (!refreshPromise) {
    refreshPromise = (async () => {
      try {
        const r = await fetch(`${API}/auth/refresh`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: rt }),
        });
        if (!r.ok) return false;
        const body = await r.json().catch(() => ({} as any));
        if (!body?.access_token) return false;
        setToken(body.access_token);
        if (body.refresh_token) setRefreshToken(body.refresh_token);
        return true;
      } catch {
        return false;
      } finally {
        refreshPromise = null;
      }
    })();
  }
  const ok = await refreshPromise;
  if (!ok) return null;
  return raw(path, options, config, true);
}

/**
 * 底层：发出请求并返回原始 Response（已完成鉴权/超时/错误规范化）。
 * `request` 在它之上做 JSON 解析；`fetchText`/`fetchBlobURL` 需要原始流。
 */
async function raw(
  path: string,
  options: RequestInit = {},
  config: RequestOptions = {},
  _isRetry = false,
): Promise<Response> {
  const token = getToken();
  // /companies 列表本身不替换
  const scoped =
    path.startsWith("/companies") && !path.includes("company_id=")
      ? path
      : applyCompanyScope(path);

  const timeoutMs = config.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  // 外部取消信号（config.signal 或 RequestInit 自带的 signal）与内部超时组合：
  // 内部始终用自己的 AbortController，外部信号"联动"它——
  // 外部信号已 aborted 或稍后 abort 时，同步 abort 内部 controller。
  // 超时逻辑不受影响：外部信号缺席时行为与旧版一致。
  const externalSignal = config.signal ?? options.signal;
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  if (timeoutMs > 0) {
    timer = setTimeout(() => controller.abort(), timeoutMs);
  }
  if (externalSignal) {
    if (externalSignal.aborted) controller.abort();
    else externalSignal.addEventListener("abort", () => controller.abort(), { once: true });
  }

  let res: Response;
  try {
    // multipart 上传：FormData 必须由浏览器自己生成
    // `Content-Type: multipart/form-data; boundary=...`，
    // 手工写死成 application/json 会让后端完全解析不出字段。
    // 改造前各页面的上传都是裸 fetch 自己拼 header——正是因为 api 层做不到这件事。
    const isForm = typeof FormData !== "undefined" && options.body instanceof FormData;
    res = await fetch(`${API}${scoped}`, {
      ...(config.cache ? { cache: config.cache } : {}),
      ...options,
      signal: controller.signal,
      headers: {
        ...(isForm ? {} : { "Content-Type": "application/json" }),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...options.headers,
      },
    });
  } catch (e: any) {
    // 调用方主动取消（组件卸载 / 轮询序列号守卫换新信号）：
    // 这是预期流程而非故障——不广播、错误信息也说清是"取消"，避免误报超时。
    if (externalSignal?.aborted) {
      throw new ApiError("请求已取消", 0, scoped, "cancelled");
    }
    // 区分"超时"与"网络不通"：两者给用户的下一步动作完全不同。
    // 网络层失败既可能是后端没起，也可能是跨域（CORS）被浏览器拦截——
    // 把请求目标地址带上，用户可对照 DevTools Network 判断。
    const aborted = e?.name === "AbortError";
    const netMsg = `无法连接后端服务（${API}）。若后端确实已启动，多为跨域（CORS）被浏览器拦截，请看浏览器 DevTools → Network 里的 CORS/预检请求，并确认访问前端的地址与后端 CORS 白名单一致。`;
    const err = new ApiError(
      aborted
        ? `请求超时（${Math.round(timeoutMs / 1000)} 秒无响应）。后端可能仍在处理，请稍后刷新查看结果。`
        : netMsg,
      0,
      scoped,
    );
    if (!config.silent) broadcastError(err);
    throw err;
  } finally {
    if (timer) clearTimeout(timer);
  }

  if (res.status === 401) {
    // 先尝试用 refresh_token 续期并重放原请求一次；
    // 续期失败或根本没有 refresh_token 时，才走"登录已过期"逻辑。
    if (!_isRetry) {
      const replayed = await tryRefreshAndReplay(path, options, config);
      if (replayed) return replayed;
    }
    if (config.noAuthRedirect) {
      // 首次安装引导：安装前没有账号，401 是预期结果。就地抛错，由调用方降级展示。
      const body = await res.json().catch(() => ({} as any));
      throw new ApiError(
        normalizeErrorDetail(body?.detail) || "需要登录后才能读取该数据",
        401,
        scoped,
      );
    }
    if (isAuthEntryPath(path)) {
      // 登录/注册入口的 401 = 凭据不对：不清 token、不整页跳转、不全局广播，
      // 把后端 detail（如"用户名或密码错误"）规范化后交给调用方就地内联显示。
      // （此前这里无条件跳转 /login，恰好把登录页自己的失败渲染冲掉了。）
      const body = await res.json().catch(() => ({} as any));
      throw new ApiError(
        normalizeErrorDetail(body?.detail) || "账号或密码不正确",
        401,
        scoped,
      );
    }
    clearToken();
    const err = new ApiError("登录已过期，请重新登录", 401, scoped);
    if (!config.silent) broadcastError(err);
    // 其他页面遇到"登录已过期"仍照旧跳转——这个行为是对的，保持不变。
    if (typeof window !== "undefined") window.location.href = "/login";
    throw err;
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({} as any));
    const readable = normalizeErrorDetail(body?.detail);
    const fallback: Record<number, string> = {
      403: "没有权限执行该操作",
      404: "请求的资源不存在",
      405: "该操作不被服务端支持（接口方法不匹配）",
      409: "当前状态不允许该操作",
      422: "提交的参数不合法",
      500: "服务端处理失败，请查看后端日志",
    };
    const err = new ApiError(
      readable || fallback[res.status] || `请求失败（HTTP ${res.status}）`,
      res.status,
      scoped,
    );
    if (!config.silent) broadcastError(err);
    throw err;
  }

  return res;
}

async function request<T = any>(
  path: string,
  options: RequestInit = {},
  config: RequestOptions = {},
): Promise<T> {
  const res = await raw(path, options, config);

  const data = res.status === 204 ? null : await res.json();
  // 公司列表：激活公司自动排第一 → 全站 cs[0] 写法天然跟随切换
  if (
    Array.isArray(data) &&
    data.length > 1 &&
    data[0]?.owner_id &&
    typeof data[0]?.name === "string"
  ) {
    const active = getActiveCompanyId();
    if (active) {
      const idx = data.findIndex((c: any) => c.id === active);
      if (idx > 0) {
        const [cur] = data.splice(idx, 1);
        data.unshift(cur);
      }
    }
  }
  return data as T;
}

/** 公司列表（激活公司自动排第一，兼容全站 cs[0] 写法） */
export async function getCompanies<T = any>(): Promise<T[]> {
  const list: any[] = await request("/companies");
  const active = getActiveCompanyId();
  if (!active) return list;
  const idx = list.findIndex((c) => c.id === active);
  if (idx <= 0) return list;
  const [cur] = list.splice(idx, 1);
  return [cur, ...list];
}

export const api = {
  get: <T = any>(p: string, config?: RequestOptions) =>
    request<T>(p, {}, config),
  post: <T = any>(p: string, body?: unknown, config?: RequestOptions) =>
    request<T>(p, { method: "POST", body: body ? JSON.stringify(body) : undefined }, config),
  put: <T = any>(p: string, body?: unknown, config?: RequestOptions) =>
    request<T>(p, { method: "PUT", body: body ? JSON.stringify(body) : undefined }, config),
  patch: <T = any>(p: string, body?: unknown, config?: RequestOptions) =>
    request<T>(p, { method: "PATCH", body: body === undefined ? undefined : JSON.stringify(body) }, config),
  del: <T = any>(p: string, config?: RequestOptions) =>
    request<T>(p, { method: "DELETE" }, config),
  /** multipart 上传（FormData）。不能走 post —— JSON.stringify(FormData) 会得到 "{}" */
  upload: <T = any>(p: string, form: FormData, config?: RequestOptions) =>
    request<T>(p, { method: "POST", body: form }, config),
};

/**
 * 带鉴权的文本读取。
 *
 * 有些接口返回的是纯文本（Markdown/CSV/日志）而不是 JSON，
 * 直接 `api.get()` 会在 JSON.parse 处炸掉，所以各页面只好裸 fetch。
 */
export async function fetchText(path: string, config?: RequestOptions): Promise<string> {
  const res = await raw(path, {}, config);
  return res.text();
}

/** SSE 事件体；具体字段由各接口约定（聊天流：status / delta / done / error） */
export type SseEvent = Record<string, any>;

/**
 * 以 SSE（text/event-stream）方式 POST 并逐条回调事件。
 *
 * 走 `raw()` 复用鉴权、401 续期、错误规范化与取消信号，只多做"读流 + 拆帧"。
 * 默认 **不限时**：流式回复本身可能持续几分钟，用普通请求的超时会把它掐断；
 * 调用方用 `signal` 主动停止即可。
 */
export async function streamPost(
  path: string,
  body: unknown,
  opts: {
    signal?: AbortSignal;
    timeoutMs?: number;
    silent?: boolean;
    onEvent: (ev: SseEvent) => void;
  },
): Promise<void> {
  const res = await raw(
    path,
    { method: "POST", body: JSON.stringify(body), headers: { Accept: "text/event-stream" } },
    { signal: opts.signal, timeoutMs: opts.timeoutMs ?? 0, silent: opts.silent },
  );
  if (!res.body) throw new ApiError("服务端没有返回可读的流", 0, path);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let finished = false;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      // SSE 以空行分帧；一帧可能跨多个 chunk，也可能一个 chunk 含多帧
      let cut = buf.indexOf("\n\n");
      while (cut >= 0) {
        const frame = buf.slice(0, cut);
        buf = buf.slice(cut + 2);
        const data = frame
          .split("\n")
          .filter((l) => l.startsWith("data:"))
          .map((l) => l.slice(5).trim())
          .join("\n");
        if (data) {
          try {
            opts.onEvent(JSON.parse(data));
          } catch {
            // 非 JSON 帧（心跳/注释）直接忽略，不影响整条流
          }
        }
        cut = buf.indexOf("\n\n");
      }
    }
    finished = true;
  } finally {
    if (!finished) {
      // 中途取消/出错：显式取消底层流，别让连接挂着
      try { await reader.cancel(); } catch { /* 已关闭 */ }
    }
    reader.releaseLock();
  }
}

/**
 * 带鉴权的二进制读取，返回可直接用的 object URL。
 *
 * **为什么必须有它**：下载类接口同样要求 Bearer token，
 * 而 `<a href="...">` / `window.open` 不会带上这个 header
 * ——改造前项目里的下载链接全都是点了以后拿到 401 页面。
 * 正确做法是先取回 blob，再用本地 objectURL 触发下载。
 *
 * 返回的 URL 用完后请调用 `revokeObjectURL` 释放。
 */
export async function fetchBlobURL(path: string, config?: RequestOptions): Promise<string> {
  const res = await raw(path, {}, config);
  const blob = await res.blob();
  return URL.createObjectURL(blob);
}

/**
 * 把一段文本当作文件下载（日志导出、报告导出等）。
 * 同理不能 `window.open(接口地址)` —— 那也带不上 token。
 */
export function downloadText(filename: string, text: string, mime = "text/markdown;charset=utf-8") {
  downloadURL(URL.createObjectURL(new Blob([text], { type: mime })), filename);
}

/** 触发浏览器下载一个已取回的 blob（自动释放 objectURL，避免内存泄漏） */
export function downloadURL(url: string, filename: string) {
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/**
 * 免 try/catch 的调用包装：返回 {ok, data, error}。
 * 适合"失败了就把原因显示在页面上"的场景，避免每个页面重复写 try/catch
 * 又容易漏掉 catch 造成 unhandled rejection。
 */
export async function safeCall<T = any>(
  fn: () => Promise<T>,
): Promise<{ ok: true; data: T; error: null } | { ok: false; data: null; error: ApiError }> {
  try {
    return { ok: true, data: await fn(), error: null };
  } catch (e: any) {
    const err = e instanceof ApiError
      ? e
      : new ApiError(e?.message || String(e), -1, "", e);
    return { ok: false, data: null, error: err };
  }
}

/** 判断错误是否为"调用方主动取消"（config.signal abort）。
 * 取消不是故障：页面据此跳过失败提示，避免卸载/竞态时误报"加载失败"。 */
export function isCancelled(e: unknown): boolean {
  return e instanceof ApiError && e.detail === "cancelled";
}
