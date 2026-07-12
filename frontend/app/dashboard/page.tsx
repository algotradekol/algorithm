'use client';
import { Suspense, useEffect, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { supabase } from '../../lib/supabaseClient';
import AlgoTab from '../../components/AlgoTab';
import CompareTab from '../../components/CompareTab';
import ChargesPanel from '../../components/ChargesPanel';
import HistoryTab from '../../components/HistoryTab';
import FyersLoginButton from '../../components/FyersLoginButton';

const TABS = ['Algo 1', 'Algo 2', 'Compare', 'History', 'Charges'] as const;

function DashboardContent() {
  const [tab, setTab] = useState<(typeof TABS)[number]>('Algo 1');
  const [ready, setReady] = useState(false);
  const [showFyersBanner, setShowFyersBanner] = useState(true);
  const router = useRouter();
  const searchParams = useSearchParams();
  const fyersLogin = searchParams.get('fyers_login');

  useEffect(() => {
    supabase.auth.getSession().then(({ data: { session } }) => {
      if (!session) router.replace('/login');
      else setReady(true);
    });
  }, [router]);

  if (!ready) return null;

  return (
    <div style={{ maxWidth: 1000, margin: '0 auto', padding: 24 }}>
      {fyersLogin && showFyersBanner && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 12,
            marginBottom: 16,
            padding: '10px 12px',
            borderRadius: 8,
            background: fyersLogin === 'success' ? '#10351f' : '#3b1515',
            border: `1px solid ${fyersLogin === 'success' ? '#198754' : '#b33a3a'}`,
          }}
        >
          <span style={{ color: '#fff', fontSize: 14 }}>
            {fyersLogin === 'success' ? 'Fyers login successful' : 'Fyers login failed, try again'}
          </span>
          <button
            onClick={() => setShowFyersBanner(false)}
            style={{ background: 'none', border: 'none', color: '#8a94a3', cursor: 'pointer', padding: 0 }}
          >
            Dismiss
          </button>
        </div>
      )}

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
        <h2>Algo Paper Trading</h2>
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
          <FyersLoginButton />
          <button
            onClick={async () => { await supabase.auth.signOut(); router.replace('/login'); }}
            style={{ background: 'none', border: '1px solid #333', color: '#8a94a3', padding: '6px 12px', borderRadius: 6 }}
          >
            Log out
          </button>
        </div>
      </div>

      <div style={{ display: 'flex', gap: 8, marginBottom: 24 }}>
        {TABS.map((t) => (
          <button
            key={t} onClick={() => setTab(t)}
            style={{
              padding: '8px 16px', borderRadius: 8, border: 'none', cursor: 'pointer',
              background: tab === t ? '#2a78d6' : '#151b23',
              color: tab === t ? '#fff' : '#8a94a3',
            }}
          >
            {t}
          </button>
        ))}
      </div>

      {tab === 'Algo 1' && <AlgoTab algoId="algo1" displayName="Algo 1 - Opening Range Gap" />}
      {tab === 'Algo 2' && <AlgoTab algoId="algo2" displayName="Algo 2 - VWAP/EMA/Volume Momentum" />}
      {tab === 'Compare' && <CompareTab />}
      {tab === 'History' && <HistoryTab />}
      {tab === 'Charges' && <ChargesPanel />}
    </div>
  );
}

export default function Dashboard() {
  return (
    <Suspense fallback={null}>
      <DashboardContent />
    </Suspense>
  );
}
