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

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

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
const COMPANY_SCOPE_SKIP = ["/companies", "/auth", "/upload", "/health"];

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
};

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
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  if (timeoutMs > 0 && !options.signal) {
    timer = setTimeout(() => controller.abort(), timeoutMs);
  }

  let res: Response;
  try {
    // multipart 上传：FormData 必须由浏览器自己生成
    // `Content-Type: multipart/form-data; boundary=...`，
    // 手工写死成 application/json 会让后端完全解析不出字段。
    // 改造前各页面的上传都是裸 fetch 自己拼 header——正是因为 api 层做不到这件事。
    const isForm = typeof FormData !== "undefined" && options.body instanceof FormData;
    res = await fetch(`${API}${scoped}`, {
      ...options,
      signal: options.signal ?? controller.signal,
      headers: {
        ...(isForm ? {} : { "Content-Type": "application/json" }),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...options.headers,
      },
    });
  } catch (e: any) {
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
    broadcastError(err);
    throw err;
  } finally {
    if (timer) clearTimeout(timer);
  }

  if (res.status === 401) {
    // 先尝试用 refresh_token 续期并重放原请求一次；
    // 续期失败或根本没有 refresh_token 时，才走原有的"登录已过期"逻辑。
    if (!_isRetry) {
      const replayed = await tryRefreshAndReplay(path, options, config);
      if (replayed) return replayed;
    }
    clearToken();
    const err = new ApiError("登录已过期，请重新登录", 401, scoped);
    broadcastError(err);
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
    broadcastError(err);
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
