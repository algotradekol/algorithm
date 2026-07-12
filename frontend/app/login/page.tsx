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
    <div style={{ display: 'flex', height: '100vh', alignItems: 'center', justifyContent: 'center' }}>
      <form onSubmit={handleLogin} style={{ width: 320, padding: 24, background: '#151b23', borderRadius: 12 }}>
        <h2 style={{ marginTop: 0 }}>Algo Paper Trading</h2>
        <input
          type="email" placeholder="Email" value={email} autoComplete="username" onChange={(e) => setEmail(e.target.value)}
          style={{ width: '100%', padding: 10, marginBottom: 10, borderRadius: 6, border: '1px solid #333', background: '#0b0f14', color: '#fff' }}
        />
        <input
          type="password" placeholder="Password" value={password} autoComplete="current-password" onChange={(e) => setPassword(e.target.value)}
          style={{ width: '100%', padding: 10, marginBottom: 10, borderRadius: 6, border: '1px solid #333', background: '#0b0f14', color: '#fff' }}
        />
        {error && <p style={{ color: '#ff6b6b', fontSize: 13 }}>{error}</p>}
        <button type="submit" style={{ width: '100%', padding: 10, borderRadius: 6, background: '#2a78d6', color: '#fff', border: 'none' }}>
          Log in
        </button>
      </form>
    </div>
  );
}
