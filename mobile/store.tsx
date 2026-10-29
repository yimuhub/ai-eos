/**
 * 移动端全局状态：服务连接(base URL + company) + 登录 token。
 * 用 SecureStore 持久化（token），AppState 管内存态。
 */
import { createContext, useContext, useState, useCallback, useEffect } from "react";
import * as SecureStore from "expo-secure-store";
import { Api, defaults, setUnauthorizedHandler } from "./lib/api";

type Svc = { base: string; companyId: string };
type Ctx = {
  svc: Svc;
  token: string;
  api: Api;
  loggedIn: boolean;
  login: (identifier: string, password: string) => Promise<void>;
  register: (identifier: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  setService: (svc: Svc) => Promise<void>;
};

const C = createContext<Ctx>(null as any);
export const useAieos = () => useContext(C);

const d = defaults();

export function AieosProvider({ children }: { children: React.ReactNode }) {
  const [svc, setSvc] = useState<Svc>(d);
  const [token, setToken] = useState("");

  // 启动时恢复（token + 服务连接都存 SecureStore，首次为 null 则用默认值）
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const raw = await SecureStore.getItemAsync("aieos.svc");
        const t = await SecureStore.getItemAsync("aieos.token");
        if (!alive) return;
        if (raw) setSvc({ ...d, ...JSON.parse(raw) });
        if (t) setToken(t);
      } catch {
        /* 首次启动无数据，保持默认 */
      }
    })();
    return () => { alive = false; };
  }, []);

  const api = new Api(svc.base, token);

  // token 过期 → 清凭据回登录态（各页面不再只显示一行 401 文本）
  useEffect(() => {
    setUnauthorizedHandler(() => {
      SecureStore.deleteItemAsync("aieos.token").catch(() => {});
      setToken("");
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  const login = useCallback(
    async (identifier: string, password: string) => {
      const r = await Api.login(svc.base, identifier, password);
      await SecureStore.setItemAsync("aieos.token", r.token);
      setToken(r.token);
    },
    [svc.base],
  );

  const register = useCallback(
    async (identifier: string, password: string) => {
      const r = await Api.register(svc.base, identifier, password);
      await SecureStore.setItemAsync("aieos.token", r.token);
      setToken(r.token);
    },
    [svc.base],
  );

  const logout = useCallback(async () => {
    await SecureStore.deleteItemAsync("aieos.token");
    setToken("");
  }, []);

  const setService = useCallback(async (next: Svc) => {
    await SecureStore.setItemAsync("aieos.svc", JSON.stringify(next));
    setSvc(next);
  }, []);

  return (
    <C.Provider value={{ svc, token, api, loggedIn: !!token, login, register, logout, setService }}>
      {children}
    </C.Provider>
  );
}
