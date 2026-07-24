import { View, Text, TextInput, FlatList, StyleSheet } from "react-native";
import { useState } from "react";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { Search as SearchIcon } from "lucide-react-native";
import { useFindFiles } from "../../hooks/useApi";
import { getTheme } from "../../lib/storage";
import { themes } from "../../constants/themes";

export default function SearchScreen() {
  const insets = useSafeAreaInsets();
  const theme = themes[getTheme()];
  const [query, setQuery] = useState("");
  const { data: results, isLoading } = useFindFiles(query);

  return (
    <View style={[styles.container, { backgroundColor: theme.colors.bg }]}>
      <View style={[styles.header, { paddingTop: insets.top + 12 }]}>
        <Text style={[styles.title, { color: theme.colors.text }]}>Search</Text>
      </View>

      <View style={[styles.searchBar, { backgroundColor: theme.colors.surface, borderColor: theme.colors.border }]}>
        <SearchIcon size={16} color={theme.colors.textMuted} />
        <TextInput
          style={[styles.searchInput, { color: theme.colors.text }]}
          placeholder="Search files, code, text..."
          placeholderTextColor={theme.colors.textMuted}
          value={query}
          onChangeText={setQuery}
          autoFocus
        />
      </View>

      <FlatList
        data={results || []}
        keyExtractor={(_, i) => String(i)}
        contentContainerStyle={{ paddingBottom: insets.bottom + 100, paddingHorizontal: 20 }}
        showsVerticalScrollIndicator={false}
        ListEmptyComponent={
          <View style={styles.empty}>
            <SearchIcon size={40} color={theme.colors.textMuted} />
            <Text style={[styles.emptyText, { color: theme.colors.textMuted }]}>
              {query.length === 0
                ? "Type to search"
                : isLoading
                ? "Searching..."
                : "No results"}
            </Text>
          </View>
        }
        renderItem={({ item }) => (
          <View style={[styles.resultCard, { backgroundColor: theme.colors.surface }]}>
            <View style={styles.resultHeader}>
              <Text style={[styles.resultFile, { color: theme.colors.primary }]} numberOfLines={1}>
                {item.file}
              </Text>
              <Text style={[styles.resultLine, { color: theme.colors.textMuted }]}>
                L{item.line}
              </Text>
            </View>
            <Text style={[styles.resultMatch, { color: theme.colors.text }]} numberOfLines={2}>
              {item.context}
            </Text>
          </View>
        )}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  header: {
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
  resultCard: {
    padding: 12,
    borderRadius: 10,
    marginBottom: 6,
  },
  resultHeader: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: 4,
  },
  resultFile: { fontSize: 13, fontWeight: "500", flex: 1, marginRight: 8 },
  resultLine: { fontSize: 12 },
  resultMatch: { fontSize: 13 },
  empty: { alignItems: "center", marginTop: 80, gap: 12 },
  emptyText: { fontSize: 14 },
});
