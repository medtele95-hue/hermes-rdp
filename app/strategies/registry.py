from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class StrategyMode(str, Enum):
    ACTIVE_EXECUTION = "ACTIVE_EXECUTION"
    OBSERVATION = "OBSERVATION"


class StrategyClass(str, Enum):
    """Three-way classification used by StrategyManager and dashboard."""
    ACTIVE_EXECUTION_STRATEGY = "ACTIVE_EXECUTION_STRATEGY"
    CONFIRMATION_MODULE = "CONFIRMATION_MODULE"
    INTERNAL_DATA_FEED = "INTERNAL_DATA_FEED"


@dataclass(frozen=True)
class StrategySpec:
    name: str
    mode: StrategyMode
    symbols: frozenset[str] = frozenset({"BTCUSD", "GOLD", "EURUSD"})


ACTIVE_EXECUTION_STRATEGIES = frozenset(
    {
        "SIMO_ATM_BREAKOUT",
        "BTC_SCALPING_AGENT",
        "EUR_EMA_RSI_ATR_CROSSOVER",
        "FIB_CONFLUENCE_EXECUTION_AGENT",
        "GOLD_LIQUIDITY_HUNTER_PRO",
        "GOLD_M1_M5_EMA_SWEEP_SCALPER",
        "GOLD_ORDER_FLOW_CVD_VWAP",
        "GOLD_RANGE_BREAKOUT",
        "ORDER_FLOW_EXECUTION_AGENT",
        "HERMES_STRATEGY_PACK_AGENT",
        "HERMES_QUICK_EXIT_MANAGER",
    }
)

OBSERVATION_STRATEGIES = frozenset(
    {
        "BREAKOUT_RETEST",
        "TREND_CONTINUATION_BREAKDOWN",
        "CRT_TBS_REVERSAL",
        "AMD_FVG_IFVG_REVERSAL",
        "FIB_OTE_RETEST",
        "QUANT_STATISTICAL_PULLBACK",
        "QUANT_PRO_REGIME_SWITCHING",
        "ORDER_FLOW_READER",
        "EMA_PULLBACK",
        "SECOND_ENTRY",
        "SCALPING_AGENT",
    }
)

CONFIRMATION_STRATEGIES = frozenset({"EMA_PULLBACK"})
ALLOWED_GOLD_EXECUTION_STRATEGIES = frozenset(
    {
        "SIMO_ATM_BREAKOUT",
        "FIB_CONFLUENCE_EXECUTION_AGENT",
        "GOLD_LIQUIDITY_HUNTER_PRO",
        "GOLD_M1_M5_EMA_SWEEP_SCALPER",
        "GOLD_ORDER_FLOW_CVD_VWAP",
        "GOLD_RANGE_BREAKOUT",
        "ORDER_FLOW_EXECUTION_AGENT",
        "HERMES_STRATEGY_PACK_AGENT",
    }
)
ALLOWED_EUR_EXECUTION_STRATEGIES = frozenset({"SIMO_ATM_BREAKOUT", "EUR_EMA_RSI_ATR_CROSSOVER", "FIB_CONFLUENCE_EXECUTION_AGENT", "ORDER_FLOW_EXECUTION_AGENT", "HERMES_STRATEGY_PACK_AGENT"})
ALLOWED_BTC_EXECUTION_STRATEGIES = frozenset({"SIMO_ATM_BREAKOUT", "BTC_SCALPING_AGENT", "FIB_CONFLUENCE_EXECUTION_AGENT", "ORDER_FLOW_EXECUTION_AGENT", "HERMES_STRATEGY_PACK_AGENT"})
ALLOWED_US100_EXECUTION_STRATEGIES = frozenset({"SIMO_ATM_BREAKOUT", "FIB_CONFLUENCE_EXECUTION_AGENT", "ORDER_FLOW_EXECUTION_AGENT", "HERMES_STRATEGY_PACK_AGENT"})


def canonical_symbol(symbol: object) -> str:
    text = str(symbol or "").upper().strip()
    if text.startswith("BTCUSD"):
        return "BTCUSD"
    if text.startswith(("GOLD", "XAUUSD")):
        return "GOLD"
    if text.startswith("EURUSD"):
        return "EURUSD"
    if text.startswith(("US100", "NAS100", "USTEC")):
        return "US100"
    return text.replace("#", "")


def strategy_mode(strategy: object) -> StrategyMode:
    name = str(strategy or "").upper()
    if name in ACTIVE_EXECUTION_STRATEGIES:
        return StrategyMode.ACTIVE_EXECUTION
    return StrategyMode.OBSERVATION


def is_active_execution(strategy: object) -> bool:
    return strategy_mode(strategy) == StrategyMode.ACTIVE_EXECUTION


def is_observation(strategy: object) -> bool:
    return strategy_mode(strategy) == StrategyMode.OBSERVATION


def strategy_role(strategy: object) -> str:
    name = str(strategy or "").upper()
    if name in CONFIRMATION_STRATEGIES:
        return "CONFIRMATION"
    if is_active_execution(name):
        return "ENTRY"
    return "OBSERVER"


def allowed_for_symbol(strategy: object, *symbols: object) -> bool:
    name = str(strategy or "").upper()
    canonical = next((canonical_symbol(symbol) for symbol in symbols if symbol), "")
    if canonical == "GOLD":
        return name in ALLOWED_GOLD_EXECUTION_STRATEGIES
    if canonical == "EURUSD":
        return name in ALLOWED_EUR_EXECUTION_STRATEGIES
    if canonical == "BTCUSD":
        return name in ALLOWED_BTC_EXECUTION_STRATEGIES
    if canonical == "US100":
        return name in ALLOWED_US100_EXECUTION_STRATEGIES
    return is_active_execution(name)


def executable_strategies() -> set[str]:
    return set(ACTIVE_EXECUTION_STRATEGIES)


# ---------------------------------------------------------------------------
# Three-way classification sets
# ---------------------------------------------------------------------------

CONFIRMATION_MODULE_STRATEGIES: frozenset[str] = frozenset({
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

INTERNAL_DATA_FEED_STRATEGIES: frozenset[str] = frozenset({
    "ORDER_FLOW_READER",
})


def strategy_class(strategy: object) -> StrategyClass:
    """Return the three-way StrategyClass for a strategy name."""
    name = str(strategy or "").upper()
    if name in ACTIVE_EXECUTION_STRATEGIES:
        return StrategyClass.ACTIVE_EXECUTION_STRATEGY
    if name in INTERNAL_DATA_FEED_STRATEGIES:
        return StrategyClass.INTERNAL_DATA_FEED
    return StrategyClass.CONFIRMATION_MODULE
