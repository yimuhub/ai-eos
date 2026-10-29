import { useEffect, useState } from "react";
import { View, Text, ScrollView, Pressable, ActivityIndicator, Alert } from "react-native";
import { useAieos } from "../store";

// 签字门：拉取后端真实待审批队列（intervention ApprovalRequest status=PENDING），
// 老板在此一键通过/驳回；通过/驳回直接落库，成功后刷新列表，失败必须显性报错。
type Item = {
  id: string;
  title: string;
  ref: string;
  status: string;
  pending: boolean;
  actionType?: string;
  requestedBy?: string;
  reason?: string;
};

const ACTION_LABELS: Record<string, string> = {
  task_create: "创建任务",
  task_complete: "完成任务",
  meeting_start: "启动会议",
  artifact_approve: "产出物验收",
  escalate: "升级处理",
  computer_command: "本机高危操作",
  external_agent: "外部 Agent 外发",
};

function normalize(raw: any): Item {
  const actionType = String(raw?.action_type || "");
  return {
    id: String(raw?.id ?? ""),
    title:
      raw?.reason ||
      ACTION_LABELS[actionType] ||
      actionType ||
      "待审批项",
    ref: String(raw?.id ?? ""),
    status: String(raw?.status || "PENDING"),
    pending: String(raw?.status || "").toUpperCase() === "PENDING",
    actionType,
    requestedBy: raw?.requested_by ? String(raw.requested_by) : undefined,
    reason: raw?.reason ? String(raw.reason) : undefined,
  };
}

export default function Approve() {
  const { api, svc, loggedIn } = useAieos();
  const [items, setItems] = useState<Item[]>([]);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState("");
  const [err, setErr] = useState("");

  const load = async () => {
    if (!loggedIn) return;
    setLoading(true);
    setErr("");
    try {
      // 真实待审批队列：GET /intervention/requests?company_id=...&status=PENDING
      const list = await api.pendingApprovals(svc.companyId);
      if (!Array.isArray(list)) {
        throw new Error("审批列表返回格式异常，请升级客户端或检查后端版本");
      }
      setItems(list.map(normalize));
    } catch (e: any) {
      setItems([]);
      setErr(e?.message || "无法拉取审批列表，请先确认服务连接");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [loggedIn, svc]);

  const decide = async (it: Item, ok: boolean) => {
    if (busyId) return;
    setBusyId(it.id);
    setErr("");
    try {
      // 真接口：POST /intervention/requests/{id}/decide
      // 失败会抛 ApiError，绝不本地标记成功。
      await api.decideApproval(it.id, ok ? "approve" : "reject");
      Alert.alert("签字门", ok ? "已通过，审批单已生效" : "已驳回，审批单已退回");
      await load(); // 成功后才刷新列表
    } catch (e: any) {
      const msg = e?.message || "审批提交失败，请稍后重试";
      setErr(msg);
      Alert.alert("审批失败", msg);
    } finally {
      setBusyId("");
    }
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
        items.map((it) => {
          const busy = busyId === it.id;
          return (
            <View
              key={it.id}
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
              <Text style={{ color: "#5a6272", fontSize: 11 }}>
                {it.actionType || "pending"}
                {it.requestedBy ? ` · 发起人 ${it.requestedBy.slice(0, 8)}` : ""}
              </Text>
              <View style={{ flexDirection: "row", gap: 10 }}>
                <Pressable
                  disabled={busy}
                  onPress={() => decide(it, true)}
                  style={{
                    flex: 1, padding: 10, borderRadius: 10,
                    backgroundColor: busy ? "rgba(34,197,94,0.4)" : "#22c55e",
                    alignItems: "center",
                  }}
                >
                  {busy ? <ActivityIndicator color="#fff" /> : (
                    <Text style={{ color: "#fff", fontWeight: 700 }}>✍️ 通过</Text>
                  )}
                </Pressable>
                <Pressable
                  disabled={busy}
                  onPress={() => decide(it, false)}
                  style={{
                    flex: 1, padding: 10, borderRadius: 10,
                    backgroundColor: busy ? "rgba(239,68,68,0.35)" : "rgba(239,68,68,0.85)",
                    alignItems: "center",
                  }}
                >
                  {busy ? <ActivityIndicator color="#fff" /> : (
                    <Text style={{ color: "#fff", fontWeight: 700 }}>✕ 驳回</Text>
                  )}
                </Pressable>
              </View>
            </View>
          );
        })
      )}

      <Pressable
        onPress={load}
        disabled={loading || !!busyId}
        style={{ padding: 12, borderRadius: 12, backgroundColor: "rgba(255,255,255,0.08)", alignItems: "center" }}
      >
        <Text style={{ color: "#22d3ee" }}>🔄 刷新待签</Text>
      </Pressable>
    </ScrollView>
  );
}
