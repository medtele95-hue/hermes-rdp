from __future__ import annotations


def test_eurusd_weekend_blocks_before_spread_gate():
    from app.mt5.demo_router import _weekend_symbol_block_reason
    assert _weekend_symbol_block_reason("EURUSD", True) == "WEEKEND_CLOSED"


def test_gold_weekend_blocks_before_spread_gate():
    from app.mt5.demo_router import _weekend_symbol_block_reason
    assert _weekend_symbol_block_reason("GOLD#", True) == "WEEKEND_CLOSED"


def test_btc_weekend_is_delegated_to_btc_safety_rules():
    from app.mt5.demo_router import _weekend_symbol_block_reason
    assert _weekend_symbol_block_reason("BTCUSD#", True) is None


def test_weekday_does_not_add_weekend_block():
    from app.mt5.demo_router import _weekend_symbol_block_reason
    assert _weekend_symbol_block_reason("EURUSD", False) is None
