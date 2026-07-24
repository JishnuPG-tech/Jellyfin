import { View, Text, TextInput, TouchableOpacity, ScrollView, StyleSheet } from "react-native";
import { useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { useState, useEffect } from "react";
import { ArrowLeft, Check, Shield, Globe, Key, Lock } from "lucide-react-native";
import { getHealth } from "../lib/api";
import {
  getServerUrl,
  setServerUrl as persistUrl,
  getAuthType,
  setAuthType,
  getAuthValue,
  setAuthValue,
  clearAuth,
  getTheme,
  setTheme as persistTheme,
} from "../lib/storage";
import type { AuthType } from "../lib/storage";
import { themes } from "../constants/themes";
import { ThemeName } from "../constants/types";

const themeNames = Object.keys(themes) as ThemeName[];

const authTypes: { type: AuthType; label: string; description: string; icon: typeof Shield }[] = [
  { type: "none", label: "None", description: "No authentication", icon: Globe },
  { type: "basic", label: "Basic Auth", description: "Username & password", icon: Lock },
  { type: "bearer", label: "Bearer Token", description: "Authorization header", icon: Key },
  { type: "apikey", label: "API Key", description: "X-API-Key header", icon: Shield },
];

export default function SettingsScreen() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const currentTheme = getTheme();
  const theme = themes[currentTheme];

  const [serverInput, setServerInput] = useState(getServerUrl());
  const [authType, setAuthTypeState] = useState<AuthType>(getAuthType());
  const [authValue, setAuthValueState] = useState(getAuthValue());
  const [connected, setConnected] = useState<boolean | null>(null);
  const [serverVersion, setServerVersion] = useState<string | null>(null);

  // Basic auth split
  const [basicUser, setBasicUser] = useState(() => {
    const val = getAuthValue();
    return getAuthType() === "basic" && val.includes(":") ? val.split(":")[0] : "";
  });
  const [basicPass, setBasicPass] = useState(() => {
    const val = getAuthValue();
    return getAuthType() === "basic" && val.includes(":") ? val.split(":").slice(1).join(":") : "";
  });

  const testConnection = async () => {
    persistUrl(serverInput);

    // Save auth
    if (authType === "basic") {
      setAuthValue(`${basicUser}:${basicPass}`);
    } else {
      setAuthValue(authValue);
    }
    setAuthType(authType);

    try {
      const health = await getHealth();
      setConnected(health.healthy);
      setServerVersion(health.version || null);
    } catch {
      setConnected(false);
      setServerVersion(null);
    }
  };

  const handleThemeSelect = (name: ThemeName) => {
    persistTheme(name);
  };

  return (
    <View style={[styles.container, { backgroundColor: theme.colors.bg }]}>
      <View style={[styles.header, { paddingTop: insets.top + 12, borderBottomColor: theme.colors.border }]}>
        <TouchableOpacity onPress={() => router.back()} style={styles.backBtn}>
          <ArrowLeft size={20} color={theme.colors.text} />
        </TouchableOpacity>
        <Text style={[styles.title, { color: theme.colors.text }]}>Settings</Text>
      </View>

      <ScrollView
        contentContainerStyle={{ padding: 20, paddingBottom: insets.bottom + 40 }}
        showsVerticalScrollIndicator={false}
      >
        {/* Server */}
        <Text style={[styles.sectionLabel, { color: theme.colors.textSecondary }]}>Server</Text>
        <View style={[styles.card, { backgroundColor: theme.colors.surface, borderColor: theme.colors.border }]}>
          <Text style={[styles.label, { color: theme.colors.textMuted }]}>Server URL</Text>
          <TextInput
            style={[styles.input, { color: theme.colors.text, backgroundColor: theme.colors.bgTertiary, borderColor: theme.colors.border }]}
            value={serverInput}
            onChangeText={setServerInput}
            placeholder="http://127.0.0.1:4096"
            placeholderTextColor={theme.colors.textMuted}
            autoCapitalize="none"
            autoCorrect={false}
          />

          {/* Auth Type */}
          <Text style={[styles.label, { color: theme.colors.textMuted, marginTop: 8 }]}>Authentication</Text>
          <View style={styles.authGrid}>
            {authTypes.map((a) => {
              const Icon = a.icon;
              const selected = authType === a.type;
              return (
                <TouchableOpacity
                  key={a.type}
                  style={[
                    styles.authCard,
                    {
                      backgroundColor: selected ? theme.colors.bgTertiary : theme.colors.bg,
                      borderColor: selected ? theme.colors.primary : theme.colors.border,
                    },
                  ]}
                  onPress={() => setAuthTypeState(a.type)}
                >
                  <Icon size={16} color={selected ? theme.colors.primary : theme.colors.textMuted} />
                  <Text style={[styles.authLabel, { color: selected ? theme.colors.text : theme.colors.textMuted }]}>
                    {a.label}
                  </Text>
                </TouchableOpacity>
              );
            })}
          </View>

          {/* Auth Value Input */}
          {authType === "basic" && (
            <View style={{ gap: 8, marginTop: 8 }}>
              <TextInput
                style={[styles.input, { color: theme.colors.text, backgroundColor: theme.colors.bgTertiary, borderColor: theme.colors.border }]}
                value={basicUser}
                onChangeText={setBasicUser}
                placeholder="Username"
                placeholderTextColor={theme.colors.textMuted}
                autoCapitalize="none"
              />
              <TextInput
                style={[styles.input, { color: theme.colors.text, backgroundColor: theme.colors.bgTertiary, borderColor: theme.colors.border }]}
                value={basicPass}
                onChangeText={setBasicPass}
                placeholder="Password"
                placeholderTextColor={theme.colors.textMuted}
                secureTextEntry
              />
            </View>
          )}
          {authType === "bearer" && (
            <TextInput
              style={[styles.input, { color: theme.colors.text, backgroundColor: theme.colors.bgTertiary, borderColor: theme.colors.border, marginTop: 8 }]}
              value={authValue}
              onChangeText={setAuthValueState}
              placeholder="Bearer token"
              placeholderTextColor={theme.colors.textMuted}
              autoCapitalize="none"
              secureTextEntry
            />
          )}
          {authType === "apikey" && (
            <TextInput
              style={[styles.input, { color: theme.colors.text, backgroundColor: theme.colors.bgTertiary, borderColor: theme.colors.border, marginTop: 8 }]}
              value={authValue}
              onChangeText={setAuthValueState}
              placeholder="API key"
              placeholderTextColor={theme.colors.textMuted}
              autoCapitalize="none"
              secureTextEntry
            />
          )}

          <TouchableOpacity style={[styles.btn, { backgroundColor: theme.colors.primary }]} onPress={testConnection}>
            <Text style={[styles.btnText, { color: theme.colors.primaryText }]}>Test Connection</Text>
          </TouchableOpacity>
          {connected !== null && (
            <Text style={[styles.status, { color: connected ? theme.colors.success : theme.colors.danger }]}>
              {connected ? `Connected${serverVersion ? ` (v${serverVersion})` : ""}` : "Failed to connect"}
            </Text>
          )}
        </View>

        {/* Theme */}
        <Text style={[styles.sectionLabel, { color: theme.colors.textSecondary, marginTop: 28 }]}>Theme</Text>
        <View style={styles.themeGrid}>
          {themeNames.map((name) => {
            const t = themes[name];
            const selected = currentTheme === name;
            return (
              <TouchableOpacity
                key={name}
                style={[
                  styles.themeCard,
                  {
                    backgroundColor: t.colors.bg,
                    borderColor: selected ? theme.colors.primary : t.colors.border,
                    borderWidth: selected ? 2 : 1,
                  },
                ]}
                onPress={() => handleThemeSelect(name)}
              >
                <View style={styles.themePreview}>
                  <View style={[styles.dot, { backgroundColor: t.colors.primary }]} />
                  <View style={[styles.dot, { backgroundColor: t.colors.secondary }]} />
                  <View style={[styles.dot, { backgroundColor: t.colors.danger }]} />
                </View>
                <Text style={[styles.themeName, { color: t.colors.text }]}>{t.label}</Text>
                {selected && <Check size={14} color={theme.colors.primary} />}
              </TouchableOpacity>
            );
          })}
        </View>

        {/* About */}
        <Text style={[styles.sectionLabel, { color: theme.colors.textSecondary, marginTop: 28 }]}>About</Text>
        <View style={[styles.card, { backgroundColor: theme.colors.surface, borderColor: theme.colors.border }]}>
          <Text style={[styles.aboutText, { color: theme.colors.text }]}>OpenCode Mobile</Text>
          <Text style={[styles.aboutVersion, { color: theme.colors.textMuted }]}>v1.0.0</Text>
        </View>
      </ScrollView>
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
    borderBottomWidth: 1,
  },
  backBtn: { padding: 4 },
  title: { fontSize: 28, fontWeight: "700", letterSpacing: -0.5 },
  sectionLabel: { fontSize: 13, fontWeight: "600", textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 10 },
  card: { padding: 16, borderRadius: 12, borderWidth: 1, gap: 10 },
  label: { fontSize: 13, fontWeight: "500" },
  input: {
    fontSize: 14,
    paddingHorizontal: 12,
    paddingVertical: 10,
    borderRadius: 8,
    borderWidth: 1,
  },
  btn: { paddingVertical: 12, borderRadius: 10, alignItems: "center", marginTop: 4 },
  btnText: { fontSize: 14, fontWeight: "600" },
  status: { fontSize: 13, textAlign: "center" },
  authGrid: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 8,
  },
  authCard: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 12,
    paddingVertical: 8,
    borderRadius: 8,
    borderWidth: 1,
  },
  authLabel: { fontSize: 13, fontWeight: "500" },
  themeGrid: { gap: 8 },
  themeCard: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    padding: 14,
    borderRadius: 10,
  },
  themePreview: { flexDirection: "row", gap: 4 },
  dot: { width: 10, height: 10, borderRadius: 5 },
  themeName: { flex: 1, fontSize: 14, fontWeight: "500" },
  aboutText: { fontSize: 15, fontWeight: "600" },
  aboutVersion: { fontSize: 13 },
});
