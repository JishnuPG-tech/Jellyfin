import React, { useState } from "react";
import { View, Text, TouchableOpacity, TextInput, StyleSheet } from "react-native";
import { HelpCircle, Send } from "lucide-react-native";
import { themes } from "../constants/themes";
import { getTheme } from "../lib/storage";
import type { QuestionRequest } from "../constants/types";

interface QuestionCardProps {
  request: QuestionRequest;
  onReply: (answer: string) => void;
  onReject?: () => void;
}

export function QuestionCard({ request, onReply, onReject }: QuestionCardProps) {
  const theme = themes[getTheme()];
  const [selected, setSelected] = useState<string | null>(null);
  const [customAnswer, setCustomAnswer] = useState("");

  const handleSubmit = () => {
    if (selected) {
      onReply(selected);
    } else if (customAnswer.trim()) {
      onReply(customAnswer.trim());
    }
  };

  const hasOptions = request.options && request.options.length > 0;

  return (
    <View style={[styles.card, { backgroundColor: theme.colors.surface, borderColor: theme.colors.border }]}>
      <View style={styles.header}>
        <HelpCircle size={16} color={theme.colors.primary} />
        <Text style={[styles.title, { color: theme.colors.text }]}>Question</Text>
      </View>

      <Text style={[styles.question, { color: theme.colors.textSecondary }]}>
        {request.question}
      </Text>

      {hasOptions ? (
        <View style={styles.options}>
          {request.options!.map((option) => {
            const isSelected = selected === option;
            return (
              <TouchableOpacity
                key={option}
                style={[
                  styles.option,
                  {
                    backgroundColor: isSelected ? theme.colors.bgTertiary : theme.colors.bg,
                    borderColor: isSelected ? theme.colors.primary : theme.colors.border,
                  },
                ]}
                onPress={() => setSelected(option)}
                activeOpacity={0.7}
              >
                <View
                  style={[
                    styles.radio,
                    {
                      borderColor: isSelected ? theme.colors.primary : theme.colors.textMuted,
                      backgroundColor: isSelected ? theme.colors.primary : "transparent",
                    },
                  ]}
                />
                <Text
                  style={[
                    styles.optionText,
                    { color: isSelected ? theme.colors.text : theme.colors.textSecondary },
                  ]}
                >
                  {option}
                </Text>
              </TouchableOpacity>
            );
          })}
        </View>
      ) : (
        <TextInput
          style={[styles.input, { color: theme.colors.text, backgroundColor: theme.colors.bgTertiary, borderColor: theme.colors.border }]}
          value={customAnswer}
          onChangeText={setCustomAnswer}
          placeholder="Type your answer..."
          placeholderTextColor={theme.colors.textMuted}
          multiline
        />
      )}

      <TouchableOpacity
        style={[
          styles.submitBtn,
          {
            backgroundColor: selected || customAnswer.trim() ? theme.colors.primary : theme.colors.bgTertiary,
          },
        ]}
        onPress={handleSubmit}
        disabled={!selected && !customAnswer.trim()}
        activeOpacity={0.7}
      >
        <Send size={14} color={selected || customAnswer.trim() ? theme.colors.primaryText : theme.colors.textMuted} />
        <Text
          style={[
            styles.submitText,
            { color: selected || customAnswer.trim() ? theme.colors.primaryText : theme.colors.textMuted },
          ]}
        >
          Submit
        </Text>
      </TouchableOpacity>
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
  question: {
    fontSize: 14,
    lineHeight: 20,
  },
  options: {
    gap: 6,
  },
  option: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    padding: 12,
    borderRadius: 8,
    borderWidth: 1,
  },
  radio: {
    width: 16,
    height: 16,
    borderRadius: 8,
    borderWidth: 2,
  },
  optionText: {
    fontSize: 14,
    flex: 1,
  },
  input: {
    fontSize: 14,
    paddingHorizontal: 12,
    paddingVertical: 10,
    borderRadius: 8,
    borderWidth: 1,
    minHeight: 44,
  },
  submitBtn: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 6,
    paddingVertical: 10,
    borderRadius: 8,
    marginTop: 4,
  },
  submitText: {
    fontSize: 14,
    fontWeight: "600",
  },
});
