import { View, Text, TouchableOpacity, ScrollView, StyleSheet } from "react-native";
import { useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { Plus, Wifi, WifiOff, Settings } from "lucide-react-native";
import { useSessions, useCreateSession } from "../../hooks/useApi";
import { useConnection } from "../../hooks/useConnection";
import { getTheme } from "../../lib/storage";
import { themes } from "../../constants/themes";

export default function HomeScreen() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const theme = themes[getTheme()];
  const { isConnected, state: connState } = useConnection();
  const { data: sessions } = useSessions();
  const createSession = useCreateSession();

  const handleNewSession = async () => {
    try {
      const session = await createSession.mutateAsync({ title: "New Chat", directory: "default" });
      router.push(`/session/${session.id}`);
    } catch {}
  };

  const recentSessions = (sessions || []).slice(0, 5);

  return (
    <View style={[styles.container, { backgroundColor: theme.colors.bg }]}>
      <View style={[styles.header, { paddingTop: insets.top + 12 }]}>
        <Text style={[styles.title, { color: theme.colors.text }]}>OpenCode</Text>
        <TouchableOpacity onPress={() => router.push("/settings")}>
          <Settings size={22} color={theme.colors.textMuted} />
        </TouchableOpacity>
      </View>

      <ScrollView
        style={styles.scroll}
        contentContainerStyle={{ paddingBottom: insets.bottom + 100 }}
        showsVerticalScrollIndicator={false}
      >
        <View
          style={[
            styles.statusCard,
            {
              backgroundColor: isConnected ? theme.colors.bgTertiary : theme.colors.surface,
              borderColor: isConnected ? theme.colors.success : theme.colors.danger,
            },
          ]}
        >
          {isConnected ? (
            <Wifi size={18} color={theme.colors.success} />
          ) : (
            <WifiOff size={18} color={theme.colors.danger} />
          )}
          <Text style={[styles.statusText, { color: theme.colors.text }]}>
            {isConnected ? "Connected" : connState === "reconnecting" ? "Reconnecting..." : "Disconnected"}
          </Text>
        </View>

        <TouchableOpacity
          style={[styles.newSessionBtn, { backgroundColor: theme.colors.primary }]}
          onPress={handleNewSession}
          activeOpacity={0.8}
        >
          <Plus size={20} color={theme.colors.primaryText} />
          <Text style={[styles.newSessionText, { color: theme.colors.primaryText }]}>
            New Session
          </Text>
        </TouchableOpacity>

        {recentSessions.length > 0 && (
          <View style={styles.section}>
            <Text style={[styles.sectionTitle, { color: theme.colors.textSecondary }]}>
              Recent Sessions
            </Text>
            {recentSessions.map((s) => (
              <TouchableOpacity
                key={s.id}
                style={[styles.sessionItem, { backgroundColor: theme.colors.surface }]}
                onPress={() => router.push(`/session/${s.id}`)}
              >
                <Text style={[styles.sessionTitle, { color: theme.colors.text }]} numberOfLines={1}>
                  {s.title || "Untitled"}
                </Text>
                <Text style={[styles.sessionTime, { color: theme.colors.textMuted }]}>
                  {new Date(s.updatedAt).toLocaleDateString()}
                </Text>
              </TouchableOpacity>
            ))}
          </View>
        )}
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  header: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    paddingHorizontal: 20,
    paddingBottom: 12,
  },
  title: { fontSize: 28, fontWeight: "700", letterSpacing: -0.5 },
  scroll: { flex: 1, paddingHorizontal: 20 },
  statusCard: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    padding: 14,
    borderRadius: 12,
    marginBottom: 20,
    borderWidth: 1,
  },
  statusText: { fontSize: 14, fontWeight: "500" },
  newSessionBtn: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
    padding: 16,
    borderRadius: 14,
    marginBottom: 28,
  },
  newSessionText: { fontSize: 16, fontWeight: "600" },
  section: { marginBottom: 24 },
  sectionTitle: { fontSize: 13, fontWeight: "600", textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 10 },
  sessionItem: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    padding: 14,
    borderRadius: 10,
    marginBottom: 6,
  },
  sessionTitle: { fontSize: 15, fontWeight: "500", flex: 1, marginRight: 10 },
  sessionTime: { fontSize: 12 },
});
