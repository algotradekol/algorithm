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
  const router = useRouter();

  async function handleLogin(e: React.FormEvent) {
    e.preventDefault();
    setError('');
    clearPinToken();
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) setError(error.message);
    else router.push('/dashboard');
  }

  async function handlePinLogin(e: React.FormEvent) {
    e.preventDefault();
    setError('');
    if (!API_URL) {
      setError('NEXT_PUBLIC_API_URL is not configured');
      return;
    }
    await supabase.auth.signOut();

    const res = await fetch(`${API_URL}/api/pin-login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pin }),
    });

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
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="panel grid w-full max-w-3xl gap-6 p-6 md:grid-cols-2">
        <form onSubmit={handleLogin}>
          <p className="text-xs uppercase tracking-[0.2em] text-textSoft">Email access</p>
          <h1 className="mt-2 text-2xl font-semibold text-white">Algo Paper Trading</h1>
          <input
            type="email" placeholder="Email" value={email} autoComplete="username" onChange={(e) => setEmail(e.target.value)}
            className="control mt-5"
          />
          <input
            type="password" placeholder="Password" value={password} autoComplete="current-password" onChange={(e) => setPassword(e.target.value)}
            className="control mt-3"
          />
          <button type="submit" className="mt-5 w-full rounded-md bg-action px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-success hover:text-ink">
            Log in with email
          </button>
        </form>

        <form onSubmit={handlePinLogin} className="border-t border-line pt-6 md:border-l md:border-t-0 md:pl-6 md:pt-0">
          <p className="text-xs uppercase tracking-[0.2em] text-textSoft">Quick access</p>
          <h2 className="mt-2 text-2xl font-semibold text-white">Use PIN</h2>
          <input
            className="control mt-5 text-center text-2xl tracking-[0.35em]"
            inputMode="numeric"
            maxLength={4}
            type="password"
            placeholder="1402"
            value={pin}
            onChange={(e) => setPin(e.target.value.replace(/\D/g, '').slice(0, 4))}
          />
          <button type="submit" className="mt-5 w-full rounded-md bg-success px-4 py-2.5 text-sm font-semibold text-ink transition hover:bg-white">
            Log in with PIN
          </button>
        </form>
        {error && <p className="mt-3 text-sm text-danger">{error}</p>}
      </div>
    </main>
  );
}
