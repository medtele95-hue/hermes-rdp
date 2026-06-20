from __future__ import annotations

from dataclasses import dataclass

from app.logger import log
from app.strategies.candidate import validate_candidate


ALLOWED_STRATEGIES = frozenset({
    "ORDER_FLOW_EXECUTION_AGENT",
    "BTC_SCALPING_AGENT",
    "FIB_CONFLUENCE_EXECUTION_AGENT",
    "GOLD_LIQUIDITY_HUNTER_PRO",
    "GOLD_M1_M5_EMA_SWEEP_SCALPER",
    "EUR_EMA_RSI_ATR_CROSSOVER",
    "SIMO_ATM_BREAKOUT",
    "HERMES_STRATEGY_PACK_AGENT",
})


@dataclass(frozen=True)
class BalancedSelection:
    best: dict | None
    accepted: list[dict]
    rejected: list[dict]


def select_best_candidate(
    candidates: list[dict],
    open_trades: list[dict] | None = None,
    *,
    max_total_open: int = 3,
    magic_number: int | None = None,
) -> BalancedSelection:
    open_trades = open_trades or []
    accepted: list[dict] = []
    rejected: list[dict] = []
    represented = {_asset_key(t.get("symbol") or t.get("broker_symbol")) for t in open_trades}
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        strategy = _strategy(candidate)
        symbol = str(candidate.get("symbol") or candidate.get("broker_symbol") or "")
        log.info(
            "[BALANCED_SELECTOR_IN] symbol=%s strategy=%s grade=%s score=%s rr=%s",
            symbol,
            strategy,
            candidate.get("grade"),
            _score(candidate),
            _rr(candidate),
        )
        reason = _reject_reason(candidate, open_trades, max_total_open, magic_number)
        if reason:
            rejected.append({**candidate, "balanced_selector_reason": reason})
            log.info("[BALANCED_SELECTOR_REJECT] symbol=%s strategy=%s reason=%s", symbol, strategy, reason)
            continue
        accepted.append(candidate)
        log.info("[BALANCED_SELECTOR_ACCEPT] symbol=%s strategy=%s", symbol, strategy)
    if not accepted:
        log.info("[BALANCED_SELECTOR_BEST] symbol=NONE reason=NO_ROUTEABLE_CANDIDATE")
        return BalancedSelection(best=None, accepted=[], rejected=rejected)
    accepted.sort(key=lambda c: _rank(c, represented), reverse=True)
    best = accepted[0]
    log.info("[BALANCED_SELECTOR_BEST] symbol=%s strategy=%s reason=RANKED_BEST", best.get("symbol"), _strategy(best))
    return BalancedSelection(best=best, accepted=accepted, rejected=rejected)


def _reject_reason(candidate: dict, open_trades: list[dict], max_total_open: int, magic_number: int | None) -> str | None:
    strategy = _strategy(candidate)
    symbol = str(candidate.get("symbol") or candidate.get("broker_symbol") or "")
    if strategy not in ALLOWED_STRATEGIES:
        return "STRATEGY_NOT_ACTIVE_EXECUTION"
    if str(candidate.get("safety_guard_status") or "PASS").upper() != "PASS":
        return "SAFETY_GUARD_BLOCK"
    if str(candidate.get("time_gate_status") or "PASS").upper() != "PASS":
        return "TIME_GATE_BLOCK"
    valid, reason, field = validate_candidate(_contract(candidate))
    if not valid:
        return f"{reason}_{field}"
    if (_final_score(candidate) or 0.0) < 55.0:
        return "FINAL_CONFLUENCE_TOO_LOW"
    if _grade_rank(_final_grade(candidate)) < _grade_rank("C"):
        return "FINAL_CONFLUENCE_GRADE_TOO_LOW"
    if (_rr(candidate) or 0.0) < 1.5:
        return "RR_TOO_LOW"
    if str(candidate.get("market_open", candidate.get("symbol_market_open", True))).upper() == "FALSE":
        return "MARKET_CLOSED"
    if str(candidate.get("spread_ok", True)).upper() == "FALSE":
        return "SPREAD_TOO_HIGH"
    matching_open = [_normalize_trade(t) for t in open_trades]
    if len(matching_open) >= max_total_open:
        return "MAX_TOTAL_OPEN_DEMO_TRADES"
    asset = _asset_key(symbol)
    if any(_asset_key(t.get("symbol")) == asset for t in matching_open):
        return f"MAX_{asset}_EXPOSURE" if asset in {"BTC", "GOLD", "EUR", "US100"} else "MAX_ONE_OPEN_TRADE_PER_SYMBOL"
    if any(_same_symbol_strategy_magic(t, symbol, strategy, magic_number) for t in matching_open):
        return "MAX_ONE_OPEN_TRADE_PER_SYMBOL_STRATEGY_MAGIC"
    if asset == "BTC" and any(_asset_key(t.get("symbol")) != "BTC" for t in matching_open):
        return None
    return None


def _rank(candidate: dict, represented: set[str]) -> tuple:
    asset = _asset_key(candidate.get("symbol") or candidate.get("broker_symbol"))
    diversification_bonus = 1 if asset and asset not in represented else 0
    return (
        _grade_rank(candidate.get("grade")),
        _score(candidate) or 0.0,
        _final_score(candidate) or 0.0,
        _rr(candidate) or 0.0,
        diversification_bonus,
        _strategy_priority(candidate),
    )


def _strategy_priority(candidate: dict) -> int:
    strategy = _strategy(candidate)
    asset = _asset_key(candidate.get("symbol") or candidate.get("broker_symbol"))
    if strategy == "ORDER_FLOW_EXECUTION_AGENT":
        return 100
    if asset == "GOLD" and strategy in {"GOLD_LIQUIDITY_HUNTER_PRO", "GOLD_M1_M5_EMA_SWEEP_SCALPER"}:
        return 90
    if asset == "EUR" and strategy == "EUR_EMA_RSI_ATR_CROSSOVER":
        return 85
    if asset == "US100" and strategy == "SIMO_ATM_BREAKOUT":
        return 80
    if strategy == "FIB_CONFLUENCE_EXECUTION_AGENT":
        return 78
    if strategy == "HERMES_STRATEGY_PACK_AGENT":
        return 70
    if strategy == "BTC_SCALPING_AGENT":
        return 10
    return 50


def _contract(candidate: dict) -> dict:
    out = {
        "strategy": _strategy(candidate),
        "symbol": candidate.get("symbol"),
        "broker_symbol": candidate.get("broker_symbol") or candidate.get("symbol"),
        "direction": candidate.get("direction") or candidate.get("signal"),
        "entry": candidate.get("entry"),
        "sl": candidate.get("sl"),
        "tp": candidate.get("tp"),
        "rr": _rr(candidate),
        "confidence": _score(candidate),
        "grade": candidate.get("grade"),
        "mode": candidate.get("mode") or "ACTIVE_EXECUTION",
        "route_allowed": candidate.get("route_allowed", True),
        "demo_eligible": candidate.get("demo_eligible", True),
    }
    return out


def _normalize_trade(trade: dict) -> dict:
    return {
        "symbol": str(trade.get("symbol") or trade.get("broker_symbol") or ""),
        "strategy": str(trade.get("strategy") or "").upper(),
        "magic_number": trade.get("magic_number"),
    }


def _same_symbol_strategy_magic(trade: dict, symbol: str, strategy: str, magic_number: int | None) -> bool:
    if _asset_key(trade.get("symbol")) != _asset_key(symbol):
        return False
    if str(trade.get("strategy") or "").upper() != strategy:
        return False
    if magic_number is None:
        return True
    return str(trade.get("magic_number") or "") == str(magic_number)


def _strategy(candidate: dict) -> str:
    return str(candidate.get("best_strategy") or candidate.get("strategy") or "").upper()


def _score(candidate: dict) -> float | None:
    return _num(candidate.get("confidence"), candidate.get("edge_score"), candidate.get("setup_score"))


def _rr(candidate: dict) -> float | None:
    return _num(candidate.get("rr"), candidate.get("risk_reward"), candidate.get("reward_risk"))


def _final_score(candidate: dict) -> float | None:
    return _num(candidate.get("final_confluence_score"), candidate.get("confluence_score"), candidate.get("smc_score"))


def _final_grade(candidate: dict) -> str:
    return str(candidate.get("final_confluence_grade") or candidate.get("confluence_grade") or candidate.get("grade") or "").upper()


def _num(*values: object) -> float | None:
    for value in values:
        try:
            if value is None:
                continue
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _asset_key(symbol: object) -> str:
    text = str(symbol or "").upper()
    if text.startswith("BTC"):
        return "BTC"
    if text.startswith(("GOLD", "XAU")):
        return "GOLD"
    if text.startswith("EURUSD"):
        return "EUR"
    if text.startswith(("US100", "NAS100", "USTEC")):
        return "US100"
    return text.replace("#", "")


def _grade_rank(grade: object) -> int:
    return {"A_PLUS": 4, "A+": 4, "A": 3, "B": 2, "C": 1, "D": 0}.get(str(grade or "").upper(), 0)
