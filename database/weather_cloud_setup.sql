-- DEPLOYMENT SETUP, not a local migration. Run in the SAME Supabase project
-- after cyclone_cloud.sql. Preserves all existing cyclone tables and readers.
begin;
create schema if not exists weather_private;
revoke all on schema weather_private from public, anon, authenticated;
create table if not exists public.weather_cloud_records (
  id bigint generated always as identity primary key,
  fingerprint text not null unique check (fingerprint ~ '^[a-f0-9]{64}$'),
  payload jsonb not null check (octet_length(payload::text) <= 300000),
  captured_at timestamptz not null default now()
);
create table if not exists public.weather_cloud_health (
  source text primary key check (source in ('daily','regional','rainfall','cyclone_pdf')),
  last_checked timestamptz not null, last_success timestamptz, status text not null
);
alter table public.weather_cloud_records enable row level security;
alter table public.weather_cloud_health enable row level security;
revoke all on public.weather_cloud_records,public.weather_cloud_health from public,anon,authenticated;
revoke all on sequence public.weather_cloud_records_id_seq from public,anon,authenticated;
create index if not exists weather_cloud_pdf_idx on public.weather_cloud_records ((payload->>'pdf_sha256'));

-- Custom bearer-token authorization (same existing cyclone reader-token hashes).
-- Definer is required to access otherwise inaccessible reader hashes/archive.
-- Private helper has no API exposure and returns no keys or table contents.
create or replace function weather_private.check_reader(p_token text)
returns void language plpgsql security definer set search_path='' as $$
begin
 if p_token is null or length(p_token)<40 or length(p_token)>512 or not exists (
   select 1 from public.cyclone_cloud_readers where token_hash=
   pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p_token,'UTF8')),'hex')) then
   raise exception 'Invalid reader token' using errcode='42501';
 end if;
end $$;
revoke all on function weather_private.check_reader(text) from public,anon,authenticated;

create or replace function public.store_weather_record(p_fingerprint text,p_payload jsonb)
returns void language plpgsql security definer set search_path='' as $$
begin
 perform pg_catalog.pg_advisory_xact_lock(732651099);
 if p_payload->>'version' is distinct from '1' or p_payload->>'kind' not in ('daily','rainfall','cyclone_pdf')
    or p_payload->>'kind' is null or jsonb_typeof(p_payload->'metadata') is distinct from 'object'
    or coalesce(length(p_payload->>'text'),0) not between 1 and 150000
    or (p_payload->>'pdf_sha256' is not null and p_payload->>'pdf_sha256' !~ '^[a-f0-9]{64}$') then
   raise exception 'Invalid weather payload';
 end if;
 if not exists(select 1 from public.weather_cloud_records where fingerprint=p_fingerprint)
    and (select coalesce(sum(octet_length(payload::text)),0) from public.weather_cloud_records)+octet_length(p_payload::text)>100000000 then
   raise exception 'Weather text capacity reached';
 end if;
 insert into public.weather_cloud_records(fingerprint,payload) values(p_fingerprint,p_payload)
 on conflict(fingerprint) do nothing;
end $$;
create or replace function public.weather_source_checked(p_source text,p_ok boolean)
returns void language sql security definer set search_path='' as $$
 insert into public.weather_cloud_health(source,last_checked,last_success,status)
 values(p_source,now(),case when p_ok then now() end,case when p_ok then 'CHECKED' else 'FETCH_FAILED' end)
 on conflict(source) do update set last_checked=excluded.last_checked,
 last_success=coalesce(excluded.last_success,weather_cloud_health.last_success),status=excluded.status;
$$;
create or replace function public.read_weather_archive(p_token text,p_after bigint default 0)
returns jsonb language plpgsql security definer set search_path='' as $$
declare result jsonb;
begin
 perform weather_private.check_reader(p_token);
 select jsonb_build_object('rows',coalesce((select jsonb_agg(to_jsonb(r) order by r.id) from (
   select id,payload,captured_at from public.weather_cloud_records where id>greatest(p_after,0) order by id limit 5
 )r),'[]'::jsonb),'health',coalesce((select jsonb_agg(to_jsonb(h)) from public.weather_cloud_health h),'[]'::jsonb)) into result;
 return result;
end $$;
create or replace function public.weather_pdf_access(p_token text,p_sha text)
returns boolean language plpgsql security definer set search_path='' as $$
begin
 perform weather_private.check_reader(p_token);
 return p_sha ~ '^[a-f0-9]{64}$' and exists(select 1 from public.weather_cloud_records where payload->>'pdf_sha256'=p_sha);
end $$;
create or replace function public.weather_storage_usage()
returns bigint language sql security definer set search_path='' as $$
 select coalesce(sum((metadata->>'size')::bigint),0) from storage.objects where bucket_id='pagasa-pdfs';
$$;
create or replace function public.weather_pdf_stored(p_sha text)
returns boolean language sql security definer set search_path='' as $$
 select exists(select 1 from storage.objects where bucket_id='pagasa-pdfs' and name=p_sha||'.pdf');
$$;
revoke all on function public.store_weather_record(text,jsonb),public.weather_source_checked(text,boolean),
 public.read_weather_archive(text,bigint),public.weather_pdf_access(text,text),public.weather_storage_usage(),public.weather_pdf_stored(text) from public,anon,authenticated;
grant execute on function public.store_weather_record(text,jsonb),public.weather_source_checked(text,boolean),public.weather_storage_usage(),public.weather_pdf_stored(text) to service_role;
grant execute on function public.read_weather_archive(text,bigint) to anon;
grant execute on function public.weather_pdf_access(text,text) to service_role;
-- No public Storage policies: only the cloud writer and guarded Edge Function
-- use service-role storage access. Never change the bucket to public.
do $$ begin
 if not exists(select 1 from storage.buckets where id='pagasa-pdfs' and not public) then
   raise exception 'Create the private pagasa-pdfs bucket first';
 end if;
end $$;
commit;
