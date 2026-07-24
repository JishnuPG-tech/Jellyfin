import { View, Text, TextInput, TouchableOpacity, FlatList, KeyboardAvoidingView, Platform, StyleSheet } from "react-native";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { useState, useEffect, useRef } from "react";
import { ArrowLeft, Send, Loader, AlertTriangle } from "lucide-react-native";
import { useMessages, useSession, useSendMessage, useReplyPermission, useReplyQuestion } from "../../hooks/useApi";
import { useSSE, getMessagesFromEvents } from "../../hooks/useSSE";
import { MarkdownRenderer } from "../../components/MarkdownRenderer";
import { ToolTimeline } from "../../components/ToolTimeline";
import { PermissionCard } from "../../components/PermissionCard";
import { QuestionCard } from "../../components/QuestionCard";
import { Message, MessagePart, ToolPart, PermissionRequest, QuestionRequest } from "../../constants/types";
import { getTheme } from "../../lib/storage";
import { themes } from "../../constants/themes";

export default function ChatScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const theme = themes[getTheme()];
  const [input, setInput] = useState("");
  const flatListRef = useRef<FlatList>(null);

  const { data: session } = useSession(id || "");
  const { data: serverMessages } = useMessages(id || "");
  const { connect, disconnect, streaming, events, error } = useSSE(id);
  const sendMessage = useSendMessage();
  const replyPermission = useReplyPermission();
  const replyQuestion = useReplyQuestion();

  // Build messages from server + SSE events
  const sseMessages = getMessagesFromEvents(events);
  const allMessages: Message[] = [...(serverMessages || []), ...sseMessages];

  // Deduplicate by id
  const seen = new Set<string>();
  const uniqueMessages = allMessages.filter((m) => {
    if (seen.has(m.id)) return false;
    seen.add(m.id);
    return true;
  });

  // Collect pending permissions and questions
  const pendingPermissions: PermissionRequest[] = [];
  const pendingQuestions: QuestionRequest[] = [];
  for (const ev of events) {
    if (ev.type === "permission.requested") {
      const perm = ev.properties as PermissionRequest;
      if (perm.state === "pending") pendingPermissions.push(perm);
    }
    if (ev.type === "question.asked") {
      const q = ev.properties as QuestionRequest;
      if (q.state === "pending") pendingQuestions.push(q);
    }
  }

  useEffect(() => {
    connect();
    return () => disconnect();
  }, [id]);

  const handleSend = () => {
    if (!input.trim() || !id) return;
    sendMessage.mutate({ sessionId: id, content: input.trim() });
    setInput("");
  };

  useEffect(() => {
    if (uniqueMessages.length > 0 || events.length > 0) {
      setTimeout(() => flatListRef.current?.scrollToEnd({ animated: true }), 100);
    }
  }, [uniqueMessages.length, events.length]);

  const renderPart = (part: MessagePart, index: number) => {
    if (part.type === "text" && part.text) {
      return <MarkdownRenderer key={index} content={part.text} />;
    }
    if (part.type === "tool") {
      const toolPart = part as ToolPart;
      return (
        <ToolTimeline
          key={toolPart.callID || index}
          parts={[toolPart]}
        />
      );
    }
    if (part.type === "reasoning") {
      return (
        <View key={index} style={[styles.reasoning, { backgroundColor: theme.colors.bgTertiary, borderColor: theme.colors.border }]}>
          <Text style={[styles.reasoningLabel, { color: theme.colors.textMuted }]}>Thinking...</Text>
          <Text style={[styles.reasoningText, { color: theme.colors.textMuted }]} numberOfLines={3}>
            {part.text}
          </Text>
        </View>
      );
    }
    return null;
  };

  const renderMessage = (item: Message) => {
    const isUser = item.role === "user";

    // Group consecutive tool parts
    const toolParts: ToolPart[] = [];
    const otherParts: MessagePart[] = [];
    for (const part of item.parts) {
      if (part.type === "tool") toolParts.push(part as ToolPart);
      else otherParts.push(part);
    }

    return (
      <View
        style={[
          styles.bubble,
          isUser
            ? { backgroundColor: theme.colors.primary, alignSelf: "flex-end", maxWidth: "85%" }
            : { backgroundColor: theme.colors.surface, alignSelf: "flex-start", maxWidth: "90%" },
        ]}
      >
        {otherParts.map((part, i) => renderPart(part, i))}
        {toolParts.length > 0 && <ToolTimeline parts={toolParts} />}
      </View>
    );
  };

  return (
    <KeyboardAvoidingView
      style={[styles.container, { backgroundColor: theme.colors.bg }]}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
      keyboardVerticalOffset={Platform.OS === "ios" ? 0 : 0}
    >
      <View style={[styles.header, { paddingTop: insets.top + 8, backgroundColor: theme.colors.bgSecondary, borderBottomColor: theme.colors.border }]}>
        <TouchableOpacity onPress={() => router.back()} style={styles.backBtn}>
          <ArrowLeft size={20} color={theme.colors.text} />
        </TouchableOpacity>
        <Text style={[styles.headerTitle, { color: theme.colors.text }]} numberOfLines={1}>
          {session?.title || "Chat"}
        </Text>
      </View>

      <FlatList
        ref={flatListRef}
        data={uniqueMessages}
        keyExtractor={(item) => item.id}
        contentContainerStyle={{ padding: 20, paddingBottom: 20, gap: 12 }}
        showsVerticalScrollIndicator={false}
        onContentSizeChange={() => flatListRef.current?.scrollToEnd({ animated: true })}
        renderItem={({ item }) => renderMessage(item)}
        ListFooterComponent={
          <>
            {/* Pending permissions */}
            {pendingPermissions.map((perm) => (
              <PermissionCard
                key={perm.id}
                request={perm}
                onAllow={() => replyPermission.mutate({ sessionId: id!, requestId: perm.id, allow: true })}
                onDeny={() => replyPermission.mutate({ sessionId: id!, requestId: perm.id, allow: false })}
              />
            ))}

            {/* Pending questions */}
            {pendingQuestions.map((q) => (
              <QuestionCard
                key={q.id}
                request={q}
                onReply={(answer) => replyQuestion.mutate({ sessionId: id!, requestId: q.id, answer })}
              />
            ))}

            {/* Streaming indicator */}
            {streaming && (
              <View style={[styles.bubble, { backgroundColor: theme.colors.surface, alignSelf: "flex-start" }]}>
                <View style={styles.typing}>
                  <Loader size={14} color={theme.colors.textMuted} />
                  <Text style={[styles.typingText, { color: theme.colors.textMuted }]}>Thinking...</Text>
                </View>
              </View>
            )}
          </>
        }
      />

      {error && (
        <View style={[styles.errorBar, { backgroundColor: theme.colors.danger }]}>
          <AlertTriangle size={14} color="#fff" />
          <Text style={[styles.errorText, { color: "#fff" }]}>{error}</Text>
        </View>
      )}

      <View style={[styles.inputBar, { borderTopColor: theme.colors.border, backgroundColor: theme.colors.bgSecondary, paddingBottom: insets.bottom + 8 }]}>
        <TextInput
          style={[styles.textInput, { color: theme.colors.text, backgroundColor: theme.colors.surface, borderColor: theme.colors.border }]}
          placeholder="Type a message..."
          placeholderTextColor={theme.colors.textMuted}
          value={input}
          onChangeText={setInput}
          onSubmitEditing={handleSend}
          multiline
          maxLength={4000}
        />
        <TouchableOpacity
          style={[styles.sendBtn, { backgroundColor: input.trim() ? theme.colors.primary : theme.colors.surface }]}
          onPress={handleSend}
          disabled={!input.trim() || streaming}
        >
          <Send size={18} color={input.trim() ? theme.colors.primaryText : theme.colors.textMuted} />
        </TouchableOpacity>
      </View>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  header: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    paddingHorizontal: 16,
    paddingBottom: 10,
    borderBottomWidth: 1,
  },
  backBtn: { padding: 4 },
  headerTitle: { fontSize: 17, fontWeight: "600", flex: 1 },
  bubble: {
    padding: 14,
    borderRadius: 14,
  },
  reasoning: {
    padding: 10,
    borderRadius: 8,
    borderWidth: 1,
    marginTop: 4,
  },
  reasoningLabel: { fontSize: 11, fontWeight: "600", textTransform: "uppercase", marginBottom: 4 },
  reasoningText: { fontSize: 12, lineHeight: 18, fontStyle: "italic" },
  typing: { flexDirection: "row", alignItems: "center", gap: 6 },
  typingText: { fontSize: 14 },
  errorBar: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingHorizontal: 16,
    paddingVertical: 8,
  },
  errorText: { fontSize: 13, flex: 1 },
  inputBar: {
    flexDirection: "row",
    alignItems: "flex-end",
    gap: 8,
    paddingHorizontal: 16,
    paddingTop: 10,
    borderTopWidth: 1,
  },
  textInput: {
    flex: 1,
    fontSize: 15,
    paddingHorizontal: 14,
    paddingVertical: 10,
    borderRadius: 12,
    borderWidth: 1,
    maxHeight: 120,
  },
  sendBtn: {
    width: 40,
    height: 40,
    borderRadius: 12,
    alignItems: "center",
    justifyContent: "center",
  },
});
