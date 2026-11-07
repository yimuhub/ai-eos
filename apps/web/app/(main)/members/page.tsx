"use client";

// 成员与邀请：老板把同事加进公司的唯一入口。
//
// 旧流程要求邀请人替对方想密码再口头/微信传过去——既是明文口令传输，
// 也让「邀请」变成「代注册」。现在只生成一条一次性链接：对方自己注册/登录，
// 链接用掉即失效，管理员可随时撤销。

import { useRef, useState } from "react";
import Link from "next/link";
import { api, describeError } from "@/lib/api";
import { useActiveCompanyId, useResource } from "@/lib/useResource";
import { confirmAction } from "@/components/ConfirmDialog";
import { notify } from "@/components/Toast";

type Member = {
  id: string;
  user_id: string;
  email: string | null;
  username: string | null;
  display_name: string | null;
  role: string;
  status: string;
  departments: { id: string; name: string; role_in_dept: string }[];
  created_at?: string;
};

type Invite = {
  id: string;
  role: string;
  department_id: string | null;
  department_name: string | null;
  state: "valid" | "used" | "revoked" | "expired";
  reason: string | null;
  expires_at: string;
  created_at?: string;
};

type Dept = { id: string; name: string };

type Bundle = { members: Member[]; invites: Invite[]; departments: Dept[] };

const ROLE_LABEL: Record<string, string> = {
  owner: "所有者",
  company_admin: "管理员",
  member: "成员",
  viewer: "只读成员",
};

const STATE_LABEL: Record<string, string> = {
  valid: "待接受",
  used: "已使用",
  revoked: "已撤销",
  expired: "已过期",
};

const MEMBER_STATUS: Record<string, string> = {
  active: "在职",
  invited: "待加入",
  suspended: "已停用",
};

function fmt(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString("zh-CN");
}

export default function MembersPage() {
  const cid = useActiveCompanyId();
  const { data, error, loading, refresh } = useResource<Bundle | null>(
    cid ? `members:${cid}` : null,
    async () => {
      const [members, invites, departments] = await Promise.all([
        api.get<Member[]>(`/companies/${cid}/members`),
        api.get<{ invites: Invite[] }>(`/companies/${cid}/invites`),
        api.get<Dept[]>(`/companies/${cid}/departments`),
      ]);
      return { members, invites: invites.invites || [], departments };
    },
  );

  const [role, setRole] = useState("member");
  const [deptId, setDeptId] = useState("");
  const [days, setDays] = useState(7);
  const [busy, setBusy] = useState(false);
  const [createdLink, setCreatedLink] = useState("");
  const linkRef = useRef<HTMLInputElement>(null);
  const [msg, setMsg] = useState("");

  const members = data?.members ?? [];
  const invites = data?.invites ?? [];
  const departments = data?.departments ?? [];

  async function createInvite() {
    if (!cid) return;
    setBusy(true);
    setMsg("");
    try {
      const r = await api.post<Invite & { token: string; link_path: string }>(
        `/companies/${cid}/invites`,
        {
          role,
          department_id: deptId || null,
          expires_in_days: days,
        },
      );
      const link = `${window.location.origin}${r.link_path}`;
      setCreatedLink(link);
      notify("success", "邀请链接已生成，发给对方即可");
      refresh();
    } catch (e) {
      setMsg(`❌ ${describeError(e, "生成邀请失败")}`);
    } finally {
      setBusy(false);
    }
  }

  async function copyLink() {
    if (!createdLink) return;
    try {
      await navigator.clipboard.writeText(createdLink);
      notify("success", "链接已复制");
    } catch {
      // 非 HTTPS / 无剪贴板权限：选中文本让用户自己按 ⌘C，而不是假装复制成功
      linkRef.current?.focus();
      linkRef.current?.select();
      notify("warning", "无法自动复制，已选中链接，请按 ⌘C / Ctrl+C");
    }
  }

  async function revoke(inv: Invite) {
    if (!cid) return;
    if (!(await confirmAction({
      title: "撤销这条邀请链接？",
      description: "撤销后链接立即失效，已经拿到链接的人也无法再加入。",
      confirmText: "撤销链接",
    }))) return;
    try {
      await api.del(`/companies/${cid}/invites/${inv.id}`);
      notify("success", "邀请已撤销");
      refresh();
    } catch (e) {
      setMsg(`❌ ${describeError(e, "撤销失败")}`);
    }
  }

  if (!cid) {
    return (
      <div className="p-6 max-w-[1100px] mx-auto">
        <div className="card card-static text-center py-16 space-y-2">
          <p className="text-sm">还没有可管理的公司</p>
          <Link href="/companies" className="text-xs text-indigo-300 hover:underline">
            前往公司管理创建一家 →
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="p-6 max-w-[1100px] mx-auto animate-fade-up">
      <div className="relative overflow-hidden rounded-2xl mb-5 p-6"
        style={{ background: "linear-gradient(120deg, rgba(99,102,241,0.14), rgba(34,211,238,0.06) 60%, transparent)", border: "1px solid rgba(99,102,241,0.22)" }}>
        <div className="flex items-center gap-4 flex-wrap">
          <div className="w-12 h-12 rounded-full flex items-center justify-center text-2xl"
            style={{ background: "linear-gradient(135deg,#6366f1,#8b5cf6)" }}>👥</div>
          <div>
            <h1 className="text-xl font-bold">成员与邀请</h1>
            <p className="text-xs mt-0.5" style={{ color: "var(--muted)" }}>
              生成一次性链接邀请同事：对方自己注册、登录后加入，密码不经过你
            </p>
          </div>
          <span className="ml-auto text-xs" style={{ color: "var(--muted)" }}>
            {members.length} 名成员 · {invites.filter((i) => i.state === "valid").length} 条待接受
          </span>
        </div>
      </div>

      {(msg || error) && (
        <div className="mb-4 px-4 py-2 rounded-lg text-sm"
          style={{ background: "rgba(99,102,241,0.1)", color: "#a5b4fc", border: "1px solid rgba(99,102,241,0.25)" }}>
          {msg || `加载失败：${error}`}
        </div>
      )}

      {/* 生成邀请 */}
      <div className="card mb-4">
        <h2 className="text-sm font-semibold mb-3">🔗 生成邀请链接</h2>
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-xs space-y-1">
            <span style={{ color: "var(--muted)" }}>加入后的角色</span>
            <select className="input w-40" value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="member">成员</option>
              <option value="viewer">只读成员</option>
              <option value="company_admin">管理员</option>
            </select>
          </label>
          <label className="text-xs space-y-1">
            <span style={{ color: "var(--muted)" }}>加入部门（可选）</span>
            <select className="input w-44" value={deptId} onChange={(e) => setDeptId(e.target.value)}>
              <option value="">不指定</option>
              {departments.map((d) => (
                <option key={d.id} value={d.id}>{d.name}</option>
              ))}
            </select>
          </label>
          <label className="text-xs space-y-1">
            <span style={{ color: "var(--muted)" }}>有效期</span>
            <select className="input w-28" value={days} onChange={(e) => setDays(Number(e.target.value))}>
              <option value={1}>1 天</option>
              <option value={7}>7 天</option>
              <option value={14}>14 天</option>
              <option value={30}>30 天</option>
            </select>
          </label>
          <button onClick={createInvite} disabled={busy}
            className="text-xs px-4 py-2 rounded-lg font-medium text-white disabled:opacity-50"
            style={{ background: "linear-gradient(135deg,#8b5cf6,#6366f1)" }}>
            {busy ? "生成中…" : "生成链接"}
          </button>
        </div>
        <p className="text-[10px] mt-2" style={{ color: "var(--muted)" }}>
          链接只能使用一次；「管理员」角色只有公司所有者可以授予。
        </p>
        {createdLink && (
          <div className="mt-3 flex flex-wrap items-center gap-2 rounded-lg p-3"
            style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
            <input ref={linkRef} readOnly value={createdLink}
              onFocus={(e) => e.currentTarget.select()}
              className="flex-1 min-w-[260px] text-xs bg-transparent outline-none"
              style={{ color: "var(--text)" }} />
            <button onClick={copyLink} className="btn-ghost text-xs">复制</button>
          </div>
        )}
      </div>

      {/* 待接受 / 历史邀请 */}
      <div className="card mb-4">
        <h2 className="text-sm font-semibold mb-3">邀请记录</h2>
        {!invites.length ? (
          <p className="text-xs py-6 text-center" style={{ color: "var(--muted)" }}>
            还没有生成过邀请链接
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr style={{ color: "var(--muted)" }}>
                  <th className="text-left font-medium py-2">角色</th>
                  <th className="text-left font-medium py-2">部门</th>
                  <th className="text-left font-medium py-2">状态</th>
                  <th className="text-left font-medium py-2">过期时间</th>
                  <th className="text-right font-medium py-2">操作</th>
                </tr>
              </thead>
              <tbody>
                {invites.map((inv) => (
                  <tr key={inv.id} style={{ borderTop: "1px solid var(--border)" }}>
                    <td className="py-2">{ROLE_LABEL[inv.role] || inv.role}</td>
                    <td className="py-2">{inv.department_name || "—"}</td>
                    <td className="py-2">
                      <span className={inv.state === "valid" ? "text-emerald-300" : ""}
                        style={inv.state === "valid" ? undefined : { color: "var(--muted)" }}>
                        {STATE_LABEL[inv.state] || inv.state}
                      </span>
                    </td>
                    <td className="py-2" style={{ color: "var(--muted)" }}>{fmt(inv.expires_at)}</td>
                    <td className="py-2 text-right">
                      {inv.state === "valid" ? (
                        <button onClick={() => revoke(inv)}
                          className="text-red-400/90 hover:underline">撤销</button>
                      ) : (
                        <span style={{ color: "var(--muted)" }}>—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 成员列表 */}
      <div className="card">
        <h2 className="text-sm font-semibold mb-3">成员</h2>
        {loading ? (
          <p className="text-xs py-6 text-center" style={{ color: "var(--muted)" }}>加载中…</p>
        ) : !members.length ? (
          <p className="text-xs py-6 text-center" style={{ color: "var(--muted)" }}>
            还没有别人加入。生成一条邀请链接发给同事即可。
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr style={{ color: "var(--muted)" }}>
                  <th className="text-left font-medium py-2">成员</th>
                  <th className="text-left font-medium py-2">邮箱</th>
                  <th className="text-left font-medium py-2">角色</th>
                  <th className="text-left font-medium py-2">部门</th>
                  <th className="text-left font-medium py-2">状态</th>
                  <th className="text-left font-medium py-2">加入时间</th>
                </tr>
              </thead>
              <tbody>
                {members.map((m) => (
                  <tr key={m.id} style={{ borderTop: "1px solid var(--border)" }}>
                    <td className="py-2">{m.display_name || m.username || m.user_id}</td>
                    <td className="py-2" style={{ color: "var(--muted)" }}>{m.email || "—"}</td>
                    <td className="py-2">{ROLE_LABEL[m.role] || m.role}</td>
                    <td className="py-2">
                      {m.departments.length
                        ? m.departments.map((d) => d.name).join("、")
                        : "—"}
                    </td>
                    <td className="py-2">
                      <span className={m.status === "active" ? "text-emerald-300" : ""}
                        style={m.status === "active" ? undefined : { color: "var(--muted)" }}>
                        {MEMBER_STATUS[m.status] || m.status}
                      </span>
                    </td>
                    <td className="py-2" style={{ color: "var(--muted)" }}>{fmt(m.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
