import { clsx } from 'clsx'

type BadgeVariant = 'live' | 'wait' | 'block' | 'observe' | 'demo' | 'stale' | 'no-data' | 'green' | 'red' | 'amber' | 'blue' | 'purple' | 'muted'

interface BadgeProps {
  variant?: BadgeVariant
  children: React.ReactNode
  dot?: boolean
  className?: string
}

const variantClasses: Record<BadgeVariant, string> = {
  live: 'bg-accent-green/15 text-accent-green border-accent-green/30',
  wait: 'bg-surface-4 text-text-secondary border-border',
  block: 'bg-accent-red/15 text-accent-red border-accent-red/30',
  observe: 'bg-accent-purple/15 text-accent-purple border-accent-purple/30',
  demo: 'bg-accent-amber/15 text-accent-amber border-accent-amber/30',
  stale: 'bg-accent-orange/15 text-accent-orange border-accent-orange/30',
  'no-data': 'bg-surface-4 text-text-muted border-border',
  green: 'bg-accent-green/15 text-accent-green border-accent-green/30',
  red: 'bg-accent-red/15 text-accent-red border-accent-red/30',
  amber: 'bg-accent-amber/15 text-accent-amber border-accent-amber/30',
  blue: 'bg-accent-blue/15 text-accent-blue border-accent-blue/30',
  purple: 'bg-accent-purple/15 text-accent-purple border-accent-purple/30',
  muted: 'bg-surface-4 text-text-muted border-border',
}

const dotClasses: Record<BadgeVariant, string> = {
  live: 'bg-accent-green',
  wait: 'bg-text-muted',
  block: 'bg-accent-red',
  observe: 'bg-accent-purple',
  demo: 'bg-accent-amber',
  stale: 'bg-accent-orange',
  'no-data': 'bg-surface-4',
  green: 'bg-accent-green',
  red: 'bg-accent-red',
  amber: 'bg-accent-amber',
  blue: 'bg-accent-blue',
  purple: 'bg-accent-purple',
  muted: 'bg-text-muted',
}

export function Badge({ variant = 'muted', children, dot, className }: BadgeProps) {
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-2xs font-semibold font-mono uppercase tracking-wide border',
        variantClasses[variant],
        className,
      )}
    >
      {dot && (
        <span className={clsx('w-1.5 h-1.5 rounded-full flex-shrink-0', dotClasses[variant])} />
      )}
      {children}
    </span>
  )
}

export function StateBadge({ badge }: { badge: string }) {
  const map: Record<string, BadgeVariant> = {
    LIVE: 'live',
    WAIT: 'wait',
    BLOCK: 'block',
    OBSERVE_ONLY: 'observe',
    STALE: 'stale',
    NO_DATA: 'no-data',
    STARTING: 'muted',
    RUNNING: 'live',
    DEMO: 'demo',
  }
  const v = map[badge?.toUpperCase()] ?? 'muted'
  return <Badge variant={v} dot={v === 'live'}>{badge}</Badge>
}

export function DecisionBadge({ decision }: { decision: string | null | undefined }) {
  if (!decision) return <Badge variant="no-data">—</Badge>
  const d = decision.toUpperCase()
  if (d === 'ROUTE_TO_DEMO' || d === 'BUY' || d === 'SELL') return <Badge variant="live" dot>{decision}</Badge>
  if (d.includes('BLOCK') || d === 'REJECT') return <Badge variant="block">{decision}</Badge>
  if (d === 'WAIT' || d === 'WAIT_ANALYSIS_ONLY') return <Badge variant="wait">{decision}</Badge>
  if (d.includes('OBSERVE')) return <Badge variant="observe">{decision}</Badge>
  return <Badge variant="muted">{decision}</Badge>
}

export function DirectionBadge({ direction }: { direction: string | null | undefined }) {
  if (!direction) return <Badge variant="no-data">—</Badge>
  const d = direction.toUpperCase()
  if (d === 'BUY') return <Badge variant="green">▲ BUY</Badge>
  if (d === 'SELL') return <Badge variant="red">▼ SELL</Badge>
  return <Badge variant="wait">{direction}</Badge>
}

export function GradeBadge({ grade }: { grade: string | null | undefined }) {
  if (!grade) return null
  const map: Record<string, BadgeVariant> = {
    'A+': 'green', A: 'green', B: 'blue', C: 'amber', D: 'red',
  }
  return <Badge variant={map[grade] ?? 'muted'}>{grade}</Badge>
}

export function FreshnessBadge({ freshness }: { freshness: string }) {
  const map: Record<string, BadgeVariant> = {
    FRESH: 'live', RECENT: 'green', STALE: 'stale', VERY_STALE: 'block', NO_DATA: 'no-data', UNKNOWN: 'muted',
  }
  return <Badge variant={map[freshness] ?? 'muted'}>{freshness}</Badge>
}
