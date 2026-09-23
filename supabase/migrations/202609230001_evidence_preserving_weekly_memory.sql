-- Raw Moments remain the source of truth. These indexes and summaries are derived.

create table if not exists public.moment_index_chunks (
  id uuid primary key default gen_random_uuid(),
  moment_id uuid not null references public.moments(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  chunk_index integer not null check (chunk_index >= 0),
  content text not null check (char_length(content) between 1 and 4000),
  embedding extensions.vector not null,
  embedding_model text not null,
  embedding_dimension integer not null,
  created_at timestamptz not null default now(),
  unique (moment_id, chunk_index)
);

create index if not exists moment_index_chunks_user_moment_idx
  on public.moment_index_chunks(user_id, moment_id);

create index if not exists moments_content_trgm_idx
  on public.moments using gin (content extensions.gin_trgm_ops);

create table if not exists public.weekly_reports (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  week_start date not null,
  week_end date not null,
  status text not null check (status in ('processing', 'completed', 'insufficient', 'no_records', 'failed', 'stale')),
  digest jsonb not null default '[]'::jsonb,
  insight_id uuid references public.insights(id) on delete set null,
  input_version text,
  error_code text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, week_start),
  check (week_end = week_start + 7)
);

create index if not exists weekly_reports_user_week_idx
  on public.weekly_reports(user_id, week_start desc);

create table if not exists public.weekly_summary_cards (
  id uuid primary key default gen_random_uuid(),
  report_id uuid not null references public.weekly_reports(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  topic text not null,
  summary text not null check (char_length(summary) between 1 and 1000),
  source_moment_ids uuid[] not null,
  time_start timestamptz,
  time_end timestamptz,
  embedding extensions.vector,
  embedding_model text,
  embedding_dimension integer,
  created_at timestamptz not null default now(),
  check (cardinality(source_moment_ids) > 0)
);

create index if not exists weekly_summary_cards_user_report_idx
  on public.weekly_summary_cards(user_id, report_id);
create index if not exists weekly_summary_cards_summary_trgm_idx
  on public.weekly_summary_cards using gin (summary extensions.gin_trgm_ops);

create trigger weekly_reports_set_updated_at before update on public.weekly_reports
for each row execute function public.set_updated_at();

create or replace function public.enforce_weekly_report_owner()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  if new.insight_id is not null and not exists (
    select 1 from public.insights i where i.id = new.insight_id and i.user_id = new.user_id
  ) then
    raise exception 'weekly report insight owner mismatch';
  end if;
  return new;
end;
$$;

create trigger weekly_reports_enforce_owner before insert or update on public.weekly_reports
for each row execute function public.enforce_weekly_report_owner();

create or replace function public.enforce_weekly_card_owner()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  if not exists (
    select 1 from public.weekly_reports r where r.id = new.report_id and r.user_id = new.user_id
  ) or exists (
    select 1 from unnest(new.source_moment_ids) as source_id
    where not exists (
      select 1 from public.moments m
      where m.id = source_id and m.user_id = new.user_id and m.memory_enabled = true
    )
  ) then
    raise exception 'weekly card source owner mismatch';
  end if;
  return new;
end;
$$;

create trigger weekly_summary_cards_enforce_owner before insert or update on public.weekly_summary_cards
for each row execute function public.enforce_weekly_card_owner();

create or replace function public.enforce_moment_chunk_owner()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  if not exists (
    select 1 from public.moments m
    where m.id = new.moment_id and m.user_id = new.user_id and m.memory_enabled = true
  ) then
    raise exception 'moment chunk owner mismatch';
  end if;
  return new;
end;
$$;

create trigger moment_index_chunks_enforce_owner before insert or update on public.moment_index_chunks
for each row execute function public.enforce_moment_chunk_owner();

alter table public.moment_index_chunks enable row level security;
alter table public.weekly_reports enable row level security;
alter table public.weekly_summary_cards enable row level security;
grant select, insert, update, delete on table
  public.moment_index_chunks, public.weekly_reports, public.weekly_summary_cards to authenticated;

create policy moment_index_chunks_owner_all on public.moment_index_chunks
for all using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy weekly_reports_owner_all on public.weekly_reports
for all using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy weekly_summary_cards_owner_all on public.weekly_summary_cards
for all using (user_id = auth.uid()) with check (user_id = auth.uid());

create or replace function public.search_personal_moment(
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
language sql stable security invoker set search_path = public, extensions as $$
  with semantic as (
    select distinct on (c.moment_id) c.moment_id,
      1 - (c.embedding <=> p_query_embedding) as similarity_score,
      c.content as matched_content
    from public.moment_index_chunks c
    where c.user_id = auth.uid()
      and vector_dims(c.embedding) = vector_dims(p_query_embedding)
    order by c.moment_id, c.embedding <=> p_query_embedding
  )
  select ranked.memory_id,
    ranked.memory_content,
    ranked.memory_type,
    ranked.confidence,
    ranked.moment_id,
    ranked.moment_content,
    ranked.occurred_at,
    ranked.score
  from (
    select
      null::uuid as memory_id,
      left(m.content, 4000) as memory_content,
      'moment'::text as memory_type,
      1::real as confidence,
      m.id as moment_id,
      coalesce(s.matched_content, m.content) as moment_content,
      m.created_at as occurred_at,
      (
        coalesce(s.similarity_score, 0) * 0.82
        + greatest(similarity(m.content, p_query_text), 0) * 0.15
        + least(0.03, 0.03 / greatest(1, extract(day from now() - m.created_at)))
      )::double precision as score
    from public.moments m
    left join semantic s on s.moment_id = m.id
    where m.user_id = auth.uid()
      and m.memory_enabled = true
      and not exists (
        select 1 from public.memory_sources legacy_source
        join public.memories legacy on legacy.id = legacy_source.memory_id
        where legacy_source.moment_id = m.id and legacy.user_id = auth.uid()
          and legacy.status = 'deleted'
      )
      and (p_from is null or m.created_at >= p_from)
      and (p_to is null or m.created_at < p_to)
  ) ranked
  order by ranked.score desc
  limit greatest(1, least(p_match_count, 40));
$$;

revoke all on function public.search_personal_moment(text, extensions.vector, integer, timestamptz, timestamptz) from public;
grant execute on function public.search_personal_moment(text, extensions.vector, integer, timestamptz, timestamptz) to authenticated;

create or replace function public.search_weekly_summary_card(
  p_query_text text,
  p_query_embedding extensions.vector,
  p_match_count integer default 6
)
returns table (
  card_id uuid,
  summary text,
  source_moment_ids uuid[],
  score double precision
)
language sql stable security invoker set search_path = public, extensions as $$
  select ranked.card_id,
    ranked.summary,
    ranked.source_moment_ids,
    ranked.score
  from (
    select
      c.id as card_id,
      c.summary,
      c.source_moment_ids,
      (
        case when c.embedding is not null
          and vector_dims(c.embedding) = vector_dims(p_query_embedding)
        then (1 - (c.embedding <=> p_query_embedding)) * 0.85
        else 0 end
        + greatest(similarity(c.summary, p_query_text), 0) * 0.15
      )::double precision as score
  from public.weekly_summary_cards c
  join public.weekly_reports r on r.id = c.report_id and r.user_id = auth.uid()
  where c.user_id = auth.uid()
    and r.status in ('completed', 'insufficient')
    and not exists (
      select 1 from unnest(c.source_moment_ids) as source_id
      where not exists (
        select 1 from public.moments m
        where m.id = source_id and m.user_id = auth.uid() and m.memory_enabled = true
          and not exists (
            select 1 from public.memory_sources legacy_source
            join public.memories legacy on legacy.id = legacy_source.memory_id
            where legacy_source.moment_id = m.id and legacy.user_id = auth.uid()
              and legacy.status = 'deleted'
          )
      )
    )
  ) ranked
  order by ranked.score desc
  limit greatest(1, least(p_match_count, 20));
$$;

revoke all on function public.search_weekly_summary_card(text, extensions.vector, integer) from public;
grant execute on function public.search_weekly_summary_card(text, extensions.vector, integer) to authenticated;
