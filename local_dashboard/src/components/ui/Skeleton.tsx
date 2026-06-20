import { clsx } from 'clsx'

export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      className={clsx(
        'animate-pulse rounded bg-surface-3',
        className,
      )}
    />
  )
}

export function SkeletonCard({ rows = 4 }: { rows?: number }) {
  return (
    <div className="card p-4 space-y-3">
      <Skeleton className="h-4 w-32" />
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex justify-between">
          <Skeleton className="h-3 w-24" />
          <Skeleton className="h-3 w-16" />
        </div>
      ))}
    </div>
  )
}

export function SkeletonTable({ rows = 5, cols = 4 }: { rows?: number; cols?: number }) {
  return (
    <div className="space-y-1">
      {Array.from({ length: rows }).map((_, r) => (
        <div key={r} className="flex gap-4 px-3 py-2">
          {Array.from({ length: cols }).map((_, c) => (
            <Skeleton key={c} className="h-3 flex-1" />
          ))}
        </div>
      ))}
    </div>
  )
}

export function ErrorState({ message }: { message: string }) {
  return (
    <div className="flex items-center gap-2 p-4 rounded-lg bg-accent-red/10 border border-accent-red/20 text-accent-red text-xs font-mono">
      <span className="flex-shrink-0">✗</span>
      <span>{message}</span>
    </div>
  )
}

export function NoDataState({ label = 'NO_DATA', sub }: { label?: string; sub?: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-12 gap-2 text-text-muted">
      <span className="text-2xl opacity-30">◎</span>
      <span className="text-xs font-mono uppercase tracking-widest">{label}</span>
      {sub && <span className="text-2xs text-text-muted/60">{sub}</span>}
    </div>
  )
}
