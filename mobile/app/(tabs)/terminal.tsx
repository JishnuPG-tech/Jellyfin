import { View, Text, StyleSheet } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { Terminal } from "lucide-react-native";
import { getTheme } from "../../lib/storage";
import { themes } from "../../constants/themes";

export default function TerminalScreen() {
  const insets = useSafeAreaInsets();
  const theme = themes[getTheme()];

  return (
    <View style={[styles.container, { backgroundColor: theme.colors.bg }]}>
      <View style={[styles.header, { paddingTop: insets.top + 12 }]}>
        <Text style={[styles.title, { color: theme.colors.text }]}>Terminal</Text>
      </View>
      <View style={styles.empty}>
        <Terminal size={48} color={theme.colors.textMuted} />
        <Text style={[styles.emptyTitle, { color: theme.colors.text }]}>Terminal</Text>
        <Text style={[styles.emptyText, { color: theme.colors.textMuted }]}>
          Terminal sessions will appear here.
        </Text>
      </View>
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
  empty: { flex: 1, alignItems: "center", justifyContent: "center", gap: 12 },
  emptyTitle: { fontSize: 18, fontWeight: "600" },
  emptyText: { fontSize: 14 },
});
