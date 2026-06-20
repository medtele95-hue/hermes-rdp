import { useRisk, useAccountSnapshot } from '../api/usePolling'
import { Badge } from '../components/ui/Badge'
import { StatRow, Num, SectionHeader } from '../components/ui/StatRow'
import { Skeleton } from '../components/ui/Skeleton'

function PnLValue({ v, label }: { v?: number | null; label: string }) {
  return (
    <div className="flex items-center justify-between py-2 border-b border-border/40 last:border-0">
      <span className="text-xs text-text-secondary">{label}</span>
      {v == null ? (
        <span className="text-text-muted font-mono">—</span>
      ) : (
        <span className={`text-sm font-mono font-semibold tabular-nums ${v >= 0 ? 'text-accent-green' : 'text-accent-red'}`}>
          {v >= 0 ? '+' : ''}{v.toFixed(2)}
        </span>
      )}
    </div>
  )
}

export function RiskPnL() {
  const { data: risk, loading: rLoading } = useRisk()
  const { data: account } = useAccountSnapshot()

  const acc = risk?.account ?? {}
  const limits = risk?.limits
  const sg = risk?.safety_guard as Record<string, unknown> | null

  return (
    <div className="p-6 space-y-6 max-w-screen-2xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Risk & PnL</h1>
          <p className="text-xs text-text-muted">Live account snapshot — read-only</p>
        </div>
        <div className="flex gap-2">
          <Badge variant="demo">DEMO ONLY</Badge>
          <Badge variant="block">NO LIVE TRADING</Badge>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-4">
        {/* Account snapshot */}
        <div className="card p-4">
          <SectionHeader title="Account Snapshot" />
          <PnLValue v={account?.balance} label="Balance" />
          <PnLValue v={account?.equity} label="Equity" />
          <PnLValue v={account?.margin} label="Margin" />
          <PnLValue v={account?.free_margin} label="Free Margin" />
          <StatRow label="Margin level" value={
            account?.margin_level != null
              ? `${account.margin_level.toFixed(1)}%`
              : '—'
          } />
          <StatRow label="Account type" value={account?.account_type ?? '—'} />
          <StatRow label="Server" value={account?.server ?? '—'} />
        </div>

        {/* PnL */}
        <div className="card p-4">
          <SectionHeader title="Profit & Loss" />
          <PnLValue v={acc.floating_pnl} label="Floating PnL (open)" />
          <PnLValue v={acc.closed_pnl_today} label="Closed PnL today" />
          <PnLValue v={acc.daily_pnl} label="Daily PnL" />
          <PnLValue v={acc.total_pnl_today} label="Total PnL today" />
          <StatRow label="Open positions" value={String(risk?.open_positions_count ?? 0)} />
        </div>

        {/* Limits */}
        <div className="card p-4">
          <SectionHeader title="Safety Limits" />
          {rLoading && !risk ? (
            <Skeleton className="h-32 w-full" />
          ) : limits ? (
            <>
              <StatRow label="Demo only" value={<Badge variant="demo">{String(limits.demo_only)}</Badge>} mono={false} />
              <StatRow label="Live trading" value={<Badge variant="block">DISABLED</Badge>} mono={false} />
              <StatRow label="Max lot" value={limits.demo_max_lot.toFixed(2)} highlight="amber" />
              <StatRow label="Max open trades" value={String(limits.demo_max_open_trades)} />
              <StatRow label="Max trades/day" value={String(limits.demo_max_trades_per_day)} />
              <StatRow label="Max daily loss" value={`${limits.demo_max_daily_loss_pct}%`} highlight="red" />
              <StatRow label="Max risk/trade" value={`${limits.demo_max_risk_per_trade_pct}%`} highlight="amber" />
              <StatRow label="Max consec. losses" value={String(limits.demo_stop_after_consecutive_losses)} />
            </>
          ) : null}
        </div>
      </div>

      {/* Safety guard status */}
      {sg && (
        <div className="card p-4">
          <SectionHeader title="Safety Guard" />
          <div className="grid grid-cols-3 gap-x-6">
            <StatRow label="Status" value={
              <Badge variant={sg.last_status === 'PASS' ? 'green' : 'block'}>
                {String(sg.last_status ?? '—')}
              </Badge>
            } mono={false} />
            <StatRow label="Reason" value={String(sg.last_reason ?? '—')} />
            <StatRow label="Last update" value={
              sg.last_update_utc
                ? new Date(String(sg.last_update_utc)).toLocaleTimeString()
                : '—'
            } />
          </div>
        </div>
      )}

      {/* Guards */}
      {risk?.guards && (
        <div className="card p-4">
          <SectionHeader title="Active Safety Guards" />
          <div className="space-y-1.5">
            {risk.guards.map((g, i) => (
              <div key={i} className="flex items-center gap-2">
                <span className="text-accent-green text-xs">✓</span>
                <span className="text-xs font-mono text-text-secondary">{g}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
