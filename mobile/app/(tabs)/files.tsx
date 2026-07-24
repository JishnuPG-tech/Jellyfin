import { View, Text, TextInput, TouchableOpacity, FlatList, StyleSheet } from "react-native";
import { useState } from "react";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { Search, ChevronRight, Folder, File, ArrowLeft } from "lucide-react-native";
import { useListFiles } from "../../hooks/useApi";
import { getTheme } from "../../lib/storage";
import { themes } from "../../constants/themes";

export default function FilesScreen() {
  const insets = useSafeAreaInsets();
  const theme = themes[getTheme()];
  const [currentDir, setCurrentDir] = useState(".");
  const [search, setSearch] = useState("");
  const { data: files, isLoading } = useListFiles(currentDir);

  const filtered = (files || []).filter(
    (f) => !search || f.name.toLowerCase().includes(search.toLowerCase())
  );

  const dirs = currentDir === "." ? [] : currentDir.split("/");
  const goBack = () => {
    if (dirs.length <= 1) {
      setCurrentDir(".");
    } else {
      setCurrentDir(dirs.slice(0, -1).join("/"));
    }
  };

  return (
    <View style={[styles.container, { backgroundColor: theme.colors.bg }]}>
      <View style={[styles.header, { paddingTop: insets.top + 12 }]}>
        {currentDir !== "." && (
          <TouchableOpacity onPress={goBack} style={styles.backBtn}>
            <ArrowLeft size={20} color={theme.colors.text} />
          </TouchableOpacity>
        )}
        <Text style={[styles.title, { color: theme.colors.text }]} numberOfLines={1}>
          {currentDir === "." ? "Files" : dirs[dirs.length - 1]}
        </Text>
      </View>

      <View style={styles.breadcrumb}>
        {dirs.map((d, i) => (
          <TouchableOpacity
            key={i}
            onPress={() => setCurrentDir(dirs.slice(0, i + 1).join("/"))}
            style={styles.breadcrumbItem}
          >
            <Text style={[styles.breadcrumbText, { color: theme.colors.primary }]}>{d}</Text>
            <ChevronRight size={12} color={theme.colors.textMuted} />
          </TouchableOpacity>
        ))}
        {dirs.length === 0 && (
          <Text style={[styles.breadcrumbText, { color: theme.colors.textMuted }]}>root</Text>
        )}
      </View>

      <View style={[styles.searchBar, { backgroundColor: theme.colors.surface, borderColor: theme.colors.border }]}>
        <Search size={16} color={theme.colors.textMuted} />
        <TextInput
          style={[styles.searchInput, { color: theme.colors.text }]}
          placeholder="Filter files..."
          placeholderTextColor={theme.colors.textMuted}
          value={search}
          onChangeText={setSearch}
        />
      </View>

      <FlatList
        data={filtered}
        keyExtractor={(item) => item.path}
        contentContainerStyle={{ paddingBottom: insets.bottom + 100, paddingHorizontal: 20 }}
        showsVerticalScrollIndicator={false}
        ListEmptyComponent={
          <View style={styles.empty}>
            <Folder size={40} color={theme.colors.textMuted} />
            <Text style={[styles.emptyText, { color: theme.colors.textMuted }]}>
              {isLoading ? "Loading..." : "Empty"}
            </Text>
          </View>
        }
        renderItem={({ item }) => (
          <TouchableOpacity
            style={[styles.fileItem, { backgroundColor: theme.colors.surface }]}
            onPress={() => {
              if (item.type === "directory") {
                setCurrentDir(item.path);
              }
            }}
          >
            {item.type === "directory" ? (
              <Folder size={18} color={theme.colors.warning} />
            ) : (
              <File size={18} color={theme.colors.textMuted} />
            )}
            <Text style={[styles.fileName, { color: theme.colors.text }]} numberOfLines={1}>
              {item.name}
            </Text>
            {item.type === "directory" && (
              <ChevronRight size={16} color={theme.colors.textMuted} />
            )}
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
    alignItems: "center",
    gap: 10,
    paddingHorizontal: 20,
    paddingBottom: 12,
  },
  backBtn: { padding: 4 },
  title: { fontSize: 28, fontWeight: "700", letterSpacing: -0.5, flex: 1 },
  breadcrumb: {
    flexDirection: "row",
    alignItems: "center",
    flexWrap: "wrap",
    paddingHorizontal: 20,
    marginBottom: 12,
    gap: 4,
  },
  breadcrumbItem: { flexDirection: "row", alignItems: "center", gap: 4 },
  breadcrumbText: { fontSize: 13 },
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
  fileItem: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    padding: 12,
    borderRadius: 10,
    marginBottom: 4,
  },
  fileName: { flex: 1, fontSize: 14 },
  empty: { alignItems: "center", marginTop: 80, gap: 12 },
  emptyText: { fontSize: 14 },
});
