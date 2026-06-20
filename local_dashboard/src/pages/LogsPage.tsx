import { useState, useRef, useEffect } from 'react'
import { useLogs } from '../api/usePolling'
import { Badge } from '../components/ui/Badge'
import { Skeleton, NoDataState } from '../components/ui/Skeleton'
import type { LogEntry } from '../api/types'

const LEVEL_COLORS: Record<string, string> = {
  INFO: 'text-text-primary',
  WARNING: 'text-accent-amber',
  ERROR: 'text-accent-red',
  DEBUG: 'text-text-muted',
  CRITICAL: 'text-accent-red',
}

const LEVEL_BADGE: Record<string, string> = {
  INFO: 'muted',
  WARNING: 'amber',
  ERROR: 'red',
  DEBUG: 'muted',
  CRITICAL: 'block',
}

const HIGHLIGHT_TOKENS = [
  '[ROUTER_BLOCK]',
  '[SAFETY_GUARD]',
  '[ORDER_FLOW_PAYLOAD_EMITTED]',
  '[US100_TICK_OK]',
  '[DASHBOARD_HEARTBEAT_OK]',
  '[SETUP_HUNTER_ACCEPT]',
  '[SETUP_HUNTER_REJECT]',
  '[CONFIRMATION_BLOCK]',
  '[CYCLE]',
  '[LOCAL_DASHBOARD]',
  '[HERMES_CONFIG]',
]

function highlightMessage(msg: string): React.ReactNode {
  let result = msg
  let parts: React.ReactNode[] = []

  // Color specific tokens
  const tokenColors: Record<string, string> = {
    '[ROUTER_BLOCK]': 'text-accent-red font-semibold',
    '[SAFETY_GUARD]': 'text-accent-amber font-semibold',
    '[SETUP_HUNTER_ACCEPT]': 'text-accent-green font-semibold',
    '[SETUP_HUNTER_REJECT]': 'text-accent-red',
    '[CONFIRMATION_BLOCK]': 'text-accent-red font-semibold',
    '[CYCLE]': 'text-accent-blue font-semibold',
    '[LOCAL_DASHBOARD]': 'text-accent-cyan font-semibold',
    '[ORDER_FLOW_PAYLOAD_EMITTED]': 'text-accent-purple',
  }

  // Find first matching token
  for (const [token, color] of Object.entries(tokenColors)) {
    if (msg.includes(token)) {
      const idx = msg.indexOf(token)
      return (
        <>
          <span className="text-text-secondary">{msg.slice(0, idx)}</span>
          <span className={color}>{token}</span>
          <span className="text-text-secondary">{msg.slice(idx + token.length)}</span>
        </>
      )
    }
  }

  return <span className="text-text-secondary">{msg}</span>
}

export function LogsPage() {
  const [level, setLevel] = useState('')
  const [symbol, setSymbol] = useState('')
  const [strategy, setStrategy] = useState('')
  const [search, setSearch] = useState('')
  const [autoScroll, setAutoScroll] = useState(true)
  const bottomRef = useRef<HTMLDivElement>(null)

  const { data, loading } = useLogs({
    limit: 500,
    level: level || undefined,
    symbol: symbol || undefined,
    strategy: strategy || undefined,
    search: search || undefined,
  })

  const logs = data?.logs ?? []

  useEffect(() => {
    if (autoScroll && bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [logs.length, autoScroll])

  return (
    <div className="h-full flex flex-col">
      {/* Header + filters */}
      <div className="px-6 py-3 border-b border-border flex-shrink-0 space-y-3">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-lg font-semibold">Live Logs</h1>
            <p className="text-xs text-text-muted">Real-time backend log stream</p>
          </div>
          <div className="flex items-center gap-3">
            <Badge variant="muted">{logs.length} entries</Badge>
            <label className="flex items-center gap-1.5 text-xs text-text-secondary cursor-pointer">
              <input
                type="checkbox"
                checked={autoScroll}
                onChange={e => setAutoScroll(e.target.checked)}
                className="rounded"
              />
              Auto-scroll
            </label>
          </div>
        </div>

        {/* Filters */}
        <div className="flex gap-3 flex-wrap">
          <select
            value={level}
            onChange={e => setLevel(e.target.value)}
            className="bg-surface-3 border border-border rounded px-2 py-1 text-xs text-text-primary font-mono"
          >
            <option value="">All levels</option>
            <option value="INFO">INFO</option>
            <option value="WARNING">WARNING</option>
            <option value="ERROR">ERROR</option>
            <option value="DEBUG">DEBUG</option>
          </select>

          <input
            type="text"
            placeholder="Filter by symbol…"
            value={symbol}
            onChange={e => setSymbol(e.target.value)}
            className="bg-surface-3 border border-border rounded px-2 py-1 text-xs text-text-primary font-mono w-36"
          />

          <input
            type="text"
            placeholder="Filter by strategy…"
            value={strategy}
            onChange={e => setStrategy(e.target.value)}
            className="bg-surface-3 border border-border rounded px-2 py-1 text-xs text-text-primary font-mono w-44"
          />

          <input
            type="text"
            placeholder="Search messages…"
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="bg-surface-3 border border-border rounded px-2 py-1 text-xs text-text-primary font-mono flex-1 min-w-40"
          />

          {(level || symbol || strategy || search) && (
            <button
              onClick={() => { setLevel(''); setSymbol(''); setStrategy(''); setSearch('') }}
              className="px-2 py-1 text-xs text-accent-red font-mono border border-accent-red/30 rounded hover:bg-accent-red/10"
            >
              Clear
            </button>
          )}
        </div>

        {/* Token filter shortcuts */}
        <div className="flex gap-1.5 flex-wrap">
          {HIGHLIGHT_TOKENS.map(tok => (
            <button
              key={tok}
              onClick={() => setSearch(search === tok ? '' : tok)}
              className={`text-2xs font-mono px-1.5 py-0.5 rounded border transition-colors ${
                search === tok
                  ? 'bg-accent-green/20 text-accent-green border-accent-green/40'
                  : 'border-border text-text-muted hover:text-text-primary hover:border-border/80'
              }`}
            >
              {tok}
            </button>
          ))}
        </div>
      </div>

      {/* Log stream */}
      <div className="flex-1 overflow-y-auto p-0 font-mono text-xs bg-surface" onScroll={e => {
        const el = e.currentTarget
        const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50
        if (atBottom !== autoScroll) setAutoScroll(atBottom)
      }}>
        {loading && !data ? (
          <div className="p-4 space-y-1">
            {Array.from({ length: 10 }).map((_, i) => <Skeleton key={i} className="h-3 w-full" />)}
          </div>
        ) : logs.length === 0 ? (
          <NoDataState label="NO LOGS" sub="No log entries match the current filter" />
        ) : (
          <table className="w-full">
            <tbody>
              {logs.map((entry: LogEntry, i) => (
                <tr
                  key={i}
                  className={`border-b border-border/20 hover:bg-surface-2 group ${
                    entry.level === 'ERROR' || entry.level === 'CRITICAL' ? 'bg-accent-red/5' :
                    entry.level === 'WARNING' ? 'bg-accent-amber/5' : ''
                  }`}
                >
                  <td className="px-3 py-0.5 whitespace-nowrap text-text-muted w-28 align-top">
                    {entry.timestamp
                      ? new Date(entry.timestamp).toLocaleTimeString('en', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
                      : '—'}
                  </td>
                  <td className="px-1 py-0.5 whitespace-nowrap align-top w-16">
                    <span className={`text-2xs uppercase ${
                      LEVEL_COLORS[entry.level] ?? 'text-text-muted'
                    }`}>
                      {entry.level?.slice(0, 4) ?? ''}
                    </span>
                  </td>
                  <td className="px-3 py-0.5 align-top text-wrap break-all">
                    {highlightMessage(entry.message ?? '')}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
