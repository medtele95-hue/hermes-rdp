from __future__ import annotations


def test_atr_below_20_selects_quick_only():
    from app.mt5.btc_exit_arbiter import select_exit_manager
    result = select_exit_manager(19.99, "B")
    assert result["manager"] == "QUICK"
    assert result["enabled_managers"] == ["QUICK"]


def test_atr_20_to_60_selects_dynamic_only():
    from app.mt5.btc_exit_arbiter import select_exit_manager
    assert select_exit_manager(20.0, "A")["manager"] == "DYNAMIC"
    assert select_exit_manager(60.0, "A")["manager"] == "DYNAMIC"


def test_atr_above_60_selects_swing_only():
    from app.mt5.btc_exit_arbiter import select_exit_manager
    result = select_exit_manager(60.01, "A+")
    assert result["manager"] == "SWING"
    assert result["enabled_managers"] == ["SWING"]


def test_invalid_atr_fails_to_swing_protection():
    from app.mt5.btc_exit_arbiter import select_exit_manager
    assert select_exit_manager(None, "D")["manager"] == "SWING"
    assert select_exit_manager(-1, "D")["manager"] == "SWING"


def test_exactly_one_manager_is_always_enabled():
    from app.mt5.btc_exit_arbiter import select_exit_manager
    for atr in (0.1, 20, 40, 60, 100, None):
        assert len(select_exit_manager(atr, "B")["enabled_managers"]) == 1


def _dynamic(atr: float) -> dict:
    return {
        "mode": "dynamic", "atr_value": atr, "tp_usd": 6.0, "lock_usd": 1.0,
        "trail_start_usd": 2.0, "trail_gap_usd": 1.0, "rr_target": 2.0,
        "realized_rr": 2.0,
    }


def _daemon_and_position():
    from types import SimpleNamespace
    from app.mt5.btc_fast_exit_daemon import BtcFastExitDaemon
    closed = []
    settings = SimpleNamespace(btc_exit_arbiter_enabled=True, hermes_execution_profile="")
    daemon = BtcFastExitDaemon(settings, lambda pos, reason: closed.append(reason) or {"status": "ORDER_CONFIRMED", "order_result": {"retcode": 10009}})
    position = SimpleNamespace(ticket=1, profit=0.10, price_open=100.0, type=0, grade="A")
    return daemon, position, closed


def test_quick_authority_can_close_but_does_not_run_sl_engine():
    from unittest.mock import patch
    daemon, position, closed = _daemon_and_position()
    params = _dynamic(10.0)
    with patch("app.mt5.btc_fast_exit_daemon._dynamic_exit.compute", return_value=params), \
         patch("app.mt5.btc_fast_exit_daemon._dynamic_exit.adjust_on_tick", return_value=params), \
         patch("app.mt5.btc_fast_exit_daemon._sl_engine.run") as sl_run, \
         patch.object(daemon, "_publish_state"):
        daemon._evaluate_position(position, 0.03, 0.01, True)
    assert closed == ["ANY_POSITIVE_FAST_EXIT"]
    sl_run.assert_not_called()


def test_dynamic_authority_runs_sl_engine_but_not_quick_close():
    # F-05 fix: SL engine must run for DYNAMIC authority (atr in 20-60 range).
    # Position is not closed because profit (0.10) is below DYNAMIC tp_usd (6.0).
    from unittest.mock import patch
    daemon, position, closed = _daemon_and_position()
    params = _dynamic(30.0)
    with patch("app.mt5.btc_fast_exit_daemon._dynamic_exit.compute", return_value=params), \
         patch("app.mt5.btc_fast_exit_daemon._dynamic_exit.adjust_on_tick", return_value=params), \
         patch("app.mt5.btc_fast_exit_daemon._sl_engine.run", return_value={"applied": False, "reason": "TEST"}) as sl_run:
        daemon._evaluate_position(position, 0.03, 0.01, True)
    assert closed == []
    sl_run.assert_called_once()


def test_swing_authority_runs_sl_manager_without_quick_close():
    from unittest.mock import patch
    daemon, position, closed = _daemon_and_position()
    params = _dynamic(70.0)
    with patch("app.mt5.btc_fast_exit_daemon._dynamic_exit.compute", return_value=params), \
         patch("app.mt5.btc_fast_exit_daemon._dynamic_exit.adjust_on_tick", return_value=params), \
         patch("app.mt5.btc_fast_exit_daemon._sl_engine.run", return_value={"applied": False, "reason": "TEST"}) as sl_run:
        daemon._evaluate_position(position, 0.03, 0.01, True)
    assert closed == []
    sl_run.assert_called_once()
