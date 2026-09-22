import { ActivityIndicator, StyleSheet, Text, View } from 'react-native';

import { colors } from '@/lib/theme';

export function StateMessage({ text, loading = false }: { text: string; loading?: boolean }) {
  return (
    <View style={styles.container}>
      {loading ? <ActivityIndicator color={colors.accent} /> : null}
      <Text style={styles.text}>{text}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, minHeight: 180, alignItems: 'center', justifyContent: 'center', gap: 12 },
  text: { color: colors.muted, fontSize: 15, lineHeight: 24, textAlign: 'center' },
});

