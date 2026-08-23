'use client';

import { useEffect, useMemo, useRef, useState } from 'react';

const CHART_COLORS = {
  ema: '#3b82f6',
  buy: '#22c55e',
  sell: '#ef4444',
  buyTrigger: '#14b8a6',
  sellTrigger: '#d946ef',
  selectedTrade: '#22d3ee',
  selectedCandle: '#f8fafc',
  tradePath: '#f0abfc',
  entryPrice: '#22d3ee',
  exit: '#facc15',
  initialSl: '#fb923c',
  effectiveSl: '#ec4899',
  target: '#8b5cf6',
  trailing: '#a3e635',
  crosshair: '#94a3b8',
};

type OverlayState = {
  ema: boolean;
  setups: boolean;
  trades: boolean;
  levels: boolean;
  trailing: boolean;
};

type Props = {
  days: any[];
  selectedDate: string;
  onSelectedDateChange: (value: string) => void;
  selectedTradeId: string | null;
  onSelectedTradeIdChange: (value: string | null) => void;
  overlays: OverlayState;
  expanded?: boolean;
};

export default function SilverBacktestChart({
  days,
  selectedDate,
  onSelectedDateChange,
  selectedTradeId,
  onSelectedTradeIdChange,
  overlays,
  expanded = false,
}: Props) {
  const selectedDay = days.find((day) => day.date === selectedDate) || days[0];
  const chart = selectedDay?.chart || {};
  const chartCandles = Array.isArray(chart.candles) ? chart.candles : [];
  const chartTrades = Array.isArray(chart.trades) ? chart.trades : [];
  const chartSetups = Array.isArray(chart.setups) ? chart.setups : [];
  const viewportHint = chart.viewport_hint || {};

  const normalized = useMemo(() => chartCandles.map((candle: any) => ({
    ...candle,
    open: Number(candle.open),
    high: Number(candle.high),
    low: Number(candle.low),
    close: Number(candle.close),
    volume: Number(candle.volume || 0),
    ema20: Number(candle.ema20),
    timeMs: parseMaybeDate(candle.time)?.getTime() ?? null,
  })).filter((candle: any) => Number.isFinite(candle.close)), [chartCandles]);

  const selectedTrade = chartTrades.find((trade: any) => trade.trade_id === selectedTradeId) || null;
  const [visibleCount, setVisibleCount] = useState(48);
  const [offsetFromEnd, setOffsetFromEnd] = useState(0);
  const [crosshair, setCrosshair] = useState<{ x: number; y: number } | null>(null);
  const [selectedCandleIndex, setSelectedCandleIndex] = useState<number | null>(null);
  const chartRef = useRef<HTMLDivElement | null>(null);
  const pinchRef = useRef<{ distance: number; ratio: number } | null>(null);
  const dragRef = useRef<{ x: number; offset: number } | null>(null);
  const [containerWidth, setContainerWidth] = useState(0);
  const [scrollLeft, setScrollLeft] = useState(0);
  const [fullDayFit, setFullDayFit] = useState(false);

  const maxVisible = Math.max(10, normalized.length);
  const clampedVisible = Math.min(Math.max(visibleCount, 10), maxVisible);
  const maxOffset = Math.max(0, normalized.length - clampedVisible);
  const clampedOffset = Math.min(offsetFromEnd, maxOffset);
  const end = normalized.length - clampedOffset;
  const start = Math.max(0, end - clampedVisible);
  const visible = normalized.slice(start, end);

  useEffect(() => {
    const element = chartRef.current;
    if (!element) return;
    const updateWidth = () => setContainerWidth(Math.round(element.clientWidth));
    updateWidth();
    const observer = new ResizeObserver(updateWidth);
    observer.observe(element);
    return () => observer.disconnect();
  }, [selectedDate, expanded]);

  useEffect(() => {
    fitInitialViewport();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedDate, selectedTradeId, normalized.length]);

  useEffect(() => {
    setSelectedCandleIndex(null);
    setCrosshair(null);
  }, [selectedDate, normalized.length]);

  useEffect(() => {
    const chartElement = chartRef.current;
    if (!chartElement) return;

    function handleWheel(event: WheelEvent) {
      event.preventDefault();
      if (!normalized.length) return;
      const element = chartRef.current;
      const rect = element?.getBoundingClientRect();
      if (!rect) return;
      const svg = element?.querySelector('svg');
      const svgRect = svg?.getBoundingClientRect() || rect;
      const ratio = Math.min(1, Math.max(0, (event.clientX - svgRect.left) / Math.max(svgRect.width, 1)));
      zoomAtRatio(ratio, event.deltaY < 0);
    }

    chartElement.addEventListener('wheel', handleWheel, { passive: false, capture: true });
    return () => chartElement.removeEventListener('wheel', handleWheel, { capture: true });
  }, [normalized.length, start, clampedVisible]);

  if (!selectedDay) return null;

  if (!normalized.length) {
    return (
      <section className="panel p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h3 className="text-sm font-semibold text-gray-100">Silver Replay Chart</h3>
            <p className="mt-1 text-xs text-gray-500">No chartable Silver candles were returned for {selectedDay.date}.</p>
          </div>
          <DateSelector days={days} selectedDate={selectedDate} onSelectedDateChange={onSelectedDateChange} />
        </div>
      </section>
    );
  }

  const priceScaleWidth = expanded ? 124 : 114;
  const minCandleSpacing = expanded ? 30 : 26;
  const viewportWidth = containerWidth || (expanded ? 1200 : 900);
  const width = fullDayFit && clampedVisible >= maxVisible
    ? viewportWidth
    : Math.max(viewportWidth, visible.length * minCandleSpacing + priceScaleWidth);
  const plotWidth = Math.max(480, width - priceScaleWidth);
  const priceHeight = expanded ? 500 : 420;
  const volumeHeight = expanded ? 96 : 88;
  const totalHeight = priceHeight + volumeHeight + 76;
  const candleWidth = plotWidth / Math.max(visible.length, 1);
  const rawHigh = Math.max(...visible.map((candle: any) => candle.high));
  const rawLow = Math.min(...visible.map((candle: any) => candle.low));
  const priceScale = buildNicePriceScale(rawHigh, rawLow, expanded ? 9 : 7);
  const high = priceScale.high;
  const low = priceScale.low;
  const priceTicks = priceScale.ticks;
  const maxVolume = Math.max(...visible.map((candle: any) => candle.volume), 1);
  const priceSpan = high - low || 1;
  const first = visible[0];
  const last = visible[visible.length - 1];
  const change = last.close - first.open;
  const changePct = first.open ? change / first.open * 100 : 0;
  const selectedVisibleIndex = selectedCandleIndex !== null && selectedCandleIndex >= start && selectedCandleIndex < end
    ? selectedCandleIndex - start
    : null;
  const activeIndex = crosshair
    ? Math.min(visible.length - 1, Math.max(0, Math.floor(crosshair.x / candleWidth)))
    : selectedVisibleIndex;
  const activeCandle = activeIndex !== null ? visible[activeIndex] : null;
  const statCandle = activeCandle || last;
  const activeX = activeIndex !== null ? activeIndex * candleWidth + candleWidth / 2 : 0;
  const activePrice = crosshair ? high - ((crosshair.y - 16) / (priceHeight - 32)) * priceSpan : null;
  const visibleSetupOverlays = chartSetups
    .map((setup: any) => ({ ...setup, index: indexForTime(normalized, setup.time) }))
    .filter((setup: any) => setup.index >= start && setup.index < end);
  const visibleTradeOverlays = chartTrades
    .map((trade: any) => ({
      ...trade,
      entryIndex: indexForTime(normalized, trade.entry_time),
      exitIndex: indexForTime(normalized, trade.exit_time),
      selected: trade.trade_id === selectedTradeId,
    }))
    .filter((trade: any) => trade.entryIndex < end && trade.exitIndex >= start);
  const selectedTradeOverlay = visibleTradeOverlays.find((trade: any) => trade.selected) || (
    selectedTrade
      ? {
          ...selectedTrade,
          entryIndex: indexForTime(normalized, selectedTrade.entry_time),
          exitIndex: indexForTime(normalized, selectedTrade.exit_time),
          selected: true,
        }
      : null
  );
  const entryMarkerGroups = new Map<string, any[]>();
  visibleTradeOverlays.forEach((trade: any) => {
    const anchorIndex = Math.max(start, trade.entryIndex);
    const groupKey = `${trade.side || 'UNKNOWN'}:${anchorIndex}`;
    const group = entryMarkerGroups.get(groupKey) || [];
    group.push(trade);
    entryMarkerGroups.set(groupKey, group);
  });
  const timeTicks = buildTimeTicks(visible, start, candleWidth, expanded ? 8 : 6);
  const chartHeight = expanded ? 680 : 600;
  const fixedPlotViewportWidth = Math.max(0, (containerWidth || width) - priceScaleWidth);
  const visibleTimeTicks = fitTimeTicksToViewport(
    timeTicks,
    scrollLeft,
    fixedPlotViewportWidth,
    expanded ? 112 : 96,
  );

  function y(price: number) {
    return 16 + ((high - price) / priceSpan) * (priceHeight - 32);
  }

  function fitInitialViewport() {
    if (!normalized.length) {
      setVisibleCount(48);
      setOffsetFromEnd(0);
      return;
    }
    if (selectedTrade) {
      fitTradeWindow(selectedTrade, 8);
      return;
    }
    const startTime = viewportHint.start_time || normalized[0]?.time;
    const endTime = viewportHint.end_time || normalized[normalized.length - 1]?.time;
    fitTimeRange(startTime, endTime, viewportHint.mode === 'trade_window' ? 2 : 0);
  }

  function fitTimeRange(startTime: string | null | undefined, endTime: string | null | undefined, padBars = 0) {
    if (!normalized.length) return;
    const startIndex = Math.max(0, indexForTime(normalized, startTime));
    const endIndex = Math.max(startIndex, indexForTime(normalized, endTime));
    const paddedStart = Math.max(0, startIndex - padBars);
    const paddedEnd = Math.min(normalized.length - 1, endIndex + padBars);
    const nextVisible = Math.min(maxVisible, Math.max(10, paddedEnd - paddedStart + 1));
    setFullDayFit(false);
    setVisibleCount(nextVisible);
    setOffsetFromEnd(Math.max(0, normalized.length - (paddedStart + nextVisible)));
  }

  function fitTradeWindow(trade: any, padBars = 8) {
    fitTimeRange(trade?.entry_time, trade?.exit_time || trade?.entry_time, padBars);
  }

  function showFullDay() {
    setFullDayFit(true);
    setVisibleCount(maxVisible);
    setOffsetFromEnd(0);
    setScrollLeft(0);
    chartRef.current?.scrollTo({ left: 0, behavior: 'smooth' });
  }

  function zoomAtRatio(ratio: number, zoomingIn: boolean) {
    const currentVisible = clampedVisible;
    const anchorIndex = start + ratio * Math.max(0, currentVisible - 1);
    const step = Math.max(4, Math.round(currentVisible * (zoomingIn ? 0.22 : 0.28)));
    const nextVisible = Math.min(maxVisible, Math.max(10, zoomingIn ? currentVisible - step : currentVisible + step));
    const nextStart = Math.round(anchorIndex - ratio * Math.max(0, nextVisible - 1));
    const clampedStart = Math.min(Math.max(0, nextStart), Math.max(0, normalized.length - nextVisible));
    setFullDayFit(nextVisible >= maxVisible);
    setVisibleCount(nextVisible);
    setOffsetFromEnd(Math.max(0, normalized.length - (clampedStart + nextVisible)));
  }

  function moveTimeline(direction: 'left' | 'right') {
    const step = Math.max(1, Math.round(clampedVisible * 0.25));
    const nextOffset = direction === 'left'
      ? Math.min(maxOffset, clampedOffset + step)
      : Math.max(0, clampedOffset - step);
    setFullDayFit(false);
    setOffsetFromEnd(nextOffset);
  }

  function handleMouseMove(event: React.MouseEvent<SVGSVGElement>) {
    const svg = event.currentTarget;
    const rect = svg.getBoundingClientRect();
    const x = Math.min(width, Math.max(0, ((event.clientX - rect.left) / Math.max(rect.width, 1)) * width));
    const yPos = Math.min(priceHeight + 18, Math.max(0, ((event.clientY - rect.top) / Math.max(rect.height, 1)) * totalHeight));
    setCrosshair({ x, y: yPos });
  }

  function handlePointerDown(event: React.PointerEvent<HTMLDivElement>) {
    dragRef.current = { x: event.clientX, offset: clampedOffset };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function handlePointerMove(event: React.PointerEvent<HTMLDivElement>) {
    if (!dragRef.current || !normalized.length) return;
    const deltaX = event.clientX - dragRef.current.x;
    const movedBars = Math.round(deltaX / Math.max(candleWidth, 1));
    const nextOffset = Math.min(maxOffset, Math.max(0, dragRef.current.offset - movedBars));
    setOffsetFromEnd(nextOffset);
  }

  function handlePointerUp(event: React.PointerEvent<HTMLDivElement>) {
    dragRef.current = null;
    event.currentTarget.releasePointerCapture(event.pointerId);
  }

  function handleTouchStart(event: React.TouchEvent<HTMLDivElement>) {
    if (event.touches.length !== 2 || !chartRef.current) return;
    event.preventDefault();
    const rect = chartRef.current.getBoundingClientRect();
    const midpointX = (event.touches[0].clientX + event.touches[1].clientX) / 2;
    pinchRef.current = {
      distance: touchDistance(event.touches[0], event.touches[1]),
      ratio: Math.min(1, Math.max(0, (midpointX - rect.left) / Math.max(rect.width, 1))),
    };
  }

  function handleTouchMove(event: React.TouchEvent<HTMLDivElement>) {
    if (event.touches.length !== 2 || !pinchRef.current) return;
    event.preventDefault();
    const nextDistance = touchDistance(event.touches[0], event.touches[1]);
    const previousDistance = pinchRef.current.distance;
    if (!previousDistance) return;
    const scale = nextDistance / previousDistance;
    if (Math.abs(scale - 1) < 0.06) return;
    zoomAtRatio(pinchRef.current.ratio, scale > 1);
    pinchRef.current = { ...pinchRef.current, distance: nextDistance };
  }

  return (
    <section className="panel p-4">
      <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
        <div>
          <h3 className="text-sm font-semibold text-gray-100">Silver Replay Chart</h3>
          <p className="mt-1 text-xs text-gray-500">Executed replay trades only: arrows, exits, trailing paths, and P&amp;L come from trades the simulator actually opened and closed. Setup markers are optional signal context, not extra trades.</p>
        </div>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <DateSelector days={days} selectedDate={selectedDate} onSelectedDateChange={onSelectedDateChange} />
        </div>
      </div>

      <div className="mt-4 space-y-3">
        <div className="rounded border border-[#1f2937] bg-[#111827] p-3">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="font-mono text-base font-semibold text-gray-100">{chart.symbol} / 15-minute candles / {selectedDay.date}</div>
              <div className="mt-1 text-sm text-gray-400">
                {activeCandle ? `Focused candle: ${formatAxisDateTime(activeCandle.time)} IST` : `Showing candles ${start + 1}-${end} of ${normalized.length}`}
              </div>
            </div>
          </div>

          <ChartSymbolLibrary />

          <div className="mt-3 flex flex-wrap items-center gap-4 border-t border-[#1f2937] pt-3 text-xs">
            <Stat label="Open" value={formatNumber(statCandle.open)} />
            <Stat label="High" value={formatNumber(statCandle.high)} />
            <Stat label="Low" value={formatNumber(statCandle.low)} />
            <Stat label="Close" value={formatNumber(statCandle.close)} />
            <Stat label="Change" value={`${change >= 0 ? '+' : ''}${formatNumber(change)} (${changePct.toFixed(2)}%)`} tone={change >= 0 ? 'text-[#22c55e]' : 'text-[#ef4444]'} />
          </div>

          <div className="mb-3 flex flex-wrap items-center gap-1.5">
            <button onClick={() => zoomAtRatio(0.5, false)} disabled={clampedVisible >= maxVisible} aria-label="Zoom out" title="Zoom out" className="inline-flex h-8 w-8 items-center justify-center rounded border border-[#334155] bg-[#1f2937] text-base font-bold text-gray-100 shadow-sm disabled:cursor-not-allowed disabled:opacity-40">−</button>
            <button onClick={() => zoomAtRatio(0.5, true)} disabled={clampedVisible <= 10} aria-label="Zoom in" title="Zoom in" className="inline-flex h-8 w-8 items-center justify-center rounded border border-[#334155] bg-[#1f2937] text-base font-bold text-gray-100 shadow-sm disabled:cursor-not-allowed disabled:opacity-40">+</button>
            <button onClick={() => moveTimeline('left')} disabled={maxOffset === 0 || clampedOffset >= maxOffset} aria-label="Move earlier" title="Move earlier" className="inline-flex h-8 w-8 items-center justify-center rounded border border-[#334155] bg-[#1f2937] text-lg text-gray-100 shadow-sm disabled:cursor-not-allowed disabled:opacity-40">‹</button>
            <button onClick={() => moveTimeline('right')} disabled={maxOffset === 0 || clampedOffset <= 0} aria-label="Move later" title="Move later" className="inline-flex h-8 w-8 items-center justify-center rounded border border-[#334155] bg-[#1f2937] text-lg text-gray-100 shadow-sm disabled:cursor-not-allowed disabled:opacity-40">›</button>
            <button onClick={showFullDay} aria-label="Reset zoom to full day" title="Reset zoom to full day" className="inline-flex h-8 w-8 items-center justify-center rounded border border-[#334155] bg-[#1f2937] text-base text-gray-100 shadow-sm">↻</button>
            <button onClick={() => selectedTrade && fitTradeWindow(selectedTrade, 8)} disabled={!selectedTrade} className="rounded border border-[#1f2937] px-2 py-1 text-xs text-gray-400 disabled:cursor-not-allowed disabled:opacity-50">Fit trade window</button>
          </div>

          <div className="relative overflow-hidden border border-[#1f2937] bg-[#0a0e14]" style={{ height: chartHeight }}>
            <div
              ref={chartRef}
              onScroll={() => setScrollLeft(chartRef.current?.scrollLeft || 0)}
              onPointerDown={handlePointerDown}
              onPointerMove={handlePointerMove}
              onPointerUp={handlePointerUp}
              onPointerCancel={() => { dragRef.current = null; }}
              onTouchStart={handleTouchStart}
              onTouchMove={handleTouchMove}
              onTouchEnd={(event) => { if (event.touches.length < 2) pinchRef.current = null; }}
              onTouchCancel={() => { pinchRef.current = null; }}
              className="h-full overscroll-contain overflow-x-auto overflow-y-hidden bg-[#0a0e14]"
              style={{ overscrollBehavior: 'contain', touchAction: 'none' }}
            >
            <svg
              viewBox={`0 0 ${width} ${totalHeight}`}
              width={width}
              height={chartHeight}
              preserveAspectRatio="none"
              className="block max-w-none cursor-crosshair"
              style={{ width: `${width}px`, minWidth: '100%', height: chartHeight }}
              onMouseMove={handleMouseMove}
              onMouseLeave={() => setCrosshair(null)}
            >
              {priceTicks.map((price) => {
                const lineY = y(price);
                return (
                  <g key={`grid-price-${price}`}>
                    <line x1={0} x2={plotWidth} y1={lineY} y2={lineY} stroke="#1f2937" strokeWidth="1" />
                  </g>
                );
              })}

              {overlays.ema && (
                <path
                  d={visible.map((candle: any, index: number) => {
                    const pointX = index * candleWidth + candleWidth / 2;
                    const pointY = y(candle.ema20);
                    return `${index === 0 ? 'M' : 'L'} ${pointX} ${pointY}`;
                  }).join(' ')}
                  fill="none"
                  stroke={CHART_COLORS.ema}
                  strokeWidth="2"
                />
              )}

              {visible.map((candle: any, index: number) => {
                const x = index * candleWidth + candleWidth / 2;
                const openY = y(candle.open);
                const closeY = y(candle.close);
                const highY = y(candle.high);
                const lowY = y(candle.low);
                const bullish = candle.close >= candle.open;
                const color = bullish ? CHART_COLORS.buy : CHART_COLORS.sell;
                const bodyTop = Math.min(openY, closeY);
                const bodyHeight = Math.max(1, Math.abs(closeY - openY));
                const bodyWidth = Math.max(2, candleWidth * 0.58);
                const volumeBarHeight = candle.volume / maxVolume * (volumeHeight - 10);
                const volumeY = priceHeight + 18 + (volumeHeight - volumeBarHeight);
                const absoluteIndex = start + index;
                const highlighted = Boolean(
                  selectedTradeOverlay
                  && absoluteIndex >= selectedTradeOverlay.entryIndex
                  && absoluteIndex <= selectedTradeOverlay.exitIndex,
                );
                const candleTrade = visibleTradeOverlays
                  .filter((trade: any) => (
                    trade.entryIndex === absoluteIndex
                    || trade.exitIndex === absoluteIndex
                    || (absoluteIndex >= trade.entryIndex && absoluteIndex <= trade.exitIndex)
                  ))
                  .reduce((latest: any, candidate: any) => (
                    !latest || String(candidate.exit_time || '') > String(latest.exit_time || '')
                      ? candidate
                      : latest
                  ), null);
                const selectedCandle = selectedCandleIndex === absoluteIndex;
                const selectedEntryCandle = Boolean(selectedTradeOverlay && absoluteIndex === selectedTradeOverlay.entryIndex);
                const selectedExitCandle = Boolean(selectedTradeOverlay && absoluteIndex === selectedTradeOverlay.exitIndex);
                return (
                  <g
                    key={`${candle.time}-${index}`}
                    onPointerDown={(event) => event.stopPropagation()}
                    onClick={(event) => {
                      event.stopPropagation();
                      setSelectedCandleIndex(absoluteIndex);
                      setCrosshair({ x, y: closeY });
                      if (candleTrade) onSelectedTradeIdChange(candleTrade.trade_id);
                    }}
                    style={{ cursor: 'pointer' }}
                  >
                    <title>{`${candle.time}\nO ${formatNumber(candle.open)} H ${formatNumber(candle.high)} L ${formatNumber(candle.low)} C ${formatNumber(candle.close)}\nEMA20 ${formatNumber(candle.ema20)}\nVol ${candle.volume.toLocaleString('en-IN')}`}</title>
                    <line x1={x} x2={x} y1={highY} y2={lowY} stroke={color} strokeWidth="1.2" />
                    <rect x={x - bodyWidth / 2} y={bodyTop} width={bodyWidth} height={bodyHeight} fill={color} opacity={bullish ? 0.85 : 0.75} />
                    <rect x={x - bodyWidth / 2} y={volumeY} width={bodyWidth} height={volumeBarHeight} fill={color} opacity="0.35" />
                    {highlighted && (
                      <rect
                        x={x - bodyWidth / 2 - 6}
                        y={Math.max(4, highY - 7)}
                        width={bodyWidth + 12}
                        height={Math.max(12, Math.min(priceHeight - 8, lowY + 7) - Math.max(4, highY - 7))}
                        fill="none"
                         stroke={CHART_COLORS.selectedTrade}
                        strokeWidth={selectedEntryCandle || selectedExitCandle ? 2.5 : 1.1}
                        opacity={selectedEntryCandle || selectedExitCandle ? 1 : 0.45}
                        rx="2"
                      />
                    )}
                    {selectedCandle && (
                      <rect
                        x={x - bodyWidth / 2 - 9}
                        y={Math.max(2, highY - 10)}
                        width={bodyWidth + 18}
                        height={Math.max(16, Math.min(priceHeight - 4, lowY + 10) - Math.max(2, highY - 10))}
                        fill="none"
                         stroke={CHART_COLORS.selectedCandle}
                        strokeWidth="2.5"
                        rx="3"
                        pointerEvents="none"
                      />
                    )}
                  </g>
                );
              })}

              {overlays.setups && visibleSetupOverlays.map((setup: any, index: number) => {
                const localIndex = setup.index - start;
                const pointX = localIndex * candleWidth + candleWidth / 2;
                const setupColor = setup.side === 'BUY' ? CHART_COLORS.buyTrigger : CHART_COLORS.sellTrigger;
                return (
                  <g key={`${setup.side}-${setup.time}-${index}`}>
                    <line x1={pointX} x2={plotWidth} y1={y(Number(setup.setup_close))} y2={y(Number(setup.setup_close))} stroke={setupColor} strokeWidth="1.4" opacity="0.45" />
                    <line x1={pointX} x2={plotWidth} y1={y(Number(setup.trigger_level))} y2={y(Number(setup.trigger_level))} stroke={setupColor} strokeWidth="1" strokeDasharray="5 4" opacity="0.7" />
                    <circle cx={pointX} cy={y(Number(setup.setup_close))} r={4} fill={setupColor} />
                  </g>
                );
              })}

              {overlays.trades && Array.from(entryMarkerGroups.entries()).map(([groupKey, group]) => {
                const latestTrade = group.reduce((latest: any, candidate: any) => (
                  !latest || String(candidate.exit_time || '') > String(latest.exit_time || '')
                    ? candidate
                    : latest
                ), group[0]);
                const trade = group.find((item: any) => item.selected) || latestTrade;
                const entryAnchorIndex = Math.max(start, trade.entryIndex);
                const entryIndex = entryAnchorIndex - start;
                const exitIndex = Math.max(start, trade.exitIndex) - start;
                const entryX = entryIndex * candleWidth + candleWidth / 2;
                const exitX = exitIndex * candleWidth + candleWidth / 2;
                const entryCandle = normalized[Math.max(0, Math.min(normalized.length - 1, trade.entryIndex))];
                const entryY = y(Number(trade.entry_price));
                const exitY = y(Number(trade.exit_price));
                const isBuy = trade.side === 'BUY';
                const isSelected = Boolean(trade.selected);
                const color = isBuy ? CHART_COLORS.buy : CHART_COLORS.sell;
                const opacity = isSelected ? 1 : 0.75;
                const candleBoundaryY = isBuy ? y(Number(entryCandle?.high ?? trade.entry_price)) : y(Number(entryCandle?.low ?? trade.entry_price));
                const arrowGap = isSelected ? 20 : 16;
                const arrowReach = isSelected ? 52 : 44;
                const tipY = isBuy
                  ? Math.max(18, candleBoundaryY - arrowGap)
                  : Math.min(priceHeight - 12, candleBoundaryY + arrowGap);
                const shaftStartY = isBuy ? tipY - arrowReach : tipY + arrowReach;
                const shaftEndY = isBuy ? tipY - 12 : tipY + 12;
                const arrowHeadPoints = isBuy
                  ? `${entryX - 7},${tipY - 12} ${entryX + 7},${tipY - 12} ${entryX},${tipY}`
                  : `${entryX - 7},${tipY + 12} ${entryX + 7},${tipY + 12} ${entryX},${tipY}`;
                const exitPointerColor = CHART_COLORS.exit;
                const tradeCount = group.length;
                const badgeY = isBuy
                  ? Math.min(priceHeight - 24, tipY + 14)
                  : Math.min(priceHeight - 24, tipY + 34);
                return (
                  <g
                    key={`entry-group-${groupKey}`}
                    opacity={opacity}
                    onClick={() => onSelectedTradeIdChange(latestTrade.trade_id)}
                    style={{ cursor: 'pointer' }}
                  >
                    <title>{`${tradeCount} executed ${trade.side || ''} trade${tradeCount === 1 ? '' : 's'} in this 15-minute candle. Click to inspect ${trade.trade_id}.`}</title>
                    <line x1={entryX} x2={entryX} y1={shaftStartY} y2={shaftEndY} stroke={color} strokeWidth={isSelected ? 3 : 2.4} strokeLinecap="round" />
                    <polygon points={arrowHeadPoints} fill={color} stroke="#e5e7eb" strokeWidth="0.8" />
                    {tradeCount > 1 && (
                      <g pointerEvents="none">
                        <rect x={entryX + 10} y={badgeY} width={36} height={20} rx={4} fill="#111827" stroke={color} strokeWidth="1.2" />
                        <text x={entryX + 28} y={badgeY + 14} textAnchor="middle" fill="#f8fafc" fontSize="12" fontWeight="800" fontFamily="ui-monospace">×{tradeCount}</text>
                      </g>
                    )}
                    {isSelected && (
                      <g>
                        <line x1={entryX} x2={exitX} y1={entryY} y2={exitY} stroke={CHART_COLORS.tradePath} strokeWidth="2.4" />
                        <circle cx={exitX} cy={exitY} r="5.4" fill="#0a0e14" stroke={exitPointerColor} strokeWidth="2.4" />
                      </g>
                    )}
                    <circle
                      cx={entryX}
                      cy={entryY}
                      r={isSelected ? 5.4 : 4.5}
                      fill="#0a0e14"
                      stroke={CHART_COLORS.entryPrice}
                      strokeWidth={isSelected ? 2.6 : 2}
                    />
                  </g>
                );
              })}

              {overlays.levels && selectedTradeOverlay && (
                <g pointerEvents="none">
                   <line x1={Math.max(0, selectedTradeOverlay.entryIndex - start) * candleWidth + candleWidth / 2} x2={plotWidth} y1={y(Number(selectedTradeOverlay.initial_sl_price))} y2={y(Number(selectedTradeOverlay.initial_sl_price))} stroke={CHART_COLORS.initialSl} strokeWidth="1.5" strokeDasharray="6 4" />
                   <line x1={Math.max(0, selectedTradeOverlay.entryIndex - start) * candleWidth + candleWidth / 2} x2={plotWidth} y1={y(Number(selectedTradeOverlay.final_sl_price))} y2={y(Number(selectedTradeOverlay.final_sl_price))} stroke={CHART_COLORS.effectiveSl} strokeWidth="1.8" />
                   <line x1={Math.max(0, selectedTradeOverlay.entryIndex - start) * candleWidth + candleWidth / 2} x2={plotWidth} y1={y(Number(selectedTradeOverlay.target_price))} y2={y(Number(selectedTradeOverlay.target_price))} stroke={CHART_COLORS.target} strokeWidth="1.5" strokeDasharray="4 3" />
                </g>
              )}

              {overlays.trailing && selectedTradeOverlay && Array.isArray(selectedTradeOverlay.trailing_moves) && selectedTradeOverlay.trailing_moves.length > 0 && (
                <path
                  d={buildTrailingPath(
                    normalized,
                    start,
                    candleWidth,
                    y,
                    Number(selectedTradeOverlay.initial_sl_price),
                    selectedTradeOverlay.entry_time,
                    selectedTradeOverlay.exit_time,
                    selectedTradeOverlay.trailing_moves,
                    Number(selectedTradeOverlay.final_sl_price),
                  )}
                  fill="none"
                   stroke={CHART_COLORS.trailing}
                  strokeWidth="2"
                />
              )}

              {crosshair && activeCandle && activePrice !== null && (
                <g pointerEvents="none">
                  <line x1={activeX} x2={activeX} y1={0} y2={priceHeight + 18} stroke={CHART_COLORS.crosshair} strokeDasharray="5 5" strokeWidth="1" opacity="0.75" />
                  <line x1={0} x2={plotWidth} y1={crosshair.y} y2={crosshair.y} stroke={CHART_COLORS.crosshair} strokeDasharray="5 5" strokeWidth="1" opacity="0.75" />
                </g>
              )}

              <line x1={0} x2={plotWidth} y1={priceHeight + 18} y2={priceHeight + 18} stroke="#1f2937" />
              {timeTicks.map((tick) => (
                <g key={tick.key}>
                  <line x1={tick.x} x2={tick.x} y1={priceHeight + 18} y2={priceHeight + 30} stroke="#334155" strokeWidth="1" />
                  <text x={tick.x} y={totalHeight - 28} textAnchor="middle" fill="#cbd5e1" fontSize="13" fontWeight="600" fontFamily="ui-monospace">
                    {tick.dateLabel}
                  </text>
                  <text x={tick.x} y={totalHeight - 10} textAnchor="middle" fill="#f8fafc" fontSize="14" fontWeight="700" fontFamily="ui-monospace">
                    {tick.label}
                  </text>
                </g>
              ))}
              <text x={8} y={totalHeight - 46} fill="#60a5fa" fontSize="13" fontWeight="600" fontFamily="ui-monospace">
                15-minute candles · time in IST
              </text>
              <text x={8} y={totalHeight - 10} fill="#94a3b8" fontSize="12" fontFamily="ui-monospace">
                {activeCandle ? `Focused ${formatAxisDateTime(activeCandle.time)}  |  window ${formatAxisDateTime(first.time)} -> ${formatAxisDateTime(last.time)}` : `${formatAxisDateTime(first.time)} -> ${formatAxisDateTime(last.time)}`}
              </text>
            </svg>
            </div>

            <div
              className="pointer-events-none absolute right-0 top-0 z-20 border-l border-[#334155] bg-[#0a0e14]"
              style={{ width: priceScaleWidth, height: chartHeight }}
              aria-label="Fixed price scale"
            >
              <svg viewBox={`0 0 ${priceScaleWidth} ${totalHeight}`} width={priceScaleWidth} height={chartHeight} preserveAspectRatio="none">
                {priceTicks.map((price) => {
                  const lineY = y(price);
                  return (
                    <g key={`fixed-price-${price}`}>
                      <line x1={0} x2={8} y1={lineY} y2={lineY} stroke="#475569" strokeWidth="1" />
                      <rect x={8} y={lineY - 15} width={priceScaleWidth - 16} height={25} rx={4} fill="#111827" stroke="#334155" />
                      <text x={priceScaleWidth - 12} y={lineY + 2} textAnchor="end" fill="#e2e8f0" fontSize="13.5" fontWeight="600" fontFamily="ui-monospace">{formatNumber(price)}</text>
                    </g>
                  );
                })}
                {crosshair && activePrice !== null && (
                  <g>
                    <rect x={8} y={Math.max(2, Math.min(priceHeight - 25, crosshair.y - 13))} width={priceScaleWidth - 16} height={25} fill="#172033" stroke="#60a5fa" />
                    <text x={priceScaleWidth - 12} y={Math.max(18, Math.min(priceHeight - 7, crosshair.y + 5))} textAnchor="end" fill="#f8fafc" fontSize="13.5" fontWeight="700" fontFamily="ui-monospace">{formatNumber(activePrice)}</text>
                  </g>
                )}
              </svg>
            </div>

            <div
              className="pointer-events-none absolute bottom-0 left-0 z-20 border-t border-[#334155] bg-[#0a0e14]"
              style={{ right: priceScaleWidth, height: 76 }}
              aria-label="Fixed time scale"
            >
              <svg viewBox={`0 0 ${Math.max(fixedPlotViewportWidth, 1)} 76`} width="100%" height="76" preserveAspectRatio="none">
                <text x={8} y={18} fill="#60a5fa" fontSize="13" fontWeight="600" fontFamily="ui-monospace">15-minute candles · IST</text>
                {visibleTimeTicks.map((tick) => {
                  const x = tick.x;
                  return (
                    <g key={`fixed-time-${tick.key}`}>
                      <line x1={x} x2={x} y1={0} y2={10} stroke="#64748b" strokeWidth="1" />
                      <text x={tick.labelX} y={39} textAnchor="middle" fill="#cbd5e1" fontSize="12.5" fontWeight="600" fontFamily="ui-monospace">{tick.dateLabel}</text>
                      <text x={tick.labelX} y={61} textAnchor="middle" fill="#f8fafc" fontSize="14" fontWeight="700" fontFamily="ui-monospace">{tick.label}</text>
                    </g>
                  );
                })}
              </svg>
            </div>
          </div>
        </div>

        <div className="grid gap-3 xl:grid-cols-[minmax(0,1.55fr)_minmax(0,280px)]">
          <div className="rounded border border-[#1f2937] bg-[#111827] p-3">
            <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">Selected trade</div>
            {!selectedTrade ? (
              <p className="mt-3 text-sm text-gray-500">No trade selected for this day yet. Pick a trade row or marker to focus it.</p>
            ) : (
              <div className="mt-3 grid gap-2 text-sm md:grid-cols-2">
                <ChartMetric label="Trade ID" value={String(selectedTrade.trade_id)} mono className="md:col-span-2" valueClassName="text-[12px] leading-5 break-all" />
                <ChartMetric label="Side" value={selectedTrade.side} tone={selectedTrade.side === 'BUY' ? 'text-[#22c55e]' : 'text-[#ef4444]'} />
                <ChartMetric label="Net P&L" value={money(Number(selectedTrade.net_pnl || 0))} tone={Number(selectedTrade.net_pnl || 0) >= 0 ? 'text-[#22c55e]' : 'text-[#ef4444]'} />
                <ChartMetric label="Entry mode" value={formatEntryMode(selectedTrade.entry_mode, selectedTrade.entry_mode_label)} className="md:col-span-2" valueClassName="leading-5 whitespace-normal text-[#fbbf24]" />
                <ChartMetric label="Entry" value={`${formatBacktestDateTime(selectedTrade.entry_time, selectedTrade.exit_time)} @ ${formatNumber(Number(selectedTrade.entry_price))}`} className="md:col-span-2" valueClassName="leading-5 whitespace-normal" />
                <ChartMetric label="Exit" value={`${formatBacktestDateTime(selectedTrade.exit_time, selectedTrade.entry_time)} @ ${formatNumber(Number(selectedTrade.exit_price))}`} className="md:col-span-2" valueClassName="leading-5 whitespace-normal" />
                {selectedTrade.side === 'SELL' && <>
                  <ChartMetric label="Active reference" value={formatOptionalNumber(selectedTrade.active_reference_close)} />
                  <ChartMetric label="Trigger used" value={formatOptionalNumber(selectedTrade.trigger_level_used)} />
                </>}
                <ChartMetric label="Timing" value={sameBacktestMinute(selectedTrade.entry_time, selectedTrade.exit_time) ? 'Same 1-minute bar; exact second unavailable' : 'Different 1-minute bars'} className="md:col-span-2" valueClassName="leading-5 whitespace-normal text-[#fbbf24]" />
                <ChartMetric label="Final SL" value={formatNumber(Number(selectedTrade.final_sl_price))} />
                <ChartMetric label="Target" value={formatNumber(Number(selectedTrade.target_price))} />
              </div>
            )}
          </div>

          <div className="rounded border border-[#1f2937] bg-[#111827] p-3">
            <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">Replay summary</div>
            <div className="mt-3 grid gap-2 text-sm">
              <ChartMetric label="Trades on day" value={String(chartTrades.length)} />
              <ChartMetric label="Setups on day" value={String(chartSetups.length)} />
              <ChartMetric label="Viewport" value={viewportHint.mode === 'trade_window' ? 'Trade-focused' : 'Full day'} />
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function DateSelector({
  days,
  selectedDate,
  onSelectedDateChange,
}: {
  days: any[];
  selectedDate: string;
  onSelectedDateChange: (value: string) => void;
}) {
  return (
    <select value={selectedDate} onChange={(event) => onSelectedDateChange(event.target.value)} className="control text-sm">
      {days.map((day) => <option key={day.date} value={day.date}>{day.date}</option>)}
    </select>
  );
}

function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: () => void }) {
  return (
    <label className="flex items-center justify-between gap-3">
      <span>{label}</span>
      <input type="checkbox" checked={checked} onChange={onChange} />
    </label>
  );
}

function ChartSymbolLibrary() {
  return (
    <div className="rounded border border-[#1f2937] bg-[#0d1117] p-3" aria-label="Chart symbol library">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="text-xs font-semibold uppercase tracking-wide text-gray-300">Chart symbol library</div>
        <div className="text-[11px] text-gray-500">15-minute candles · IST · selected trade is highlighted</div>
      </div>
      <div className="mt-3 grid gap-x-4 gap-y-2 sm:grid-cols-2 xl:grid-cols-4">
        <LegendItem swatch="buy" label="BUY entry" detail="Green down arrow" />
        <LegendItem swatch="sell" label="SELL entry" detail="Red up arrow" />
        <LegendItem swatch="entry-price" label="Entry price" detail="Cyan dot; ×N groups same-candle trades" />
        <LegendItem swatch="exit" label="Selected exit" detail="Gold exit dot" />
        <LegendItem swatch="path" label="Trade path" detail="Pink entry-to-exit line" />
        <LegendItem swatch="setup-buy" label="BUY setup" detail="Teal setup dot" />
        <LegendItem swatch="setup-sell" label="SELL setup" detail="Magenta setup dot" />
        <LegendItem swatch="trigger" label="Trigger" detail="Teal/magenta dashed line" />
        <LegendItem swatch="sl-initial" label="Initial SL" detail="Orange dashed line" />
        <LegendItem swatch="sl-final" label="Effective SL" detail="Pink solid line" />
        <LegendItem swatch="target" label="Target" detail="Violet dashed line" />
        <LegendItem swatch="trailing" label="Trailing path" detail="Lime stepped line" />
        <LegendItem swatch="crosshair" label="Crosshair" detail="Gray guide lines" />
      </div>
    </div>
  );
}

function LegendItem({ swatch, label, detail }: { swatch: string; label: string; detail: string }) {
  return (
    <div className="flex min-w-0 items-center gap-2">
      <LegendSwatch type={swatch} />
      <div className="min-w-0">
        <div className="text-xs font-semibold text-gray-200">{label}</div>
        <div className="truncate text-[11px] text-gray-500" title={detail}>{detail}</div>
      </div>
    </div>
  );
}

function LegendSwatch({ type }: { type: string }) {
  if (type === 'buy' || type === 'sell') {
    const buy = type === 'buy';
    return (
      <span
        className="relative inline-flex h-8 w-7 shrink-0 items-center justify-center"
        style={{ color: buy ? CHART_COLORS.buy : CHART_COLORS.sell }}
        aria-hidden="true"
      >
        <span className="absolute h-5 w-0.5 rounded bg-current" />
        <span className={`absolute ${buy ? 'top-5' : 'bottom-5'} h-0 w-0 border-x-[5px] border-x-transparent ${buy ? 'border-t-[7px] border-t-current' : 'border-b-[7px] border-b-current'}`} />
      </span>
    );
  }

  if (type === 'exit') {
    return (
      <span className="relative inline-flex h-8 w-7 shrink-0 items-center justify-center" style={{ color: CHART_COLORS.exit }} aria-hidden="true">
        <span className="absolute h-3 w-3 rounded-full border-2 border-current bg-[#0a0e14]" />
      </span>
    );
  }

  if (type === 'entry-price') {
    return <span className="inline-flex h-8 w-7 shrink-0 items-center justify-center" aria-hidden="true"><span className="h-3 w-3 rounded-full border-2 border-[#22d3ee] bg-[#0a0e14]" /></span>;
  }

  const styles: Record<string, string> = {
    path: 'h-0 w-7 border-t-2 border-[#f0abfc]',
    'setup-buy': 'h-3 w-3 rounded-full bg-[#14b8a6]',
    'setup-sell': 'h-3 w-3 rounded-full bg-[#d946ef]',
    trigger: 'h-0 w-7 border-t border-dashed border-[#14b8a6]',
    'sl-initial': 'h-0 w-7 border-t-2 border-dashed border-[#fb923c]',
    'sl-final': 'h-0 w-7 border-t-2 border-[#ec4899]',
    target: 'h-0 w-7 border-t-2 border-dashed border-[#8b5cf6]',
    trailing: 'h-0 w-7 border-t-2 border-[#a3e635]',
    crosshair: 'h-0 w-7 border-t border-dashed border-[#94a3b8]',
  };
  return <span className={`inline-flex w-7 shrink-0 items-center justify-center ${styles[type] || 'h-3 w-3 rounded-full bg-[#64748b]'}`} aria-hidden="true" />;
}

function Stat({ label, value, tone = 'text-gray-100' }: { label: string; value: string; tone?: string }) {
  return (
    <div>
      <div className="label">{label}</div>
      <div className={`num mt-1 ${tone}`}>{value}</div>
    </div>
  );
}

function ChartMetric({
  label,
  value,
  tone = 'text-gray-100',
  mono = false,
  className = '',
  valueClassName = '',
}: {
  label: string;
  value: string;
  tone?: string;
  mono?: boolean;
  className?: string;
  valueClassName?: string;
}) {
  return (
    <div className={`min-w-0 rounded border border-[#1f2937] bg-[#0d1117] p-2 ${className}`}>
      <div className="label">{label}</div>
      <div className={`mt-1 text-sm ${tone} ${mono ? 'font-mono break-all' : 'num whitespace-normal'} ${valueClassName}`}>{value}</div>
    </div>
  );
}

function formatEntryMode(value: unknown, label?: unknown) {
  if (label) return String(label);
  if (value === 'SAME_REFERENCE_REENTRY') return 'Same-reference re-entry after exit';
  if (value === 'LEGACY_CONFIRMATION') return 'Legacy confirmation entry';
  if (value === 'THRESHOLD_TRIGGER') return 'Initial threshold trigger';
  return value ? String(value).replaceAll('_', ' ').toLowerCase().replace(/(^|\s)\S/g, (letter) => letter.toUpperCase()) : '--';
}

function formatOptionalNumber(value: unknown) {
  return value === null || value === undefined ? '--' : formatNumber(Number(value));
}

function indexForTime(candles: any[], timeValue: string | null | undefined) {
  const target = parseMaybeDate(timeValue)?.getTime();
  if (!target || !candles.length) return 0;
  let bestIndex = 0;
  for (let index = 0; index < candles.length; index += 1) {
    const current = candles[index].timeMs;
    if (current === null || current === undefined) continue;
    if (current <= target) bestIndex = index;
    if (current > target) break;
  }
  return bestIndex;
}

function buildTrailingPath(
  candles: any[],
  start: number,
  candleWidth: number,
  yForPrice: (price: number) => number,
  initialSl: number,
  entryTime: string,
  exitTime: string,
  trailingMoves: any[],
  finalSl: number,
) {
  const points = [
    { time: entryTime, sl: initialSl },
    ...trailingMoves.map((move: any) => ({ time: move.time, sl: Number(move.new_sl) })),
    { time: exitTime, sl: finalSl },
  ];
  let path = '';
  let previousX = 0;
  let previousY = 0;
  points.forEach((point, index) => {
    const rawIndex = indexForTime(candles, point.time);
    const localIndex = Math.max(0, rawIndex - start);
    const x = localIndex * candleWidth + candleWidth / 2;
    const y = yForPrice(Number(point.sl));
    if (index === 0) {
      path = `M ${x} ${y}`;
    } else {
      path += ` H ${x} V ${y}`;
    }
    previousX = x;
    previousY = y;
  });
  if (!path && Number.isFinite(previousX) && Number.isFinite(previousY)) {
    path = `M ${previousX} ${previousY}`;
  }
  return path;
}

function buildTimeTicks(visibleCandles: any[], startIndex: number, candleWidth: number, desiredCount: number) {
  if (!visibleCandles.length) return [];
  const count = Math.max(2, desiredCount);
  const rawStep = Math.max(1, Math.floor((visibleCandles.length - 1) / Math.max(1, count - 1)));
  const indices = new Set<number>([0, visibleCandles.length - 1]);
  for (let index = rawStep; index < visibleCandles.length - 1; index += rawStep) {
    indices.add(index);
  }
  return Array.from(indices)
    .sort((left, right) => left - right)
    .map((visibleIndex) => {
      const candle = visibleCandles[visibleIndex];
      return {
        key: `${startIndex + visibleIndex}-${candle.time}`,
        x: visibleIndex * candleWidth + candleWidth / 2,
        label: formatAxisTime(candle.time),
        dateLabel: formatAxisDate(candle.time),
      };
    });
}

function fitTimeTicksToViewport(ticks: any[], scrollLeft: number, viewportWidth: number, minimumGap: number) {
  if (!ticks.length || viewportWidth <= 0) return [];

  const labelHalfWidth = 38;
  const candidates = ticks
    .map((tick) => {
      const x = tick.x - scrollLeft;
      return {
        ...tick,
        x,
        labelX: Math.min(viewportWidth - labelHalfWidth, Math.max(labelHalfWidth, x)),
      };
    })
    .filter((tick) => tick.x >= -labelHalfWidth && tick.x <= viewportWidth + labelHalfWidth);

  const selected: any[] = [];
  candidates.forEach((candidate, index) => {
    const previous = selected[selected.length - 1];
    const isLastCandidate = index === candidates.length - 1;
    if (!previous || candidate.labelX - previous.labelX >= minimumGap) {
      selected.push(candidate);
    } else if (isLastCandidate) {
      selected[selected.length - 1] = candidate;
    }
  });

  return selected;
}

function formatNumber(value: number) {
  return Number(value || 0).toLocaleString('en-IN', { maximumFractionDigits: 2 });
}

function buildNicePriceScale(rawHigh: number, rawLow: number, desiredTickCount: number) {
  const range = Math.max(rawHigh - rawLow, 1);
  const roughStep = range / Math.max(desiredTickCount - 1, 1);
  const magnitude = 10 ** Math.floor(Math.log10(roughStep));
  const normalized = roughStep / magnitude;
  const multiplier = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  const step = multiplier * magnitude;
  const high = Math.ceil(rawHigh / step) * step;
  const low = Math.floor(rawLow / step) * step;
  const ticks: number[] = [];

  for (let price = low; price <= high + step * 0.001; price += step) {
    ticks.push(Number(price.toFixed(8)));
  }

  return { high, low, ticks: ticks.reverse() };
}

function money(value: number) {
  return `Rs ${formatNumber(value)}`;
}

function parseMaybeDate(value: string | null | undefined) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatDateTimeShort(value: string | null | undefined) {
  const date = parseMaybeDate(value);
  if (!date) return '--';
  return date.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

function sameBacktestMinute(first: string | null | undefined, second: string | null | undefined) {
  if (!first || !second) return false;
  const firstDate = parseMaybeDate(first);
  const secondDate = parseMaybeDate(second);
  if (!firstDate || !secondDate) return false;
  const parts = (date: Date) => date.toLocaleString('en-CA', { timeZone: 'Asia/Kolkata', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  return parts(firstDate) === parts(secondDate);
}

function formatBacktestDateTime(value: string | null | undefined, otherTime: string | null | undefined) {
  const formatted = formatDateTimeShort(value);
  return sameBacktestMinute(value, otherTime) ? `${formatted} · same 1-minute bar` : formatted;
}

function formatAxisTime(value: string | null | undefined) {
  const date = parseMaybeDate(value);
  if (!date) return '--';
  return date.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

function formatAxisDate(value: string | null | undefined) {
  const date = parseMaybeDate(value);
  if (!date) return '--';
  return date.toLocaleDateString('en-IN', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit',
    month: 'short',
  });
}

function formatAxisDateTime(value: string | null | undefined) {
  const date = parseMaybeDate(value);
  if (!date) return '--';
  return date.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

function touchDistance(
  first: { clientX: number; clientY: number },
  second: { clientX: number; clientY: number },
) {
  return Math.hypot(first.clientX - second.clientX, first.clientY - second.clientY);
}
