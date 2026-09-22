import Ionicons from '@expo/vector-icons/Ionicons';
import { router, useLocalSearchParams } from 'expo-router';
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Animated,
  FlatList,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';

import { Screen } from '@/components/Screen';
import { api, ApiError } from '@/lib/api';
import { colors, radii, spacing } from '@/lib/theme';
import type { CaptureMode, ChatResponse, Moment, ThreadMessage } from '@/lib/types';

type LocalMessage = Pick<ThreadMessage, 'role' | 'content' | 'evidence_moment_ids'> & { id: string };

export default function HollowScreen() {
  const params = useLocalSearchParams<{ sourceMomentId?: string; content?: string; threadId?: string }>();
  const [mode, setMode] = useState<CaptureMode>('capture');
  const [draft, setDraft] = useState('');
  const [threadId, setThreadId] = useState<string | null>(null);
  const [sourceMomentId, setSourceMomentId] = useState<string | null>(null);
  const [messages, setMessages] = useState<LocalMessage[]>([]);
  const [sending, setSending] = useState(false);
  const [notice, setNotice] = useState('');
  const ripple = useRef(new Animated.Value(0)).current;

  useEffect(() => {
    if (params.threadId) {
      setMode('chat');
      setThreadId(params.threadId);
      setSourceMomentId(null);
      setNotice('正在接上之前的对话…');
      api.get<ThreadMessage[]>(`/threads/${params.threadId}/messages`)
        .then((history) => {
          setMessages(history);
          setNotice('');
          return api.post(`/threads/${params.threadId}/resumed`);
        })
        .catch(() => setNotice('之前的对话暂时没有加载出来'));
      return;
    }
    if (params.sourceMomentId) {
      setMode('chat');
      setSourceMomentId(params.sourceMomentId);
      if (params.content) setDraft(params.content);
    }
  }, [params.content, params.sourceMomentId, params.threadId]);

  const canSubmit = Boolean(draft.trim()) && !sending;
  const emptyText = useMemo(() => (mode === 'capture' ? '想说什么都可以' : '从现在这句话开始'), [mode]);

  function showSaved() {
    setNotice('树洞收到了');
    ripple.setValue(0);
    Animated.timing(ripple, { toValue: 1, duration: 480, useNativeDriver: true }).start(() => {
      setTimeout(() => setNotice(''), 850);
    });
  }

  function processMoment(momentId: string) {
    void api.post<{ memories_created: number; insight_refresh_recommended: boolean }>(`/moments/${momentId}/process`)
      .then((result) => {
        if (result.insight_refresh_recommended) {
          return api.post('/insights/refresh');
        }
        return undefined;
      })
      .catch(() => undefined);
  }

  async function submit() {
    const content = draft.trim();
    if (!content || sending) return;
    setSending(true);
    setNotice('');
    try {
      if (mode === 'capture') {
        const moment = await api.post<Moment>('/moments', {
          content,
          mode: 'capture',
          input_type: 'text',
          memory_enabled: true,
        });
        setDraft('');
        showSaved();
        processMoment(moment.id);
      } else {
        const optimistic: LocalMessage = {
          id: `local-${Date.now()}`,
          role: 'user',
          content,
          evidence_moment_ids: [],
        };
        setMessages((current) => [...current, optimistic]);
        setDraft('');
        const response = await api.post<ChatResponse>('/chat', {
          content,
          thread_id: threadId,
          source_moment_id: threadId ? null : sourceMomentId,
          input_type: 'text',
        });
        setThreadId(response.thread_id);
        setSourceMomentId(null);
        setMessages((current) => [...current, response.message]);
        processMoment(response.moment_id);
      }
    } catch (error) {
      setDraft(content);
      setNotice(error instanceof ApiError ? error.message : '刚刚没接住，再说一次？');
    } finally {
      setSending(false);
    }
  }

  const composer = (
    <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined} keyboardVerticalOffset={78}>
      <View style={styles.composerWrap}>
        {notice ? <Text style={styles.notice}>{notice}</Text> : null}
        <View style={styles.composer}>
          <TextInput
            value={draft}
            onChangeText={setDraft}
            placeholder="写点什么…"
            placeholderTextColor={colors.muted}
            multiline
            maxLength={20000}
            style={styles.input}
          />
          <Pressable accessibilityLabel="发送" disabled={!canSubmit} onPress={submit} style={[styles.send, !canSubmit && styles.sendDisabled]}>
            <Ionicons name="arrow-up" size={21} color={canSubmit ? colors.white : colors.muted} />
          </Pressable>
        </View>
      </View>
    </KeyboardAvoidingView>
  );

  return (
    <Screen style={styles.screen} footer={composer}>
      <View style={styles.header}>
        <Text style={styles.brand}>EchoTrace</Text>
        <Pressable onPress={() => router.push('/moments')} hitSlop={12}>
          <Text style={styles.history}>时刻</Text>
        </Pressable>
      </View>
      <View style={styles.segment}>
        {(['capture', 'chat'] as const).map((item) => (
          <Pressable key={item} onPress={() => setMode(item)} style={[styles.segmentItem, mode === item && styles.segmentActive]}>
            <Text style={[styles.segmentText, mode === item && styles.segmentTextActive]}>{item === 'capture' ? '留一下' : '聊一会儿'}</Text>
          </Pressable>
        ))}
      </View>
      {mode === 'chat' && messages.length ? (
        <FlatList
          data={messages}
          keyExtractor={(item) => item.id}
          contentContainerStyle={styles.messages}
          renderItem={({ item }) => (
            <View style={[styles.message, item.role === 'user' ? styles.userMessage : styles.aiMessage]}>
              <Text style={styles.messageText}>{item.content}</Text>
              {item.evidence_moment_ids.length ? <Text style={styles.evidenceHint}>参考了 {item.evidence_moment_ids.length} 个过去的时刻</Text> : null}
            </View>
          )}
          ListFooterComponent={sending ? <Text style={styles.thinking}>正在听你说…</Text> : null}
        />
      ) : (
        <View style={styles.empty}>
          <Animated.View
            pointerEvents="none"
            style={[
              styles.ripple,
              {
                opacity: ripple.interpolate({ inputRange: [0, 0.2, 1], outputRange: [0, 0.35, 0] }),
                transform: [{ scale: ripple.interpolate({ inputRange: [0, 1], outputRange: [0.65, 1.4] }) }],
              },
            ]}
          />
          <Text style={styles.emptyText}>{notice === '树洞收到了' ? notice : emptyText}</Text>
          <Text style={styles.emptySub}>{mode === 'capture' ? '这里不会自动回复' : '我会陪你把这件事说下去'}</Text>
        </View>
      )}
    </Screen>
  );
}

const styles = StyleSheet.create({
  screen: { paddingBottom: 8 },
  header: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', minHeight: 42 },
  brand: { color: colors.text, fontSize: 20, fontWeight: '600', letterSpacing: 0.3 },
  history: { color: colors.muted, fontSize: 15 },
  segment: { marginTop: 14, padding: 4, flexDirection: 'row', backgroundColor: colors.soft, borderRadius: radii.pill, alignSelf: 'center' },
  segmentItem: { paddingVertical: 8, paddingHorizontal: 24, borderRadius: radii.pill },
  segmentActive: { backgroundColor: colors.surface },
  segmentText: { color: colors.muted, fontSize: 14 },
  segmentTextActive: { color: colors.text, fontWeight: '600' },
  empty: { flex: 1, minHeight: 320, alignItems: 'center', justifyContent: 'center' },
  emptyText: { color: colors.text, fontSize: 20, fontWeight: '500' },
  emptySub: { color: colors.muted, fontSize: 13, marginTop: 10 },
  ripple: { position: 'absolute', width: 104, height: 104, borderRadius: 52, borderWidth: 1.5, borderColor: colors.accent },
  messages: { paddingTop: spacing.lg, paddingBottom: 20, gap: 14 },
  message: { maxWidth: '88%', paddingVertical: 11, paddingHorizontal: 14, borderRadius: radii.medium },
  userMessage: { alignSelf: 'flex-end', backgroundColor: colors.soft },
  aiMessage: { alignSelf: 'flex-start', paddingHorizontal: 2 },
  messageText: { color: colors.text, fontSize: 16, lineHeight: 25 },
  evidenceHint: { color: colors.muted, fontSize: 12, marginTop: 8 },
  thinking: { color: colors.muted, fontSize: 14, paddingVertical: 8 },
  composerWrap: { backgroundColor: colors.background, paddingHorizontal: 16, paddingTop: 8, paddingBottom: Platform.OS === 'ios' ? 6 : 12 },
  notice: { color: colors.muted, fontSize: 13, textAlign: 'center', marginBottom: 7 },
  composer: { flexDirection: 'row', alignItems: 'flex-end', backgroundColor: colors.surface, borderRadius: radii.large, borderWidth: 1, borderColor: colors.line, padding: 7, minHeight: 56 },
  input: { flex: 1, maxHeight: 130, minHeight: 42, color: colors.text, fontSize: 16, lineHeight: 23, paddingHorizontal: 6, paddingTop: Platform.OS === 'ios' ? 10 : 8 },
  send: { width: 42, height: 42, borderRadius: 21, backgroundColor: colors.accent, alignItems: 'center', justifyContent: 'center' },
  sendDisabled: { backgroundColor: colors.soft },
});
