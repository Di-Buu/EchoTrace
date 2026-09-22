import { Stack } from 'expo-router';

import { colors } from '@/lib/theme';

export default function MomentsLayout() {
  return (
    <Stack screenOptions={{ headerStyle: { backgroundColor: colors.background }, headerShadowVisible: false, headerTintColor: colors.text }}>
      <Stack.Screen name="index" options={{ title: '时刻' }} />
      <Stack.Screen name="[id]" options={{ title: '这一刻' }} />
    </Stack>
  );
}

