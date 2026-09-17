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

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("aieos_token");
}

export function setToken(token: string) {
  localStorage.setItem("aieos_token", token);
}

export function clearToken() {
  localStorage.removeItem("aieos_token");
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

async function request<T = any>(
  path: string,
  options: RequestInit = {},
  config: RequestOptions = {},
): Promise<T> {
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
    res = await fetch(`${API}${scoped}`, {
      ...options,
      signal: options.signal ?? controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...options.headers,
      },
    });
  } catch (e: any) {
    // 区分"超时"与"网络不通"：两者给用户的下一步动作完全不同
    const aborted = e?.name === "AbortError";
    const err = new ApiError(
      aborted
        ? `请求超时（${Math.round(timeoutMs / 1000)} 秒无响应）。后端可能仍在处理，请稍后刷新查看结果。`
        : "无法连接后端服务，请确认服务已启动。",
      0,
      scoped,
    );
    broadcastError(err);
    throw err;
  } finally {
    if (timer) clearTimeout(timer);
  }

  if (res.status === 401) {
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
};

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
