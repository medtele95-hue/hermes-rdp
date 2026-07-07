"""ADAPTIVE_ACCOUNT_POLICY — three-level account policy driven by MT5 trade_mode.

Levels (fail-closed => REAL_UNKNOWN):
- DEMO           : no login pin, full policy, exploration/fallbacks EXECUTABLE.
- REAL_DECLARED  : login+server match the declared config -> strict policy,
                   exploration runs SHADOW only.
- REAL_UNKNOWN   : ultra-cautious (risk cap 2%, volume_min enforced,
                   kill-switch 1 loss/day, confluence >= 80), exploration shadow.

The policy is applied at BOOT ([ADAPTIVE_POLICY]) AND PER-ORDER ([ORDER_AUTH])
— the historical trap was enforcing only one of the two stages.

ACCOUNT_PROFILE ([ACCOUNT_PROFILE]) is built at boot from live symbol_info —
real specs, max fundable SL, TRADABLE flag — zero hardcoded values.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.logger import log

LEVEL_DEMO = "DEMO"
LEVEL_REAL_DECLARED = "REAL_DECLARED"
LEVEL_REAL_UNKNOWN = "REAL_UNKNOWN"

_TRADE_MODE_DEMO = 0
_TRADE_MODE_CONTEST = 1
_TRADE_MODE_REAL = 2


@dataclass(frozen=True)
class AccountPolicy:
    level: str
    exploration_executable: bool
    risk_cap_percent: float | None
    enforce_volume_min: bool
    max_losses_per_day: int
    max_daily_drawdown_percent: float | None
    min_confluence: float | None
    reason: str

    def as_payload(self) -> dict:
        return {
            "level": self.level,
            "exploration_executable": self.exploration_executable,
            "risk_cap_percent": self.risk_cap_percent,
            "enforce_volume_min": self.enforce_volume_min,
            "max_losses_per_day": self.max_losses_per_day,
            "max_daily_drawdown_percent": self.max_daily_drawdown_percent,
            "min_confluence": self.min_confluence,
            "reason": self.reason,
        }


_DEMO_POLICY = AccountPolicy(
    level=LEVEL_DEMO,
    exploration_executable=True,
    risk_cap_percent=None,
    enforce_volume_min=False,
    max_losses_per_day=6,
    max_daily_drawdown_percent=3.0,
    min_confluence=None,
    reason="TRADE_MODE_DEMO",
)

_REAL_DECLARED_POLICY = AccountPolicy(
    level=LEVEL_REAL_DECLARED,
    exploration_executable=False,
    risk_cap_percent=None,
    enforce_volume_min=False,
    max_losses_per_day=3,
    max_daily_drawdown_percent=3.0,
    min_confluence=None,
    reason="TRADE_MODE_REAL_LOGIN_SERVER_DECLARED",
)


def _real_unknown_policy(reason: str) -> AccountPolicy:
    return AccountPolicy(
        level=LEVEL_REAL_UNKNOWN,
        exploration_executable=False,
        risk_cap_percent=2.0,
        enforce_volume_min=True,
        max_losses_per_day=1,
        max_daily_drawdown_percent=2.0,
        min_confluence=80.0,
        reason=reason,
    )


def _field(source: object, name: str) -> object:
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def resolve_account_policy(account: object, settings: object) -> AccountPolicy:
    """Resolve the policy level from a live account snapshot. Fail-closed."""
    trade_mode_raw = _field(account, "trade_mode")
    try:
        trade_mode = int(trade_mode_raw)  # DEMO is 0 — never truthiness-test it
    except (TypeError, ValueError):
        return _real_unknown_policy("TRADE_MODE_UNREADABLE_FAIL_CLOSED")

    if trade_mode == _TRADE_MODE_DEMO:
        return _DEMO_POLICY
    if trade_mode == _TRADE_MODE_CONTEST:
        if bool(getattr(settings, "demo_allow_contest", False)):
            return _DEMO_POLICY
        return _real_unknown_policy("TRADE_MODE_CONTEST_FAIL_CLOSED")
    if trade_mode == _TRADE_MODE_REAL:
        declared_login = str(getattr(settings, "real_declared_login", "") or "").strip()
        declared_server = str(getattr(settings, "real_declared_server", "") or "").strip()
        login = str(_field(account, "login") or "").strip()
        server = str(_field(account, "server") or "").strip()
        if declared_login and declared_server and login == declared_login and server == declared_server:
            return _REAL_DECLARED_POLICY
        return _real_unknown_policy("REAL_LOGIN_SERVER_NOT_DECLARED")
    return _real_unknown_policy(f"TRADE_MODE_UNKNOWN_{trade_mode}_FAIL_CLOSED")


def log_adaptive_policy(policy: AccountPolicy, account: object) -> None:
    log.info(
        "[ADAPTIVE_POLICY] level=%s reason=%s login=%s exploration_executable=%s "
        "risk_cap_percent=%s enforce_volume_min=%s max_losses_per_day=%s min_confluence=%s",
        policy.level,
        policy.reason,
        _field(account, "login"),
        policy.exploration_executable,
        policy.risk_cap_percent,
        policy.enforce_volume_min,
        policy.max_losses_per_day,
        policy.min_confluence,
    )


def trading_authorized(account: object, settings: object) -> tuple[bool, str, AccountPolicy]:
    """Per-order authorization (stage 2). Re-resolves the policy every call.

    Returns (authorized, reason, policy). Fail-closed: unreadable account
    blocks; REAL accounts require the master allow_live_trading switch.
    """
    if account is None or _field(account, "trade_mode") is None:
        policy = _real_unknown_policy("ACCOUNT_UNREADABLE_FAIL_CLOSED")
        return False, "ORDER_AUTH_ACCOUNT_UNREADABLE", policy

    policy = resolve_account_policy(account, settings)
    if policy.level == LEVEL_DEMO:
        return True, "ORDER_AUTH_DEMO_OK", policy

    # REAL account (declared or unknown): the master live switch stays the
    # last line of defence — without it every REAL order is blocked (shadow).
    if not bool(getattr(settings, "allow_live_trading", False)):
        return False, "ORDER_AUTH_LIVE_DISABLED_FAIL_CLOSED", policy
    if policy.level == LEVEL_REAL_DECLARED:
        return True, "ORDER_AUTH_REAL_DECLARED_OK", policy
    return True, "ORDER_AUTH_REAL_UNKNOWN_ULTRA_CAUTIOUS", policy


def build_account_profile(
    account: object,
    symbols: dict[str, str] | list[str] | None,
    policy: AccountPolicy,
    symbol_info_fn=None,
) -> dict:
    """ACCOUNT_PROFILE at boot: live specs per symbol, max fundable SL,
    TRADABLE flag. Zero hardcode — everything read from MT5."""
    if symbol_info_fn is None:
        try:
            import MetaTrader5 as mt5
            symbol_info_fn = mt5.symbol_info
        except Exception:  # pragma: no cover - MT5 absent in CI
            symbol_info_fn = lambda _s: None  # noqa: E731

    balance = _to_float(_field(account, "balance"))
    equity = _to_float(_field(account, "equity"))
    currency = _field(account, "currency")
    risk_cap = policy.risk_cap_percent if policy.risk_cap_percent is not None else 100.0
    funding_base = equity if equity is not None else balance

    if isinstance(symbols, dict):
        symbol_names = list(symbols.values())
    else:
        symbol_names = list(symbols or [])

    profile_symbols: dict[str, dict] = {}
    for name in symbol_names:
        info = None
        try:
            info = symbol_info_fn(name)
        except Exception:
            info = None
        if info is None:
            profile_symbols[name] = {"tradable": False, "reason": "SYMBOL_INFO_UNAVAILABLE"}
            log.warning("[ACCOUNT_PROFILE] symbol=%s tradable=False reason=SYMBOL_INFO_UNAVAILABLE", name)
            continue
        volume_min = _to_float(_field(info, "volume_min"))
        volume_max = _to_float(_field(info, "volume_max"))
        volume_step = _to_float(_field(info, "volume_step"))
        tick_value = _to_float(_field(info, "trade_tick_value")) or _to_float(_field(info, "tick_value"))
        tick_size = _to_float(_field(info, "trade_tick_size")) or _to_float(_field(info, "tick_size"))
        stops_level = _to_float(_field(info, "trade_stops_level"))
        spread = _to_float(_field(info, "spread"))
        point = _to_float(_field(info, "point"))
        symbol_trade_mode = _field(info, "trade_mode")
        # SYMBOL_TRADE_MODE_FULL == 4 in the MT5 API; read dynamically when possible
        full_mode = 4
        try:
            import MetaTrader5 as _mt5
            full_mode = int(getattr(_mt5, "SYMBOL_TRADE_MODE_FULL", 4))
        except Exception:
            pass
        tradable = symbol_trade_mode is not None and int(symbol_trade_mode) == full_mode

        max_sl_usd = None
        max_sl_distance = None
        if funding_base is not None and volume_min and tick_value and tick_size and tick_value > 0 and tick_size > 0:
            max_sl_usd = funding_base * (risk_cap / 100.0)
            value_per_price_unit = tick_value / tick_size * volume_min
            if value_per_price_unit > 0 and math.isfinite(value_per_price_unit):
                max_sl_distance = max_sl_usd / value_per_price_unit

        profile_symbols[name] = {
            "tradable": bool(tradable),
            "symbol_trade_mode": symbol_trade_mode,
            "volume_min": volume_min,
            "volume_max": volume_max,
            "volume_step": volume_step,
            "tick_value": tick_value,
            "tick_size": tick_size,
            "point": point,
            "stops_level": stops_level,
            "spread": spread,
            "max_fundable_sl_usd": max_sl_usd,
            "max_fundable_sl_distance": max_sl_distance,
        }
        log.info(
            "[ACCOUNT_PROFILE] symbol=%s tradable=%s volume_min=%s volume_step=%s tick_value=%s "
            "tick_size=%s stops_level=%s spread=%s max_fundable_sl_usd=%s max_fundable_sl_distance=%s",
            name, tradable, volume_min, volume_step, tick_value, tick_size,
            stops_level, spread, max_sl_usd, max_sl_distance,
        )

    profile = {
        "login": _field(account, "login"),
        "server": _field(account, "server"),
        "trade_mode": _field(account, "trade_mode"),
        "currency": currency,
        "balance": balance,
        "equity": equity,
        "leverage": _field(account, "leverage"),
        "policy_level": policy.level,
        "symbols": profile_symbols,
    }
    log.info(
        "[ACCOUNT_PROFILE] login=%s server=%s trade_mode=%s currency=%s balance=%s equity=%s "
        "policy=%s symbols=%s tradable=%s",
        profile["login"], profile["server"], profile["trade_mode"], currency, balance, equity,
        policy.level, len(profile_symbols),
        sum(1 for s in profile_symbols.values() if s.get("tradable")),
    )
    return profile


def _to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None
