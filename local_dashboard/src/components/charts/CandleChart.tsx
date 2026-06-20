import { useEffect, useRef, useState } from 'react'
import {
  createChart,
  ColorType,
  CrosshairMode,
  type IChartApi,
  type ISeriesApi,
  type CandlestickData,
  type Time,
  type PriceLineOptions,
  LineStyle,
} from 'lightweight-charts'
import type { Candle } from '../../api/types'

const TIMEFRAMES = ['M1', 'M5', 'M15', 'H1', 'H4'] as const
type Timeframe = typeof TIMEFRAMES[number]

interface Level {
  price: number
  label: string
  color: string
  style?: LineStyle
}

interface CandleChartProps {
  symbol: string
  candles?: Candle[] | null
  levels?: Level[]
  loading?: boolean
  error?: string | null
  onTimeframeChange?: (tf: Timeframe) => void
  timeframe?: Timeframe
  height?: number
}

const CHART_COLORS = {
  background: '#161b22',
  text: '#8b949e',
  grid: '#21262d',
  border: '#30363d',
  up: '#3fb950',
  down: '#f85149',
  wick: '#6e7681',
}

function parseTime(candle: Candle): number {
  if (typeof candle.time === 'number' && candle.time > 1_000_000) return candle.time
  if (candle.candle_time) {
    const d = new Date(candle.candle_time)
    if (!isNaN(d.getTime())) return Math.floor(d.getTime() / 1000)
  }
  return 0
}

export function CandleChart({
  symbol,
  candles,
  levels = [],
  loading,
  error,
  onTimeframeChange,
  timeframe = 'M15',
  height = 320,
}: CandleChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const priceLinesRef = useRef<ReturnType<ISeriesApi<'Candlestick'>['createPriceLine']>[]>([])

  useEffect(() => {
    if (!containerRef.current) return

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: CHART_COLORS.background },
        textColor: CHART_COLORS.text,
        fontSize: 10,
        fontFamily: 'JetBrains Mono, monospace',
      },
      grid: {
        vertLines: { color: CHART_COLORS.grid },
        horzLines: { color: CHART_COLORS.grid },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
      },
      rightPriceScale: {
        borderColor: CHART_COLORS.border,
        textColor: CHART_COLORS.text,
      },
      timeScale: {
        borderColor: CHART_COLORS.border,
        timeVisible: true,
        secondsVisible: false,
      },
      width: containerRef.current.clientWidth,
      height,
    })

    const series = chart.addCandlestickSeries({
      upColor: CHART_COLORS.up,
      downColor: CHART_COLORS.down,
      borderUpColor: CHART_COLORS.up,
      borderDownColor: CHART_COLORS.down,
      wickUpColor: CHART_COLORS.wick,
      wickDownColor: CHART_COLORS.wick,
    })

    chartRef.current = chart
    seriesRef.current = series

    const ro = new ResizeObserver(() => {
      if (containerRef.current) {
        chart.applyOptions({ width: containerRef.current.clientWidth })
      }
    })
    ro.observe(containerRef.current)

    return () => {
      ro.disconnect()
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
    }
  }, [height])

  // Update candle data
  useEffect(() => {
    if (!seriesRef.current || !candles || candles.length === 0) return
    const data: CandlestickData[] = candles
      .map(c => ({
        time: parseTime(c) as Time,
        open: Number(c.open),
        high: Number(c.high),
        low: Number(c.low),
        close: Number(c.close),
      }))
      .filter(d => (d.time as unknown as number) > 0 && d.open > 0)
      .sort((a, b) => (a.time as number) - (b.time as number))

    if (data.length > 0) {
      seriesRef.current.setData(data)
      chartRef.current?.timeScale().fitContent()
    }
  }, [candles])

  // Update price levels
  useEffect(() => {
    if (!seriesRef.current) return
    // Remove old lines
    priceLinesRef.current.forEach(pl => {
      try { seriesRef.current?.removePriceLine(pl) } catch { /* ignore */ }
    })
    priceLinesRef.current = []
    // Add new lines
    levels.forEach(lvl => {
      if (lvl.price == null || isNaN(lvl.price)) return
      const pl = seriesRef.current!.createPriceLine({
        price: lvl.price,
        color: lvl.color,
        lineWidth: 1,
        lineStyle: lvl.style ?? LineStyle.Dashed,
        axisLabelVisible: true,
        title: lvl.label,
      } as PriceLineOptions)
      priceLinesRef.current.push(pl)
    })
  }, [levels])

  return (
    <div className="flex flex-col gap-2">
      {/* Timeframe selector */}
      <div className="flex items-center gap-1">
        {TIMEFRAMES.map(tf => (
          <button
            key={tf}
            onClick={() => onTimeframeChange?.(tf)}
            className={`px-2 py-0.5 text-2xs font-mono rounded transition-colors ${
              tf === timeframe
                ? 'bg-accent-green/20 text-accent-green border border-accent-green/40'
                : 'text-text-muted hover:text-text-primary hover:bg-surface-3 border border-transparent'
            }`}
          >
            {tf}
          </button>
        ))}
      </div>

      {/* Chart */}
      <div className="relative rounded overflow-hidden border border-border" style={{ height }}>
        {loading && (
          <div className="absolute inset-0 flex items-center justify-center bg-surface-1/80 z-10">
            <div className="flex flex-col items-center gap-2">
              <div className="w-6 h-6 border-2 border-accent-green/30 border-t-accent-green rounded-full animate-spin" />
              <span className="text-2xs font-mono text-text-muted">LOADING {symbol}/{timeframe}</span>
            </div>
          </div>
        )}
        {error && !loading && (
          <div className="absolute inset-0 flex items-center justify-center bg-surface-1 z-10">
            <div className="flex flex-col items-center gap-2 text-center px-4">
              <span className="text-accent-amber text-xl">◎</span>
              <span className="text-xs font-mono text-text-muted">{error}</span>
            </div>
          </div>
        )}
        {!loading && !error && (!candles || candles.length === 0) && (
          <div className="absolute inset-0 flex items-center justify-center bg-surface-1 z-10">
            <div className="flex flex-col items-center gap-2">
              <span className="text-2xl opacity-20">◎</span>
              <span className="text-xs font-mono text-text-muted">HISTORY_NOT_READY</span>
              <span className="text-2xs text-text-muted/50">Waiting for first backend cycle</span>
            </div>
          </div>
        )}
        <div ref={containerRef} className="w-full h-full" />
      </div>
    </div>
  )
}
