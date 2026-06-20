import { useOrderFlow } from '../api/usePolling'
import { Badge, DecisionBadge } from '../components/ui/Badge'
import { StatRow, Num, SectionHeader } from '../components/ui/StatRow'
import { NoDataState } from '../components/ui/Skeleton'
import type { OrderFlowSnapshot } from '../api/types'

const QUAD_SYMBOLS = ['BTCUSD#', 'GOLD#', 'EURUSD', 'US100Cash#']

function OFPanel({ symbol, snap }: { symbol: string; snap: OrderFlowSnapshot | undefined }) {
  const fmt5 = (v?: number | null) => v != null ? v.toFixed(5) : '—'
  const hasData = snap && snap.status !== 'NO_DATA'

  return (
    <div className="card p-4">
      <div className="card-header -mx-4 -mt-4 mb-3 px-4 pt-3 pb-2.5">
        <div className="flex items-center gap-2">
          <span className="font-mono font-bold text-sm text-text-primary">{symbol}</span>
          <Badge variant="observe" dot>OBSERVE ONLY</Badge>
          {symbol === 'US100Cash#' && (
            <Badge variant="muted">US100Cash# — observe mode only</Badge>
          )}
        </div>
        {snap && (
          <DecisionBadge decision={snap.latest_decision ?? snap.signal} />
        )}
      </div>

      {!hasData ? (
        <NoDataState
          label={symbol === 'US100Cash#' ? 'OBSERVE_ONLY — No OF payload yet' : 'NO_DATA'}
          sub={
            symbol === 'US100Cash#'
              ? 'US100Cash# order flow is mapped from US100CASH# key'
              : 'No order flow snapshot available. Waiting for backend cycle.'
          }
        />
      ) : (
        <div className="grid grid-cols-2 gap-x-6">
          <div>
            <SectionHeader title="Volume Profile" />
            <StatRow label="POC" value={fmt5(snap.poc)} />
            <StatRow label="VAH" value={fmt5(snap.vah)} />
            <StatRow label="VAL" value={fmt5(snap.val)} />
            <StatRow label="VWAP" value={fmt5(snap.vwap)} />
          </div>
          <div>
            <SectionHeader title="Flow Metrics" />
            <StatRow label="CVD slope" value={snap.cvd_slope != null ? snap.cvd_slope.toFixed(4) : '—'} />
            <StatRow label="Delta" value={snap.delta != null ? snap.delta.toFixed(2) : '—'} />
            <StatRow label="Divergence" value={snap.divergence ?? '—'} />
            <StatRow label="OF score" value={
              snap.order_flow_score != null ? snap.order_flow_score.toFixed(1) : '—'
            } />
          </div>
          <div className="col-span-2 border-t border-border/40 mt-2 pt-2">
            <SectionHeader title="Status" />
            <div className="flex gap-3 flex-wrap">
              <div className="flex items-center gap-1.5">
                <span className="text-xs text-text-muted">Signal:</span>
                <DecisionBadge decision={snap.signal} />
              </div>
              <div className="flex items-center gap-1.5">
                <span className="text-xs text-text-muted">Status:</span>
                <Badge variant={snap.status === 'ACTIVE' ? 'live' : 'muted'}>{snap.status ?? '—'}</Badge>
              </div>
              {snap.reader_status && (
                <div className="flex items-center gap-1.5">
                  <span className="text-xs text-text-muted">Reader:</span>
                  <Badge variant="observe">{snap.reader_status}</Badge>
                </div>
              )}
              {snap.execution_agent_status && (
                <div className="flex items-center gap-1.5">
                  <span className="text-xs text-text-muted">Exec agent:</span>
                  <Badge variant="muted">{snap.execution_agent_status}</Badge>
                </div>
              )}
            </div>
            {snap.latest_reason && (
              <div className="mt-2 text-xs text-text-muted font-mono">{snap.latest_reason}</div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

export function OrderFlowPage() {
  const { data, loading, error } = useOrderFlow()
  const snaps = (data?.snapshots ?? {}) as Record<string, OrderFlowSnapshot>

  return (
    <div className="p-6 space-y-4 max-w-screen-2xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Order Flow</h1>
          <p className="text-xs text-text-muted">Volume profile & flow intelligence — observe only</p>
        </div>
        <Badge variant="observe">ORDER FLOW = OBSERVE ONLY</Badge>
      </div>

      {data?.order_flow_badge && (
        <div className="px-3 py-2 rounded bg-accent-purple/5 border border-accent-purple/20 text-accent-purple text-xs font-mono">
          {data.order_flow_badge}
        </div>
      )}

      {error && !data && (
        <div className="p-3 rounded bg-accent-red/10 border border-accent-red/20 text-accent-red text-xs font-mono">
          {error}
        </div>
      )}

      <div className="grid grid-cols-2 gap-4">
        {QUAD_SYMBOLS.map(sym => (
          <OFPanel key={sym} symbol={sym} snap={snaps[sym]} />
        ))}
      </div>

      {/* US100 mapping note */}
      <div className="card p-3 text-xs">
        <SectionHeader title="US100Cash# Symbol Mapping" />
        <div className="text-text-muted space-y-0.5">
          <p>US100CASH → US100Cash# (normalized)</p>
          <p>US100CASH# → US100Cash# (normalized)</p>
          <p>Order flow emitted under any US100 key is shown in the US100Cash# panel above.</p>
          <p className="text-accent-purple mt-1">US100Cash# remains OBSERVE_ONLY regardless of order flow signal.</p>
        </div>
      </div>
    </div>
  )
}
