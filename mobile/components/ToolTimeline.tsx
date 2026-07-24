import React from "react";
import { View, Text, StyleSheet } from "react-native";
import { Loader, Check, X, FileText, Search, Terminal, Globe, Wrench } from "lucide-react-native";
import { themes } from "../constants/themes";
import { getTheme } from "../lib/storage";
import type { ToolPart } from "../constants/types";

interface ToolTimelineProps {
  parts: ToolPart[];
}

const toolIcons: Record<string, typeof Wrench> = {
  read: FileText,
  write: FileText,
  edit: FileText,
  glob: Search,
  grep: Search,
  bash: Terminal,
  webfetch: Globe,
  websearch: Globe,
};

const toolLabels: Record<string, string> = {
  read: "Reading file",
  write: "Writing file",
  edit: "Editing file",
  glob: "Searching files",
  grep: "Searching content",
  bash: "Running command",
  webfetch: "Fetching URL",
  websearch: "Searching web",
};

export function ToolTimeline({ parts }: ToolTimelineProps) {
  const theme = themes[getTheme()];

  if (parts.length === 0) return null;

  return (
    <View style={styles.container}>
      {parts.map((part, index) => {
        const Icon = toolIcons[part.tool] || Wrench;
        const label = toolLabels[part.tool] || part.tool;
        const isRunning = part.state === "call";
        const isError = part.state === "error";
        const isDone = part.state === "result";

        return (
          <View key={part.callID || index} style={styles.row}>
            {/* Status indicator */}
            <View
              style={[
                styles.indicator,
                {
                  backgroundColor: isRunning
                    ? theme.colors.warning
                    : isError
                    ? theme.colors.danger
                    : theme.colors.success,
                },
              ]}
            />

            {/* Content */}
            <View style={styles.content}>
              <View style={styles.header}>
                <Icon
                  size={14}
                  color={
                    isRunning
                      ? theme.colors.warning
                      : isError
                      ? theme.colors.danger
                      : theme.colors.success
                  }
                />
                <Text
                  style={[
                    styles.label,
                    {
                      color: isRunning
                        ? theme.colors.text
                        : theme.colors.textMuted,
                    },
                  ]}
                  numberOfLines={1}
                >
                  {label}
                </Text>

                {isRunning && <Loader size={12} color={theme.colors.warning} />}
                {isDone && <Check size={12} color={theme.colors.success} />}
                {isError && <X size={12} color={theme.colors.danger} />}
              </View>

              {/* Tool title (if provided) */}
              {part.title ? (
                <Text
                  style={[styles.title, { color: theme.colors.textSecondary }]}
                  numberOfLines={1}
                >
                  {part.title}
                </Text>
              ) : null}

              {/* Show truncated input for certain tools */}
              {part.input && (part.tool === "bash" || part.tool === "webfetch") && (
                <Text
                  style={[styles.detail, { color: theme.colors.textMuted }]}
                  numberOfLines={1}
                >
                  {part.tool === "bash"
                    ? `$ ${part.input.command || JSON.stringify(part.input).slice(0, 60)}`
                    : String(part.input.url || "").slice(0, 80)}
                </Text>
              )}
            </View>
          </View>
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    gap: 2,
    marginVertical: 4,
  },
  row: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: 10,
  },
  indicator: {
    width: 6,
    height: 6,
    borderRadius: 3,
    marginTop: 6,
  },
  content: {
    flex: 1,
    gap: 2,
  },
  header: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
  },
  label: {
    fontSize: 13,
    fontWeight: "500",
    flex: 1,
  },
  title: {
    fontSize: 12,
    marginLeft: 20,
  },
  detail: {
    fontSize: 12,
    fontFamily: "monospace",
    marginLeft: 20,
    opacity: 0.7,
  },
});
