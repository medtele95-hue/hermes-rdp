"""Centralized MT5 account mode detection.

Never tests truthiness of trade_mode — ACCOUNT_TRADE_MODE_DEMO is 0,
which is falsy in Python.  Always use is_mt5_demo_account() instead
of raw `if account.trade_mode` or `trade_mode or -1` patterns.
"""
from __future__ import annotations

from app.logger import log

_DEMO_MODE_FALLBACK: int = 0  # mt5.ACCOUNT_TRADE_MODE_DEMO


def is_mt5_demo_account(account_info: object) -> bool:
    """Return True iff account_info.trade_mode == ACCOUNT_TRADE_MODE_DEMO (0).

    Safe against: None account_info, missing trade_mode, non-int trade_mode,
    and the Python truthiness trap where trade_mode=0 evaluates as falsy.

    Emits [MT5_ACCOUNT_MODE_DIAG] at debug level.
    """
    if account_info is None:
        log.debug("[MT5_ACCOUNT_MODE_DIAG] account_info=None is_demo=False")
        return False
    trade_mode = getattr(account_info, "trade_mode", None)
    if trade_mode is None:
        log.debug("[MT5_ACCOUNT_MODE_DIAG] trade_mode=MISSING is_demo=False")
        return False
    try:
        mode_int = int(trade_mode)  # no `or` — DEMO is 0, which is falsy
    except (TypeError, ValueError):
        log.warning(
            "[MT5_ACCOUNT_MODE_DIAG] trade_mode=%r cast_error is_demo=False", trade_mode
        )
        return False
    try:
        import MetaTrader5 as _mt5
        demo_value = int(_mt5.ACCOUNT_TRADE_MODE_DEMO)
    except Exception:
        demo_value = _DEMO_MODE_FALLBACK
    is_demo = mode_int == demo_value
    log.debug(
        "[MT5_ACCOUNT_MODE_DIAG] trade_mode=%s demo_value=%s is_demo=%s",
        mode_int, demo_value, is_demo,
    )
    return is_demo
