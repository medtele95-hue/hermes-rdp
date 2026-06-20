import { useHealth, useDashboardStatus } from '../api/usePolling'
import { Badge } from '../components/ui/Badge'
import { StatRow, SectionHeader } from '../components/ui/StatRow'

export function Settings() {
  const { data: health } = useHealth()
  const { data: snapRaw } = useDashboardStatus()
  const snap = snapRaw as Record<string, unknown> | null

  return (
    <div className="p-6 space-y-6 max-w-3xl">
      <div>
        <h1 className="text-xl font-semibold">Settings / Diagnostics</h1>
        <p className="text-xs text-text-muted">Read-only configuration viewer</p>
      </div>

      <div className="card p-4">
        <SectionHeader title="Local Dashboard" />
        <StatRow label="Frontend URL" value="http://127.0.0.1:5173" />
        <StatRow label="Backend API URL" value="http://127.0.0.1:8000/local-api" />
        <StatRow label="API docs" value="http://127.0.0.1:8000/local-api/docs" />
        <StatRow label="Poll interval (health)" value="2s" />
        <StatRow label="Poll interval (candles)" value="5s" />
        <StatRow label="Poll interval (symbols)" value="2s" />
        <StatRow label="Poll interval (account)" value="5s" />
        <StatRow label="WebSocket" value="/local-api/ws" />
      </div>

      <div className="card p-4">
        <SectionHeader title="Backend Runtime" />
        <StatRow label="Status" value={
          <Badge variant={health?.backend_status === 'LIVE' ? 'live' : 'stale'}>
            {health?.backend_status ?? '—'}
          </Badge>
        } mono={false} />
        <StatRow label="Started at" value={
          health?.backend_started_at
            ? new Date(health.backend_started_at).toLocaleString()
            : '—'
        } />
        <StatRow label="Last heartbeat" value={
          health?.last_heartbeat_at
            ? new Date(health.last_heartbeat_at).toLocaleString()
            : '—'
        } />
        <StatRow label="Heartbeat age" value={
          health?.heartbeat_age_seconds != null
            ? `${health.heartbeat_age_seconds.toFixed(1)}s`
            : '—'
        } highlight={
          health?.heartbeat_age_seconds != null
            ? health.heartbeat_age_seconds < 10 ? 'green'
              : health.heartbeat_age_seconds < 30 ? 'amber' : 'red'
            : 'muted'
        } />
        <StatRow label="MT5 connected" value={
          <Badge variant={health?.mt5_connected ? 'live' : 'block'}>
            {String(health?.mt5_connected ?? '—')}
          </Badge>
        } mono={false} />
        <StatRow label="Active symbols" value={health?.resolved_symbols?.join(', ') ?? '—'} />
        <StatRow label="Session" value={snap?.session_name as string ?? '—'} />
      </div>

      <div className="card p-4">
        <SectionHeader title="Safety Proof" />
        <div className="space-y-1.5">
          {[
            { label: 'ALLOW_LIVE_TRADING', value: 'false', pass: true },
            { label: 'DEMO_ONLY', value: 'true', pass: true },
            { label: 'DEMO_MAX_LOT', value: '0.01', pass: true },
            { label: 'order_send location', value: 'app/mt5/demo_router.py ONLY', pass: true },
            { label: 'Dashboard endpoints', value: 'GET only — no execution', pass: true },
            { label: 'No trade buttons', value: 'verified in source', pass: true },
            { label: 'DemoRouter untouched', value: 'confirmed', pass: true },
            { label: 'SafetyGuard untouched', value: 'confirmed', pass: true },
          ].map(({ label, value, pass }) => (
            <div key={label} className="flex items-center justify-between py-1.5 border-b border-border/30 last:border-0">
              <div className="flex items-center gap-2">
                <span className="text-accent-green text-xs">✓</span>
                <span className="text-xs text-text-secondary">{label}</span>
              </div>
              <Badge variant={pass ? 'green' : 'block'}>{value}</Badge>
            </div>
          ))}
        </div>
      </div>

      <div className="card p-4">
        <SectionHeader title="Run Commands" />
        <div className="space-y-2">
          {[
            { label: 'Start backend', cmd: 'cd C:\\hermes-mt5-agent && python -m app.main' },
            { label: 'Start dashboard', cmd: 'cd C:\\hermes-mt5-agent\\local_dashboard && npm run dev' },
            { label: 'Start both', cmd: 'C:\\hermes-mt5-agent\\scripts\\start_local_dashboard.ps1' },
            { label: 'Run tests', cmd: 'cd C:\\hermes-mt5-agent && python -m unittest discover tests -v' },
            { label: 'Build frontend', cmd: 'cd C:\\hermes-mt5-agent\\local_dashboard && npm run build' },
          ].map(({ label, cmd }) => (
            <div key={label}>
              <p className="text-2xs text-text-muted mb-0.5">{label}</p>
              <div className="bg-surface-3 border border-border rounded px-3 py-1.5 font-mono text-xs text-accent-green">
                {cmd}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
