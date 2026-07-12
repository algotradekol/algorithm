# Algo Paper Trading — Deployment Guide

Architecture: Next.js frontend (Vercel) + FastAPI backend (Railway) +
Supabase (auth + database). Backend holds one live Fyers connection
feeding both Algo 1 and Algo 2 simultaneously.

## 1. Supabase setup (do this first)

1. In your existing Supabase project, go to SQL Editor -> New query,
   paste the entire contents of `backend/supabase_schema.sql`, run it.
2. Go to Authentication -> Users -> Add user. Create yourself (and
   anyone else who should log in) with an email + password. This is
   what gates the frontend -- no public signup, you add users
   manually.
3. Collect three values you'll need below:
   - Project URL and anon public key: Project Settings -> API
   - JWT Secret: Project Settings -> API -> JWT Settings
   - Service role key: same API settings page (keep this one secret --
     backend only, never in frontend code)

## 2. Backend on Railway

1. Push the `backend/` folder to a GitHub repo (or a repo containing
   both folders, pointing Railway at the `backend` subdirectory).
2. In Railway: New Project -> Deploy from GitHub repo.
3. In the service's Variables tab, add everything from
   `backend/.env.example`, filled in with your real values:
   - `FYERS_CLIENT_ID`, `FYERS_SECRET_KEY`, `FYERS_FY_ID`, `FYERS_PIN`,
     `FYERS_TOTP_KEY` — from your Fyers developer app + account 2FA
     setup (same as before)
   - `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_JWT_SECRET`
     — from step 1.3 above
   - `ALLOWED_ORIGINS` — your Vercel URL once you have it (step 3);
     you can update this after deploying the frontend
4. Railway auto-detects the `Procfile` and starts the app. Once
   deployed, note the public URL Railway gives you, e.g.
   `https://your-app.up.railway.app` — you need this for the frontend.
5. Check `https://your-app.up.railway.app/health` in a browser — it
   should return `{"status": "ok", ...}`.

## 3. Frontend on Vercel

1. Push `frontend/` to GitHub (same repo, different subdirectory is
   fine — set Vercel's "Root Directory" to `frontend`).
2. In Vercel: New Project -> Import your repo -> set Root Directory
   to `frontend`.
3. Add environment variables (Project Settings -> Environment
   Variables), from `frontend/.env.local.example`:
   - `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` — from
     step 1.3
   - `NEXT_PUBLIC_API_URL` — your Railway URL from step 2.4
4. Deploy. Vercel gives you a URL like `https://your-app.vercel.app`.
5. Go back to Railway and update `ALLOWED_ORIGINS` to that exact URL,
   so CORS allows the frontend to call the backend.

## 4. First login

Open your Vercel URL, log in with the email/password you created in
Supabase (step 1.2). You should land on the dashboard with 4 tabs:
Algo 1, Algo 2, Compare, Charges.

## 5. Before trusting this with real decisions

- Watch `token_refresh` / the engine logs (Railway -> Deployments ->
  View Logs) for the first few days to confirm the 8:45 AM Fyers
  auto-login is actually succeeding.
- Both algo implementations contain explicit ASSUMPTION comments
  where the original spec was ambiguous (candidate ordering in Algo 1's
  overflow logic, "average volume" window in Algo 2). Read those in
  `backend/app/strategies/algo1_opening_range.py` and
  `algo2_momentum.py` and adjust if they don't match what you intended.
- Charges defaults are standard published rates but can drift —
  cross-check against a current Fyers contract note via the Charges
  tab before trusting Net P&L numbers.
- This is paper trading throughout — no real orders are placed
  anywhere in this codebase. Going live is a deliberate, separate step
  we'd build once you're happy with the paper results.

## Adding a 3rd (or 4th, 5th...) algo later

1. Create `backend/app/strategies/algo3_whatever.py` implementing the
   `Strategy` interface from `base.py`.
2. Add two lines to `backend/app/engine.py`: import it, and add
   `STRATEGIES["algo3"] = Algo3Whatever(watchlist)` in `start_engine()`.
3. Add one line to `frontend/app/dashboard/page.tsx`'s `TABS` array and
   a matching `<AlgoTab algoId="algo3" .../>` block.
4. The Compare tab needs no changes — it already reads whatever algos
   the backend reports.
