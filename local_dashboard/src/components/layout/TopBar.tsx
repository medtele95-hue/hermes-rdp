import { useHealth } from '../../api/usePolling'
import { Badge } from '../ui/Badge'
import { Num } from '../ui/StatRow'

function HeartbeatAge({ ageSeconds }: { ageSeconds: number | null }) {
  if (ageSeconds === null) return <span className="text-text-muted text-xs font-mono">—</span>
  const color =
    ageSeconds < 10 ? 'text-accent-green' :
    ageSeconds < 30 ? 'text-accent-amber' :
    'text-accent-red'
  return (
    <span className={`text-xs font-mono tabular-nums ${color}`}>
      {ageSeconds.toFixed(0)}s ago
    </span>
  )
}

export function TopBar() {
  const { data } = useHealth()

  const backendStatus = data?.backend_status ?? 'STARTING'
  const mt5 = data?.mt5_connected ?? false
  const equity = data?.account_equity
  const session = data?.session_name ?? '—'

  return (
    <header className="fixed top-0 left-64 right-0 z-40 h-12 bg-surface-1 border-b border-border flex items-center px-4 gap-4">
      {/* Backend status */}
      <div className="flex items-center gap-2">
        <span className="text-2xs text-text-muted uppercase tracking-wider">Backend</span>
        {backendStatus === 'LIVE' ? (
          <Badge variant="live" dot>LIVE</Badge>
        ) : backendStatus === 'STALE' ? (
          <Badge variant="stale" dot>STALE</Badge>
        ) : (
          <Badge variant="muted" dot>STARTING</Badge>
        )}
        <HeartbeatAge ageSeconds={data?.heartbeat_age_seconds ?? null} />
      </div>

      <div className="h-4 w-px bg-border" />

      {/* MT5 */}
      <div className="flex items-center gap-2">
        <span className="text-2xs text-text-muted uppercase tracking-wider">MT5</span>
        {mt5 ? (
          <Badge variant="live" dot>CONNECTED</Badge>
        ) : (
          <Badge variant="block" dot>DISCONNECTED</Badge>
        )}
      </div>

      <div className="h-4 w-px bg-border" />

      {/* Session */}
      <div className="flex items-center gap-2">
        <span className="text-2xs text-text-muted uppercase tracking-wider">Session</span>
        <span className="text-xs font-mono text-text-primary">{session}</span>
      </div>

      <div className="h-4 w-px bg-border" />

      {/* Cycle */}
      <div className="flex items-center gap-2">
        <span className="text-2xs text-text-muted uppercase tracking-wider">Cycle</span>
        <span className="text-xs font-mono text-text-primary">{data?.cycle_status ?? '—'}</span>
      </div>

      {/* Spacer */}
      <div className="flex-1" />

      {/* Account equity */}
      {equity != null && (
        <div className="flex items-center gap-2">
          <span className="text-2xs text-text-muted uppercase tracking-wider">Equity</span>
          <span className="text-sm font-mono font-semibold text-text-primary tabular-nums">
            <Num value={equity} decimals={2} prefix="$" />
          </span>
        </div>
      )}

      {/* Safety badges — always visible */}
      <div className="flex items-center gap-2">
        <Badge variant="demo">DEMO ONLY</Badge>
        <Badge variant="block">LIVE DISABLED</Badge>
      </div>
    </header>
  )
}
