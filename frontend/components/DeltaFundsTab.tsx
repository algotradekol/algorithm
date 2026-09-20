'use client';

import { useEffect, useState } from 'react';
import { DeltaAsset, deltaApi } from '../lib/api';

type Transaction = {
  id: string | null;
  transaction_type: string;
  amount_usd: number | null;
  amount_inr: number | null;
  asset_symbol: string;
  balance_after_usd: number | null;
  reference: string | null;
  created_at: string | null;
};

const INR = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 });
const USD = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 });

function istDate(iso: string | null) {
  if (!iso) return '--';
  try {
    const d = new Date(iso);
    return d.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', dateStyle: 'medium', timeStyle: 'short' });
  } catch { return iso; }
}

function labelFor(kind: string) {
  if (kind === 'deposit' || kind === 'user_credit') return 'Deposit';
  if (kind === 'withdrawal' || kind === 'user_debit') return 'Withdrawal';
  return kind;
}

export default function DeltaFundsTab({ asset, mode }: { asset: DeltaAsset; mode: 'paper' | 'live' }) {
  const [rows, setRows] = useState<Transaction[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (mode !== 'live') return;
    let cancelled = false;
    deltaApi(asset, 'live').deltaWalletTransactions(200)
      .then((data: { transactions: Transaction[] }) => { if (!cancelled) { setRows(data.transactions); setError(null); } })
      .catch((err: Error) => { if (!cancelled) setError(err.message || 'Wallet history failed'); });
    return () => { cancelled = true; };
  }, [asset, mode]);
  if (mode !== 'live') return <p className="panel p-4 text-sm text-gray-400">Switch to LIVE mode to see funds history from your Delta account.</p>;
  if (error) return <p role="alert" className="panel p-4 text-sm text-[#f87171]">{error}</p>;
  if (rows === null) return <p className="panel p-4 text-sm text-gray-400">Loading funds history…</p>;
  if (rows.length === 0) return <p className="panel p-4 text-sm text-gray-400">No deposits or withdrawals on this Delta account yet.</p>;
  return <div className="panel overflow-x-auto">
    <table className="w-full text-sm">
      <thead>
        <tr className="border-b border-[#1f2937] text-left text-[11px] uppercase tracking-wide text-gray-500">
          <th className="px-3 py-2">Date (IST)</th>
          <th className="px-3 py-2">Type</th>
          <th className="px-3 py-2 text-right">Amount (INR)</th>
          <th className="px-3 py-2 text-right">Amount (USD)</th>
          <th className="px-3 py-2 text-right">Balance after (USD)</th>
          <th className="px-3 py-2">Reference</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => {
          const isDeposit = row.transaction_type === 'deposit' || row.transaction_type === 'user_credit';
          return <tr key={row.id || index} className="border-b border-[#111827]/60">
            <td className="px-3 py-2 text-gray-300">{istDate(row.created_at)}</td>
            <td className={`px-3 py-2 font-semibold ${isDeposit ? 'text-[#4ade80]' : 'text-[#f87171]'}`}>{labelFor(row.transaction_type)}</td>
            <td className="px-3 py-2 text-right text-gray-100">{row.amount_inr != null ? `₹${INR.format(row.amount_inr)}` : '--'}</td>
            <td className="px-3 py-2 text-right text-gray-400">{row.amount_usd != null ? `$${USD.format(row.amount_usd)}` : '--'}</td>
            <td className="px-3 py-2 text-right text-gray-400">{row.balance_after_usd != null ? `$${USD.format(row.balance_after_usd)}` : '--'}</td>
            <td className="px-3 py-2 text-gray-500">{row.reference || '--'}</td>
          </tr>;
        })}
      </tbody>
    </table>
  </div>;
}
