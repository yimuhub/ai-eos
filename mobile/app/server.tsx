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
import * as FileSystem from "expo-file-system";
import Constants from "expo-constants";
import Share from "react-native-share";
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

  // ---- 自动升级（更新源：你自己服务器上的 /clients/update/*） ----
  const [updating, setUpdating] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [upInfo, setUpInfo] = useState<{ latest: string; notes?: string; file?: string; url?: string } | null>(null);
  const [apkPath, setApkPath] = useState<string | null>(null);
  const curVer = (Constants.expoConfig?.version as string) || "5.1.0";

  const checkUpdate = async () => {
    const target = base.trim() || d.base;
    setUpdating(true);
    setApkPath(null);
    setUpInfo(null);
    try {
      const r = await fetch(
        `${target}/clients/update/check?platform=android&current=${curVer}`,
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const info = await r.json();
      setUpdating(false);
      if (info.has_update && info.url && info.file) {
        setUpInfo({ latest: info.latest, notes: info.notes, file: info.file, url: info.url });
      } else {
        Alert.alert(
          "已是最新版",
          `当前 Android 客户端 v${curVer}。${info.message || ""}`,
        );
      }
    } catch (e: any) {
      setUpdating(false);
      Alert.alert("检查更新失败", `无法访问更新通道（${target}）。\n${e?.message || ""}`);
    }
  };

  const downloadAndInstall = async () => {
    if (!upInfo?.url) return;
    const target = base.trim() || d.base;
    const dest = FileSystem.documentDirectory + `AI-EOS-Android-v${upInfo.latest}.apk`;
    setDownloading(true);
    try {
      const res = await FileSystem.downloadAsync(target + upInfo.url, dest);
      setDownloading(false);
      if (res && res.status < 400) {
        setApkPath(dest);
        Alert.alert("✅ 下载完成", "下一步：安装到手机。", [
          { text: "取消" },
          { text: "安装", onPress: openInstalled },
        ]);
      } else {
        Alert.alert("下载未完成", `状态码 ${res?.status ?? "未知"}，请重试。`);
      }
    } catch (e: any) {
      setDownloading(false);
      Alert.alert("下载失败", e?.message || String(e));
    }
  };

  const openInstalled = async () => {
    if (!apkPath) return;
    try {
      await Share.open({
        url: apkPath,
        type: "application/vnd.android.package-archive",
        title: "安装 AI-EOS",
        filename: "AI-EOS.apk",
      });
    } catch {
      Alert.alert(
        "请在文件管理器中打开",
        `APK 已下载到：${apkPath}\n用文件管理器找到它并安装（需允许「未知来源应用」）。`,
      );
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

      {/* 自动升级 */}
      <View style={{ gap: 10 }}>
        <View style={{ flexDirection: "row", justifyContent: "space-between", alignItems: "center" }}>
          <View>
            <Text style={{ color: "#8a93a6", fontSize: 13, marginBottom: 4 }}>自动升级（当前 v{curVer}）</Text>
            {upInfo ? (
              <Text style={{ color: "#fbbf24", fontSize: 13, fontWeight: 700 }}>
                🆕 新版本 v{upInfo.latest} 可用{upInfo.notes ? `：${String(upInfo.notes).split("\n")[0].slice(0, 40)}` : ""}
              </Text>
            ) : null}
            {apkPath ? (
              <Text style={{ color: "#86efac", fontSize: 12, marginTop: 4 }}>✅ 已下载，可直接安装</Text>
            ) : null}
          </View>
          <Pressable
            onPress={checkUpdate}
            disabled={updating || downloading}
            style={{ padding: 10, borderRadius: 10, backgroundColor: "rgba(251,191,36,0.2)", alignItems: "center" }}
          >
            {updating ? <ActivityIndicator color="#fbbf24" /> : (
              <Text style={{ color: "#fbbf24", fontWeight: 700, fontSize: 13 }}>🔍 检查更新</Text>
            )}
          </Pressable>
        </View>
        {upInfo ? (
          <View style={{ flexDirection: "row", gap: 10 }}>
            <Pressable
              onPress={downloadAndInstall}
              disabled={downloading || !!apkPath}
              style={{ flex: 1, padding: 12, borderRadius: 10, backgroundColor: downloading ? "rgba(255,255,255,0.15)" : "#f59e0b", alignItems: "center" }}
            >
              {downloading ? <ActivityIndicator color="#fff" /> : (
                <Text style={{ color: "#fff", fontWeight: 700, fontSize: 13 }}>{apkPath ? "重新下载" : "⬇️ 下载更新"}</Text>
              )}
            </Pressable>
            {apkPath ? (
              <Pressable
                onPress={openInstalled}
                style={{ flex: 1, padding: 12, borderRadius: 10, backgroundColor: "#22c55e", alignItems: "center" }}
              >
                <Text style={{ color: "#fff", fontWeight: 700, fontSize: 13 }}>📲 安装</Text>
              </Pressable>
            ) : null}
          </View>
        ) : null}
      </View>
    </ScrollView>
  );
}
