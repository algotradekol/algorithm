'use client';
import { CSSProperties, useEffect, useState } from 'react';
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
    <div>
      <h3>History and Logs</h3>
      <p style={{ color: '#8a94a3', fontSize: 13, marginBottom: 16 }}>
        Daily P&amp;L, recent trade logs, and historical price candles in one place.
      </p>
      {error && <p style={{ color: '#ff6b6b', marginBottom: 12 }}>{error}</p>}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: 12, marginBottom: 20 }}>
        <label>
          <div style={{ fontSize: 12, color: '#8a94a3', marginBottom: 4 }}>Algo</div>
          <select value={algoId} onChange={(e) => setAlgoId(e.target.value)} style={inputStyle}>
            <option value="algo1">Algo 1</option>
            <option value="algo2">Algo 2</option>
          </select>
        </label>
        <label>
          <div style={{ fontSize: 12, color: '#8a94a3', marginBottom: 4 }}>Days</div>
          <input type="number" min={1} max={180} value={days} onChange={(e) => setDays(Number(e.target.value) || 30)} style={inputStyle} />
        </label>
        <label>
          <div style={{ fontSize: 12, color: '#8a94a3', marginBottom: 4 }}>Symbol</div>
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)} style={inputStyle}>
            {watchlist.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label>
          <div style={{ fontSize: 12, color: '#8a94a3', marginBottom: 4 }}>Resolution</div>
          <select value={resolution} onChange={(e) => setResolution(e.target.value)} style={inputStyle}>
            {RESOLUTIONS.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
      </div>

      <h4>Daily Performance</h4>
      <Table rows={dailyHistory} columns={['date', 'trade_count', 'gross_pnl', 'charges', 'net_pnl']} />

      <h4>Recent Trade Logs</h4>
      <Table rows={recentTrades} columns={['exit_time', 'symbol', 'side', 'qty', 'entry_price', 'exit_price', 'exit_reason', 'net_pnl']} />

      <h4>Historical Price Candles</h4>
      {marketError && <p style={{ color: '#ffb366', marginBottom: 12 }}>{marketError}</p>}
      <MiniChart candles={marketHistory} />
      <Table rows={marketHistory.slice(-20).reverse()} columns={['time', 'open', 'high', 'low', 'close', 'volume']} />
    </div>
  );
}

function MiniChart({ candles }: { candles: any[] }) {
  if (!candles.length) return <p style={{ color: '#8a94a3', fontSize: 13 }}>No candle history available yet.</p>;

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
    <div style={{ background: '#151b23', borderRadius: 10, padding: 12, marginBottom: 16 }}>
      <div style={{ color: '#8a94a3', fontSize: 12, marginBottom: 8 }}>
        Close range: Rs {min.toFixed(2)} to Rs {max.toFixed(2)}
      </div>
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" style={{ width: '100%', height: 180 }}>
        <polyline fill="none" stroke="#4ade80" strokeWidth="2" points={points} />
      </svg>
    </div>
  );
}

const inputStyle: CSSProperties = {
  width: '100%',
  padding: 8,
  borderRadius: 6,
  border: '1px solid #333',
  background: '#0b0f14',
  color: '#fff',
};
