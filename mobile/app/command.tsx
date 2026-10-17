import { useState } from "react";
import { View, Text, TextInput, Pressable, ScrollView, ActivityIndicator, Alert } from "react-native";
import { useAieos } from "../store";
import type { Plan } from "../lib/api";

// 一句话下达：输入 → AI 出方案 → 老板确认/改 → 执行 → 报告
// 设计原则（产品设计哲学）：每一步有老板可视化确认点，绝不自动生效。
export default function Command() {
  const { api, svc, loggedIn } = useAieos();
  const [phrase, setPhrase] = useState("");
  const [attach, setAttach] = useState("");
  const [plan, setPlan] = useState<Plan | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<any>(null);
  const [err, setErr] = useState("");

  const doPlan = async () => {
    if (!loggedIn) return Alert.alert("请先登录", "顶部 KPI 页需登录后才能操作。");
    if (!phrase.trim()) return;
    setBusy(true);
    setErr("");
    setResult(null);
    try {
      const p = await api.plan(phrase.trim(), svc.companyId, attach.trim() || undefined);
      setPlan(p);
    } catch (e: any) {
      setErr(e?.message || "生成方案失败");
    } finally {
      setBusy(false);
    }
  };

  const doExecute = async () => {
    if (!plan) return;
    setBusy(true);
    setErr("");
    try {
      const r = await api.execute(plan, svc.companyId);
      setResult(r);
      setPlan(null);
      setPhrase("");
    } catch (e: any) {
      setErr(e?.message || "执行失败");
    } finally {
      setBusy(false);
    }
  };

  const pathBadge = (p: Plan) => {
    const m: Record<string, [string, string]> = {
      DIRECT: ["直接派活", "⚡"],
      EXPERT: ["专家组队", "👥"],
      PROJECT: ["作战室立项", "📋"],
    };
    const [label, emoji] = m[p.path] || [p.path, "❓"];
    return `${emoji} ${label}`;
  };

  return (
    <ScrollView contentContainerStyle={{ padding: 16, gap: 14 }}>
      <View>
        <Text style={{ fontSize: 22, fontWeight: 700, color: "#e8ecf4" }}>一句话指挥台</Text>
        <Text style={{ color: "#8a93a6", fontSize: 13, marginTop: 4 }}>
          说人话就行，AI 自动判断该直接派活、组队还是立项
        </Text>
      </View>

      <TextInput
        value={phrase}
        onChangeText={setPhrase}
        placeholder="例：帮我策划一场新品发布会"
        placeholderTextColor="#5a6272"
        style={{
          minHeight: 80,
          padding: 14,
          borderRadius: 14,
          backgroundColor: "rgba(255,255,255,0.06)",
          color: "#e8ecf4",
          fontSize: 15,
        }}
        multiline
      />

      {/* 硬性要求：输入页必带📎附件口 */}
      <Pressable
        onPress={() => {
          // 移动端附件口：当前用文本占位（图片选择器在 V4.2 接 RN ImagePicker）
          setAttach((a) => a || "📎 待上传参考材料");
          Alert.alert("附件口", "📎 参考材料入口已挂上。图片/文件选择器随 V4.2 接 ImagePicker。");
        }}
        style={{
          padding: 12,
          borderRadius: 12,
          backgroundColor: "rgba(99,102,241,0.14)",
          borderColor: "rgba(99,102,241,0.4)",
          borderWidth: 1,
        }}
      >
        <Text style={{ color: "#a5b4fc" }}>
          {attach ? `📎 ${attach}` : "📎 附参考材料（可选）"}
        </Text>
      </Pressable>

      <Pressable
        onPress={doPlan}
        style={{
          padding: 16,
          borderRadius: 14,
          backgroundColor: "linear-gradient(90deg,#6366f1,#22d3ee)" as any,
          alignItems: "center",
        }}
      >
        {busy ? <ActivityIndicator color="#fff" /> : (
          <Text style={{ color: "#fff", fontWeight: 700, fontSize: 16 }}>⚡ 出方案</Text>
        )}
      </Pressable>

      {err ? (
        <View style={{ padding: 14, backgroundColor: "rgba(239,68,68,0.12)", borderRadius: 12 }}>
          <Text style={{ color: "#fca5a5" }}>{err}</Text>
        </View>
      ) : null}

      {/* 方案确认：老板可视化确认点，可改可拒 */}
      {plan ? (
        <View style={{ padding: 16, borderRadius: 14, backgroundColor: "rgba(255,255,255,0.06)", gap: 10 }}>
          <Text style={{ color: "#22d3ee", fontWeight: 700 }}>{pathBadge(plan)}</Text>
          {plan.team_goal ? (
            <Text style={{ color: "#e8ecf4" }}>
              <Text style={{ color: "#8a93a6" }}>目标：</Text>
              {plan.team_goal}
            </Text>
          ) : null}
          {plan.est_minutes ? (
            <Text style={{ color: "#8a93a6", fontSize: 13 }}>⏱ 预计 {plan.est_minutes} 分钟</Text>
          ) : null}
          {plan.targets?.length ? (
            <View style={{ gap: 6 }}>
              <Text style={{ color: "#8a93a6", fontSize: 12, marginTop: 4 }}>分工（{plan.targets.length}）：</Text>
              {plan.targets.map((t, i) => (
                <Text key={i} style={{ color: "#e8ecf4", fontSize: 13, marginLeft: 8 }}>
                  • {t.agent_name || t.agent_id}：{(t.task || "").slice(0, 40)}
                </Text>
              ))}
            </View>
          ) : null}
          <View style={{ flexDirection: "row", gap: 10, marginTop: 6 }}>
            <Pressable
              onPress={doExecute}
              style={{ flex: 1, padding: 12, borderRadius: 10, backgroundColor: "#22c55e", alignItems: "center" }}
            >
              {busy ? <ActivityIndicator color="#fff" /> : <Text style={{ color: "#fff", fontWeight: 700 }}>✅ 确认执行</Text>}
            </Pressable>
            <Pressable
              onPress={() => setPlan(null)}
              style={{ flex: 1, padding: 12, borderRadius: 10, backgroundColor: "rgba(255,255,255,0.1)", alignItems: "center" }}
            >
              <Text style={{ color: "#8a93a6", fontWeight: 600 }}>✕ 重改</Text>
            </Pressable>
          </View>
        </View>
      ) : null}

      {/* 执行结果控制台 */}
      {result ? (
        <View style={{ padding: 16, borderRadius: 14, backgroundColor: "rgba(34,197,94,0.10)", gap: 8 }}>
          <Text style={{ color: "#86efac", fontWeight: 700 }}>✅ 已下达</Text>
          <Text style={{ color: "#8a93a6", fontSize: 13 }}>{result.path} · 完成 {result.jobs?.length ?? 0} 项分派</Text>
          <Pressable
            onPress={async () => {
              try {
                const ref = result.session_id || result.jobs?.[0]?.job_id || result.jobs?.[0]?.tasks?.[0]?.task_id;
                if (!ref) return Alert.alert("提示", "本次执行没有可查报告的引用。");
                const r = await api.report(ref, svc.companyId);
                Alert.alert("执行报告", r.title || JSON.stringify(r).slice(0, 400));
              } catch (e: any) {
                Alert.alert("报告", e?.message || "获取报告失败");
              }
            }}
            style={{
              padding: 10,
              borderRadius: 10,
              backgroundColor: "rgba(255,255,255,0.08)",
              alignItems: "center",
            }}
          >
            <Text style={{ color: "#22d3ee" }}>📄 查看执行报告</Text>
          </Pressable>
        </View>
      ) : null}
    </ScrollView>
  );
}
