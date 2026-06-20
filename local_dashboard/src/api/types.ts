// ─── Core API response wrapper ─────────────────────────────────────────────
export interface ApiOk<T> {
  ok: true
  data: T
  timestamp: string
}

export interface ApiError {
  ok: false
  status: string
  reason: string
  timestamp: string
}

export type ApiResponse<T> = ApiOk<T> | ApiError

// ─── Health / Status ────────────────────────────────────────────────────────
export interface HealthData {
  backend_status: 'LIVE' | 'STALE' | 'STARTING'
  backend_started_at: string
  last_heartbeat_at: string
  heartbeat_age_seconds: number | null
  stale: boolean
  mt5_connected: boolean
  demo_only: boolean
  allow_live_trading: boolean
  cycle_status: string
  session_name: string | null
  account_equity: number | null
  account_balance: number | null
  resolved_symbols: string[]
  ingest_health: { status: string; reason: string | null }
  safety_proof: {
    allow_live_trading: false
    demo_only: true
    order_send_location: string
  }
}

// ─── Account ────────────────────────────────────────────────────────────────
export interface AccountSnapshot {
  login: number | null
  name: string | null
  server: string | null
  company: string | null
  account_type: string | null
  currency: string
  balance: number | null
  equity: number | null
  margin: number | null
  free_margin: number | null
  margin_level: number | null
  floating_pnl: number | null
  closed_pnl_today: number | null
  total_pnl_today: number | null
  daily_pnl: number | null
  open_positions_count: number | null
  snapshot_time: string | null
  trade_allowed: boolean | null
  trade_expert: boolean | null
}

// ─── Candles ─────────────────────────────────────────────────────────────────
export interface Candle {
  time: number
  open: number
  high: number
  low: number
  close: number
  tick_volume?: number
  spread?: number
  candle_time?: string
}

export interface CandleData {
  symbol: string
  timeframe: string
  count: number
  candles: Candle[]
}

// ─── Symbol State ─────────────────────────────────────────────────────────────
export interface SymbolState {
  symbol: string
  internal_key: string
  broker_symbol: string | null
  price: number | null
  spread: number | null
  spread_status: string | null
  session: string | null
  time_gate: string | null
  latest_decision: string
  latest_reason: string | null
  route_status: string
  last_update_utc: string | null
  data_freshness: 'FRESH' | 'RECENT' | 'STALE' | 'VERY_STALE' | 'NO_DATA' | 'UNKNOWN'
}

// ─── Order Flow ──────────────────────────────────────────────────────────────
export interface OrderFlowSnapshot {
  display_symbol: string
  internal_key?: string
  poc: number | null
  vah: number | null
  val: number | null
  vwap: number | null
  cvd_slope?: number | null
  delta?: number | null
  divergence?: string | null
  order_flow_score?: number | null
  signal?: string | null
  status?: string | null
  latest_decision?: string | null
  latest_reason?: string | null
  mode: string
  reader_status?: string | null
  execution_agent_status?: string | null
}

// ─── Candidate / Confirmation ────────────────────────────────────────────────
export interface Candidate {
  symbol: string
  broker_symbol?: string
  strategy?: string
  direction?: string
  smc_score?: number | null
  smc_status?: string | null
  mtfa_score?: number | null
  mtfa_status?: string | null
  confluence_score?: number | null
  confluence_grade?: string | null
  geometry_score?: number | null
  breakout_score?: number | null
  order_flow_status?: string | null
  time_gate?: string | null
  spread_status?: string | null
  hard_block?: boolean | null
  block_reason?: string | null
  failed_gates?: string[]
  missing_confirmations?: string[]
  demo_eligible?: boolean
  analysis_only?: boolean
  // Plan fields
  entry?: number | null
  sl?: number | null
  tp?: number | null
  tp2?: number | null
  rr?: number | null
  grade?: string | null
  edge_score?: number | null
  confidence?: number | null
  best_strategy?: string | null
}

// ─── Strategy Signal ─────────────────────────────────────────────────────────
export interface StrategySignal {
  strategy?: string
  setup_type?: string
  decision?: string
  direction?: string
  score?: number | null
  confidence?: number | null
  grade?: string | null
  reason?: string | null
  demo_eligible?: boolean | null
  route_allowed?: boolean | null
  mode?: string | null
  status?: string | null
}

// ─── Quad Terminal Card ──────────────────────────────────────────────────────
export interface SymbolCard {
  symbol: string
  available: boolean
  state_badge: 'LIVE' | 'WAIT' | 'BLOCK' | 'OBSERVE_ONLY' | 'STALE' | 'NO_DATA'
  price: number | null
  spread: number | null
  spread_status: string | null
  session: string | null
  time_gate: string | null
  latest_decision: string
  latest_reason: string | null
  route_status: string
  last_update_utc: string | null
  candles: Record<string, {
    available: boolean
    count: number
    last_close: number | null
    last_time: string | null
  }>
  confirmations: Partial<Candidate>
  plan: {
    direction?: string | null
    bias?: string | null
    entry?: number | null
    sl?: number | null
    tp?: number | null
    tp2?: number | null
    rr?: number | null
    confidence?: number | null
    grade?: string | null
    analysis_only?: boolean
    blocked?: boolean
    block_reason?: string | null
    best_strategy?: string | null
    demo_eligible?: boolean
  }
  strategies: StrategySignal[]
  order_flow: OrderFlowSnapshot
  levels: {
    poc?: number | null
    vah?: number | null
    val?: number | null
    vwap?: number | null
    support?: number | null
    resistance?: number | null
    entry?: number | null
    sl?: number | null
    tp1?: number | null
    tp2?: number | null
  }
  mode: string
}

export interface QuadTerminalData {
  symbols: string[]
  cards: Record<string, SymbolCard>
  generated_at: string
  note: string
}

// ─── Setup Hunter ────────────────────────────────────────────────────────────
export interface SetupHunterData {
  best_candidate: Candidate | null
  edge_ready_count: number
  near_miss_count: number
  accepted_candidates: Candidate[]
  rejected_candidates: (Candidate & { reject_reason: string })[]
  setup_hunter_block: Record<string, unknown>
}

// ─── Risk ─────────────────────────────────────────────────────────────────────
export interface RiskData {
  account: {
    balance?: number | null
    equity?: number | null
    margin?: number | null
    free_margin?: number | null
    floating_pnl?: number | null
    closed_pnl_today?: number | null
    daily_pnl?: number | null
    total_pnl_today?: number | null
  }
  limits: {
    demo_only: boolean
    allow_live_trading: boolean
    demo_max_lot: number
    demo_max_open_trades: number
    demo_max_trades_per_day: number
    demo_max_daily_loss_pct: number
    demo_max_risk_per_trade_pct: number
    demo_stop_after_consecutive_losses: number
  }
  open_positions_count: number
  safety_guard: Record<string, unknown> | null
  risk_exposure: Record<string, unknown>
  guards: string[]
}

// ─── Trades ──────────────────────────────────────────────────────────────────
export interface Trade {
  symbol: string
  ticket?: number | null
  position_id?: number | string | null
  deal_ticket?: number | string | null
  magic?: number | null
  strategy?: string | null
  direction?: string | null
  entry?: number | null
  sl?: number | null
  tp?: number | null
  lot?: number | null
  pnl?: number | null
  profit?: number | null
  commission?: number | null
  swap?: number | null
  comment?: string | null
  source?: string | null
  timestamp?: string | null
  open_time?: string | null
  close_time?: string | null
  note?: string | null
  reason?: string | null
  exit_price?: number | null
}

export interface TradesData {
  open_trades: Trade[]
  closed_trades: Trade[]
  open_count?: number
  closed_count?: number
  open_source?: string
  closed_source?: string
  pnl_source?: string
  mt5_closed_deals_count?: number
  fallback_used?: boolean
  history_error?: string | null
  note?: string | null
}

// ─── Logs ────────────────────────────────────────────────────────────────────
export interface LogEntry {
  timestamp: string
  level: string
  message: string
  logger?: string
}

// ─── Setup Audit ─────────────────────────────────────────────────────────────
export interface AuditRecord {
  event_type: string
  timestamp_utc: string | null
  timestamp_casa: string | null
  hour: string
  symbol: string
  broker_symbol: string | null
  strategy: string
  direction: string | null
  entry: number | null
  stop_loss: number | null
  take_profit: number | null
  rr: number | null
  score: number | null
  grade: string | null
  confidence: number | null
  demo_eligible: boolean | null
  execution_candidate: boolean | null
  execution_policy: string | null
  time_gate_status: string | null
  session: string
  smc_status: string | null
  smc_score: number | null
  mtfa_status: string | null
  mtfa_score: number | null
  top_down_status: string | null
  missing_confirmations: string[]
  top_down_missing: boolean
  top_down_missing_fields: string[]
  setup_reason: string | null
  rejection_reason: string | null
  final_status: string
  pipeline_stages: string[]
  sh_decision: 'ACCEPT' | 'REJECT'
  safety_decision: 'PASS' | 'BLOCK' | 'N/A'
  router_decision: 'PASS' | 'BLOCK' | 'N/A'
  mt5_status: 'OPENED' | 'SYNCED_CLOSED' | 'SENT' | 'NOT_SENT'
}

export interface SetupAuditData {
  generated_at: string
  hours: number
  total_setups_detected: number
  setup_hunter_accepted: number
  setup_hunter_rejected: number
  safety_guard_pass: number
  safety_guard_blocked: number
  demo_router_sent: number
  executed_by_hermes: number
  mt5_synced_closed: number
  accepted_but_router_blocked: number
  accepted_but_not_sent: number
  top_down_missing_count: number
  top_missing_fields: Record<string, number>
  top_down_missing_by_symbol: Record<string, number>
  by_final_status: Record<string, number>
  by_symbol: Record<string, number>
  by_strategy: Record<string, number>
  by_hour: Record<string, number>
  by_session: Record<string, number>
  top_block_reasons: Record<string, number>
  top_symbols_by_accepted: Record<string, number>
  top_strategies_by_accepted: Record<string, number>
  accepted_not_executed_count: number
  accepted_not_executed: AuditRecord[]
  executed_records: AuditRecord[]
  synced_closed_records: AuditRecord[]
}

// ─── HERMES BTC Smart Exit Status ───────────────────────────────────────────
export interface BtcStatusData {
  open_count: number
  floating_pnl: number
  positive_candidates: number
  last_smart_exit_reason: string | null
  emergency_active: boolean
  max_open_positions: number
  smart_exit_enabled: boolean
  danger_exit_min_usd: number
  demo_only: boolean
  allow_live_trading: boolean
  // Fast Smart Exit daemon
  fast_exit_daemon_enabled: boolean
  fast_exit_interval_ms: number
  fast_exit_min_profit_usd: number
  fast_exit_hard_min_profit_usd: number
  fast_exit_close_at_any_positive: boolean
  fast_exit_last_tick: string | null
  fast_exit_positive_candidates: number
  fast_exit_last_close_ticket: number | null
  fast_exit_last_close_profit: number | null
  fast_exit_last_close_reason: string | null
  fast_exit_last_error: string | null
  timestamp: string
}

// ─── Dashboard Snapshot ──────────────────────────────────────────────────────
export interface DashboardSnapshot {
  mode: string
  demo_only: boolean
  allow_live_trading: boolean
  mt5_connected: boolean
  session_name?: string
  cycle_status?: Record<string, unknown>
  symbols?: Record<string, unknown>
  account?: Record<string, unknown>
  setup_hunter?: Record<string, unknown>
  safety_guard?: Record<string, unknown>
  strategy_manager?: Record<string, unknown>
  order_flow?: Record<string, unknown>
  [key: string]: unknown
}
