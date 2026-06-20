"""LOVABLE_BTC_OLD_SYSTEM execution profile.

Restores the original BTC execution behaviour from the Lovable phase
(June 2026, 64 BTCUSD# trades with BTC_SCALPING_AGENT + ORDER_FLOW_EXECUTION_AGENT).

In this profile ONLY:
- Symbol cycle is restricted to BTCUSD#
- Active strategies: BTC_SCALPING_AGENT and ORDER_FLOW_EXECUTION_AGENT
- After SETUP_HUNTER_ACCEPT + SAFETY_GUARD PASS: route both strategies to DemoRouter
- FINAL_CONFLUENCE_TOO_LOW bypassed for ORDER_FLOW_EXECUTION_AGENT
- CONFIRMATION_MATRIX_HARD_BLOCK bypassed for BTC_SCALPING_AGENT

Safety guarantees remain fully enforced regardless of this profile:
- ALLOW_LIVE_TRADING=false
- DEMO_ONLY=true
- DEMO_MAX_LOT=0.01
- DEMO_MAGIC_NUMBER=909002
- order execution exclusively via app/mt5/demo_router.py
"""
from __future__ import annotations

PROFILE_NAME = "LOVABLE_BTC_OLD_SYSTEM"
OLD_BTC_SYMBOL = "BTCUSD#"
OLD_BTC_STRATEGIES: frozenset[str] = frozenset({"BTC_SCALPING_AGENT", "ORDER_FLOW_EXECUTION_AGENT"})

OLD_BTC_MODES: dict[str, str] = {
    "BTC_SCALPING_AGENT": "DEMO_ADAPTIVE_FALLBACK",
    "ORDER_FLOW_EXECUTION_AGENT": "DEMO_MICRO_DISCOVERY",
}

OLD_BTC_SCALPING_RR: float = 2.0
OLD_BTC_ORDER_FLOW_RR: float = 1.5


def is_active(settings: object) -> bool:
    """Return True when HERMES_EXECUTION_PROFILE=LOVABLE_BTC_OLD_SYSTEM."""
    profile = str(getattr(settings, "hermes_execution_profile", "") or "").upper().strip()
    return profile == PROFILE_NAME


def is_old_btc_strategy(strategy: str) -> bool:
    """Return True if strategy is one of the two old BTC execution strategies."""
    return str(strategy or "").upper() in OLD_BTC_STRATEGIES


def old_btc_mode_for_strategy(strategy: str) -> str:
    """Return the DemoRouter mode string for a given old BTC strategy."""
    return OLD_BTC_MODES.get(str(strategy or "").upper(), "DEMO_ADAPTIVE_FALLBACK")


def old_btc_rr_for_strategy(strategy: str, settings: object = None) -> float:
    """Return target RR for the strategy, reading from settings if available."""
    s = str(strategy or "").upper()
    if s == "ORDER_FLOW_EXECUTION_AGENT":
        return float(getattr(settings, "old_btc_order_flow_rr", OLD_BTC_ORDER_FLOW_RR) or OLD_BTC_ORDER_FLOW_RR)
    return float(getattr(settings, "old_btc_scalping_rr", OLD_BTC_SCALPING_RR) or OLD_BTC_SCALPING_RR)


def compute_old_btc_tp(entry: float, sl: float, target_rr: float, direction: str) -> float | None:
    """Compute RR-based TP. BUY: entry + risk*rr; SELL: entry - risk*rr."""
    if not (entry and sl and target_rr and direction):
        return None
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    d = str(direction or "").upper()
    if d == "BUY":
        return round(entry + risk * target_rr, 2)
    if d == "SELL":
        return round(entry - risk * target_rr, 2)
    return None
