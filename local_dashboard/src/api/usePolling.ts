/**
 * usePolling — stable polling hook with in-flight deduplication.
 *
 * Key fixes vs. previous version:
 *  • inFlightRef: skips the next tick if a request is still pending
 *  • fetchFnRef:  stable callback identity so intervals don't restart on re-render
 *  • All convenience hooks except useHealth are gated on BackendHealthContext.online
 *  • Intervals slowed down: 2 s → 5–10 s depending on endpoint
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './client'
import type { ApiResponse } from './types'
import { useBackendOnline } from './BackendHealthContext'

// ─── Rate-limited console markers ────────────────────────────────────────────
const _skipCooldowns: Record<string, number> = {}
function _logSkipOnce(key: string): void {
  const now = Date.now()
  if (!_skipCooldowns[key] || now - _skipCooldowns[key] > 30_000) {
    console.debug(`[DASHBOARD_POLL_SKIPPED] endpoint=${key} reason=request_in_flight`)
    _skipCooldowns[key] = now
  }
}

// ─── Core polling hook ───────────────────────────────────────────────────────

interface UsePollingOptions {
  intervalMs?: number
  enabled?: boolean
  immediate?: boolean
}

export function usePolling<T>(
  fetchFn: () => Promise<ApiResponse<T>>,
  options: UsePollingOptions = {},
) {
  const { intervalMs = 5_000, enabled = true, immediate = true } = options

  const [data,        setData       ] = useState<T | null>(null)
  const [error,       setError      ] = useState<string | null>(null)
  const [loading,     setLoading    ] = useState(true)
  const [lastFetchAt, setLastFetchAt] = useState<string | null>(null)

  const timerRef    = useRef<ReturnType<typeof setInterval> | null>(null)
  const mountedRef  = useRef(true)
  const inFlightRef = useRef(false)

  // Stable ref so fetch_ callback never changes identity on re-render
  const fetchFnRef = useRef(fetchFn)
  fetchFnRef.current = fetchFn

  const fetch_ = useCallback(async (endpointHint = '') => {
    if (!mountedRef.current) return
    if (inFlightRef.current) {
      _logSkipOnce(endpointHint || 'unknown')
      return
    }
    inFlightRef.current = true
    try {
      const res = await fetchFnRef.current()
      if (!mountedRef.current) return
      if (res.ok) {
        setData(res.data)
        setError(null)
      } else {
        setError(res.reason || res.status)
      }
    } catch (err) {
      if (mountedRef.current) {
        setError(err instanceof Error ? err.message : 'Unknown error')
      }
    } finally {
      inFlightRef.current = false
      if (mountedRef.current) {
        setLoading(false)
        setLastFetchAt(new Date().toISOString())
      }
    }
  }, [])   // stable — reads via ref

  useEffect(() => {
    mountedRef.current  = true
    inFlightRef.current = false

    if (!enabled) {
      setLoading(false)
      return
    }

    if (immediate) fetch_()
    timerRef.current = setInterval(fetch_, intervalMs)

    return () => {
      mountedRef.current = false
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [enabled, intervalMs, immediate, fetch_])

  return {
    data,
    error,
    loading,
    lastFetchAt,
    refresh: () => fetch_(),
  }
}

// ─── Gated polling — pauses when backend is offline ─────────────────────────

function useGatedPolling<T>(
  fetchFn: () => Promise<ApiResponse<T>>,
  options: UsePollingOptions = {},
) {
  const backendOnline = useBackendOnline()
  return usePolling(fetchFn, {
    ...options,
    enabled: (options.enabled ?? true) && backendOnline,
  })
}

// ─── Convenience hooks — tuned intervals ─────────────────────────────────────

/** Health: always runs (NOT gated). Used by TopBar and CommandCenter. */
export const useHealth = () =>
  usePolling(() => api.health(), { intervalMs: 10_000 })

/** Dashboard status: gated, 5 s */
export const useDashboardStatus = () =>
  useGatedPolling(() => api.dashboardStatus(), { intervalMs: 5_000 })

/** Quad terminal: gated, 5 s */
export const useQuadTerminal = () =>
  useGatedPolling(() => api.quadTerminal(), { intervalMs: 5_000 })

/** Symbols: gated, 5 s */
export const useSymbols = () =>
  useGatedPolling(() => api.symbols(), { intervalMs: 5_000 })

/** Account snapshot: gated, 5 s */
export const useAccountSnapshot = () =>
  useGatedPolling(() => api.accountSnapshot(), { intervalMs: 5_000 })

/** Order flow: gated, 5 s */
export const useOrderFlow = () =>
  useGatedPolling(() => api.orderFlow(), { intervalMs: 5_000 })

/** Setup hunter: gated, 8 s */
export const useSetupHunter = () =>
  useGatedPolling(() => api.setupHunter(), { intervalMs: 8_000 })

/** Strategies: gated, 5 s */
export const useStrategies = (symbol?: string) =>
  useGatedPolling(() => api.strategies(symbol), { intervalMs: 5_000 })

/** Confirmations: gated, 5 s */
export const useConfirmations = (symbol?: string) =>
  useGatedPolling(() => api.confirmations(symbol), { intervalMs: 5_000 })

/** Risk: gated, 5 s */
export const useRisk = () =>
  useGatedPolling(() => api.risk(), { intervalMs: 5_000 })

/** Trades: gated, 8 s */
export const useTrades = () =>
  useGatedPolling(() => api.trades(), { intervalMs: 8_000 })

/** Logs: gated, 5 s */
export const useLogs = (opts?: Parameters<typeof api.logs>[0]) =>
  useGatedPolling(() => api.logs(opts), { intervalMs: 5_000 })

/** Audit safety: gated, 10 s */
export const useAuditSafety = () =>
  useGatedPolling(() => api.auditSafety(), { intervalMs: 10_000 })

/** Candles: gated, 5 s */
export const useCandles = (symbol: string, timeframe: string) =>
  useGatedPolling(() => api.candles(symbol, timeframe), { intervalMs: 5_000 })

/** BTC smart exit status: gated, 5 s */
export const useBtcStatus = () =>
  useGatedPolling(() => api.btcStatus(), { intervalMs: 5_000 })
