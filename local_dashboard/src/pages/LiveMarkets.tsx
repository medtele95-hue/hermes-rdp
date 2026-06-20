import { useSymbols } from '../api/usePolling'
import { Badge, StateBadge, DecisionBadge, FreshnessBadge } from '../components/ui/Badge'
import { Skeleton, NoDataState } from '../components/ui/Skeleton'
import type { SymbolState } from '../api/types'

function routeBadge(route: string) {
  if (route === 'ROUTE_TO_DEMO') return <Badge variant="live" dot>ROUTE TO DEMO</Badge>
  if (route?.includes('BLOCK')) return <Badge variant="block">{route}</Badge>
  if (route?.includes('OBSERVE')) return <Badge variant="observe">{route}</Badge>
  return <Badge variant="wait">{route || 'WAIT'}</Badge>
}

export function LiveMarkets() {
  const { data, loading, error } = useSymbols()
  const symbols = Object.values(data?.symbols ?? {})

  return (
    <div className="p-6 space-y-4 max-w-screen-2xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-text-primary">Live Markets</h1>
          <p className="text-xs text-text-muted">All active symbols — read-only</p>
        </div>
        <Badge variant="muted">{symbols.length} symbols</Badge>
      </div>

      {error && !data && (
        <div className="p-3 rounded bg-accent-red/10 border border-accent-red/20 text-accent-red text-xs font-mono">
          {error}
        </div>
      )}

      <div className="card overflow-hidden">
        <table className="w-full">
          <thead>
            <tr className="bg-surface-2">
              {['Symbol', 'Price', 'Spread', 'Spread Status', 'Session', 'Time Gate',
                'Decision', 'Reason', 'Route', 'Freshness', 'Last Update'].map(h => (
                <th key={h} className="table-head text-left">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading && !data ? (
              Array.from({ length: 4 }).map((_, i) => (
                <tr key={i}>
                  {Array.from({ length: 11 }).map((_, j) => (
                    <td key={j} className="table-cell">
                      <Skeleton className="h-3 w-full" />
                    </td>
                  ))}
                </tr>
              ))
            ) : symbols.length === 0 ? (
              <tr>
                <td colSpan={11} className="py-12">
                  <NoDataState label="NO_SYMBOLS" sub="Backend hasn't reported symbol state yet" />
                </td>
              </tr>
            ) : symbols.map((sym: SymbolState) => (
              <tr key={sym.symbol} className="hover:bg-surface-2 transition-colors">
                <td className="table-cell">
                  <span className="font-mono font-medium text-text-primary">{sym.symbol}</span>
                </td>
                <td className="table-cell">
                  <span className="font-mono tabular-nums text-text-primary">
                    {sym.price != null ? sym.price.toFixed(5) : '—'}
                  </span>
                </td>
                <td className="table-cell">
                  <span className="font-mono tabular-nums">
                    {sym.spread != null ? sym.spread.toFixed(1) : '—'}
                  </span>
                </td>
                <td className="table-cell">
                  <Badge variant={sym.spread_status === 'OK' ? 'green' : 'block'}>
                    {sym.spread_status ?? '—'}
                  </Badge>
                </td>
                <td className="table-cell text-xs text-text-secondary">{sym.session ?? '—'}</td>
                <td className="table-cell">
                  <Badge variant={sym.time_gate?.includes('OK') || sym.time_gate?.includes('PASS') ? 'green' : 'amber'}>
                    {sym.time_gate ?? '—'}
                  </Badge>
                </td>
                <td className="table-cell">
                  <DecisionBadge decision={sym.latest_decision} />
                </td>
                <td className="table-cell max-w-xs">
                  <span className="text-xs text-text-muted truncate block">{sym.latest_reason ?? '—'}</span>
                </td>
                <td className="table-cell">
                  {routeBadge(sym.route_status)}
                </td>
                <td className="table-cell">
                  <FreshnessBadge freshness={sym.data_freshness} />
                </td>
                <td className="table-cell text-2xs text-text-muted font-mono">
                  {sym.last_update_utc
                    ? new Date(sym.last_update_utc).toLocaleTimeString()
                    : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
