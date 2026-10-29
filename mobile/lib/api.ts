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
  let res: Response;
  try {
    res = await fetch(base + path, {
      method: init?.method || "GET",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(init?.headers || {}),
      },
      body: init?.body,
    });
  } catch (e: any) {
    // fetch 自身抛错 = 服务连不上（地址错/端口未开/网络不通），
    // 与"接口不存在"必须区分，否则用户会误以为客户端坏了。
    throw new ApiError(
      `无法连接服务（请检查服务地址与网络）：${e?.message || base}`,
      0,
    );
  }
  const text = await res.text();
  let data: any = text;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {}
  if (!res.ok) {
    // 404 单独文案：多半是客户端落后于后端路由（例如旧版把 company_id
    // 当 query 传给只接受路径参数的接口），升级客户端通常即可解决。
    if (res.status === 404) {
      const route = path.split("?")[0];
      throw new ApiError(
        `接口不存在（客户端版本过旧，请升级）：${init?.method || "GET"} ${route}`,
        404,
      );
    }
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
  // 后端路由是 GET /dashboard/{company_id}（路径参数），不是 ?company_id=。
  dashboard(companyId: string): Promise<any> {
    return call(this.base, this.token, `/dashboard/${encodeURIComponent(companyId)}`);
  }

  // ---- 签字门：待审批队列 + 通过/驳回 ----
  // 后端：GET  /intervention/requests?company_id=&status=PENDING
  //       POST /intervention/requests/{request_id}/decide {decision: approve|reject}
  pendingApprovals(companyId: string): Promise<any[]> {
    return call(
      this.base,
      this.token,
      `/intervention/requests?company_id=${encodeURIComponent(companyId)}&status=PENDING`,
    );
  }

  decideApproval(
    requestId: string,
    decision: "approve" | "reject",
    note?: string,
  ): Promise<any> {
    return call(
      this.base,
      this.token,
      `/intervention/requests/${encodeURIComponent(requestId)}/decide`,
      {
        method: "POST",
        body: JSON.stringify({ decision, ...(note ? { note } : {}) }),
      },
    );
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
