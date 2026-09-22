import { router, useLocalSearchParams } from 'expo-router';
import { useEffect, useState } from 'react';
import { Pressable, StyleSheet, Switch, Text, View } from 'react-native';

import { Screen } from '@/components/Screen';
import { StateMessage } from '@/components/StateMessage';
import { api } from '@/lib/api';
import { colors, radii, spacing } from '@/lib/theme';
import type { Moment } from '@/lib/types';

export default function MomentDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const [moment, setMoment] = useState<Moment | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!id) return;
    api.get<Moment>(`/moments/${id}`)
      .then(setMoment)
      .catch(() => setMoment(null))
      .finally(() => setLoading(false));
  }, [id]);

  async function toggleMemory(enabled: boolean) {
    if (!moment) return;
    const next = await api.patch<Moment>(`/moments/${moment.id}/memory?enabled=${enabled}`);
    setMoment((current) => current ? { ...current, memory_enabled: next.memory_enabled } : current);
    if (enabled) {
      void api.post(`/moments/${moment.id}/process`).catch(() => undefined);
    }
  }

  if (loading) return <Screen><StateMessage text="正在打开这一刻…" loading /></Screen>;
  if (!moment) return <Screen><StateMessage text="没有找到这一刻" /></Screen>;

  return (
    <Screen scroll>
      <Text style={styles.date}>{new Date(moment.created_at).toLocaleString('zh-CN', { dateStyle: 'long', timeStyle: 'short' })}</Text>
      <Text style={styles.content}>{moment.content}</Text>
      <View style={styles.sourceRow}>
        <Text style={styles.source}>文字记录</Text>
      </View>

      {moment.messages?.length ? (
        <View style={styles.thread}>
          <Text style={styles.sectionTitle}>延伸交流</Text>
          {moment.messages.map((message) => (
            <View key={message.id} style={[styles.message, message.role === 'user' && styles.userMessage]}>
              <Text style={styles.messageRole}>{message.role === 'user' ? '你' : 'EchoTrace'}</Text>
              <Text style={styles.messageText}>{message.content}</Text>
            </View>
          ))}
          <Pressable
            onPress={() => router.navigate({ pathname: '/', params: { threadId: moment.thread_id ?? undefined } })}
            style={styles.chatButton}
          >
            <Text style={styles.chatButtonText}>继续聊聊</Text>
          </Pressable>
        </View>
      ) : (
        <Pressable
          onPress={() => {
            void api.event('capture_to_chat', { moment_id: moment.id, entry_point: 'moment_detail' }).catch(() => undefined);
            router.navigate({ pathname: '/', params: { sourceMomentId: moment.id, content: moment.content } });
          }}
          style={styles.chatButton}
        >
          <Text style={styles.chatButtonText}>聊聊这个</Text>
        </Pressable>
      )}

      <View style={styles.control}>
        <View style={styles.controlText}>
          <Text style={styles.controlTitle}>参与长期理解</Text>
          <Text style={styles.controlHint}>关闭后，相关记忆不会再用于召回或洞察。</Text>
        </View>
        <Switch value={moment.memory_enabled} onValueChange={toggleMemory} trackColor={{ true: colors.accent }} />
      </View>
    </Screen>
  );
}

const styles = StyleSheet.create({
  date: { color: colors.muted, fontSize: 13, marginTop: 12 },
  content: { color: colors.text, fontSize: 19, lineHeight: 31, marginTop: spacing.lg },
  sourceRow: { marginTop: 14 },
  source: { color: colors.muted, fontSize: 12 },
  chatButton: { alignSelf: 'flex-start', marginTop: 34, backgroundColor: colors.soft, paddingHorizontal: 18, paddingVertical: 11, borderRadius: radii.pill },
  chatButtonText: { color: colors.accent, fontSize: 15, fontWeight: '600' },
  thread: { marginTop: 38, gap: 16 },
  sectionTitle: { color: colors.text, fontSize: 18, fontWeight: '600' },
  message: { paddingVertical: 4 },
  userMessage: { backgroundColor: colors.soft, padding: 14, borderRadius: radii.medium },
  messageRole: { color: colors.muted, fontSize: 12, marginBottom: 5 },
  messageText: { color: colors.text, fontSize: 15, lineHeight: 24 },
  control: { marginTop: 46, paddingTop: 18, borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: colors.line, flexDirection: 'row', alignItems: 'center', gap: 20 },
  controlText: { flex: 1 },
  controlTitle: { color: colors.text, fontSize: 15, fontWeight: '500' },
  controlHint: { color: colors.muted, fontSize: 12, lineHeight: 19, marginTop: 5 },
});
