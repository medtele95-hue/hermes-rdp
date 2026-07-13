from __future__ import annotations

from unittest.mock import patch

import pytest


TIMEFRAMES = ["M1", "M5", "M15", "H1", "H4"]


def _tf_data(close: float, ema: float, rsi: float) -> dict:
    return {"close_prev": close, "ema_prev": ema, "rsi_prev": rsi, "timestamp": 123}


class TestMultiTimeframeMomentum:
    def test_all_bullish_timeframes_score_plus10(self):
        from app.mt5.multi_timeframe_momentum import calculate_momentum_confluence
        with patch("app.mt5.multi_timeframe_momentum.fetch_tf_data", return_value=_tf_data(110, 100, 60)):
            result = calculate_momentum_confluence("BTCUSD", TIMEFRAMES)
        assert result["composite_score"] == 10
        assert result["bull_count"] == 5
        assert result["momentum_score"] == 100.0

    def test_all_bearish_timeframes_score_minus10(self):
        from app.mt5.multi_timeframe_momentum import calculate_momentum_confluence
        with patch("app.mt5.multi_timeframe_momentum.fetch_tf_data", return_value=_tf_data(90, 100, 40)):
            result = calculate_momentum_confluence("BTCUSD", TIMEFRAMES)
        assert result["composite_score"] == -10
        assert result["bear_count"] == 5
        assert result["momentum_score"] == 0.0

    def test_mixed_timeframes_neut_score_0(self):
        from app.mt5.multi_timeframe_momentum import calculate_momentum_confluence
        values = [
            _tf_data(110, 100, 60), _tf_data(110, 100, 60),
            _tf_data(90, 100, 40), _tf_data(90, 100, 40),
            _tf_data(110, 100, 40),
        ]
        with patch("app.mt5.multi_timeframe_momentum.fetch_tf_data", side_effect=values):
            result = calculate_momentum_confluence("BTCUSD", TIMEFRAMES)
        assert result["composite_score"] == 0
        assert result["neut_count"] == 1
        assert result["confluence_active"] is False

    def test_confluence_4_of_5_bull_active(self):
        from app.mt5.multi_timeframe_momentum import calculate_momentum_confluence
        values = [_tf_data(110, 100, 60)] * 4 + [_tf_data(110, 100, 40)]
        with patch("app.mt5.multi_timeframe_momentum.fetch_tf_data", side_effect=values):
            result = calculate_momentum_confluence("BTCUSD", TIMEFRAMES, min_confluence=4)
        assert result["confluence_active"] is True
        assert result["confluence_direction"] == "BULL"
        assert result["confluence_strength"] == 0.8

    def test_momentum_score_maps_to_100_for_plus10(self):
        from app.mt5.multi_timeframe_momentum import momentum_score_to_confluence
        assert momentum_score_to_confluence(10, True, 1.0) == 100.0

    def test_momentum_score_maps_to_0_for_minus10(self):
        from app.mt5.multi_timeframe_momentum import momentum_score_to_confluence
        assert momentum_score_to_confluence(-10, True, 1.0) == 0.0
        assert momentum_score_to_confluence(-10, False, 0.0) == 0.0

    def test_insufficient_data_returns_none(self):
        from app.mt5.multi_timeframe_momentum import fetch_tf_data
        with patch("app.mt5.multi_timeframe_momentum.mt5.copy_rates_from_pos", return_value=[]):
            assert fetch_tf_data("BTCUSD", "M5") is None

    def test_fetch_uses_confirmed_bar_not_forming_bar(self):
        from app.mt5.multi_timeframe_momentum import fetch_tf_data
        rates = [{"time": i, "close": 100.0 + i} for i in range(40)]
        rates[-1]["close"] = 9999.0
        with patch("app.mt5.multi_timeframe_momentum.mt5.copy_rates_from_pos", return_value=rates):
            result = fetch_tf_data("BTCUSD", "M5")
        assert result is not None
        assert result["close_prev"] == rates[-2]["close"]
        assert result["timestamp"] == rates[-2]["time"]
        assert result["ema_prev"] < 1000.0

    def test_repainting_detection_raises_error(self):
        from app.mt5.multi_timeframe_momentum import RepaintingDataError, fetch_tf_data
        rates = [{"time": 2, "close": 101.0}, {"time": 1, "close": 100.0}, {"time": 3, "close": 102.0}]
        with patch("app.mt5.multi_timeframe_momentum.mt5.copy_rates_from_pos", return_value=rates):
            with pytest.raises(RepaintingDataError):
                fetch_tf_data("BTCUSD", "M5", ema_length=1, rsi_length=1)

    def test_min_confluence_5_with_4_bull_inactive(self):
        from app.mt5.multi_timeframe_momentum import calculate_momentum_confluence
        values = [_tf_data(110, 100, 60)] * 4 + [_tf_data(110, 100, 40)]
        with patch("app.mt5.multi_timeframe_momentum.fetch_tf_data", side_effect=values):
            result = calculate_momentum_confluence("BTCUSD", TIMEFRAMES, min_confluence=5)
        assert result["confluence_active"] is False

    def test_momentum_injected_into_final_confluence(self):
        from app.mt5.geometric_engine_v2 import final_trade_gate
        common = dict(
            geometric_result={"score": 70, "gann_confluence": 50},
            order_flow={"score": 70}, smc_result={"status": "PASS", "score": 70},
            mtfa_result={"status": "PASS", "score": 70, "trend_strength": 0.5},
            spread_usd=1, atr=20, session="LONDON", symbol="BTCUSD",
            capital_risk_pct=0.5, mode="SHADOW",
        )
        low = final_trade_gate(**common, momentum_result={"momentum_score": 0})
        high = final_trade_gate(**common, momentum_result={"momentum_score": 100})
        assert high["component_scores"]["momentum"] == 100
        assert high["final_confluence"] > low["final_confluence"]

    def test_momentum_and_geometric_alignment_bonus(self):
        from app.mt5.geometric_engine_v2 import final_trade_gate
        result = final_trade_gate(
            {"score": 90, "gann_confluence": 90}, {"score": 90},
            {"status": "PASS", "score": 90}, {"status": "PASS", "score": 90, "trend_strength": 0.5},
            1, 20, "LONDON", "BTCUSD", 0.5, "SHADOW",
            momentum_result={"momentum_score": 95, "aligned": True},
        )
        assert result["alignment_bonus"] == 15.0

    def test_momentum_vs_structure_conflict_penalty(self):
        from app.mt5.geometric_engine_v2 import final_trade_gate
        result = final_trade_gate(
            {"score": 70}, {"score": 70},
            {"status": "SOFT_FAIL", "score": 20}, {"status": "PASS", "score": 40, "trend_strength": 0.5},
            1, 20, "LONDON", "BTCUSD", 0.5, "SHADOW",
            momentum_result={"momentum_score": 90, "aligned": False},
        )
        assert result["conflict_penalty"] >= 10.0

    def test_final_gate_logs_traceable_components(self):
        import unittest
        from app.mt5.geometric_engine_v2 import final_trade_gate
        from app.utils import throttle

        # [FINAL_GATE] passe par log_event_throttled (geometric_engine_v2.py:633), dont
        # l'etat `_event_state` est au niveau MODULE (throttle.py:10) et supprime une
        # re-emission identique dans les 60 s. Un autre test de ce fichier peut donc
        # avoir deja consomme la meme cle : l'emission est etouffee et le `next(...)`
        # ci-dessous ne trouve rien. C'etait la cause reelle de l'echec intermittent
        # de ce test (et non une rotation de log, comme suppose un temps).
        # On isole le throttle pour ce test — aucun changement de comportement produit.
        throttle._event_state.clear()

        with unittest.TestCase().assertLogs("hermes", level="INFO") as captured:
            final_trade_gate(
                {"score": 70}, {"score": 70},
                {"status": "PASS", "score": 70}, {"status": "PASS", "score": 70, "trend_strength": 0.5},
                1, 20, "LONDON", "BTCUSD", 0.5, "SHADOW",
                momentum_result={"momentum_score": 70, "aligned": True},
            )
        line = next(line for line in captured.output if "[FINAL_GATE]" in line)
        assert "symbol=BTCUSD" in line
        assert "components=" in line
        assert "momentum" in line
        assert "mode=SHADOW" in line
