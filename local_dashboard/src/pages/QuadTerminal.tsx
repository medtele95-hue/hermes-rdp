import { useState, useCallback } from 'react'
import { useQuadTerminal, useCandles } from '../api/usePolling'
import { CandleChart } from '../components/charts/CandleChart'
import { Badge, StateBadge, DecisionBadge, DirectionBadge, GradeBadge } from '../components/ui/Badge'
import { StatRow, Num } from '../components/ui/StatRow'
import { Skeleton, NoDataState } from '../components/ui/Skeleton'
import type { SymbolCard, Candle } from '../api/types'

const TABS = ['Confirmations', 'Plan', 'Levels', 'Strategy', 'Order Flow'] as const
type Tab = typeof TABS[number]

const TIMEFRAMES = ['M1', 'M5', 'M15', 'H1', 'H4'] as const
type Timeframe = typeof TIMEFRAMES[number]

// ── Per-card live candle loader
function LiveCandleChart({ symbol, cardCandles }: {
  symbol: string
  cardCandles: Record<string, { available: boolean; count: number }>
}) {
  const [tf, setTf] = useState<Timeframe>('M15')
  const cleanSym = symbol.replace('#', '')
  const { data: candleData, loading, error } = useCandles(cleanSym, tf)

  const candles = candleData?.candles as Candle[] | undefined

  const levels: { price: number; label: string; color: string }[] = []

  return (
    <CandleChart
      symbol={symbol}
      candles={candles}
      levels={levels}
      loading={loading}
      error={!candleData && error ? error : null}
      timeframe={tf}
      onTimeframeChange={setTf}
      height={300}
    />
  )
}

// ── Confirmations tab
function ConfirmationsTab({ card }: { card: SymbolCard }) {
  const c = card.confirmations
  const score = (v?: number | null) =>
    v != null ? (
      <span className={`font-mono ${v >= 70 ? 'text-accent-green' : v >= 40 ? 'text-accent-amber' : 'text-accent-red'}`}>
        {v.toFixed(1)}
      </span>
    ) : <span className="text-text-muted">—</span>

  return (
    <div className="space-y-1 text-xs">
      <StatRow label="SMC score" value={score(c.smc_score)} mono={false} />
      <StatRow label="SMC status" value={<Badge variant={c.smc_status === 'PASS' ? 'green' : 'amber'}>{c.smc_status ?? '—'}</Badge>} mono={false} />
      <StatRow label="MTFA score" value={score(c.mtfa_score)} mono={false} />
      <StatRow label="MTFA status" value={<Badge variant={c.mtfa_status === 'PASS' ? 'green' : 'amber'}>{c.mtfa_status ?? '—'}</Badge>} mono={false} />
      <StatRow label="Confluence" value={score(c.confluence_score)} mono={false} />
      <StatRow label="Grade" value={<GradeBadge grade={c.confluence_grade} />} mono={false} />
      <StatRow label="Geometry score" value={score(c.geometry_score)} mono={false} />
      <StatRow label="Time gate" value={<Badge variant={c.time_gate?.includes('OK') ? 'green' : 'amber'}>{c.time_gate ?? '—'}</Badge>} mono={false} />
      <StatRow label="Spread" value={<Badge variant={c.spread_status === 'OK' ? 'green' : 'block'}>{c.spread_status ?? '—'}</Badge>} mono={false} />
      <StatRow label="Demo eligible" value={<Badge variant={c.demo_eligible ? 'green' : 'muted'}>{String(c.demo_eligible ?? false)}</Badge>} mono={false} />
      {c.hard_block && (
        <StatRow label="HARD BLOCK" value={<Badge variant="block">{c.block_reason ?? 'BLOCKED'}</Badge>} mono={false} />
      )}
      {(c.failed_gates ?? []).length > 0 && (
        <div className="pt-1">
          <span className="text-2xs text-text-muted uppercase">Failed gates</span>
          <div className="flex flex-wrap gap-1 mt-1">
            {(c.failed_gates ?? []).map((g, i) => <Badge key={i} variant="stale">{g}</Badge>)}
          </div>
        </div>
      )}
    </div>
  )
}

// ── Plan tab
function PlanTab({ card }: { card: SymbolCard }) {
  const p = card.plan
  return (
    <div className="space-y-1 text-xs">
      <StatRow label="Direction" value={<DirectionBadge direction={p.direction} />} mono={false} />
      <StatRow label="Strategy" value={p.best_strategy ?? '—'} />
      <StatRow label="Entry" value={<Num value={p.entry} decimals={5} />} />
      <StatRow label="Stop Loss" value={<Num value={p.sl} decimals={5} />} />
      <StatRow label="Take Profit 1" value={<Num value={p.tp} decimals={5} />} />
      <StatRow label="Take Profit 2" value={<Num value={p.tp2} decimals={5} />} />
      <StatRow label="Risk/Reward" value={p.rr != null ? `${p.rr.toFixed(2)}:1` : '—'} />
      <StatRow label="Confidence" value={p.confidence != null ? `${p.confidence.toFixed(1)}%` : '—'} />
      <StatRow label="Grade" value={<GradeBadge grade={p.grade} />} mono={false} />
      <StatRow label="Analysis only" value={<Badge variant={p.analysis_only ? 'observe' : 'muted'}>{String(p.analysis_only ?? false)}</Badge>} mono={false} />
      <StatRow label="Blocked" value={<Badge variant={p.blocked ? 'block' : 'green'}>{String(p.blocked ?? false)}</Badge>} mono={false} />
      {p.block_reason && <StatRow label="Block reason" value={p.block_reason} highlight="red" />}
    </div>
  )
}

// ── Levels tab
function LevelsTab({ card }: { card: SymbolCard }) {
  const l = card.levels
  const fmt = (v?: number | null) => v != null ? v.toFixed(5) : '—'
  const rows = [
    { label: 'POC', value: fmt(l.poc) },
    { label: 'VAH', value: fmt(l.vah) },
    { label: 'VAL', value: fmt(l.val) },
    { label: 'VWAP', value: fmt(l.vwap) },
    { label: 'Entry', value: fmt(l.entry) },
    { label: 'Stop Loss', value: fmt(l.sl) },
    { label: 'Take Profit 1', value: fmt(l.tp1) },
    { label: 'Take Profit 2', value: fmt(l.tp2) },
    { label: 'Support', value: fmt(l.support) },
    { label: 'Resistance', value: fmt(l.resistance) },
  ]
  return (
    <div className="space-y-0">
      {rows.map(r => (
        <StatRow key={r.label} label={r.label} value={r.value} />
      ))}
    </div>
  )
}

// ── Strategy tab
function StrategyTab({ card }: { card: SymbolCard }) {
  const sigs = card.strategies
  if (!sigs || sigs.length === 0) {
    return <NoDataState label="NO_STRATEGY_SIGNALS" sub="No signals from this cycle" />
  }
  return (
    <div className="space-y-2">
      {sigs.map((s, i) => {
        const name = s.strategy ?? s.setup_type ?? `STRATEGY_${i}`
        const dec = s.decision ?? 'WAIT'
        const decVariant = dec === 'BUY' || dec === 'SELL' || dec === 'PASS' ? 'live' :
                           dec === 'BLOCK' || dec === 'REJECT' ? 'block' :
                           dec === 'WAIT' ? 'wait' : 'observe'
        return (
          <div key={i} className="border border-border rounded p-2 space-y-1">
            <div className="flex items-center justify-between">
              <span className="text-2xs font-mono font-medium text-text-primary truncate">{name}</span>
              <Badge variant={decVariant}>{dec}</Badge>
            </div>
            <div className="flex gap-2 flex-wrap">
              {s.direction && <DirectionBadge direction={s.direction} />}
              {s.grade && <GradeBadge grade={s.grade} />}
              {s.score != null && (
                <span className="text-2xs font-mono text-text-muted">score={s.score.toFixed(1)}</span>
              )}
              <Badge variant={s.demo_eligible ? 'green' : 'muted'}>
                {s.demo_eligible ? 'ELIGIBLE' : 'NOT_ELIGIBLE'}
              </Badge>
            </div>
            {s.reason && (
              <p className="text-2xs text-text-muted truncate">{s.reason}</p>
            )}
          </div>
        )
      })}
    </div>
  )
}

// ── Order Flow tab
function OrderFlowTab({ card }: { card: SymbolCard }) {
  const of_ = card.order_flow
  const fmt = (v?: number | null) => v != null ? v.toFixed(5) : '—'
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2 mb-2">
        <Badge variant="observe" dot>OBSERVE ONLY</Badge>
        <span className="text-2xs text-text-muted">Order flow does not block trades</span>
      </div>
      <StatRow label="POC" value={fmt(of_.poc)} />
      <StatRow label="VAH" value={fmt(of_.vah)} />
      <StatRow label="VAL" value={fmt(of_.val)} />
      <StatRow label="VWAP" value={fmt(of_.vwap)} />
      <StatRow label="CVD slope" value={fmt(of_.cvd_slope)} />
      <StatRow label="Delta" value={fmt(of_.delta)} />
      <StatRow label="Divergence" value={of_.divergence ?? '—'} />
      <StatRow label="OF score" value={of_.order_flow_score != null ? of_.order_flow_score.toFixed(1) : '—'} />
      <StatRow label="Signal" value={<DecisionBadge decision={of_.signal} />} mono={false} />
      <StatRow label="Status" value={<DecisionBadge decision={of_.status} />} mono={false} />
    </div>
  )
}

// ── Symbol Card
function SymbolCardComponent({ card }: { card: SymbolCard }) {
  const [tab, setTab] = useState<Tab>('Confirmations')

  const badge = card.state_badge
  const badgeVariant = badge === 'LIVE' ? 'live' :
                       badge === 'BLOCK' ? 'block' :
                       badge === 'OBSERVE_ONLY' ? 'observe' :
                       badge === 'STALE' ? 'stale' :
                       badge === 'NO_DATA' ? 'no-data' : 'wait'

  return (
    <div className="card flex flex-col h-full">
      {/* Header */}
      <div className="card-header">
        <div className="flex items-center gap-2">
          <span className="text-base font-mono font-bold text-text-primary">{card.symbol}</span>
          <Badge variant={badgeVariant} dot={badge === 'LIVE'}>{badge}</Badge>
          {card.mode === 'OBSERVE_ONLY' && <Badge variant="observe">OBSERVE</Badge>}
        </div>
        <div className="flex items-center gap-3">
          {card.price != null ? (
            <span className="text-lg font-mono font-bold tabular-nums text-text-primary">
              {card.price.toFixed(card.symbol.startsWith('BTC') ? 1 : card.symbol.startsWith('GOLD') ? 2 : 5)}
            </span>
          ) : (
            <span className="text-text-muted font-mono">—</span>
          )}
          {card.spread != null && (
            <Badge variant={card.spread_status === 'OK' ? 'muted' : 'block'}>
              {card.spread.toFixed(1)} pts
            </Badge>
          )}
        </div>
      </div>

      {/* Symbol meta row */}
      <div className="px-4 py-2 border-b border-border flex items-center gap-3 flex-wrap text-2xs">
        <span className="text-text-muted">Session: <span className="text-text-primary font-mono">{card.session ?? '—'}</span></span>
        <span className="text-text-muted">Gate: <span className="text-text-primary font-mono">{card.time_gate ?? '—'}</span></span>
        <span className="text-text-muted">Decision: </span>
        <DecisionBadge decision={card.latest_decision} />
        {card.latest_reason && (
          <span className="text-text-muted truncate max-w-xs">{card.latest_reason}</span>
        )}
      </div>

      {/* Chart */}
      <div className="p-3 border-b border-border">
        <LiveCandleChart symbol={card.symbol} cardCandles={card.candles} />
      </div>

      {/* Tabs */}
      <div className="flex border-b border-border">
        {TABS.map(t => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`flex-1 px-2 py-2 text-2xs font-medium transition-colors ${
              tab === t
                ? 'text-accent-green border-b-2 border-accent-green bg-accent-green/5'
                : 'text-text-muted hover:text-text-primary'
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div className="p-3 flex-1 overflow-y-auto min-h-0 max-h-64">
        {tab === 'Confirmations' && <ConfirmationsTab card={card} />}
        {tab === 'Plan' && <PlanTab card={card} />}
        {tab === 'Levels' && <LevelsTab card={card} />}
        {tab === 'Strategy' && <StrategyTab card={card} />}
        {tab === 'Order Flow' && <OrderFlowTab card={card} />}
      </div>
    </div>
  )
}

// ── Main Quad Terminal page
export function QuadTerminal() {
  const { data, loading, error } = useQuadTerminal()

  const SYMBOLS = ['BTCUSD#', 'GOLD#', 'EURUSD', 'US100Cash#']

  return (
    <div className="h-full flex flex-col">
      <div className="px-6 py-3 border-b border-border flex items-center justify-between flex-shrink-0">
        <div>
          <h1 className="text-lg font-semibold text-text-primary">Quad Terminal</h1>
          <p className="text-xs text-text-muted">2×2 live symbol cockpit • READ ONLY</p>
        </div>
        <div className="flex gap-2 items-center">
          <Badge variant="observe">US100Cash# OBSERVE ONLY</Badge>
          <Badge variant="demo">DEMO MODE</Badge>
        </div>
      </div>

      {error && !data && (
        <div className="p-6">
          <div className="p-4 rounded bg-accent-red/10 border border-accent-red/20 text-accent-red text-xs font-mono">
            Backend unavailable: {error}. Start python -m app.main first.
          </div>
        </div>
      )}

      <div className="flex-1 overflow-auto p-4">
        <div className="grid grid-cols-2 gap-4 h-full min-h-0" style={{ gridTemplateRows: '1fr 1fr' }}>
          {SYMBOLS.map(sym => {
            const card = data?.cards?.[sym]
            if (!card) {
              return (
                <div key={sym} className="card flex items-center justify-center">
                  {loading ? (
                    <div className="flex flex-col items-center gap-2">
                      <div className="w-6 h-6 border-2 border-accent-green/30 border-t-accent-green rounded-full animate-spin" />
                      <span className="text-xs font-mono text-text-muted">Loading {sym}…</span>
                    </div>
                  ) : (
                    <NoDataState label={sym} sub="Waiting for backend data" />
                  )}
                </div>
              )
            }
            return <SymbolCardComponent key={sym} card={card} />
          })}
        </div>
      </div>
    </div>
  )
}
