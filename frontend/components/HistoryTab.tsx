'use client';
import { WheelEvent, useEffect, useMemo, useState } from 'react';
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
    <section className="space-y-4">
      {error && <p className="rounded border border-[#ef4444]/40 bg-[#ef4444]/10 px-3 py-2 text-sm text-[#ef4444]">{error}</p>}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <label>
          <div className="label mb-1">Algo</div>
          <select value={algoId} onChange={(e) => setAlgoId(e.target.value)} className="control">
            <option value="algo1">Algo 1</option>
            <option value="algo2">Algo 2</option>
            <option value="algo3">Algo 3</option>
            <option value="algo4">Algo 4</option>
          </select>
        </label>
        <label>
          <div className="label mb-1">Days</div>
          <input type="number" min={1} max={180} value={days} onChange={(e) => setDays(Number(e.target.value) || 30)} className="control num" />
        </label>
        <label>
          <div className="label mb-1">Symbol</div>
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)} className="control">
            {watchlist.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label>
          <div className="label mb-1">Resolution</div>
          <select value={resolution} onChange={(e) => setResolution(e.target.value)} className="control">
            {RESOLUTIONS.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
      </div>

      <section>
        <div className="mb-2 flex items-center justify-between gap-4">
          <h3 className="label">Historical Price Candles</h3>
          <div className="text-xs text-gray-500">Mouse wheel over chart to zoom in/out</div>
        </div>
        {marketError && <p className="mb-3 text-sm text-[#f59e0b]">{marketError}</p>}
        <ZoomableCandleChart candles={marketHistory} symbol={symbol} resolution={resolution} />
      </section>

      <div className="grid gap-4 xl:grid-cols-2">
        <section>
          <h3 className="label mb-2">Daily Performance</h3>
          <Table rows={dailyHistory} columns={['date', 'trade_count', 'gross_pnl', 'charges', 'net_pnl']} />
        </section>

        <section>
          <h3 className="label mb-2">Recent Trade Logs</h3>
          <Table rows={recentTrades} columns={['exit_time', 'symbol', 'side', 'qty', 'entry_price', 'exit_price', 'exit_reason', 'net_pnl']} />
        </section>
      </div>

      <section>
        <h3 className="label mb-2">Visible Candle Details</h3>
        <Table rows={marketHistory.slice(-30).reverse()} columns={['time', 'open', 'high', 'low', 'close', 'volume']} />
      </section>
    </section>
  );
}

function ZoomableCandleChart({ candles, symbol, resolution }: { candles: any[]; symbol: string; resolution: string }) {
  const [visibleCount, setVisibleCount] = useState(80);
  const [offsetFromEnd, setOffsetFromEnd] = useState(0);

  useEffect(() => {
    setVisibleCount(80);
    setOffsetFromEnd(0);
  }, [symbol, resolution, candles.length]);

  const normalized = useMemo(() => candles.map((candle) => ({
    ...candle,
    open: Number(candle.open),
    high: Number(candle.high),
    low: Number(candle.low),
    close: Number(candle.close),
    volume: Number(candle.volume || 0),
  })).filter((candle) => Number.isFinite(candle.close)), [candles]);

  if (!normalized.length) return <p className="rounded border border-[#1f2937] bg-[#111827] p-4 text-sm text-gray-500">No candle history available yet.</p>;

  const maxVisible = Math.max(10, normalized.length);
  const clampedVisible = Math.min(Math.max(visibleCount, 10), maxVisible);
  const maxOffset = Math.max(0, normalized.length - clampedVisible);
  const clampedOffset = Math.min(offsetFromEnd, maxOffset);
  const end = normalized.length - clampedOffset;
  const start = Math.max(0, end - clampedVisible);
  const visible = normalized.slice(start, end);

  const high = Math.max(...visible.map((candle) => candle.high));
  const low = Math.min(...visible.map((candle) => candle.low));
  const maxVolume = Math.max(...visible.map((candle) => candle.volume), 1);
  const priceSpan = high - low || 1;
  const width = 1200;
  const priceHeight = 330;
  const volumeHeight = 70;
  const totalHeight = priceHeight + volumeHeight + 34;
  const candleWidth = width / Math.max(visible.length, 1);

  function y(price: number) {
    return 16 + ((high - price) / priceSpan) * (priceHeight - 32);
  }

  function handleWheel(event: WheelEvent<HTMLDivElement>) {
    event.preventDefault();
    const zoomingIn = event.deltaY < 0;
    setVisibleCount((current) => {
      const step = Math.max(4, Math.round(current * 0.12));
      return Math.min(maxVisible, Math.max(10, zoomingIn ? current - step : current + step));
    });
  }

  const first = visible[0];
  const last = visible[visible.length - 1];
  const change = last.close - first.open;
  const changePct = first.open ? change / first.open * 100 : 0;

  return (
    <div className="rounded border border-[#1f2937] bg-[#111827] p-3">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="font-mono text-sm font-semibold text-gray-100">{symbol || 'Symbol'} / {resolution}</div>
          <div className="mt-1 text-xs text-gray-500">
            Showing candles {start + 1}-{end} of {normalized.length}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-4 text-xs">
          <Stat label="Open" value={formatNumber(first.open)} />
          <Stat label="High" value={formatNumber(high)} />
          <Stat label="Low" value={formatNumber(low)} />
          <Stat label="Close" value={formatNumber(last.close)} />
          <Stat label="Change" value={`${change >= 0 ? '+' : ''}${formatNumber(change)} (${changePct.toFixed(2)}%)`} tone={change >= 0 ? 'text-[#22c55e]' : 'text-[#ef4444]'} />
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => setVisibleCount((current) => Math.max(10, Math.round(current * 0.75)))} className="rounded border border-[#3b82f6] px-2 py-1 text-xs text-[#3b82f6]">Zoom In</button>
          <button onClick={() => setVisibleCount((current) => Math.min(maxVisible, Math.round(current * 1.35)))} className="rounded border border-[#3b82f6] px-2 py-1 text-xs text-[#3b82f6]">Zoom Out</button>
          <button onClick={() => { setVisibleCount(80); setOffsetFromEnd(0); }} className="rounded border border-[#1f2937] px-2 py-1 text-xs text-gray-500">Reset</button>
        </div>
      </div>

      <div onWheel={handleWheel} className="overflow-x-auto border border-[#1f2937] bg-[#0a0e14]">
        <svg viewBox={`0 0 ${width} ${totalHeight}`} className="h-[440px] min-w-[900px] w-full">
          {[0, 0.25, 0.5, 0.75, 1].map((ratio) => {
            const price = high - priceSpan * ratio;
            const lineY = y(price);
            return (
              <g key={ratio}>
                <line x1={0} x2={width} y1={lineY} y2={lineY} stroke="#1f2937" strokeWidth="1" />
                <text x={8} y={lineY - 4} fill="#6b7280" fontSize="11" fontFamily="ui-monospace">{formatNumber(price)}</text>
              </g>
            );
          })}

          {visible.map((candle, index) => {
            const x = index * candleWidth + candleWidth / 2;
            const openY = y(candle.open);
            const closeY = y(candle.close);
            const highY = y(candle.high);
            const lowY = y(candle.low);
            const bullish = candle.close >= candle.open;
            const color = bullish ? '#22c55e' : '#ef4444';
            const bodyTop = Math.min(openY, closeY);
            const bodyHeight = Math.max(1, Math.abs(closeY - openY));
            const bodyWidth = Math.max(2, candleWidth * 0.58);
            const volumeBarHeight = candle.volume / maxVolume * (volumeHeight - 10);
            const volumeY = priceHeight + 18 + (volumeHeight - volumeBarHeight);
            return (
              <g key={`${candle.time}-${index}`}>
                <title>{`${candle.time}\nO ${formatNumber(candle.open)} H ${formatNumber(candle.high)} L ${formatNumber(candle.low)} C ${formatNumber(candle.close)}\nVol ${candle.volume.toLocaleString('en-IN')}`}</title>
                <line x1={x} x2={x} y1={highY} y2={lowY} stroke={color} strokeWidth="1.2" />
                <rect x={x - bodyWidth / 2} y={bodyTop} width={bodyWidth} height={bodyHeight} fill={color} opacity={bullish ? 0.85 : 0.75} />
                <rect x={x - bodyWidth / 2} y={volumeY} width={bodyWidth} height={volumeBarHeight} fill={color} opacity="0.35" />
              </g>
            );
          })}

          <line x1={0} x2={width} y1={priceHeight + 18} y2={priceHeight + 18} stroke="#1f2937" />
          <text x={8} y={totalHeight - 8} fill="#6b7280" fontSize="11" fontFamily="ui-monospace">
            {`${first.time} -> ${last.time}`}
          </text>
        </svg>
      </div>
    </div>
  );
}

function Stat({ label, value, tone = 'text-gray-100' }: { label: string; value: string; tone?: string }) {
  return (
    <div>
      <div className="label">{label}</div>
      <div className={`num mt-1 ${tone}`}>{value}</div>
    </div>
  );
}

function formatNumber(value: number) {
  return value.toLocaleString('en-IN', { maximumFractionDigits: 2 });
}
