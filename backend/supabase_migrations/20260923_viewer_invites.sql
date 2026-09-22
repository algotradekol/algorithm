-- One-use, time-limited viewer invite codes for read-only Delta demos.
create table if not exists public.viewer_invites (
    id uuid primary key default gen_random_uuid(),
    code_hash text not null unique,
    label text,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    redeemed_at timestamptz,
    revoked_at timestamptz
);

alter table public.viewer_invites enable row level security;

create or replace function public.redeem_viewer_invite(p_code_hash text)
returns public.viewer_invites
language plpgsql
security invoker
set search_path = public
as $$
declare
    invite public.viewer_invites;
begin
    update public.viewer_invites
       set redeemed_at = now()
     where code_hash = p_code_hash
       and redeemed_at is null
       and revoked_at is null
       and expires_at > now()
     returning * into invite;

    return invite;
end;
$$;

revoke all on function public.redeem_viewer_invite(text) from public, anon, authenticated;
grant execute on function public.redeem_viewer_invite(text) to service_role;
grant all on public.viewer_invites to service_role;
