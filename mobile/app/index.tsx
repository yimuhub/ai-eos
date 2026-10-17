import { useEffect, useState } from "react";
import { View, Text, ScrollView, ActivityIndicator, Pressable } from "react-native";
import { useAieos } from "../store";

export default function Dashboard() {
  const { api, svc, loggedIn } = useAieos();
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  const load = async () => {
    if (!loggedIn) return;
    setLoading(true);
    setErr("");
    try {
      const d = await api.dashboard(svc.companyId);
      setData(d);
    } catch (e: any) {
      setErr(e?.message || "无法连接后端，请先在「连接」页配置服务地址");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [loggedIn, svc]);

  if (!loggedIn) {
    return (
      <View style={{ flex: 1, alignItems: "center", justifyContent: "center", gap: 12, padding: 32 }}>
        <Text style={{ fontSize: 40 }}>🔒</Text>
        <Text style={{ color: "#8a93a6" }}>请先登录</Text>
        <Text style={{ color: "#6366f1" }}>登录后即可查看公司 KPI 仪表盘</Text>
      </View>
    );
  }

  return (
    <ScrollView contentContainerStyle={{ padding: 16 }}>
      <Text style={{ fontSize: 22, fontWeight: "700", color: "#e8ecf4", marginBottom: 4 }}>
        公司 KPI 仪表盘
      </Text>
      <Text style={{ color: "#8a93a6", fontSize: 13, marginBottom: 16 }}>
        {svc.base} · 点下方「刷新」拉取实时数据
      </Text>

      {loading ? (
        <View style={{ padding: 40, alignItems: "center" }}>
          <ActivityIndicator color="#22d3ee" />
        </View>
      ) : err ? (
        <View style={{ padding: 20, backgroundColor: "rgba(239,68,68,0.12)", borderRadius: 12 }}>
          <Text style={{ color: "#fca5a5" }}>{err}</Text>
        </View>
      ) : (
        <View style={{ gap: 12 }}>
          {renderKpi(data)}
        </View>
      )}

      <Pressable
        onPress={load}
        style={{
          marginTop: 16,
          padding: 14,
          borderRadius: 12,
          backgroundColor: "linear-gradient(90deg,#6366f1,#22d3ee)" as any,
          alignItems: "center",
        }}
      >
        <Text style={{ color: "#fff", fontWeight: "600" }}>🔄 刷新数据</Text>
      </Pressable>
    </ScrollView>
  );
}

// 容错渲染：dashboard 返回结构可能带 agent_count/task_count/token_stats 等
function renderKpi(d: any) {
  const cards: { label: string; value: string; emoji: string }[] = [];
  const pick = (k: string, suffix = "") => {
    const v = d?.[k];
    if (v !== undefined && v !== null)
      cards.push({ label: k, value: typeof v === "object" ? JSON.stringify(v) : String(v) + suffix, emoji: "📈" });
  };
  pick("agent_count", " 位 Agent");
  pick("task_count");
  pick("project_count", " 个项目");
  pick("expert_count", " 位专家");
  if (Array.isArray(d?.lines)) {
    cards.push({ label: "报告行数", value: String(d.lines.length), emoji: "📋" });
  }
  if (cards.length === 0) {
    return (
      <View style={{ padding: 24, backgroundColor: "rgba(255,255,255,0.05)", borderRadius: 12 }}>
        <Text style={{ color: "#8a93a6" }}>后端已连通，但 /dashboard 暂无可展示字段。</Text>
        <Text style={{ color: "#5a6272", fontSize: 12, marginTop: 8 }}>原始数据：</Text>
        <Text style={{ color: "#5a6272", fontSize: 11, marginTop: 4 }}>{JSON.stringify(d, null, 2)?.slice(0, 400)}</Text>
      </View>
    );
  }
  return cards.map((c, i) => (
    <View
      key={i}
      style={{
        padding: 18,
        borderRadius: 14,
        backgroundColor: "rgba(255,255,255,0.06)",
        borderColor: "rgba(255,255,255,0.08)",
        borderWidth: 1,
      }}
    >
      <Text style={{ color: "#8a93a6", fontSize: 12, marginBottom: 6 }}>{c.emoji} {c.label}</Text>
      <Text style={{ color: "#e8ecf4", fontSize: 26, fontWeight: "700" }}>{c.value}</Text>
    </View>
  ));
}
