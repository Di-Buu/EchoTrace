create schema if not exists extensions;
create extension if not exists vector with schema extensions;
create extension if not exists pg_trgm with schema extensions;

create table public.moments (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  content text not null check (char_length(content) between 1 and 20000),
  mode text not null check (mode in ('capture', 'chat')),
  input_type text not null default 'text' check (input_type in ('text', 'voice')),
  memory_enabled boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index moments_user_created_idx on public.moments(user_id, created_at desc);

create table public.threads (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  source_moment_id uuid not null unique references public.moments(id) on delete cascade,
  summary text,
  summary_message_count integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index threads_user_updated_idx on public.threads(user_id, updated_at desc);

alter table public.moments
  add column thread_id uuid references public.threads(id) on delete set null;
create index moments_thread_idx on public.moments(thread_id, created_at asc);

create table public.thread_messages (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  thread_id uuid not null references public.threads(id) on delete cascade,
  role text not null check (role in ('user', 'assistant')),
  content text not null check (char_length(content) between 1 and 20000),
  input_type text not null default 'text' check (input_type in ('text', 'voice', 'system')),
  evidence_moment_ids uuid[] not null default '{}',
  created_at timestamptz not null default now()
);

create index thread_messages_thread_created_idx
  on public.thread_messages(thread_id, created_at asc);

create table public.memories (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  memory_type text not null check (
    memory_type in ('event', 'view', 'interest', 'goal', 'decision', 'question', 'state')
  ),
  content text not null check (char_length(content) between 1 and 4000),
  source_type text not null default 'personal' check (source_type in ('personal', 'reference')),
  confidence real not null check (confidence between 0 and 1),
  status text not null default 'active' check (
    status in ('active', 'superseded', 'disputed', 'deleted')
  ),
  occurred_at timestamptz,
  valid_from timestamptz,
  valid_to timestamptz,
  supersedes_memory_id uuid references public.memories(id) on delete set null,
  embedding extensions.vector,
  embedding_model text,
  embedding_dimension integer,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index memories_user_status_updated_idx
  on public.memories(user_id, status, updated_at desc);
create index memories_content_trgm_idx
  on public.memories using gin (content extensions.gin_trgm_ops);

create table public.memory_sources (
  memory_id uuid not null references public.memories(id) on delete cascade,
  moment_id uuid not null references public.moments(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  primary key (memory_id, moment_id)
);

create index memory_sources_user_idx on public.memory_sources(user_id);

create table public.insight_candidates (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  candidate_key text not null,
  reason text not null,
  memory_ids uuid[] not null default '{}',
  status text not null default 'pending' check (status in ('pending', 'processing', 'completed', 'dismissed')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(user_id, candidate_key)
);

create table public.insights (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  insight_type text not null check (
    insight_type in (
      'temporal_change', 'repeated_pattern', 'idea_return', 'unresolved_loop',
      'cross_record_relation', 'fact'
    )
  ),
  trigger_type text not null check (trigger_type in ('automatic', 'user_query')),
  query text,
  title text not null,
  body text not null,
  limitation text,
  verification_status text not null check (verification_status in ('PASS', 'WEAK')),
  time_start timestamptz,
  time_end timestamptz,
  cache_key text,
  evidence_version text not null,
  agent_version text not null,
  feedback text check (feedback is null or feedback in ('match', 'partial', 'mismatch')),
  status text not null default 'active' check (status in ('active', 'stale', 'deleted')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index insights_user_cache_idx
  on public.insights(user_id, cache_key) where cache_key is not null and status = 'active';
create index insights_user_created_idx on public.insights(user_id, created_at desc);

create table public.insight_evidence (
  insight_id uuid not null references public.insights(id) on delete cascade,
  moment_id uuid not null references public.moments(id) on delete cascade,
  memory_id uuid references public.memories(id) on delete set null,
  user_id uuid not null references auth.users(id) on delete cascade,
  stance text not null default 'support' check (stance in ('support', 'counter')),
  primary key (insight_id, moment_id, stance)
);

create index insight_evidence_user_idx on public.insight_evidence(user_id);

create table public.product_events (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  event_name text not null,
  session_id text,
  request_id text,
  client_version text,
  properties jsonb not null default '{}',
  created_at timestamptz not null default now()
);

create index product_events_user_created_idx
  on public.product_events(user_id, created_at desc);

create table public.ai_runs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  request_id text not null,
  trace_id text not null,
  task_type text not null,
  agent_name text,
  model text,
  prompt_version text,
  success boolean not null,
  latency_ms integer,
  input_tokens integer,
  output_tokens integer,
  tool_calls integer not null default 0,
  retrieval_candidates integer not null default 0,
  evidence_count integer not null default 0,
  failure_code text,
  metadata jsonb not null default '{}',
  created_at timestamptz not null default now()
);

create index ai_runs_user_created_idx on public.ai_runs(user_id, created_at desc);
create index ai_runs_user_trace_idx on public.ai_runs(user_id, trace_id, created_at asc);

create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create trigger moments_set_updated_at before update on public.moments
for each row execute function public.set_updated_at();
create trigger threads_set_updated_at before update on public.threads
for each row execute function public.set_updated_at();
create trigger memories_set_updated_at before update on public.memories
for each row execute function public.set_updated_at();
create trigger insight_candidates_set_updated_at before update on public.insight_candidates
for each row execute function public.set_updated_at();
create trigger insights_set_updated_at before update on public.insights
for each row execute function public.set_updated_at();

-- RLS protects rows; these triggers also prove that every referenced parent has the same owner.
create or replace function public.enforce_moment_thread_owner()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  if new.thread_id is not null and not exists (
    select 1 from public.threads t where t.id = new.thread_id and t.user_id = new.user_id
  ) then
    raise exception 'thread owner mismatch';
  end if;
  return new;
end;
$$;

create or replace function public.enforce_thread_source_owner()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  if not exists (
    select 1 from public.moments m where m.id = new.source_moment_id and m.user_id = new.user_id
  ) then
    raise exception 'source moment owner mismatch';
  end if;
  return new;
end;
$$;

create or replace function public.enforce_message_thread_owner()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  if not exists (
    select 1 from public.threads t where t.id = new.thread_id and t.user_id = new.user_id
  ) then
    raise exception 'message thread owner mismatch';
  end if;
  return new;
end;
$$;

create or replace function public.enforce_memory_relation_owner()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  if new.supersedes_memory_id is not null and not exists (
    select 1 from public.memories m where m.id = new.supersedes_memory_id and m.user_id = new.user_id
  ) then
    raise exception 'superseded memory owner mismatch';
  end if;
  return new;
end;
$$;

create or replace function public.enforce_memory_source_owner()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  if not exists (
    select 1 from public.memories m where m.id = new.memory_id and m.user_id = new.user_id
  ) or not exists (
    select 1 from public.moments mo where mo.id = new.moment_id and mo.user_id = new.user_id
  ) then
    raise exception 'memory source owner mismatch';
  end if;
  return new;
end;
$$;

create or replace function public.enforce_insight_evidence_owner()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  if not exists (
    select 1 from public.insights i where i.id = new.insight_id and i.user_id = new.user_id
  ) or not exists (
    select 1 from public.moments mo where mo.id = new.moment_id and mo.user_id = new.user_id
  ) or (
    new.memory_id is not null and not exists (
      select 1 from public.memories m where m.id = new.memory_id and m.user_id = new.user_id
    )
  ) then
    raise exception 'insight evidence owner mismatch';
  end if;
  return new;
end;
$$;

create trigger moments_enforce_thread_owner before insert or update on public.moments
for each row execute function public.enforce_moment_thread_owner();
create trigger threads_enforce_source_owner before insert or update on public.threads
for each row execute function public.enforce_thread_source_owner();
create trigger messages_enforce_thread_owner before insert or update on public.thread_messages
for each row execute function public.enforce_message_thread_owner();
create trigger memories_enforce_relation_owner before insert or update on public.memories
for each row execute function public.enforce_memory_relation_owner();
create trigger memory_sources_enforce_owner before insert or update on public.memory_sources
for each row execute function public.enforce_memory_source_owner();
create trigger insight_evidence_enforce_owner before insert or update on public.insight_evidence
for each row execute function public.enforce_insight_evidence_owner();

alter table public.moments enable row level security;
alter table public.threads enable row level security;
alter table public.thread_messages enable row level security;
alter table public.memories enable row level security;
alter table public.memory_sources enable row level security;
alter table public.insight_candidates enable row level security;
alter table public.insights enable row level security;
alter table public.insight_evidence enable row level security;
alter table public.product_events enable row level security;
alter table public.ai_runs enable row level security;

-- Project creation may disable automatic table exposure. Grant the signed-in
-- role table access explicitly; the policies below still enforce per-user rows.
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

create policy moments_owner_all on public.moments
for all using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy threads_owner_all on public.threads
for all using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy thread_messages_owner_all on public.thread_messages
for all using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy memories_owner_all on public.memories
for all using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy memory_sources_owner_all on public.memory_sources
for all using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy insight_candidates_owner_all on public.insight_candidates
for all using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy insights_owner_all on public.insights
for all using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy insight_evidence_owner_all on public.insight_evidence
for all using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy product_events_owner_all on public.product_events
for all using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy ai_runs_owner_all on public.ai_runs
for all using (user_id = auth.uid()) with check (user_id = auth.uid());

create or replace function public.search_personal_memory(
  p_query_text text,
  p_query_embedding extensions.vector,
  p_match_count integer default 8,
  p_from timestamptz default null,
  p_to timestamptz default null
)
returns table (
  memory_id uuid,
  memory_content text,
  memory_type text,
  confidence real,
  moment_id uuid,
  moment_content text,
  occurred_at timestamptz,
  score double precision
)
language sql
stable
security invoker
set search_path = public, extensions
as $$
  with scoped as (
    select m.*
    from public.memories m
    where m.user_id = auth.uid()
      and m.status = 'active'
      and m.source_type = 'personal'
      and m.embedding is not null
      and vector_dims(m.embedding) = vector_dims(p_query_embedding)
      and (p_from is null or coalesce(m.occurred_at, m.created_at) >= p_from)
      and (p_to is null or coalesce(m.occurred_at, m.created_at) <= p_to)
  ), ranked as (
    select
      m.id,
      m.content,
      m.memory_type,
      m.confidence,
      (
        (1 - (m.embedding <=> p_query_embedding)) * 0.82
        + greatest(similarity(m.content, p_query_text), 0) * 0.13
        + least(0.05, 0.05 / greatest(1, extract(day from now() - coalesce(m.occurred_at, m.created_at))))
      ) as combined_score
    from scoped m
    order by combined_score desc
    limit greatest(1, least(p_match_count, 30))
  )
  select
    r.id,
    r.content,
    r.memory_type,
    r.confidence,
    mo.id,
    mo.content,
    mo.created_at,
    r.combined_score
  from ranked r
  join public.memory_sources ms on ms.memory_id = r.id and ms.user_id = auth.uid()
  join public.moments mo on mo.id = ms.moment_id and mo.user_id = auth.uid()
  where mo.memory_enabled = true
  order by r.combined_score desc, mo.created_at asc;
$$;

revoke all on function public.search_personal_memory(text, extensions.vector, integer, timestamptz, timestamptz) from public;
grant execute on function public.search_personal_memory(text, extensions.vector, integer, timestamptz, timestamptz) to authenticated;
