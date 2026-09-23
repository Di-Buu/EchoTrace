import Ionicons from '@expo/vector-icons/Ionicons';
import { router, useFocusEffect } from 'expo-router';
import { useCallback, useEffect, useState } from 'react';
import { FlatList, KeyboardAvoidingView, Platform, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';

import { Screen } from '@/components/Screen';
import { StateMessage } from '@/components/StateMessage';
import { api, ApiError } from '@/lib/api';
import { colors, radii, spacing } from '@/lib/theme';
import type { Insight, WeeklyReport } from '@/lib/types';

export default function InsightsScreen() {
  const [tab, setTab] = useState<'discoveries' | 'ask'>('discoveries');
  const [items, setItems] = useState<Insight[]>([]);
  const [weeks, setWeeks] = useState<WeeklyReport[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [weeklyError, setWeeklyError] = useState('');
  const [question, setQuestion] = useState('');
  const [asking, setAsking] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [message, setMessage] = useState('');

  const load = useCallback(async () => {
    try {
      const result = await api.get<Insight[]>('/insights');
      setItems(result);
      result.forEach((item) => {
        void api.event('insight_shown', {
          insight_id: item.id,
          insight_type: item.insight_type,
          trigger_type: item.trigger_type,
        }).catch(() => undefined);
      });
      setLoadError('');
    } catch {
      setLoadError('洞察暂时没有加载出来');
    } finally {
      setLoading(false);
    }
  }, []);

  const loadWeeks = useCallback(async () => {
    try {
      setWeeks(await api.get<WeeklyReport[]>('/weekly-reports'));
      setWeeklyError('');
    } catch {
      setWeeklyError('每周回顾暂时没有加载出来');
    }
  }, []);

  useFocusEffect(useCallback(() => {
    void load();
    void loadWeeks();
    void api.post('/weekly-reports/ensure')
      .then(() => loadWeeks())
      .catch(() => setWeeklyError('每周回顾暂时无法整理'));
  }, [load, loadWeeks]));

  useEffect(() => {
    if (!weeks.some((week) => week.status === 'processing')) return;
    const timer = setInterval(() => { void loadWeeks(); }, 6000);
    return () => clearInterval(timer);
  }, [loadWeeks, weeks]);

  async function ask() {
    if (!question.trim() || asking) return;
    setAsking(true);
    setMessage('');
    try {
      const result = await api.post<Insight>('/insights/query', { question: question.trim() });
      setQuestion('');
      await load();
      router.push({ pathname: '/insight/[id]', params: { id: result.id } });
    } catch (error) {
      setMessage(error instanceof ApiError ? error.message : '这次没能找到足够可靠的线索。');
    } finally {
      setAsking(false);
    }
  }

  async function refreshDiscoveries() {
    if (refreshing) return;
    setRefreshing(true);
    setMessage('');
    try {
      await api.post('/weekly-reports/ensure');
      await loadWeeks();
      await load();
    } catch (error) {
      setMessage(error instanceof ApiError ? error.message : '暂时无法整理每周回顾。');
    } finally {
      setRefreshing(false);
    }
  }

  return (
    <Screen style={styles.screen}>
      <Text style={styles.title}>洞察</Text>
      <View style={styles.tabs}>
        <Pressable onPress={() => setTab('discoveries')} style={styles.tab}><Text style={[styles.tabText, tab === 'discoveries' && styles.tabActive]}>AI 发现</Text></Pressable>
        <Pressable onPress={() => setTab('ask')} style={styles.tab}><Text style={[styles.tabText, tab === 'ask' && styles.tabActive]}>我想知道</Text></Pressable>
      </View>

      {tab === 'discoveries' ? (
        loading && !weeks.length ? <StateMessage text="正在翻一翻过去的你…" loading /> : !weeks.length && !items.length ? (
          <View style={styles.emptyDiscoveries}>
            <StateMessage text="树洞还在慢慢认识你" />
            <Pressable onPress={refreshDiscoveries} disabled={refreshing} style={styles.refreshButton}>
              <Text style={styles.refreshText}>{refreshing ? '正在检查…' : '检查每周回顾'}</Text>
            </Pressable>
            {message || weeklyError || loadError ? <Text style={styles.status}>{message || weeklyError || loadError}</Text> : null}
          </View>
        ) : (
          <FlatList
            data={items}
            keyExtractor={(item) => item.id}
            contentContainerStyle={styles.list}
            ListHeaderComponent={
              <View>
                {weeks.some((week) => week.status === 'processing') ? (
                  <View style={styles.progress}>
                    <Text style={styles.progressTitle}>正在整理新的发现</Text>
                    <Text style={styles.progressText}>之前的回顾和发现仍然可以查看。</Text>
                  </View>
                ) : null}
                {weeklyError ? <Text style={styles.status}>{weeklyError}</Text> : null}
                <Text style={styles.sectionTitle}>每周回顾</Text>
                {!weeks.length ? <Text style={styles.emptyHint}>第一份回顾会在有记录的一周结束后整理。</Text> : null}
                {weeks.map((week) => (
                  <Pressable
                    key={week.id}
                    onPress={() => router.push({ pathname: '/weekly-report/[id]', params: { id: week.id } })}
                    style={styles.card}
                  >
                    <Text style={styles.weekLabel}>{week.week_start} 起的一周</Text>
                    <Text style={styles.cardTitle}>
                      {week.status === 'completed' ? '本周的新发现'
                        : week.status === 'insufficient' ? '本周留下的线索'
                        : week.status === 'no_records' ? '本周没有新记录'
                        : week.status === 'processing' ? '正在整理新的发现'
                        : week.status === 'stale' ? '记录已调整，等待重新整理'
                        : '整理暂时没有完成'}
                    </Text>
                    {week.digest[0] ? <Text numberOfLines={2} style={styles.body}>{week.digest[0].summary}</Text> : null}
                  </Pressable>
                ))}
                {items.length ? <Text style={styles.sectionTitle}>过往发现</Text> : null}
                {loadError ? <Text style={styles.status}>{loadError}</Text> : null}
              </View>
            }
            renderItem={({ item }) => (
              <Pressable onPress={() => router.push({ pathname: '/insight/[id]', params: { id: item.id } })} style={styles.card}>
                <View style={styles.cardTop}>
                  <Text style={styles.cardTitle}>{item.title}</Text>
                  {item.verification_status === 'WEAK' ? <Text style={styles.weak}>可能的观察</Text> : null}
                </View>
                <Text numberOfLines={3} style={styles.body}>{item.body}</Text>
                <View style={styles.metaRow}>
                  <Ionicons name="ellipse-outline" size={13} color={colors.muted} />
                  <Text style={styles.meta}>
                    来自 {new Set((item.insight_evidence ?? []).map((evidence) => evidence.moment_id)).size} 个证据时刻
                  </Text>
                </View>
              </Pressable>
            )}
          />
        )
      ) : (
        <KeyboardAvoidingView style={styles.ask} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
          <View>
            <Text style={styles.askTitle}>关于过去的自己，你想知道什么？</Text>
            <Text style={styles.askHint}>答案只会使用你留下的真实记录，并附上可以回看的证据。</Text>
          </View>
          <View>
            <View style={styles.askBox}>
              <TextInput
                value={question}
                onChangeText={setQuestion}
                placeholder="例如：过去几个月，我对工作的想法有变化吗？"
                placeholderTextColor={colors.muted}
                multiline
                style={styles.askInput}
              />
              <Pressable disabled={!question.trim() || asking} onPress={ask} style={[styles.askButton, (!question.trim() || asking) && styles.disabled]}>
                <Ionicons name="arrow-up" size={21} color={colors.white} />
              </Pressable>
            </View>
            {asking ? <Text style={styles.status}>正在翻一翻过去的你……</Text> : message ? <Text style={styles.status}>{message}</Text> : null}
          </View>
        </KeyboardAvoidingView>
      )}
    </Screen>
  );
}

const styles = StyleSheet.create({
  screen: { paddingBottom: 0 },
  title: { color: colors.text, fontSize: 27, fontWeight: '600', marginTop: 4 },
  tabs: { flexDirection: 'row', marginTop: 18, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: colors.line },
  tab: { paddingVertical: 12, marginRight: 28 },
  tabText: { color: colors.muted, fontSize: 15 },
  tabActive: { color: colors.accent, fontWeight: '600' },
  list: { paddingTop: 8, paddingBottom: 30 },
  sectionTitle: { color: colors.text, fontSize: 16, fontWeight: '600', marginTop: 22 },
  weekLabel: { color: colors.muted, fontSize: 12, marginBottom: 8 },
  emptyHint: { color: colors.muted, fontSize: 13, lineHeight: 20, marginTop: 14 },
  progress: { backgroundColor: colors.soft, borderRadius: radii.medium, padding: 15, marginTop: 12 },
  progressTitle: { color: colors.text, fontSize: 14, fontWeight: '600' },
  progressText: { color: colors.muted, fontSize: 12, marginTop: 5 },
  emptyDiscoveries: { alignItems: 'center' },
  refreshButton: { marginTop: -10, paddingHorizontal: 16, paddingVertical: 9, borderRadius: radii.pill, backgroundColor: colors.soft },
  refreshText: { color: colors.accent, fontSize: 13, fontWeight: '600' },
  card: { paddingVertical: 22, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: colors.line },
  cardTop: { flexDirection: 'row', alignItems: 'center', gap: 10 },
  cardTitle: { color: colors.text, fontSize: 18, fontWeight: '600', flex: 1 },
  weak: { color: colors.muted, fontSize: 11, backgroundColor: colors.soft, paddingHorizontal: 8, paddingVertical: 4, borderRadius: radii.pill },
  body: { color: colors.text, fontSize: 15, lineHeight: 24, marginTop: 10 },
  metaRow: { flexDirection: 'row', alignItems: 'center', gap: 5, marginTop: 13 },
  meta: { color: colors.muted, fontSize: 12 },
  ask: { flex: 1, justifyContent: 'space-between', paddingTop: 46, paddingBottom: spacing.lg },
  askTitle: { color: colors.text, fontSize: 24, lineHeight: 35, fontWeight: '500', maxWidth: 310 },
  askHint: { color: colors.muted, fontSize: 14, lineHeight: 22, marginTop: 14, maxWidth: 330 },
  askBox: { backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, borderRadius: radii.large, padding: 12 },
  askInput: { color: colors.text, minHeight: 96, maxHeight: 180, fontSize: 16, lineHeight: 24, textAlignVertical: 'top' },
  askButton: { alignSelf: 'flex-end', width: 42, height: 42, borderRadius: 21, alignItems: 'center', justifyContent: 'center', backgroundColor: colors.accent },
  disabled: { opacity: 0.35 },
  status: { color: colors.muted, fontSize: 13, textAlign: 'center', marginTop: 10 },
});
