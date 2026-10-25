-- Public readiness marker only; no data deletion or automatic maintenance.
begin;
create function public.product_schema_ready() returns text language sql stable
as $$ select 'product-fixes-v1'::text $$;
revoke all on function public.product_schema_ready() from public;
grant execute on function public.product_schema_ready() to anon,authenticated,service_role;
commit;
