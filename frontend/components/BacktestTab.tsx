'use client';

import { useEffect, useState } from 'react';
import { api } from '../lib/api';

const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Kolkata' }).format(new Date());

export default function BacktestTab() {
  const [algoId, setAlgoId] = useState('algo1');
  const [date, setDate] = useState(today);
  const [job, setJob] = useState<any>(null);
  const [error, setError] = useState('');

  async function run() {
    setError('');
    setJob(null);
    try {
      setJob(await api.startBacktest({ algo_id: algoId, date }));
    } catch (e: any) {
      setError(e?.message || 'Could not start backtest');
    }
  }

  useEffect(() => {
    if (!job?.id || !['queued', 'running'].includes(job.status)) return;
    const timer = window.setInterval(async () => {
      try {
        setJob(await api.backtestStatus(job.id));
      } catch (e: any) {
        setError(e?.message || 'Could not read backtest progress');
      }
    }, 2_000);
    return () => window.clearInterval(timer);
  }, [job?.id, job?.status]);

  const progress = job ? Math.round((Number(job.completed_symbols || 0) / Math.max(1, Number(job.total_symbols || 1))) * 100) : 0;
  const result = job?.result;
  return (
    <section className="space-y-4">
      <div className="panel p-4">
        <h2 className="text-base font-semibold text-gray-100">Historical Backtest</h2>
        <p className="mt-1 max-w-3xl text-sm text-gray-500">
          Replays the selected trading date using Fyers 1-minute OHLCV candles. It never starts live paper trades or changes live positions.
        </p>
        <div className="mt-4 grid gap-3 sm:grid-cols-3">
          <label><span className="label">Strategy</span><select value={algoId} onChange={(e) => setAlgoId(e.target.value)} className="control mt-1"><option value="algo1">Simple 9:15</option><option value="algo2">Filter 9:15</option></select></label>
          <label><span className="label">Trading date</span><input value={date} onChange={(e) => setDate(e.target.value)} max={today} type="date" className="control mt-1" /></label>
          <div className="flex items-end"><button onClick={run} disabled={['queued', 'running'].includes(job?.status)} className="min-h-10 w-full rounded border border-[#3b82f6] bg-[#3b82f6] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"><i className="ri-play-circle-fill mr-2" />Run NSE 500 Backtest</button></div>
        </div>
        <p className="mt-3 text-xs text-[#f59e0b]"><i className="ri-error-warning-fill mr-1" />Historical approximation: entry uses the 09:16 candle open. If one later candle touches both SL and target, SL is assumed first.</p>
      </div>

      {error && <p className="rounded border border-[#ef4444]/40 bg-[#ef4444]/10 px-3 py-2 text-sm text-[#ef4444]">{error}</p>}
      {job && !result && <section className="panel p-4"><div className="flex justify-between gap-3 text-sm text-gray-200"><span>{job.message}</span><span className="num">{job.completed_symbols} / {job.total_symbols}</span></div><div className="mt-3 h-2 overflow-hidden rounded bg-[#020617]"><div className="h-full bg-[#3b82f6]" style={{ width: `${progress}%` }} /></div><p className="mt-2 text-xs text-gray-500">{progress}% complete · {job.failed_symbols || 0} symbols returned no usable history.</p></section>}
      {job?.status === 'failed' && <p className="rounded border border-[#ef4444]/40 bg-[#ef4444]/10 px-3 py-2 text-sm text-[#ef4444]">{job.error || job.message}</p>}
      {result && <BacktestResult result={result} />}
    </section>
  );
}

function BacktestResult({ result }: { result: any }) {
  const summary = result.summary || {};
  const coverage = result.data_coverage || {};
  return <>
    <section className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-6">
      <Card label="Trades" value={summary.trade_count || 0} />
      <Card label="Buy / Sell" value={`${summary.buy_count || 0}B ${summary.sell_count || 0}S`} />
      <Card label="Wins / Losses" value={`${summary.win_count || 0} / ${summary.loss_count || 0}`} />
      <Card label="Gross P&L" value={money(summary.gross_pnl)} tone={Number(summary.gross_pnl)} />
      <Card label="Charges" value={money(summary.total_charges)} />
      <Card label="Net P&L" value={money(summary.net_pnl)} tone={Number(summary.net_pnl)} />
    </section>
    <section className="panel p-4"><div className="flex flex-wrap items-start justify-between gap-3"><div><h3 className="text-sm font-semibold text-gray-100">{result.date} replay</h3><p className="mt-1 text-xs text-gray-500">{result.execution_assumption}</p></div><div className="text-xs text-gray-500">History coverage: <span className="num text-gray-100">{coverage.symbols_with_history} / {coverage.requested_symbols}</span></div></div><div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{(result.condition_breakdown || []).map((step: any) => <div key={step.label} className="rounded border border-[#1f2937] bg-[#0d1117] p-3"><div className="label">{step.label}</div><div className="num mt-1 text-lg font-semibold text-gray-100">{step.passed} <span className="text-gray-500">/ {step.total}</span></div></div>)}</div></section>
    <section className="panel overflow-hidden"><div className="border-b border-[#1f2937] p-4"><h3 className="text-sm font-semibold text-gray-100">Simulated Trades</h3></div><div className="overflow-x-auto"><table className="w-full min-w-[850px] text-xs"><thead className="bg-[#111827]"><tr>{['Symbol', 'Side', 'Qty', 'Entry', 'Exit', 'Reason', 'Gross', 'Charges', 'Net'].map((name) => <th key={name} className="table-cell label">{name}</th>)}</tr></thead><tbody>{!(result.trades || []).length ? <tr><td colSpan={9} className="table-cell text-gray-500">No simulated trades for this date.</td></tr> : result.trades.map((trade: any, index: number) => <tr key={`${trade.symbol}-${index}`} className={index % 2 ? 'bg-[#0d1117]' : 'bg-[#111827]'}><td className="table-cell font-mono text-gray-100">{trade.symbol}</td><td className={`table-cell font-semibold ${trade.side === 'BUY' ? 'text-[#22c55e]' : 'text-[#ef4444]'}`}>{trade.side}</td><td className="table-cell num">{trade.qty}</td><td className="table-cell num">{number(trade.entry_price)}</td><td className="table-cell num">{number(trade.exit_price)}</td><td className="table-cell">{trade.exit_reason}</td><td className={`table-cell num ${tone(trade.gross_pnl)}`}>{money(trade.gross_pnl)}</td><td className="table-cell num">{money(trade.total_charges)}</td><td className={`table-cell num font-semibold ${tone(trade.net_pnl)}`}>{money(trade.net_pnl)}</td></tr>)}</tbody></table></div></section>
  </>;
}

function Card({ label, value, tone: valueTone }: { label: string; value: any; tone?: number }) { return <div className="rounded border border-[#1f2937] bg-[#111827] p-3"><div className="label">{label}</div><div className={`num mt-2 text-lg font-semibold ${tone(valueTone)}`}>{value}</div></div>; }
function number(value: any) { const parsed = Number(value || 0); return parsed.toLocaleString('en-IN', { maximumFractionDigits: 2 }); }
function money(value: any) { return `Rs ${number(value)}`; }
function tone(value?: number) { return value && value > 0 ? 'text-[#22c55e]' : value && value < 0 ? 'text-[#ef4444]' : 'text-gray-100'; }
