-- Additive product fixes; apply once. No model call holds these transactions open.
begin;

create index moments_history_cursor_idx on public.moments(user_id,created_at desc,id desc);

create table echotrace_private.chat_turns (
  user_id uuid not null references auth.users(id) on delete cascade,
  key text not null,
  request_hash text not null,
  thread_id uuid not null,
  moment_id uuid not null,
  source_revision bigint not null,
  status text not null check(status in ('running','retryable','done','cancelled')),
  lease uuid,
  lease_until timestamptz,
  response jsonb not null default '{}',
  expires_at timestamptz not null default now()+interval '7 days',
  primary key(user_id,key)
);
create index chat_turns_thread on echotrace_private.chat_turns(user_id,thread_id,status);
revoke all on echotrace_private.chat_turns from public,anon,authenticated;

create function public.chat_prepare(p_key text,p_payload jsonb) returns jsonb
language plpgsql security definer set search_path=public as $$
declare u uuid:=auth.uid(); h text; turn echotrace_private.chat_turns;
  t uuid; mid uuid; src uuid; m public.moments; fresh boolean:=false; l uuid:=gen_random_uuid();
begin
  if u is null then raise exception 'LOGIN_REQUIRED' using errcode='42501'; end if;
  if p_key is null or length(p_key) not between 1 and 128 then raise exception 'INVALID_KEY'; end if;
  if coalesce(length(trim(p_payload->>'content')),0) not between 1 and 20000 then
    raise exception 'INVALID_CONTENT';
  end if;
  h:=encode(sha256(convert_to(p_payload::text,'UTF8')),'hex');
  perform echotrace_private.owner_lock(u);
  delete from echotrace_private.chat_turns where user_id=u and key=p_key and expires_at<=now();
  select * into turn from echotrace_private.chat_turns where user_id=u and key=p_key for update;
  if found then
    if turn.request_hash<>h then raise exception 'IDEMPOTENCY_CONFLICT'; end if;
    if turn.status='cancelled' then raise exception 'SOURCE_UNAVAILABLE'; end if;
    if not exists(select 1 from public.moments where id=turn.moment_id and user_id=u and revision=turn.source_revision)
       or not exists(select 1 from public.threads where id=turn.thread_id and user_id=u) then
      raise exception 'SOURCE_UNAVAILABLE';
    end if;
    if turn.status='done' then return jsonb_build_object('cached',turn.response); end if;
    if turn.status='running' and turn.lease_until>clock_timestamp() then raise exception 'CHAT_BUSY'; end if;
    t:=turn.thread_id;mid:=turn.moment_id;
  else
    t:=(p_payload->>'thread_id')::uuid;src:=(p_payload->>'source_moment_id')::uuid;
    if t is not null then
      select source_moment_id into src from public.threads where id=t and user_id=u;
      if not found then raise exception 'SOURCE_UNAVAILABLE'; end if;
    elsif src is not null then
      select * into m from public.moments where id=src and user_id=u;
      if not found then raise exception 'SOURCE_UNAVAILABLE'; end if;
      select id into t from public.threads where source_moment_id=src and user_id=u;
    end if;
    if t is not null and exists(select 1 from echotrace_private.chat_turns where user_id=u and thread_id=t
      and status='running' and lease_until>clock_timestamp()) then raise exception 'CHAT_BUSY'; end if;
    if t is null then
      if src is null then
        insert into public.moments(user_id,content,mode,input_type,memory_enabled)
          values(u,p_payload->>'content','chat','text',true) returning id into src;
      end if;
      insert into public.threads(user_id,source_moment_id) values(u,src) returning id into t;
      update public.moments set thread_id=t where id=src and user_id=u;
      mid:=src;fresh:=true;
    else
      insert into public.moments(user_id,content,mode,input_type,memory_enabled,thread_id)
        values(u,p_payload->>'content','chat','text',true,t) returning id into mid;
    end if;
    insert into public.thread_messages(user_id,thread_id,role,content,input_type)
      values(u,t,'user',p_payload->>'content','text');
    select revision into m.revision from public.moments where id=mid and user_id=u;
    insert into echotrace_private.chat_turns(user_id,key,request_hash,thread_id,moment_id,source_revision,status)
      values(u,p_key,h,t,mid,m.revision,'retryable');
  end if;
  update echotrace_private.chat_turns set status='running',lease=l,lease_until=clock_timestamp()+interval '5 minutes'
    where user_id=u and key=p_key;
  return jsonb_build_object('thread_id',t,'moment_id',mid,'lease',l,'new_thread',fresh);
end;
$$;

create function public.chat_control(p_key text,p_lease uuid,p_action text,p_data jsonb default '{}') returns jsonb
language plpgsql security definer set search_path=public as $$
declare u uuid:=auth.uid(); turn echotrace_private.chat_turns; msg public.thread_messages;
  ids uuid[]; v jsonb; result jsonb;
begin
  if u is null then raise exception 'LOGIN_REQUIRED' using errcode='42501'; end if;
  perform echotrace_private.owner_lock(u);
  select * into turn from echotrace_private.chat_turns where user_id=u and key=p_key for update;
  if not found or turn.status<>'running' or turn.lease is distinct from p_lease or turn.lease_until<=clock_timestamp() then
    raise exception 'CHAT_LEASE_LOST';
  end if;
  if p_action='fail' then
    update echotrace_private.chat_turns set status='retryable',lease=null,lease_until=null where user_id=u and key=p_key;
    return '{"ok":true}'::jsonb;
  end if;
  if p_action is distinct from 'finish' then raise exception 'INVALID_OPERATION'; end if;
  if not exists(select 1 from public.moments where id=turn.moment_id and user_id=u and revision=turn.source_revision)
    or not exists(select 1 from public.threads where id=turn.thread_id and user_id=u) then
    raise exception 'SOURCE_UNAVAILABLE';
  end if;
  ids:=array(select jsonb_array_elements_text(coalesce(p_data->'evidence_ids','[]'))::uuid);
  v:=coalesce(p_data->'versions','{}');
  if exists(select 1 from unnest(ids) x where not exists(select 1 from public.moments m where m.id=x
    and m.user_id=u and m.memory_enabled and m.revision=(v->>x::text)::bigint)) then
    raise exception 'EVIDENCE_CHANGED';
  end if;
  insert into public.thread_messages(user_id,thread_id,role,content,input_type,evidence_moment_ids)
    values(u,turn.thread_id,'assistant',p_data->>'content','system',ids) returning * into msg;
  result:=jsonb_build_object('thread_id',turn.thread_id,'moment_id',turn.moment_id,'message',to_jsonb(msg),
    'evidence_moment_ids',to_jsonb(ids),'used_long_term_memory',cardinality(ids)>0);
  update echotrace_private.chat_turns set status='done',response=result,lease=null,lease_until=null
    where user_id=u and key=p_key;
  return result;
end;
$$;

-- Source edits cancel an in-flight chat instead of publishing an answer from old text.
create function echotrace_private.invalidate_chat_source() returns trigger
language plpgsql security definer set search_path=public as $$
begin
  if tg_op='UPDATE' and new.revision=old.revision then return new; end if;
  update echotrace_private.chat_turns set status='cancelled',response='{}',lease=null,lease_until=null
    where user_id=old.user_id and (moment_id=old.id or response->'evidence_moment_ids' ? old.id::text);
  if tg_op='DELETE' then return old; end if;
  return new;
end;
$$;
create trigger b_chat_source before update or delete on public.moments
  for each row execute function echotrace_private.invalidate_chat_source();

create function public.clear_personal_data() returns jsonb
language plpgsql security definer set search_path=public as $$
declare u uuid:=auth.uid(); tab text; n bigint; counts jsonb:='{}';
begin
  if u is null then raise exception 'LOGIN_REQUIRED' using errcode='42501'; end if;
  perform echotrace_private.owner_lock(u);
  perform public.cancel_owned_jobs();
  update echotrace_private.chat_turns set status='cancelled',response='{}',lease=null,lease_until=null where user_id=u;
  foreach tab in array array['ai_runs','product_events','weekly_summary_cards','weekly_reports','insight_evidence',
    'insights','insight_candidates','memory_sources','thread_messages','threads','memories','moment_index_chunks','moments']
  loop
    execute format('delete from public.%I where user_id=$1',tab) using u;
    get diagnostics n=row_count;
    counts:=counts||jsonb_build_object(tab,n);
  end loop;
  return jsonb_build_object('ok',true,'deleted',counts);
end;
$$;

create function public.delete_moment_data(p_id uuid) returns jsonb
language plpgsql security definer set search_path=public as $$
declare u uuid:=auth.uid(); t uuid; mids uuid[]; n bigint;
begin
  if u is null then raise exception 'LOGIN_REQUIRED' using errcode='42501'; end if;
  perform echotrace_private.owner_lock(u);
  select thread_id into t from public.moments where id=p_id and user_id=u;
  if not found then return '{"deleted":false}'::jsonb; end if;
  if t is null then select id into t from public.threads where source_moment_id=p_id and user_id=u; end if;
  select array_agg(id) into mids from public.moments where user_id=u and (id=p_id or (t is not null and thread_id=t));
  update echotrace_private.chat_turns set status='cancelled',response='{}',lease=null,lease_until=null
    where user_id=u and (moment_id=any(mids) or (t is not null and thread_id=t));
  update public.memories set status='deleted' where user_id=u and id in
    (select memory_id from public.memory_sources where user_id=u and moment_id=any(mids));
  delete from public.threads where user_id=u and id=t;
  delete from public.moments where user_id=u and id=any(mids);
  get diagnostics n=row_count;
  return jsonb_build_object('deleted',true,'moment_count',n);
end;
$$;

create function public.set_moment_memory(p_id uuid,p_enabled boolean) returns jsonb
language plpgsql security definer set search_path=public as $$
declare u uuid:=auth.uid(); m public.moments;
begin
  if u is null then raise exception 'LOGIN_REQUIRED' using errcode='42501'; end if;
  perform echotrace_private.owner_lock(u);
  update public.moments set memory_enabled=p_enabled where id=p_id and user_id=u returning * into m;
  if not found then return '{}'::jsonb; end if;
  if not p_enabled then
    update public.memories set status='disputed' where user_id=u and status='active' and id in
      (select memory_id from public.memory_sources where user_id=u and moment_id=p_id);
  end if;
  return to_jsonb(m);
end;
$$;

revoke all on function public.chat_prepare(text,jsonb), public.chat_control(text,uuid,text,jsonb),
  public.clear_personal_data(), public.delete_moment_data(uuid), public.set_moment_memory(uuid,boolean) from public,anon;
grant execute on function public.chat_prepare(text,jsonb), public.chat_control(text,uuid,text,jsonb),
  public.clear_personal_data(), public.delete_moment_data(uuid), public.set_moment_memory(uuid,boolean) to authenticated;
revoke all on function echotrace_private.invalidate_chat_source() from public,anon,authenticated;

commit;
