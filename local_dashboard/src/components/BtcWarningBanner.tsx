import { useBtcStatus } from '../api/usePolling'
import type { BtcStatusData } from '../api/types'

export function BtcWarningBanner() {
  const { data } = useBtcStatus()
  const status = data as BtcStatusData | undefined

  if (!status) return null

  const showEmergency = status.emergency_active
  const showFastExit = status.fast_exit_daemon_enabled

  if (!showEmergency && !showFastExit) return null

  const pnl = typeof status.floating_pnl === 'number'
    ? (status.floating_pnl >= 0 ? '+' : '') + status.floating_pnl.toFixed(2) + ' USD'
    : 'N/A'

  return (
    <div className="flex flex-col gap-0">
      {showFastExit && (
        <div className="bg-emerald-700 text-white px-4 py-1 text-xs font-medium flex items-center gap-2 border-b border-emerald-800">
          <span className="text-emerald-300">&#9679;</span>
          <span>
            <strong>Fast Smart Exit: ACTIVE</strong>
            &nbsp;&mdash; interval {status.fast_exit_interval_ms}ms &middot; min +${status.fast_exit_min_profit_usd?.toFixed(2)} &middot; hard +${status.fast_exit_hard_min_profit_usd?.toFixed(2)}
            {status.fast_exit_positive_candidates > 0 && (
              <>&nbsp;&middot; positive candidates: <strong>{status.fast_exit_positive_candidates}</strong></>
            )}
            {status.fast_exit_last_close_reason && (
              <>&nbsp;&middot; last close: <strong>{status.fast_exit_last_close_reason}</strong></>
            )}
          </span>
        </div>
      )}
      {showEmergency && (
        <div className="bg-red-600 text-white px-4 py-2 text-sm font-medium flex items-start gap-3 border-b border-red-700">
          <span className="text-red-200 shrink-0 mt-0.5">&#9888;</span>
          <div className="flex-1 min-w-0">
            <span className="font-bold">Emergency: multiple HERMES BTC demo positions open. New entries blocked. Smart positive exit active.</span>
            <span className="ml-4 text-red-100">
              Open: <strong>{status.open_count}</strong>
              &nbsp;&middot;&nbsp;Float PnL: <strong>{pnl}</strong>
              &nbsp;&middot;&nbsp;Positive candidates: <strong>{status.positive_candidates}</strong>
              {status.last_smart_exit_reason && (
                <>&nbsp;&middot;&nbsp;Last exit: <strong>{status.last_smart_exit_reason}</strong></>
              )}
            </span>
          </div>
        </div>
      )}
    </div>
  )
}
