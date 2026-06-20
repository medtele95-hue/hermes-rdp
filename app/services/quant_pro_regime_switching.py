from __future__ import annotations

import math
import logging
from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.config import Settings


STRATEGY = "QUANT_PRO_REGIME_SWITCHING"
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuantProResult:
    payload: dict


class QuantProRegimeSwitching:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(self, symbol: str, candles: Any) -> dict:
        df = _closed_frame(candles)
        required = max(
            self.settings.hermes_quant_pro_reg_period,
            self.settings.hermes_quant_pro_kalman_period,
            self.settings.hermes_quant_pro_ou_period,
            self.settings.hermes_quant_pro_hurst_period,
            self.settings.hermes_quant_pro_ewma_vol_period + 1,
        )
        if len(df) < required:
            return self._wait(symbol, "QUANT_PRO_NOT_ENOUGH_CLOSED_CANDLES", no_lookahead=True)
        closes = pd.to_numeric(df["close"], errors="coerce").dropna().astype(float).tolist()
        if len(closes) < required:
            return self._wait(symbol, "QUANT_PRO_NO_CLOSED_CANDLES", no_lookahead=True)

        ols = ols_regression(closes[-self.settings.hermes_quant_pro_reg_period :])
        kal = kalman_filter(
            closes[-self.settings.hermes_quant_pro_kalman_period :],
            self.settings.hermes_quant_pro_kal_q_level,
            self.settings.hermes_quant_pro_kal_q_vel,
            self.settings.hermes_quant_pro_kal_r,
        )
        ou = ou_test(closes[-self.settings.hermes_quant_pro_ou_period :], self.settings.hermes_quant_pro_tcrit)
        hurst = hurst_rs(closes[-self.settings.hermes_quant_pro_hurst_period :])
        ewma = ewma_volatility(closes[-(self.settings.hermes_quant_pro_ewma_vol_period + 1) :], self.settings.hermes_quant_pro_ewma_lambda)
        price = closes[-1]
        if ewma <= 0 or not math.isfinite(ewma):
            return self._wait(symbol, "QUANT_PRO_INVALID_EWMA_VOL", ols, kal, ou, hurst, ewma, no_lookahead=True)

        mean_reverting = bool(
            ou["beta"] < 0
            and ou["tstat"] < -self.settings.hermes_quant_pro_tcrit
            and self.settings.hermes_quant_pro_hl_min <= ou["half_life"] <= self.settings.hermes_quant_pro_hl_max
        )
        trending = bool(abs(ols["tstat"]) > self.settings.hermes_quant_pro_tcrit and not mean_reverting)
        regime = "MEAN_REVERSION" if mean_reverting else ("TREND" if trending else "FLAT")
        direction = "WAIT"
        if regime == "TREND":
            hurst_filter = _hurst_filter_payload(hurst, self.settings)
            _log_hurst(symbol, hurst_filter)
            if not hurst_filter["passed"]:
                return self._wait(symbol, str(hurst_filter["block_reason"]), ols, kal, ou, hurst, ewma, regime, hurst_filter, no_lookahead=True)
            if ols["slope"] > 0 and kal["velocity"] > 0:
                direction = "BUY"
            elif ols["slope"] < 0 and kal["velocity"] < 0:
                direction = "SELL"
        elif regime == "MEAN_REVERSION":
            if kal["z"] < -self.settings.hermes_quant_pro_z_entry:
                direction = "BUY"
            elif kal["z"] > self.settings.hermes_quant_pro_z_entry:
                direction = "SELL"
        if direction == "WAIT":
            return self._wait(symbol, f"QUANT_PRO_{regime}_NO_DIRECTION", ols, kal, ou, hurst, ewma, regime, no_lookahead=True)

        sl_dist = price * ewma * self.settings.hermes_quant_pro_sl_vol_mult
        tp_dist = sl_dist * self.settings.hermes_quant_pro_min_rr
        if direction == "BUY":
            sl = price - sl_dist
            tp = price + tp_dist
        else:
            sl = price + sl_dist
            tp = price - tp_dist
        rr = reward_risk(direction, price, sl, tp)
        if sl_dist <= 0 or rr is None or rr + 1e-9 < self.settings.hermes_quant_pro_min_rr:
            return self._wait(symbol, "QUANT_PRO_INVALID_SL_TP_RR", ols, kal, ou, hurst, ewma, regime, no_lookahead=True)
        score = score_regime(regime, direction, ols, kal, ou, hurst, ewma, rr, self.settings)
        if score < self.settings.hermes_quant_pro_min_score:
            return self._wait(symbol, "QUANT_PRO_SCORE_BELOW_MIN", ols, kal, ou, hurst, ewma, regime, no_lookahead=True)
        grade = grade_for(score)
        return {
            "strategy": STRATEGY,
            "role": "ENTRY_STRATEGY",
            "regime": regime,
            "direction": direction,
            "signal": direction,
            "score": score,
            "grade": grade,
            "entry": round(price, 8),
            "sl": round(sl, 8),
            "tp": round(tp, 8),
            "rr": round(rr, 6),
            "ols_slope": ols["slope"],
            "ols_r2": ols["r2"],
            "ols_tstat": ols["tstat"],
            "kalman_velocity": kal["velocity"],
            "kalman_z": kal["z"],
            "ou_beta": ou["beta"],
            "ou_tstat": ou["tstat"],
            "ou_half_life": ou["half_life"],
            "hurst": hurst,
            "quant_pro_hurst_filter": _hurst_filter_payload(hurst, self.settings, applicable=regime == "TREND"),
            "ewma_vol": ewma,
            "reason": f"QUANT_PRO_{regime}_{direction}",
            "no_lookahead": True,
        }

    def _wait(
        self,
        symbol: str,
        reason: str,
        ols: dict | None = None,
        kal: dict | None = None,
        ou: dict | None = None,
        hurst: float | None = None,
        ewma: float | None = None,
        regime: str = "FLAT",
        hurst_filter: dict | None = None,
        no_lookahead: bool = True,
    ) -> dict:
        ols = ols or {"slope": None, "r2": None, "tstat": None}
        kal = kal or {"velocity": None, "z": None}
        ou = ou or {"beta": None, "tstat": None, "half_life": None}
        return {
            "strategy": STRATEGY,
            "role": "ENTRY_STRATEGY",
            "regime": regime,
            "direction": "WAIT",
            "signal": "WAIT",
            "score": 0,
            "grade": "D",
            "entry": None,
            "sl": None,
            "tp": None,
            "rr": None,
            "ols_slope": ols.get("slope"),
            "ols_r2": ols.get("r2"),
            "ols_tstat": ols.get("tstat"),
            "kalman_velocity": kal.get("velocity"),
            "kalman_z": kal.get("z"),
            "ou_beta": ou.get("beta"),
            "ou_tstat": ou.get("tstat"),
            "ou_half_life": ou.get("half_life"),
            "hurst": hurst,
            "quant_pro_hurst_filter": hurst_filter or _hurst_filter_payload(hurst, self.settings, applicable=regime == "TREND"),
            "ewma_vol": ewma,
            "reason": reason,
            "no_lookahead": no_lookahead,
        }


def ols_regression(values: list[float]) -> dict:
    y = [float(item) for item in values]
    n = len(y)
    x = list(range(n))
    sx = sum(x)
    sy = sum(y)
    sxy = sum(a * b for a, b in zip(x, y))
    sx2 = sum(a * a for a in x)
    denom = n * sx2 - sx * sx
    slope = 0.0 if denom == 0 else (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n if n else 0.0
    pred = [slope * a + intercept for a in x]
    mean = sy / n if n else 0.0
    ss_res = sum((a - b) ** 2 for a, b in zip(y, pred))
    ss_tot = sum((a - mean) ** 2 for a in y)
    r2 = 0.0 if ss_tot == 0 else max(0.0, min(1.0, 1 - ss_res / ss_tot))
    sxx = sx2 - sx * sx / n if n else 0.0
    mse = ss_res / (n - 2) if n > 2 else 0.0
    se = math.sqrt(mse / sxx) if sxx > 0 and mse >= 0 else 0.0
    tstat = slope / se if se > 0 else (math.copysign(999.0, slope) if slope else 0.0)
    return {"slope": slope, "intercept": intercept, "r2": r2, "tstat": tstat}


def kalman_filter(values: list[float], q_level: float, q_velocity: float, r: float) -> dict:
    level = float(values[0])
    velocity = 0.0
    p00, p01, p10, p11 = 1.0, 0.0, 0.0, 1.0
    innovation = 0.0
    variance = max(r, 1e-12)
    for close in values[1:]:
        level_pred = level + velocity
        velocity_pred = velocity
        pp00 = p00 + p01 + p10 + p11 + q_level
        pp01 = p01 + p11
        pp10 = p10 + p11
        pp11 = p11 + q_velocity
        innovation = float(close) - level_pred
        variance = pp00 + r
        k0 = pp00 / variance
        k1 = pp10 / variance
        level = level_pred + k0 * innovation
        velocity = velocity_pred + k1 * innovation
        p00 = (1 - k0) * pp00
        p01 = (1 - k0) * pp01
        p10 = pp10 - k1 * pp00
        p11 = pp11 - k1 * pp01
    z = innovation / math.sqrt(max(variance, 1e-12))
    return {"level": level, "velocity": velocity, "z": z}


def ou_test(values: list[float], tcrit: float) -> dict:
    y = [float(item) for item in values]
    x = y[:-1]
    dy = [y[i + 1] - y[i] for i in range(len(y) - 1)]
    reg = _simple_regression(x, dy)
    beta = reg["slope"]
    theta = -beta
    half_life = math.log(2) / theta if theta > 0 else math.inf
    return {"beta": beta, "alpha": reg["intercept"], "tstat": reg["tstat"], "half_life": half_life}


def hurst_rs(values: list[float]) -> float:
    y = [float(item) for item in values]
    chunks = []
    max_power = int(math.log2(len(y)))
    for power in range(3, max_power + 1):
        size = 2**power
        if size > len(y):
            continue
        segment = y[-size:]
        mean = sum(segment) / size
        cumulative = []
        total = 0.0
        for item in segment:
            total += item - mean
            cumulative.append(total)
        r = max(cumulative) - min(cumulative)
        s = math.sqrt(sum((item - mean) ** 2 for item in segment) / size)
        if r > 0 and s > 0:
            chunks.append((math.log(size), math.log(r / s)))
    if len(chunks) < 2:
        return 0.5
    return max(0.0, min(1.0, _simple_regression([a for a, _ in chunks], [b for _, b in chunks])["slope"]))


def _hurst_filter_payload(hurst: float | None, settings: Settings, applicable: bool = True) -> dict:
    enabled = bool(getattr(settings, "quant_pro_hurst_filter_enabled", True))
    threshold = float(getattr(settings, "quant_pro_min_trend_hurst", 0.90))
    value = _finite_float(hurst)
    if not applicable or not enabled:
        return {
            "enabled": enabled,
            "hurst": value,
            "min_trend_hurst": threshold,
            "trend_strength": "UNKNOWN" if value is None else ("STRONG_TREND" if value >= threshold else "WEAK_TREND"),
            "passed": True,
            "block_reason": None,
        }
    if value is None:
        return {
            "enabled": enabled,
            "hurst": None,
            "min_trend_hurst": threshold,
            "trend_strength": "UNKNOWN",
            "passed": False,
            "block_reason": "QUANT_PRO_HURST_MISSING",
        }
    if value < threshold:
        return {
            "enabled": enabled,
            "hurst": value,
            "min_trend_hurst": threshold,
            "trend_strength": "WEAK_TREND",
            "passed": False,
            "block_reason": "QUANT_PRO_HURST_TREND_TOO_WEAK",
        }
    return {
        "enabled": enabled,
        "hurst": value,
        "min_trend_hurst": threshold,
        "trend_strength": "STRONG_TREND",
        "passed": True,
        "block_reason": None,
    }


def _log_hurst(symbol: str, payload: dict) -> None:
    hurst = payload.get("hurst")
    threshold = payload.get("min_trend_hurst")
    if payload.get("passed"):
        log.info(
            "[QUANT_PRO_HURST] symbol=%s hurst=%s threshold=%.2f status=PASS trend_strength=%s",
            symbol,
            "null" if hurst is None else round(float(hurst), 6),
            float(threshold),
            payload.get("trend_strength"),
        )
        return
    if hurst is None:
        log.info("[QUANT_PRO_HURST] symbol=%s hurst=null status=BLOCK reason=%s", symbol, payload.get("block_reason"))
        return
    log.info(
        "[QUANT_PRO_HURST] symbol=%s hurst=%s threshold=%.2f status=BLOCK reason=%s",
        symbol,
        round(float(hurst), 6),
        float(threshold),
        payload.get("block_reason"),
    )


def _finite_float(value: float | None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def ewma_volatility(values: list[float], lam: float) -> float:
    returns = []
    for prev, cur in zip(values, values[1:]):
        if prev == 0:
            continue
        returns.append((cur - prev) / prev)
    if not returns:
        return 0.0
    var = returns[0] * returns[0]
    for item in returns[1:]:
        var = lam * var + (1 - lam) * item * item
    return math.sqrt(max(var, 0.0))


def reward_risk(direction: str, entry: float, sl: float, tp: float) -> float | None:
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 0 or reward <= 0:
        return None
    if direction == "BUY" and not (sl < entry < tp):
        return None
    if direction == "SELL" and not (tp < entry < sl):
        return None
    rr = reward / risk
    return rr if math.isfinite(rr) else None


def score_regime(regime: str, direction: str, ols: dict, kal: dict, ou: dict, hurst: float, ewma: float, rr: float | None, settings: Settings) -> int:
    score = 0
    if regime == "TREND":
        if abs(ols["tstat"]) >= settings.hermes_quant_pro_tcrit:
            score += 25
        if (direction == "BUY" and ols["slope"] > 0) or (direction == "SELL" and ols["slope"] < 0):
            score += 20
        if (direction == "BUY" and kal["velocity"] > 0) or (direction == "SELL" and kal["velocity"] < 0):
            score += 20
        if hurst >= settings.hermes_quant_pro_hurst_trend:
            score += 15
        if ewma > 0:
            score += 10
        if rr is not None and rr >= settings.hermes_quant_pro_min_rr:
            score += 10
    elif regime == "MEAN_REVERSION":
        valid_ou = ou["beta"] < 0 and ou["tstat"] < -settings.hermes_quant_pro_tcrit
        if valid_ou:
            score += 25
        if settings.hermes_quant_pro_hl_min <= ou["half_life"] <= settings.hermes_quant_pro_hl_max:
            score += 20
        if (direction == "BUY" and kal["z"] < -settings.hermes_quant_pro_z_entry) or (direction == "SELL" and kal["z"] > settings.hermes_quant_pro_z_entry):
            score += 25
        if ewma > 0:
            score += 10
        if rr is not None and rr >= settings.hermes_quant_pro_min_rr:
            score += 10
        if direction in {"BUY", "SELL"}:
            score += 10
    return max(0, min(100, score))


def grade_for(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    return "D"


def _simple_regression(x: list[float], y: list[float]) -> dict:
    n = len(x)
    sx = sum(x)
    sy = sum(y)
    sxy = sum(a * b for a, b in zip(x, y))
    sx2 = sum(a * a for a in x)
    denom = n * sx2 - sx * sx
    slope = 0.0 if denom == 0 else (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n if n else 0.0
    pred = [slope * a + intercept for a in x]
    ss_res = sum((a - b) ** 2 for a, b in zip(y, pred))
    sxx = sx2 - sx * sx / n if n else 0.0
    mse = ss_res / (n - 2) if n > 2 else 0.0
    se = math.sqrt(mse / sxx) if sxx > 0 and mse > 0 else 0.0
    tstat = slope / se if se > 0 else (math.copysign(999.0, slope) if slope else 0.0)
    return {"slope": slope, "intercept": intercept, "tstat": tstat}


def _closed_frame(value: Any) -> pd.DataFrame:
    if value is None:
        return pd.DataFrame()
    df = value.copy() if isinstance(value, pd.DataFrame) else pd.DataFrame(value)
    if df.empty or "close" not in df:
        return pd.DataFrame()
    return df.iloc[:-1].copy() if len(df) > 1 else df.copy()
