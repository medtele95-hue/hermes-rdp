import { useBackendHealthContext } from '../api/BackendHealthContext'

export function OfflineBanner() {
  const { online, backendState, lastOnlineAt, retryInMs } = useBackendHealthContext()

  if (online) return null

  const retryLabel = retryInMs != null
    ? `retrying in ${Math.round(retryInMs / 1_000)}s`
    : 'retrying…'

  const lastLabel = lastOnlineAt
    ? `last online ${new Date(lastOnlineAt).toLocaleTimeString()}`
    : 'never connected'

  return (
    <div
      role="status"
      aria-live="polite"
      className="w-full bg-red-900/80 text-red-100 text-xs px-4 py-1.5 flex items-center gap-3 z-50"
    >
      <span className="font-semibold uppercase tracking-wide">
        {backendState === 'BACKEND_RECONNECTING' ? 'Reconnecting…' : 'Backend Offline'}
      </span>
      <span className="opacity-70">—</span>
      <span className="opacity-70">{lastLabel}</span>
      <span className="opacity-70">{retryLabel}</span>
      <span className="ml-auto opacity-60">
        Showing cached data where available
      </span>
    </div>
  )
}
