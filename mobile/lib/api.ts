/**
 * AI-EOS 移动端共享 API 客户端。
 * 桌面壳与未来移动 App 共用同一套后端 API：
 *   - /auth/login、/auth/register
 *   - /command/plan、/command/execute、/command/report/{ref}、/command/notify
 *   - /experts、/dashboard、/agent-chat …
 *
 * 服务地址由"老板视角 → 服务连接"页配置（SecureStore 持久化），
 * 默认指向本机便携后端 http://localhost:8001（可改为远程/自托管服务器）。
 */
const DEFAULT_BASE = "http://localhost:8001";
const DEFAULT_COMPANY = "dd6b3c57-4716-4cd7-92d6-3860c367efd3";

export type Plan = {
  path: "DIRECT" | "EXPERT" | "PROJECT";
  targets: { agent_id?: string; agent_name?: string; task?: string }[];
  team_goal?: string;
  phrase?: string;
  est_minutes?: number;
};

export class ApiError extends Error {
  status: number;
  constructor(msg: string, status: number) {
    super(msg);
    this.status = status;
  }
}

async function call(
  base: string,
  token: string,
  path: string,
  init?: RequestInit,
): Promise<any> {
  const res = await fetch(base + path, {
    method: init?.method || "GET",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers || {}),
    },
    body: init?.body,
  });
  const text = await res.text();
  let data: any = text;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {}
  if (!res.ok) {
    const detail = data?.detail || data?.error || res.statusText || "请求失败";
    const msg = typeof detail === "string" ? detail : JSON.stringify(detail);
    throw new ApiError(`${res.status} ${msg}`, res.status);
  }
  return data;
}

export class Api {
  constructor(private base: string, private token: string) {}

  // ---- 鉴权 ----
  static async login(base: string, identifier: string, password: string): Promise<{ token: string }> {
    const d = await call(base, "", "/auth/login", {
      method: "POST",
      body: JSON.stringify({ identifier, password }),
    });
    return d;
  }

  static async register(base: string, identifier: string, password: string): Promise<{ token: string }> {
    const d = await call(base, "", "/auth/register", {
      method: "POST",
      body: JSON.stringify({ identifier, password }),
    });
    return d;
  }

  // ---- 一句话指挥台 ----
  plan(phrase: string, companyId: string, attach?: string): Promise<any> {
    return call(this.base, this.token, `/command/plan?company_id=${companyId}`, {
      method: "POST",
      body: JSON.stringify({ phrase, attachment: attach || undefined }),
    }).then((r) => r.plan || r);
  }

  execute(plan: Plan, companyId: string): Promise<any> {
    return call(this.base, this.token, `/command/execute?company_id=${companyId}`, {
      method: "POST",
      body: JSON.stringify({ plan }),
    });
  }

  report(ref: string, companyId: string): Promise<any> {
    return call(this.base, this.token, `/command/report/${ref}?company_id=${companyId}&as_json=1`);
  }

  notify(ref: string, companyId: string, webhook: string): Promise<any> {
    return call(this.base, this.token, "/command/notify", {
      method: "POST",
      body: JSON.stringify({ ref, company_id: companyId, webhook_url: webhook }),
    });
  }

  // ---- 老板视角 KPI ----
  dashboard(companyId: string): Promise<any> {
    return call(this.base, this.token, `/dashboard?company_id=${companyId}`);
  }

  // ---- 专家库 ----
  experts(query?: string, companyId?: string): Promise<any> {
    const q = new URLSearchParams();
    if (companyId) q.set("company_id", companyId);
    if (query) q.set("q", query);
    return call(this.base, this.token, `/experts?${q.toString()}`);
  }
}

export const defaults = () => ({ base: DEFAULT_BASE, companyId: DEFAULT_COMPANY });
