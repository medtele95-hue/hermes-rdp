import { clsx } from 'clsx'
import { Skeleton } from './Skeleton'

interface StatRowProps {
  label: string
  value?: React.ReactNode
  loading?: boolean
  mono?: boolean
  className?: string
  highlight?: 'green' | 'red' | 'amber' | 'blue' | 'muted'
}

export function StatRow({ label, value, loading, mono = true, className, highlight }: StatRowProps) {
  const colorMap = {
    green: 'text-accent-green',
    red: 'text-accent-red',
    amber: 'text-accent-amber',
    blue: 'text-accent-blue',
    muted: 'text-text-muted',
  }
  return (
    <div className={clsx('flex items-center justify-between py-1.5 border-b border-border/40 last:border-0', className)}>
      <span className="text-xs text-text-secondary">{label}</span>
      {loading ? (
        <Skeleton className="h-3 w-20" />
      ) : (
        <span className={clsx(
          'text-xs',
          mono && 'font-mono tabular-nums',
          highlight ? colorMap[highlight] : 'text-text-primary',
        )}>
          {value ?? <span className="text-text-muted">—</span>}
        </span>
      )}
    </div>
  )
}

export function StatGrid({ children, cols = 2 }: { children: React.ReactNode; cols?: number }) {
  return (
    <div className={clsx(
      'grid gap-x-6 gap-y-0',
      cols === 2 ? 'grid-cols-2' : cols === 3 ? 'grid-cols-3' : 'grid-cols-1',
    )}>
      {children}
    </div>
  )
}

export function SectionHeader({ title, right }: { title: string; right?: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between mb-3">
      <span className="text-2xs font-semibold uppercase tracking-widest text-text-muted">{title}</span>
      {right}
    </div>
  )
}

export function Num({ value, decimals = 2, prefix = '', suffix = '', emptyText = '—', positiveGreen = false }: {
  value?: number | null
  decimals?: number
  prefix?: string
  suffix?: string
  emptyText?: string
  positiveGreen?: boolean
}) {
  if (value == null) return <span className="text-text-muted font-mono">{emptyText}</span>
  const formatted = `${prefix}${value.toFixed(decimals)}${suffix}`
  if (positiveGreen) {
    return (
      <span className={clsx('font-mono tabular-nums', value > 0 ? 'text-accent-green' : value < 0 ? 'text-accent-red' : 'text-text-primary')}>
        {value > 0 ? '+' : ''}{formatted}
      </span>
    )
  }
  return <span className="font-mono tabular-nums text-text-primary">{formatted}</span>
}
