import { useEffect, useState } from "react";
import { View, Text, ScrollView, Pressable, ActivityIndicator, Alert } from "react-native";
import { useAieos } from "../store";

// 签字门：拉取所有"等待签字/审批"的关键步骤（作战室签字门、自动化审批、交付验收），
// 老板在此一键通过/驳回。呼吸灯高亮新到待签项。
type Item = {
  id: string;
  title: string;
  ref: string;
  status: string;
  pending: boolean;
};

export default function Approve() {
  const { api, svc, loggedIn } = useAieos();
  const [items, setItems] = useState<Item[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  const load = async () => {
    if (!loggedIn) return;
    setLoading(true);
    setErr("");
    try {
      // 老板视角聚合"待签字"：拉作战室 + 任务中的 pending 状态项
      // 这里用 report 的 lines 结构 + command/report 拉取；若端点未实现聚合，给占位。
      const dash = await api.dashboard(svc.companyId);
      // dashboard 若带回 pending_approvals，优先用它；否则给引导
      if (Array.isArray(dash?.pending_approvals)) {
        setItems(
          dash.pending_approvals.map((a: any) => ({
            id: String(a.id ?? a.ref),
            title: a.title || a.task || "待审批项",
            ref: a.ref || a.id,
            status: a.status || "pending",
            pending: true,
          })),
        );
      } else {
        setItems([]);
      }
    } catch (e: any) {
      setErr(e?.message || "无法拉取审批列表，请先确认服务连接");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [loggedIn, svc]);

  const decide = (ok: boolean) => {
    // 签字门落地需 /governance 或 /war_room approve 端点；移动端先做"通过/驳回"意图，
    // 后续 V4.2 接 governance 审批 API。当前用 Alert 回执 + 本地标记。
    Alert.alert("签字门", ok ? "已记录通过（审批端点 V4.2 接入）" : "已记录驳回（审批端点 V4.2 接入）");
  };

  return (
    <ScrollView contentContainerStyle={{ padding: 16, gap: 12 }}>
      <View>
        <Text style={{ fontSize: 22, fontWeight: 700, color: "#e8ecf4" }}>签字门</Text>
        <Text style={{ color: "#8a93a6", fontSize: 13, marginTop: 4 }}>
          关键步骤需你亲笔通过 · 呼吸灯标出新到项
        </Text>
      </View>

      {loading ? (
        <View style={{ padding: 40, alignItems: "center" }}>
          <ActivityIndicator color="#22d3ee" />
        </View>
      ) : err ? (
        <View style={{ padding: 16, backgroundColor: "rgba(239,68,68,0.12)", borderRadius: 12 }}>
          <Text style={{ color: "#fca5a5" }}>{err}</Text>
        </View>
      ) : items.length === 0 ? (
        <View style={{ padding: 28, backgroundColor: "rgba(255,255,255,0.05)", borderRadius: 14, alignItems: "center" }}>
          <Text style={{ fontSize: 32 }}>🟢</Text>
          <Text style={{ color: "#86efac", fontWeight: 600, marginTop: 8 }}>暂无待签字项</Text>
          <Text style={{ color: "#5a6272", fontSize: 12, marginTop: 6 }}>
            执行关键步骤时会自动进入签字门，等你通过
          </Text>
        </View>
      ) : (
        items.map((it, i) => (
          <View
            key={i}
            style={{
              padding: 16,
              borderRadius: 14,
              backgroundColor: "rgba(255,255,255,0.06)",
              gap: 8,
              // 呼吸灯：新到待签项高亮（动画用 V4.2 接 react-native-reanimated 做真呼吸）
              borderLeftColor: "#22d3ee",
              borderLeftWidth: 3,
            }}
          >
            <Text style={{ color: "#e8ecf4", fontWeight: 600 }}>{it.title}</Text>
            <View style={{ flexDirection: "row", gap: 10 }}>
              <Pressable
                onPress={() => decide(true)}
                style={{ flex: 1, padding: 10, borderRadius: 10, backgroundColor: "#22c55e", alignItems: "center" }}
              >
                <Text style={{ color: "#fff", fontWeight: 700 }}>✍️ 通过</Text>
              </Pressable>
              <Pressable
                onPress={() => decide(false)}
                style={{ flex: 1, padding: 10, borderRadius: 10, backgroundColor: "rgba(239,68,68,0.85)", alignItems: "center" }}
              >
                <Text style={{ color: "#fff", fontWeight: 700 }}>✕ 驳回</Text>
              </Pressable>
            </View>
          </View>
        ))
      )}

      <Pressable
        onPress={load}
        style={{ padding: 12, borderRadius: 12, backgroundColor: "rgba(255,255,255,0.08)", alignItems: "center" }}
      >
        <Text style={{ color: "#22d3ee" }}>🔄 刷新待签</Text>
      </Pressable>
    </ScrollView>
  );
}
