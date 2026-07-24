import { Tabs } from "expo-router";
import { Home, MessageSquare, FolderOpen, Search, Terminal } from "lucide-react-native";
import { getTheme } from "../../lib/storage";
import { themes } from "../../constants/themes";

export default function TabLayout() {
  const theme = themes[getTheme()];

  const icons = {
    index: Home,
    sessions: MessageSquare,
    files: FolderOpen,
    search: Search,
    terminal: Terminal,
  };

  return (
    <Tabs
      screenOptions={{
        headerShown: false,
        tabBarStyle: {
          backgroundColor: theme.colors.bgSecondary,
          borderTopColor: theme.colors.border,
          height: 60,
          paddingBottom: 8,
          paddingTop: 8,
        },
        tabBarActiveTintColor: theme.colors.primary,
        tabBarInactiveTintColor: theme.colors.textMuted,
      }}
    >
      <Tabs.Screen
        name="index"
        options={{
          title: "Home",
          tabBarIcon: ({ color, size }) => <icons.index size={size} color={color} />,
        }}
      />
      <Tabs.Screen
        name="sessions"
        options={{
          title: "Sessions",
          tabBarIcon: ({ color, size }) => <icons.sessions size={size} color={color} />,
        }}
      />
      <Tabs.Screen
        name="files"
        options={{
          title: "Files",
          tabBarIcon: ({ color, size }) => <icons.files size={size} color={color} />,
        }}
      />
      <Tabs.Screen
        name="search"
        options={{
          title: "Search",
          tabBarIcon: ({ color, size }) => <icons.search size={size} color={color} />,
        }}
      />
      <Tabs.Screen
        name="terminal"
        options={{
          title: "Terminal",
          tabBarIcon: ({ color, size }) => <icons.terminal size={size} color={color} />,
        }}
      />
    </Tabs>
  );
}
