# Deployment Workflow

Two environments share this repo. **Same code, two deployments, different secrets.**

```
main branch          →  DEV Railway  + DEV Vercel   (your Fyers, your Supabase)
production branch    →  PROD Railway + PROD Vercel  (client's Fyers, client's Supabase)
```

Client only gets updates when you explicitly merge `main` → `production`.

## Daily workflow

### 1. Develop on `main`

```bash
git checkout main
# ... edit code, run smoke tests ...
cd backend && python -m tests.smoke_live_orders    # must pass
git add -A
git commit -m "feat: whatever"
git push
```

DEV Railway auto-deploys `main` → test on your dev URL.

### 2. Verified working? Promote to `production`

```bash
git checkout production
git pull                    # get latest production state (usually a no-op)
git merge main --no-ff -m "promote to prod: <short summary>"
git push
```

PROD Railway auto-deploys `production` → client's app updates.

Use `--no-ff` so each promote shows as a merge commit in the log — easy to spot in `git log --oneline --graph`.

### 3. If a prod deploy breaks, roll back

```bash
git checkout production
git revert HEAD             # or `git reset --hard <last-good-commit>` if you're sure
git push
```

PROD Railway auto-redeploys the reverted version. Fix the bug on `main`, verify, re-promote.

## What lives WHERE

| Concern               | DEV (main)                          | PROD (production)                     |
| --------------------- | ----------------------------------- | ------------------------------------- |
| Fyers Trading API app | Your `ONJWWY4966-200`               | Client's own `-200` app               |
| Fyers Primary IP      | `34.100.255.224` (GCP Squid proxy)  | `34.100.255.224` (same proxy)         |
| Supabase project      | Your existing project               | Client's own Supabase project         |
| Railway service       | current one                         | NEW project pointed at same repo      |
| Deploy branch         | `main`                              | `production`                          |
| Frontend URL          | e.g. `test.kolkatalgo.in`           | Client's domain                       |
| Backend URL           | `api.kolkatalgo.in` (current)       | Client's backend subdomain            |

## Environment variables (Railway)

The **only** difference between DEV and PROD is these values. Same code reads them either way.

### Backend (both DEV and PROD Railway)

Required in **both** but with **different values**:

```
LIVE_FYERS_CLIENT_ID       = <mode>-specific
LIVE_FYERS_SECRET_KEY      = <mode>-specific
LIVE_FYERS_REDIRECT_URI    = https://<mode>-backend/api/fyers/callback
LIVE_FYERS_PROXY_URL       = https://bore-tunnel/  (both can share)

FYERS_CLIENT_ID            = <mode>-specific (paper Data API app)
FYERS_SECRET_KEY           = <mode>-specific
FYERS_REDIRECT_URI         = https://<mode>-backend/api/fyers/callback

SUPABASE_URL               = <mode>-specific project URL
SUPABASE_ANON_KEY          = <mode>-specific
SUPABASE_SERVICE_ROLE_KEY  = <mode>-specific

FRONTEND_URL               = <mode>-specific frontend URL

# Any others in backend/app/config.py — check that file
```

### Frontend (both DEV and PROD Vercel)

```
NEXT_PUBLIC_API_BASE_URL   = <mode> backend URL
```

## Client onboarding (one-time)

Before pointing PROD at the client:

1. Client creates their Fyers Trading API app (see message you sent them)
2. Client shares: App ID, Secret ID, Fyers User ID
3. Client sets **Primary IP = `34.100.255.224`** in their Fyers app
4. You set up their Supabase project (or use theirs) and run the schema migrations
5. Enter all client-specific values into PROD Railway env vars
6. Set PROD Railway's **Deploy Branch = `production`**
7. Set PROD Vercel's **Production Branch = `production`**

## Schema migrations

Whenever a new column is added (like the recent `order_type`, `parallel_paper_enabled`, `signal_snapshot`), run the ALTER in **both** Supabase projects. Committed SQL files under `backend/migrations/` (create as needed) so you don't lose track.

Known migrations from recent work:

```sql
-- 2026-08-10: LIMIT-at-LTP order type + entry_trigger/signal_snapshot audit
ALTER TABLE public.strategy_settings
  ADD COLUMN IF NOT EXISTS order_type text DEFAULT 'LIMIT';
ALTER TABLE public.live_positions
  ADD COLUMN IF NOT EXISTS entry_trigger text,
  ADD COLUMN IF NOT EXISTS signal_snapshot jsonb;
ALTER TABLE public.live_trades
  ADD COLUMN IF NOT EXISTS entry_trigger text,
  ADD COLUMN IF NOT EXISTS signal_snapshot jsonb;

-- 2026-08-13: parallel paper trading toggle
ALTER TABLE public.strategy_settings
  ADD COLUMN IF NOT EXISTS parallel_paper_enabled boolean DEFAULT true;
```

## Rules

- **Never commit `.env` files** — always use Railway/Vercel env var UIs.
- **Never hardcode a Fyers `client_id` in Python** — always read from env.
- **`main` and `production` diverge only briefly.** Merge frequently or you'll get merge conflicts.
- **Never `git push --force` on `production`** without a fresh backup of the client's Supabase — it can leave the deploy in a broken state.
- **Smoke tests must pass before promoting.** `cd backend && python -m tests.smoke_live_orders` → EXIT=0.
