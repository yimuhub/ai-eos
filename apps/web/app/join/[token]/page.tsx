"use client";

// 接受成员邀请（公开页，不在登录守卫内）。
//
// 被邀请人点开链接时通常还没账号：先给出「哪家公司 / 什么角色」的预览，
// 再引导登录或注册。登录页支持 ?next= 回跳，回来后再点「接受邀请」。

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ApiError, api, describeError, getToken, setActiveCompanyId } from "@/lib/api";

type Preview = {
  valid: boolean;
  state: "valid" | "used" | "revoked" | "expired";
  reason: string | null;
  company_name: string | null;
  role: string;
  department_name: string | null;
  invited_by: string | null;
  expires_at: string;
};

const ROLE_LABEL: Record<string, string> = {
  company_admin: "管理员",
  member: "成员",
  viewer: "只读成员",
};

export default function JoinPage({ params }: { params: { token: string } }) {
  const router = useRouter();
  const token = params?.token || "";
  const [preview, setPreview] = useState<Preview | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loggedIn, setLoggedIn] = useState(false);

  useEffect(() => {
    setLoggedIn(!!getToken());
    let alive = true;
    (async () => {
      try {
        const p = await api.get<Preview>(`/invites/${encodeURIComponent(token)}`,
                                         { silent: true });
        if (alive) setPreview(p);
      } catch (e) {
        if (alive) setError(describeError(e, "邀请链接无效"));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, [token]);

  async function accept() {
    setBusy(true);
    setError("");
    try {
      const r = await api.post<{ company_id: string; company_name: string }>(
        `/invites/${encodeURIComponent(token)}/accept`, undefined,
        // 401 不整页跳走：本页要把"登录后可加入"的入口留在原地
        { noAuthRedirect: true, silent: true });
      setActiveCompanyId(r.company_id);
      router.replace("/workspace");
    } catch (e) {
      const err = e instanceof ApiError ? e : null;
      if (err?.isAuth) {
        setLoggedIn(false);
        setError("登录已过期，请重新登录后再接受邀请");
      } else {
        setError(describeError(e, "加入失败"));
      }
    } finally {
      setBusy(false);
    }
  }

  const next = `/join/${encodeURIComponent(token)}`;

  return (
    <div className="min-h-screen flex items-center justify-center p-6"
      style={{ background: "var(--bg)" }}>
      <div className="card w-full max-w-md space-y-4">
        <div className="flex items-center gap-3">
          <span className="w-10 h-10 rounded-xl flex items-center justify-center text-lg"
            style={{ background: "linear-gradient(135deg,#6366f1,#8b5cf6)" }}>✉</span>
          <div>
            <h1 className="text-base font-bold">加入团队邀请</h1>
            <p className="text-[11px]" style={{ color: "var(--muted)" }}>
              链接只能使用一次，加入后自动成为公司成员
            </p>
          </div>
        </div>

        {loading && (
          <p className="text-xs py-8 text-center" style={{ color: "var(--muted)" }}>
            正在读取邀请…
          </p>
        )}

        {!loading && error && !preview && (
          <div className="space-y-4">
            <p className="text-sm text-red-400/90">{error}</p>
            <Link href="/" className="btn-ghost text-xs inline-block">返回首页</Link>
          </div>
        )}

        {preview && (
          <div className="space-y-4">
            <div className="rounded-lg p-4 space-y-2"
              style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
              <p className="text-sm">
                <span style={{ color: "var(--muted)" }}>邀请你加入：</span>
                <strong>{preview.company_name || "未知公司"}</strong>
              </p>
              <p className="text-sm">
                <span style={{ color: "var(--muted)" }}>加入后的角色：</span>
                {ROLE_LABEL[preview.role] || preview.role}
              </p>
              {preview.department_name && (
                <p className="text-sm">
                  <span style={{ color: "var(--muted)" }}>部门：</span>
                  {preview.department_name}
                </p>
              )}
              {preview.invited_by && (
                <p className="text-xs" style={{ color: "var(--muted)" }}>
                  邀请人：{preview.invited_by}
                </p>
              )}
            </div>

            {!preview.valid ? (
              <p className="text-sm text-red-400/90">
                {preview.reason || "邀请链接已不可用"}
              </p>
            ) : loggedIn ? (
              <button onClick={accept} disabled={busy}
                className="w-full py-2.5 rounded-lg text-sm font-medium text-white disabled:opacity-50"
                style={{ background: "linear-gradient(135deg,#8b5cf6,#6366f1)" }}>
                {busy ? "正在加入…" : "接受邀请并加入"}
              </button>
            ) : (
              <div className="space-y-2">
                <Link href={`/login?next=${encodeURIComponent(next)}`}
                  className="block text-center w-full py-2.5 rounded-lg text-sm font-medium text-white"
                  style={{ background: "linear-gradient(135deg,#8b5cf6,#6366f1)" }}>
                  登录后加入
                </Link>
                <Link href={`/login?next=${encodeURIComponent(next)}&mode=register`}
                  className="block text-center text-xs hover:underline"
                  style={{ color: "var(--muted)" }}>
                  还没有账号？注册一个 →
                </Link>
              </div>
            )}

            {error && preview.valid && (
              <p className="text-xs text-red-400/90">{error}</p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
