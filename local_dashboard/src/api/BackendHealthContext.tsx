/**
 * BackendHealthContext — single health gate for the entire dashboard.
 *
 * Polls /local-api/health once per app (not per component).
 * Uses exponential backoff when offline: 2s → 4 → 8 → 16 → 30s max.
 * When online: polls every 10 s.
 * Exposes `online` flag consumed by useGatedPolling to stop all heavy polls.
 */
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'

export type BackendState =
  | 'BACKEND_ONLINE'
  | 'BACKEND_OFFLINE'
  | 'BACKEND_RECONNECTING'
  | 'BACKEND_SLOW'

export interface BackendHealthValue {
  online: boolean
  backendState: BackendState
  lastOnlineAt: string | null
  retryInMs: number | null
}

const DEFAULTS: BackendHealthValue = {
  online: true,          // optimistic — prevents flash of "offline" on first load
  backendState: 'BACKEND_ONLINE',
  lastOnlineAt: null,
  retryInMs: null,
}

const BackendHealthContext = createContext<BackendHealthValue>(DEFAULTS)

export function useBackendOnline(): boolean {
  return useContext(BackendHealthContext).online
}

export function useBackendHealthContext(): BackendHealthValue {
  return useContext(BackendHealthContext)
}

// ─── Timing constants ────────────────────────────────────────────────────────
const ONLINE_INTERVAL_MS  = 10_000
const BACKOFF_INIT_MS     =  2_000
const BACKOFF_MAX_MS      = 30_000
const SLOW_THRESHOLD_MS   =  1_500
const REQUEST_TIMEOUT_MS  =  3_000

// ─── Rate-limited logging ────────────────────────────────────────────────────
const _cooldowns: Record<string, number> = {}
function _once(key: string, fn: () => void, cooldownMs = 5_000): void {
  const now = Date.now()
  if (!_cooldowns[key] || now - _cooldowns[key] > cooldownMs) {
    fn()
    _cooldowns[key] = now
  }
}

// ─── Provider ────────────────────────────────────────────────────────────────
export function BackendHealthProvider({ children }: { children: ReactNode }) {
  const [value, setValue] = useState<BackendHealthValue>(DEFAULTS)
  const backoffRef      = useRef(BACKOFF_INIT_MS)
  const offlineCountRef = useRef(0)
  const inFlightRef     = useRef(false)
  const mountedRef      = useRef(true)
  const timerRef        = useRef<ReturnType<typeof setTimeout> | null>(null)

  const schedule = useCallback((delayMs: number, fn: () => void) => {
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(fn, delayMs)
  }, [])

  useEffect(() => {
    mountedRef.current = true

    async function check(): Promise<void> {
      if (!mountedRef.current || inFlightRef.current) return
      inFlightRef.current = true
      const t0 = Date.now()
      let nextMs = ONLINE_INTERVAL_MS

      try {
        const ctrl = new AbortController()
        const killTimer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS)
        const res = await fetch('/local-api/health', {
          signal: ctrl.signal,
          headers: { Accept: 'application/json' },
        })
        clearTimeout(killTimer)
        const elapsed = Date.now() - t0

        if (!mountedRef.current) return

        if (res.ok) {
          const wasOffline = offlineCountRef.current > 0
          offlineCountRef.current  = 0
          backoffRef.current       = BACKOFF_INIT_MS
          const now = new Date().toISOString()
          const st: BackendState   = elapsed > SLOW_THRESHOLD_MS ? 'BACKEND_SLOW' : 'BACKEND_ONLINE'
          setValue({ online: true, backendState: st, lastOnlineAt: now, retryInMs: null })
          if (wasOffline) {
            console.info(`[DASHBOARD_BACKEND_ONLINE] recovered=true latency_ms=${elapsed}`)
          } else {
            _once('poll-ok', () =>
              console.debug(`[DASHBOARD_POLL] endpoint=/health status=ok interval=${ONLINE_INTERVAL_MS}`),
            30_000)
          }
          nextMs = ONLINE_INTERVAL_MS
        } else {
          throw new Error(`HTTP ${res.status}`)
        }
      } catch {
        if (!mountedRef.current) return
        offlineCountRef.current++
        const delay = Math.min(backoffRef.current, BACKOFF_MAX_MS)
        backoffRef.current = Math.min(backoffRef.current * 2, BACKOFF_MAX_MS)
        const st: BackendState =
          offlineCountRef.current === 1 ? 'BACKEND_OFFLINE' : 'BACKEND_RECONNECTING'
        setValue(prev => ({ ...prev, online: false, backendState: st, retryInMs: delay }))
        _once('offline', () =>
          console.warn(`[DASHBOARD_BACKEND_OFFLINE] reason=ECONNREFUSED next_retry_ms=${delay}`),
        5_000)
        nextMs = delay
      } finally {
        inFlightRef.current = false
      }

      if (mountedRef.current) schedule(nextMs, check)
    }

    check()

    return () => {
      mountedRef.current  = false
      inFlightRef.current = false
      if (timerRef.current) clearTimeout(timerRef.current)
    }
  }, [schedule])

  return (
    <BackendHealthContext.Provider value={value}>
      {children}
    </BackendHealthContext.Provider>
  )
}
