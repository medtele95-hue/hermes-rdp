from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.mt5.demo_router import _is_symbol_trade_cooldown_active, _last_demo_order_at


def test_last_demo_order_at_returns_latest_same_symbol_order():
    now = datetime.now(timezone.utc)
    events = [
        {"event_type": "DEMO_ORDER", "symbol": "BTCUSD#", "created_at": (now - timedelta(minutes=20)).isoformat()},
        {"event_type": "DEMO_ORDER", "symbol": "BTCUSD", "created_at": (now - timedelta(minutes=5)).isoformat()},
        {"event_type": "DEMO_ORDER", "symbol": "GOLD#", "created_at": now.isoformat()},
    ]
    assert _last_demo_order_at(events, "BTCUSD#") == now - timedelta(minutes=5)


def test_last_demo_order_at_ignores_failed_and_other_symbol_events():
    events = [
        {"event_type": "DEMO_ORDER_FAILED", "symbol": "BTCUSD", "created_at": datetime.now(timezone.utc).isoformat()},
        {"event_type": "DEMO_ORDER", "symbol": "EURUSD", "created_at": datetime.now(timezone.utc).isoformat()},
    ]
    assert _last_demo_order_at(events, "BTCUSD") is None


def test_demo_router_contains_cooldown_block_before_order_send():
    import inspect
    from app.mt5.demo_router import DemoKellyRouter
    source = inspect.getsource(DemoKellyRouter._first_block_reason)
    assert "SYMBOL_TRADE_COOLDOWN" in source


def test_cooldown_is_active_within_configured_window():
    now = datetime.now(timezone.utc)
    assert _is_symbol_trade_cooldown_active(
        now, now - timedelta(minutes=5), 15, enabled=True
    )


def test_cooldown_is_inactive_when_disabled_or_expired():
    now = datetime.now(timezone.utc)
    assert not _is_symbol_trade_cooldown_active(
        now, now - timedelta(minutes=5), 15, enabled=False
    )
    assert not _is_symbol_trade_cooldown_active(
        now, now - timedelta(minutes=15), 15, enabled=True
    )
