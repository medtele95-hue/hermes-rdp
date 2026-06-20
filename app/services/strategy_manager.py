from __future__ import annotations

from datetime import datetime, timezone

from app.logger import log
from app.strategies.registry import StrategyClass

_DISCOVERY_CACHE_SECONDS = 300

# ---------------------------------------------------------------------------
# Three-way classification sets
# ---------------------------------------------------------------------------

_ACTIVE_EXECUTION: frozenset[str] = frozenset({
    "SIMO_ATM_BREAKOUT",
    "BTC_SCALPING_AGENT",
    "EUR_EMA_RSI_ATR_CROSSOVER",
    "FIB_CONFLUENCE_EXECUTION_AGENT",
    "GOLD_LIQUIDITY_HUNTER_PRO",
    "GOLD_M1_M5_EMA_SWEEP_SCALPER",
    "GOLD_ORDER_FLOW_CVD_VWAP",
    "ORDER_FLOW_EXECUTION_AGENT",
})

_CONFIRMATION_MODULE: frozenset[str] = frozenset({
    # Confluence / structure analysis modules
    "SMC_TAGGER",
    "MTFA",
    "MTF_STRUCTURE",
    "TOP_DOWN_MARKET_READER",
    "BIG_SETUP_DETECTOR",
    "CONFLUENCE_ENGINE",
    "GEOMETRY_ENGINE",
    "MARKOV_ENGINE",
    "KELLY_RISK_ENGINE",
    "TIME_ENGINE",
    # Observation / analysis strategies that never route to execution
    "EMA_PULLBACK",
    "BREAKOUT_RETEST",
    "TREND_CONTINUATION_BREAKDOWN",
    "CRT_TBS_REVERSAL",
    "AMD_FVG_IFVG_REVERSAL",
    "FIB_OTE_RETEST",
    "QUANT_STATISTICAL_PULLBACK",
    "QUANT_PRO_REGIME_SWITCHING",
    "SECOND_ENTRY",
    "SCALPING_AGENT",
})

_INTERNAL_DATA_FEED: frozenset[str] = frozenset({
    "ORDER_FLOW_READER",
})

# Backward-compatible alias used by tests and external code.
_OBSERVATION_ONLY: frozenset[str] = _CONFIRMATION_MODULE | _INTERNAL_DATA_FEED


class StrategyManager:
    """Tracks strategy classification and discovers SIMO_ATM_BREAKOUT index symbols."""

    def __init__(self, settings: object) -> None:
        self.settings = settings
        self._resolved_simo_symbol: str | None = None
        self._last_discovery_ts: datetime | None = None

    @property
    def simo_enabled(self) -> bool:
        return bool(getattr(self.settings, "strategy_manager_enabled", True)) and bool(
            getattr(self.settings, "simo_atm_breakout_enabled", True)
        )

    @property
    def _symbol_candidates(self) -> list[str]:
        return list(
            getattr(
                self.settings,
                "simo_atm_symbol_list",
                ["US100", "NAS100", "USTEC", "US100Cash#", "NASDAQ"],
            )
        )

    def discover_simo_symbol(self) -> str | None:
        """Try to resolve a tradeable broker index symbol from the SIMO candidate list."""
        try:
            import MetaTrader5 as mt5
        except ImportError:
            return None

        for sym in self._symbol_candidates:
            try:
                info = mt5.symbol_info(sym)
                if info is None:
                    log.info("[SYMBOL_DISCOVERY] skipped=%s reason=UNAVAILABLE_OR_NO_RATES", sym)
                    continue
                if not mt5.symbol_select(sym, True):
                    log.info("[SYMBOL_DISCOVERY] skipped=%s reason=UNAVAILABLE_OR_NO_RATES", sym)
                    continue
                tick = mt5.symbol_info_tick(sym)
                if tick is None or (
                    getattr(tick, "bid", None) is None and getattr(tick, "ask", None) is None
                ):
                    log.info("[SYMBOL_DISCOVERY] skipped=%s reason=UNAVAILABLE_OR_NO_RATES", sym)
                    continue
                log.info("[SYMBOL_DISCOVERY] requested=%s resolved=%s selected=true", sym, sym)
                return sym
            except Exception:
                log.info("[SYMBOL_DISCOVERY] skipped=%s reason=UNAVAILABLE_OR_NO_RATES", sym)
        return None

    def refresh_simo_symbol(self) -> str | None:
        """Return cached index symbol, re-running discovery if stale."""
        now = datetime.now(timezone.utc)
        needs_refresh = (
            self._resolved_simo_symbol is None
            or self._last_discovery_ts is None
            or (now - self._last_discovery_ts).total_seconds() >= _DISCOVERY_CACHE_SECONDS
        )
        if needs_refresh:
            self._resolved_simo_symbol = self.discover_simo_symbol()
            self._last_discovery_ts = now
        return self._resolved_simo_symbol

    def log_active_status(self) -> None:
        """Log SIMO_ATM_BREAKOUT active status (called each SIMO cycle)."""
        mode = getattr(self.settings, "simo_atm_breakout_mode", "ACTIVE_EXECUTION")
        log.info(
            "[STRATEGY_MANAGER] strategy=SIMO_ATM_BREAKOUT mode=%s route_allowed=true", mode
        )

    # ------------------------------------------------------------------
    # Startup classification log
    # ------------------------------------------------------------------

    def log_all_strategies(self) -> None:
        """Emit one [STRATEGY_CLASS] log line per strategy/module on startup."""
        for strategy in sorted(_ACTIVE_EXECUTION):
            enabled = self._strategy_enabled(strategy)
            route = enabled  # route_allowed mirrors enabled for flag-gated active strategies
            log.info(
                "[STRATEGY_CLASS] name=%s class=ACTIVE_EXECUTION_STRATEGY route_allowed=%s enabled=%s",
                strategy, str(route).lower(), str(enabled).lower(),
            )

        for strategy in sorted(_CONFIRMATION_MODULE):
            log.info(
                "[STRATEGY_CLASS] name=%s class=CONFIRMATION_MODULE route_allowed=false enabled=true",
                strategy,
            )

        for strategy in sorted(_INTERNAL_DATA_FEED):
            log.info(
                "[STRATEGY_CLASS] name=%s class=INTERNAL_DATA_FEED route_allowed=false",
                strategy,
            )

    # ------------------------------------------------------------------
    # Per-strategy helpers
    # ------------------------------------------------------------------

    def _strategy_enabled(self, strategy: str) -> bool:
        """Return whether a strategy is currently enabled via feature flags."""
        name = strategy.upper()
        if name == "GOLD_ORDER_FLOW_CVD_VWAP":
            return bool(getattr(self.settings, "gold_order_flow_execution_enabled", False))
        if name == "ORDER_FLOW_EXECUTION_AGENT":
            return bool(getattr(self.settings, "order_flow_execution_enabled", False))
        if name == "SIMO_ATM_BREAKOUT":
            return bool(getattr(self.settings, "simo_atm_breakout_enabled", True))
        return True

    # ------------------------------------------------------------------
    # Static helpers (backward-compatible)
    # ------------------------------------------------------------------

    @staticmethod
    def strategy_mode(strategy: str) -> str:
        """Return "ACTIVE_EXECUTION" or "OBSERVATION_ONLY" (two-way, backward-compat)."""
        name = str(strategy or "").upper()
        if name in _ACTIVE_EXECUTION:
            return "ACTIVE_EXECUTION"
        return "OBSERVATION_ONLY"

    @staticmethod
    def strategy_class(strategy: str) -> StrategyClass:
        """Return the three-way StrategyClass for a strategy name."""
        name = str(strategy or "").upper()
        if name in _ACTIVE_EXECUTION:
            return StrategyClass.ACTIVE_EXECUTION_STRATEGY
        if name in _INTERNAL_DATA_FEED:
            return StrategyClass.INTERNAL_DATA_FEED
        return StrategyClass.CONFIRMATION_MODULE

    @staticmethod
    def route_allowed(strategy: str) -> bool:
        """Return True only if strategy is in the active-execution set."""
        return str(strategy or "").upper() in _ACTIVE_EXECUTION
