import Ionicons from '@expo/vector-icons/Ionicons';
import { Tabs } from 'expo-router';

import { colors } from '@/lib/theme';

export default function TabsLayout() {
  return (
    <Tabs
      screenOptions={{
        headerShown: false,
        tabBarActiveTintColor: colors.accent,
        tabBarInactiveTintColor: colors.muted,
        tabBarStyle: { backgroundColor: colors.surface, borderTopColor: colors.line, height: 66, paddingTop: 6 },
        tabBarLabelStyle: { fontSize: 12, marginBottom: 6 },
      }}
    >
      <Tabs.Screen
        name="index"
        options={{ title: '树洞', tabBarIcon: ({ color, size }) => <Ionicons name="ellipse-outline" color={color} size={size} /> }}
      />
      <Tabs.Screen
        name="insights"
        options={{ title: '洞察', tabBarIcon: ({ color, size }) => <Ionicons name="layers-outline" color={color} size={size} /> }}
      />
      <Tabs.Screen
        name="me"
        options={{ title: '我的', tabBarIcon: ({ color, size }) => <Ionicons name="person-outline" color={color} size={size} /> }}
      />
    </Tabs>
  );
}

