'use client';

import { useEffect, useMemo, useState } from 'react';
import { api } from '../lib/api';
import { PAGE_SIZE, PaginationControls } from './PaginationControls';
import SilverBacktestChart from './SilverBacktestChart';

const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Kolkata' }).format(new Date());
const defaultStart = new Date(`${today}T00:00:00`);
defaultStart.setDate(defaultStart.getDate() - 6);
const weekAgo = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Kolkata' }).format(defaultStart);
const BACKTEST_STORAGE_KEY = 'backtest-tab-state-v1';
const BACKTEST_UI_STORAGE_KEY = 'backtest-tab-ui-v1';
const SILVER_BUY_PLANS = {
  live_breakout: {
    label: '15m breakout (current)',
    description: 'Green 15m close above EMA20 becomes the reference; a later 1-minute break above reference + n enters.',
  },
  legacy_confirmation: {
    label: '5m EMA/volume confirmation (legacy)',
    description: 'Green 5m close above price EMA20 and volume EMA20, confirmed within 15 minutes, enters next 5m open.',
  },
};
const SILVER_SELL_PLANS = {
  red_chain: {
    label: 'Red-chain comparison (current)',
    description: 'Compare each new qualifying red close with the previous red reference. Green candles do not reset it.',
  },
  latest_reference: {
    label: 'Latest red reference (legacy)',
    description: 'Replace the reference on every qualifying red candle, then wait for a later 1-minute break below it.',
  },
};

export default function BacktestTab() {
  const [algoId, setAlgoId] = useState('algo1');
  const [silverBuyPlan, setSilverBuyPlan] = useState('live_breakout');
  const [silverSellPlan, setSilverSellPlan] = useState('red_chain');
  const [startDate, setStartDate] = useState(weekAgo);
  const [endDate, setEndDate] = useState(today);
  const [job, setJob] = useState<any>(null);
  const [error, setError] = useState('');
  const [storageReady, setStorageReady] = useState(false);
  const active = ['queued', 'running', 'cancelling'].includes(job?.status);

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(BACKTEST_STORAGE_KEY);
      if (!raw) {
        setStorageReady(true);
        return;
      }
      const snapshot = JSON.parse(raw);
      if (snapshot?.algoId) setAlgoId(snapshot.algoId);
      if (snapshot?.silverBuyPlan === 'live_breakout' || snapshot?.silverBuyPlan === 'legacy_confirmation') setSilverBuyPlan(snapshot.silverBuyPlan);
      if (snapshot?.silverSellPlan === 'red_chain' || snapshot?.silverSellPlan === 'latest_reference') setSilverSellPlan(snapshot.silverSellPlan);
      if (snapshot?.startDate) setStartDate(snapshot.startDate);
      if (snapshot?.endDate) setEndDate(snapshot.endDate);
      if (snapshot?.job) setJob(snapshot.job);
      if (snapshot?.error) setError(snapshot.error);
    } catch {
      // Ignore corrupted local snapshots and start fresh.
    } finally {
      setStorageReady(true);
    }
  }, []);

  useEffect(() => {
    if (!storageReady) return;
    try {
      window.localStorage.setItem(BACKTEST_STORAGE_KEY, JSON.stringify({
        algoId,
        silverBuyPlan,
        silverSellPlan,
        startDate,
        endDate,
        job,
        error,
        savedAt: Date.now(),
      }));
    } catch {
      // Best-effort persistence only.
    }
  }, [algoId, silverBuyPlan, silverSellPlan, startDate, endDate, job, error, storageReady]);

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
      setJob(await api.startBacktest({
        algo_id: algoId,
        start_date: startDate,
        end_date: endDate,
        ...(algoId === 'algo3' ? { silver_buy_plan: silverBuyPlan, silver_sell_plan: silverSellPlan } : {}),
      }));
    } catch (e: any) {
      setError(e?.message || 'Could not start backtest');
    }
  }

  async function cancel() {
    if (!job?.id || !active) return;
    setError('');
    try {
      setJob(await api.cancelBacktest(job.id));
    } catch (e: any) {
      setError(e?.message || 'Could not cancel backtest');
    }
  }

  useEffect(() => {
    if (!job?.id || !['queued', 'running', 'cancelling'].includes(job.status)) return;
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

  useEffect(() => {
    if (!storageReady || !job?.id || !['queued', 'running', 'cancelling'].includes(job.status)) return;
    let cancelled = false;
    (async () => {
      try {
        const latest = await api.backtestStatus(job.id);
        if (!cancelled) setJob(latest);
      } catch (e: any) {
        const message = e?.message || 'Could not restore backtest progress';
        if (!cancelled && !message.includes('API error 404')) setError(message);
      }
    })();
    return () => { cancelled = true; };
  }, [job?.id, job?.status, storageReady]);

  const replaying = job?.phase === 'replaying';
  const progressCompleted = replaying ? Number(job.replay_completed || 0) : Number(job?.completed_symbols || 0);
  const progressTotal = replaying ? Number(job.replay_total || 0) : Number(job?.total_symbols || 0);
  const progress = job ? Math.round((progressCompleted / Math.max(1, progressTotal)) * 100) : 0;
  const result = job?.result;
  const introCopy = algoId === 'algo3'
    ? {
        title: 'Historical Silver Micro Backtest',
        body: 'Replays MCX:SILVERMIC26AUGFUT on 15-minute candles with EMA20 breakout logic. Read-only; does not touch the live engine.',
        note: 'Uses the live tab’s rules: green/red setup candle stores its close as the level; entry fires when price crosses (setup close +/- n) in the setup direction. n defaults to 150 points.',
      }
    : {
        title: 'Historical Backtest',
        body: 'Downloads each NSE 500 symbol once, then replays every weekday in your chosen range. It cannot create live paper trades or alter the live engine.',
        note: 'Maximum 31 calendar days. Signal uses the 09:15 candle only; entry uses the 09:16 candle open. If a later candle touches both SL and target, SL is assumed first.',
      };
  return (
    <section className="space-y-4">
      <div className="panel p-4">
        <h2 className="text-base font-semibold text-gray-100">{introCopy.title}</h2>
        <p className="mt-1 max-w-3xl text-sm text-gray-500">{introCopy.body}</p>
        <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
          <label><span className="label">Strategy</span><select value={algoId} onChange={(e) => setAlgoId(e.target.value)} className="control mt-1"><option value="algo1">Simple 9:15</option><option value="algo2">Filter 9:15</option><option value="algo3">Silver Micro (MCX:SILVERMIC26AUGFUT)</option></select></label>
          {algoId === 'algo3' && <label><span className="label">Silver BUY logic</span><select value={silverBuyPlan} onChange={(e) => setSilverBuyPlan(e.target.value)} className="control mt-1"><option value="live_breakout">{SILVER_BUY_PLANS.live_breakout.label}</option><option value="legacy_confirmation">{SILVER_BUY_PLANS.legacy_confirmation.label}</option></select><span className="mt-1 block text-[11px] leading-4 text-gray-500">{SILVER_BUY_PLANS[silverBuyPlan as keyof typeof SILVER_BUY_PLANS].description}</span></label>}
          {algoId === 'algo3' && <label><span className="label">Silver SELL logic</span><select value={silverSellPlan} onChange={(e) => setSilverSellPlan(e.target.value)} className="control mt-1"><option value="red_chain">{SILVER_SELL_PLANS.red_chain.label}</option><option value="latest_reference">{SILVER_SELL_PLANS.latest_reference.label}</option></select><span className="mt-1 block text-[11px] leading-4 text-gray-500">{SILVER_SELL_PLANS[silverSellPlan as keyof typeof SILVER_SELL_PLANS].description}</span></label>}
          <label><span className="label">Start date</span><input value={startDate} onChange={(e) => setStartDate(e.target.value)} max={today} type="date" className="control mt-1" /></label>
          <label><span className="label">End date</span><input value={endDate} onChange={(e) => setEndDate(e.target.value)} max={today} type="date" className="control mt-1" /></label>
          <div className="flex items-end">
            {active ? (
              <button onClick={cancel} disabled={job?.status === 'cancelling'} className="min-h-10 w-full rounded border border-[#ef4444] bg-[#ef4444]/10 px-4 py-2 text-sm font-semibold text-[#ef4444] disabled:cursor-wait disabled:opacity-60">
                <i className="ri-stop-circle-fill mr-2" />{job?.status === 'cancelling' ? 'Cancelling...' : 'Cancel backtest'}
              </button>
            ) : (
              <button onClick={run} className="min-h-10 w-full rounded border border-[#3b82f6] bg-[#3b82f6] px-4 py-2 text-sm font-semibold text-white">
                <i className="ri-play-circle-fill mr-2" />Run range backtest
              </button>
            )}
          </div>
        </div>
        <p className="mt-3 text-xs text-[#f59e0b]"><i className="ri-error-warning-fill mr-1" />{introCopy.note}</p>
      </div>

      {error && <p className="rounded border border-[#ef4444]/40 bg-[#ef4444]/10 px-3 py-2 text-sm text-[#ef4444]">{error}</p>}
      {job && !result && <section className="panel p-4"><div className="flex justify-between gap-3 text-sm text-gray-200"><span>{job.message}</span><span className="num">{progressCompleted} / {progressTotal}</span></div><div className="mt-3 h-2 overflow-hidden rounded bg-[#020617]"><div className="h-full bg-[#3b82f6] transition-[width] duration-500" style={{ width: `${progress}%` }} /></div><p className="mt-2 text-xs text-gray-500">{progress}% complete. {replaying ? `${job.replay_failed || 0} selected signals could not be replayed.` : `${job.failed_symbols || 0} symbols returned no usable history.`}</p>{replaying && <ReplayMonitor activity={job.replay_activity || []} />}</section>}
      {job?.status === 'failed' && <p className="rounded border border-[#ef4444]/40 bg-[#ef4444]/10 px-3 py-2 text-sm text-[#ef4444]">{job.error || job.message}</p>}
      {job?.status === 'cancelled' && <p className="rounded border border-[#f59e0b]/40 bg-[#f59e0b]/10 px-3 py-2 text-sm text-[#f59e0b]">Backtest cancelled. You can start a new range now.</p>}
      {result && <BacktestResult result={result} />}
    </section>
  );
}

function ReplayMonitor({ activity }: { activity: any[] }) {
  return <div className="mt-4 border-t border-[#1f2937] pt-3">
    <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-gray-400"><i className="ri-radar-fill animate-pulse text-[#3b82f6]" />Live replay monitor <span className="normal-case font-normal text-gray-600">Latest 8 completed simulations</span></div>
    {!activity.length ? <p className="mt-2 text-xs text-gray-500">Preparing the first selected signal for replay...</p> : <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">{[...activity].reverse().map((event, index) => {
      const positive = Number(event.net_pnl) > 0;
      const negative = Number(event.net_pnl) < 0;
      const noEntry = event.status === 'NO_ENTRY_CANDLE';
      return <div key={`${event.date}-${event.symbol}-${index}`} className="border border-[#1f2937] bg-[#0d1117] px-2 py-2 text-xs">
        <div className="flex items-center justify-between gap-2"><span className="truncate font-mono text-gray-100">{event.symbol}</span><span className={event.side === 'BUY' ? 'text-[#22c55e]' : 'text-[#ef4444]'}>{event.side}</span></div>
        <div className="mt-1 flex items-center justify-between gap-2 text-gray-500"><span>{event.date}</span><span className={noEntry ? 'text-[#f59e0b]' : event.status === 'TARGET' ? 'text-[#22c55e]' : event.status === 'SL' ? 'text-[#ef4444]' : 'text-gray-300'}>{noEntry ? 'No entry candle' : event.status}</span></div>
        {!noEntry && <div className={`num mt-1 ${positive ? 'text-[#22c55e]' : negative ? 'text-[#ef4444]' : 'text-gray-300'}`}>Net {money(event.net_pnl)}</div>}
      </div>;
    })}</div>}
  </div>;
}

function BacktestResult({ result }: { result: any }) {
  const summary = result.summary || {};
  const coverage = result.data_coverage || {};
  const exits = summary.exit_counts || {};
  const daily = useMemo(() => Array.isArray(result.daily_results) ? result.daily_results : [], [result.daily_results]);
  const allTrades = useMemo(
    () => daily.flatMap((day: any) => (day.trades || []).map((trade: any) => ({ ...trade, session_date: day.date }))),
    [daily],
  );
  const silverChartDays = useMemo(
    () => result.algo_id === 'algo3'
      ? daily.filter((day: any) => Array.isArray(day?.chart?.candles) && day.chart.candles.length > 0)
      : [],
    [daily, result.algo_id],
  );
  const [selectedChartDate, setSelectedChartDate] = useState('');
  const [selectedTradeId, setSelectedTradeId] = useState<string | null>(null);
  const [chartOverlays, setChartOverlays] = useState({
    ema: true,
    setups: false,
    trades: true,
    levels: true,
    trailing: true,
  });
  const [uiStorageReady, setUiStorageReady] = useState(false);
  const sectorBreakdown = Array.isArray(result.sector_breakdown) ? result.sector_breakdown : [];
  const visibleTrades = result.algo_id === 'algo3' && selectedChartDate
    ? allTrades.filter((trade: any) => trade.session_date === selectedChartDate)
    : allTrades;
  const resultStorageId = `${result.algo_id}:${result.start_date}:${result.end_date}:${result.silver_buy_plan || ''}:${result.silver_sell_plan || ''}`;

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(BACKTEST_UI_STORAGE_KEY);
      if (!raw) {
        setUiStorageReady(true);
        return;
      }
      const snapshot = JSON.parse(raw);
      if (snapshot?.resultStorageId === resultStorageId) {
        if (typeof snapshot.selectedChartDate === 'string') setSelectedChartDate(snapshot.selectedChartDate);
        if (typeof snapshot.selectedTradeId === 'string' || snapshot.selectedTradeId === null) setSelectedTradeId(snapshot.selectedTradeId);
        if (snapshot.chartOverlays) setChartOverlays({
          ema: snapshot.chartOverlays.ema !== false,
          setups: false,
          trades: snapshot.chartOverlays.trades !== false,
          levels: snapshot.chartOverlays.levels !== false,
          trailing: snapshot.chartOverlays.trailing !== false,
        });
      }
    } catch {
      // Ignore corrupted chart UI snapshots.
    } finally {
      setUiStorageReady(true);
    }
  }, [resultStorageId]);

  useEffect(() => {
    if (!silverChartDays.length) {
      setSelectedChartDate('');
      return;
    }
    const defaultDay = [...silverChartDays].reverse().find((day: any) => (day.trades || []).length > 0) || silverChartDays[silverChartDays.length - 1];
    setSelectedChartDate((current) => silverChartDays.some((day: any) => day.date === current) ? current : defaultDay.date);
  }, [silverChartDays]);

  useEffect(() => {
    const selectedDay = silverChartDays.find((day: any) => day.date === selectedChartDate) || null;
    const trades = Array.isArray(selectedDay?.chart?.trades) ? selectedDay.chart.trades : [];
    setSelectedTradeId((current) => trades.some((trade: any) => trade.trade_id === current) ? current : null);
  }, [selectedChartDate, silverChartDays]);

  useEffect(() => {
    if (!uiStorageReady) return;
    try {
      window.localStorage.setItem(BACKTEST_UI_STORAGE_KEY, JSON.stringify({
        resultStorageId,
        selectedChartDate,
        selectedTradeId,
        chartOverlays: { ...chartOverlays, setups: false },
        savedAt: Date.now(),
      }));
    } catch {
      // Best-effort persistence only.
    }
  }, [uiStorageReady, resultStorageId, selectedChartDate, selectedTradeId, chartOverlays]);

  function focusTrade(trade: any) {
    setSelectedChartDate(trade.session_date);
    setSelectedTradeId(trade.trade_id || null);
  }

  return <>
    <section className="panel p-4">
       <div className="flex flex-wrap items-start justify-between gap-3"><div><h3 className="text-sm font-semibold text-gray-100">{result.start_date} to {result.end_date}</h3><p className="mt-1 max-w-3xl text-xs text-gray-500">{result.execution_assumption}</p>{result.algo_id === 'algo3' && <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-[#bfdbfe]"><span className="rounded border border-[#3b82f6]/40 bg-[#3b82f6]/10 px-2 py-1"><span className="font-semibold">BUY:</span> {result.silver_buy_plan_label || result.silver_buy_plan || '15m breakout (current)'}</span><span className="rounded border border-[#3b82f6]/40 bg-[#3b82f6]/10 px-2 py-1"><span className="font-semibold">SELL:</span> {result.silver_sell_plan_label || result.silver_sell_plan || 'Red-chain comparison (current)'}</span></div>}</div><div className="flex items-center gap-3"><div className="text-xs text-gray-500">History coverage: <span className="num text-gray-100">{coverage.symbols_with_history} / {coverage.requested_symbols}</span></div><button onClick={() => downloadBacktestCsv(result)} className="inline-flex min-h-10 items-center gap-2 rounded border border-[#22c55e] bg-[#22c55e]/10 px-3 py-2 text-xs font-semibold text-[#22c55e]"><i className="ri-file-download-fill text-sm" />Download CSV</button></div></div>
      <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <Card label="Trading days" value={summary.trading_days_replayed || 0} />
        <Card label="Trades" value={summary.trade_count || 0} />
        <Card label="Wins / losses" value={`${summary.win_count || 0} / ${summary.loss_count || 0}`} />
        <Card label="Win rate" value={`${number(summary.win_rate_pct)}%`} tone={Number(summary.win_rate_pct) >= 50 ? 1 : -1} />
        <Card label="Profit factor" value={summary.profit_factor ?? '-'} tone={Number(summary.profit_factor) >= 1 ? 1 : -1} />
        <Card label="Net P&L" value={money(summary.net_pnl)} tone={Number(summary.net_pnl)} />
      </div>
    </section>
    {sectorBreakdown.length > 0 && <details className="group panel">
      <summary className="flex min-h-12 cursor-pointer list-none items-center justify-between gap-3 p-4">
        <div><h3 className="text-sm font-semibold text-gray-100">Sector Breakdown</h3><p className="mt-1 text-xs text-gray-500">Shows how the replayed universe clustered by sector.</p></div>
        <i className="ri-arrow-down-s-fill text-base text-[#60a5fa] transition-transform group-open:rotate-180" />
      </summary>
      <div className="border-t border-[#1f2937] p-4">
        <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        {sectorBreakdown.map((sector: any) => (
          <div key={sector.sector} className="rounded border border-[#1f2937] bg-[#0d1117] p-3">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="font-semibold text-gray-100">{sector.sector}</div>
                <div className="text-[11px] text-gray-500">{sector.direction} sector · {sector.rows} symbols</div>
              </div>
            </div>
            <div className="mt-2 h-1.5 overflow-hidden rounded-sm bg-[#020617]">
              <div className="h-full bg-[#22c55e]" style={{ width: `${Math.max(4, Math.min(100, Number(sector.alignment_strength || 0) * 100))}%` }} />
            </div>
            <div className="mt-1 text-[11px] text-gray-500">
              {sector.buy} BUY · {sector.sell} SELL · {sector.selected} selected · avg move {number(sector.avg_move_pct)}%
            </div>
          </div>
        ))}
        </div>
      </div>
    </details>}
    <section className="grid gap-4 lg:grid-cols-2">
      <div className="panel p-4"><h3 className="text-sm font-semibold text-gray-100">Trade Quality</h3><div className="mt-3 grid grid-cols-2 gap-2 text-sm"><Metric label="Gross profit" value={money(summary.gross_profit)} positive /><Metric label="Gross loss" value={money(-Number(summary.gross_loss || 0))} negative /><Metric label="Average win" value={money(summary.average_win)} positive /><Metric label="Average loss" value={money(summary.average_loss)} negative /><Metric label="Average net / trade" value={money(summary.average_net_per_trade)} tone={Number(summary.average_net_per_trade)} /><Metric label="Max drawdown" value={money(summary.max_drawdown)} negative /></div></div>
      <div className="panel p-4"><h3 className="text-sm font-semibold text-gray-100">Execution And Range</h3><div className="mt-3 grid grid-cols-2 gap-2 text-sm"><Metric label="Gross P&L" value={money(summary.gross_pnl)} tone={Number(summary.gross_pnl)} /><Metric label="Charges" value={money(summary.total_charges)} /><Metric label="Capital deployed" value={money(summary.capital_deployed)} /><Metric label="Net return / deployed" value={`${number(summary.net_return_on_deployed_pct)}%`} tone={Number(summary.net_return_on_deployed_pct)} /><Metric label="Best day" value={result.best_day ? `${result.best_day.date}: ${money(result.best_day.net_pnl)}` : '-'} tone={Number(result.best_day?.net_pnl)} /><Metric label="Worst day" value={result.worst_day ? `${result.worst_day.date}: ${money(result.worst_day.net_pnl)}` : '-'} tone={Number(result.worst_day?.net_pnl)} /></div><p className="mt-3 text-xs text-gray-500">Exits: Target {exits.TARGET || 0}, SL {exits.SL || 0}, EOD {exits.EOD_SQUAREOFF || 0}.</p></div>
    </section>
    {silverChartDays.length > 0 ? (
      <section className="grid items-start gap-4 xl:grid-cols-[minmax(0,1.24fr)_minmax(400px,0.96fr)]">
        <SilverBacktestChart
          days={silverChartDays}
          selectedDate={selectedChartDate}
          onSelectedDateChange={setSelectedChartDate}
          selectedTradeId={selectedTradeId}
          onSelectedTradeIdChange={setSelectedTradeId}
          overlays={chartOverlays}
        />
        <BacktestTrades
          rows={visibleTrades}
          selectedTradeId={selectedTradeId}
          onViewChart={focusTrade}
          selectedDate={result.algo_id === 'algo3' ? selectedChartDate : null}
          isSilver={result.algo_id === 'algo3'}
          compact
        />
      </section>
    ) : (
      <BacktestTrades
        rows={visibleTrades}
        selectedTradeId={selectedTradeId}
        onViewChart={focusTrade}
        selectedDate={result.algo_id === 'algo3' ? selectedChartDate : null}
        isSilver={result.algo_id === 'algo3'}
      />
    )}
    <DailyResults rows={daily} />
  </>;
}

function DailyResults({ rows }: { rows: any[] }) {
  const [page, setPage] = useState(0);
  const safePage = Math.min(page, Math.max(0, Math.ceil(rows.length / PAGE_SIZE) - 1));
  const visibleRows = rows.slice(safePage * PAGE_SIZE, safePage * PAGE_SIZE + PAGE_SIZE);
  const isSilver = rows[0]?.algo_id === 'algo3';

  if (isSilver) {
    return (
      <section className="panel overflow-hidden">
        <div className="border-b border-[#1f2937] p-4">
          <h3 className="text-sm font-semibold text-gray-100">Daily Results</h3>
          <p className="mt-1 text-xs text-gray-500">Silver rows now show actual replay depth per day: available 1-minute history, built 15-minute bars, captured setups, and executed entries.</p>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[1040px] text-xs">
            <thead className="bg-[#111827]">
              <tr>
                {['Date', '1m bars', '15m bars', 'Setups', 'Entries', 'Trades', 'Wins / Losses', 'Net', 'Status'].map((name) => (
                  <th key={name} className="table-cell label">{name}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {visibleRows.map((day: any, index: number) => {
                const summary = day.summary || {};
                const steps = Array.isArray(day.condition_breakdown) ? day.condition_breakdown : [];
                const barsProcessed = steps.find((step: any) => step.label === '15m bars processed');
                const setupsCaptured = steps.find((step: any) => step.label === 'Setups captured (green above / red below EMA20)');
                const entriesExecuted = steps.find((step: any) => step.label === 'Final: entries executed');
                const minuteBars = Number(barsProcessed?.total || 0);
                const fifteenMinuteBars = Number(barsProcessed?.passed || 0);
                const setups = Number(setupsCaptured?.passed || 0);
                const entries = Number(entriesExecuted?.passed || 0);
                const trades = Number(summary.trade_count || 0);
                const status = minuteBars <= 0
                  ? { label: 'No intraday history', tone: 'text-[#f59e0b]' }
                  : trades > 0
                    ? { label: 'Trades replayed', tone: 'text-[#22c55e]' }
                    : setups > 0
                      ? { label: 'Setups seen, no entry', tone: 'text-[#93c5fd]' }
                      : { label: 'Bars replayed, no setup', tone: 'text-gray-400' };
                return (
                  <tr key={day.date} className={index % 2 ? 'bg-[#0d1117]' : 'bg-[#111827]'}>
                    <td className="table-cell num text-gray-100">{day.date}</td>
                    <td className="table-cell num">{minuteBars}</td>
                    <td className="table-cell num">{fifteenMinuteBars}</td>
                    <td className="table-cell num">{setups}</td>
                    <td className="table-cell num">{entries}</td>
                    <td className="table-cell num">{trades}</td>
                    <td className="table-cell num">{summary.win_count || 0} / {summary.loss_count || 0}</td>
                    <td className={`table-cell num font-semibold ${tone(summary.net_pnl)}`}>{money(summary.net_pnl)}</td>
                    <td className={`table-cell text-xs font-medium ${status.tone}`}>{status.label}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <div className="px-4 pb-4">
          <PaginationControls page={safePage} totalRows={rows.length} onPageChange={setPage} />
        </div>
      </section>
    );
  }

  return <section className="panel overflow-hidden"><div className="border-b border-[#1f2937] p-4"><h3 className="text-sm font-semibold text-gray-100">Daily Results</h3></div><div className="overflow-x-auto"><table className="w-full min-w-[900px] text-xs"><thead className="bg-[#111827]"><tr>{['Date', 'Data coverage', 'Trades', 'Wins / Losses', 'Win rate', 'Gross', 'Charges', 'Net', 'Selected'].map((name) => <th key={name} className="table-cell label">{name}</th>)}</tr></thead><tbody>{visibleRows.map((day: any, index: number) => { const s = day.summary || {}; const selected = (day.condition_breakdown || []).find((step: any) => step.label === 'Final: selected for trade'); return <tr key={day.date} className={index % 2 ? 'bg-[#0d1117]' : 'bg-[#111827]'}><td className="table-cell num text-gray-100">{day.date}</td><td className="table-cell num">{day.data_available_symbols}</td><td className="table-cell num">{s.trade_count || 0}</td><td className="table-cell num">{s.win_count || 0} / {s.loss_count || 0}</td><td className="table-cell num">{number(s.win_rate_pct)}%</td><td className={`table-cell num ${tone(s.gross_pnl)}`}>{money(s.gross_pnl)}</td><td className="table-cell num">{money(s.total_charges)}</td><td className={`table-cell num font-semibold ${tone(s.net_pnl)}`}>{money(s.net_pnl)}</td><td className="table-cell num">{selected?.passed || 0}</td></tr>; })}</tbody></table></div><div className="px-4 pb-4"><PaginationControls page={safePage} totalRows={rows.length} onPageChange={setPage} /></div></section>;
}

function BacktestCandidates({ days }: { days: any[] }) {
  const [selectedDate, setSelectedDate] = useState('');
  const [query, setQuery] = useState('');
  const [showMissing, setShowMissing] = useState(false);
  const [sortKey, setSortKey] = useState('symbol');
  const [sortDirection, setSortDirection] = useState<'asc' | 'desc'>('asc');
  const availableDates = useMemo(() => days.map((day) => day.date), [days]);

  useEffect(() => {
    if (!availableDates.length) {
      setSelectedDate('');
      return;
    }
    setSelectedDate((current) => availableDates.includes(current) ? current : availableDates[0]);
  }, [availableDates]);

  const day = days.find((item) => item.date === selectedDate) || days[0];
  const candidates = (day?.candidates || [])
    .filter((row: any) => showMissing || row.has_opening_candle)
    .filter((row: any) => row.symbol?.toLowerCase().includes(query.toLowerCase()))
    .sort((left: any, right: any) => compareCandidates(left, right, sortKey, sortDirection));

  const columns: [string, string][] = [
    ['symbol', 'Symbol'],
    ['sector', 'Sector'],
    ['side', 'Side'],
    ['open', 'Open'],
    ['high', 'High'],
    ['low', 'Low'],
    ['close', 'Close'],
    ['volume', 'Volume'],
    ['prev_close', 'Prev Close'],
    ['gap_pct', 'Gap %'],
    ['shape_passed', 'Shape'],
    ['gap_passed', 'Gap'],
    ['filters_passed', 'Filters'],
    ['selected_for_trade', 'Selected'],
    ['rejection_reason', 'Reason'],
    ['vwap', 'VWAP'],
    ['rsi', 'RSI'],
    ['adx', 'ADX'],
  ];

  function toggleSort(key: string) {
    if (key === sortKey) setSortDirection((direction) => direction === 'asc' ? 'desc' : 'asc');
    else {
      setSortKey(key);
      setSortDirection('asc');
    }
  }

  return (
    <section className="panel overflow-hidden">
      <div className="flex flex-col gap-3 border-b border-[#1f2937] p-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h3 className="text-sm font-semibold text-gray-100">9:15 Candle Filtered List</h3>
          <p className="mt-1 text-xs text-gray-500">{candidates.length} visible symbols. Missing 9:15 data is hidden by default, but remains available for audit.</p>
        </div>
        <div className="grid gap-2 sm:grid-cols-2">
          <select value={selectedDate} onChange={(e) => setSelectedDate(e.target.value)} className="control text-sm">
            {days.map((item) => <option key={item.date} value={item.date}>{item.date}</option>)}
          </select>
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Filter symbols..." className="control text-sm" />
          <label className="flex min-h-10 items-center gap-2 text-xs text-gray-400 sm:col-span-2">
            <input type="checkbox" checked={showMissing} onChange={(e) => setShowMissing(e.target.checked)} /> Show missing 9:15 candle data
          </label>
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[1450px] text-xs">
          <thead className="bg-[#111827]">
            <tr>
              {columns.map(([key, name]) => (
                <th key={key} className="table-cell label">
                  <button onClick={() => toggleSort(key)} className="inline-flex items-center gap-1 whitespace-nowrap text-left hover:text-[#3b82f6]">
                    {name}
                    <span className={sortKey === key ? 'text-[#3b82f6]' : 'text-gray-600'}>{sortKey === key ? (sortDirection === 'asc' ? '?' : '?') : '?'}</span>
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {!candidates.length ? (
              <tr>
                <td colSpan={18} className="table-cell text-gray-500">No rows to show. This date may be a market holiday, or enable missing-candle data for an audit view.</td>
              </tr>
            ) : (
              candidates.map((row: any, index: number) => (
                <tr key={row.symbol} className={`${index % 2 ? 'bg-[#0d1117]' : 'bg-[#111827]'} ${row.selected_for_trade ? 'border-l-2 border-l-[#22c55e]' : row.filters_passed ? 'border-l-2 border-l-[#f59e0b]' : 'border-l-2 border-l-[#ef4444]'}`}>
                  <td className="table-cell font-mono text-gray-100">{row.symbol}</td>
                  <td className="table-cell text-gray-400">{row.sector || '-'}</td>
                  <td className={row.side === 'BUY' ? 'table-cell text-[#22c55e]' : row.side === 'SELL' ? 'table-cell text-[#ef4444]' : 'table-cell text-gray-500'}>{row.side || 'WATCH'}</td>
                  <td className="table-cell num">{optionalNumber(row.open)}</td>
                  <td className="table-cell num">{optionalNumber(row.high)}</td>
                  <td className="table-cell num">{optionalNumber(row.low)}</td>
                  <td className="table-cell num">{optionalNumber(row.close)}</td>
                  <td className="table-cell num">{optionalNumber(row.volume)}</td>
                  <td className="table-cell num">{optionalNumber(row.prev_close)}</td>
                  <td className={`table-cell num ${Number(row.gap_pct) > 0 ? 'text-[#22c55e]' : Number(row.gap_pct) < 0 ? 'text-[#ef4444]' : ''}`}>{optionalNumber(row.gap_pct)}%</td>
                  <td className="table-cell">{flag(row.shape_passed)}</td>
                  <td className="table-cell">{flag(row.gap_passed)}</td>
                  <td className="table-cell">{flag(row.filters_passed)}</td>
                  <td className="table-cell">{flag(row.selected_for_trade)}</td>
                  <td className="table-cell text-gray-400">{row.rejection_reason || '--'}</td>
                  <td className="table-cell num">{indicatorValue(row, 'vwap')}</td>
                  <td className="table-cell num">{indicatorValue(row, 'rsi')}</td>
                  <td className="table-cell num">{indicatorValue(row, 'adx')}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
function BacktestTrades({
  rows,
  selectedTradeId,
  onViewChart,
  selectedDate,
  isSilver = false,
  compact = false,
}: {
  rows: any[];
  selectedTradeId: string | null;
  onViewChart: (trade: any) => void;
  selectedDate: string | null;
  isSilver?: boolean;
  compact?: boolean;
}) {
  const [selectedTrade, setSelectedTrade] = useState<any | null>(null);
  const [selectedDiagnosticTrade, setSelectedDiagnosticTrade] = useState<any | null>(null);
  const headers = isSilver
    ? ['Side', 'Qty', 'Entry Time', 'Entry', 'Exit Time', 'Exit', 'Initial SL', 'Final SL', 'Target', 'Trailing SL', 'Chart', 'Why loss?', 'Reason', 'Net']
    : ['Date', 'Symbol', 'Side', 'Qty', 'Entry Time', 'Entry', 'Exit Time', 'Exit', 'Initial SL', 'Final SL', 'Target', 'Trailing SL', 'Chart', 'Why loss?', 'Reason', 'Net'];

  return <>
    <section className="panel overflow-hidden">
      <div className="border-b border-[#1f2937] p-4">
        <h3 className="text-sm font-semibold text-gray-100">Simulated Trades</h3>
        <p className="mt-1 text-xs text-gray-500">
          {selectedDate
            ? `Showing only trades for ${selectedDate}. Times are based on the historical one-minute candle used for simulated entry and exit.`
            : 'Times are based on the historical one-minute candle used for simulated entry and exit.'}
        </p>
      </div>
      <div className={compact ? 'max-h-[920px] overflow-auto' : 'overflow-x-auto'}>
        <table className={`w-full ${isSilver ? 'min-w-[1480px]' : 'min-w-[1880px]'} text-xs`}>
          <thead className="bg-[#111827]">
            <tr>{headers.map((name) => <th key={name} className="table-cell label">{name}</th>)}</tr>
          </thead>
          <tbody>
            {!rows.length ? <tr><td colSpan={headers.length} className="table-cell text-gray-500">No simulated trades in this range.</td></tr> : rows.map((trade, index) => <tr key={trade.trade_id || `${trade.session_date}-${trade.symbol}-${index}`} className={`${index % 2 ? 'bg-[#0d1117]' : 'bg-[#111827]'} ${trade.trade_id === selectedTradeId ? 'outline outline-1 outline-[#3b82f6]/60' : ''}`}>
              {!isSilver && <><td className="table-cell num">{trade.session_date}</td><td className="table-cell font-mono text-gray-100">{trade.symbol}</td></>}
              <td className={`table-cell font-semibold ${trade.side === 'BUY' ? 'text-[#22c55e]' : 'text-[#ef4444]'}`}>{trade.side}</td>
              <td className="table-cell num">{trade.qty}</td>
              <td className="table-cell num">{formatBacktestTime(trade.entry_time, trade.exit_time)}</td>
              <td className="table-cell num">{number(trade.entry_price)}</td>
              <td className="table-cell num">{formatBacktestTime(trade.exit_time, trade.entry_time)}</td>
              <td className="table-cell num">{number(trade.exit_price)}</td>
              <td className="table-cell num">{optionalNumber(trade.initial_sl_price)}</td>
              <td className="table-cell num">{optionalNumber(trade.sl_price)}</td>
              <td className="table-cell num">{optionalNumber(trade.target_price)}</td>
              <td className="table-cell">
                <div className="flex min-w-[170px] items-center gap-2">
                  <span className={trade.trailing_sl_enabled ? (trade.trailing_sl_active ? 'text-[#22c55e]' : 'text-[#f59e0b]') : 'text-gray-500'}>
                    {backtestTrailingLabel(trade)}
                  </span>
                  {Number(trade.trailing_move_count || 0) > 0 && (
                    <button
                      onClick={() => setSelectedTrade(trade)}
                      className="rounded border border-[#3b82f6]/40 bg-[#3b82f6]/10 px-2 py-1 text-[11px] font-semibold text-[#93c5fd]"
                    >
                      View {trade.trailing_move_count}
                    </button>
                  )}
                </div>
              </td>
              <td className="table-cell">
                <div className="flex min-w-[120px] items-center gap-2">
                  <button
                    onClick={() => onViewChart(trade)}
                    className="rounded border border-[#60a5fa]/40 bg-[#1d4ed8]/10 px-2 py-1 text-[11px] font-semibold text-[#bfdbfe]"
                  >
                    View chart
                  </button>
                </div>
              </td>
              <td className="table-cell">
                <div className="flex min-w-[220px] items-center gap-2">
                  <span className={`font-semibold ${diagnosticTone(trade)}`}>{backtestCauseLabel(trade)}</span>
                  {trade?.diagnostics && (
                    <button
                      onClick={() => setSelectedDiagnosticTrade(trade)}
                      className="rounded border border-[#a78bfa]/40 bg-[#8b5cf6]/10 px-2 py-1 text-[11px] font-semibold text-[#c4b5fd]"
                    >
                      Why?
                    </button>
                  )}
                </div>
              </td>
              <td className="table-cell">{trade.exit_reason}</td>
              <td className={`table-cell num font-semibold ${tone(trade.net_pnl)}`}>{money(trade.net_pnl)}</td>
            </tr>)}
          </tbody>
        </table>
      </div>
    </section>
    {selectedTrade && <BacktestTrailModal trade={selectedTrade} onClose={() => setSelectedTrade(null)} />}
    {selectedDiagnosticTrade && <BacktestDiagnosticModal trade={selectedDiagnosticTrade} onClose={() => setSelectedDiagnosticTrade(null)} />}
  </>;
}

function BacktestTrailModal({ trade, onClose }: { trade: any; onClose: () => void }) {
  const moves = Array.isArray(trade.trailing_moves) ? trade.trailing_moves : [];
  return (
    <div className="fixed inset-0 z-50">
      <button
        type="button"
        aria-label="Close trailing SL history"
        onClick={onClose}
        className="absolute inset-0 bg-black/35"
      />
      <div className="absolute inset-y-3 right-3 flex w-full max-w-[520px] justify-end">
        <div className="flex h-full w-full flex-col overflow-hidden rounded-xl border border-[#1f2937] bg-[#0b1220] shadow-2xl">
          <div className="flex items-start justify-between gap-4 border-b border-[#1f2937] px-5 py-4">
            <div>
              <h3 className="text-xl font-semibold text-white">Backtest trailing SL history</h3>
              <p className="mt-1 text-sm text-gray-400">{trade.symbol} · {trade.side} · {trade.session_date}</p>
              <p className="mt-2 text-xs text-gray-500">This panel stays on the side so you can still inspect the chart and summary behind it.</p>
            </div>
            <button onClick={onClose} className="text-2xl leading-none text-gray-400 hover:text-white">×</button>
          </div>
          <div className="grid gap-3 border-b border-[#1f2937] px-5 py-4 sm:grid-cols-2">
            <TrailStat label="Initial SL" value={optionalNumber(trade.initial_sl_price)} />
            <TrailStat label="Final SL" value={optionalNumber(trade.sl_price)} />
            <TrailStat label="Trigger" value={`${number(trade.trailing_trigger_points || 0)} pts`} />
            <TrailStat label="Distance" value={`${number(trade.trailing_distance_points || 0)} pts`} />
            <TrailStat label="Protected" value={`${number(trade.max_protected_points || 0)} pts`} />
          </div>
          <div className="min-h-0 flex-1 overflow-auto px-5 py-4">
            {!moves.length ? (
              <p className="text-sm text-gray-500">No per-step trailing moves were recorded for this backtest trade.</p>
            ) : (
              <table className="w-full min-w-[540px] text-xs">
                <thead className="bg-[#111827]">
                  <tr>{['Time', 'Gain', 'Reference', 'Previous SL', 'New SL', 'Protected'].map((name) => <th key={name} className="table-cell label">{name}</th>)}</tr>
                </thead>
                <tbody>
                  {moves.map((move: any, index: number) => (
                    <tr key={`${trade.symbol}-${trade.session_date}-${index}`} className={index % 2 ? 'bg-[#0d1117]' : 'bg-[#111827]'}>
                      <td className="table-cell num">{formatTime(move.time)}</td>
                      <td className="table-cell num">{optionalNumber(move.gain_points)}</td>
                      <td className="table-cell num">{optionalNumber(move.reference_price)}</td>
                      <td className="table-cell num">{optionalNumber(move.previous_sl)}</td>
                      <td className="table-cell num text-[#22c55e]">{optionalNumber(move.new_sl)}</td>
                      <td className="table-cell num">{optionalNumber(move.protected_points)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function TrailStat({ label, value }: { label: string; value: string }) {
  return <div className="rounded border border-[#1f2937] bg-[#111827] p-3"><div className="label">{label}</div><div className="num mt-2 text-sm text-gray-100">{value}</div></div>;
}

function BacktestDiagnosticModal({ trade, onClose }: { trade: any; onClose: () => void }) {
  const diagnostics = trade?.diagnostics || {};
  const entry = diagnostics.entry_context || {};
  const exit = diagnostics.exit_context || {};
  const path = diagnostics.path_metrics || {};
  const warnings = Array.isArray(diagnostics.warning_messages) ? diagnostics.warning_messages : [];
  const warningCodes = Array.isArray(diagnostics.warning_codes) ? diagnostics.warning_codes : [];
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
      <div className="max-h-[88vh] w-full max-w-5xl overflow-hidden rounded-xl border border-[#1f2937] bg-[#0b1220] shadow-2xl">
        <div className="flex items-start justify-between gap-4 border-b border-[#1f2937] px-5 py-4">
          <div>
            <h3 className="text-xl font-semibold text-white">Backtest loss diagnostics</h3>
            <p className="mt-1 text-sm text-gray-400">{trade.symbol} · {trade.side} · {trade.session_date}</p>
          </div>
          <button onClick={onClose} className="text-2xl leading-none text-gray-400 hover:text-white">×</button>
        </div>

        <div className="border-b border-[#1f2937] px-5 py-4">
          <div className="flex flex-wrap items-center gap-3">
            <span className={`rounded border px-3 py-1 text-sm font-semibold ${diagnosticChipTone(trade)}`}>
              {diagnostics.primary_cause_label || 'No diagnosis'}
            </span>
            <span className="rounded border border-[#1f2937] bg-[#111827] px-3 py-1 text-xs text-gray-400">
              code: {diagnostics.primary_cause_code || '--'}
            </span>
          </div>
          <p className="mt-3 text-sm text-gray-300">{diagnostics.summary || 'No diagnostic summary was generated for this trade.'}</p>
        </div>

        <div className="max-h-[62vh] overflow-auto px-5 py-4">
          <div className="grid gap-4 xl:grid-cols-3">
            <section className="rounded border border-[#1f2937] bg-[#111827] p-4">
              <h4 className="text-sm font-semibold text-gray-100">Setup and entry facts</h4>
              <div className="mt-3 grid gap-3">
                <TrailStat label="Setup side" value={String(entry.setup_side || '--')} />
                <TrailStat label="Setup time" value={formatTimeWithDate(entry.setup_time)} />
                <TrailStat label="Setup close" value={optionalNumber(entry.setup_close)} />
                <TrailStat label="Trigger level" value={optionalNumber(entry.trigger_level)} />
                <TrailStat label="EMA20" value={optionalNumber(entry.ema20)} />
                <TrailStat label="Entry time" value={formatBacktestDateTime(entry.entry_time, exit.exit_time)} />
                <TrailStat label="Entry price" value={optionalNumber(entry.entry_price)} />
                <TrailStat label="Delay from setup" value={entry.delay_from_setup_minutes === null || entry.delay_from_setup_minutes === undefined ? '--' : `${number(entry.delay_from_setup_minutes)} min`} />
                <TrailStat label="Prev red reference" value={optionalNumber(entry.previous_red_reference_close)} />
                <TrailStat label="Current red close" value={optionalNumber(entry.current_qualifying_red_close)} />
              </div>
            </section>

            <section className="rounded border border-[#1f2937] bg-[#111827] p-4">
              <h4 className="text-sm font-semibold text-gray-100">Exit facts</h4>
              <div className="mt-3 grid gap-3">
                <TrailStat label="Exit reason" value={String(exit.exit_reason || '--')} />
                <TrailStat label="Exit time" value={formatBacktestDateTime(exit.exit_time, entry.entry_time)} />
                <TrailStat label="Exit price" value={optionalNumber(exit.exit_price)} />
                <TrailStat label="Initial SL" value={optionalNumber(exit.initial_sl)} />
                <TrailStat label="Final SL" value={optionalNumber(exit.final_sl)} />
                <TrailStat label="Target" value={optionalNumber(exit.target)} />
                <TrailStat label="Trailing enabled" value={yesNo(exit.trailing_enabled)} />
                <TrailStat label="Trailing active" value={yesNo(exit.trailing_active)} />
                <TrailStat label="Trailing moves" value={number(exit.trailing_move_count || 0)} />
              </div>
            </section>

            <section className="rounded border border-[#1f2937] bg-[#111827] p-4">
              <h4 className="text-sm font-semibold text-gray-100">Path metrics</h4>
              <div className="mt-3 grid gap-3">
                <TrailStat label="Max favorable" value={`${optionalNumber(path.max_favorable_excursion_points)} pts`} />
                <TrailStat label="Max adverse" value={`${optionalNumber(path.max_adverse_excursion_points)} pts`} />
                <TrailStat label="Peak unrealized" value={money(path.peak_unrealized_profit)} />
                <TrailStat label="Worst drawdown" value={money(path.worst_unrealized_drawdown)} />
                <TrailStat label="Giveback" value={money(path.profit_giveback_from_peak)} />
                <TrailStat label="Giveback pts" value={`${optionalNumber(path.profit_giveback_from_peak_points)} pts`} />
                <TrailStat label="Gross P&L" value={money(trade.gross_pnl)} />
                <TrailStat label="Charges" value={money(trade.total_charges)} />
                <TrailStat label="Net P&L" value={money(trade.net_pnl)} />
              </div>
            </section>
          </div>

          <section className="mt-4 rounded border border-[#1f2937] bg-[#111827] p-4">
            <h4 className="text-sm font-semibold text-gray-100">Market warnings</h4>
            {!warnings.length ? (
              <p className="mt-3 text-sm text-gray-500">No extra market-behavior warnings were generated for this replay.</p>
            ) : (
              <>
                <div className="mt-3 flex flex-wrap gap-2">
                  {warningCodes.map((code: string) => (
                    <span key={code} className="rounded border border-[#334155] bg-[#0d1117] px-2.5 py-1 text-[11px] font-semibold text-[#fbbf24]">
                      {code}
                    </span>
                  ))}
                </div>
                <ul className="mt-3 space-y-2 text-sm text-gray-300">
                  {warnings.map((warning: string, index: number) => (
                    <li key={`${warning}-${index}`} className="rounded border border-[#1f2937] bg-[#0d1117] px-3 py-2">
                      {warning}
                    </li>
                  ))}
                </ul>
              </>
            )}
          </section>
        </div>
      </div>
    </div>
  );
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
function formatTimeWithDate(value: unknown) { if (!value) return '--'; const date = new Date(String(value)); return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }); }
function sameBacktestMinute(first: unknown, second: unknown) {
  if (!first || !second) return false;
  const firstDate = new Date(String(first));
  const secondDate = new Date(String(second));
  if (Number.isNaN(firstDate.getTime()) || Number.isNaN(secondDate.getTime())) return false;
  const parts = (date: Date) => date.toLocaleString('en-CA', { timeZone: 'Asia/Kolkata', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  return parts(firstDate) === parts(secondDate);
}
function formatBacktestTime(value: unknown, otherTime: unknown) {
  if (!value) return '--';
  if (!sameBacktestMinute(value, otherTime)) return formatTime(value);
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return String(value);
  return `${date.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hour12: false })} bar`;
}
function formatBacktestDateTime(value: unknown, otherTime: unknown) {
  const formatted = formatTimeWithDate(value);
  return sameBacktestMinute(value, otherTime) ? `${formatted} · same 1-minute bar` : formatted;
}
function yesNo(value: unknown) { return value ? 'Yes' : 'No'; }
function backtestCauseLabel(trade: any) { return trade?.diagnostics?.primary_cause_label || '--'; }
function diagnosticTone(trade: any) { return String(trade?.diagnostics?.primary_cause_code || '').includes('target') ? 'text-[#22c55e]' : Number(trade?.net_pnl) < 0 ? 'text-[#ef4444]' : 'text-[#f59e0b]'; }
function diagnosticChipTone(trade: any) { return String(trade?.diagnostics?.primary_cause_code || '').includes('target') ? 'border-[#22c55e]/40 bg-[#22c55e]/10 text-[#22c55e]' : Number(trade?.net_pnl) < 0 ? 'border-[#ef4444]/40 bg-[#ef4444]/10 text-[#ef4444]' : 'border-[#f59e0b]/40 bg-[#f59e0b]/10 text-[#f59e0b]'; }

function downloadBacktestCsv(result: any) {
  const headers = ['Record Type', 'Date', 'Symbol', 'Sector', 'Side', 'Open', 'High', 'Low', 'Close', 'Volume', 'Previous Close', 'Gap %', 'Shape Passed', 'Gap Passed', 'Filters Passed', 'Selected For Trade', 'Rejection Reason', 'VWAP', 'RSI', 'ADX', 'Quantity', 'Entry Time IST', 'Entry Price', 'Exit Time IST', 'Exit Price', 'Initial SL', 'Final SL', 'Target', 'Trailing Enabled', 'Trailing Active', 'Trailing Trigger Points', 'Trailing Distance Points', 'Trailing Move Count', 'Trailing Moves', 'Exit Reason', 'Primary Cause', 'Diagnostic Summary', 'Warning Codes', 'Gross P&L', 'Charges', 'Net P&L', 'Metric', 'Value'];
  const rows: any[][] = [];
  Object.entries(result.summary || {}).forEach(([metric, value]) => rows.push(['Summary', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', metric, typeof value === 'object' ? JSON.stringify(value) : value]));
  (result.daily_results || []).forEach((day: any) => {
    const summary = day.summary || {};
    rows.push(['Daily Result', day.date, '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', summary.gross_pnl, summary.total_charges, summary.net_pnl, 'Trades / wins / losses', `${summary.trade_count || 0} / ${summary.win_count || 0} / ${summary.loss_count || 0}`]);
    (day.trades || []).forEach((trade: any) => rows.push(['Trade', day.date, trade.symbol, trade.sector || '', trade.side, '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', trade.qty, formatTime(trade.entry_time), trade.entry_price, formatTime(trade.exit_time), trade.exit_price, trade.initial_sl_price, trade.sl_price, trade.target_price, trade.trailing_sl_enabled, trade.trailing_sl_active, trade.trailing_trigger_points, trade.trailing_distance_points, trade.trailing_move_count, JSON.stringify(trade.trailing_moves || []), trade.exit_reason, trade.diagnostics?.primary_cause_label || '', trade.diagnostics?.summary || '', JSON.stringify(trade.diagnostics?.warning_codes || []), trade.gross_pnl, trade.total_charges, trade.net_pnl, '', '']));
    (day.candidates || []).forEach((row: any) => rows.push(['Candidate', day.date, row.symbol, row.sector || '', row.side, row.open, row.high, row.low, row.close, row.volume, row.prev_close, row.gap_pct, row.shape_passed, row.gap_passed, row.filters_passed, row.selected_for_trade, row.rejection_reason, row.indicator_results?.vwap?.value, row.indicator_results?.rsi?.value, row.indicator_results?.adx?.value, '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '']));
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

function backtestTrailingLabel(trade: any) {
  if (!trade.trailing_sl_enabled) return 'OFF';
  if (!trade.trailing_sl_active) return `armed · ${number(trade.trailing_trigger_points || 0)} pts`;
  return `active @ ${number(trade.sl_price || 0)}`;
}
