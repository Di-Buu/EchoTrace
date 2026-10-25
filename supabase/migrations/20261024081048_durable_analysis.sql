-- Additive migration. Apply once, separately from API/Worker startup.
begin;
create schema if not exists echotrace_private;
revoke all on schema echotrace_private from public, anon, authenticated;
grant usage on schema echotrace_private to service_role;

alter table public.moments add column revision bigint not null default 1;
alter table public.moments add column event_at timestamptz;
alter table public.moments add column event_time_source text not null default 'unknown'
  check (event_time_source in ('unknown','user_confirmed'));
alter table public.moments add constraint event_time_explicit check (
  (event_at is null and event_time_source='unknown') or
  (event_at is not null and event_time_source='user_confirmed'));
alter table public.moment_index_chunks add column source_revision bigint not null default 1;
alter table public.insights add column evidence_revisions jsonb not null default '{}';

create table public.background_jobs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  kind text not null check (kind in ('index','analysis','weekly')),
  payload jsonb not null default '{}',
  request_hash text not null,
  status text not null default 'queued' check (status in ('queued','running','succeeded','failed','cancelled')),
  progress text not null default '等待整理',
  attempts integer not null default 0,
  max_attempts integer not null default 3 check (max_attempts between 1 and 5),
  available_at timestamptz not null default now(),
  lease_token uuid,
  lease_until timestamptz,
  deadline_at timestamptz,
  checkpoint jsonb not null default '{}',
  error_code text,
  result_id uuid references public.insights(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index jobs_due on public.background_jobs(available_at, created_at)
  where status in ('queued','running');
create unique index jobs_active_request on public.background_jobs(user_id,kind,request_hash)
  where status in ('queued','running');
alter table public.background_jobs enable row level security;
create policy jobs_owner_read on public.background_jobs for select to authenticated
  using (user_id = (select auth.uid()));
grant select on public.background_jobs to authenticated;
revoke insert,update,delete on public.background_jobs from authenticated,anon;
grant all on public.background_jobs to service_role;

create table echotrace_private.idempotency (
  user_id uuid not null references auth.users(id) on delete cascade,
  operation text not null,
  key text not null,
  request_hash text not null,
  response jsonb not null,
  expires_at timestamptz not null default now() + interval '7 days',
  primary key(user_id,operation,key)
);

-- All evidence writes and result commits take the same owner lock. No model I/O
-- takes place while it is held. Hash collisions only serialize unrelated users.
create function echotrace_private.owner_lock(p_owner uuid) returns void
language sql set search_path = public as $$
  select pg_advisory_xact_lock(hashtextextended(p_owner::text,0));
$$;

create function echotrace_private.moment_revision() returns trigger
language plpgsql security definer set search_path = public as $$
declare owner_id uuid := coalesce(new.user_id,old.user_id); changed boolean;
begin
  perform echotrace_private.owner_lock(owner_id);
  if tg_op = 'UPDATE' and new.user_id <> old.user_id then
    raise exception 'OWNER_IMMUTABLE' using errcode='42501';
  end if;
  changed := tg_op <> 'UPDATE';
  if tg_op = 'UPDATE' then
    changed := (new.content,new.memory_enabled,new.event_at,new.event_time_source,new.created_at)
      is distinct from (old.content,old.memory_enabled,old.event_at,old.event_time_source,old.created_at);
    new.revision := old.revision + case when changed then 1 else 0 end;
  end if;
  if changed and tg_op <> 'INSERT' then
    update public.insights set status='stale' where user_id=owner_id and status='active';
    update public.weekly_reports set status='stale' where user_id=owner_id;
    update public.background_jobs set status='cancelled',error_code='EVIDENCE_CHANGED',updated_at=now()
      where user_id=owner_id and status in ('queued','running') and kind in ('analysis','weekly');
    delete from public.moment_index_chunks where moment_id=old.id and user_id=owner_id;
    update public.background_jobs set status='cancelled',error_code='SOURCE_CHANGED',updated_at=now()
      where user_id=owner_id and kind='index' and payload->>'moment_id'=old.id::text
      and status in ('queued','running');
  end if;
  if tg_op = 'DELETE' then
    update public.background_jobs set status='cancelled',error_code='SOURCE_REMOVED'
      where user_id=owner_id and kind='index' and payload->>'moment_id'=old.id::text
      and status in ('queued','running');
    return old;
  end if;
  return new;
end;
$$;
create trigger a_moment_revision before insert or update or delete on public.moments
  for each row execute function echotrace_private.moment_revision();

create function echotrace_private.enqueue_index() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if new.memory_enabled and (tg_op='INSERT' or new.revision <> old.revision) then
    insert into public.background_jobs(user_id,kind,payload,request_hash,max_attempts)
    values(new.user_id,'index',jsonb_build_object('moment_id',new.id,'revision',new.revision),
      new.id::text || ':' || new.revision::text,3) on conflict do nothing;
  end if;
  return new;
end;
$$;
create trigger z_enqueue_index after insert or update on public.moments
  for each row execute function echotrace_private.enqueue_index();

-- Public API exposes only fixed operations, never a caller-supplied owner.
create function public.submit_operation(p_operation text,p_key text,p_hash text,p_payload jsonb)
returns jsonb language plpgsql security definer set search_path = public as $$
declare u uuid := auth.uid(); h text; prior echotrace_private.idempotency;
  m public.moments; j public.background_jobs; response jsonb;
begin
  if u is null then raise exception 'LOGIN_REQUIRED' using errcode='42501'; end if;
  if p_operation not in ('moment','analysis','index') or length(p_key) not between 1 and 128 then
    raise exception 'INVALID_OPERATION';
  end if;
  -- Compute inside DB as clients can call RPC directly. p_hash is informational.
  h := encode(sha256(convert_to(p_payload::text,'UTF8')),'hex');
  perform echotrace_private.owner_lock(u);
  select * into prior from echotrace_private.idempotency
    where user_id=u and operation=p_operation and key=p_key for update;
  if found and prior.expires_at > now() then
    if prior.request_hash <> h then raise exception 'IDEMPOTENCY_CONFLICT'; end if;
    return prior.response;
  end if;
  delete from echotrace_private.idempotency where user_id=u and operation=p_operation and key=p_key;
  if p_operation='moment' then
    if coalesce(length(trim(p_payload->>'content')),0) not between 1 and 20000 then
      raise exception 'INVALID_CONTENT';
    end if;
    insert into public.moments(user_id,content,mode,input_type,memory_enabled,event_at,event_time_source)
    values(u,p_payload->>'content',coalesce(p_payload->>'mode','capture'),'text',
      coalesce((p_payload->>'memory_enabled')::boolean,true),(p_payload->>'event_at')::timestamptz,
      case when p_payload->>'event_at' is null then 'unknown' else 'user_confirmed' end) returning * into m;
    response := to_jsonb(m);
    begin
      insert into public.product_events(user_id,event_name,properties)
        values(u,'moment_created',jsonb_build_object('mode',m.mode,'input_type','text'));
    exception when others then
      raise warning 'telemetry_failed kind=moment_created';
    end;
  elsif p_operation='index' then
    select * into m from public.moments where id=(p_payload->>'moment_id')::uuid and user_id=u and memory_enabled;
    if not found then raise exception 'SOURCE_UNAVAILABLE'; end if;
    insert into public.background_jobs(user_id,kind,payload,request_hash)
      values(u,'index',jsonb_build_object('moment_id',m.id,'revision',m.revision),m.id::text||':'||m.revision::text)
      on conflict do nothing returning * into j;
    if j.id is null then
      select * into j from public.background_jobs where user_id=u and kind='index'
        and request_hash=m.id::text||':'||m.revision::text and status in ('queued','running') limit 1;
    end if;
    response:=jsonb_build_object('id',j.id,'status',j.status);
  else
    if coalesce(length(trim(p_payload->>'question')),0) not between 1 and 2000 then raise exception 'INVALID_QUESTION'; end if;
    insert into public.background_jobs(user_id,kind,payload,request_hash,max_attempts)
      values(u,'analysis',jsonb_build_object('question',p_payload->>'question'),h,2)
      on conflict do nothing returning * into j;
    if j.id is null then
      select * into j from public.background_jobs where user_id=u and kind='analysis'
        and request_hash=h and status in ('queued','running');
    end if;
    response := jsonb_build_object('id',j.id,'status',j.status,'progress',j.progress);
  end if;
  insert into echotrace_private.idempotency(user_id,operation,key,request_hash,response)
    values(u,p_operation,p_key,h,response);
  return response;
end;
$$;
revoke all on function public.submit_operation(text,text,text,jsonb) from public,anon;
grant execute on function public.submit_operation(text,text,text,jsonb) to authenticated;

create function public.cancel_job(p_id uuid) returns jsonb
language plpgsql security definer set search_path = public as $$
declare j public.background_jobs;
begin
  update public.background_jobs set status='cancelled',error_code='USER_CANCELLED',updated_at=now()
  where id=p_id and user_id=auth.uid() and status in ('queued','running') returning * into j;
  return jsonb_build_object('id',p_id,'cancelled',j.id is not null);
end;
$$;
revoke all on function public.cancel_job(uuid) from public,anon;
grant execute on function public.cancel_job(uuid) to authenticated;

create function public.worker_claim(p_lease_seconds int default 45,p_timeout_seconds int default 600)
returns setof public.background_jobs language plpgsql security definer set search_path=public as $$
begin
  with expired as (update public.background_jobs set status='failed',error_code='ATTEMPTS_OR_DEADLINE_EXHAUSTED',updated_at=now()
    where status in ('queued','running') and (attempts>=max_attempts or deadline_at<=now())
    and (status='queued' or lease_until<=now()) returning *)
  update public.weekly_reports r set status='failed',error_code='ATTEMPTS_OR_DEADLINE_EXHAUSTED'
    from expired e where e.kind='weekly' and r.id=(e.payload->>'report_id')::uuid and r.user_id=e.user_id;
  return query update public.background_jobs j set status='running',attempts=j.attempts+1,
    lease_token=gen_random_uuid(),lease_until=now()+make_interval(secs=>least(300,greatest(15,p_lease_seconds))),
    deadline_at=coalesce(j.deadline_at,now()+make_interval(secs=>least(1800,greatest(30,p_timeout_seconds)))),
    updated_at=now()
    where j.id=(select q.id from public.background_jobs q where
      (q.status='queued' and q.available_at<=now() or q.status='running' and q.lease_until<=now())
      and q.attempts<q.max_attempts and (q.deadline_at is null or q.deadline_at>now())
      order by q.available_at,q.created_at for update skip locked limit 1) returning j.*;
end;
$$;

create function public.worker_control(p_action text,p_id uuid,p_lease uuid,p_data jsonb default '{}')
returns setof public.background_jobs language plpgsql security definer set search_path=public as $$
declare j public.background_jobs;
begin
  select * into j from public.background_jobs where id=p_id and lease_token=p_lease
    and status='running' and lease_until>clock_timestamp() and deadline_at>clock_timestamp() for update;
  if not found then return; end if;
  if p_action='heartbeat' then
    update public.background_jobs set lease_until=least(deadline_at,now()+interval '45 seconds'),updated_at=now() where id=p_id;
  elsif p_action='checkpoint' then
    update public.background_jobs set checkpoint=p_data || jsonb_build_object(
      'calls',greatest(coalesce((checkpoint->>'calls')::int,0),coalesce((p_data->>'calls')::int,0)),
      'tokens',greatest(coalesce((checkpoint->>'tokens')::int,0),coalesce((p_data->>'tokens')::int,0))),
      progress=coalesce(p_data->>'progress','正在整理'),updated_at=now() where id=p_id;
  elsif p_action='complete' then
    update public.background_jobs set status='succeeded',progress='整理完成',updated_at=now() where id=p_id;
  elsif p_action='fail' then
    update public.background_jobs set
      status=case when coalesce((p_data->>'retryable')::boolean,false) and attempts<max_attempts then 'queued' else 'failed' end,
      available_at=now()+make_interval(secs=>least(120,5*power(2,attempts)::int)),
      error_code=left(p_data->>'code',80),updated_at=now() where id=p_id;
    if j.kind='weekly' and exists(select 1 from public.background_jobs where id=p_id and status='failed') then
      update public.weekly_reports set status='failed',error_code=left(p_data->>'code',80)
        where id=(j.payload->>'report_id')::uuid and user_id=j.user_id;
    end if;
  elsif p_action <> 'check' then raise exception 'INVALID_WORKER_ACTION';
  end if;
  return query select * from public.background_jobs where id=p_id;
end;
$$;
revoke all on function public.worker_claim(int,int),public.worker_control(text,uuid,uuid,jsonb) from public,anon,authenticated;
grant execute on function public.worker_claim(int,int),public.worker_control(text,uuid,uuid,jsonb) to service_role;

create function public.evidence_snapshot(p_ids uuid[]) returns setof public.moments
language sql stable security invoker set search_path=public as $$
  select m.* from public.moments m where m.user_id=auth.uid() and m.id=any(p_ids) and m.memory_enabled
  and not exists(select 1 from public.memory_sources s join public.memories x on x.id=s.memory_id
    where s.moment_id=m.id and x.status='deleted');
$$;
revoke all on function public.evidence_snapshot(uuid[]) from public,anon;
grant execute on function public.evidence_snapshot(uuid[]) to authenticated,service_role;

-- Independent ranked lists; never directly compare vector and trigram scores.
create function public.search_evidence_v2(p_query text,p_embedding extensions.vector,
  p_from timestamptz default null,p_to timestamptz default null,p_axis text default 'record',p_limit int default 24)
returns table(moment_id uuid,channel text,rank bigint)
language sql stable security invoker set search_path=public,extensions as $$
  with eligible as (
    select m.* from public.moments m where m.user_id=auth.uid() and m.memory_enabled
      and (p_from is null or case when p_axis='event' then m.event_at else m.created_at end>=p_from)
      and (p_to is null or case when p_axis='event' then m.event_at else m.created_at end<p_to)
      and not exists(select 1 from public.memory_sources s join public.memories x on x.id=s.memory_id
        where s.moment_id=m.id and x.status='deleted')
  ), vectors as (
    select m.id,min(c.embedding <=> p_embedding) distance from eligible m
      join public.moment_index_chunks c on c.moment_id=m.id and c.user_id=auth.uid()
      and c.source_revision=m.revision
      and vector_dims(c.embedding)=vector_dims(p_embedding) group by m.id
  ), text_hits as (
    select m.id, greatest(similarity(m.content,p_query),
      case when position(p_query in m.content)>0 then 0.5 else 0 end) score from eligible m
  )
  (select id,'vector'::text,row_number() over(order by distance,id) from vectors order by distance,id limit least(60,p_limit))
  union all
  (select id,'text'::text,row_number() over(order by score desc,id) from text_hits where score>0
     order by score desc,id limit least(60,p_limit));
$$;
revoke all on function public.search_evidence_v2(text,extensions.vector,timestamptz,timestamptz,text,int) from public,anon;
grant execute on function public.search_evidence_v2(text,extensions.vector,timestamptz,timestamptz,text,int) to authenticated,service_role;

create function public.commit_insight(p_insight jsonb,p_evidence jsonb,p_versions jsonb,
  p_job_id uuid default null,p_lease uuid default null) returns jsonb
language plpgsql security definer set search_path=public as $$
declare u uuid:=auth.uid(); i public.insights; e jsonb; m public.moments; pair record; j public.background_jobs;
begin
  if u is null then raise exception 'LOGIN_REQUIRED' using errcode='42501'; end if;
  perform echotrace_private.owner_lock(u);
  if p_job_id is not null then
    select * into j from public.background_jobs where id=p_job_id and user_id=u for update;
    if not found or j.status<>'running' or j.lease_token<>p_lease or j.lease_until<=clock_timestamp() or j.deadline_at<=clock_timestamp() then
      raise exception 'LEASE_LOST';
    end if;
  end if;
  if jsonb_array_length(p_evidence)=0 or p_versions='{}'::jsonb then raise exception 'NO_EVIDENCE'; end if;
  for pair in select * from jsonb_each_text(p_versions) loop
    select * into m from public.evidence_snapshot(array[pair.key::uuid]);
    if not found or m.revision<>pair.value::bigint then raise exception 'EVIDENCE_CHANGED'; end if;
  end loop;
  for e in select * from jsonb_array_elements(p_evidence) loop
    if not p_versions ? (e->>'moment_id') then raise exception 'UNBOUND_EVIDENCE'; end if;
    if e->>'memory_id' is not null and not exists(select 1 from public.memories where id=(e->>'memory_id')::uuid and user_id=u and status='active') then
      raise exception 'INVALID_MEMORY_OWNER';
    end if;
  end loop;
  insert into public.insights(user_id,insight_type,trigger_type,query,title,body,limitation,verification_status,
    time_start,time_end,cache_key,evidence_version,agent_version,evidence_revisions)
  values(u,p_insight->>'insight_type',p_insight->>'trigger_type',p_insight->>'query',p_insight->>'title',
    p_insight->>'body',p_insight->>'limitation',p_insight->>'verification_status',
    (p_insight->>'time_start')::timestamptz,(p_insight->>'time_end')::timestamptz,p_insight->>'cache_key',
    p_insight->>'evidence_version',p_insight->>'agent_version',p_versions) returning * into i;
  insert into public.insight_evidence(insight_id,moment_id,memory_id,user_id,stance)
    select distinct i.id,(x->>'moment_id')::uuid,(x->>'memory_id')::uuid,u,x->>'stance'
    from jsonb_array_elements(p_evidence) x;
  if p_job_id is not null then
    update public.background_jobs set result_id=i.id,
      status=case when kind='analysis' then 'succeeded' else status end,
      progress='整理完成',updated_at=now() where id=p_job_id;
  end if;
  return to_jsonb(i) || jsonb_build_object('evidence',p_evidence);
end;
$$;
revoke all on function public.commit_insight(jsonb,jsonb,jsonb,uuid,uuid) from public,anon;
grant execute on function public.commit_insight(jsonb,jsonb,jsonb,uuid,uuid) to authenticated;

-- Worker carries a service credential, NOT a persisted user access token.
-- Restrict function names; owner is looked up from a live lease, not payload.
create function public.worker_evidence_rpc(p_id uuid,p_lease uuid,p_function text,p_payload jsonb)
returns jsonb language plpgsql security definer set search_path=public,extensions as $$
declare j public.background_jobs; result jsonb; v jsonb;
begin
  select * into j from public.background_jobs where id=p_id and lease_token=p_lease and status='running'
    and lease_until>clock_timestamp() and deadline_at>clock_timestamp();
  if not found then raise exception 'LEASE_LOST'; end if;
  perform set_config('request.jwt.claims',jsonb_build_object('sub',j.user_id,'role','authenticated')::text,true);
  if p_function='evidence_snapshot' then
    select coalesce(jsonb_agg(to_jsonb(m)),'[]') into result from public.evidence_snapshot(
      array(select jsonb_array_elements_text(p_payload->'p_ids')::uuid)) m;
  elsif p_function='search_evidence_v2' then
    select coalesce(jsonb_agg(to_jsonb(m)),'[]') into result from public.search_evidence_v2(
      p_payload->>'p_query',(p_payload->'p_embedding')::text::extensions.vector,
      (p_payload->>'p_from')::timestamptz,(p_payload->>'p_to')::timestamptz,
      coalesce(p_payload->>'p_axis','record'),coalesce((p_payload->>'p_limit')::int,24)) m;
  elsif p_function='commit_insight' then
    result:=public.commit_insight(p_payload->'p_insight',p_payload->'p_evidence',p_payload->'p_versions',j.id,p_lease);
  elsif p_function='put_index' then
    perform echotrace_private.owner_lock(j.user_id);
    select * into j from public.background_jobs where id=p_id and lease_token=p_lease and status='running'
      and lease_until>clock_timestamp() and deadline_at>clock_timestamp() for update;
    if not found then raise exception 'LEASE_LOST'; end if;
    if not exists(select 1 from public.moments where id=(j.payload->>'moment_id')::uuid
      and user_id=j.user_id and memory_enabled and revision=(j.payload->>'revision')::bigint) then
      raise exception 'EVIDENCE_CHANGED';
    end if;
    -- Fence again after waiting for owner lock.
    if not exists(select 1 from public.background_jobs where id=p_id and lease_token=p_lease and status='running'
      and lease_until>clock_timestamp() and deadline_at>clock_timestamp()) then raise exception 'LEASE_LOST'; end if;
    delete from public.moment_index_chunks where moment_id=(j.payload->>'moment_id')::uuid and user_id=j.user_id;
    for v in select * from jsonb_array_elements(p_payload->'chunks') loop
      insert into public.moment_index_chunks(moment_id,user_id,chunk_index,content,embedding,embedding_model,embedding_dimension,source_revision)
        values((j.payload->>'moment_id')::uuid,j.user_id,(v->>'chunk_index')::int,v->>'content',
          (v->'embedding')::text::extensions.vector,v->>'embedding_model',jsonb_array_length(v->'embedding'),(j.payload->>'revision')::bigint);
    end loop;
    update public.background_jobs set status='succeeded',progress='整理完成',updated_at=now() where id=p_id;
    result:='[]';
  elsif p_function='finish_weekly' then
    perform echotrace_private.owner_lock(j.user_id);
    select * into j from public.background_jobs where id=p_id and lease_token=p_lease and status='running'
      and lease_until>clock_timestamp() and deadline_at>clock_timestamp() for update;
    if not found then raise exception 'LEASE_LOST'; end if;
    if not exists(select 1 from public.background_jobs where id=p_id and lease_token=p_lease and status='running'
      and lease_until>clock_timestamp() and deadline_at>clock_timestamp()) then raise exception 'LEASE_LOST'; end if;
    for v in select jsonb_build_object('id',key,'revision',value) from jsonb_each(p_payload->'versions') loop
      if not exists(select 1 from public.evidence_snapshot(array[(v->>'id')::uuid]) x
        where x.revision=(v->>'revision')::bigint) then raise exception 'EVIDENCE_CHANGED'; end if;
    end loop;
    delete from public.weekly_summary_cards where report_id=(j.payload->>'report_id')::uuid and user_id=j.user_id;
    for v in select * from jsonb_array_elements(p_payload->'index_cards') loop
      if exists(select 1 from jsonb_array_elements_text(v->'source_moment_ids') as source(value)
        where not ((p_payload->'versions') ? source.value)) then
        raise exception 'UNBOUND_WEEKLY_SOURCE';
      end if;
      insert into public.weekly_summary_cards(report_id,user_id,topic,summary,source_moment_ids,time_start,time_end,
        embedding,embedding_model,embedding_dimension)
        values((j.payload->>'report_id')::uuid,j.user_id,v->>'topic',v->>'summary',
        array(select jsonb_array_elements_text(v->'source_moment_ids')::uuid),
        (v->>'time_start')::timestamptz,(v->>'time_end')::timestamptz,(v->'embedding')::text::extensions.vector,
        v->>'embedding_model',(v->>'embedding_dimension')::int);
    end loop;
    update public.weekly_reports set status=p_payload->>'status',digest=p_payload->'digest',
      input_version=p_payload->>'input_version',insight_id=(p_payload->>'insight_id')::uuid,error_code=null
      where id=(j.payload->>'report_id')::uuid and user_id=j.user_id;
    update public.background_jobs set status='succeeded',progress='整理完成',updated_at=now() where id=p_id;
    result:='[]';
  elsif p_function in ('search_personal_moment','search_personal_memory','search_weekly_summary_card') then
    -- Legacy path for Companion/weekly compatibility. All functions constrain auth.uid().
    if p_function='search_personal_moment' then
      select coalesce(jsonb_agg(to_jsonb(x)),'[]') into result from public.search_personal_moment(
        p_payload->>'p_query_text',(p_payload->'p_query_embedding')::text::extensions.vector,
        (p_payload->>'p_match_count')::int,(p_payload->>'p_from')::timestamptz,(p_payload->>'p_to')::timestamptz) x;
    elsif p_function='search_personal_memory' then
      select coalesce(jsonb_agg(to_jsonb(x)),'[]') into result from public.search_personal_memory(
        p_payload->>'p_query_text',(p_payload->'p_query_embedding')::text::extensions.vector,
        (p_payload->>'p_match_count')::int,(p_payload->>'p_from')::timestamptz,(p_payload->>'p_to')::timestamptz) x;
    else
      select coalesce(jsonb_agg(to_jsonb(x)),'[]') into result from public.search_weekly_summary_card(
        p_payload->>'p_query_text',(p_payload->'p_query_embedding')::text::extensions.vector,(p_payload->>'p_match_count')::int) x;
    end if;
  else raise exception 'FORBIDDEN_WORKER_TOOL' using errcode='42501'; end if;
  return result;
end;
$$;
revoke all on function public.worker_evidence_rpc(uuid,uuid,text,jsonb) from public,anon,authenticated;
grant execute on function public.worker_evidence_rpc(uuid,uuid,text,jsonb) to service_role;
grant execute on function public.commit_insight(jsonb,jsonb,jsonb,uuid,uuid) to service_role;

-- Cleanup is an explicit server-only maintenance operation (never model-controlled).
create function public.cleanup_job_history() returns void language sql security definer set search_path=public as $$
  delete from echotrace_private.idempotency where expires_at<now();
  delete from public.background_jobs where status in ('succeeded','failed','cancelled') and updated_at<now()-interval '30 days';
$$;
revoke all on function public.cleanup_job_history() from public,anon,authenticated;
grant execute on function public.cleanup_job_history() to service_role;
grant execute on function echotrace_private.owner_lock(uuid) to service_role;

create function public.schema_ready() returns text language sql stable as $$ select 'durable-v2'::text $$;
revoke all on function public.schema_ready() from public;
grant execute on function public.schema_ready() to anon,authenticated,service_role;

-- A legacy Memory deletion also changes whether its source is eligible.
create function echotrace_private.memory_changed() returns trigger
language plpgsql security definer set search_path=public as $$
declare u uuid:=coalesce(new.user_id,old.user_id);
begin
  perform echotrace_private.owner_lock(u);
  update public.insights set status='stale' where user_id=u and status='active';
  update public.weekly_reports set status='stale' where user_id=u;
  update public.background_jobs set status='cancelled',error_code='EVIDENCE_CHANGED'
    where user_id=u and status in ('queued','running') and kind in ('analysis','weekly');
  if tg_op='DELETE' then return old; end if;
  return new;
end;
$$;
create trigger memory_change before update or delete on public.memories
  for each row execute function echotrace_private.memory_changed();

create function public.cancel_owned_jobs() returns jsonb
language plpgsql security definer set search_path=public as $$
begin
  if auth.uid() is null then raise exception 'LOGIN_REQUIRED' using errcode='42501'; end if;
  update public.background_jobs set status='cancelled',error_code='ACCOUNT_DATA_CLEARED',
    payload='{}',checkpoint='{}',updated_at=now() where user_id=auth.uid();
  update echotrace_private.idempotency set response='{}',expires_at=now() where user_id=auth.uid();
  return '{"ok":true}'::jsonb;
end;
$$;
revoke all on function public.cancel_owned_jobs() from public,anon;
grant execute on function public.cancel_owned_jobs() to authenticated;

create function public.enqueue_indexes(p_model text,p_limit int default 20,p_offset int default 0)
returns jsonb language plpgsql security definer set search_path=public as $$
declare m public.moments; total int:=0; scheduled int:=0; added int;
begin
  if auth.uid() is null then raise exception 'LOGIN_REQUIRED' using errcode='42501'; end if;
  for m in select * from public.moments where user_id=auth.uid() and memory_enabled
    and not exists(select 1 from public.memory_sources s join public.memories x on x.id=s.memory_id
      where s.moment_id=moments.id and x.status='deleted')
    order by created_at,id limit least(100,greatest(1,p_limit)) offset greatest(0,p_offset) loop
    total:=total+1;
    if (select count(*) from public.moment_index_chunks c where c.moment_id=m.id
      and c.source_revision=m.revision and c.embedding_model=p_model)
      <> (case when length(m.content)<=2400 then 1 else ceil(length(m.content)/2280.0)::int end) then
      insert into public.background_jobs(user_id,kind,payload,request_hash)
        values(auth.uid(),'index',jsonb_build_object('moment_id',m.id,'revision',m.revision),m.id::text||':'||m.revision::text)
        on conflict do nothing;
      get diagnostics added = row_count;
      scheduled:=scheduled+added;
    end if;
  end loop;
  return jsonb_build_object('moments_checked',total,'tasks_scheduled',scheduled,'chunks_available',0,'next_offset',p_offset+total);
end;
$$;
revoke all on function public.enqueue_indexes(text,int,int) from public,anon;
grant execute on function public.enqueue_indexes(text,int,int) to authenticated;

-- Fixed read statements only. No SQL, column names or owner accepted from callers.
create function public.worker_read(p_id uuid,p_lease uuid,p_resource text,p_ids uuid[] default null,
  p_moment_ids uuid[] default null,p_after timestamptz default null,p_before timestamptz default null,
  p_enabled boolean default null,p_status text default null,p_cache text default null,
  p_offset int default 0,p_limit int default 100,p_desc boolean default false)
returns jsonb language plpgsql security definer set search_path=public as $$
declare j public.background_jobs; result jsonb;
begin
  select * into j from public.background_jobs where id=p_id and lease_token=p_lease and status='running'
    and lease_until>clock_timestamp() and deadline_at>clock_timestamp();
  if not found then raise exception 'LEASE_LOST'; end if;
  if p_resource='moments' then
    select coalesce(jsonb_agg(to_jsonb(x)),'[]') into result from (
      select m.* from public.moments m where m.user_id=j.user_id
      and not exists(select 1 from public.memory_sources s join public.memories x on x.id=s.memory_id
        where s.moment_id=m.id and x.status='deleted')
      and (p_ids is null or m.id=any(p_ids)) and (p_enabled is null or m.memory_enabled=p_enabled)
      and (p_after is null or m.created_at>=p_after) and (p_before is null or m.created_at<p_before)
      order by case when p_desc then m.created_at end desc,m.created_at,m.id
      limit least(100,greatest(1,p_limit)) offset greatest(0,p_offset)) x;
  elsif p_resource='memories' then
    select coalesce(jsonb_agg(to_jsonb(x)-'embedding'),'[]') into result from (
      select m.* from public.memories m where m.user_id=j.user_id
        and (p_ids is null or m.id=any(p_ids)) and (p_status is null or m.status=p_status)
      order by m.id limit least(100,greatest(1,p_limit))) x;
  elsif p_resource='memory_sources' then
    select coalesce(jsonb_agg(to_jsonb(x)),'[]') into result from (
      select s.* from public.memory_sources s where s.user_id=j.user_id
        and (p_moment_ids is null or s.moment_id=any(p_moment_ids))
      order by s.moment_id,s.memory_id limit least(100,greatest(1,p_limit))) x;
  elsif p_resource='weekly_reports' then
    select coalesce(jsonb_agg(to_jsonb(x)),'[]') into result from (
      select r.* from public.weekly_reports r where r.user_id=j.user_id
        and (p_ids is null or r.id=any(p_ids)) and (p_status is null or r.status=p_status)
      order by r.week_start limit least(100,greatest(1,p_limit))) x;
  elsif p_resource='insights' then
    select coalesce(jsonb_agg(to_jsonb(x) || jsonb_build_object('insight_evidence',
      (select coalesce(jsonb_agg(to_jsonb(e)),'[]') from public.insight_evidence e
       where e.user_id=j.user_id and e.insight_id=x.id))),'[]') into result from (
      select i.* from public.insights i where i.user_id=j.user_id
        and (p_ids is null or i.id=any(p_ids)) and (p_status is null or i.status=p_status)
        and (p_cache is null or i.cache_key=p_cache)
      order by i.created_at desc limit least(100,greatest(1,p_limit))) x;
  else raise exception 'FORBIDDEN_WORKER_RESOURCE' using errcode='42501'; end if;
  return result;
end;
$$;
revoke all on function public.worker_read(uuid,uuid,text,uuid[],uuid[],timestamptz,timestamptz,boolean,text,text,int,int,boolean) from public,anon,authenticated;
grant execute on function public.worker_read(uuid,uuid,text,uuid[],uuid[],timestamptz,timestamptz,boolean,text,text,int,int,boolean) to service_role;

create function public.worker_log(p_id uuid,p_lease uuid,p_kind text,p_data jsonb) returns jsonb
language plpgsql security definer set search_path=public as $$
declare j public.background_jobs;
begin
  select * into j from public.background_jobs where id=p_id and lease_token=p_lease and status='running'
    and lease_until>clock_timestamp() and deadline_at>clock_timestamp() for share;
  if not found then raise exception 'LEASE_LOST'; end if;
  if p_kind='ai_runs' then
    insert into public.ai_runs(user_id,request_id,trace_id,task_type,agent_name,model,prompt_version,
      success,latency_ms,input_tokens,output_tokens,tool_calls,retrieval_candidates,evidence_count,failure_code,metadata)
      values(j.user_id,coalesce(p_data->>'request_id',gen_random_uuid()::text),j.id::text,p_data->>'task_type',
      p_data->>'agent_name',p_data->>'model',p_data->>'prompt_version',coalesce((p_data->>'success')::boolean,true),
      (p_data->>'latency_ms')::int,(p_data->>'input_tokens')::int,(p_data->>'output_tokens')::int,
      coalesce((p_data->>'tool_calls')::int,0),coalesce((p_data->>'retrieval_candidates')::int,0),
      coalesce((p_data->>'evidence_count')::int,0),p_data->>'failure_code',coalesce(p_data->'metadata','{}'));
  elsif p_kind='product_events' then
    insert into public.product_events(user_id,event_name,request_id,properties)
      values(j.user_id,p_data->>'event_name',j.id::text,coalesce(p_data->'properties','{}'));
  else raise exception 'FORBIDDEN_WORKER_LOG' using errcode='42501'; end if;
  return '[]'::jsonb;
end;
$$;
revoke all on function public.worker_log(uuid,uuid,text,jsonb) from public,anon,authenticated;
grant execute on function public.worker_log(uuid,uuid,text,jsonb) to service_role;

-- Weekly report state and the required job are also committed together.
create function echotrace_private.enqueue_weekly() returns trigger
language plpgsql security definer set search_path=public as $$
begin
  if new.status='processing' then
    insert into public.background_jobs(user_id,kind,payload,request_hash,max_attempts)
      values(new.user_id,'weekly',jsonb_build_object('report_id',new.id),new.id::text,2) on conflict do nothing;
  end if;
  return new;
end;
$$;
create trigger enqueue_weekly_job after insert or update of status on public.weekly_reports
  for each row execute function echotrace_private.enqueue_weekly();

-- Old results are not evidence-versioned; do not continue presenting them as current.
update public.insights set status='stale' where status='active';
notify pgrst, 'reload schema';
commit;
