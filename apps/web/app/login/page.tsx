"use client";

// 登录 / 注册。
//
// ## 改造说明
//
// 原实现有三个直接影响首次使用的问题：
// 1. 注册模式只有一个"邮箱或用户名"输入框，但后端 register 要求
//    **合法邮箱**（EmailStr）——用户按提示填用户名会被 422 打回，
//    而错误信息是一串英文校验详情。现在注册模式明确区分邮箱与用户名，
//    并在提交前本地校验。
// 2. 密码没有任何提示，注册完才知道后端规则；现在把规则写在界面上，
//    并在本机先行校验（与后端保持一致：≥8 位、含字母与数字）。
// 3. 样式用了另一套灰阶类，与全站设计令牌不一致；现统一到 design tokens，
//    并补上加载态、密码可见性切换与"这套系统能干什么"的简短说明。

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api, setToken, setRefreshToken } from "@/lib/api";

const MIN_PW = 8;

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [identity, setIdentity] = useState("");   // 登录：邮箱或用户名
  const [email, setEmail] = useState("");         // 注册：邮箱
  const [username, setUsername] = useState("");   // 注册：用户名
  const [password, setPassword] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  /** 与后端 RegisterIn 的规则保持一致，先本地拦一道，避免无谓往返 */
  function validate(): string {
    if (mode === "login") {
      if (!identity.trim()) return "请填写邮箱或用户名";
      if (!password) return "请填写密码";
      return "";
    }
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.trim())) return "请填写有效的邮箱地址";
    if (username.trim().length < 2) return "用户名至少 2 个字符";
    if (password.length < MIN_PW) return `密码至少 ${MIN_PW} 位`;
    if (!/[A-Za-z]/.test(password) || !/\d/.test(password)) return "密码需同时包含字母和数字";
    return "";
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const v = validate();
    if (v) {
      setError(v);
      return;
    }
    setError("");
    setLoading(true);
    try {
      if (mode === "register") {
        await api.post("/auth/register", {
          email: email.trim(),
          username: username.trim(),
          password,
        });
      }
      const res = await api.post<{ access_token: string; refresh_token?: string }>("/auth/login", {
        identity: mode === "register" ? email.trim() : identity.trim(),
        password,
      });
      setToken(res.access_token);
      // 后端返回 refresh_token 时存下，供 401 自动续期用；没有也不影响
      if (res.refresh_token) setRefreshToken(res.refresh_token);
      // 用 replace 避免用户回退时又落回登录页
      router.replace("/workspace");
    } catch (err: any) {
      // lib/api.ts 对 /auth/login、/auth/register 的 401 不再整页跳转，
      // 错误在此内联呈现：文案可见、已输入内容不清空、loading 在 finally 恢复。
      setError(err?.message || "操作失败，请重试");
    } finally {
      setLoading(false);
    }
  }

  function switchMode() {
    setMode(mode === "login" ? "register" : "login");
    setError("");
    setPassword("");
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-4 py-10">
      <div className="w-full max-w-[880px] grid md:grid-cols-2 gap-0 rounded-2xl overflow-hidden"
        style={{ border: "1px solid var(--border)", background: "var(--surface)" }}>

        {/* 左侧：价值说明（首次使用者需要知道"这是什么"） */}
        <div className="hidden md:flex flex-col justify-between p-8"
          style={{ background: "linear-gradient(160deg, rgba(99,102,241,0.16), rgba(34,211,238,0.06))" }}>
          <div>
            <h1 className="text-2xl font-bold tracking-tight">⚡ AI-EOS <span className="text-xs align-top ml-1 px-1.5 py-0.5 rounded-full bg-gradient-to-r from-indigo-500 to-cyan-400 text-white font-semibold">V5.0</span></h1>
            <p className="text-sm mt-1.5" style={{ color: "var(--muted)" }}>
              人工智能企业操作系统 · 专家 × 组织形态版
            </p>
            <ul className="mt-8 space-y-4 text-sm">
              {[
                ["🏢", "建一家 AI 公司", "按模板一键生成部门与数字员工"],
                ["🎯", "下达目标而非步骤", "说清要什么结果，Agent 自己拆解执行"],
                ["📄", "交付真实成果", "自动产出 Word / PPT / Excel 并归档"],
                ["🔍", "全程可追溯", "每个动作、每次失败都留痕，可回滚"],
              ].map(([icon, title, desc]) => (
                <li key={title} className="flex gap-3">
                  <span className="text-lg shrink-0">{icon}</span>
                  <span>
                    <span className="block font-medium">{title}</span>
                    <span className="block text-xs mt-0.5" style={{ color: "var(--muted)" }}>{desc}</span>
                  </span>
                </li>
              ))}
            </ul>
          </div>
          <p className="text-[11px] leading-relaxed" style={{ color: "var(--muted)" }}>
            数据保存在你自己的机器上，模型密钥只写在本地 .env。
          </p>
        </div>

        {/* 右侧：表单 */}
        <form onSubmit={submit} className="p-8 space-y-4">
          <div className="md:hidden">
            <h1 className="text-xl font-bold">⚡ AI-EOS</h1>
          </div>

          <div>
            <h2 className="text-lg font-semibold">
              {mode === "login" ? "登录" : "创建账号"}
            </h2>
            <p className="text-xs mt-1" style={{ color: "var(--muted)" }}>
              {mode === "login" ? "欢迎回来" : "只需邮箱、用户名和密码"}
            </p>
          </div>

          {mode === "login" ? (
            <label className="block">
              <span className="text-xs block mb-1" style={{ color: "var(--muted)" }}>邮箱或用户名</span>
              <input
                autoFocus
                value={identity}
                onChange={(e) => setIdentity(e.target.value)}
                placeholder="you@example.com"
                className="input"
                disabled={loading}
                autoComplete="username"
              />
            </label>
          ) : (
            <>
              <label className="block">
                <span className="text-xs block mb-1" style={{ color: "var(--muted)" }}>邮箱</span>
                <input
                  autoFocus
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                  type="email"
                  className="input"
                  disabled={loading}
                  autoComplete="email"
                />
              </label>
              <label className="block">
                <span className="text-xs block mb-1" style={{ color: "var(--muted)" }}>用户名</span>
                <input
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="登录时也可用它"
                  className="input"
                  disabled={loading}
                  autoComplete="nickname"
                />
              </label>
            </>
          )}

          <label className="block">
            <span className="text-xs block mb-1" style={{ color: "var(--muted)" }}>密码</span>
            <div className="relative">
              <input
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={mode === "register" ? `至少 ${MIN_PW} 位，含字母和数字` : "••••••••"}
                type={showPw ? "text" : "password"}
                className="input pr-12"
                disabled={loading}
                autoComplete={mode === "login" ? "current-password" : "new-password"}
              />
              <button
                type="button"
                onClick={() => setShowPw((s) => !s)}
                className="absolute right-2.5 top-1/2 -translate-y-1/2 text-xs px-1.5 py-0.5 rounded"
                style={{ color: "var(--muted)" }}
                tabIndex={-1}
              >
                {showPw ? "隐藏" : "显示"}
              </button>
            </div>
          </label>

          {error && (
            <div className="rounded-lg px-3 py-2 text-xs leading-relaxed"
              style={{ background: "rgba(248,113,113,0.12)", border: "1px solid rgba(248,113,113,0.4)",
                       color: "#fca5a5" }}>
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="btn-primary w-full disabled:opacity-50"
          >
            {loading ? "请稍候…" : mode === "login" ? "登录" : "注册并登录"}
          </button>

          <button
            type="button"
            onClick={switchMode}
            disabled={loading}
            className="w-full text-xs transition-colors"
            style={{ color: "var(--muted)" }}
          >
            {mode === "login" ? "还没有账号？立即注册" : "已有账号？返回登录"}
          </button>
        </form>
      </div>
    </div>
  );
}
