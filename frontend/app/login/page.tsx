'use client';
import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { supabase } from '../../lib/supabaseClient';

export default function Login() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const router = useRouter();

  async function handleLogin(e: React.FormEvent) {
    e.preventDefault();
    setError('');
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) setError(error.message);
    else router.push('/dashboard');
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <form onSubmit={handleLogin} className="panel w-full max-w-sm p-6">
        <p className="text-xs uppercase tracking-[0.2em] text-textSoft">Supabase access</p>
        <h1 className="mt-2 text-2xl font-semibold text-white">Algo Paper Trading</h1>
        <input
          type="email" placeholder="Email" value={email} autoComplete="username" onChange={(e) => setEmail(e.target.value)}
          className="control mt-5"
        />
        <input
          type="password" placeholder="Password" value={password} autoComplete="current-password" onChange={(e) => setPassword(e.target.value)}
          className="control mt-3"
        />
        {error && <p className="mt-3 text-sm text-danger">{error}</p>}
        <button type="submit" className="mt-5 w-full rounded-md bg-action px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-success hover:text-ink">
          Log in
        </button>
      </form>
    </main>
  );
}
