export type CaptureMode = 'capture' | 'chat';

export type Moment = {
  id: string;
  user_id: string;
  content: string;
  mode: CaptureMode;
  input_type: 'text';
  memory_enabled: boolean;
  thread_id: string | null;
  created_at: string;
  updated_at?: string;
  messages?: ThreadMessage[];
};

export type ThreadMessage = {
  id: string;
  thread_id: string;
  role: 'user' | 'assistant';
  content: string;
  evidence_moment_ids: string[];
  created_at: string;
};

export type ChatResponse = {
  thread_id: string;
  moment_id: string;
  message: ThreadMessage;
  evidence_moment_ids: string[];
  used_long_term_memory: boolean;
};

export type Memory = {
  id: string;
  memory_type: string;
  content: string;
  confidence: number;
  status: 'active' | 'disputed';
  occurred_at: string | null;
  updated_at: string;
  memory_sources: { moment_id: string }[];
};

export type InsightEvidence = {
  moment_id: string;
  memory_id: string | null;
  stance: 'support' | 'counter';
  moments?: Pick<Moment, 'id' | 'content' | 'created_at' | 'input_type'>;
};

export type Insight = {
  id: string;
  insight_type: string;
  trigger_type: 'automatic' | 'user_query';
  query?: string | null;
  title: string;
  body: string;
  limitation?: string | null;
  verification_status: 'PASS' | 'WEAK';
  time_start?: string | null;
  time_end?: string | null;
  feedback?: 'match' | 'partial' | 'mismatch' | null;
  created_at: string;
  insight_evidence: InsightEvidence[];
};
