import { useStrategies } from '../api/usePolling'
import { Badge, DirectionBadge, GradeBadge } from '../components/ui/Badge'
import { NoDataState, Skeleton } from '../components/ui/Skeleton'

const KNOWN_STRATEGIES = [
  'ORDER_FLOW_READER',
  'ORDER_FLOW_EXECUTION_AGENT',
  'FIB_CONFLUENCE_EXECUTION_AGENT',
  'BTC_SCALPING_AGENT',
  'GOLD_LIQUIDITY_HUNTER_PRO',
  'GOLD_M1_M5_EMA_SWEEP_SCALPER',
  'EUR_EMA_RSI_ATR_CROSSOVER',
  'SIMO_ATM_BREAKOUT',
  'HERMES_STRATEGY_PACK_AGENT',
  'GOLD_ORDER_FLOW_CVD_VWAP',
  'QUANT_PRO_REGIME_SWITCHING',
]

const QUAD_SYMBOLS = ['BTCUSD#', 'GOLD#', 'EURUSD', 'US100Cash#']

function decisionStyle(dec?: string | null) {
  if (!dec) return 'muted'
  const d = dec.toUpperCase()
  if (d === 'BUY' || d === 'SELL' || d === 'PASS' || d === 'ROUTE_TO_DEMO') return 'live'
  if (d === 'BLOCK' || d === 'REJECT') return 'block'
  if (d === 'WAIT') return 'wait'
  if (d.includes('OBSERVE')) return 'observe'
  return 'muted'
}

function CellContent({ signal }: { signal: Record<string, unknown> | undefined }) {
  if (!signal) {
    return <span className="text-text-muted text-2xs font-mono">—</span>
  }
  const dec = signal.decision as string | undefined
  const dir = signal.direction as string | undefined
  const score = signal.score as number | undefined
  const grade = signal.grade as string | undefined
  const reason = signal.reason as string | undefined
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-1 flex-wrap">
        <Badge variant={decisionStyle(dec)}>{dec ?? '?'}</Badge>
        {dir && dir !== 'WAIT' && <DirectionBadge direction={dir} />}
        {grade && <GradeBadge grade={grade} />}
      </div>
      {score != null && (
        <div className="text-2xs font-mono text-text-muted">
          score={score.toFixed(1)}
        </div>
      )}
      {reason && (
        <div className="text-2xs text-text-muted truncate max-w-[120px]" title={reason}>
          {reason}
        </div>
      )}
    </div>
  )
}

export function StrategyEngine() {
  const { data, loading } = useStrategies()

  const bySymbol = (data?.by_symbol ?? {}) as Record<string, Array<Record<string, unknown>>>

  // Build matrix: strategy → symbol → signal
  const matrix: Record<string, Record<string, Record<string, unknown>>> = {}
  KNOWN_STRATEGIES.forEach(s => { matrix[s] = {} })

  QUAD_SYMBOLS.forEach(sym => {
    const cleanSym = sym.replace('#', '').toUpperCase()
    // Try both sym and cleanSym as keys
    const sigs = bySymbol[sym] ?? bySymbol[cleanSym] ?? []
    sigs.forEach((sig: Record<string, unknown>) => {
      const name = String(sig.strategy ?? sig.setup_type ?? '').toUpperCase()
      if (!matrix[name]) matrix[name] = {}
      matrix[name][sym] = sig
    })
  })

  // Add any extra strategies from data
  Object.entries(bySymbol).forEach(([, sigs]) => {
    sigs.forEach((sig: Record<string, unknown>) => {
      const name = String(sig.strategy ?? sig.setup_type ?? '').toUpperCase()
      if (name && !matrix[name]) matrix[name] = {}
    })
  })

  const strategies = Object.keys(matrix).filter(s => s)

  return (
    <div className="p-6 space-y-4 max-w-screen-2xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Strategy Engine</h1>
          <p className="text-xs text-text-muted">Strategy × Symbol matrix — read-only</p>
        </div>
        <Badge variant="muted">{strategies.length} strategies</Badge>
      </div>

      <div className="card overflow-x-auto">
        <table className="w-full min-w-[800px]">
          <thead>
            <tr className="bg-surface-2">
              <th className="table-head text-left w-48">Strategy</th>
              {QUAD_SYMBOLS.map(sym => (
                <th key={sym} className="table-head text-left">{sym}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading && !data ? (
              Array.from({ length: 9 }).map((_, i) => (
                <tr key={i}>
                  {Array.from({ length: 5 }).map((_, j) => (
                    <td key={j} className="table-cell">
                      <Skeleton className="h-4 w-full" />
                    </td>
                  ))}
                </tr>
              ))
            ) : strategies.length === 0 ? (
              <tr>
                <td colSpan={5} className="py-12">
                  <NoDataState label="NO_STRATEGY_DATA" sub="Waiting for backend cycle" />
                </td>
              </tr>
            ) : strategies.map(strat => (
              <tr key={strat} className="hover:bg-surface-2 transition-colors">
                <td className="table-cell">
                  <span className="text-xs font-mono text-text-secondary">{strat}</span>
                </td>
                {QUAD_SYMBOLS.map(sym => (
                  <td key={sym} className="table-cell align-top">
                    <CellContent signal={matrix[strat]?.[sym]} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Legend */}
      <div className="flex items-center gap-3 flex-wrap">
        <span className="text-2xs text-text-muted uppercase tracking-wider">Legend:</span>
        {[
          { variant: 'live' as const, label: 'PASS / BUY / SELL' },
          { variant: 'block' as const, label: 'BLOCK / REJECT' },
          { variant: 'wait' as const, label: 'WAIT' },
          { variant: 'observe' as const, label: 'OBSERVE ONLY' },
          { variant: 'muted' as const, label: 'NO SIGNAL' },
        ].map(({ variant, label }) => (
          <div key={label} className="flex items-center gap-1.5">
            <Badge variant={variant}>{label}</Badge>
          </div>
        ))}
      </div>
    </div>
  )
}
