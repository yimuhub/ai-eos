"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { api, setToken } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      if (mode === "register") {
        await api.post("/auth/register", { email, username, password });
      }
      const res = await api.post("/auth/login", { identity: email || username, password });
      setToken(res.access_token);
      router.push("/");
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen bg-gray-950 flex items-center justify-center text-gray-100">
      <form onSubmit={submit} className="w-96 bg-gray-900 border border-gray-800 rounded-xl p-8 space-y-4">
        <h1 className="text-2xl font-bold text-center">⚡ AI-EOS</h1>
        <p className="text-sm text-gray-500 text-center">人工智能企业操作系统</p>
        <input className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm"
          placeholder="邮箱或用户名" type="text" value={email} onChange={(e) => setEmail(e.target.value)} required />
        {mode === "register" && (
          <input className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm"
            placeholder="用户名" value={username} onChange={(e) => setUsername(e.target.value)} required />
        )}
        <input className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm"
          placeholder="密码" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        {error && <p className="text-red-400 text-xs">{error}</p>}
        <button disabled={loading}
          className="w-full bg-blue-600 hover:bg-blue-500 disabled:opacity-50 rounded-md py-2 text-sm font-medium">
          {loading ? "请稍候..." : mode === "login" ? "登录" : "注册并登录"}
        </button>
        <button type="button" onClick={() => setMode(mode === "login" ? "register" : "login")}
          className="w-full text-xs text-gray-500 hover:text-blue-400">
          {mode === "login" ? "没有账号？注册" : "已有账号？登录"}
        </button>
      </form>
    </div>
  );
}
