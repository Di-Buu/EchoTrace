import { router, useLocalSearchParams } from 'expo-router';
import { useEffect, useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { Screen } from '@/components/Screen';
import { StateMessage } from '@/components/StateMessage';
import { api } from '@/lib/api';
import { colors, radii, spacing } from '@/lib/theme';
import type { Insight, InsightEvidence } from '@/lib/types';

export default function InsightDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const [insight, setInsight] = useState<Insight | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!id) return;
    api.get<Insight>(`/insights/${id}`)
      .then((result) => {
        setInsight(result);
        void api.event('insight_opened', { insight_id: result.id }).catch(() => undefined);
      })
      .catch(() => setInsight(null))
      .finally(() => setLoading(false));
  }, [id]);

  async function feedback(rating: 'match' | 'partial' | 'mismatch') {
    if (!insight) return;
    const next = await api.post<Insight>(`/insights/${insight.id}/feedback`, { rating });
    setInsight((current) => current ? { ...current, feedback: next.feedback } : current);
  }

  if (loading) return <Screen><StateMessage text="正在打开证据…" loading /></Screen>;
  if (!insight) return <Screen><StateMessage text="这条洞察已失效或不存在" /></Screen>;

  const evidenceByMoment = new Map<string, {
    record: InsightEvidence;
    stances: Set<InsightEvidence['stance']>;
  }>();
  for (const item of insight.insight_evidence ?? []) {
    const existing = evidenceByMoment.get(item.moment_id);
    if (existing) {
      existing.stances.add(item.stance);
      if (!existing.record.moments && item.moments) existing.record = item;
    } else {
      evidenceByMoment.set(item.moment_id, {
        record: item,
        stances: new Set([item.stance]),
      });
    }
  }
  const evidence = [...evidenceByMoment.values()];
  return (
    <Screen scroll>
      <View style={styles.labelRow}>
        <Text style={styles.label}>{insight.trigger_type === 'automatic' ? 'AI 发现' : '你的问题'}</Text>
        {insight.verification_status === 'WEAK' ? <Text style={styles.weak}>可能的观察</Text> : null}
      </View>
      <Text style={styles.title}>{insight.title}</Text>
      <Text style={styles.body}>{insight.body}</Text>
      {insight.limitation ? <Text style={styles.limitation}>{insight.limitation}</Text> : null}

      <Text style={styles.sectionTitle}>来自这些时刻</Text>
      {!evidence.length ? <Text style={styles.muted}>证据正在整理中。</Text> : evidence.map(({ record: item, stances }) => (
        <Pressable
          key={item.moment_id}
          onPress={() => {
            void api.event('evidence_opened', { insight_id: insight.id, moment_id: item.moment_id }).catch(() => undefined);
            router.push({ pathname: '/moments/[id]', params: { id: item.moment_id } });
          }}
          style={styles.evidence}
        >
          <Text style={styles.evidenceDate}>{item.moments ? new Date(item.moments.created_at).toLocaleDateString('zh-CN') : '查看原始时刻'}</Text>
          <Text numberOfLines={4} style={styles.evidenceText}>{item.moments?.content ?? `证据 ${item.moment_id.slice(0, 8)}`}</Text>
          <Text style={styles.stance}>
            {stances.size > 1 ? '涉及支持与反例' : item.stance === 'counter' ? '可能的反例' : '支持证据'} · 查看原文
          </Text>
        </Pressable>
      ))}

      <Text style={styles.sectionTitle}>这个观察符合你吗？</Text>
      <View style={styles.feedbackRow}>
        {([['match', '符合'], ['partial', '部分符合'], ['mismatch', '不符合']] as const).map(([value, label]) => (
          <Pressable key={value} onPress={() => feedback(value)} style={[styles.feedback, insight.feedback === value && styles.feedbackActive]}>
            <Text style={[styles.feedbackText, insight.feedback === value && styles.feedbackTextActive]}>{label}</Text>
          </Pressable>
        ))}
      </View>
    </Screen>
  );
}

const styles = StyleSheet.create({
  labelRow: { flexDirection: 'row', alignItems: 'center', gap: 10, marginTop: 12 },
  label: { color: colors.accent, fontSize: 13, fontWeight: '600' },
  weak: { color: colors.muted, fontSize: 11, backgroundColor: colors.soft, paddingHorizontal: 8, paddingVertical: 4, borderRadius: radii.pill },
  title: { color: colors.text, fontSize: 27, lineHeight: 38, fontWeight: '600', marginTop: 16 },
  body: { color: colors.text, fontSize: 17, lineHeight: 29, marginTop: 18 },
  limitation: { color: colors.muted, fontSize: 14, lineHeight: 22, marginTop: 16, paddingLeft: 12, borderLeftWidth: 2, borderLeftColor: colors.soft },
  sectionTitle: { color: colors.text, fontSize: 18, fontWeight: '600', marginTop: 38, marginBottom: 12 },
  muted: { color: colors.muted, fontSize: 14 },
  evidence: { paddingVertical: 16, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: colors.line },
  evidenceDate: { color: colors.muted, fontSize: 12 },
  evidenceText: { color: colors.text, fontSize: 15, lineHeight: 24, marginTop: 7 },
  stance: { color: colors.accent, fontSize: 12, marginTop: 9 },
  feedbackRow: { flexDirection: 'row', gap: spacing.sm, paddingBottom: 30 },
  feedback: { flex: 1, minHeight: 42, alignItems: 'center', justifyContent: 'center', borderRadius: radii.pill, borderWidth: 1, borderColor: colors.line },
  feedbackActive: { backgroundColor: colors.soft, borderColor: colors.accent },
  feedbackText: { color: colors.muted, fontSize: 13 },
  feedbackTextActive: { color: colors.accent, fontWeight: '600' },
});
