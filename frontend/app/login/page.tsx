'use client';
import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { supabase } from '../../lib/supabaseClient';
import { clearPinToken, setPinToken } from '../../lib/pinAuth';

const API_URL = process.env.NEXT_PUBLIC_API_URL;

export default function Login() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [pin, setPin] = useState('');
  const [error, setError] = useState('');
  const [emailLoading, setEmailLoading] = useState(false);
  const [pinLoading, setPinLoading] = useState(false);
  const router = useRouter();

  async function handleLogin(e: React.FormEvent) {
    e.preventDefault();
    setError('');
    setEmailLoading(true);
    clearPinToken();
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    setEmailLoading(false);
    if (error) setError(error.message);
    else router.push('/dashboard');
  }

  async function handlePinLogin(e: React.FormEvent) {
    e.preventDefault();
    setError('');
    setPinLoading(true);
    if (!API_URL) {
      setError('NEXT_PUBLIC_API_URL is not configured');
      setPinLoading(false);
      return;
    }
    await supabase.auth.signOut();

    const res = await fetch(`${API_URL}/api/pin-login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pin }),
    });

    setPinLoading(false);
    if (!res.ok) {
      setError('Incorrect PIN');
      setPin('');
      return;
    }

    const data = await res.json() as { access_token: string };
    setPinToken(data.access_token);
    router.push('/dashboard');
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-[#0a0e14] px-4 py-8">
      <section className="w-full max-w-md rounded border border-[#1f2937] bg-[#111827] p-6">
        <div className="border-b border-[#1f2937] pb-5">
          <div className="font-mono text-base font-semibold tracking-[0.18em] text-gray-100">ALGO TRADING</div>
          <p className="mt-1 text-xs uppercase tracking-wider text-gray-500">Paper Trading Dashboard</p>
        </div>

        <form onSubmit={handleLogin} className="mt-5">
          <label className="label" htmlFor="email">Email</label>
          <input
            id="email"
            type="email"
            placeholder="you@example.com"
            value={email}
            autoComplete="username"
            onChange={(e) => setEmail(e.target.value)}
            className="control mt-1"
          />

          <label className="label mt-4 block" htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            placeholder="Password"
            value={password}
            autoComplete="current-password"
            onChange={(e) => setPassword(e.target.value)}
            className="control mt-1"
          />

          <button
            type="submit"
            disabled={emailLoading}
            className="mt-5 w-full rounded border border-[#3b82f6] bg-[#3b82f6] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-70"
          >
            {emailLoading ? 'Logging in...' : 'Login with email'}
          </button>
        </form>

        <div className="my-5 flex items-center gap-3">
          <div className="h-px flex-1 bg-[#1f2937]" />
          <span className="text-xs uppercase tracking-wider text-gray-500">or pin</span>
          <div className="h-px flex-1 bg-[#1f2937]" />
        </div>

        <form onSubmit={handlePinLogin}>
          <label className="label" htmlFor="pin">Custom PIN</label>
          <input
            id="pin"
            className="control mt-1 text-center font-mono text-2xl tracking-[0.35em]"
            inputMode="numeric"
            maxLength={4}
            type="password"
            autoComplete="one-time-code"
            placeholder="****"
            value={pin}
            onChange={(e) => setPin(e.target.value.replace(/\D/g, '').slice(0, 4))}
          />
          <button
            type="submit"
            disabled={pinLoading}
            className="mt-5 w-full rounded border border-[#3b82f6] bg-[#3b82f6] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-70"
          >
            {pinLoading ? 'Logging in...' : 'Login with PIN'}
          </button>
        </form>

        {error && <p className="mt-4 border border-[#ef4444]/40 bg-[#ef4444]/10 px-3 py-2 text-sm text-[#ef4444]">{error}</p>}
      </section>
    </main>
  );
}
