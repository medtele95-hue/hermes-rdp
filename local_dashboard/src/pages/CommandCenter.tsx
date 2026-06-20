import { useHealth, useDashboardStatus, useAccountSnapshot, useSetupHunter } from '../api/usePolling'
import { StatRow, SectionHeader, Num } from '../components/ui/StatRow'
import { Badge, StateBadge, DecisionBadge, DirectionBadge } from '../components/ui/Badge'
import { Skeleton, SkeletonCard, ErrorState, NoDataState } from '../components/ui/Skeleton'

function PnlValue({ v }: { v?: number | null }) {
  if (v == null) return <span className="text-text-muted font-mono">—</span>
  return (
    <span className={`font-mono tabular-nums font-medium ${v >= 0 ? 'text-accent-green' : 'text-accent-red'}`}>
      {v >= 0 ? '+' : ''}{v.toFixed(2)}
    </span>
  )
}

export function CommandCenter() {
  const { data: health, loading: hLoading } = useHealth()
  const { data: snapRaw, loading: sLoading } = useDashboardStatus()
  const { data: account, loading: aLoading } = useAccountSnapshot()
  const { data: hunter } = useSetupHunter()

  const snap = snapRaw as Record<string, unknown> | null
  const loading = hLoading || sLoading || aLoading

  const cycleStatus = (snap?.cycle_status as Record<string, unknown>) ?? {}
  const symbolsBlock = (snap?.symbols as Record<string, unknown>) ?? {}
  const setupHunterBlock = (snap?.setup_hunter as Record<string, unknown>) ?? {}
  const safetyGuard = (snap?.safety_guard as Record<string, unknown>) ?? {}
  const bestCand = hunter?.best_candidate

  return (
    <div className="p-6 space-y-6 max-w-screen-2xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-text-primary">Command Center</h1>
          <p className="text-xs text-text-muted mt-0.5">Executive overview — read-only</p>
        </div>
        <div className="flex gap-2">
          <Badge variant="demo">DEMO ONLY</Badge>
          <Badge variant="block">LIVE TRADING DISABLED</Badge>
        </div>
      </div>

      <div className="grid grid-cols-4 gap-4">
        {/* Backend & MT5 */}
        <div className="card p-4 space-y-1">
          <SectionHeader title="System Status" />
          <StatRow label="Backend" value={
            loading ? <Skeleton className="h-4 w-16" /> :
            <StateBadge badge={health?.backend_status ?? 'STARTING'} />
          } mono={false} />
          <StatRow label="MT5" value={
            <Badge variant={health?.mt5_connected ? 'live' : 'block'} dot>
              {health?.mt5_connected ? 'Connected' : 'Disconnected'}
            </Badge>
          } mono={false} />
          <StatRow label="Heartbeat age" value={
            health?.heartbeat_age_seconds != null
              ? `${health.heartbeat_age_seconds.toFixed(0)}s`
              : '—'
          } highlight={
            health?.heartbeat_age_seconds != null ?
              health.heartbeat_age_seconds < 10 ? 'green' :
              health.heartbeat_age_seconds < 30 ? 'amber' : 'red'
            : 'muted'
          } />
          <StatRow label="Session" value={snap?.session_name as string ?? '—'} />
          <StatRow label="Backend started" value={
            health?.backend_started_at
              ? new Date(health.backend_started_at).toLocaleTimeString()
              : '—'
          } />
        </div>

        {/* Account */}
        <div className="card p-4 space-y-1">
          <SectionHeader title="Account" />
          <StatRow label="Balance" value={<Num value={account?.balance} prefix="$" />} />
          <StatRow label="Equity" value={<Num value={account?.equity} prefix="$" />} />
          <StatRow label="Margin" value={<Num value={account?.margin} prefix="$" />} />
          <StatRow label="Free margin" value={<Num value={account?.free_margin} prefix="$" />} />
          <StatRow label="Floating PnL" value={<PnlValue v={account?.floating_pnl} />} mono={false} />
          <StatRow label="Closed PnL today" value={<PnlValue v={account?.closed_pnl_today} />} mono={false} />
        </div>

        {/* Safety */}
        <div className="card p-4 space-y-1">
          <SectionHeader title="Safety State" />
          <StatRow label="Mode" value={<Badge variant="demo">{snap?.mode as string ?? 'DEMO_ONLY'}</Badge>} mono={false} />
          <StatRow label="DEMO_ONLY" value={<Badge variant="green">true</Badge>} mono={false} />
          <StatRow label="allow_live_trading" value={<Badge variant="block">false</Badge>} mono={false} />
          <StatRow label="Demo max lot" value="0.01" highlight="amber" />
          <StatRow label="Open trades" value={String(account?.open_positions_count ?? 0)} />
          <StatRow label="Safety guard" value={
            <Badge variant={(safetyGuard?.last_status as string) === 'PASS' ? 'green' : 'amber'}>
              {(safetyGuard?.last_status as string) ?? '—'}
            </Badge>
          } mono={false} />
        </div>

        {/* Cycle */}
        <div className="card p-4 space-y-1">
          <SectionHeader title="Cycle State" />
          <StatRow label="Status" value={
            <StateBadge badge={
              cycleStatus?.last_status as string ??
              (snap?.cycle_status as Record<string,unknown>)?.last_status as string ??
              'STARTING'
            } />
          } mono={false} />
          <StatRow label="Analyzed" value={String(cycleStatus?.analyzed ?? '—')} />
          <StatRow label="Skipped" value={String(cycleStatus?.skipped ?? '—')} />
          <StatRow label="Demo orders" value={String(cycleStatus?.demo_orders ?? 0)} />
          <StatRow label="Active symbols" value={
            String(health?.resolved_symbols?.length ?? (symbolsBlock?.count as number ?? '—'))
          } />
          <StatRow label="Symbols" value={
            health?.resolved_symbols?.join(', ') ?? '—'
          } />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4">
        {/* Latest candidate */}
        <div className="card p-4">
          <SectionHeader title="Latest Setup Candidate" />
          {bestCand ? (
            <div className="space-y-1">
              <StatRow label="Symbol" value={bestCand.symbol ?? '—'} />
              <StatRow label="Strategy" value={bestCand.best_strategy ?? bestCand.strategy ?? '—'} />
              <StatRow label="Direction" value={<DirectionBadge direction={bestCand.direction} />} mono={false} />
              <StatRow label="Grade" value={bestCand.grade ?? '—'} highlight={
                bestCand.grade === 'A+' || bestCand.grade === 'A' ? 'green' :
                bestCand.grade === 'B' ? 'blue' :
                bestCand.grade === 'C' ? 'amber' : 'red'
              } />
              <StatRow label="Edge score" value={
                bestCand.edge_score != null ? bestCand.edge_score.toFixed(1) : '—'
              } />
              <StatRow label="Demo eligible" value={
                <Badge variant={bestCand.demo_eligible ? 'green' : 'amber'}>
                  {String(bestCand.demo_eligible ?? false)}
                </Badge>
              } mono={false} />
              <StatRow label="Entry" value={<Num value={bestCand.entry} decimals={5} />} />
              <StatRow label="SL" value={<Num value={bestCand.sl} decimals={5} />} />
              <StatRow label="TP" value={<Num value={bestCand.tp} decimals={5} />} />
              <StatRow label="RR" value={
                bestCand.rr != null ? `${bestCand.rr.toFixed(2)}:1` : '—'
              } />
              {(bestCand.failed_gates ?? []).length > 0 && (
                <div className="mt-2 pt-2 border-t border-border/40">
                  <span className="text-2xs text-text-muted uppercase">Block reason</span>
                  <div className="flex flex-wrap gap-1 mt-1">
                    {(bestCand.failed_gates ?? []).slice(0, 4).map((g, i) => (
                      <Badge key={i} variant="stale">{g}</Badge>
                    ))}
                  </div>
                </div>
              )}
            </div>
          ) : (
            <NoDataState label="NO_CANDIDATE" sub="No setup candidate available" />
          )}
        </div>

        {/* Setup hunter & safety summary */}
        <div className="card p-4 space-y-4">
          <div>
            <SectionHeader title="Setup Hunter" />
            <div className="space-y-1">
              <StatRow label="Edge-ready candidates" value={String(hunter?.edge_ready_count ?? 0)} />
              <StatRow label="Near-miss candidates" value={String(hunter?.near_miss_count ?? 0)} />
              <StatRow label="Latest accepted" value={
                hunter?.accepted_candidates?.[0]?.strategy ?? '—'
              } />
            </div>
          </div>

          <div className="border-t border-border/40 pt-4">
            <SectionHeader title="Safety Invariants (Hard)" />
            <div className="space-y-1.5">
              {[
                'ALLOW_LIVE_TRADING = false',
                'DEMO_ONLY = true',
                'DEMO_MAX_LOT = 0.01',
                'order_send → app/mt5/demo_router.py ONLY',
                'No execution endpoints in this dashboard',
              ].map((line, i) => (
                <div key={i} className="flex items-center gap-2">
                  <span className="text-accent-green text-xs">✓</span>
                  <span className="text-xs font-mono text-text-secondary">{line}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="border-t border-border/40 pt-4">
            <SectionHeader title="Ingest Health" />
            <StatRow
              label="Lovable ingest"
              value={
                <Badge variant={
                  health?.ingest_health?.status === 'LIVE' ? 'live' :
                  health?.ingest_health?.status === 'DEGRADED' ? 'stale' : 'muted'
                }>
                  {health?.ingest_health?.status ?? '—'}
                </Badge>
              }
              mono={false}
            />
            {health?.ingest_health?.reason && (
              <StatRow label="Reason" value={health.ingest_health.reason} highlight="amber" />
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
