"""DECISION_DATASET — append-only learning dataset (fail-silent, versioned).

Every routed decision — EXECUTED or REFUSED — is flattened into one JSONL
row in data/decision_dataset.jsonl with:
- schema_version + timestamps,
- every scalar feature of the router event (~200 features: scores and
  components, SMC/MTF flags, of_score, atr, spread_to_atr, net RR, ...),
- ees_sell + band / ees_buy + band, exec quality, account_policy, session,
- regime: h4_bias, atr_percentile (window up to 5000 bars, limited by the
  frames actually available), kill_zone_active,
- momentum_alignment — 3 votes (last 5 CLOSED M1 candles, sign of
  cvd_slope, sign of delta; >=2 agreeing = BULL/BEAR; a missing vote counts
  0 -> NEUTRAL; then ALIGNED / NEUTRAL / AGAINST vs the entry direction).
  PURE SHADOW: zero decisional effect.
- Outcome rows: MFE/MAE tracking + pnl reconciled from MT5 deals for real
  tickets, virtual SL/TP outcome for refused decisions.

Everything is wrapped fail-silent: a dataset failure must never touch the
trading path.
"""
from __future__ import annotations

import json
import math
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.logger import log

SCHEMA_VERSION = 2
DATASET_PATH = Path(__file__).resolve().parent.parent / "data" / "decision_dataset.jsonl"

_KILL_ZONES_UTC = ((7, 9), (12, 14), (1, 3))


# ── momentum alignment (pure shadow feature) ────────────────────────────────

def momentum_alignment(direction: object, m1_rows: list[dict] | None, cvd_slope: object, delta: object) -> dict:
    """3 votes: last 5 CLOSED M1 candles, sign(cvd_slope), sign(delta).
    >=2 agreeing votes -> BULL/BEAR, else NEUTRAL; a missing vote counts 0.
    Then ALIGNED / NEUTRAL / AGAINST versus the entry direction."""
    vote_m1 = 0
    rows = [r for r in (m1_rows or []) if isinstance(r, dict)]
    if len(rows) >= 5:
        closes = [_to_float(r.get("close")) for r in rows[-5:]]
        if all(v is not None for v in closes):
            net = closes[-1] - closes[0]
            vote_m1 = 1 if net > 0 else (-1 if net < 0 else 0)
    cvd = _to_float(cvd_slope)
    vote_cvd = 0 if cvd is None else (1 if cvd > 0 else (-1 if cvd < 0 else 0))
    dlt = _to_float(delta)
    vote_delta = 0 if dlt is None else (1 if dlt > 0 else (-1 if dlt < 0 else 0))

    votes = [vote_m1, vote_cvd, vote_delta]
    bulls = sum(1 for v in votes if v == 1)
    bears = sum(1 for v in votes if v == -1)
    consensus = "BULL" if bulls >= 2 else ("BEAR" if bears >= 2 else "NEUTRAL")

    side = str(direction or "").upper()
    if consensus == "NEUTRAL" or side not in {"BUY", "SELL"}:
        alignment = "NEUTRAL"
    elif (consensus == "BULL") == (side == "BUY"):
        alignment = "ALIGNED"
    else:
        alignment = "AGAINST"
    return {
        "votes": {"m1_last5": vote_m1, "cvd_slope": vote_cvd, "delta": vote_delta},
        "consensus": consensus,
        "alignment": alignment,
    }


# ── regime features ─────────────────────────────────────────────────────────

def atr_percentile(frame: object, period: int = 14, window: int = 5000) -> float | None:
    """Percentile of the current ATR within up to `window` bars of history.
    Limited by the bars actually available (documented limitation)."""
    try:
        if frame is None or getattr(frame, "empty", True) or len(frame) < period + 5:
            return None
        df = frame.tail(window)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = tr1.combine(tr2, max).combine(tr3, max)
        atr_series = tr.rolling(period).mean().dropna()
        if atr_series.empty:
            return None
        current = float(atr_series.iloc[-1])
        rank = float((atr_series <= current).mean())
        return round(rank * 100.0, 1)
    except Exception:
        return None


def kill_zone_active(now_utc: datetime | None = None) -> bool:
    hour = (now_utc or datetime.now(timezone.utc)).hour
    return any(start <= hour < end for start, end in _KILL_ZONES_UTC)


# ── row building ────────────────────────────────────────────────────────────

_NESTED_FLATTEN = (
    "gate_statuses",
    "exec_quality",
    "account_policy_detail",
    "daily_killswitch",
    "time_gate",
    "order_flow",
    "order_flow_execution_agent",
    "ees_components",
    "max_money_tp",
)


def build_decision_row(event: dict, extras: dict | None = None) -> dict:
    now = datetime.now(timezone.utc)
    row: dict = {
        "row_type": "decision",
        "schema_version": SCHEMA_VERSION,
        "recorded_at": now.isoformat(),
    }
    for key, value in (event or {}).items():
        if key in _NESTED_FLATTEN:
            continue
        if _is_scalar(value):
            row[key] = value
    for nested_key in _NESTED_FLATTEN:
        nested = (event or {}).get(nested_key)
        if isinstance(nested, dict):
            for sub_key, sub_value in nested.items():
                if _is_scalar(sub_value):
                    row[f"{nested_key}.{sub_key}"] = sub_value

    extras = extras or {}
    frames = extras.get("frames") if isinstance(extras.get("frames"), dict) else {}
    # regime
    h4 = frames.get("H4") if frames else None
    row["regime.h4_bias"] = event.get("h4_bias") or event.get("smc_h4_direction")
    row["regime.atr_percentile"] = atr_percentile(h4 if h4 is not None else frames.get("M5") if frames else None)
    row["regime.kill_zone_active"] = kill_zone_active(now)
    row["session"] = event.get("session_name") or (event.get("time_gate") or {}).get("session_name") if isinstance(event.get("time_gate"), dict) else event.get("session_name")

    # derived features
    atr_value = _to_float(event.get("atr") or event.get("atr_value"))
    spread = _to_float(event.get("spread") or event.get("spread_at_send_points"))
    row["atr"] = atr_value
    row["spread_to_atr"] = (spread / atr_value) if (spread is not None and atr_value and atr_value > 0) else None
    entry = _to_float(event.get("entry"))
    sl = _to_float(event.get("sl"))
    tp = _to_float(event.get("tp"))
    direction = str(event.get("direction") or "").upper()
    net_rr = None
    if entry is not None and sl is not None and tp is not None:
        risk = (entry - sl) if direction == "BUY" else (sl - entry)
        reward = (tp - entry) if direction == "BUY" else (entry - tp)
        if risk and risk > 0:
            net_rr = round(reward / risk, 3)
    row["net_rr"] = net_rr

    # momentum alignment — PURE SHADOW
    of_payload = event.get("order_flow_execution_agent") if isinstance(event.get("order_flow_execution_agent"), dict) else {}
    m1_rows = extras.get("m1_rows")
    if m1_rows is None and frames:
        m1_frame = frames.get("M1")
        try:
            m1_rows = m1_frame.iloc[:-1].to_dict("records") if m1_frame is not None and not m1_frame.empty else None
        except Exception:
            m1_rows = None
    momentum = momentum_alignment(
        direction,
        m1_rows,
        of_payload.get("cvd_slope") if of_payload else extras.get("cvd_slope"),
        of_payload.get("delta") if of_payload else extras.get("delta"),
    )
    row["momentum_alignment.vote_m1_last5"] = momentum["votes"]["m1_last5"]
    row["momentum_alignment.vote_cvd_slope"] = momentum["votes"]["cvd_slope"]
    row["momentum_alignment.vote_delta"] = momentum["votes"]["delta"]
    row["momentum_alignment.consensus"] = momentum["consensus"]
    row["momentum_alignment.alignment"] = momentum["alignment"]

    for key, value in extras.items():
        if key in {"frames", "m1_rows"}:
            continue
        if _is_scalar(value):
            row[f"extra.{key}"] = value
    return row


# ── dataset writer + outcome tracker ────────────────────────────────────────

class DecisionDataset:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else DATASET_PATH
        self._lock = threading.Lock()
        self.tracker = OutcomeTracker(self)

    def record_decision(self, event: dict, extras: dict | None = None) -> dict | None:
        """Fail-silent: any error is swallowed (logged debug), trading is
        never touched."""
        try:
            row = build_decision_row(event, extras)
            self._append(row)
            self.tracker.register(row)
            return row
        except Exception as exc:  # fail-silent by contract
            try:
                log.debug("[DECISION_DATASET] record_failed error=%s", str(exc)[:200])
            except Exception:
                pass
            return None

    def record_outcome(self, outcome: dict) -> None:
        try:
            self._append({"row_type": "outcome", "schema_version": SCHEMA_VERSION, **outcome})
        except Exception:
            pass

    def _append(self, row: dict) -> None:
        payload = json.dumps(row, default=str, sort_keys=True)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(payload + "\n")


class OutcomeTracker:
    """MFE/MAE tracking + outcome for every decision.

    - EXECUTED decisions (real ticket): MFE/MAE updated from ticks; on
      close, pnl reconciled from the MT5 deals history.
    - REFUSED decisions: a VIRTUAL position (entry/sl/tp at refusal) is
      simulated on subsequent ticks -> virtual WIN/LOSS outcome.
    Fail-silent everywhere.
    """

    def __init__(self, dataset: "DecisionDataset") -> None:
        self.dataset = dataset
        self._open: dict[str, dict] = {}
        self._counter = 0

    def register(self, row: dict) -> None:
        try:
            direction = str(row.get("direction") or "").upper()
            entry = _to_float(row.get("entry"))
            sl = _to_float(row.get("sl"))
            tp = _to_float(row.get("tp"))
            if direction not in {"BUY", "SELL"} or entry is None or sl is None or tp is None:
                return
            ticket = row.get("ticket")
            executed = bool(row.get("order_success")) and ticket
            self._counter += 1
            key = f"T{ticket}" if executed else f"V{self._counter}"
            self._open[key] = {
                "key": key,
                "virtual": not executed,
                "ticket": ticket if executed else None,
                "symbol": row.get("broker_symbol") or row.get("symbol"),
                "direction": direction,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "mfe": 0.0,
                "mae": 0.0,
                "opened_at": row.get("recorded_at"),
                "setup_id": row.get("setup_id"),
                "reason": row.get("reason"),
            }
        except Exception:
            pass

    def update(self, prices: dict[str, float], deals_fn=None, now_utc: datetime | None = None) -> list[dict]:
        """prices: {symbol: last_price}. Returns closed outcome rows."""
        closed: list[dict] = []
        try:
            for key in list(self._open):
                item = self._open[key]
                price = _to_float(prices.get(str(item.get("symbol") or "")))
                if price is None:
                    continue
                sign = 1.0 if item["direction"] == "BUY" else -1.0
                excursion = sign * (price - item["entry"])
                item["mfe"] = max(item["mfe"], excursion)
                item["mae"] = min(item["mae"], excursion)
                hit_tp = price >= item["tp"] if item["direction"] == "BUY" else price <= item["tp"]
                hit_sl = price <= item["sl"] if item["direction"] == "BUY" else price >= item["sl"]
                if not (hit_tp or hit_sl):
                    continue
                outcome = {
                    **{k: item[k] for k in ("key", "virtual", "ticket", "symbol", "direction", "entry", "sl", "tp", "mfe", "mae", "opened_at", "setup_id", "reason")},
                    "closed_at": (now_utc or datetime.now(timezone.utc)).isoformat(),
                    "outcome": "TP_HIT" if hit_tp else "SL_HIT",
                    "close_price": price,
                }
                if not item["virtual"] and deals_fn is not None:
                    outcome["pnl_reconciled"] = _reconcile_pnl(item["ticket"], deals_fn)
                    outcome["pnl_source"] = "MT5_HISTORY_DEALS" if outcome["pnl_reconciled"] is not None else "UNRESOLVED"
                else:
                    sign_risk = abs(item["entry"] - item["sl"])
                    outcome["virtual_r_multiple"] = round(
                        (sign * (price - item["entry"])) / sign_risk, 3
                    ) if sign_risk > 0 else None
                self.dataset.record_outcome(outcome)
                closed.append(outcome)
                self._open.pop(key, None)
        except Exception:
            pass
        return closed

    def open_count(self) -> int:
        return len(self._open)


def _reconcile_pnl(ticket: object, deals_fn) -> float | None:
    try:
        deals = list(deals_fn(ticket) or [])
        total = 0.0
        found = False
        for deal in deals:
            found = True
            total += _to_float(getattr(deal, "profit", 0)) or 0.0
            total += _to_float(getattr(deal, "commission", 0)) or 0.0
            total += _to_float(getattr(deal, "swap", 0)) or 0.0
        return round(total, 2) if found else None
    except Exception:
        return None


def _is_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None
