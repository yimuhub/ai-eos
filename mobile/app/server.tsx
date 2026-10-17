import { useState } from "react";
import {
  View,
  Text,
  TextInput,
  Pressable,
  ScrollView,
  ActivityIndicator,
  Alert,
} from "react-native";
import { useAieos } from "../store";
import { defaults } from "../lib/api";

// 服务连接：老板视角 App 的入口页。
// 配置服务地址（本机便携版 / 远程自托管服务器）+ 登录。
export default function Server() {
  const { svc, token, loggedIn, api, login, register, logout, setService } =
    useAieos();
  const d = defaults();
  const [base, setBase] = useState(svc.base || d.base);
  const [companyId, setCompanyId] = useState(svc.companyId || d.companyId);
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [testing, setTesting] = useState(false);
  const [online, setOnline] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setTesting(true);
    setOnline(null);
    try {
      await setService({ base: base.trim() || d.base, companyId: companyId.trim() || d.companyId });
      setTesting(false);
      Alert.alert("已保存", `服务地址：${base.trim() || d.base}\n公司：${companyId.trim() || d.companyId}`);
    } catch (e: any) {
      setTesting(false);
      Alert.alert("保存失败", e?.message || String(e));
    }
  };

  const testConn = async () => {
    setTesting(true);
    const target = base.trim() || d.base;
    try {
      const r = await fetch(target + "/health", { method: "GET" });
      setOnline(r.ok);
      setTesting(false);
      Alert.alert(
        r.ok ? "✅ 连接成功" : "❌ 连接失败",
        r.ok
          ? `${target} 后端在线，可以登录使用。`
          : `${target} 无法访问。\n若在本机：先启动便携后端（packaging/start-mac.sh）\n若在远程：确认真实 IP:8001 且允许外网访问。`,
      );
    } catch (e: any) {
      setOnline(false);
      setTesting(false);
      Alert.alert("❌ 连接失败", `无法访问 ${target}。\n${e?.message || ""}`);
    }
  };

  const doLogin = async () => {
    if (!identifier.trim() || !password)
      return Alert.alert("请填写", "账号和口令都要填。");
    setBusy(true);
    try {
      await login(identifier.trim(), password);
      setBusy(false);
      Alert.alert("✅ 已登录", "老板视角已解锁。");
    } catch (e: any) {
      setBusy(false);
      Alert.alert("登录失败", e?.message || String(e));
    }
  };

  const doRegister = async () => {
    if (!identifier.trim() || !password)
      return Alert.alert("请填写", "账号和口令都要填。");
    setBusy(true);
    try {
      await register(identifier.trim(), password);
      setBusy(false);
      Alert.alert("✅ 注册并登录成功", "老板视角已解锁。");
    } catch (e: any) {
      setBusy(false);
      Alert.alert("注册失败", e?.message || String(e));
    }
  };

  return (
    <ScrollView contentContainerStyle={{ padding: 16, gap: 16 }}>
      <View>
        <Text style={{ fontSize: 22, fontWeight: 700, color: "#e8ecf4" }}>服务连接</Text>
        <Text style={{ color: "#8a93a6", fontSize: 13, marginTop: 4 }}>
          连到你的 AI-EOS 服务器（本机便携版 / 远程自托管）
        </Text>
      </View>

      {/* 服务地址 */}
      <View style={{ gap: 10 }}>
        <TextInput
          value={base}
          onChangeText={setBase}
          placeholder="http://192.168.x.x:8001"
          placeholderTextColor="#5a6272"
          autoCapitalize="none"
          style={{
            padding: 14,
            borderRadius: 12,
            backgroundColor: "rgba(255,255,255,0.06)",
            color: "#e8ecf4",
            fontSize: 15,
          }}
        />
        <TextInput
          value={companyId}
          onChangeText={setCompanyId}
          placeholder="公司 ID（默认测试公司）"
          placeholderTextColor="#5a6272"
          style={{
            padding: 14,
            borderRadius: 12,
            backgroundColor: "rgba(255,255,255,0.06)",
            color: "#e8ecf4",
            fontSize: 13,
          }}
        />
        <View style={{ flexDirection: "row", gap: 10 }}>
          <Pressable
            onPress={save}
            style={{ flex: 1, padding: 12, borderRadius: 10, backgroundColor: "#6366f1", alignItems: "center" }}
            disabled={testing}
          >
            {testing ? <ActivityIndicator color="#fff" /> : (
              <Text style={{ color: "#fff", fontWeight: 700 }}>💾 保存</Text>
            )}
          </Pressable>
          <Pressable
            onPress={testConn}
            style={{ flex: 1, padding: 12, borderRadius: 10, backgroundColor: "rgba(34,211,238,0.2)", alignItems: "center" }}
            disabled={testing}
          >
            <Text style={{ color: "#22d3ee", fontWeight: 700 }}>🔌 测试连接</Text>
          </Pressable>
        </View>
        {online !== null ? (
          <Text style={{ color: online ? "#86efac" : "#fca5a5", fontSize: 13 }}>
            {online ? "✅ 后端在线" : "❌ 后端离线"}
          </Text>
        ) : null}
      </View>

      {/* 登录/注册 */}
      <View style={{ gap: 10 }}>
        <Text style={{ color: "#8a93a6", fontSize: 13, marginBottom: 4 }}>
          {loggedIn ? "当前已登录（可换号）" : "登录进入老板视角"}
        </Text>
        <TextInput
          value={identifier}
          onChangeText={setIdentifier}
          placeholder="账号 / 邮箱 / 手机"
          placeholderTextColor="#5a6272"
          autoCapitalize="none"
          style={{
            padding: 14,
            borderRadius: 12,
            backgroundColor: "rgba(255,255,255,0.06)",
            color: "#e8ecf4",
            fontSize: 15,
          }}
        />
        <TextInput
          value={password}
          onChangeText={setPassword}
          placeholder="口令"
          placeholderTextColor="#5a6272"
          secureTextEntry
          style={{
            padding: 14,
            borderRadius: 12,
            backgroundColor: "rgba(255,255,255,0.06)",
            color: "#e8ecf4",
            fontSize: 15,
          }}
        />
        <View style={{ flexDirection: "row", gap: 10 }}>
          <Pressable
            onPress={doLogin}
            style={{ flex: 1, padding: 12, borderRadius: 10, backgroundColor: "#22c55e", alignItems: "center" }}
            disabled={busy}
          >
            {busy ? <ActivityIndicator color="#fff" /> : (
              <Text style={{ color: "#fff", fontWeight: 700 }}>🔑 登录</Text>
            )}
          </Pressable>
          <Pressable
            onPress={doRegister}
            style={{ flex: 1, padding: 12, borderRadius: 10, backgroundColor: "rgba(255,255,255,0.1)", alignItems: "center" }}
            disabled={busy}
          >
            <Text style={{ color: "#e8ecf4", fontWeight: 600 }}>📝 注册</Text>
          </Pressable>
          {loggedIn ? (
            <Pressable
              onPress={logout}
              style={{ padding: 12, borderRadius: 10, backgroundColor: "rgba(239,68,68,0.7)", alignItems: "center" }}
            >
              <Text style={{ color: "#fff", fontWeight: 600 }}>退出</Text>
            </Pressable>
          ) : null}
        </View>
      </View>
    </ScrollView>
  );
}
