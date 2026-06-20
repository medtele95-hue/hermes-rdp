from __future__ import annotations

import os
from functools import lru_cache
from typing import List

from dotenv import load_dotenv
from pydantic import BaseModel


load_dotenv()


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _optional_float_env(name: str) -> float | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    return float(value)


def _int_env(name: str, default: int, fallback: str | None = None) -> int:
    value = os.getenv(name)
    if value is None and fallback:
        value = os.getenv(fallback)
    if value is None or value.strip() == "":
        return default
    return int(value)


class Settings(BaseModel):
    hermes_ingest_url: str = ""
    hermes_ingest_secret: str = ""
    lovable_ingest_timeout_seconds: float = 2.0
    lovable_ingest_fail_soft: bool = True
    lovable_ingest_circuit_breaker_enabled: bool = True
    lovable_ingest_circuit_breaker_seconds: float = 300.0

    read_only: bool = True
    paper_trading: bool = False
    demo_trading: bool = False
    allow_live_trading: bool = False
    demo_only: bool = True
    demo_pilot_enabled: bool = False
    demo_pilot_hours: int = 24
    demo_magic_number: int = 909002
    demo_comment: str = "HERMES_DEMO_KELLY_24H"
    demo_max_lot: float = 0.01
    demo_max_open_trades: int = 1
    demo_max_trades_per_day: int = 5
    demo_max_trades_per_day_total: int = 15
    demo_max_trades_per_symbol_per_day: int = 5
    demo_max_open_trades_total: int = 3
    demo_max_open_trades_per_symbol: int = 1
    demo_max_open_trades_per_symbol_strategy: int = 1
    demo_max_daily_loss_pct: float = 1.0
    demo_max_risk_per_trade_pct: float = 0.25
    demo_stop_after_consecutive_losses: int = 3
    quick_exit_enabled: bool = True
    quick_exit_demo_only: bool = True
    quick_exit_magic_number: int = 909002
    quick_exit_tp_usd: float = 1.50
    quick_exit_lock_usd: float = 0.80
    quick_exit_be_buffer_usd: float = 0.10
    quick_exit_trail_start_usd: float = 1.00
    quick_exit_trail_gap_usd: float = 0.60
    demo_allow_btc_weekend_bad_hour: bool = False
    demo_allow_contest: bool = False
    demo_allowed_login: str = ""
    demo_pilot_started_at: str = ""
    demo_exploration_mode: bool = True
    demo_exploration_max_lot: float = 0.01
    demo_exploration_min_rr: float = 2.0
    demo_exploration_min_edge_score: float = 90.0
    demo_exploration_allow_smc_fail: bool = True
    demo_exploration_allow_mtfa_fail: bool = True
    demo_exploration_max_trades_per_day: int = 3
    demo_exploration_max_trades_per_day_total: int = 15
    demo_exploration_max_trades_per_symbol_per_day: int = 5
    demo_exploration_ignore_bad_hour: bool = False
    demo_test_ignore_bad_hours: bool = False
    demo_ignore_all_time_blocks: bool = True
    demo_ignore_session_blocks: bool = True
    demo_ignore_bad_hour_blocks: bool = True
    demo_ignore_duration_blocks: bool = True
    demo_ignore_setup_wait_hours: bool = True
    max_money_tp_enabled: bool = True
    max_tp_usd: float = 2.0
    max_tp_applies_to: str = "BTCUSD#,BTCUSD,GOLD#,GOLD,XAUUSD,EURUSD"
    demo_strong_setup_learning_mode: bool = False
    demo_strong_setup_min_edge: float = 95.0
    demo_strong_setup_min_rr: float = 2.0
    demo_strong_setup_max_trades_per_day: int = 3
    demo_strong_setup_max_open_trades: int = 1
    demo_strong_setup_max_trades_per_day_total: int = 15
    demo_strong_setup_max_trades_per_symbol_per_day: int = 5
    demo_smoke_test_24h: bool = False
    demo_smoke_test_max_confirmed_orders: int = 1
    demo_smoke_test_end_after_hours: int = 24
    hermes_free_demo_discovery_mode: bool = False
    hermes_demo_topdown_fallback_mode: bool = False
    hermes_demo_micro_discovery_mode: bool = False
    hermes_adaptive_confluence_enabled: bool = True
    hermes_default_min_confluence: float = 60.0
    hermes_hard_avoid_confluence: float = 45.0
    btcusd_min_confluence: float = 65.0
    btcusd_topdown_min: float = 65.0
    btcusd_m15_required: bool = True
    btcusd_m1_required: bool = True
    gold_min_confluence: float = 55.0
    gold_topdown_min: float = 55.0
    gold_m15_required: bool = True
    gold_m1_required: bool = False
    eurusd_min_confluence: float = 60.0
    eurusd_topdown_min: float = 60.0
    eurusd_m15_required: bool = True
    eurusd_m1_required: bool = True
    hermes_trade_symbols: str = "BTCUSD#,BTCUSD,GOLD#,GOLD,XAUUSD,EURUSD"
    hermes_analysis_only_symbols: str = ""
    gold_liquidity_mode: str = "false"
    gold_liquidity_strategy_enabled: bool = False
    gold_disable_generic_strategies: bool = True
    gold_pivot_length: int = 15
    gold_atr_zone_thickness: float = 0.5
    gold_zone_capacity: float = 5.0
    gold_max_zones_per_side: int = 10
    gold_min_zone_stars: int = 3
    gold_min_liquidity_score: int = 75
    gold_min_rr: float = 2.0
    gold_max_open_trades: int = 1
    gold_allowed_signals: str = "ABS,REJ"
    gold_observer_signals: str = "EXH,DIV"
    gold_m1m5_relaxed_demo_mode: bool = True
    gold_m1m5_relaxed_after_hours_no_setup: int = 24
    gold_m1m5_min_score_strict: int = 75
    gold_m1m5_min_score_relaxed: int = 65
    gold_m1m5_atr_low_relax_factor: float = 0.80
    gold_liquidity_relaxed_zone_stars: int = 2
    order_flow_reader_enabled: bool = True
    gold_order_flow_execution_enabled: bool = False
    gold_order_flow_min_confidence: int = 70
    gold_order_flow_require_divergence: bool = True
    strict_gold_order_flow_topdown: bool = False
    order_flow_execution_enabled: bool = False
    order_flow_min_score: int = 75
    order_flow_min_rr: float = 1.5
    order_flow_cooldown_minutes: int = 15
    order_flow_allowed_symbols: str = "BTCUSD,BTCUSD#,GOLD,GOLD#,XAUUSD,EURUSD"
    hermes_strategy_pack_enabled: bool = True
    hermes_entry_gates_enabled: bool = False
    hermes_setup_tier_enabled: bool = False
    hermes_confluence_strategy_aware: bool = False
    of_native_key_level_tol_atr: float = 0.25
    geometric_confluence_mode: str = "SHADOW"
    geometric_confirm_bonus: float = 5.0
    geometric_ratio_tolerance: float = 0.05
    fib_confluence_execution_enabled: bool = False
    fib_confluence_lookback: int = 30
    fib_confluence_struct_window: int = 15
    fib_confluence_vol_period: int = 20
    fib_confluence_vol_mult: float = 1.5
    fib_confluence_liq_lookback: int = 10
    fib_confluence_sl_atr_mult: float = 0.5
    fib_confluence_rr: float = 2.0
    fib_confluence_max_spread_atr_frac: float = 0.15
    fib_confluence_min_stop_distance_points: int = 0
    eur_ema_rsi_atr_enabled: bool = True
    eur_fast_ema: int = 20
    eur_slow_ema: int = 50
    eur_rsi_period: int = 14
    eur_rsi_buy_max: float = 70.0
    eur_rsi_sell_min: float = 30.0
    eur_atr_period: int = 14
    eur_atr_mult: float = 1.5
    eur_rr: float = 2.0
    eur_ema_rsi_atr_relaxed_demo_mode: bool = True
    eur_relaxed_after_hours_no_setup: int = 24
    eur_allow_near_cross: bool = True
    eur_near_cross_max_distance_atr: float = 0.15
    max_open_eur_trades: int = 1
    demo_allow_asia_trading: bool = False

    hermes_magic_number: int = 909001
    symbols: str = "BTCUSD,XAUUSD,EURUSD"
    main_timeframe: str = "M5"
    poll_seconds: int = 5

    max_risk_per_trade: float = 0.5
    max_daily_loss: float = 3.0
    max_drawdown: float = 10.0
    max_open_hermes_trades: int = 1
    max_spread: float = 30
    max_spread_btcusd: float | None = None
    max_spread_gold: float | None = None
    max_spread_eurusd: float | None = None
    paper_max_lot: float = 0.30
    paper_max_lot_btcusd: float | None = None
    paper_max_lot_gold: float | None = None
    paper_max_lot_eurusd: float | None = None
    paper_max_hold_minutes: int = 30
    learning_mode: bool = True
    auto_apply_learning: bool = True
    learning_min_setups: int = 200
    learning_batch_size: int = 25
    learning_max_change_per_batch: str = "small"
    learning_explore_rate: float = 0.15
    min_closed_trades_for_weight_update: int = 5
    min_weight_before_200_setups: float = 0.70
    min_closed_trades_for_strategy_disable: int = 20
    mtfa_enabled: bool = True
    mtfa_mode: str = "TAG_ONLY"
    mtfa_require_h1_bias: bool = True
    mtfa_require_m15_liquidity: bool = True
    mtfa_require_m5_cisd: bool = True
    journal_layer_enabled: bool = True
    confluence_tagging_enabled: bool = True
    performance_summary_every_setups: int = 25
    mtf_structure_enabled: bool = True
    mtf_structure_mode: str = "TAG_ONLY"
    safety_guard_enabled: bool = True
    btc_weekend_analysis_only: bool = True
    crypto_24_7_enabled: bool = True
    btc_bad_hours_local: str = "21,23,2,4"
    btc_caution_hours_local: str = "18,19,20"
    bad_hour_analysis_only: bool = True
    ema_pullback_require_extra_confirmation: bool = True
    ema_pullback_block_if_mtfa_and_mtf_fail: bool = True
    ema_pullback_min_smc_score: float = 50.0
    ema_pullback_block_after_symbol_strategy_loss: bool = True
    risk_diag_max_realized_risk_percent: float = 0.75
    risk_diag_max_mismatch_abs_percent: float = 0.25
    second_entry_enabled: bool = False
    scalping_agent_enabled: bool = False
    second_entry_legacy_observer: bool = True
    scalping_agent_legacy_observer: bool = True
    btc_scalping_relaxed_demo_mode: bool = True
    btc_scalping_min_confidence: int = 55
    btc_scalping_allow_m5_momentum: bool = True
    btc_scalping_allow_m1_breakout: bool = True
    btc_scalping_max_trades_per_day: int = 6
    btc_scalping_cooldown_minutes: int = 20
    crt_tbs_reversal_enabled: bool = True
    amd_fvg_ifvg_reversal_enabled: bool = True
    fib_ote_retest_enabled: bool = True
    hermes_quant_strategy_enabled: bool = True
    hermes_quant_strategy_role: str = "ENTRY_STRATEGY"
    hermes_quant_reg_period: int = 50
    hermes_quant_min_r2: float = 0.30
    hermes_quant_z_period: int = 20
    hermes_quant_z_entry: float = 1.0
    hermes_quant_stdev_period: int = 20
    hermes_quant_sl_stdev_mult: float = 2.0
    hermes_quant_min_score: int = 75
    hermes_quant_min_rr: float = 2.0
    btc_disable_quant_statistical_pullback: bool = False
    hermes_quant_pro_enabled: bool = True
    hermes_quant_pro_role: str = "ENTRY_STRATEGY"
    hermes_quant_pro_reg_period: int = 50
    hermes_quant_pro_tcrit: float = 2.0
    hermes_quant_pro_kalman_period: int = 100
    hermes_quant_pro_kal_q_level: float = 0.001
    hermes_quant_pro_kal_q_vel: float = 0.00001
    hermes_quant_pro_kal_r: float = 1.0
    hermes_quant_pro_ou_period: int = 100
    hermes_quant_pro_hl_min: float = 2.0
    hermes_quant_pro_hl_max: float = 60.0
    hermes_quant_pro_hurst_period: int = 128
    hermes_quant_pro_hurst_trend: float = 0.50
    quant_pro_hurst_filter_enabled: bool = True
    quant_pro_min_trend_hurst: float = 0.90
    hermes_quant_pro_z_entry: float = 1.0
    hermes_quant_pro_ewma_vol_period: int = 50
    hermes_quant_pro_ewma_lambda: float = 0.94
    hermes_quant_pro_sl_vol_mult: float = 2.0
    hermes_quant_pro_min_score: int = 75
    hermes_quant_pro_min_rr: float = 2.0
    new_strategies_paper_only: bool = True
    new_strategies_min_score: int = 75
    new_strategies_require_safety_pass: bool = True
    new_strategies_require_risk_ok: bool = True
    new_strategies_require_rr_min: float = 1.5
    report_timezone: str = "Africa/Casablanca"
    timezone_local: str = "Africa/Casablanca"
    btc_bad_hours: str = "0,1,2,3,4,5,6,7,22,23"
    fx_bad_hours: str = "0,1,22,23"
    gold_bad_hours: str = "0,1,22,23"
    rollover_block_minutes: int = 30
    report_clean_start_at: str = ""
    report_excluded_trade_ids: str = ""
    asia_session_start: str = "00:00"
    asia_session_end: str = "06:00"
    london_session_start: str = "07:00"
    london_session_end: str = "11:00"
    new_york_session_start: str = "13:30"
    new_york_session_end: str = "17:00"
    overlap_session_start: str = "13:30"
    overlap_session_end: str = "16:00"
    btc_weekend_sandbox_enabled: bool = False
    btc_weekend_sandbox_symbol: str = "BTCUSD#"
    btc_weekend_sandbox_output: str = "backtests/btc_weekend_sandbox"
    btc_weekend_sandbox_risk_percent: float = 0.25
    btc_weekend_sandbox_max_open_trades: int = 1
    btc_weekend_sandbox_require_smc_pass: bool = True
    btc_weekend_sandbox_min_smc_score: float = 70.0
    btc_weekend_sandbox_require_big_setup_grade: str = "A"
    btc_weekend_sandbox_min_rr: float = 1.5
    btc_weekend_sandbox_disable_ema_pullback_entry: bool = True
    btc_weekend_sandbox_ema_confirmation_only: bool = True
    btc_weekend_sandbox_strategies: str = "BREAKOUT_RETEST,CRT_TBS_REVERSAL,AMD_FVG_IFVG_REVERSAL,FIB_OTE_RETEST"
    btc_weekend_sandbox_poll_seconds: int = 30

    strategy_manager_enabled: bool = True
    simo_atm_breakout_enabled: bool = True
    simo_atm_breakout_mode: str = "ACTIVE_EXECUTION"
    simo_atm_breakout_symbols: str = "US100,NAS100,USTEC,US100Cash#,NASDAQ"
    simo_atm_timeframe: str = "M5"
    simo_atm_lookback: int = 6
    simo_atm_atr_period: int = 14
    simo_atm_impulse_atr_mult: float = 1.6
    simo_atm_close_zone: float = 0.70
    simo_atm_entry_buffer_points: int = 20
    simo_atm_swing_bars: int = 10
    simo_atm_sl_buffer_points: int = 30
    simo_atm_rr: float = 2.0
    simo_atm_max_spread_points: int = 120
    simo_atm_session_filter: bool = True
    simo_atm_session_start_hour: int = 14
    simo_atm_session_end_hour: int = 20
    simo_atm_pending_expiry_minutes: int = 30
    simo_atm_max_trades_per_day: int = 5
    simo_atm_max_daily_loss_percent: float = 2.0

    hermes_main_symbols: str = "BTCUSD#,GOLD#,EURUSD,US100Cash#"
    allow_time_block_override: bool = False
    demo_router_events_max_lines: int = 5000
    demo_router_events_max_bytes: int = 10485760
    research_allow_low_confluence: bool = False
    hermes_execution_profile: str = ""
    old_btc_scalping_rr: float = 2.0
    old_btc_order_flow_rr: float = 1.5
    old_btc_smart_quick_exit_enabled: bool = True
    old_btc_rescue_min_profit_usd: float = 0.08
    old_btc_rescue_arm_drawdown_usd: float = -0.20
    old_btc_emergency_any_positive_exit_when_open_count_gt: int = 1
    old_btc_emergency_any_positive_exit_usd: float = 0.08
    old_btc_rescue_max_hold_seconds: int = 180
    old_btc_protect_existing_positions: bool = True
    old_btc_max_open_positions: int = 1
    # General smart exit (applies to both BTC strategies)
    old_btc_smart_exit_enabled: bool = True
    old_btc_positive_exit_min_usd: float = 0.08
    old_btc_danger_exit_min_usd: float = 0.08
    old_btc_emergency_open_count: int = 1
    old_btc_smart_exit_min_danger_signals: int = 2
    # BTC entry gate (shared by BTC_SCALPING_AGENT + ORDER_FLOW_EXECUTION_AGENT)
    old_btc_entry_gate_enabled: bool = True
    old_btc_entry_gate_scalping_min_confidence: int = 55
    old_btc_entry_gate_order_flow_min_score: int = 75
    old_btc_entry_gate_require_market_confirmation: bool = True
    # Fast Smart Exit daemon — tick-fast exit independent of analysis cycle
    old_btc_fast_exit_daemon_enabled: bool = True
    old_btc_fast_exit_interval_ms: int = 250
    old_btc_fast_exit_min_profit_usd: float = 0.03
    old_btc_fast_exit_hard_min_profit_usd: float = 0.01
    old_btc_fast_exit_close_at_any_positive: bool = True

    @property
    def hermes_main_symbol_list(self) -> List[str]:
        return [s.strip() for s in (self.hermes_main_symbols or "").split(",") if s.strip()]

    @property
    def simo_atm_symbol_list(self) -> List[str]:
        return [s.strip() for s in (self.simo_atm_breakout_symbols or "").split(",") if s.strip()]

    @property
    def symbol_list(self) -> List[str]:
        return [item.strip().upper() for item in self.symbols.split(",") if item.strip()]

    @property
    def trade_symbol_list(self) -> List[str]:
        return [item.strip().upper() for item in self.hermes_trade_symbols.split(",") if item.strip()]

    @property
    def analysis_only_symbol_list(self) -> List[str]:
        return [item.strip().upper() for item in self.hermes_analysis_only_symbols.split(",") if item.strip()]

    @property
    def gold_liquidity_trade_enabled(self) -> bool:
        return str(self.gold_liquidity_mode or "").strip().lower() in {"1", "true", "yes", "y", "on", "trade", "enabled"}

    @property
    def gold_allowed_signal_list(self) -> List[str]:
        return [item.strip().upper() for item in self.gold_allowed_signals.split(",") if item.strip()]

    @property
    def gold_observer_signal_list(self) -> List[str]:
        return [item.strip().upper() for item in self.gold_observer_signals.split(",") if item.strip()]

    def max_spread_for_symbol(self, symbol: str) -> float:
        normalized = (symbol or "").upper()
        if normalized.startswith("BTCUSD") and self.max_spread_btcusd is not None:
            return self.max_spread_btcusd
        if (normalized.startswith("XAUUSD") or normalized.startswith("GOLD")) and self.max_spread_gold is not None:
            return self.max_spread_gold
        if normalized.startswith("EURUSD") and self.max_spread_eurusd is not None:
            return self.max_spread_eurusd
        return self.max_spread

    def paper_max_lot_for_symbol(self, symbol: str) -> float:
        normalized = (symbol or "").upper()
        if normalized.startswith("BTCUSD") and self.paper_max_lot_btcusd is not None:
            return self.paper_max_lot_btcusd
        if (normalized.startswith("XAUUSD") or normalized.startswith("GOLD")) and self.paper_max_lot_gold is not None:
            return self.paper_max_lot_gold
        if normalized.startswith("EURUSD") and self.paper_max_lot_eurusd is not None:
            return self.paper_max_lot_eurusd
        return self.paper_max_lot

    def int_set(self, value: str) -> set[int]:
        out = set()
        for item in str(value or "").split(","):
            item = item.strip()
            if not item:
                continue
            try:
                out.add(int(item))
            except ValueError:
                continue
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings(
        hermes_ingest_url=os.getenv("HERMES_INGEST_URL", ""),
        hermes_ingest_secret=os.getenv("HERMES_INGEST_SECRET", ""),
        lovable_ingest_timeout_seconds=float(os.getenv("LOVABLE_INGEST_TIMEOUT_SECONDS", "2")),
        lovable_ingest_fail_soft=_bool_env("LOVABLE_INGEST_FAIL_SOFT", True),
        lovable_ingest_circuit_breaker_enabled=_bool_env("LOVABLE_INGEST_CIRCUIT_BREAKER_ENABLED", True),
        lovable_ingest_circuit_breaker_seconds=float(os.getenv("LOVABLE_INGEST_CIRCUIT_BREAKER_SECONDS", "300")),
        read_only=_bool_env("READ_ONLY", True),
        paper_trading=_bool_env("PAPER_TRADING", False),
        demo_trading=_bool_env("DEMO_TRADING", False),
        allow_live_trading=_bool_env("ALLOW_LIVE_TRADING", False),
        demo_only=_bool_env("DEMO_ONLY", True),
        demo_pilot_enabled=_bool_env("DEMO_PILOT_ENABLED", False),
        demo_pilot_hours=int(os.getenv("DEMO_PILOT_HOURS", "24")),
        demo_magic_number=int(os.getenv("DEMO_MAGIC_NUMBER", "909002")),
        demo_comment=os.getenv("DEMO_COMMENT", "HERMES_DEMO_KELLY_24H"),
        demo_max_lot=float(os.getenv("DEMO_MAX_LOT", "0.01")),
        demo_max_open_trades=int(os.getenv("DEMO_MAX_OPEN_TRADES", "1")),
        demo_max_trades_per_day=int(os.getenv("DEMO_MAX_TRADES_PER_DAY", "5")),
        demo_max_trades_per_day_total=_int_env("DEMO_MAX_TRADES_PER_DAY_TOTAL", 15, "DEMO_MAX_TRADES_PER_DAY"),
        demo_max_trades_per_symbol_per_day=_int_env("DEMO_MAX_TRADES_PER_SYMBOL_PER_DAY", 5, "DEMO_MAX_TRADES_PER_DAY"),
        demo_max_open_trades_total=_int_env("DEMO_MAX_OPEN_TRADES_TOTAL", 3, "DEMO_MAX_OPEN_TRADES"),
        demo_max_open_trades_per_symbol=_int_env("DEMO_MAX_OPEN_TRADES_PER_SYMBOL", 1, "DEMO_MAX_OPEN_TRADES"),
        demo_max_open_trades_per_symbol_strategy=int(os.getenv("DEMO_MAX_OPEN_TRADES_PER_SYMBOL_STRATEGY", "1")),
        demo_max_daily_loss_pct=float(os.getenv("DEMO_MAX_DAILY_LOSS_PCT", "1.0")),
        demo_max_risk_per_trade_pct=float(os.getenv("DEMO_MAX_RISK_PER_TRADE_PCT", "0.25")),
        demo_stop_after_consecutive_losses=int(os.getenv("DEMO_STOP_AFTER_CONSECUTIVE_LOSSES", "3")),
        quick_exit_enabled=_bool_env("QUICK_EXIT_ENABLED", True),
        quick_exit_demo_only=_bool_env("QUICK_EXIT_DEMO_ONLY", True),
        quick_exit_magic_number=int(os.getenv("QUICK_EXIT_MAGIC_NUMBER", "909002")),
        quick_exit_tp_usd=float(os.getenv("QUICK_EXIT_TP_USD", "1.50")),
        quick_exit_lock_usd=float(os.getenv("QUICK_EXIT_LOCK_USD", "0.80")),
        quick_exit_be_buffer_usd=float(os.getenv("QUICK_EXIT_BE_BUFFER_USD", "0.10")),
        quick_exit_trail_start_usd=float(os.getenv("QUICK_EXIT_TRAIL_START_USD", "1.00")),
        quick_exit_trail_gap_usd=float(os.getenv("QUICK_EXIT_TRAIL_GAP_USD", "0.60")),
        demo_allow_btc_weekend_bad_hour=_bool_env("DEMO_ALLOW_BTC_WEEKEND_BAD_HOUR", False),
        demo_allow_contest=_bool_env("DEMO_ALLOW_CONTEST", False),
        demo_allowed_login=os.getenv("DEMO_ALLOWED_LOGIN", "").strip(),
        demo_pilot_started_at=os.getenv("DEMO_PILOT_STARTED_AT", ""),
        demo_exploration_mode=_bool_env("DEMO_EXPLORATION_MODE", True),
        demo_exploration_max_lot=float(os.getenv("DEMO_EXPLORATION_MAX_LOT", "0.01")),
        demo_exploration_min_rr=float(os.getenv("DEMO_EXPLORATION_MIN_RR", "2.0")),
        demo_exploration_min_edge_score=float(os.getenv("DEMO_EXPLORATION_MIN_EDGE_SCORE", "90")),
        demo_exploration_allow_smc_fail=_bool_env("DEMO_EXPLORATION_ALLOW_SMC_FAIL", True),
        demo_exploration_allow_mtfa_fail=_bool_env("DEMO_EXPLORATION_ALLOW_MTFA_FAIL", True),
        demo_exploration_max_trades_per_day=int(os.getenv("DEMO_EXPLORATION_MAX_TRADES_PER_DAY", "3")),
        demo_exploration_max_trades_per_day_total=_int_env("DEMO_EXPLORATION_MAX_TRADES_PER_DAY_TOTAL", 15, "DEMO_EXPLORATION_MAX_TRADES_PER_DAY"),
        demo_exploration_max_trades_per_symbol_per_day=_int_env("DEMO_EXPLORATION_MAX_TRADES_PER_SYMBOL_PER_DAY", 5, "DEMO_EXPLORATION_MAX_TRADES_PER_DAY"),
        demo_exploration_ignore_bad_hour=_bool_env("DEMO_EXPLORATION_IGNORE_BAD_HOUR", False),
        demo_test_ignore_bad_hours=_bool_env("DEMO_TEST_IGNORE_BAD_HOURS", False),
        demo_ignore_all_time_blocks=_bool_env("DEMO_IGNORE_ALL_TIME_BLOCKS", True),
        demo_ignore_session_blocks=_bool_env("DEMO_IGNORE_SESSION_BLOCKS", True),
        demo_ignore_bad_hour_blocks=_bool_env("DEMO_IGNORE_BAD_HOUR_BLOCKS", True),
        demo_ignore_duration_blocks=_bool_env("DEMO_IGNORE_DURATION_BLOCKS", True),
        demo_ignore_setup_wait_hours=_bool_env("DEMO_IGNORE_SETUP_WAIT_HOURS", True),
        max_money_tp_enabled=_bool_env("MAX_MONEY_TP_ENABLED", True),
        max_tp_usd=float(os.getenv("MAX_TP_USD", "2.00")),
        max_tp_applies_to=os.getenv("MAX_TP_APPLIES_TO", "BTCUSD#,BTCUSD,GOLD#,GOLD,XAUUSD,EURUSD"),
        demo_strong_setup_learning_mode=_bool_env("DEMO_STRONG_SETUP_LEARNING_MODE", False),
        demo_strong_setup_min_edge=float(os.getenv("DEMO_STRONG_SETUP_MIN_EDGE", "95")),
        demo_strong_setup_min_rr=float(os.getenv("DEMO_STRONG_SETUP_MIN_RR", "2.0")),
        demo_strong_setup_max_trades_per_day=int(os.getenv("DEMO_STRONG_SETUP_MAX_TRADES_PER_DAY", "3")),
        demo_strong_setup_max_open_trades=int(os.getenv("DEMO_STRONG_SETUP_MAX_OPEN_TRADES", "1")),
        demo_strong_setup_max_trades_per_day_total=_int_env("DEMO_STRONG_SETUP_MAX_TRADES_PER_DAY_TOTAL", 15, "DEMO_STRONG_SETUP_MAX_TRADES_PER_DAY"),
        demo_strong_setup_max_trades_per_symbol_per_day=_int_env("DEMO_STRONG_SETUP_MAX_TRADES_PER_SYMBOL_PER_DAY", 5, "DEMO_STRONG_SETUP_MAX_TRADES_PER_DAY"),
        demo_smoke_test_24h=_bool_env("DEMO_SMOKE_TEST_24H", False),
        demo_smoke_test_max_confirmed_orders=int(os.getenv("DEMO_SMOKE_TEST_MAX_CONFIRMED_ORDERS", "1")),
        demo_smoke_test_end_after_hours=int(os.getenv("DEMO_SMOKE_TEST_END_AFTER_HOURS", "24")),
        hermes_free_demo_discovery_mode=_bool_env("HERMES_FREE_DEMO_DISCOVERY_MODE", False),
        hermes_demo_topdown_fallback_mode=_bool_env("HERMES_DEMO_TOPDOWN_FALLBACK_MODE", False),
        hermes_demo_micro_discovery_mode=_bool_env("HERMES_DEMO_MICRO_DISCOVERY_MODE", False),
        hermes_adaptive_confluence_enabled=_bool_env("HERMES_ADAPTIVE_CONFLUENCE_ENABLED", True),
        hermes_default_min_confluence=float(os.getenv("HERMES_DEFAULT_MIN_CONFLUENCE", "60")),
        hermes_hard_avoid_confluence=float(os.getenv("HERMES_HARD_AVOID_CONFLUENCE", "45")),
        btcusd_min_confluence=float(os.getenv("BTCUSD_MIN_CONFLUENCE", "65")),
        btcusd_topdown_min=float(os.getenv("BTCUSD_TOPDOWN_MIN", "65")),
        btcusd_m15_required=_bool_env("BTCUSD_M15_REQUIRED", True),
        btcusd_m1_required=_bool_env("BTCUSD_M1_REQUIRED", True),
        gold_min_confluence=float(os.getenv("GOLD_MIN_CONFLUENCE", "55")),
        gold_topdown_min=float(os.getenv("GOLD_TOPDOWN_MIN", "55")),
        gold_m15_required=_bool_env("GOLD_M15_REQUIRED", True),
        gold_m1_required=_bool_env("GOLD_M1_REQUIRED", False),
        eurusd_min_confluence=float(os.getenv("EURUSD_MIN_CONFLUENCE", "60")),
        eurusd_topdown_min=float(os.getenv("EURUSD_TOPDOWN_MIN", "60")),
        eurusd_m15_required=_bool_env("EURUSD_M15_REQUIRED", True),
        eurusd_m1_required=_bool_env("EURUSD_M1_REQUIRED", True),
        hermes_trade_symbols=os.getenv("HERMES_TRADE_SYMBOLS", "BTCUSD#,BTCUSD,GOLD#,GOLD,XAUUSD,EURUSD"),
        hermes_analysis_only_symbols=os.getenv("HERMES_ANALYSIS_ONLY_SYMBOLS", ""),
        gold_liquidity_mode=os.getenv("GOLD_LIQUIDITY_MODE", "false").strip(),
        gold_liquidity_strategy_enabled=_bool_env("GOLD_LIQUIDITY_STRATEGY_ENABLED", False),
        gold_disable_generic_strategies=_bool_env("GOLD_DISABLE_GENERIC_STRATEGIES", True),
        gold_pivot_length=int(os.getenv("GOLD_PIVOT_LENGTH", "15")),
        gold_atr_zone_thickness=float(os.getenv("GOLD_ATR_ZONE_THICKNESS", "0.5")),
        gold_zone_capacity=float(os.getenv("GOLD_ZONE_CAPACITY", "5.0")),
        gold_max_zones_per_side=int(os.getenv("GOLD_MAX_ZONES_PER_SIDE", "10")),
        gold_min_zone_stars=int(os.getenv("GOLD_MIN_ZONE_STARS", "3")),
        gold_min_liquidity_score=int(os.getenv("GOLD_MIN_LIQUIDITY_SCORE", "75")),
        gold_min_rr=float(os.getenv("GOLD_MIN_RR", "2.0")),
        gold_max_open_trades=int(os.getenv("GOLD_MAX_OPEN_TRADES", "1")),
        gold_allowed_signals=os.getenv("GOLD_ALLOWED_SIGNALS", "ABS,REJ"),
        gold_observer_signals=os.getenv("GOLD_OBSERVER_SIGNALS", "EXH,DIV"),
        gold_m1m5_relaxed_demo_mode=_bool_env("GOLD_M1M5_RELAXED_DEMO_MODE", True),
        gold_m1m5_relaxed_after_hours_no_setup=int(os.getenv("GOLD_M1M5_RELAXED_AFTER_HOURS_NO_SETUP", "24")),
        gold_m1m5_min_score_strict=int(os.getenv("GOLD_M1M5_MIN_SCORE_STRICT", "75")),
        gold_m1m5_min_score_relaxed=int(os.getenv("GOLD_M1M5_MIN_SCORE_RELAXED", "65")),
        gold_m1m5_atr_low_relax_factor=float(os.getenv("GOLD_M1M5_ATR_LOW_RELAX_FACTOR", "0.80")),
        gold_liquidity_relaxed_zone_stars=int(os.getenv("GOLD_LIQUIDITY_RELAXED_ZONE_STARS", "2")),
        order_flow_reader_enabled=_bool_env("ORDER_FLOW_READER_ENABLED", True),
        gold_order_flow_execution_enabled=_bool_env("ORDER_FLOW_ENTRY_ENABLED", False),
        gold_order_flow_min_confidence=int(os.getenv("GOLD_ORDER_FLOW_MIN_CONFIDENCE", "70")),
        gold_order_flow_require_divergence=_bool_env("GOLD_ORDER_FLOW_REQUIRE_DIVERGENCE", True),
        strict_gold_order_flow_topdown=_bool_env("STRICT_GOLD_ORDER_FLOW_TOPDOWN", False),
        eur_ema_rsi_atr_enabled=_bool_env("EUR_EMA_RSI_ATR_ENABLED", True),
        eur_fast_ema=int(os.getenv("FAST_EMA", os.getenv("EUR_FAST_EMA", "20"))),
        eur_slow_ema=int(os.getenv("SLOW_EMA", os.getenv("EUR_SLOW_EMA", "50"))),
        eur_rsi_period=int(os.getenv("RSI_PERIOD", os.getenv("EUR_RSI_PERIOD", "14"))),
        eur_rsi_buy_max=float(os.getenv("RSI_BUY_MAX", os.getenv("EUR_RSI_BUY_MAX", "70"))),
        eur_rsi_sell_min=float(os.getenv("RSI_SELL_MIN", os.getenv("EUR_RSI_SELL_MIN", "30"))),
        eur_atr_period=int(os.getenv("ATR_PERIOD", os.getenv("EUR_ATR_PERIOD", "14"))),
        eur_atr_mult=float(os.getenv("ATR_MULT", os.getenv("EUR_ATR_MULT", "1.5"))),
        eur_rr=float(os.getenv("RR", os.getenv("EUR_RR", "2.0"))),
        eur_ema_rsi_atr_relaxed_demo_mode=_bool_env("EUR_EMA_RSI_ATR_RELAXED_DEMO_MODE", True),
        eur_relaxed_after_hours_no_setup=int(os.getenv("EUR_RELAXED_AFTER_HOURS_NO_SETUP", "24")),
        eur_allow_near_cross=_bool_env("EUR_ALLOW_NEAR_CROSS", True),
        eur_near_cross_max_distance_atr=float(os.getenv("EUR_NEAR_CROSS_MAX_DISTANCE_ATR", "0.15")),
        max_open_eur_trades=int(os.getenv("MAX_OPEN_EUR_TRADES", "1")),
        demo_allow_asia_trading=_bool_env("DEMO_ALLOW_ASIA_TRADING", False),
        hermes_magic_number=int(os.getenv("HERMES_MAGIC_NUMBER", "909001")),
        symbols=os.getenv("SYMBOLS", "BTCUSD,XAUUSD,EURUSD"),
        main_timeframe=os.getenv("MAIN_TIMEFRAME", "M5"),
        poll_seconds=int(os.getenv("POLL_SECONDS", "5")),
        max_risk_per_trade=float(os.getenv("MAX_RISK_PER_TRADE", "0.5")),
        max_daily_loss=float(os.getenv("MAX_DAILY_LOSS", "3.0")),
        max_drawdown=float(os.getenv("MAX_DRAWDOWN", "10.0")),
        max_open_hermes_trades=int(os.getenv("MAX_OPEN_HERMES_TRADES", "1")),
        max_spread=float(os.getenv("MAX_SPREAD", "30")),
        max_spread_btcusd=_optional_float_env("MAX_SPREAD_BTCUSD"),
        max_spread_gold=_optional_float_env("MAX_SPREAD_GOLD"),
        max_spread_eurusd=_optional_float_env("MAX_SPREAD_EURUSD"),
        paper_max_lot=float(os.getenv("PAPER_MAX_LOT", "0.30")),
        paper_max_lot_btcusd=_optional_float_env("PAPER_MAX_LOT_BTCUSD"),
        paper_max_lot_gold=_optional_float_env("PAPER_MAX_LOT_GOLD"),
        paper_max_lot_eurusd=_optional_float_env("PAPER_MAX_LOT_EURUSD"),
        paper_max_hold_minutes=int(os.getenv("PAPER_MAX_HOLD_MINUTES", "30")),
        learning_mode=_bool_env("LEARNING_MODE", True),
        auto_apply_learning=_bool_env("AUTO_APPLY_LEARNING", True),
        learning_min_setups=int(os.getenv("LEARNING_MIN_SETUPS", "200")),
        learning_batch_size=int(os.getenv("LEARNING_BATCH_SIZE", "25")),
        learning_max_change_per_batch=os.getenv("LEARNING_MAX_CHANGE_PER_BATCH", "small"),
        learning_explore_rate=float(os.getenv("LEARNING_EXPLORE_RATE", "0.15")),
        min_closed_trades_for_weight_update=int(os.getenv("MIN_CLOSED_TRADES_FOR_WEIGHT_UPDATE", "5")),
        min_weight_before_200_setups=float(os.getenv("MIN_WEIGHT_BEFORE_200_SETUPS", "0.70")),
        min_closed_trades_for_strategy_disable=int(os.getenv("MIN_CLOSED_TRADES_FOR_STRATEGY_DISABLE", "20")),
        mtfa_enabled=_bool_env("MTFA_ENABLED", True),
        mtfa_mode=os.getenv("MTFA_MODE", "TAG_ONLY").strip().upper(),
        mtfa_require_h1_bias=_bool_env("MTFA_REQUIRE_H1_BIAS", True),
        mtfa_require_m15_liquidity=_bool_env("MTFA_REQUIRE_M15_LIQUIDITY", True),
        mtfa_require_m5_cisd=_bool_env("MTFA_REQUIRE_M5_CISD", True),
        journal_layer_enabled=_bool_env("JOURNAL_LAYER_ENABLED", True),
        confluence_tagging_enabled=_bool_env("CONFLUENCE_TAGGING_ENABLED", True),
        performance_summary_every_setups=int(os.getenv("PERFORMANCE_SUMMARY_EVERY_SETUPS", "25")),
        mtf_structure_enabled=_bool_env("MTF_STRUCTURE_ENABLED", True),
        mtf_structure_mode=os.getenv("MTF_STRUCTURE_MODE", "TAG_ONLY").strip().upper(),
        safety_guard_enabled=_bool_env("SAFETY_GUARD_ENABLED", True),
        btc_weekend_analysis_only=_bool_env("BTC_WEEKEND_ANALYSIS_ONLY", True),
        btc_bad_hours_local=os.getenv("BTC_BAD_HOURS_LOCAL", os.getenv("BTC_BAD_HOURS", "0,1,2,3,4,5,6,7,22,23")),
        btc_caution_hours_local=os.getenv("BTC_CAUTION_HOURS_LOCAL", "18,19,20"),
        bad_hour_analysis_only=_bool_env("BAD_HOUR_ANALYSIS_ONLY", True),
        ema_pullback_require_extra_confirmation=_bool_env("EMA_PULLBACK_REQUIRE_EXTRA_CONFIRMATION", True),
        ema_pullback_block_if_mtfa_and_mtf_fail=_bool_env("EMA_PULLBACK_BLOCK_IF_MTFA_AND_MTF_FAIL", True),
        ema_pullback_min_smc_score=float(os.getenv("EMA_PULLBACK_MIN_SMC_SCORE", "50")),
        ema_pullback_block_after_symbol_strategy_loss=_bool_env("EMA_PULLBACK_BLOCK_AFTER_SYMBOL_STRATEGY_LOSS", True),
        risk_diag_max_realized_risk_percent=float(os.getenv("RISK_DIAG_MAX_REALIZED_RISK_PERCENT", "0.75")),
        risk_diag_max_mismatch_abs_percent=float(os.getenv("RISK_DIAG_MAX_MISMATCH_ABS_PERCENT", "0.25")),
        second_entry_enabled=_bool_env("SECOND_ENTRY_ENABLED", False),
        scalping_agent_enabled=_bool_env("SCALPING_AGENT_ENABLED", False),
        second_entry_legacy_observer=_bool_env("SECOND_ENTRY_LEGACY_OBSERVER", True),
        scalping_agent_legacy_observer=_bool_env("SCALPING_AGENT_LEGACY_OBSERVER", True),
        btc_scalping_relaxed_demo_mode=_bool_env("BTC_SCALPING_RELAXED_DEMO_MODE", True),
        btc_scalping_min_confidence=int(os.getenv("BTC_SCALPING_MIN_CONFIDENCE", "55")),
        btc_scalping_allow_m5_momentum=_bool_env("BTC_SCALPING_ALLOW_M5_MOMENTUM", True),
        btc_scalping_allow_m1_breakout=_bool_env("BTC_SCALPING_ALLOW_M1_BREAKOUT", True),
        btc_scalping_max_trades_per_day=int(os.getenv("BTC_SCALPING_MAX_TRADES_PER_DAY", "6")),
        btc_scalping_cooldown_minutes=int(os.getenv("BTC_SCALPING_COOLDOWN_MINUTES", "20")),
        crt_tbs_reversal_enabled=_bool_env("CRT_TBS_REVERSAL_ENABLED", True),
        amd_fvg_ifvg_reversal_enabled=_bool_env("AMD_FVG_IFVG_REVERSAL_ENABLED", True),
        fib_ote_retest_enabled=_bool_env("FIB_OTE_RETEST_ENABLED", True),
        hermes_quant_strategy_enabled=_bool_env("HERMES_QUANT_STRATEGY_ENABLED", True),
        hermes_quant_strategy_role=os.getenv("HERMES_QUANT_STRATEGY_ROLE", "ENTRY_STRATEGY").strip().upper(),
        hermes_quant_reg_period=int(os.getenv("HERMES_QUANT_REG_PERIOD", "50")),
        hermes_quant_min_r2=float(os.getenv("HERMES_QUANT_MIN_R2", "0.30")),
        hermes_quant_z_period=int(os.getenv("HERMES_QUANT_Z_PERIOD", "20")),
        hermes_quant_z_entry=float(os.getenv("HERMES_QUANT_Z_ENTRY", "1.0")),
        hermes_quant_stdev_period=int(os.getenv("HERMES_QUANT_STDEV_PERIOD", "20")),
        hermes_quant_sl_stdev_mult=float(os.getenv("HERMES_QUANT_SL_STDEV_MULT", "2.0")),
        hermes_quant_min_score=int(os.getenv("HERMES_QUANT_MIN_SCORE", "75")),
        hermes_quant_min_rr=float(os.getenv("HERMES_QUANT_MIN_RR", "2.0")),
        btc_disable_quant_statistical_pullback=_bool_env("BTC_DISABLE_QUANT_STATISTICAL_PULLBACK", False),
        hermes_quant_pro_enabled=_bool_env("HERMES_QUANT_PRO_ENABLED", True),
        hermes_quant_pro_role=os.getenv("HERMES_QUANT_PRO_ROLE", "ENTRY_STRATEGY").strip().upper(),
        hermes_quant_pro_reg_period=int(os.getenv("HERMES_QUANT_PRO_REG_PERIOD", "50")),
        hermes_quant_pro_tcrit=float(os.getenv("HERMES_QUANT_PRO_TCRIT", "2.0")),
        hermes_quant_pro_kalman_period=int(os.getenv("HERMES_QUANT_PRO_KALMAN_PERIOD", "100")),
        hermes_quant_pro_kal_q_level=float(os.getenv("HERMES_QUANT_PRO_KAL_Q_LEVEL", "0.001")),
        hermes_quant_pro_kal_q_vel=float(os.getenv("HERMES_QUANT_PRO_KAL_Q_VEL", "0.00001")),
        hermes_quant_pro_kal_r=float(os.getenv("HERMES_QUANT_PRO_KAL_R", "1.0")),
        hermes_quant_pro_ou_period=int(os.getenv("HERMES_QUANT_PRO_OU_PERIOD", "100")),
        hermes_quant_pro_hl_min=float(os.getenv("HERMES_QUANT_PRO_HL_MIN", "2.0")),
        hermes_quant_pro_hl_max=float(os.getenv("HERMES_QUANT_PRO_HL_MAX", "60.0")),
        hermes_quant_pro_hurst_period=int(os.getenv("HERMES_QUANT_PRO_HURST_PERIOD", "128")),
        hermes_quant_pro_hurst_trend=float(os.getenv("HERMES_QUANT_PRO_HURST_TREND", "0.50")),
        quant_pro_hurst_filter_enabled=_bool_env("QUANT_PRO_HURST_FILTER_ENABLED", True),
        quant_pro_min_trend_hurst=float(os.getenv("QUANT_PRO_MIN_TREND_HURST", "0.90")),
        hermes_quant_pro_z_entry=float(os.getenv("HERMES_QUANT_PRO_Z_ENTRY", "1.0")),
        hermes_quant_pro_ewma_vol_period=int(os.getenv("HERMES_QUANT_PRO_EWMA_VOL_PERIOD", "50")),
        hermes_quant_pro_ewma_lambda=float(os.getenv("HERMES_QUANT_PRO_EWMA_LAMBDA", "0.94")),
        hermes_quant_pro_sl_vol_mult=float(os.getenv("HERMES_QUANT_PRO_SL_VOL_MULT", "2.0")),
        hermes_quant_pro_min_score=int(os.getenv("HERMES_QUANT_PRO_MIN_SCORE", "75")),
        hermes_quant_pro_min_rr=float(os.getenv("HERMES_QUANT_PRO_MIN_RR", "2.0")),
        new_strategies_paper_only=_bool_env("NEW_STRATEGIES_PAPER_ONLY", True),
        new_strategies_min_score=int(os.getenv("NEW_STRATEGIES_MIN_SCORE", "75")),
        new_strategies_require_safety_pass=_bool_env("NEW_STRATEGIES_REQUIRE_SAFETY_PASS", True),
        new_strategies_require_risk_ok=_bool_env("NEW_STRATEGIES_REQUIRE_RISK_OK", True),
        new_strategies_require_rr_min=float(os.getenv("NEW_STRATEGIES_REQUIRE_RR_MIN", "1.5")),
        report_timezone=os.getenv("REPORT_TIMEZONE", os.getenv("TIMEZONE_LOCAL", "Africa/Casablanca")),
        timezone_local=os.getenv("TIMEZONE_LOCAL", os.getenv("REPORT_TIMEZONE", "Africa/Casablanca")),
        btc_bad_hours=os.getenv("BTC_BAD_HOURS", "0,1,2,3,4,5,6,7,22,23"),
        fx_bad_hours=os.getenv("FX_BAD_HOURS", "0,1,22,23"),
        gold_bad_hours=os.getenv("GOLD_BAD_HOURS", "0,1,22,23"),
        rollover_block_minutes=int(os.getenv("ROLLOVER_BLOCK_MINUTES", "30")),
        report_clean_start_at=os.getenv("REPORT_CLEAN_START_AT", ""),
        report_excluded_trade_ids=os.getenv("REPORT_EXCLUDED_TRADE_IDS", ""),
        asia_session_start=os.getenv("ASIA_SESSION_START", "00:00"),
        asia_session_end=os.getenv("ASIA_SESSION_END", "06:00"),
        london_session_start=os.getenv("LONDON_SESSION_START", "07:00"),
        london_session_end=os.getenv("LONDON_SESSION_END", "11:00"),
        new_york_session_start=os.getenv("NEW_YORK_SESSION_START", "13:30"),
        new_york_session_end=os.getenv("NEW_YORK_SESSION_END", "17:00"),
        overlap_session_start=os.getenv("OVERLAP_SESSION_START", "13:30"),
        overlap_session_end=os.getenv("OVERLAP_SESSION_END", "16:00"),
        btc_weekend_sandbox_enabled=_bool_env("BTC_WEEKEND_SANDBOX_ENABLED", False),
        btc_weekend_sandbox_symbol=os.getenv("BTC_WEEKEND_SANDBOX_SYMBOL", "BTCUSD#"),
        btc_weekend_sandbox_output=os.getenv("BTC_WEEKEND_SANDBOX_OUTPUT", "backtests/btc_weekend_sandbox"),
        btc_weekend_sandbox_risk_percent=float(os.getenv("BTC_WEEKEND_SANDBOX_RISK_PERCENT", "0.25")),
        btc_weekend_sandbox_max_open_trades=int(os.getenv("BTC_WEEKEND_SANDBOX_MAX_OPEN_TRADES", "1")),
        btc_weekend_sandbox_require_smc_pass=_bool_env("BTC_WEEKEND_SANDBOX_REQUIRE_SMC_PASS", True),
        btc_weekend_sandbox_min_smc_score=float(os.getenv("BTC_WEEKEND_SANDBOX_MIN_SMC_SCORE", "70")),
        btc_weekend_sandbox_require_big_setup_grade=os.getenv("BTC_WEEKEND_SANDBOX_REQUIRE_BIG_SETUP_GRADE", "A"),
        btc_weekend_sandbox_min_rr=float(os.getenv("BTC_WEEKEND_SANDBOX_MIN_RR", "1.5")),
        btc_weekend_sandbox_disable_ema_pullback_entry=_bool_env("BTC_WEEKEND_SANDBOX_DISABLE_EMA_PULLBACK_ENTRY", True),
        btc_weekend_sandbox_ema_confirmation_only=_bool_env("BTC_WEEKEND_SANDBOX_EMA_CONFIRMATION_ONLY", True),
        btc_weekend_sandbox_strategies=os.getenv("BTC_WEEKEND_SANDBOX_STRATEGIES", "BREAKOUT_RETEST,CRT_TBS_REVERSAL,AMD_FVG_IFVG_REVERSAL,FIB_OTE_RETEST"),
        btc_weekend_sandbox_poll_seconds=int(os.getenv("BTC_WEEKEND_SANDBOX_POLL_SECONDS", "30")),
        order_flow_execution_enabled=_bool_env("ORDER_FLOW_EXECUTION_ENABLED", False),
        order_flow_min_score=_int_env("ORDER_FLOW_MIN_SCORE", 75),
        order_flow_min_rr=float(os.getenv("ORDER_FLOW_MIN_RR", "1.5")),
        order_flow_cooldown_minutes=_int_env("ORDER_FLOW_COOLDOWN_MINUTES", 15),
        order_flow_allowed_symbols=os.getenv("ORDER_FLOW_ALLOWED_SYMBOLS", "BTCUSD,BTCUSD#,GOLD,GOLD#,XAUUSD,EURUSD"),
        hermes_strategy_pack_enabled=_bool_env("HERMES_STRATEGY_PACK_ENABLED", True),
        hermes_entry_gates_enabled=_bool_env("HERMES_ENTRY_GATES_ENABLED", False),
        hermes_setup_tier_enabled=_bool_env("HERMES_SETUP_TIER_ENABLED", False),
        hermes_confluence_strategy_aware=_bool_env("HERMES_CONFLUENCE_STRATEGY_AWARE", False),
        of_native_key_level_tol_atr=float(os.getenv("OF_NATIVE_KEY_LEVEL_TOL_ATR", "0.25")),
        geometric_confluence_mode=os.getenv("GEOMETRIC_CONFLUENCE_MODE", "SHADOW").strip().upper(),
        geometric_confirm_bonus=float(os.getenv("GEOMETRIC_CONFIRM_BONUS", "5.0")),
        geometric_ratio_tolerance=float(os.getenv("GEOMETRIC_RATIO_TOLERANCE", "0.05")),
        fib_confluence_execution_enabled=_bool_env("FIB_CONFLUENCE_EXECUTION_ENABLED", False),
        fib_confluence_lookback=_int_env("FIB_CONFLUENCE_LOOKBACK", 30),
        fib_confluence_struct_window=_int_env("FIB_CONFLUENCE_STRUCT_WINDOW", 15),
        fib_confluence_vol_period=_int_env("FIB_CONFLUENCE_VOL_PERIOD", 20),
        fib_confluence_vol_mult=float(os.getenv("FIB_CONFLUENCE_VOL_MULT", "1.5")),
        fib_confluence_liq_lookback=_int_env("FIB_CONFLUENCE_LIQ_LOOKBACK", 10),
        fib_confluence_sl_atr_mult=float(os.getenv("FIB_CONFLUENCE_SL_ATR_MULT", "0.5")),
        fib_confluence_rr=float(os.getenv("FIB_CONFLUENCE_RR", "2.0")),
        fib_confluence_max_spread_atr_frac=float(os.getenv("FIB_CONFLUENCE_MAX_SPREAD_ATR_FRAC", "0.15")),
        fib_confluence_min_stop_distance_points=_int_env("FIB_CONFLUENCE_MIN_STOP_DISTANCE_POINTS", 0),
        strategy_manager_enabled=_bool_env("STRATEGY_MANAGER_ENABLED", True),
        simo_atm_breakout_enabled=_bool_env("SIMO_ATM_BREAKOUT_ENABLED", True),
        simo_atm_breakout_mode=os.getenv("SIMO_ATM_BREAKOUT_MODE", "ACTIVE_EXECUTION"),
        simo_atm_breakout_symbols=os.getenv("SIMO_ATM_BREAKOUT_SYMBOLS", "US100,NAS100,USTEC,US100Cash#,NASDAQ"),
        simo_atm_timeframe=os.getenv("SIMO_ATM_TIMEFRAME", "M5"),
        simo_atm_lookback=int(os.getenv("SIMO_ATM_LOOKBACK", "6")),
        simo_atm_atr_period=int(os.getenv("SIMO_ATM_ATR_PERIOD", "14")),
        simo_atm_impulse_atr_mult=float(os.getenv("SIMO_ATM_IMPULSE_ATR_MULT", "1.6")),
        simo_atm_close_zone=float(os.getenv("SIMO_ATM_CLOSE_ZONE", "0.70")),
        simo_atm_entry_buffer_points=int(os.getenv("SIMO_ATM_ENTRY_BUFFER_POINTS", "20")),
        simo_atm_swing_bars=int(os.getenv("SIMO_ATM_SWING_BARS", "10")),
        simo_atm_sl_buffer_points=int(os.getenv("SIMO_ATM_SL_BUFFER_POINTS", "30")),
        simo_atm_rr=float(os.getenv("SIMO_ATM_RR", "2.0")),
        simo_atm_max_spread_points=int(os.getenv("SIMO_ATM_MAX_SPREAD_POINTS", "120")),
        simo_atm_session_filter=_bool_env("SIMO_ATM_SESSION_FILTER", True),
        simo_atm_session_start_hour=int(os.getenv("SIMO_ATM_SESSION_START_HOUR", "14")),
        simo_atm_session_end_hour=int(os.getenv("SIMO_ATM_SESSION_END_HOUR", "20")),
        simo_atm_pending_expiry_minutes=int(os.getenv("SIMO_ATM_PENDING_EXPIRY_MINUTES", "30")),
        simo_atm_max_trades_per_day=int(os.getenv("SIMO_ATM_MAX_TRADES_PER_DAY", "5")),
        simo_atm_max_daily_loss_percent=float(os.getenv("SIMO_ATM_MAX_DAILY_LOSS_PERCENT", "2.0")),
        hermes_main_symbols=os.getenv("HERMES_MAIN_SYMBOLS", "BTCUSD#,GOLD#,EURUSD,US100Cash#"),
        allow_time_block_override=_bool_env("ALLOW_TIME_BLOCK_OVERRIDE", False),
        demo_router_events_max_lines=_int_env("DEMO_ROUTER_EVENTS_MAX_LINES", 5000),
        demo_router_events_max_bytes=_int_env("DEMO_ROUTER_EVENTS_MAX_BYTES", 10485760),
        research_allow_low_confluence=_bool_env("RESEARCH_ALLOW_LOW_CONFLUENCE", False),
        hermes_execution_profile=os.getenv("HERMES_EXECUTION_PROFILE", ""),
        old_btc_scalping_rr=float(os.getenv("OLD_BTC_SCALPING_RR", "2.0")),
        old_btc_order_flow_rr=float(os.getenv("OLD_BTC_ORDER_FLOW_RR", "1.5")),
        old_btc_smart_quick_exit_enabled=_bool_env("OLD_BTC_SMART_QUICK_EXIT_ENABLED", True),
        old_btc_rescue_min_profit_usd=float(os.getenv("OLD_BTC_RESCUE_MIN_PROFIT_USD", "0.08")),
        old_btc_rescue_arm_drawdown_usd=float(os.getenv("OLD_BTC_RESCUE_ARM_DRAWDOWN_USD", "-0.20")),
        old_btc_emergency_any_positive_exit_when_open_count_gt=int(os.getenv("OLD_BTC_EMERGENCY_ANY_POSITIVE_EXIT_WHEN_OPEN_COUNT_GT", "1")),
        old_btc_emergency_any_positive_exit_usd=float(os.getenv("OLD_BTC_EMERGENCY_ANY_POSITIVE_EXIT_USD", "0.08")),
        old_btc_rescue_max_hold_seconds=int(os.getenv("OLD_BTC_RESCUE_MAX_HOLD_SECONDS", "180")),
        old_btc_protect_existing_positions=_bool_env("OLD_BTC_PROTECT_EXISTING_POSITIONS", True),
        old_btc_max_open_positions=int(os.getenv("OLD_BTC_MAX_OPEN_POSITIONS", "1")),
        old_btc_smart_exit_enabled=_bool_env("OLD_BTC_SMART_EXIT_ENABLED", True),
        old_btc_positive_exit_min_usd=float(os.getenv("OLD_BTC_POSITIVE_EXIT_MIN_USD", "0.08")),
        old_btc_danger_exit_min_usd=float(os.getenv("OLD_BTC_DANGER_EXIT_MIN_USD", "0.08")),
        old_btc_emergency_open_count=int(os.getenv("OLD_BTC_EMERGENCY_OPEN_COUNT", "1")),
        old_btc_smart_exit_min_danger_signals=int(os.getenv("OLD_BTC_SMART_EXIT_MIN_DANGER_SIGNALS", "2")),
        old_btc_entry_gate_enabled=_bool_env("OLD_BTC_ENTRY_GATE_ENABLED", True),
        old_btc_entry_gate_scalping_min_confidence=int(os.getenv("OLD_BTC_ENTRY_GATE_SCALPING_MIN_CONFIDENCE", "55")),
        old_btc_entry_gate_order_flow_min_score=int(os.getenv("OLD_BTC_ENTRY_GATE_ORDER_FLOW_MIN_SCORE", "75")),
        old_btc_entry_gate_require_market_confirmation=_bool_env("OLD_BTC_ENTRY_GATE_REQUIRE_MARKET_CONFIRMATION", True),
        old_btc_fast_exit_daemon_enabled=_bool_env("OLD_BTC_FAST_EXIT_DAEMON_ENABLED", True),
        old_btc_fast_exit_interval_ms=int(os.getenv("OLD_BTC_FAST_EXIT_INTERVAL_MS", "250")),
        old_btc_fast_exit_min_profit_usd=float(os.getenv("OLD_BTC_FAST_EXIT_MIN_PROFIT_USD", "0.03")),
        old_btc_fast_exit_hard_min_profit_usd=float(os.getenv("OLD_BTC_FAST_EXIT_HARD_MIN_PROFIT_USD", "0.01")),
        old_btc_fast_exit_close_at_any_positive=_bool_env("OLD_BTC_FAST_EXIT_CLOSE_AT_ANY_POSITIVE", True),
    )
