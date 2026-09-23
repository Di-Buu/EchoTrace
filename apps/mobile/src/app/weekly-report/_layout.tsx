import { Stack } from 'expo-router';

import { colors } from '@/lib/theme';

export default function WeeklyReportLayout() {
  return (
    <Stack screenOptions={{ headerStyle: { backgroundColor: colors.background }, headerShadowVisible: false, headerTintColor: colors.text }}>
      <Stack.Screen name="[id]" options={{ title: '每周回顾' }} />
    </Stack>
  );
}
