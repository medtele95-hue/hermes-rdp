"""BTC Self-Diagnosis — detects repeating loss patterns and manages temp entry blocks.

Reads trade records (with pattern flags) fed from BtcPerformanceMemory trade history.
Activates a timed temp block when a pattern appears >= 2 times in recent losses.

No MT5 calls. No order execution. Pure diagnostic/advisory module.
Integration point: btc_entry_gate.py logs active blocks (advisory only, not hard veto).

Singleton: use get_btc_self_diagnosis() to get the shared instance.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from app.logger import log

# ---------------------------------------------------------------------------
# Pattern taxonomy — 9 named patterns with TTL and trade record flag names
# ---------------------------------------------------------------------------

PATTERN_TAXONOMY: dict[str, dict] = {
    "LATE_AFTER_IMPULSE": {
        "description": "Entry taken too late after a strong impulsive move",
        "ttl_minutes": 45,
        "trade_flag": "late_entry",
    },
    "AGAINST_VWAP": {
        "description": "Entry direction opposes the current VWAP context",
        "ttl_minutes": 60,
        "trade_flag": "against_vwap",
    },
    "AGAINST_POC": {
        "description": "Entry taken against the Point of Control (POC) level",
        "ttl_minutes": 45,
        "trade_flag": "against_poc",
    },
    "WEAK_CVD": {
        "description": "Entry taken with weak or opposing Cumulative Volume Delta",
        "ttl_minutes": 30,
        "trade_flag": "weak_cvd",
    },
    "HIGH_SPREAD": {
        "description": "Entry taken when spread exceeded acceptable threshold",
        "ttl_minutes": 30,
        "trade_flag": "high_spread",
    },
    "SMC_MTFA_STRONG_FAIL": {
        "description": "Strong SMC/MTFA confluence was present but trade lost",
        "ttl_minutes": 60,
        "trade_flag": "smc_mtfa_hard_fail",
    },
    "BEFORE_REVERSAL_CONFIRM": {
        "description": "Entry taken before reversal structure was confirmed",
        "ttl_minutes": 45,
        "trade_flag": "before_reversal",
    },
    "BAD_SESSION": {
        "description": "Entry taken during a low-liquidity or off-hours session",
        "ttl_minutes": 120,
        "trade_flag": "bad_session",
    },
    "NARRATIVE_BLOCK_IGNORED": {
        "description": "Trade executed despite a narrative blocking verdict (TRAPPING/DANGEROUS/EXHAUSTED)",
        "ttl_minutes": 90,
        "trade_flag": "narrative_block_ignored",
    },
}

_PATTERN_THRESHOLD: int = 2      # occurrences in recent losses before block fires
_RECENT_LOSS_WINDOW: int = 20    # look at last N losses when counting patterns


class BtcSelfDiagnosis:
    """Detects repeating loss patterns and activates timed advisory temp blocks.

    Feed recent loss records via diagnose_losses().
    Query advisory blocks via get_active_blocks() or is_blocked().

    Thread-safe. No MT5 calls. All blocks are advisory (logged in btc_entry_gate,
    not enforced as hard veto).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._temp_blocks: dict[str, float] = {}   # pattern_name → expire_ts (unix)

    # ─── Public diagnostic API ───────────────────────────────────────────────

    def diagnose_losses(self, recent_losses: list[dict]) -> list[str]:
        """Scan recent_losses for repeating patterns; activate temp block when >= 2 found.

        Args:
            recent_losses: list of closed-trade dicts (last N losses, newest last).
                           Each dict may carry boolean pattern flags matching trade_flag
                           keys in PATTERN_TAXONOMY.

        Returns:
            List of pattern names for which a temp block was just activated or refreshed.
        """
        window = recent_losses[-_RECENT_LOSS_WINDOW:]
        activated: list[str] = []

        with self._lock:
            for name, spec in PATTERN_TAXONOMY.items():
                flag = spec["trade_flag"]
                count = sum(1 for t in window if bool(t.get(flag)))
                if count >= _PATTERN_THRESHOLD:
                    ttl_m = spec["ttl_minutes"]
                    expire_ts = time.time() + ttl_m * 60
                    self._temp_blocks[name] = expire_ts
                    activated.append(name)
                    log.info(
                        "[BTC_SELF_DIAGNOSIS] pattern=%s action=TEMP_BLOCK "
                        "ttl_minutes=%s occurrences=%s (advisory only)",
                        name, ttl_m, count,
                    )

        return activated

    def get_active_blocks(self) -> dict[str, float]:
        """Return currently active advisory blocks {pattern_name: expire_ts}.

        Expired blocks are pruned on each call.
        """
        with self._lock:
            now = time.time()
            active = {p: ts for p, ts in self._temp_blocks.items() if now < ts}
            self._temp_blocks = dict(active)
            return dict(active)

    def get_active_block_names(self) -> list[str]:
        """Convenience: return just the names of active advisory blocks."""
        return list(self.get_active_blocks().keys())

    def is_blocked(self, pattern: str) -> bool:
        """Return True if the named pattern has an active advisory block."""
        with self._lock:
            expire = self._temp_blocks.get(pattern, 0.0)
            if time.time() < expire:
                return True
            self._temp_blocks.pop(pattern, None)
            return False

    def get_ttl_remaining_minutes(self, pattern: str) -> float:
        """Return remaining block duration in minutes (0.0 if not blocked)."""
        with self._lock:
            expire = self._temp_blocks.get(pattern, 0.0)
            remaining = expire - time.time()
            return round(max(0.0, remaining / 60.0), 2)

    # ─── Testing helper ──────────────────────────────────────────────────────

    def _raw_set_block(self, pattern: str, expire_ts: float) -> None:
        """Testing only: directly set a block with an explicit expiry timestamp."""
        with self._lock:
            self._temp_blocks[pattern] = expire_ts

    def _clear_all_blocks(self) -> None:
        """Testing only: remove all temp blocks."""
        with self._lock:
            self._temp_blocks.clear()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_instance: Optional[BtcSelfDiagnosis] = None
_instance_lock = threading.Lock()


def get_btc_self_diagnosis() -> BtcSelfDiagnosis:
    """Return the shared BtcSelfDiagnosis singleton."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = BtcSelfDiagnosis()
    return _instance


# ---------------------------------------------------------------------------
# Integration helper — call from btc_entry_gate for advisory logging
# ---------------------------------------------------------------------------

def log_active_diagnosis_blocks(symbol: str, strategy: str) -> None:
    """Log any active advisory diagnosis blocks. Does NOT block execution."""
    diag = get_btc_self_diagnosis()
    active = diag.get_active_block_names()
    if active:
        log.info(
            "[BTC_SELF_DIAGNOSIS] advisory_blocks_active=%s symbol=%s strategy=%s "
            "(logging only — no hard veto applied)",
            active, symbol, strategy,
        )
