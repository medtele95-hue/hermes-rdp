import { useEffect, useState, useMemo } from 'react'
import api from '../api/client'
import type { SetupAuditData, AuditRecord } from '../api/types'

// ── helpers ───────────────────────────────────────────────────────────────────

function pct(n: number, total: number): string {
  if (!total) return '0.0%'
  return `${((n / total) * 100).toFixed(1)}%`
}

function statusBadge(status: string): string {
  const map: Record<string, string> = {
    EXECUTED_BY_HERMES: 'bg-accent-green/20 text-accent-green border border-accent-green/40',
    MT5_SYNCED_CLOSED: 'bg-blue-500/20 text-blue-400 border border-blue-500/40',
    SENT_TO_DEMO_ROUTER: 'bg-blue-500/20 text-blue-400 border border-blue-500/40',
    ACCEPTED_BUT_ROUTER_BLOCKED: 'bg-amber-500/20 text-amber-400 border border-amber-500/40',
    ACCEPTED_BUT_NOT_SENT: 'bg-yellow-500/20 text-yellow-400 border border-yellow-500/40',
    SAFETY_BLOCKED: 'bg-red-500/20 text-red-400 border border-red-500/40',
    NO_VALID_SETUP: 'bg-surface-3 text-text-muted border border-border',
    SETUP_HUNTER_REJECTED: 'bg-surface-3 text-text-muted border border-border',
    OBSERVE_ONLY: 'bg-purple-500/20 text-purple-400 border border-purple-500/40',
    WAITING_CONFIRMATION: 'bg-yellow-500/20 text-yellow-400 border border-yellow-500/40',
  }
  return map[status] ?? 'bg-surface-3 text-text-muted border border-border'
}

function decisionBadge(d: string): string {
  if (d === 'ACCEPT' || d === 'PASS') return 'text-accent-green'
  if (d === 'BLOCK') return 'text-red-400'
  if (d === 'REJECT') return 'text-amber-400'
  return 'text-text-muted'
}

// ── stat card ─────────────────────────────────────────────────────────────────

function StatCard({ label, value, sub, accent }: {
  label: string; value: number | string; sub?: string; accent?: string
}) {
  const color = accent ?? 'text-text-primary'
  return (
    <div className="bg-surface-1 border border-border rounded p-4 flex flex-col gap-1 min-w-0">
      <div className={`text-2xl font-mono font-bold ${color}`}>{value}</div>
      <div className="text-xs text-text-muted font-mono uppercase tracking-wide truncate">{label}</div>
      {sub && <div className="text-xs text-text-muted font-mono">{sub}</div>}
    </div>
  )
}

// ── filter bar ────────────────────────────────────────────────────────────────

interface Filters {
  symbol: string
  strategy: string
  status: string
  reason: string
}

function FilterBar({ symbols, strategies, statuses, filters, onChange }: {
  symbols: string[]
  strategies: string[]
  statuses: string[]
  filters: Filters
  onChange: (f: Filters) => void
}) {
  return (
    <div className="flex flex-wrap gap-2 items-center">
      <select
        className="bg-surface-1 border border-border rounded px-2 py-1 text-xs text-text-primary font-mono"
        value={filters.symbol}
        onChange={e => onChange({ ...filters, symbol: e.target.value })}
      >
        <option value="">All Symbols</option>
        {symbols.map(s => <option key={s} value={s}>{s}</option>)}
      </select>
      <select
        className="bg-surface-1 border border-border rounded px-2 py-1 text-xs text-text-primary font-mono"
        value={filters.strategy}
        onChange={e => onChange({ ...filters, strategy: e.target.value })}
      >
        <option value="">All Strategies</option>
        {strategies.map(s => <option key={s} value={s}>{s}</option>)}
      </select>
      <select
        className="bg-surface-1 border border-border rounded px-2 py-1 text-xs text-text-primary font-mono"
        value={filters.status}
        onChange={e => onChange({ ...filters, status: e.target.value })}
      >
        <option value="">All Statuses</option>
        {statuses.map(s => <option key={s} value={s}>{s}</option>)}
      </select>
      <input
        className="bg-surface-1 border border-border rounded px-2 py-1 text-xs text-text-primary font-mono w-40"
        placeholder="Block reason..."
        value={filters.reason}
        onChange={e => onChange({ ...filters, reason: e.target.value })}
      />
      {(filters.symbol || filters.strategy || filters.status || filters.reason) && (
        <button
          className="text-xs text-text-muted hover:text-text-primary font-mono underline"
          onClick={() => onChange({ symbol: '', strategy: '', status: '', reason: '' })}
        >
          clear
        </button>
      )}
    </div>
  )
}

// ── records table ─────────────────────────────────────────────────────────────

function RecordsTable({ records, title }: { records: AuditRecord[]; title: string }) {
  if (!records.length) {
    return (
      <div className="text-xs text-text-muted font-mono py-4 px-2">{title}: none</div>
    )
  }
  return (
    <div>
      <div className="text-xs text-text-muted font-mono mb-2">{title} ({records.length})</div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs font-mono border-collapse min-w-[900px]">
          <thead>
            <tr className="text-text-muted border-b border-border">
              <th className="text-left py-1.5 px-2 font-normal">CASA Time</th>
              <th className="text-left py-1.5 px-2 font-normal">Symbol</th>
              <th className="text-left py-1.5 px-2 font-normal">Strategy</th>
              <th className="text-left py-1.5 px-2 font-normal">Dir</th>
              <th className="text-left py-1.5 px-2 font-normal">Grd</th>
              <th className="text-right py-1.5 px-2 font-normal">Score</th>
              <th className="text-left py-1.5 px-2 font-normal">SH</th>
              <th className="text-left py-1.5 px-2 font-normal">Safety</th>
              <th className="text-left py-1.5 px-2 font-normal">Router</th>
              <th className="text-left py-1.5 px-2 font-normal">MT5</th>
              <th className="text-left py-1.5 px-2 font-normal">Block Reason</th>
              <th className="text-left py-1.5 px-2 font-normal">Status</th>
            </tr>
          </thead>
          <tbody>
            {records.map((r, i) => (
              <tr key={i} className="border-b border-border/40 hover:bg-surface-3/40 transition-colors">
                <td className="py-1 px-2 text-text-muted whitespace-nowrap">{r.timestamp_casa ?? r.timestamp_utc ?? '—'}</td>
                <td className="py-1 px-2 text-text-primary whitespace-nowrap">{r.symbol}</td>
                <td className="py-1 px-2 text-text-secondary truncate max-w-[160px]" title={r.strategy}>{r.strategy}</td>
                <td className="py-1 px-2 text-text-secondary">{r.direction ?? '—'}</td>
                <td className="py-1 px-2 text-accent-green font-bold">{r.grade ?? '—'}</td>
                <td className="py-1 px-2 text-right text-text-secondary">{r.score != null ? r.score.toFixed(1) : '—'}</td>
                <td className={`py-1 px-2 ${decisionBadge(r.sh_decision)}`}>{r.sh_decision}</td>
                <td className={`py-1 px-2 ${decisionBadge(r.safety_decision)}`}>{r.safety_decision}</td>
                <td className={`py-1 px-2 ${decisionBadge(r.router_decision)}`}>{r.router_decision}</td>
                <td className={`py-1 px-2 ${r.mt5_status === 'OPENED' ? 'text-accent-green' : r.mt5_status === 'SYNCED_CLOSED' ? 'text-blue-400' : 'text-text-muted'}`}>{r.mt5_status}</td>
                <td className="py-1 px-2 text-amber-400 truncate max-w-[200px]" title={r.rejection_reason ?? ''}>{r.rejection_reason ?? '—'}</td>
                <td className="py-1 px-2">
                  <span className={`inline-block px-1.5 py-0.5 rounded text-2xs ${statusBadge(r.final_status)}`}>
                    {r.final_status}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ── main page ─────────────────────────────────────────────────────────────────

export function SetupAuditPage() {
  const [data, setData] = useState<SetupAuditData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [hours, setHours] = useState(48)
  const [filters, setFilters] = useState<Filters>({ symbol: '', strategy: '', status: '', reason: '' })
  const [activeTab, setActiveTab] = useState<'accepted_not_executed' | 'all_executed' | 'breakdown'>('accepted_not_executed')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    api.setupAudit(hours).then(res => {
      if (cancelled) return
      if (res.ok) {
        setData(res.data)
      } else {
        setError(res.reason ?? 'Unknown error')
      }
      setLoading(false)
    }).catch(e => {
      if (!cancelled) { setError(String(e)); setLoading(false) }
    })
    return () => { cancelled = true }
  }, [hours])

  // Build filter options from data
  const symbolOpts = useMemo(() => {
    if (!data) return []
    return Object.keys(data.by_symbol).sort()
  }, [data])
  const strategyOpts = useMemo(() => {
    if (!data) return []
    return Object.keys(data.by_strategy).sort()
  }, [data])
  const statusOpts = useMemo(() => {
    if (!data) return []
    return Object.keys(data.by_final_status).sort()
  }, [data])

  // Apply filters to records
  const filteredANE = useMemo(() => {
    if (!data) return []
    return data.accepted_not_executed.filter(r => {
      if (filters.symbol && r.symbol !== filters.symbol) return false
      if (filters.strategy && r.strategy !== filters.strategy) return false
      if (filters.status && r.final_status !== filters.status) return false
      if (filters.reason && !(r.rejection_reason ?? '').toLowerCase().includes(filters.reason.toLowerCase())) return false
      return true
    })
  }, [data, filters])

  const filteredExecuted = useMemo(() => {
    if (!data) return []
    return [...data.executed_records, ...(data.synced_closed_records ?? [])].filter(r => {
      if (filters.symbol && r.symbol !== filters.symbol) return false
      if (filters.strategy && r.strategy !== filters.strategy) return false
      return true
    })
  }, [data, filters])

  if (loading) {
    return (
      <div className="p-6 flex items-center gap-3 text-text-muted font-mono text-sm">
        <div className="w-4 h-4 border-2 border-accent-green/40 border-t-accent-green rounded-full animate-spin" />
        Running {hours}h setup audit...
      </div>
    )
  }

  if (error) {
    return (
      <div className="p-6 text-red-400 font-mono text-sm">
        <div className="font-bold mb-1">Audit error</div>
        <div className="text-text-muted">{error}</div>
        <button
          className="mt-3 text-xs underline text-text-muted hover:text-text-primary"
          onClick={() => setError(null)}
        >retry</button>
      </div>
    )
  }

  if (!data) return null

  const total = data.total_setups_detected
  const execTotal = data.executed_records.length + (data.synced_closed_records?.length ?? 0)

  return (
    <div className="p-4 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-sm font-mono font-bold text-text-primary uppercase tracking-widest">
            48H Setup Audit
          </h1>
          <div className="text-2xs text-text-muted font-mono mt-0.5">
            Read-only pipeline analysis — no execution, no trade buttons
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-text-muted font-mono">Window:</span>
          {[24, 48, 72, 168].map(h => (
            <button
              key={h}
              onClick={() => setHours(h)}
              className={`text-xs font-mono px-2 py-1 rounded border transition-colors ${
                hours === h
                  ? 'bg-accent-green/20 text-accent-green border-accent-green/40'
                  : 'text-text-muted border-border hover:text-text-primary'
              }`}
            >
              {h}h
            </button>
          ))}
          <div className="text-2xs text-text-muted font-mono ml-2">
            {data.generated_at.slice(0, 19)} UTC
          </div>
        </div>
      </div>

      {/* Safety badge */}
      <div className="flex items-center gap-2 text-2xs font-mono text-text-muted bg-surface-1 border border-border rounded px-3 py-2">
        <span className="text-accent-green font-bold">READ ONLY</span>
        <span>·</span>
        <span>ALLOW_LIVE_TRADING=false</span>
        <span>·</span>
        <span>DEMO_ONLY=true</span>
        <span>·</span>
        <span>NO TRADE BUTTONS</span>
        <span>·</span>
        <span>DemoRouter untouched</span>
      </div>

      {/* Primary stat cards — execution funnel */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
        <StatCard label="Total Setups" value={total} />
        <StatCard
          label="SH Accepted"
          value={data.setup_hunter_accepted}
          sub={pct(data.setup_hunter_accepted, total)}
          accent="text-accent-green"
        />
        <StatCard
          label="SH Rejected"
          value={data.setup_hunter_rejected}
          sub={pct(data.setup_hunter_rejected, total)}
          accent="text-text-muted"
        />
        <StatCard
          label="Accepted But Blocked"
          value={data.accepted_but_router_blocked}
          sub={pct(data.accepted_but_router_blocked, data.setup_hunter_accepted)}
          accent="text-amber-400"
        />
        <StatCard
          label="Executed by Hermes"
          value={data.executed_by_hermes}
          sub={pct(data.executed_by_hermes, total)}
          accent={data.executed_by_hermes > 0 ? 'text-accent-green' : 'text-text-muted'}
        />
        <StatCard
          label="MT5 Synced Closed"
          value={data.mt5_synced_closed}
          sub="no DemoRouter match"
          accent={data.mt5_synced_closed > 0 ? 'text-blue-400' : 'text-text-muted'}
        />
      </div>

      {/* Top-down missing stat cards */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
        <StatCard
          label="Top-Down Data Missing"
          value={data.top_down_missing_count}
          sub={pct(data.top_down_missing_count, total)}
          accent={data.top_down_missing_count > 0 ? 'text-amber-400' : 'text-text-muted'}
        />
        <StatCard
          label="Accepted But Not Sent"
          value={data.accepted_but_not_sent}
          sub={pct(data.accepted_but_not_sent, data.setup_hunter_accepted)}
          accent="text-yellow-400"
        />
        <StatCard
          label="DemoRouter Sent"
          value={data.demo_router_sent}
          sub={pct(data.demo_router_sent, total)}
          accent={data.demo_router_sent > 0 ? 'text-blue-400' : 'text-text-muted'}
        />
        <StatCard
          label="Accepted Not Executed"
          value={data.accepted_not_executed_count}
          sub={`of ${data.setup_hunter_accepted} accepted`}
          accent="text-text-secondary"
        />
      </div>

      {/* Top-down missing fields panel */}
      {Object.keys(data.top_missing_fields).length > 0 && (
        <div className="bg-surface-1 border border-amber-500/30 rounded p-3">
          <div className="text-2xs text-amber-400 font-mono uppercase mb-2 font-bold">
            Top-Down Missing Fields (blocking {data.top_down_missing_count} setups)
          </div>
          <div className="flex flex-wrap gap-2">
            {Object.entries(data.top_missing_fields)
              .sort((a, b) => b[1] - a[1])
              .map(([field, cnt]) => (
                <span key={field} className="text-2xs font-mono bg-amber-500/10 text-amber-400 border border-amber-500/30 rounded px-2 py-0.5">
                  {field}: {cnt}
                </span>
              ))}
          </div>
          {Object.keys(data.top_down_missing_by_symbol).length > 0 && (
            <div className="mt-2 flex flex-wrap gap-2">
              <span className="text-2xs text-text-muted font-mono">by symbol:</span>
              {Object.entries(data.top_down_missing_by_symbol)
                .sort((a, b) => b[1] - a[1])
                .slice(0, 8)
                .map(([sym, cnt]) => (
                  <span key={sym} className="text-2xs font-mono text-text-secondary">
                    {sym}:{cnt}
                  </span>
                ))}
            </div>
          )}
        </div>
      )}

      {/* Tabs */}
      <div className="flex gap-1 border-b border-border">
        {([
          ['accepted_not_executed', `Accepted But Not Executed (${data.accepted_not_executed_count})`],
          ['all_executed', `Executed / Synced (${execTotal})`],
          ['breakdown', 'Breakdown'],
        ] as [typeof activeTab, string][]).map(([tab, label]) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`text-xs font-mono px-3 py-2 border-b-2 transition-colors ${
              activeTab === tab
                ? 'border-accent-green text-accent-green'
                : 'border-transparent text-text-muted hover:text-text-primary'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Filters */}
      <FilterBar
        symbols={symbolOpts}
        strategies={strategyOpts}
        statuses={statusOpts}
        filters={filters}
        onChange={setFilters}
      />

      {/* Tab content */}
      {activeTab === 'accepted_not_executed' && (
        <div className="bg-surface-1 border border-border rounded p-3 space-y-3">
          <div className="text-xs font-mono text-text-muted">
            SetupHunter ACCEPTED these setups but they never reached MT5.
            Typical cause: top-down data missing (
            {data.top_down_missing_count} events, see panel above) or router block.
          </div>
          <RecordsTable records={filteredANE} title="Accepted but not executed" />
        </div>
      )}

      {activeTab === 'all_executed' && (
        <div className="bg-surface-1 border border-border rounded p-3 space-y-4">
          {data.executed_records.length > 0 && (
            <RecordsTable records={filteredExecuted.filter(r => r.final_status === 'EXECUTED_BY_HERMES')} title="Executed by Hermes (DemoRouter confirmed)" />
          )}
          {(data.synced_closed_records?.length ?? 0) > 0 && (
            <RecordsTable records={filteredExecuted.filter(r => r.final_status === 'MT5_SYNCED_CLOSED')} title="MT5 Synced Closed (no DemoRouter match)" />
          )}
          {execTotal === 0 && (
            <div className="text-xs text-text-muted font-mono py-4">No executed or synced positions in this window.</div>
          )}
        </div>
      )}

      {activeTab === 'breakdown' && (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {/* Final status */}
          <div className="bg-surface-1 border border-border rounded p-3">
            <div className="text-2xs text-text-muted font-mono uppercase mb-2">Final Status</div>
            <table className="w-full text-xs font-mono">
              <tbody>
                {Object.entries(data.by_final_status).sort((a, b) => b[1] - a[1]).map(([s, n]) => (
                  <tr key={s} className="border-b border-border/30">
                    <td className="py-1">
                      <span className={`inline-block px-1.5 py-0.5 rounded text-2xs ${statusBadge(s)}`}>{s}</span>
                    </td>
                    <td className="py-1 text-right text-text-secondary">{n}</td>
                    <td className="py-1 text-right text-text-muted pl-2">{pct(n, total)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Top-down missing fields */}
          <div className="bg-surface-1 border border-border rounded p-3">
            <div className="text-2xs text-text-muted font-mono uppercase mb-2">Top-Down Missing Fields</div>
            <table className="w-full text-xs font-mono">
              <tbody>
                {Object.entries(data.top_missing_fields).sort((a, b) => b[1] - a[1]).slice(0, 10).map(([f, n]) => (
                  <tr key={f} className="border-b border-border/30">
                    <td className="py-1 text-amber-400 truncate max-w-[180px]" title={f}>{f}</td>
                    <td className="py-1 text-right text-text-secondary">{n}</td>
                  </tr>
                ))}
                {Object.keys(data.top_missing_fields).length === 0 && (
                  <tr><td className="py-1 text-text-muted" colSpan={2}>none</td></tr>
                )}
              </tbody>
            </table>
          </div>

          {/* Top block reasons */}
          <div className="bg-surface-1 border border-border rounded p-3">
            <div className="text-2xs text-text-muted font-mono uppercase mb-2">Top Block Reasons</div>
            <table className="w-full text-xs font-mono">
              <tbody>
                {Object.entries(data.top_block_reasons).slice(0, 10).map(([r, n]) => (
                  <tr key={r} className="border-b border-border/30">
                    <td className="py-1 text-amber-400 truncate max-w-[180px]" title={r}>{r}</td>
                    <td className="py-1 text-right text-text-secondary">{n}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* By symbol */}
          <div className="bg-surface-1 border border-border rounded p-3">
            <div className="text-2xs text-text-muted font-mono uppercase mb-2">By Symbol</div>
            <table className="w-full text-xs font-mono">
              <tbody>
                {Object.entries(data.by_symbol).sort((a, b) => b[1] - a[1]).slice(0, 10).map(([s, n]) => (
                  <tr key={s} className="border-b border-border/30">
                    <td className="py-1 text-text-primary">{s}</td>
                    <td className="py-1 text-right text-text-secondary">{n}</td>
                    <td className="py-1 text-right text-text-muted pl-2">{pct(n, total)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Top-down missing by symbol */}
          <div className="bg-surface-1 border border-border rounded p-3">
            <div className="text-2xs text-text-muted font-mono uppercase mb-2">Top-Down Missing By Symbol</div>
            <table className="w-full text-xs font-mono">
              <tbody>
                {Object.entries(data.top_down_missing_by_symbol).sort((a, b) => b[1] - a[1]).slice(0, 10).map(([s, n]) => (
                  <tr key={s} className="border-b border-border/30">
                    <td className="py-1 text-amber-400">{s}</td>
                    <td className="py-1 text-right text-text-secondary">{n}</td>
                  </tr>
                ))}
                {Object.keys(data.top_down_missing_by_symbol).length === 0 && (
                  <tr><td className="py-1 text-text-muted" colSpan={2}>none</td></tr>
                )}
              </tbody>
            </table>
          </div>

          {/* By strategy */}
          <div className="bg-surface-1 border border-border rounded p-3">
            <div className="text-2xs text-text-muted font-mono uppercase mb-2">By Strategy</div>
            <table className="w-full text-xs font-mono">
              <tbody>
                {Object.entries(data.by_strategy).sort((a, b) => b[1] - a[1]).slice(0, 10).map(([s, n]) => (
                  <tr key={s} className="border-b border-border/30">
                    <td className="py-1 text-text-secondary truncate max-w-[200px]" title={s}>{s}</td>
                    <td className="py-1 text-right text-text-secondary">{n}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* By session */}
          <div className="bg-surface-1 border border-border rounded p-3">
            <div className="text-2xs text-text-muted font-mono uppercase mb-2">By Session</div>
            <table className="w-full text-xs font-mono">
              <tbody>
                {Object.entries(data.by_session).sort((a, b) => b[1] - a[1]).map(([s, n]) => (
                  <tr key={s} className="border-b border-border/30">
                    <td className="py-1 text-text-primary">{s}</td>
                    <td className="py-1 text-right text-text-secondary">{n}</td>
                    <td className="py-1 text-right text-text-muted pl-2">{pct(n, total)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* By hour */}
          <div className="bg-surface-1 border border-border rounded p-3">
            <div className="text-2xs text-text-muted font-mono uppercase mb-2">Setups Per Hour (CASA)</div>
            <div className="max-h-48 overflow-y-auto">
              <table className="w-full text-xs font-mono">
                <tbody>
                  {Object.entries(data.by_hour).slice(-48).map(([h, n]) => (
                    <tr key={h} className="border-b border-border/30">
                      <td className="py-0.5 text-text-muted truncate">{h}</td>
                      <td className="py-0.5 text-right text-text-secondary">{n}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
