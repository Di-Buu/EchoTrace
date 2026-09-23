import { router, useLocalSearchParams } from 'expo-router';
import { useEffect, useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { Screen } from '@/components/Screen';
import { StateMessage } from '@/components/StateMessage';
import { api } from '@/lib/api';
import { colors, radii } from '@/lib/theme';
import type { WeeklyReport } from '@/lib/types';

export default function WeeklyReportDetail() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const [report, setReport] = useState<WeeklyReport | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!id) return;
    void api.get<WeeklyReport>(`/weekly-reports/${id}`)
      .then(setReport)
      .catch(() => setError('这份回顾暂时无法打开'));
  }, [id]);

  if (error) return <Screen><StateMessage text={error} /></Screen>;
  if (!report) return <Screen><StateMessage text="正在翻开这周的记录…" loading /></Screen>;

  return (
    <Screen scroll>
      <Text style={styles.eyebrow}>{report.week_start} 起的一周</Text>
      <Text style={styles.title}>每周回顾</Text>
      {report.status === 'no_records' ? (
        <Text style={styles.muted}>这周还没有新的记录。等你想说的时候，树洞仍在这里。</Text>
      ) : report.status === 'processing' ? (
        <Text style={styles.muted}>正在整理新的发现。之前的回顾仍可查看。</Text>
      ) : report.status === 'stale' ? (
        <Text style={styles.muted}>部分来源记录已调整，这份回顾需要重新整理。</Text>
      ) : report.status === 'failed' ? (
        <Text style={styles.muted}>这次整理没有完成，稍后打开洞察页会继续尝试。</Text>
      ) : (
        <>
          {report.status === 'insufficient' ? (
            <Text style={styles.muted}>留下的记录还不足以支持可靠的长期发现。下面是本周原文的简要整理。</Text>
          ) : null}
          {report.digest.map((card, index) => (
            <View key={`${card.topic}-${index}`} style={styles.card}>
              <Text style={styles.topic}>{card.topic}</Text>
              <Text style={styles.summary}>{card.summary}</Text>
              <Text style={styles.sourceLabel}>来自这些时刻</Text>
              {card.source_moment_ids.map((momentId) => (
                <Pressable
                  key={momentId}
                  onPress={() => router.push({ pathname: '/moments/[id]', params: { id: momentId } })}
                  style={styles.source}
                >
                  <Text style={styles.sourceText}>查看原始记录 · {momentId.slice(0, 8)}</Text>
                </Pressable>
              ))}
            </View>
          ))}
          {report.insight_id ? (
            <Pressable
              onPress={() => router.push({ pathname: '/insight/[id]', params: { id: report.insight_id! } })}
              style={styles.insightLink}
            >
              <Text style={styles.insightText}>查看有证据支持的本周发现 →</Text>
            </Pressable>
          ) : null}
        </>
      )}
    </Screen>
  );
}

const styles = StyleSheet.create({
  eyebrow: { color: colors.muted, fontSize: 12, marginTop: 8 },
  title: { color: colors.text, fontSize: 27, fontWeight: '600', marginTop: 8 },
  muted: { color: colors.muted, fontSize: 14, lineHeight: 23, marginTop: 20 },
  card: { borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: colors.line, paddingVertical: 20 },
  topic: { color: colors.accent, fontSize: 13, fontWeight: '600' },
  summary: { color: colors.text, fontSize: 16, lineHeight: 26, marginTop: 9 },
  sourceLabel: { color: colors.muted, fontSize: 12, marginTop: 15 },
  source: { paddingVertical: 9 },
  sourceText: { color: colors.accent, fontSize: 13 },
  insightLink: { backgroundColor: colors.soft, borderRadius: radii.medium, padding: 15, marginTop: 24, marginBottom: 30 },
  insightText: { color: colors.accent, fontSize: 14, fontWeight: '600' },
});
