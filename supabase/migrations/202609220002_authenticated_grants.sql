-- Apply this after 202609220001_initial.sql when the Supabase project was
-- created with "Automatically expose new tables" disabled.
-- RLS remains the row-level authorization boundary for every personal table.

grant usage on schema public to authenticated;

grant select, insert, update, delete on table
  public.moments,
  public.threads,
  public.thread_messages,
  public.memories,
  public.memory_sources,
  public.insight_candidates,
  public.insights,
  public.insight_evidence,
  public.product_events,
  public.ai_runs
to authenticated;
