from __future__ import annotations

import unittest
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from app.agents.strategies import quant_statistical_pullback as quant
from app.config import Settings
from app.services.quant_statistical_audit import build_quant_statistical_audit, write_quant_statistical_audit


def quant_settings(**overrides) -> Settings:
    values = {
        "hermes_quant_strategy_enabled": True,
        "hermes_quant_strategy_role": "ENTRY_STRATEGY",
        "hermes_quant_reg_period": 50,
        "hermes_quant_min_r2": 0.30,
        "hermes_quant_z_period": 20,
        "hermes_quant_z_entry": 1.0,
        "hermes_quant_stdev_period": 20,
        "hermes_quant_sl_stdev_mult": 2.0,
        "hermes_quant_min_score": 75,
        "hermes_quant_min_rr": 2.0,
    }
    values.update(overrides)
    return Settings(**values)


def m5_frame(closes: list[float]) -> pd.DataFrame:
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = []
    for idx, close in enumerate(closes):
        open_ = closes[idx - 1] if idx else close
        rows.append(
            {
                "candle_time": start + timedelta(minutes=5 * idx),
                "open": open_,
                "high": max(open_, close) + 0.01,
                "low": min(open_, close) - 0.01,
                "close": close,
                "spread": 1,
                "tick_volume": 100,
            }
        )
    return pd.DataFrame(rows)


def buy_closes() -> list[float]:
    values = [100 + i * 0.05 for i in range(60)]
    values[-1] -= 1.0
    return values + [values[-1] + 4.0]


def sell_closes() -> list[float]:
    values = [100 - i * 0.05 for i in range(60)]
    values[-1] += 1.0
    return values + [values[-1] - 4.0]


class QuantStatisticalPullbackTests(unittest.TestCase):
    def test_positive_slope_r2_ok_negative_z_creates_buy_candidate(self) -> None:
        result = quant.evaluate("EURUSD", {"M5": m5_frame(buy_closes())}, {}, quant_settings())
        self.assertEqual(result["strategy"], "QUANT_STATISTICAL_PULLBACK")
        self.assertEqual(result["signal"], "BUY")
        self.assertGreater(result["quant_slope"], 0)
        self.assertGreaterEqual(result["quant_r2"], 0.30)
        self.assertLessEqual(result["quant_z_score"], -1.0)
        self.assertGreaterEqual(result["quant_score"], 75)

    def test_negative_slope_r2_ok_positive_z_creates_sell_candidate(self) -> None:
        result = quant.evaluate("EURUSD", {"M5": m5_frame(sell_closes())}, {}, quant_settings())
        self.assertEqual(result["signal"], "SELL")
        self.assertLess(result["quant_slope"], 0)
        self.assertGreaterEqual(result["quant_r2"], 0.30)
        self.assertGreaterEqual(result["quant_z_score"], 1.0)
        self.assertGreaterEqual(result["quant_score"], 75)

    def test_low_r2_creates_wait_no_candidate(self) -> None:
        values = [100 + (idx % 2) * 2 for idx in range(61)]
        result = quant.evaluate("EURUSD", {"M5": m5_frame(values)}, {}, quant_settings(hermes_quant_min_r2=0.95))
        self.assertEqual(result["signal"], "WAIT")
        self.assertIn(result["quant_reason"], {"QUANT_R2_BELOW_MIN", "QUANT_PULLBACK_CONDITION_NOT_MET"})

    def test_invalid_stdev_creates_wait_no_candidate(self) -> None:
        result = quant.evaluate("EURUSD", {"M5": m5_frame([100.0] * 61)}, {}, quant_settings())
        self.assertEqual(result["signal"], "WAIT")
        self.assertEqual(result["quant_reason"], "QUANT_INVALID_STDEV")

    def test_buy_sl_tp_rr_valid(self) -> None:
        result = quant.evaluate("EURUSD", {"M5": m5_frame(buy_closes())}, {}, quant_settings())
        self.assertLess(result["sl"], result["entry"])
        self.assertLess(result["entry"], result["tp"])
        self.assertGreaterEqual(result["risk_reward"], 2.0)

    def test_sell_sl_tp_rr_valid(self) -> None:
        result = quant.evaluate("EURUSD", {"M5": m5_frame(sell_closes())}, {}, quant_settings())
        self.assertLess(result["tp"], result["entry"])
        self.assertLess(result["entry"], result["sl"])
        self.assertGreaterEqual(result["risk_reward"], 2.0)

    def test_strategy_uses_closed_candles_only_and_no_lookahead(self) -> None:
        values = buy_closes()
        frame = m5_frame(values)
        changed_forming = frame.copy()
        changed_forming.loc[len(changed_forming) - 1, "close"] = 999999.0
        base = quant.evaluate("BTCUSD#", {"M5": frame}, {}, quant_settings())
        changed = quant.evaluate("BTCUSD#", {"M5": changed_forming}, {}, quant_settings())
        self.assertEqual(base["entry"], changed["entry"])
        self.assertEqual(base["quant_z_score"], changed["quant_z_score"])

    def test_strategy_audit_report_is_generated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            out_dir = Path(tmp) / "reports"
            path = write_quant_statistical_audit(
                quant_settings(btc_disable_quant_statistical_pullback=True),
                "BTCUSD#",
                48,
                output_dir=out_dir,
                events_path=events,
                now=datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
            )
            self.assertEqual(path.name, "strategy_audit_BTCUSD_QUANT_STATISTICAL_PULLBACK.json")
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "DISABLED_PENDING_AUDIT")
        self.assertEqual(payload["math_checks"]["lookahead_free"], True)
        self.assertEqual(payload["math_checks"]["closed_candles_only"], True)

    def test_audit_detects_invalid_kelly_lot(self) -> None:
        now = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            events.write_text(
                json.dumps(
                    {
                        "event_type": "DEMO_SKIP",
                        "created_at": now.isoformat(),
                        "symbol": "BTCUSD#",
                        "strategy": "QUANT_STATISTICAL_PULLBACK",
                        "reason": "KELLY_INVALID_LOT",
                        "kelly_suggested_lot": 0,
                    }
                ),
                encoding="utf-8",
            )
            report = build_quant_statistical_audit(quant_settings(), "BTCUSD#", 48, events, now)
        self.assertIn("KELLY_INVALID_LOT", report["failures"])
        self.assertFalse(report["math_checks"]["kelly_valid"])

    def test_audit_detects_invalid_rr_sltp(self) -> None:
        now = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            events.write_text(
                json.dumps(
                    {
                        "event_type": "DEMO_SKIP",
                        "created_at": now.isoformat(),
                        "symbol": "BTCUSD#",
                        "strategy": "QUANT_STATISTICAL_PULLBACK",
                        "signal": "BUY",
                        "entry": 100,
                        "sl": 101,
                        "tp": 99,
                        "reward_risk": -1,
                    }
                ),
                encoding="utf-8",
            )
            report = build_quant_statistical_audit(quant_settings(), "BTCUSD#", 48, events, now)
        self.assertIn("INVALID_RR", report["failures"])
        self.assertIn("INVALID_SL_TP", report["failures"])
        self.assertFalse(report["math_checks"]["rr_valid"])
        self.assertFalse(report["math_checks"]["sl_tp_valid"])


if __name__ == "__main__":
    unittest.main()
