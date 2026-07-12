# Algo Paper Trading App

Two intraday strategies (Algo 1: opening-range gap, Algo 2:
VWAP/EMA/volume momentum) running paper trades against live NSE 500
data from Fyers, with a shared configurable charges engine and a
password-gated web dashboard.

Start with `DEPLOY.md` — it walks through Supabase, Railway, and
Vercel setup in order.

## Structure

```
backend/    FastAPI app — the live engine, both strategies, Supabase
            read/writes, deployed on Railway
frontend/   Next.js app — login, 4 tabs (Algo 1, Algo 2, Compare,
            Charges), deployed on Vercel
```

## Local development (optional, before deploying)

Backend:
```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in real values
uvicorn app.main:app --reload
```

Frontend:
```bash
cd frontend
npm install
cp .env.local.example .env.local   # fill in real values, point
                                    # NEXT_PUBLIC_API_URL at localhost:8000
npm run dev
```
