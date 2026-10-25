import { router, useLocalSearchParams } from 'expo-router';
import { useEffect, useState } from 'react';
import { Pressable, StyleSheet, Switch, Text, TextInput, View } from 'react-native';

import { Screen } from '@/components/Screen';
import { StateMessage } from '@/components/StateMessage';
import { api, ApiError } from '@/lib/api';
import { colors, radii, spacing } from '@/lib/theme';
import type { Moment } from '@/lib/types';
import { useAuth } from '@/providers/AuthProvider';

export default function MomentDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const [moment, setMoment] = useState<Moment | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadedId, setLoadedId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState('');
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [notice, setNotice] = useState('');
  const { resetPersonalViews } = useAuth();

  useEffect(() => {
    if (!id) return;
    let active = true;
    api.get<Moment>(`/moments/${id}`)
      .then((result) => {
        if (!active) return;
        setMoment(result);
        setEditing(false);
        setConfirmDelete(false);
        setNotice('');
      })
      .catch(() => { if (active) setMoment(null); })
      .finally(() => { if (active) { setLoadedId(id); setLoading(false); } });
    return () => { active = false; };
  }, [id]);

  async function toggleMemory(enabled: boolean) {
    if (!moment || busy) return;
    setBusy(true);
    try {
      const next = await api.patch<Moment>(`/moments/${moment.id}/memory?enabled=${enabled}`);
      setMoment((current) => current ? { ...current, memory_enabled: next.memory_enabled } : current);
      setNotice(enabled ? '已允许后续长期理解，正在后台整理。' : '已退出长期理解，相关旧发现会失效。');
    } catch (error) { setNotice(error instanceof ApiError ? error.message : '设置没有保存成功，请重试。'); }
    finally { setBusy(false); }
  }

  async function saveEdit() {
    if (!moment || busy || !editText.trim()) return;
    setBusy(true);
    try {
      const next = await api.patch<Moment>(`/moments/${moment.id}`, { content: editText.trim() });
      setMoment((current) => current ? { ...current, ...next } : current);
      setEditing(false);
      setNotice(next.memory_enabled
        ? '原文已更新，相关旧发现会失效，索引会在后台重新整理。'
        : '原文已更新。此时刻未参与长期理解，不会建立长期索引。');
    } catch (error) { setNotice(error instanceof ApiError ? error.message : '原文没有保存成功，请重试。'); }
    finally { setBusy(false); }
  }

  async function removeMoment() {
    if (!moment || busy) return;
    if (!confirmDelete) { setConfirmDelete(true); return; }
    setBusy(true);
    try {
      await api.delete(`/moments/${moment.id}`);
      resetPersonalViews();
      router.replace('/moments');
    } catch (error) { setNotice(error instanceof ApiError ? error.message : '删除没有完成，请重试。'); }
    finally { setBusy(false); }
  }

  if (loading || loadedId !== id) return <Screen><StateMessage text="正在打开这一刻…" loading /></Screen>;
  if (!moment) return <Screen><StateMessage text="没有找到这一刻" /></Screen>;

  return (
    <Screen scroll>
      <Text style={styles.date}>{new Date(moment.created_at).toLocaleString('zh-CN', { dateStyle: 'long', timeStyle: 'short' })}</Text>
      {editing ? (
        <View>
          <TextInput value={editText} onChangeText={setEditText} multiline maxLength={20000} style={styles.editInput} accessibilityLabel="编辑时刻原文" />
          <Pressable onPress={saveEdit} disabled={busy || !editText.trim()} style={styles.chatButton}><Text style={styles.chatButtonText}>{busy ? '正在保存…' : '保存修改'}</Text></Pressable>
          <Pressable onPress={() => setEditing(false)} disabled={busy}><Text style={styles.source}>取消修改</Text></Pressable>
          <Text style={styles.source}>修改原文不会改写已经发送的对话；相关旧发现会失效。</Text>
        </View>
      ) : <Text style={styles.content}>{moment.content}</Text>}
      {notice ? <Text accessibilityRole="alert" style={styles.notice}>{notice}</Text> : null}
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
            onPress={() => router.dismissTo({ pathname: '/(tabs)', params: { threadId: moment.thread_id ?? undefined } })}
            style={styles.chatButton}
          >
            <Text style={styles.chatButtonText}>继续聊聊</Text>
          </Pressable>
        </View>
      ) : (
        <Pressable
          onPress={() => {
            void api.event('capture_to_chat', { moment_id: moment.id, entry_point: 'moment_detail' }).catch(() => undefined);
            router.dismissTo({ pathname: '/(tabs)', params: { sourceMomentId: moment.id, content: moment.content } });
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
        <Switch value={moment.memory_enabled} onValueChange={toggleMemory} disabled={busy} trackColor={{ true: colors.accent }} />
      </View>
      <View style={styles.manage}>
        <Pressable disabled={busy} onPress={() => { setEditText(moment.content); setEditing(true); setConfirmDelete(false); }}><Text style={styles.chatButtonText}>编辑原文</Text></Pressable>
        {confirmDelete ? <Text style={styles.source}>{moment.thread_id ? '这会删除此时刻及关联的整段对话和对话记录，无法恢复。' : '此时刻及相关索引将被删除，无法恢复。'}</Text> : null}
        <Pressable disabled={busy} onPress={removeMoment}><Text style={{ color: colors.danger }}>{confirmDelete ? '确认删除时刻' : '删除时刻'}</Text></Pressable>
        {confirmDelete ? <Pressable disabled={busy} onPress={() => setConfirmDelete(false)}><Text style={styles.source}>取消删除</Text></Pressable> : null}
      </View>
    </Screen>
  );
}

const styles = StyleSheet.create({
  date: { color: colors.muted, fontSize: 13, marginTop: 12 },
  content: { color: colors.text, fontSize: 19, lineHeight: 31, marginTop: spacing.lg },
  editInput: { color: colors.text, fontSize: 17, lineHeight: 26, borderWidth: 1, borderColor: colors.line, padding: 12, marginTop: 18, borderRadius: radii.small },
  notice: { color: colors.accent, fontSize: 13, lineHeight: 21, marginTop: 14 },
  manage: { marginTop: 28, gap: 18, paddingBottom: 24 },
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
