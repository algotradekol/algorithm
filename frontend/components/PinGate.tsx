'use client';

import { FormEvent, useEffect, useState } from 'react';

const APP_PIN = '1402';
const PIN_SESSION_KEY = 'algo-pin-unlocked';

export function clearPinUnlock() {
  if (typeof window !== 'undefined') {
    window.sessionStorage.removeItem(PIN_SESSION_KEY);
  }
}

export default function PinGate({ children }: { children: React.ReactNode }) {
  const [unlocked, setUnlocked] = useState(false);
  const [pin, setPin] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    setUnlocked(window.sessionStorage.getItem(PIN_SESSION_KEY) === 'true');
  }, []);

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (pin === APP_PIN) {
      window.sessionStorage.setItem(PIN_SESSION_KEY, 'true');
      setUnlocked(true);
      setError('');
      return;
    }
    setError('Incorrect PIN');
    setPin('');
  }

  if (unlocked) return <>{children}</>;

  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <form onSubmit={handleSubmit} className="panel w-full max-w-sm p-6">
        <p className="text-xs uppercase tracking-[0.2em] text-textSoft">Secure desk</p>
        <h1 className="mt-2 text-2xl font-semibold text-white">Enter trading PIN</h1>
        <input
          className="control mt-5 text-center text-2xl tracking-[0.35em]"
          inputMode="numeric"
          maxLength={4}
          type="password"
          value={pin}
          autoFocus
          onChange={(event) => setPin(event.target.value.replace(/\D/g, '').slice(0, 4))}
        />
        {error && <p className="mt-3 text-sm text-danger">{error}</p>}
        <button
          type="submit"
          className="mt-5 w-full rounded-md bg-action px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-success hover:text-ink"
        >
          Unlock dashboard
        </button>
      </form>
    </main>
  );
}
