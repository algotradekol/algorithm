'use client';

import { useEffect, useState } from 'react';
import { api } from '../lib/api';

const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Kolkata' }).format(new Date());
const defaultStart = new Date(`${today}T00:00:00`);
defaultStart.setDate(defaultStart.getDate() - 6);
const weekAgo = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Kolkata' }).format(defaultStart);

export default function BacktestTab() {
  const [algoId, setAlgoId] = useState('algo1');
  const [startDate, setStartDate] = useState(weekAgo);
  const [endDate, setEndDate] = useState(today);
  const [job, setJob] = useState<any>(null);
  const [error, setError] = useState('');

  async function run() {
    setError('');
    setJob(null);
    if (!startDate || !endDate) {
      setError('Choose both a start date and an end date.');
      return;
    }
    if (startDate > endDate) {
      setError('Start date must be on or before end date.');
      return;
    }
    if (endDate > today) {
      setError('Choose today or an earlier date.');
      return;
    }
    try {
      setJob(await api.startBacktest({ algo_id: algoId, start_date: startDate, end_date: endDate }));
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
        const message = e?.message || 'Could not read backtest progress';
        if (message.includes('API error 404')) {
          setJob(null);
          setError('This backtest was interrupted because the backend restarted or was redeployed. Start it again after the backend is healthy; the previous in-memory job cannot be recovered.');
          return;
        }
        setError(message);
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
        <p className="mt-1 max-w-3xl text-sm text-gray-500">Downloads each NSE 500 symbol once, then replays every weekday in your chosen range. It cannot create live paper trades or alter the live engine.</p>
        <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <label><span className="label">Strategy</span><select value={algoId} onChange={(e) => setAlgoId(e.target.value)} className="control mt-1"><option value="algo1">Simple 9:15</option><option value="algo2">Filter 9:15</option></select></label>
          <label><span className="label">Start date</span><input value={startDate} onChange={(e) => setStartDate(e.target.value)} max={today} type="date" className="control mt-1" /></label>
          <label><span className="label">End date</span><input value={endDate} onChange={(e) => setEndDate(e.target.value)} max={today} type="date" className="control mt-1" /></label>
          <div className="flex items-end"><button onClick={run} disabled={['queued', 'running'].includes(job?.status)} className="min-h-10 w-full rounded border border-[#3b82f6] bg-[#3b82f6] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"><i className="ri-play-circle-fill mr-2" />Run range backtest</button></div>
        </div>
        <p className="mt-3 text-xs text-[#f59e0b]"><i className="ri-error-warning-fill mr-1" />Maximum 31 calendar days. Entry uses the 09:16 candle open. If a later candle touches both SL and target, SL is assumed first.</p>
      </div>

      {error && <p className="rounded border border-[#ef4444]/40 bg-[#ef4444]/10 px-3 py-2 text-sm text-[#ef4444]">{error}</p>}
      {job && !result && <section className="panel p-4"><div className="flex justify-between gap-3 text-sm text-gray-200"><span>{job.message}</span><span className="num">{job.completed_symbols} / {job.total_symbols}</span></div><div className="mt-3 h-2 overflow-hidden rounded bg-[#020617]"><div className="h-full bg-[#3b82f6]" style={{ width: `${progress}%` }} /></div><p className="mt-2 text-xs text-gray-500">{progress}% complete. {job.failed_symbols || 0} symbols returned no usable history.</p></section>}
      {job?.status === 'failed' && <p className="rounded border border-[#ef4444]/40 bg-[#ef4444]/10 px-3 py-2 text-sm text-[#ef4444]">{job.error || job.message}</p>}
      {result && <BacktestResult result={result} />}
    </section>
  );
}

function BacktestResult({ result }: { result: any }) {
  const summary = result.summary || {};
  const coverage = result.data_coverage || {};
  const exits = summary.exit_counts || {};
  const daily = result.daily_results || [];
  return <>
    <section className="panel p-4">
      <div className="flex flex-wrap items-start justify-between gap-3"><div><h3 className="text-sm font-semibold text-gray-100">{result.start_date} to {result.end_date}</h3><p className="mt-1 max-w-3xl text-xs text-gray-500">{result.execution_assumption}</p></div><div className="flex items-center gap-3"><div className="text-xs text-gray-500">History coverage: <span className="num text-gray-100">{coverage.symbols_with_history} / {coverage.requested_symbols}</span></div><button onClick={() => downloadBacktestCsv(result)} className="inline-flex min-h-10 items-center gap-2 rounded border border-[#22c55e] bg-[#22c55e]/10 px-3 py-2 text-xs font-semibold text-[#22c55e]"><i className="ri-file-download-fill text-sm" />Download CSV</button></div></div>
      <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <Card label="Trading days" value={summary.trading_days_replayed || 0} />
        <Card label="Trades" value={summary.trade_count || 0} />
        <Card label="Wins / losses" value={`${summary.win_count || 0} / ${summary.loss_count || 0}`} />
        <Card label="Win rate" value={`${number(summary.win_rate_pct)}%`} tone={Number(summary.win_rate_pct) >= 50 ? 1 : -1} />
        <Card label="Profit factor" value={summary.profit_factor ?? '-'} tone={Number(summary.profit_factor) >= 1 ? 1 : -1} />
        <Card label="Net P&L" value={money(summary.net_pnl)} tone={Number(summary.net_pnl)} />
      </div>
    </section>
    <section className="grid gap-4 lg:grid-cols-2">
      <div className="panel p-4"><h3 className="text-sm font-semibold text-gray-100">Trade Quality</h3><div className="mt-3 grid grid-cols-2 gap-2 text-sm"><Metric label="Gross profit" value={money(summary.gross_profit)} positive /><Metric label="Gross loss" value={money(-Number(summary.gross_loss || 0))} negative /><Metric label="Average win" value={money(summary.average_win)} positive /><Metric label="Average loss" value={money(summary.average_loss)} negative /><Metric label="Average net / trade" value={money(summary.average_net_per_trade)} tone={Number(summary.average_net_per_trade)} /><Metric label="Max drawdown" value={money(summary.max_drawdown)} negative /></div></div>
      <div className="panel p-4"><h3 className="text-sm font-semibold text-gray-100">Execution And Range</h3><div className="mt-3 grid grid-cols-2 gap-2 text-sm"><Metric label="Gross P&L" value={money(summary.gross_pnl)} tone={Number(summary.gross_pnl)} /><Metric label="Charges" value={money(summary.total_charges)} /><Metric label="Capital deployed" value={money(summary.capital_deployed)} /><Metric label="Net return / deployed" value={`${number(summary.net_return_on_deployed_pct)}%`} tone={Number(summary.net_return_on_deployed_pct)} /><Metric label="Best day" value={result.best_day ? `${result.best_day.date}: ${money(result.best_day.net_pnl)}` : '-'} tone={Number(result.best_day?.net_pnl)} /><Metric label="Worst day" value={result.worst_day ? `${result.worst_day.date}: ${money(result.worst_day.net_pnl)}` : '-'} tone={Number(result.worst_day?.net_pnl)} /></div><p className="mt-3 text-xs text-gray-500">Exits: Target {exits.TARGET || 0}, SL {exits.SL || 0}, EOD {exits.EOD_SQUAREOFF || 0}.</p></div>
    </section>
    <DailyResults rows={daily} />
    <BacktestCandidates days={daily} />
    <BacktestTrades rows={daily.flatMap((day: any) => (day.trades || []).map((trade: any) => ({ ...trade, session_date: day.date })))} />
  </>;
}

function DailyResults({ rows }: { rows: any[] }) {
  return <section className="panel overflow-hidden"><div className="border-b border-[#1f2937] p-4"><h3 className="text-sm font-semibold text-gray-100">Daily Results</h3></div><div className="overflow-x-auto"><table className="w-full min-w-[900px] text-xs"><thead className="bg-[#111827]"><tr>{['Date', 'Data coverage', 'Trades', 'Wins / Losses', 'Win rate', 'Gross', 'Charges', 'Net', 'Selected'].map((name) => <th key={name} className="table-cell label">{name}</th>)}</tr></thead><tbody>{rows.map((day: any, index: number) => { const s = day.summary || {}; const selected = (day.condition_breakdown || []).find((step: any) => step.label === 'Final: selected for trade'); return <tr key={day.date} className={index % 2 ? 'bg-[#0d1117]' : 'bg-[#111827]'}><td className="table-cell num text-gray-100">{day.date}</td><td className="table-cell num">{day.data_available_symbols}</td><td className="table-cell num">{s.trade_count || 0}</td><td className="table-cell num">{s.win_count || 0} / {s.loss_count || 0}</td><td className="table-cell num">{number(s.win_rate_pct)}%</td><td className={`table-cell num ${tone(s.gross_pnl)}`}>{money(s.gross_pnl)}</td><td className="table-cell num">{money(s.total_charges)}</td><td className={`table-cell num font-semibold ${tone(s.net_pnl)}`}>{money(s.net_pnl)}</td><td className="table-cell num">{selected?.passed || 0}</td></tr>; })}</tbody></table></div></section>;
}

function BacktestCandidates({ days }: { days: any[] }) {
  const [selectedDate, setSelectedDate] = useState('');
  const [query, setQuery] = useState('');
  const [showMissing, setShowMissing] = useState(false);
  const [sortKey, setSortKey] = useState('symbol');
  const [sortDirection, setSortDirection] = useState<'asc' | 'desc'>('asc');
  useEffect(() => {
    if (days.length && !days.some((day) => day.date === selectedDate)) setSelectedDate(days[0].date);
  }, [days, selectedDate]);
  const day = days.find((item) => item.date === selectedDate) || days[0];
  const candidates = (day?.candidates || [])
    .filter((row: any) => showMissing || row.has_opening_candle)
    .filter((row: any) => row.symbol?.toLowerCase().includes(query.toLowerCase()))
    .sort((left: any, right: any) => compareCandidates(left, right, sortKey, sortDirection));
  const columns: [string, string][] = [['symbol', 'Symbol'], ['side', 'Side'], ['open', 'Open'], ['high', 'High'], ['low', 'Low'], ['close', 'Close'], ['volume', 'Volume'], ['prev_close', 'Prev Close'], ['gap_pct', 'Gap %'], ['shape_passed', 'Shape'], ['gap_passed', 'Gap'], ['filters_passed', 'Filters'], ['selected_for_trade', 'Selected'], ['rejection_reason', 'Reason'], ['vwap', 'VWAP'], ['rsi', 'RSI'], ['adx', 'ADX']];
  function toggleSort(key: string) { if (key === sortKey) setSortDirection((direction) => direction === 'asc' ? 'desc' : 'asc'); else { setSortKey(key); setSortDirection('asc'); } }
  return <section className="panel overflow-hidden"><div className="flex flex-col gap-3 border-b border-[#1f2937] p-4 sm:flex-row sm:items-end sm:justify-between"><div><h3 className="text-sm font-semibold text-gray-100">9:15 Candle Filtered List</h3><p className="mt-1 text-xs text-gray-500">{candidates.length} visible symbols. Missing 9:15 data is hidden by default, but remains available for audit.</p></div><div className="grid gap-2 sm:grid-cols-2"><select value={day?.date || ''} onChange={(e) => setSelectedDate(e.target.value)} className="control text-sm">{days.map((item) => <option key={item.date} value={item.date}>{item.date}</option>)}</select><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Filter symbols..." className="control text-sm" /><label className="flex min-h-10 items-center gap-2 text-xs text-gray-400 sm:col-span-2"><input type="checkbox" checked={showMissing} onChange={(e) => setShowMissing(e.target.checked)} /> Show missing 9:15 candle data</label></div></div><div className="overflow-x-auto"><table className="w-full min-w-[1450px] text-xs"><thead className="bg-[#111827]"><tr>{columns.map(([key, name]) => <th key={key} className="table-cell label"><button onClick={() => toggleSort(key)} className="inline-flex items-center gap-1 whitespace-nowrap text-left hover:text-[#3b82f6]">{name}<span className={sortKey === key ? 'text-[#3b82f6]' : 'text-gray-600'}>{sortKey === key ? (sortDirection === 'asc' ? '▲' : '▼') : '↕'}</span></button></th>)}</tr></thead><tbody>{!candidates.length ? <tr><td colSpan={17} className="table-cell text-gray-500">No rows to show. This date may be a market holiday, or enable missing-candle data for an audit view.</td></tr> : candidates.map((row: any, index: number) => <tr key={row.symbol} className={`${index % 2 ? 'bg-[#0d1117]' : 'bg-[#111827]'} ${row.selected_for_trade ? 'border-l-2 border-l-[#22c55e]' : row.filters_passed ? 'border-l-2 border-l-[#f59e0b]' : 'border-l-2 border-l-[#ef4444]'}`}><td className="table-cell font-mono text-gray-100">{row.symbol}</td><td className={row.side === 'BUY' ? 'table-cell text-[#22c55e]' : row.side === 'SELL' ? 'table-cell text-[#ef4444]' : 'table-cell text-gray-500'}>{row.side || 'WATCH'}</td><td className="table-cell num">{optionalNumber(row.open)}</td><td className="table-cell num">{optionalNumber(row.high)}</td><td className="table-cell num">{optionalNumber(row.low)}</td><td className="table-cell num">{optionalNumber(row.close)}</td><td className="table-cell num">{optionalNumber(row.volume)}</td><td className="table-cell num">{optionalNumber(row.prev_close)}</td><td className={`table-cell num ${Number(row.gap_pct) > 0 ? 'text-[#22c55e]' : Number(row.gap_pct) < 0 ? 'text-[#ef4444]' : ''}`}>{optionalNumber(row.gap_pct)}%</td><td className="table-cell">{flag(row.shape_passed)}</td><td className="table-cell">{flag(row.gap_passed)}</td><td className="table-cell">{flag(row.filters_passed)}</td><td className="table-cell">{flag(row.selected_for_trade)}</td><td className="table-cell text-gray-400">{row.rejection_reason || '--'}</td><td className="table-cell num">{indicatorValue(row, 'vwap')}</td><td className="table-cell num">{indicatorValue(row, 'rsi')}</td><td className="table-cell num">{indicatorValue(row, 'adx')}</td></tr>)}</tbody></table></div></section>;
}

function BacktestTrades({ rows }: { rows: any[] }) {
  return <section className="panel overflow-hidden"><div className="border-b border-[#1f2937] p-4"><h3 className="text-sm font-semibold text-gray-100">Simulated Trades</h3><p className="mt-1 text-xs text-gray-500">Times are based on the historical one-minute candle used for simulated entry and exit.</p></div><div className="overflow-x-auto"><table className="w-full min-w-[1150px] text-xs"><thead className="bg-[#111827]"><tr>{['Date', 'Symbol', 'Side', 'Qty', 'Entry Time', 'Entry', 'Exit Time', 'Exit', 'Reason', 'Net'].map((name) => <th key={name} className="table-cell label">{name}</th>)}</tr></thead><tbody>{!rows.length ? <tr><td colSpan={10} className="table-cell text-gray-500">No simulated trades in this range.</td></tr> : rows.map((trade, index) => <tr key={`${trade.session_date}-${trade.symbol}-${index}`} className={index % 2 ? 'bg-[#0d1117]' : 'bg-[#111827]'}><td className="table-cell num">{trade.session_date}</td><td className="table-cell font-mono text-gray-100">{trade.symbol}</td><td className={`table-cell font-semibold ${trade.side === 'BUY' ? 'text-[#22c55e]' : 'text-[#ef4444]'}`}>{trade.side}</td><td className="table-cell num">{trade.qty}</td><td className="table-cell num">{formatTime(trade.entry_time)}</td><td className="table-cell num">{number(trade.entry_price)}</td><td className="table-cell num">{formatTime(trade.exit_time)}</td><td className="table-cell num">{number(trade.exit_price)}</td><td className="table-cell">{trade.exit_reason}</td><td className={`table-cell num font-semibold ${tone(trade.net_pnl)}`}>{money(trade.net_pnl)}</td></tr>)}</tbody></table></div></section>;
}

function Card({ label, value, tone: valueTone }: { label: string; value: any; tone?: number }) { return <div className="rounded border border-[#1f2937] bg-[#111827] p-3"><div className="label">{label}</div><div className={`num mt-2 text-lg font-semibold ${tone(valueTone)}`}>{value}</div></div>; }
function Metric({ label, value, positive, negative, tone: valueTone }: { label: string; value: any; positive?: boolean; negative?: boolean; tone?: number }) { return <div className="rounded border border-[#1f2937] bg-[#0d1117] p-2"><div className="label">{label}</div><div className={`num mt-1 ${positive ? 'text-[#22c55e]' : negative ? 'text-[#ef4444]' : tone(valueTone)}`}>{value}</div></div>; }
function number(value: any) { const parsed = Number(value || 0); return parsed.toLocaleString('en-IN', { maximumFractionDigits: 2 }); }
function optionalNumber(value: any) { return value === null || value === undefined ? '--' : number(value); }
function indicatorValue(row: any, key: string) { const result = row.indicator_results?.[key]; return result?.value === null || result?.value === undefined ? '--' : number(result.value); }
function flag(value: boolean) { return <span className={value ? 'text-[#22c55e]' : 'text-[#ef4444]'}>{value ? 'Pass' : 'Fail'}</span>; }
function compareCandidates(left: any, right: any, key: string, direction: 'asc' | 'desc') {
  const leftValue = candidateSortValue(left, key);
  const rightValue = candidateSortValue(right, key);
  const leftMissing = leftValue === null || leftValue === undefined || leftValue === '';
  const rightMissing = rightValue === null || rightValue === undefined || rightValue === '';
  if (leftMissing || rightMissing) return leftMissing === rightMissing ? 0 : leftMissing ? 1 : -1;
  const comparison = typeof leftValue === 'number' && typeof rightValue === 'number'
    ? leftValue - rightValue
    : String(leftValue).localeCompare(String(rightValue));
  return direction === 'asc' ? comparison : -comparison;
}
function candidateSortValue(row: any, key: string) { return ['vwap', 'rsi', 'adx'].includes(key) ? row.indicator_results?.[key]?.value : row[key]; }
function money(value: any) { return `Rs ${number(value)}`; }
function tone(value?: number) { return value && value > 0 ? 'text-[#22c55e]' : value && value < 0 ? 'text-[#ef4444]' : 'text-gray-100'; }
function formatTime(value: unknown) { if (!value) return '--'; const date = new Date(String(value)); return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }); }

function downloadBacktestCsv(result: any) {
  const headers = ['Record Type', 'Date', 'Symbol', 'Side', 'Open', 'High', 'Low', 'Close', 'Volume', 'Previous Close', 'Gap %', 'Shape Passed', 'Gap Passed', 'Filters Passed', 'Selected For Trade', 'Rejection Reason', 'VWAP', 'RSI', 'ADX', 'Quantity', 'Entry Time IST', 'Entry Price', 'Exit Time IST', 'Exit Price', 'Exit Reason', 'Gross P&L', 'Charges', 'Net P&L', 'Metric', 'Value'];
  const rows: any[][] = [];
  Object.entries(result.summary || {}).forEach(([metric, value]) => rows.push(['Summary', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', metric, typeof value === 'object' ? JSON.stringify(value) : value]));
  (result.daily_results || []).forEach((day: any) => {
    const summary = day.summary || {};
    rows.push(['Daily Result', day.date, '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', summary.gross_pnl, summary.total_charges, summary.net_pnl, 'Trades / wins / losses', `${summary.trade_count || 0} / ${summary.win_count || 0} / ${summary.loss_count || 0}`]);
    (day.trades || []).forEach((trade: any) => rows.push(['Trade', day.date, trade.symbol, trade.side, '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', trade.qty, formatTime(trade.entry_time), trade.entry_price, formatTime(trade.exit_time), trade.exit_price, trade.exit_reason, trade.gross_pnl, trade.total_charges, trade.net_pnl, '', '']));
    (day.candidates || []).forEach((row: any) => rows.push(['Candidate', day.date, row.symbol, row.side, row.open, row.high, row.low, row.close, row.volume, row.prev_close, row.gap_pct, row.shape_passed, row.gap_passed, row.filters_passed, row.selected_for_trade, row.rejection_reason, row.indicator_results?.vwap?.value, row.indicator_results?.rsi?.value, row.indicator_results?.adx?.value, '', '', '', '', '', '', '', '', '', '', '']));
  });
  const csv = [headers, ...rows].map((row) => row.map(csvValue).join(',')).join('\r\n');
  const blob = new Blob([`\uFEFF${csv}`], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `backtest_${result.algo_id}_${result.start_date}_to_${result.end_date}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}

function csvValue(value: unknown) {
  const text = String(value ?? '');
  // Avoid spreadsheet formula execution when a symbol or text begins with a formula prefix.
  const safe = /^[=+\-@]/.test(text) ? `'${text}` : text;
  return `"${safe.replace(/"/g, '""')}"`;
}
