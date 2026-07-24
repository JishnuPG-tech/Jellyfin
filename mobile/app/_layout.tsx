import { useEffect } from "react";
import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { QueryProvider } from "../lib/query-provider";
import { getTheme } from "../lib/storage";
import { themes } from "../constants/themes";

export default function RootLayout() {
  const theme = themes[getTheme()];

  useEffect(() => {
    const { SystemUI } = require("expo-system-ui");
    SystemUI.setBackgroundColorAsync(theme.colors.bg);
  }, []);

  return (
    <QueryProvider>
      <StatusBar style="light" />
      <Stack
        screenOptions={{
          headerShown: false,
          contentStyle: { backgroundColor: theme.colors.bg },
        }}
      />
    </QueryProvider>
  );
}
