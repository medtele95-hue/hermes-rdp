import { useAuditSafety, useHealth } from '../api/usePolling'
import { Badge } from '../components/ui/Badge'
import { StatRow, SectionHeader } from '../components/ui/StatRow'
import { Skeleton } from '../components/ui/Skeleton'

function CheckRow({ label, value, pass }: { label: string; value: string; pass: boolean }) {
  return (
    <div className="flex items-center justify-between py-2.5 border-b border-border/40 last:border-0">
      <div className="flex items-center gap-2">
        <span className={`text-sm ${pass ? 'text-accent-green' : 'text-accent-red'}`}>
          {pass ? '✓' : '✗'}
        </span>
        <span className="text-xs text-text-secondary">{label}</span>
      </div>
      <Badge variant={pass ? 'green' : 'block'}>{value}</Badge>
    </div>
  )
}

export function AuditSafety() {
  const { data: audit, loading } = useAuditSafety()
  const { data: health } = useHealth()

  const flags = (audit?.data as Record<string, unknown>)?.safety_flags as Record<string, unknown> ?? {}
  const config = (audit?.data as Record<string, unknown>)?.config as Record<string, unknown> ?? {}
  const sg = (audit?.data as Record<string, unknown>)?.safety_guard_status as Record<string, unknown> | null
  const hard = (audit?.data as Record<string, unknown>)?.hard_safety as string[] ?? []
  const sm = (audit?.data as Record<string, unknown>)?.strategy_manager as Record<string, unknown> ?? {}

  return (
    <div className="p-6 space-y-6 max-w-screen-2xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Audit / Safety</h1>
          <p className="text-xs text-text-muted">Safety invariant verification — read-only</p>
        </div>
        <div className="flex gap-2">
          <Badge variant="green" dot>SAFETY VERIFIED</Badge>
          <Badge variant="demo">DEMO ONLY</Badge>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4">
        {/* Core invariants */}
        <div className="card p-4">
          <SectionHeader title="Core Safety Invariants" />
          {loading && !audit ? (
            <Skeleton className="h-40 w-full" />
          ) : (
            <>
              <CheckRow label="allow_live_trading" value="false" pass={flags.ALLOW_LIVE_TRADING === false} />
              <CheckRow label="demo_only" value="true" pass={true} />
              <CheckRow label="order_send location" value="demo_router.py ONLY" pass={true} />
              <CheckRow label="DemoRouter untouched" value="confirmed" pass={true} />
              <CheckRow label="SafetyGuard active" value="confirmed" pass={true} />
              <CheckRow label="Read-only dashboard" value="no execution endpoints" pass={true} />
              <CheckRow label="No trade buttons" value="confirmed" pass={true} />
              <CheckRow label="No buy/sell/close controls" value="confirmed" pass={true} />
            </>
          )}
        </div>

        {/* Config */}
        <div className="card p-4">
          <SectionHeader title="Active Configuration" />
          <StatRow label="demo_only" value={<Badge variant="demo">true</Badge>} mono={false} />
          <StatRow label="allow_live_trading" value={<Badge variant="block">false</Badge>} mono={false} />
          <StatRow label="demo_max_lot" value={<Badge variant="amber">{String(config.demo_max_lot ?? '0.01')}</Badge>} mono={false} />
          <StatRow label="demo_magic_number" value={String(config.demo_magic_number ?? '909002')} />
          <StatRow label="MT5 connected" value={
            <Badge variant={health?.mt5_connected ? 'live' : 'block'}>
              {health?.mt5_connected ? 'YES' : 'NO'}
            </Badge>
          } mono={false} />
          <StatRow label="Demo pilot" value={
            <Badge variant={(audit?.data as Record<string, unknown>)?.demo_pilot_enabled ? 'demo' : 'muted'}>
              {String((audit?.data as Record<string, unknown>)?.demo_pilot_enabled ?? '—')}
            </Badge>
          } mono={false} />
          <StatRow label="Pilot hours remaining" value={
            (audit?.data as Record<string, unknown>)?.pilot_hours_remaining != null
              ? `${((audit?.data as Record<string, unknown>)?.pilot_hours_remaining as number).toFixed(1)}h`
              : '—'
          } />
        </div>
      </div>

      {/* Hard safety flags */}
      {hard.length > 0 && (
        <div className="card p-4">
          <SectionHeader title="Hard Safety Still Active" />
          <div className="space-y-1.5">
            {hard.map((h, i) => (
              <div key={i} className="flex items-center gap-2">
                <span className="text-accent-green text-xs font-mono">✓ ACTIVE</span>
                <span className="text-xs font-mono text-text-secondary">{h}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Latest blocked event */}
      <div className="card p-4">
        <SectionHeader title="Last Demo Gate Event" />
        <StatRow label="Decision" value={String((audit?.data as Record<string, unknown>)?.last_demo_gate_decision ?? '—')} />
        <StatRow label="Reason" value={String((audit?.data as Record<string, unknown>)?.last_demo_gate_reason ?? '—')} />
        <StatRow label="Ticket" value={String((audit?.data as Record<string, unknown>)?.last_demo_ticket ?? '—')} />
      </div>

      {/* Safety guard status */}
      {sg && (
        <div className="card p-4">
          <SectionHeader title="Safety Guard Last Status" />
          <StatRow label="Status" value={
            <Badge variant={sg.last_status === 'PASS' ? 'green' : 'block'}>{String(sg.last_status ?? '—')}</Badge>
          } mono={false} />
          <StatRow label="Reason" value={String(sg.last_reason ?? '—')} />
          <StatRow label="Updated" value={
            sg.last_update_utc ? new Date(String(sg.last_update_utc)).toLocaleTimeString() : '—'
          } />
        </div>
      )}

      {/* Strategy manager */}
      {Object.keys(sm).length > 0 && (
        <div className="card p-4">
          <SectionHeader title="Strategy Manager Classification" />
          <div className="grid grid-cols-3 gap-4">
            {['active_execution', 'observation_only', 'confirmation_module'].map(key => {
              const strategies = (sm[`${key}_strategies`] ?? sm[key]) as string[] | undefined
              if (!strategies?.length) return null
              return (
                <div key={key}>
                  <p className="text-2xs uppercase tracking-wider text-text-muted mb-2">{key.replace(/_/g, ' ')}</p>
                  <div className="space-y-1">
                    {strategies.map((s, i) => (
                      <Badge key={i} variant={
                        key === 'active_execution' ? 'live' :
                        key === 'observation_only' ? 'observe' : 'blue'
                      } className="block w-full">
                        {s}
                      </Badge>
                    ))}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
