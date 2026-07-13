'use client';
import { useEffect, useState } from 'react';
import { api } from '../lib/api';
import { Table } from './AlgoTab';

const RESOLUTIONS = ['5', '15', '60', 'D'];

export default function HistoryTab() {
  const [algoId, setAlgoId] = useState('algo1');
  const [days, setDays] = useState(30);
  const [resolution, setResolution] = useState('15');
  const [watchlist, setWatchlist] = useState<string[]>([]);
  const [symbol, setSymbol] = useState('');
  const [dailyHistory, setDailyHistory] = useState<any[]>([]);
  const [marketHistory, setMarketHistory] = useState<any[]>([]);
  const [recentTrades, setRecentTrades] = useState<any[]>([]);
  const [error, setError] = useState('');
  const [marketError, setMarketError] = useState('');

  useEffect(() => {
    api.watchlist().then((result) => {
      const symbols = result.symbols || [];
      setWatchlist(symbols);
      setSymbol((current) => current || symbols[0] || '');
    }).catch((e: any) => {
      setError(e?.message || 'Failed to load watchlist');
      console.error(e);
    });
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      const [historyResult, tradesResult, marketResult] = await Promise.allSettled([
          api.history(algoId, days),
          api.trades(algoId),
          symbol ? api.marketHistory(symbol, Math.min(days, 30), resolution) : Promise.resolve({ candles: [] }),
      ]);
      if (cancelled) return;

      if (historyResult.status === 'fulfilled') {
        setDailyHistory(historyResult.value);
      }
      if (tradesResult.status === 'fulfilled') {
        setRecentTrades(tradesResult.value.slice(0, 25));
      }
      if (marketResult.status === 'fulfilled') {
        setMarketHistory(marketResult.value.candles || []);
        setMarketError(marketResult.value.warning || '');
      } else {
        setMarketHistory([]);
        setMarketError(marketResult.reason?.message || 'Historical price data is temporarily unavailable');
      }

      const primaryFailures = [historyResult, tradesResult]
        .filter((result) => result.status === 'rejected')
        .map((result) => (result as PromiseRejectedResult).reason?.message || 'Failed to load history');
      setError(primaryFailures[0] || '');
    }
    load();
    return () => { cancelled = true; };
  }, [algoId, days, resolution, symbol]);

  return (
    <section>
      <h2 className="text-xl font-semibold text-white">History and Logs</h2>
      <p className="mt-2 text-sm text-textSoft">
        Daily P&amp;L, recent trade logs, and historical price candles in one place.
      </p>
      {error && <p className="mt-3 text-sm text-danger">{error}</p>}

      <div className="my-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <label>
          <div className="mb-1 text-xs uppercase tracking-[0.1em] text-textSoft">Algo</div>
          <select value={algoId} onChange={(e) => setAlgoId(e.target.value)} className="control">
            <option value="algo1">Algo 1</option>
            <option value="algo2">Algo 2</option>
          </select>
        </label>
        <label>
          <div className="mb-1 text-xs uppercase tracking-[0.1em] text-textSoft">Days</div>
          <input type="number" min={1} max={180} value={days} onChange={(e) => setDays(Number(e.target.value) || 30)} className="control" />
        </label>
        <label>
          <div className="mb-1 text-xs uppercase tracking-[0.1em] text-textSoft">Symbol</div>
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)} className="control">
            {watchlist.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label>
          <div className="mb-1 text-xs uppercase tracking-[0.1em] text-textSoft">Resolution</div>
          <select value={resolution} onChange={(e) => setResolution(e.target.value)} className="control">
            {RESOLUTIONS.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
      </div>

      <h3 className="mb-3 mt-6 text-sm font-semibold uppercase tracking-[0.14em] text-textSoft">Daily Performance</h3>
      <Table rows={dailyHistory} columns={['date', 'trade_count', 'gross_pnl', 'charges', 'net_pnl']} />

      <h3 className="mb-3 mt-6 text-sm font-semibold uppercase tracking-[0.14em] text-textSoft">Recent Trade Logs</h3>
      <Table rows={recentTrades} columns={['exit_time', 'symbol', 'side', 'qty', 'entry_price', 'exit_price', 'exit_reason', 'net_pnl']} />

      <h3 className="mb-3 mt-6 text-sm font-semibold uppercase tracking-[0.14em] text-textSoft">Historical Price Candles</h3>
      {marketError && <p className="mb-3 text-sm text-warning">{marketError}</p>}
      <MiniChart candles={marketHistory} />
      <Table rows={marketHistory.slice(-20).reverse()} columns={['time', 'open', 'high', 'low', 'close', 'volume']} />
    </section>
  );
}

function MiniChart({ candles }: { candles: any[] }) {
  if (!candles.length) return <p className="text-sm text-textSoft">No candle history available yet.</p>;

  const closes = candles.map((candle) => Number(candle.close));
  const min = Math.min(...closes);
  const max = Math.max(...closes);
  const span = max - min || 1;
  const points = closes.map((close, index) => {
    const x = (index / Math.max(closes.length - 1, 1)) * 100;
    const y = 100 - ((close - min) / span) * 100;
    return `${x},${y}`;
  }).join(' ');

  return (
    <div className="panel mb-4 p-3">
      <div className="mb-2 text-xs text-textSoft">
        Close range: Rs {min.toFixed(2)} to Rs {max.toFixed(2)}
      </div>
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="h-44 w-full">
        <polyline fill="none" stroke="#43d17d" strokeWidth="2" points={points} />
      </svg>
    </div>
  );
}
