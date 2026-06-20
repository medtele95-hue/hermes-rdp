import type {
  ApiResponse,
  AccountSnapshot,
  BtcStatusData,
  CandleData,
  DashboardSnapshot,
  HealthData,
  LogEntry,
  OrderFlowSnapshot,
  QuadTerminalData,
  RiskData,
  SetupAuditData,
  SetupHunterData,
  SymbolCard,
  SymbolState,
  TradesData,
  Candidate,
  StrategySignal,
} from './types'

const BASE    = '/local-api'
const TIMEOUT = 3_000   // 3 s per request — fail fast when backend is slow

// ─── Rate-limited error logger ───────────────────────────────────────────────
const _errCooldowns: Record<string, number> = {}
function _logErr(endpoint: string, reason: string, cooldownMs = 15_000): void {
  const now = Date.now()
  if (!_errCooldowns[endpoint] || now - _errCooldowns[endpoint] > cooldownMs) {
    console.warn(`[DASHBOARD_API_ERROR] endpoint=${endpoint} reason=${reason}`)
    _errCooldowns[endpoint] = now
  }
}

// ─── Core fetch ──────────────────────────────────────────────────────────────
async function get<T>(path: string): Promise<ApiResponse<T>> {
  const ctrl = new AbortController()
  const kill = setTimeout(() => ctrl.abort(), TIMEOUT)
  try {
    const res = await fetch(`${BASE}${path}`, {
      headers: { Accept: 'application/json' },
      signal: ctrl.signal,
    })
    clearTimeout(kill)
    if (!res.ok) {
      const reason = `HTTP ${res.status}`
      _logErr(path, reason)
      return { ok: false, status: 'HTTP_ERROR', reason, timestamp: new Date().toISOString() }
    }
    const json = await res.json()
    return json as ApiResponse<T>
  } catch (err) {
    clearTimeout(kill)
    const reason = err instanceof Error ? err.message : 'Network error'
    _logErr(path, reason)
    return { ok: false, status: 'FETCH_ERROR', reason, timestamp: new Date().toISOString() }
  }
}

/**
 * safeGet — never rejects. Returns `fallback` when backend unavailable.
 * Use for non-critical data where a stale default is better than an error.
 */
async function safeGet<T>(path: string, fallback: T): Promise<T> {
  try {
    const res = await get<T>(path)
    return res.ok ? res.data : fallback
  } catch {
    return fallback
  }
}

// ─── Public API ──────────────────────────────────────────────────────────────

export const api = {
  health: () => get<HealthData>('/health'),

  dashboardStatus: () => get<DashboardSnapshot>('/dashboard-status'),

  quadTerminal: () => get<QuadTerminalData>('/quad-terminal'),

  symbols: () =>
    get<{ symbols: Record<string, SymbolState>; count: number }>('/symbols'),

  candles: (symbol: string, timeframe: string) =>
    get<CandleData>(`/candles/${encodeURIComponent(symbol)}/${timeframe}`),

  orderFlow: () =>
    get<{ snapshots: Record<string, OrderFlowSnapshot>; order_flow_badge: string }>('/order-flow'),

  confirmations: (symbol?: string) =>
    get<{ confirmations: Candidate[]; count: number }>(
      symbol ? `/confirmations?symbol=${encodeURIComponent(symbol)}` : '/confirmations'
    ),

  setupHunter: () => get<SetupHunterData>('/setup-hunter'),

  strategies: (symbol?: string) =>
    get<{ by_symbol?: Record<string, StrategySignal[]>; signals?: StrategySignal[]; symbol?: string }>(
      symbol ? `/strategies?symbol=${encodeURIComponent(symbol)}` : '/strategies'
    ),

  accountSnapshot: () => get<AccountSnapshot>('/account-snapshot'),

  risk: () => get<RiskData>('/risk'),

  trades: () =>
    get<TradesData>('/trades'),

  logs: (opts?: { limit?: number; level?: string; symbol?: string; strategy?: string; search?: string }) => {
    const params = new URLSearchParams()
    if (opts?.limit)    params.set('limit',    String(opts.limit))
    if (opts?.level)    params.set('level',    opts.level)
    if (opts?.symbol)   params.set('symbol',   opts.symbol)
    if (opts?.strategy) params.set('strategy', opts.strategy)
    if (opts?.search)   params.set('search',   opts.search)
    const qs = params.toString()
    return get<{ logs: LogEntry[]; count: number; filters: Record<string, string | null> }>(
      `/logs/recent${qs ? `?${qs}` : ''}`
    )
  },

  auditSafety: () => get<Record<string, unknown>>('/audit-safety'),

  setupAudit: (hours?: number) =>
    get<SetupAuditData>(`/setup-audit${hours ? `?hours=${hours}` : ''}`),

  btcStatus: () => get<BtcStatusData>('/btc-status'),

  safeGet,
}

export default api
