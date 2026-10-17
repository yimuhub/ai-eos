import { Tabs } from "expo-router";
import { AieosProvider, useAieos } from "../store";
import { Text } from "react-native";

// 深色玻璃拟态设计体系（与 ai-eos UI_DESIGN_SYSTEM 对齐）
const C = {
  bg: "#0b0e14",
  card: "rgba(255,255,255,0.06)",
  border: "rgba(255,255,255,0.10)",
  fg: "#e8ecf4",
  muted: "#8a93a6",
  accent: "#6366f1",
  cyan: "#22d3ee",
};

export { C };

function Root() {
  const { loggedIn } = useAieos();
  return (
    <Tabs
      screenOptions={{
        headerStyle: { backgroundColor: C.bg },
        tabBarStyle: { backgroundColor: "#0d1117", borderTopColor: C.border },
        headerTintColor: C.fg,
        tabBarActiveTintColor: C.cyan,
        contentStyle: { backgroundColor: C.bg },
      }}
    >
      <Tabs.Screen
        name="index"
        options={{ title: "KPI", tabBarIcon: ({ color }: { color: string }) => <Text style={{ fontSize: 18 }}>📊</Text> }}
      />
      <Tabs.Screen
        name="command"
        options={{ title: "一句话", tabBarIcon: ({ color }: { color: string }) => <Text style={{ fontSize: 18 }}>⚡</Text> }}
      />
      <Tabs.Screen
        name="approve"
        options={{ title: "签字门", tabBarIcon: ({ color }: { color: string }) => <Text style={{ fontSize: 18 }}>✍️</Text> }}
      />
      <Tabs.Screen
        name="server"
        options={{ title: "连接", tabBarIcon: ({ color }: { color: string }) => <Text style={{ fontSize: 18 }}>🔌</Text> }}
      />
    </Tabs>
  );
}

export default function Layout() {
  return (
    <AieosProvider>
      <Root />
    </AieosProvider>
  );
}
