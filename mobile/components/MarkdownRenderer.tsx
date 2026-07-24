import React from "react";
import { View, Text, StyleSheet, ScrollView } from "react-native";
import Markdown from "react-native-markdown-display";
import { themes } from "../constants/themes";
import { getTheme } from "../lib/storage";

interface MarkdownRendererProps {
  content: string;
}

const codeStyle = (theme: { colors: Record<string, string> }) =>
  StyleSheet.create({
    body: {},
    heading1: { fontSize: 22, fontWeight: "700", color: theme.colors.text, marginTop: 16, marginBottom: 8 },
    heading2: { fontSize: 19, fontWeight: "700", color: theme.colors.text, marginTop: 14, marginBottom: 6 },
    heading3: { fontSize: 16, fontWeight: "600", color: theme.colors.text, marginTop: 12, marginBottom: 4 },
    paragraph: { fontSize: 15, lineHeight: 22, color: theme.colors.text, marginBottom: 8 },
    link: { color: theme.colors.primary, textDecorationLine: "none" },
    blockquote: {
      borderLeftWidth: 3,
      borderLeftColor: theme.colors.primary,
      paddingLeft: 12,
      marginLeft: 0,
      marginVertical: 8,
      opacity: 0.8,
    },
    code_inline: {
      fontFamily: "monospace",
      fontSize: 13,
      color: theme.colors.accent,
      backgroundColor: theme.colors.bgTertiary,
      paddingHorizontal: 5,
      paddingVertical: 2,
      borderRadius: 4,
    },
    code_block: {
      fontFamily: "monospace",
      fontSize: 13,
      color: theme.colors.text,
      backgroundColor: theme.colors.bgTertiary,
      padding: 12,
      borderRadius: 8,
      marginVertical: 8,
      borderWidth: 1,
      borderColor: theme.colors.border,
    },
    fence: {
      fontFamily: "monospace",
      fontSize: 13,
      color: theme.colors.text,
      backgroundColor: theme.colors.bgTertiary,
      padding: 12,
      borderRadius: 8,
      marginVertical: 8,
      borderWidth: 1,
      borderColor: theme.colors.border,
    },
    list_item: { fontSize: 15, lineHeight: 22, color: theme.colors.text, marginBottom: 4 },
    bullet_list: { marginVertical: 8 },
    ordered_list: { marginVertical: 8 },
    table: { borderWidth: 1, borderColor: theme.colors.border, marginVertical: 8 },
    table_header_cell: {
      backgroundColor: theme.colors.bgTertiary,
      padding: 8,
      borderBottomWidth: 1,
      borderBottomColor: theme.colors.border,
    },
    table_cell: { padding: 8, borderBottomWidth: 1, borderBottomColor: theme.colors.border },
    strong: { fontWeight: "700" },
    em: { fontStyle: "italic" },
    hr: { height: 1, backgroundColor: theme.colors.border, marginVertical: 16 },
    image: { marginVertical: 8 },
  });

export function MarkdownRenderer({ content }: MarkdownRendererProps) {
  const theme = themes[getTheme()];
  const styles = codeStyle(theme);

  return (
    <Markdown style={styles}>{content}</Markdown>
  );
}

// ── Code block with language label ──

interface CodeBlockProps {
  language?: string;
  children: string;
}

export function CodeBlock({ language, children }: CodeBlockProps) {
  const theme = themes[getTheme()];

  return (
    <View style={[codeStyles.container, { backgroundColor: theme.colors.bgTertiary, borderColor: theme.colors.border }]}>
      {language ? (
        <View style={[codeStyles.header, { borderBottomColor: theme.colors.border }]}>
          <Text style={[codeStyles.language, { color: theme.colors.textMuted }]}>{language}</Text>
        </View>
      ) : null}
      <ScrollView horizontal showsHorizontalScrollIndicator={false}>
        <Text style={[codeStyles.code, { color: theme.colors.text }]} selectable>
          {children}
        </Text>
      </ScrollView>
    </View>
  );
}

const codeStyles = StyleSheet.create({
  container: {
    borderRadius: 8,
    borderWidth: 1,
    marginVertical: 8,
    overflow: "hidden",
  },
  header: {
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderBottomWidth: 1,
  },
  language: {
    fontSize: 11,
    fontWeight: "600",
    textTransform: "uppercase",
    letterSpacing: 0.5,
  },
  code: {
    fontFamily: "monospace",
    fontSize: 13,
    lineHeight: 20,
    padding: 12,
  },
});
