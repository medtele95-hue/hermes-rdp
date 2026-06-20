import { useSetupHunter } from '../api/usePolling'
import { Badge, DirectionBadge, GradeBadge, DecisionBadge } from '../components/ui/Badge'
import { StatRow, Num, SectionHeader } from '../components/ui/StatRow'
import { NoDataState, SkeletonCard } from '../components/ui/Skeleton'

function CandidateCard({
  candidate,
  accepted,
}: {
  candidate: Record<string, unknown> & { reject_reason?: string }
  accepted: boolean
}) {
  return (
    <div className={`card p-3 border-l-2 ${accepted ? 'border-accent-green' : 'border-accent-red/40'}`}>
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <span className="text-xs font-mono font-medium text-text-primary">
            {String(candidate.symbol ?? '—')}
          </span>
          <DirectionBadge direction={String(candidate.direction ?? '')} />
        </div>
        <div className="flex items-center gap-2">
          <GradeBadge grade={String(candidate.grade ?? '')} />
          {accepted ? (
            <Badge variant="green" dot>ACCEPTED</Badge>
          ) : (
            <Badge variant="block">REJECTED</Badge>
          )}
        </div>
      </div>
      <div className="grid grid-cols-2 gap-x-6">
        <StatRow label="Strategy" value={String(candidate.strategy ?? candidate.best_strategy ?? '—')} />
        <StatRow label="Score" value={
          candidate.score != null ? (candidate.score as number).toFixed(1) : '—'
        } />
        <StatRow label="Entry" value={<Num value={candidate.entry as number} decimals={5} />} />
        <StatRow label="SL" value={<Num value={candidate.sl as number} decimals={5} />} />
        <StatRow label="TP" value={<Num value={candidate.tp as number} decimals={5} />} />
        <StatRow label="RR" value={
          candidate.rr != null ? `${(candidate.rr as number).toFixed(2)}:1` : '—'
        } />
        {accepted ? (
          <StatRow label="Analysis only" value={
            <Badge variant={candidate.analysis_only ? 'observe' : 'muted'}>
              {String(candidate.analysis_only ?? false)}
            </Badge>
          } mono={false} />
        ) : (
          <>
            <StatRow label="Reject reason" value={
              <Badge variant="stale">{String(candidate.reject_reason ?? '—')}</Badge>
            } mono={false} />
          </>
        )}
      </div>
      {!accepted && Array.isArray(candidate.failed_gates) && candidate.failed_gates.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {(candidate.failed_gates as string[]).map((g, i) => (
            <Badge key={i} variant="stale">{g}</Badge>
          ))}
        </div>
      )}
    </div>
  )
}

export function SetupHunterPage() {
  const { data, loading, error } = useSetupHunter()

  if (loading && !data) {
    return (
      <div className="p-6 grid grid-cols-2 gap-4">
        {Array.from({ length: 4 }).map((_, i) => <SkeletonCard key={i} />)}
      </div>
    )
  }

  return (
    <div className="p-6 space-y-6 max-w-screen-2xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Setup Hunter</h1>
          <p className="text-xs text-text-muted">Candidate evaluation pipeline — read-only</p>
        </div>
        <div className="flex gap-3">
          <Badge variant="green">{data?.edge_ready_count ?? 0} ACCEPTED</Badge>
          <Badge variant="stale">{data?.near_miss_count ?? 0} REJECTED</Badge>
        </div>
      </div>

      {/* Best candidate */}
      {data?.best_candidate ? (
        <div className="card p-4">
          <SectionHeader title="Best Candidate This Cycle" />
          <div className="grid grid-cols-3 gap-x-6 gap-y-0">
            <StatRow label="Symbol" value={String(data.best_candidate.symbol ?? '—')} />
            <StatRow label="Strategy" value={String(data.best_candidate.best_strategy ?? data.best_candidate.strategy ?? '—')} />
            <StatRow label="Direction" value={<DirectionBadge direction={data.best_candidate.direction} />} mono={false} />
            <StatRow label="Grade" value={<GradeBadge grade={data.best_candidate.grade} />} mono={false} />
            <StatRow label="Edge score" value={data.best_candidate.edge_score != null ? data.best_candidate.edge_score.toFixed(1) : '—'} />
            <StatRow label="Demo eligible" value={
              <Badge variant={data.best_candidate.demo_eligible ? 'green' : 'muted'}>
                {String(data.best_candidate.demo_eligible ?? false)}
              </Badge>
            } mono={false} />
            <StatRow label="Entry" value={<Num value={data.best_candidate.entry as number} decimals={5} />} />
            <StatRow label="SL" value={<Num value={data.best_candidate.sl as number} decimals={5} />} />
            <StatRow label="TP" value={<Num value={data.best_candidate.tp as number} decimals={5} />} />
            <StatRow label="RR" value={data.best_candidate.rr != null ? `${(data.best_candidate.rr as number).toFixed(2)}:1` : '—'} />
            <StatRow label="Confluence grade" value={<GradeBadge grade={data.best_candidate.confluence_grade} />} mono={false} />
            <StatRow label="Analysis only" value={String(data.best_candidate.analysis_only ?? false)} />
          </div>
        </div>
      ) : (
        <div className="card p-4">
          <NoDataState label="NO_CANDIDATE" sub="No setup candidate available this cycle" />
        </div>
      )}

      <div className="grid grid-cols-2 gap-6">
        {/* Accepted */}
        <div>
          <SectionHeader title={`Accepted Candidates (${data?.accepted_candidates?.length ?? 0})`} />
          <div className="space-y-2">
            {(data?.accepted_candidates ?? []).length === 0 ? (
              <NoDataState label="NONE_ACCEPTED" sub="No edge-ready candidates this cycle" />
            ) : (
              (data?.accepted_candidates ?? []).map((c, i) => (
                <CandidateCard key={i} candidate={c as unknown as Record<string, unknown>} accepted />
              ))
            )}
          </div>
        </div>

        {/* Rejected */}
        <div>
          <SectionHeader title={`Rejected Candidates (${data?.rejected_candidates?.length ?? 0})`} />
          <div className="space-y-2">
            {(data?.rejected_candidates ?? []).length === 0 ? (
              <NoDataState label="NONE_REJECTED" sub="All candidates accepted" />
            ) : (
              (data?.rejected_candidates ?? []).slice(0, 20).map((c, i) => (
                <CandidateCard key={i} candidate={c as unknown as Record<string, unknown>} accepted={false} />
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
