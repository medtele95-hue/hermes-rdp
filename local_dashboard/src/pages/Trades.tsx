import { useTrades } from '../api/usePolling'
import { Badge, DirectionBadge } from '../components/ui/Badge'
import { NoDataState, Skeleton } from '../components/ui/Skeleton'
import type { Trade } from '../api/types'

function TradeRow({ trade, closed }: { trade: Trade; closed?: boolean }) {
  const pnl = trade.pnl
  return (
    <tr className="hover:bg-surface-2 transition-colors">
      <td className="table-cell font-mono">{trade.symbol || '—'}</td>
      <td className="table-cell font-mono text-text-muted">{trade.ticket ?? '—'}</td>
      <td className="table-cell font-mono text-2xs text-text-muted">{trade.strategy ?? '—'}</td>
      <td className="table-cell"><DirectionBadge direction={trade.direction} /></td>
      <td className="table-cell font-mono tabular-nums">
        {trade.entry != null ? trade.entry.toFixed(5) : '—'}
      </td>
      {closed && (
        <td className="table-cell font-mono tabular-nums">
          {(trade as Trade & { exit_price?: number }).exit_price != null
            ? ((trade as Trade & { exit_price?: number }).exit_price as number).toFixed(5)
            : '—'}
        </td>
      )}
      <td className="table-cell font-mono tabular-nums">
        {trade.sl != null ? trade.sl.toFixed(5) : '—'}
      </td>
      <td className="table-cell font-mono tabular-nums">
        {trade.tp != null ? trade.tp.toFixed(5) : '—'}
      </td>
      <td className="table-cell">
        {pnl != null ? (
          <span className={`font-mono tabular-nums font-medium ${pnl >= 0 ? 'text-accent-green' : 'text-accent-red'}`}>
            {pnl >= 0 ? '+' : ''}{pnl.toFixed(2)}
          </span>
        ) : '—'}
      </td>
      <td className="table-cell font-mono text-2xs text-text-muted">{trade.lot ?? '—'}</td>
      <td className="table-cell text-2xs text-text-muted font-mono">
        {trade.timestamp ? new Date(trade.timestamp).toLocaleTimeString() : '—'}
      </td>
      <td className="table-cell text-2xs text-text-muted max-w-xs truncate">{trade.reason ?? trade.comment ?? '—'}</td>
    </tr>
  )
}

export function Trades() {
  const { data, loading, error } = useTrades()

  const openTrades = data?.open_trades ?? []
  const closedTrades = data?.closed_trades ?? []
  const closedSource = data?.closed_source ?? 'UNKNOWN'
  const openSource = data?.open_source ?? 'UNKNOWN'

  const openHeaders = ['Symbol', 'Ticket', 'Strategy', 'Dir', 'Entry', 'SL', 'TP', 'PnL', 'Lot', 'Time', 'Note']
  const closedHeaders = ['Symbol', 'Ticket', 'Strategy', 'Dir', 'Entry', 'Exit', 'SL', 'TP', 'PnL', 'Lot', 'Time', 'Note']

  return (
    <div className="p-6 space-y-6 max-w-screen-2xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Trades / Journal</h1>
          <p className="text-xs text-text-muted">DEMO trades only — read-only view</p>
        </div>
        <div className="flex gap-2">
          <Badge variant="demo">DEMO ONLY</Badge>
          <Badge variant="muted">{openTrades.length} open · {closedTrades.length} recent</Badge>
        </div>
      </div>

      {closedSource !== 'MT5_HISTORY_DEALS' && (
        <div className="px-3 py-2 rounded bg-accent-amber/10 border border-accent-amber/20 text-xs font-mono text-accent-amber">
          Closed trades are using fallback memory, MT5 history unavailable.
        </div>
      )}

      {data?.note && (
        <div className="px-3 py-2 rounded bg-surface-3 border border-border text-xs font-mono text-text-muted">
          {data.note}
        </div>
      )}

      {error && !data && (
        <div className="px-3 py-2 rounded bg-accent-red/10 border border-accent-red/20 text-xs font-mono text-accent-red">
          {error}
        </div>
      )}

      {/* Open trades */}
      <div>
        <div className="flex items-center gap-2 mb-2">
          <h2 className="text-sm font-semibold text-text-primary">Open Positions</h2>
          <Badge variant={openTrades.length > 0 ? 'live' : 'muted'}>{openTrades.length}</Badge>
          <Badge variant="muted">{openSource}</Badge>
        </div>
        <div className="card overflow-x-auto">
          <table className="w-full min-w-[700px]">
            <thead>
              <tr className="bg-surface-2">
                {openHeaders.map(h => <th key={h} className="table-head text-left">{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {loading && !data ? (
                Array.from({ length: 2 }).map((_, i) => (
                  <tr key={i}>
                    {openHeaders.map((_, j) => (
                      <td key={j} className="table-cell"><Skeleton className="h-3 w-full" /></td>
                    ))}
                  </tr>
                ))
              ) : openTrades.length === 0 ? (
                <tr>
                  <td colSpan={openHeaders.length} className="py-8">
                    <NoDataState label="NO OPEN TRADES" sub="No open HERMES demo positions" />
                  </td>
                </tr>
              ) : openTrades.map((t, i) => <TradeRow key={i} trade={t} />)}
            </tbody>
          </table>
        </div>
      </div>

      {/* Closed trades */}
      <div>
        <div className="flex items-center gap-2 mb-2">
          <h2 className="text-sm font-semibold text-text-primary">Recent Closed Trades</h2>
          <Badge variant="muted">{closedTrades.length}</Badge>
          <Badge variant={closedSource === 'MT5_HISTORY_DEALS' ? 'blue' : 'amber'}>{closedSource}</Badge>
          {data?.pnl_source && <Badge variant="muted">PnL {data.pnl_source}</Badge>}
        </div>
        <div className="card overflow-x-auto">
          <table className="w-full min-w-[800px]">
            <thead>
              <tr className="bg-surface-2">
                {closedHeaders.map(h => <th key={h} className="table-head text-left">{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {loading && !data ? (
                Array.from({ length: 3 }).map((_, i) => (
                  <tr key={i}>
                    {closedHeaders.map((_, j) => (
                      <td key={j} className="table-cell"><Skeleton className="h-3 w-full" /></td>
                    ))}
                  </tr>
                ))
              ) : closedTrades.length === 0 ? (
                <tr>
                  <td colSpan={closedHeaders.length} className="py-8">
                    <NoDataState label="NO RECENT TRADES" sub="No recent closed trades in memory" />
                  </td>
                </tr>
              ) : closedTrades.map((t, i) => <TradeRow key={i} trade={t} closed />)}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
