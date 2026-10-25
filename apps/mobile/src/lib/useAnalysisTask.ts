import { useCallback, useEffect, useRef, useState } from 'react';

import { api, ApiError } from '@/lib/api';

export type AnalysisTask = { id: string; status: string; progress: string; result_id?: string; error_code?: string };

const failureMessages: Record<string, string> = {
  BUDGET_TIME: '这次分析超过了时间限制，尚未生成洞察。可以缩小问题范围后再试。',
  BUDGET_CALLS: '这次分析已达到模型调用上限，尚未生成洞察。不会自动重新分析。',
  BUDGET_TOKENS: '这次分析已达到内容处理上限，尚未生成洞察。可以缩小问题范围后再试。',
  BUDGET_INPUT_SIZE: '这次需要处理的内容太多，请缩小问题范围。',
  BUDGET_EVIDENCE_COUNT: '这次涉及的记录太多，请缩小主题或时间范围。',
  InsufficientEvidenceError: '本次未能找到足够依据，尚未生成洞察。不会自动重新分析。',
};

export function useAnalysisTask(onComplete: (id: string) => void) {
  const [task, setTask] = useState<AnalysisTask | null>(null);
  const [notice, setNotice] = useState('');
  const completed = useRef<string | null>(null);
  const onDone = useRef(onComplete);
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { onDone.current = onComplete; }, [onComplete]);
  const refresh = useCallback(async (id: string) => {
    const next = await api.get<AnalysisTask>(`/jobs/${id}`);
    if (!mounted.current) return next;
    setTask(next);
    if (next.status === 'succeeded' && next.result_id && completed.current !== id) {
      completed.current = id;
      onDone.current(next.result_id);
    }
    setNotice(next.status === 'failed' ? (failureMessages[next.error_code ?? ''] ?? '这次没有完成整理。重新发起会进行新的分析。')
      : next.status === 'cancelled' ? '整理已停止，或来源记录已变化。' : next.progress);
    return next;
  }, []);
  useEffect(() => {
    let active = true;
    void api.get<AnalysisTask[]>('/jobs').then((rows) => {
      if (active && rows[0]) setTask(rows[0]);
    }).catch(() => undefined);
    return () => { active = false; };
  }, []);
  useEffect(() => {
    if (!task || !['queued', 'running'].includes(task.status)) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    let count = 0;
    const poll = async () => {
      if (stopped) return;
      try {
        const next = await refresh(task.id);
        if (stopped || !['queued', 'running'].includes(next.status)) return;
      } catch (error) {
        if (error instanceof ApiError && [401, 404, 410].includes(error.status)) {
          if (!stopped) setNotice('当前无法访问这个任务，请检查登录状态。');
          return;
        }
        if (!stopped) setNotice('暂时无法获取进度，不会自动重新提交。');
      }
      if (!stopped && ++count < 180) timer = setTimeout(poll, 4000);
      else if (!stopped) setNotice('已暂停自动查询，可手动查看进度，不会重新生成。');
    };
    timer = setTimeout(poll, 1000);
    return () => { stopped = true; clearTimeout(timer); };
  // Status changes do not reset the polling budget.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [task?.id, refresh]);
  const cancel = async () => {
    if (!task) return;
    await api.post(`/jobs/${task.id}/cancel`);
    await refresh(task.id);
  };
  return { task, setTask, notice, setNotice, refresh, cancel, running: !!task && ['queued', 'running'].includes(task.status) };
}
