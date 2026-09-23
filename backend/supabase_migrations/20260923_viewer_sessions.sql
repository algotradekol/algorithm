-- Browser-bound 24h viewer sessions created after a one-use invite is redeemed.
create table if not exists public.viewer_sessions (
    id uuid primary key default gen_random_uuid(),
    invite_id uuid not null references public.viewer_invites(id) on delete cascade,
    device_hash text not null,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    last_seen_at timestamptz,
    revoked_at timestamptz
);

create index if not exists viewer_sessions_invite_id_idx on public.viewer_sessions(invite_id);
create index if not exists viewer_sessions_device_hash_idx on public.viewer_sessions(device_hash);

alter table public.viewer_sessions enable row level security;

create or replace function public.redeem_viewer_invite_session(
    p_code_hash text,
    p_device_hash text,
    p_expires_at timestamptz
)
returns table (
    invite_id uuid,
    label text,
    invite_created_at timestamptz,
    invite_expires_at timestamptz,
    redeemed_at timestamptz,
    session_id uuid,
    session_expires_at timestamptz
)
language plpgsql
security invoker
set search_path = public
as $$
declare
    invite public.viewer_invites;
    session_row public.viewer_sessions;
begin
    update public.viewer_invites
       set redeemed_at = now()
     where code_hash = p_code_hash
       and redeemed_at is null
       and revoked_at is null
       and expires_at > now()
     returning * into invite;

    if invite.id is null then
        return;
    end if;

    insert into public.viewer_sessions (invite_id, device_hash, expires_at)
    values (invite.id, p_device_hash, p_expires_at)
    returning * into session_row;

    invite_id := invite.id;
    label := invite.label;
    invite_created_at := invite.created_at;
    invite_expires_at := invite.expires_at;
    redeemed_at := invite.redeemed_at;
    session_id := session_row.id;
    session_expires_at := session_row.expires_at;
    return next;
end;
$$;

revoke all on function public.redeem_viewer_invite_session(text, text, timestamptz) from public, anon, authenticated;
grant execute on function public.redeem_viewer_invite_session(text, text, timestamptz) to service_role;
grant all on public.viewer_sessions to service_role;
