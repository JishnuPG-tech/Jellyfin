import { View, Text, TextInput, TouchableOpacity, FlatList, Alert, StyleSheet } from "react-native";
import { useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { Plus, Search, Trash2, MessageSquare } from "lucide-react-native";
import { useState } from "react";
import { useSessions, useCreateSession, useDeleteSession } from "../../hooks/useApi";
import { getTheme } from "../../lib/storage";
import { themes } from "../../constants/themes";

export default function SessionsScreen() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const theme = themes[getTheme()];
  const { data: sessions, isLoading } = useSessions();
  const createSession = useCreateSession();
  const deleteSession = useDeleteSession();
  const [search, setSearch] = useState("");

  const filtered = (sessions || []).filter(
    (s) => !search || (s.title || "").toLowerCase().includes(search.toLowerCase())
  );

  const handleCreate = async () => {
    try {
      const session = await createSession.mutateAsync({ title: "New Chat", directory: "default" });
      router.push(`/session/${session.id}`);
    } catch {}
  };

  const handleDelete = (id: string, title: string) => {
    Alert.alert("Delete Session", `Delete "${title || "Untitled"}"?`, [
      { text: "Cancel", style: "cancel" },
      { text: "Delete", style: "destructive", onPress: () => deleteSession.mutateAsync(id) },
    ]);
  };

  return (
    <View style={[styles.container, { backgroundColor: theme.colors.bg }]}>
      <View style={[styles.header, { paddingTop: insets.top + 12 }]}>
        <Text style={[styles.title, { color: theme.colors.text }]}>Sessions</Text>
        <TouchableOpacity onPress={handleCreate}>
          <Plus size={22} color={theme.colors.primary} />
        </TouchableOpacity>
      </View>

      <View style={[styles.searchBar, { backgroundColor: theme.colors.surface, borderColor: theme.colors.border }]}>
        <Search size={16} color={theme.colors.textMuted} />
        <TextInput
          style={[styles.searchInput, { color: theme.colors.text }]}
          placeholder="Search sessions..."
          placeholderTextColor={theme.colors.textMuted}
          value={search}
          onChangeText={setSearch}
        />
      </View>

      <FlatList
        data={filtered}
        keyExtractor={(item) => item.id}
        contentContainerStyle={{ paddingBottom: insets.bottom + 100, paddingHorizontal: 20 }}
        showsVerticalScrollIndicator={false}
        ListEmptyComponent={
          <View style={styles.empty}>
            <MessageSquare size={40} color={theme.colors.textMuted} />
            <Text style={[styles.emptyText, { color: theme.colors.textMuted }]}>
              {isLoading ? "Loading..." : "No sessions yet"}
            </Text>
          </View>
        }
        renderItem={({ item }) => (
          <TouchableOpacity
            style={[styles.sessionCard, { backgroundColor: theme.colors.surface }]}
            onPress={() => router.push(`/session/${item.id}`)}
          >
            <View style={styles.sessionInfo}>
              <Text style={[styles.sessionTitle, { color: theme.colors.text }]} numberOfLines={1}>
                {item.title || "Untitled"}
              </Text>
              <Text style={[styles.sessionDate, { color: theme.colors.textMuted }]}>
                {new Date(item.updatedAt).toLocaleString()}
              </Text>
            </View>
            <TouchableOpacity onPress={() => handleDelete(item.id, item.title)}>
              <Trash2 size={16} color={theme.colors.textMuted} />
            </TouchableOpacity>
          </TouchableOpacity>
        )}
      />
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
  searchBar: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    marginHorizontal: 20,
    marginBottom: 16,
    paddingHorizontal: 14,
    paddingVertical: 10,
    borderRadius: 10,
    borderWidth: 1,
  },
  searchInput: { flex: 1, fontSize: 14, padding: 0 },
  sessionCard: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    padding: 14,
    borderRadius: 10,
    marginBottom: 6,
  },
  sessionInfo: { flex: 1, marginRight: 12 },
  sessionTitle: { fontSize: 15, fontWeight: "500" },
  sessionDate: { fontSize: 12, marginTop: 2 },
  empty: { alignItems: "center", marginTop: 80, gap: 12 },
  emptyText: { fontSize: 14 },
});
