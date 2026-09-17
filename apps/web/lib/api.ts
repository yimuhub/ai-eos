// AI-EOS API client（多公司版：全局公司上下文自动注入）
const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

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

/** 把路径中的 company_id=... 替换为当前激活公司（有激活值才替换） */
function applyCompanyScope(path: string): string {
  const cid = getActiveCompanyId();
  if (!cid) return path;
  if (path.includes("company_id=")) {
    return path.replace(/company_id=[^&]*/, `company_id=${cid}`);
  }
  // 无 company_id 参数的接口：自动追加（/companies、/auth 等除外）
  const skip = ["/companies", "/auth", "/upload"];
  if (skip.some((p) => path.startsWith(p))) return path;
  return path + (path.includes("?") ? "&" : "?") + `company_id=${cid}`;
}

async function request(path: string, options: RequestInit = {}) {
  const token = getToken();
  // /companies 列表本身不替换
  const scoped = path.startsWith("/companies") && !path.includes("company_id=")
    ? path : applyCompanyScope(path);
  const res = await fetch(`${API}${scoped}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options.headers,
    },
  });
  if (res.status === 401) {
    clearToken();
    window.location.href = "/login";
    throw new Error("Unauthorized");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  const data = res.status === 204 ? null : await res.json();
  // 公司列表：激活公司自动排第一 → 全站 cs[0] 写法天然跟随切换
  if (Array.isArray(data) && data.length > 1 && data[0]?.owner_id &&
      typeof data[0]?.name === "string") {
    const active = getActiveCompanyId();
    if (active) {
      const idx = data.findIndex((c: any) => c.id === active);
      if (idx > 0) {
        const [cur] = data.splice(idx, 1);
        data.unshift(cur);
      }
    }
  }
  return data;
}

/** 公司列表（激活公司自动排第一，兼容全站 cs[0] 写法） */
export async function getCompanies(): Promise<any[]> {
  const list: any[] = await request("/companies");
  const active = getActiveCompanyId();
  if (!active) return list;
  const idx = list.findIndex((c) => c.id === active);
  if (idx <= 0) return list;
  const [cur] = list.splice(idx, 1);
  return [cur, ...list];
}

export const api = {
  get: (p: string) => request(p),
  post: (p: string, body?: unknown) =>
    request(p, { method: "POST", body: body ? JSON.stringify(body) : undefined }),
  put: (p: string, body?: unknown) =>
    request(p, { method: "PUT", body: body ? JSON.stringify(body) : undefined }),
  patch: (p: string, body: unknown) =>
    request(p, { method: "PATCH", body: JSON.stringify(body) }),
  del: (p: string) => request(p, { method: "DELETE" }),
};
