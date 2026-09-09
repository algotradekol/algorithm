-- Separate durable paper state and trade journal. Run in Supabase SQL Editor.
create table if not exists public.delta_paper_state (
    storage_key text primary key,
    state jsonb not null,
    updated_at timestamptz not null default now()
);
create table if not exists public.delta_paper_trades (
    storage_key text not null,
    id text not null,
    trade jsonb not null,
    closed_at timestamptz not null default now(),
    primary key (storage_key, id)
);
alter table public.delta_paper_state enable row level security;
alter table public.delta_paper_trades enable row level security;
-- A close and the position removal commit together, including retries.
create or replace function public.save_delta_paper(p_key text, p_state jsonb, p_trade jsonb default null)
returns void language plpgsql security invoker set search_path = public as $$
begin
    insert into delta_paper_state(storage_key, state, updated_at)
    values (p_key, p_state, now())
    on conflict (storage_key) do update set state = excluded.state, updated_at = now();
    if p_trade is not null then
        insert into delta_paper_trades(storage_key, id, trade)
        values (p_key, p_trade->>'id', p_trade)
        on conflict (storage_key, id) do nothing;
    end if;
end;
$$;
revoke all on function public.save_delta_paper(text, jsonb, jsonb) from public, anon, authenticated;
grant execute on function public.save_delta_paper(text, jsonb, jsonb) to service_role;
grant all on public.delta_paper_state, public.delta_paper_trades to service_role;
