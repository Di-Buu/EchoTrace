import type { PropsWithChildren, ReactNode } from 'react';
import { ScrollView, StyleProp, StyleSheet, View, ViewStyle } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { colors } from '@/lib/theme';

type Props = PropsWithChildren<{
  scroll?: boolean;
  style?: StyleProp<ViewStyle>;
  footer?: ReactNode;
}>;

export function Screen({ children, scroll = false, style, footer }: Props) {
  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      {scroll ? (
        <ScrollView contentContainerStyle={[styles.content, style]} keyboardShouldPersistTaps="handled">
          {children}
        </ScrollView>
      ) : (
        <View style={[styles.content, styles.flex, style]}>{children}</View>
      )}
      {footer}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.background },
  flex: { flex: 1 },
  content: { paddingHorizontal: 20, paddingTop: 12, paddingBottom: 24 },
});

