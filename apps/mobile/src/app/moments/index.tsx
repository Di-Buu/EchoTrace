import { router, useFocusEffect } from 'expo-router';
import { useCallback, useState } from 'react';
import { Pressable, SectionList, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { StateMessage } from '@/components/StateMessage';
import { api } from '@/lib/api';
import { colors, spacing } from '@/lib/theme';
import type { Moment } from '@/lib/types';

function dayLabel(value: string) {
  const date = new Date(value);
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (date.toDateString() === today.toDateString()) return '今天';
  if (date.toDateString() === yesterday.toDateString()) return '昨天';
  return date.toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', weekday: 'short' });
}

export default function MomentsScreen() {
  const [items, setItems] = useState<Moment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setItems(await api.get<Moment[]>('/moments'));
      setError('');
    } catch {
      setError('时刻暂时没有加载出来');
    } finally {
      setLoading(false);
    }
  }, []);

  useFocusEffect(useCallback(() => { void load(); }, [load]));

  const sections = Object.entries(
    items.reduce<Record<string, Moment[]>>((result, item) => {
      const key = dayLabel(item.created_at);
      (result[key] ??= []).push(item);
      return result;
    }, {}),
  ).map(([title, data]) => ({ title, data }));

  return (
    <SafeAreaView style={styles.safe} edges={['bottom']}>
      {loading ? <StateMessage text="正在翻开时刻…" loading /> : error ? <StateMessage text={error} /> : !items.length ? (
        <StateMessage text="还没有留下什么。第一条不用想太多，随便说点什么就好" />
      ) : (
        <SectionList
          sections={sections}
          keyExtractor={(item) => item.id}
          contentContainerStyle={styles.list}
          stickySectionHeadersEnabled={false}
          renderSectionHeader={({ section }) => <Text style={styles.day}>{section.title}</Text>}
          renderItem={({ item }) => (
            <Pressable onPress={() => router.push({ pathname: '/moments/[id]', params: { id: item.id } })} style={styles.row}>
              <View style={styles.timeColumn}>
                <Text style={styles.time}>{new Date(item.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</Text>
                <View style={styles.dot} />
              </View>
              <View style={styles.body}>
                <Text numberOfLines={3} style={styles.content}>{item.content}</Text>
                <View style={styles.metaRow}>
                  <Text style={styles.meta}>{item.thread_id ? '有延伸对话' : item.mode === 'capture' ? '留在这里' : '聊过一会儿'}</Text>
                </View>
              </View>
            </Pressable>
          )}
        />
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.background },
  list: { paddingHorizontal: 20, paddingBottom: 32 },
  day: { color: colors.text, fontSize: 18, fontWeight: '600', marginTop: spacing.lg, marginBottom: 8 },
  row: { flexDirection: 'row', paddingVertical: 14 },
  timeColumn: { width: 58, alignItems: 'flex-start' },
  time: { color: colors.muted, fontSize: 12 },
  dot: { width: 6, height: 6, borderRadius: 3, backgroundColor: colors.accent, marginTop: 10, marginLeft: 12 },
  body: { flex: 1, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: colors.line, paddingBottom: 14 },
  content: { color: colors.text, fontSize: 16, lineHeight: 25 },
  metaRow: { flexDirection: 'row', alignItems: 'center', gap: 5, marginTop: 9 },
  meta: { color: colors.muted, fontSize: 12 },
});
