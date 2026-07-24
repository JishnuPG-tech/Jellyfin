import React from "react";
import { View, Text, TouchableOpacity, StyleSheet } from "react-native";
import { Shield, Check, X } from "lucide-react-native";
import { themes } from "../constants/themes";
import { getTheme } from "../lib/storage";
import type { PermissionRequest } from "../constants/types";

interface PermissionCardProps {
  request: PermissionRequest;
  onAllow: () => void;
  onDeny: () => void;
}

export function PermissionCard({ request, onAllow, onDeny }: PermissionCardProps) {
  const theme = themes[getTheme()];

  return (
    <View style={[styles.card, { backgroundColor: theme.colors.surface, borderColor: theme.colors.border }]}>
      <View style={styles.header}>
        <Shield size={16} color={theme.colors.warning} />
        <Text style={[styles.title, { color: theme.colors.text }]}>Permission Required</Text>
      </View>

      <Text style={[styles.permission, { color: theme.colors.textSecondary }]}>
        {request.permission}
      </Text>

      {request.description ? (
        <Text style={[styles.description, { color: theme.colors.textMuted }]}>
          {request.description}
        </Text>
      ) : null}

      <View style={styles.actions}>
        <TouchableOpacity
          style={[styles.denyBtn, { backgroundColor: theme.colors.bgTertiary, borderColor: theme.colors.border }]}
          onPress={onDeny}
          activeOpacity={0.7}
        >
          <X size={16} color={theme.colors.danger} />
          <Text style={[styles.denyText, { color: theme.colors.danger }]}>Deny</Text>
        </TouchableOpacity>

        <TouchableOpacity
          style={[styles.allowBtn, { backgroundColor: theme.colors.success }]}
          onPress={onAllow}
          activeOpacity={0.7}
        >
          <Check size={16} color="#fff" />
          <Text style={[styles.allowText, { color: "#fff" }]}>Allow</Text>
        </TouchableOpacity>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    padding: 16,
    borderRadius: 12,
    borderWidth: 1,
    gap: 10,
  },
  header: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  title: {
    fontSize: 15,
    fontWeight: "600",
  },
  permission: {
    fontSize: 14,
    fontWeight: "500",
  },
  description: {
    fontSize: 13,
    lineHeight: 18,
  },
  actions: {
    flexDirection: "row",
    gap: 10,
    marginTop: 4,
  },
  denyBtn: {
    flex: 1,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 6,
    paddingVertical: 10,
    borderRadius: 8,
    borderWidth: 1,
  },
  denyText: {
    fontSize: 14,
    fontWeight: "600",
  },
  allowBtn: {
    flex: 1,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 6,
    paddingVertical: 10,
    borderRadius: 8,
  },
  allowText: {
    fontSize: 14,
    fontWeight: "600",
  },
});
