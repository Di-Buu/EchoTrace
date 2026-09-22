import Ionicons from '@expo/vector-icons/Ionicons';
import { useFocusEffect } from 'expo-router';
import { useCallback, useState } from 'react';
import { Pressable, StyleSheet, Text, TextInput, View } from 'react-native';

import { Screen } from '@/components/Screen';
import { StateMessage } from '@/components/StateMessage';
import { api } from '@/lib/api';
import { colors, radii, spacing } from '@/lib/theme';
import type { Memory } from '@/lib/types';
import { supabase } from '@/lib/supabase';
import { useAuth } from '@/providers/AuthProvider';

const typeLabel: Record<string, string> = {
  event: '经历', view: '观点', interest: '兴趣', goal: '目标', decision: '决定', question: '问题', state: '阶段状态',
};

export default function MeScreen() {
  const { session } = useAuth();
  const [memories, setMemories] = useState<Memory[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState('');
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);
  const [dataMessage, setDataMessage] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setMemories(await api.get<Memory[]>('/memories'));
      setLoadError('');
    } catch {
      setLoadError('记忆暂时没有加载出来');
    } finally {
      setLoading(false);
    }
  }, []);
  useFocusEffect(useCallback(() => { void load(); }, [load]));

  async function save(memory: Memory) {
    if (!editText.trim()) return;
    const next = await api.patch<Memory>(`/memories/${memory.id}`, { content: editText.trim() });
    setMemories((current) => current.map((item) => item.id === memory.id ? { ...item, ...next } : item));
    setEditingId(null);
  }

  async function remove(memory: Memory) {
    if (confirmDelete !== memory.id) {
      setConfirmDelete(memory.id);
      return;
    }
    await api.delete(`/memories/${memory.id}`);
    setMemories((current) => current.filter((item) => item.id !== memory.id));
    setConfirmDelete(null);
  }

  async function clearPersonalData() {
    if (!confirmClear) {
      setConfirmClear(true);
      setDataMessage('这会删除全部时刻、对话、记忆和洞察。请再次点击确认。');
      return;
    }
    try {
      await api.delete('/account/data');
      setMemories([]);
      setConfirmClear(false);
      setDataMessage('个人数据已清空，账号仍然保留。');
    } catch {
      setDataMessage('暂时无法清空数据，请稍后重试。');
    }
  }

  return (
    <Screen scroll>
      <Text style={styles.title}>我的</Text>
      <Text style={styles.sectionTitle}>AI 记得什么</Text>
      <Text style={styles.sectionHint}>这些理解来自你留下的时刻。你可以纠正或删除。</Text>
      {loading ? <StateMessage text="正在读取记忆…" loading /> : loadError ? (
        <StateMessage text={loadError} />
      ) : !memories.length ? (
        <View style={styles.emptyMemory}><Text style={styles.emptyText}>还没有形成长期记忆</Text></View>
      ) : memories.map((memory) => (
        <View key={memory.id} style={styles.memory}>
          <View style={styles.memoryHeader}>
            <Text style={styles.type}>{typeLabel[memory.memory_type] ?? '记忆'}</Text>
            {memory.status === 'disputed' ? <Text style={styles.disputed}>存在冲突</Text> : null}
          </View>
          {editingId === memory.id ? (
            <TextInput value={editText} onChangeText={setEditText} multiline autoFocus style={styles.editInput} />
          ) : (
            <Text style={styles.memoryText}>{memory.content}</Text>
          )}
          <Text style={styles.source}>来自 {memory.memory_sources?.length ?? 0} 个时刻</Text>
          <View style={styles.actions}>
            {editingId === memory.id ? (
              <>
                <Pressable onPress={() => save(memory)}><Text style={styles.action}>保存</Text></Pressable>
                <Pressable onPress={() => setEditingId(null)}><Text style={styles.actionMuted}>取消</Text></Pressable>
              </>
            ) : (
              <Pressable onPress={() => { setEditingId(memory.id); setEditText(memory.content); }}><Text style={styles.actionMuted}>纠正</Text></Pressable>
            )}
            <Pressable onPress={() => remove(memory)}><Text style={styles.delete}>{confirmDelete === memory.id ? '再次点击确认删除' : '删除'}</Text></Pressable>
          </View>
        </View>
      ))}

      <Text style={styles.sectionTitle}>隐私与数据</Text>
      <Text style={styles.sectionHint}>你可以清空全部时刻、对话、长期记忆和洞察，登录账号会继续保留。</Text>
      <Pressable onPress={clearPersonalData} style={styles.clearData}>
        <Text style={styles.delete}>{confirmClear ? '再次点击确认清空' : '清空个人数据'}</Text>
      </Pressable>
      {dataMessage ? <Text style={styles.dataMessage}>{dataMessage}</Text> : null}

      <Text style={styles.sectionTitle}>账号</Text>
      <View style={styles.account}>
        <Ionicons name="person-circle-outline" size={32} color={colors.accent} />
        <Text style={styles.email}>{session?.user.email}</Text>
      </View>
      <Pressable onPress={() => supabase.auth.signOut()} style={styles.signOut}><Text style={styles.signOutText}>退出登录</Text></Pressable>
    </Screen>
  );
}

const styles = StyleSheet.create({
  title: { color: colors.text, fontSize: 27, fontWeight: '600', marginTop: 4 },
  sectionTitle: { color: colors.text, fontSize: 18, fontWeight: '600', marginTop: 34 },
  sectionHint: { color: colors.muted, fontSize: 13, lineHeight: 20, marginTop: 7, marginBottom: 6 },
  emptyMemory: { paddingVertical: 28 },
  emptyText: { color: colors.muted, fontSize: 14 },
  memory: { paddingVertical: 18, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: colors.line },
  memoryHeader: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  type: { color: colors.accent, fontSize: 12, fontWeight: '600' },
  disputed: { color: colors.muted, fontSize: 11, backgroundColor: colors.soft, borderRadius: radii.pill, paddingHorizontal: 7, paddingVertical: 3 },
  memoryText: { color: colors.text, fontSize: 15, lineHeight: 24, marginTop: 8 },
  source: { color: colors.muted, fontSize: 12, marginTop: 9 },
  actions: { flexDirection: 'row', gap: spacing.lg, marginTop: 13 },
  action: { color: colors.accent, fontSize: 13, fontWeight: '600' },
  actionMuted: { color: colors.muted, fontSize: 13 },
  delete: { color: colors.danger, fontSize: 13 },
  clearData: { alignSelf: 'flex-start', paddingVertical: 10 },
  dataMessage: { color: colors.muted, fontSize: 12, lineHeight: 19 },
  editInput: { marginTop: 8, borderWidth: 1, borderColor: colors.line, borderRadius: radii.small, padding: 10, color: colors.text, fontSize: 15, lineHeight: 23 },
  account: { flexDirection: 'row', alignItems: 'center', gap: 12, marginTop: 14 },
  email: { color: colors.text, fontSize: 15, flex: 1 },
  signOut: { marginTop: 18, marginBottom: 40, minHeight: 48, alignItems: 'center', justifyContent: 'center', borderWidth: 1, borderColor: colors.line, borderRadius: radii.medium },
  signOutText: { color: colors.muted, fontSize: 14 },
});
