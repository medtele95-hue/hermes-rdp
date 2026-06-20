from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

import MetaTrader5 as mt5
import pandas as pd


TIMEFRAMES = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


class MT5DataReader:
    def get_candles(self, symbol: str, timeframe: str, count: int = 300) -> pd.DataFrame:
        rates = mt5.copy_rates_from_pos(symbol, TIMEFRAMES[timeframe], 0, count)
        if rates is None or len(rates) == 0:
            return pd.DataFrame()

        df = pd.DataFrame(rates)
        df["candle_time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df

    def get_all_timeframes(self, symbol: str, count: int = 300) -> Dict[str, pd.DataFrame]:
        return {timeframe: self.get_candles(symbol, timeframe, count) for timeframe in TIMEFRAMES}

    def latest_candle_rows(self, requested_symbol: str, broker_symbol: str) -> List[dict]:
        rows: List[dict] = []
        for timeframe, df in self.get_all_timeframes(broker_symbol, count=3).items():
            if df.empty or len(df) < 2:
                continue
            latest = df.iloc[-2]
            rows.append(
                {
                    "symbol": requested_symbol,
                    "broker_symbol": broker_symbol,
                    "timeframe": timeframe,
                    "candle_time": latest["candle_time"].isoformat(),
                    "open": float(latest["open"]),
                    "high": float(latest["high"]),
                    "low": float(latest["low"]),
                    "close": float(latest["close"]),
                    "tick_volume": int(latest["tick_volume"]),
                    "spread": int(latest["spread"]),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        return rows

    def account_snapshot(self) -> Optional[dict]:
        account = mt5.account_info()
        if account is None:
            return None
        data = account._asdict()
        return {
            "login": data.get("login"),
            "name": data.get("name"),
            "server": data.get("server"),
            "company": data.get("company"),
            "trade_mode": data.get("trade_mode"),
            "trade_allowed": data.get("trade_allowed"),
            "trade_expert": data.get("trade_expert"),
            "currency": data.get("currency"),
            "balance": float(data.get("balance", 0)),
            "equity": float(data.get("equity", 0)),
            "margin": float(data.get("margin", 0)),
            "free_margin": float(data.get("margin_free", 0)),
            "margin_level": float(data.get("margin_level", 0) or 0),
            "profit": float(data.get("profit", 0)),
            "snapshot_time": datetime.now(timezone.utc).isoformat(),
        }

    def hermes_open_positions_count(self, magic_number: int) -> int:
        positions = mt5.positions_get()
        if not positions:
            return 0
        return sum(1 for pos in positions if getattr(pos, "magic", None) == magic_number)

    def symbol_trade_specs(self, symbol: str) -> dict:
        info = mt5.symbol_info(symbol)
        if info is None:
            return {"symbol_info_available": False}
        data = info._asdict()
        tick_value = (
            _float_or_none(data.get("trade_tick_value"))
            or _float_or_none(data.get("trade_tick_value_profit"))
            or _float_or_none(data.get("trade_tick_value_loss"))
        )
        return {
            "symbol_info_available": True,
            "tick_value": tick_value,
            "tick_size": _float_or_none(data.get("trade_tick_size")),
            "contract_size": _float_or_none(data.get("trade_contract_size")),
            "volume_min": _float_or_none(data.get("volume_min")),
            "volume_max": _float_or_none(data.get("volume_max")),
            "volume_step": _float_or_none(data.get("volume_step")),
            "point": _float_or_none(data.get("point")),
            "digits": data.get("digits"),
        }

    def symbol_tick(self, symbol: str) -> dict:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return {}
        data = tick._asdict()
        return {
            "bid": _float_or_none(data.get("bid")),
            "ask": _float_or_none(data.get("ask")),
            "last": _float_or_none(data.get("last")),
            "time": data.get("time"),
        }


def _float_or_none(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
