-- A successful retry must not expose the previous attempt as a current error.
begin;
create function echotrace_private.clear_successful_job_error() returns trigger
language plpgsql set search_path='' as $$
begin
  if new.status='succeeded' then
    new.error_code := null;
  end if;
  return new;
end;
$$;
revoke all on function echotrace_private.clear_successful_job_error() from public,anon,authenticated;
create trigger clear_successful_job_error before update of status on public.background_jobs
  for each row execute function echotrace_private.clear_successful_job_error();
update public.background_jobs set error_code=null where status='succeeded' and error_code is not null;
commit;
