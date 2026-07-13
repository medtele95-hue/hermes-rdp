"""MISSION_COEUR_P0 (2026-07-14) — reparation du coeur.

Tests de preuve des corrections P0-A a P0-G. Chaque classe correspond a une
correction et prouve le comportement REEL, pas la presence d'une constante.

Contexte (RAPPORT_AUDIT_COEUR_HERMES.md) : sur les 98 trades du dataset v1,
AUCUN n'etait passe par le chemin strict, et 23 ordres avaient ete executes
malgre un `fallback_decision=BLOCK` explicite. Ces 23 trades ont perdu
-31,75 USD quand les 75 autres cumulaient -0,24 USD. Le systeme n'appliquait
pas ses propres decisions.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.config import Settings
from app.mt5.demo_router import (
    SOFT_OVERRIDABLE_BLOCK_REASONS,
    DemoKellyRouter,
    _is_hard_block,
)

NOW = datetime(2026, 6, 1, 10, tzinfo=timezone.utc)


def settings(**overrides) -> Settings:
    values = {
        "paper_trading": False,
        "demo_trading": True,
        "allow_live_trading": False,
        "demo_only": True,
        "demo_pilot_enabled": True,
        "demo_pilot_hours": 240,
        "demo_pilot_started_at": "2026-06-01T00:00:00+00:00",
        "demo_magic_number": 909002,
        "demo_max_lot": 0.01,
        "demo_max_risk_per_trade_pct": 999,
        "demo_max_daily_loss_pct": 999,
        "demo_max_open_trades_total": 21,
        "demo_max_open_trades_per_symbol": 7,
        "demo_max_open_trades_per_symbol_strategy": 7,
        "demo_max_trades_per_day_total": 999,
        "demo_max_trades_per_symbol_per_day": 999,
        "demo_ignore_all_time_blocks": False,
        "demo_ignore_session_blocks": False,
        "demo_ignore_bad_hour_blocks": False,
        "demo_ignore_duration_blocks": False,
        "demo_ignore_setup_wait_hours": False,
        "max_money_tp_enabled": False,
        "hermes_trade_symbols": "BTCUSD#,BTCUSD,GOLD#,GOLD,XAUUSD",
        "hermes_analysis_only_symbols": "EURUSD",
        "hermes_adaptive_confluence_enabled": False,
        "report_timezone": "UTC",
        "timezone_local": "UTC",
        "btc_weekend_analysis_only": False,
        "bad_hour_analysis_only": False,
        "symbol_trade_cooldown_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def account() -> dict:
    return {"login": 1, "trade_mode": 0, "balance": 10000.0, "equity": 10000.0, "currency": "USD"}


def specs() -> dict:
    return {"tick_value": 1.0, "tick_size": 0.01, "volume_step": 0.01, "volume_min": 0.01, "digits": 2}


def btc_decision(**overrides) -> dict:
    payload = {
        "symbol": "BTCUSD#",
        "strategy": "BTC_SCALPING_AGENT",
        "signal": "BUY",
        "entry": 60000.0,
        "sl": 59000.0,
        "tp": 62000.0,
        "reward_risk": 2.0,
        "m15_confirmation": True,
        "m1_entry_confirmation": True,
        "mtfa_status": "PASS",
        "smc_confluence_status": "PASS",
        "big_setup_grade": "B",
    }
    payload.update(overrides)
    return payload


# ══════════════════════════════════════════════════════════════════════════
# P0-A — un override ne peut JAMAIS effacer un blocage dur
# ══════════════════════════════════════════════════════════════════════════

class TestP0A_HardBlocksSurviventAuxOverrides(unittest.TestCase):
    """T1 — `fallback_decision=BLOCK` ⇒ aucun ordre ne part, quel que soit le mode."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.router = DemoKellyRouter(settings(), Path(self.tmp.name) / "events.jsonl")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _evaluate(self, first_reason, fallback="ALLOW", micro="ALLOW", exploration="ALLOW", decision=None):
        """Force _first_block_reason a renvoyer `first_reason`, et fait dire ALLOW a
        TOUS les modes d'override. Si le motif est dur, il doit survivre."""
        allow_review = lambda *a, **k: {  # noqa: E731
            "enabled": True, "decision": fallback, "block_reason": None,
            "block_reasons": [], "warnings": [], "override_reason": None,
        }
        micro_review = lambda *a, **k: {  # noqa: E731
            "enabled": True, "decision": micro, "block_reason": None,
            "block_reasons": [], "warnings": [], "override_reason": None,
        }
        expl_review = lambda *a, **k: {  # noqa: E731
            "enabled": True, "decision": exploration, "block_reason": None,
            "block_reasons": [], "warnings": [], "override_reason": None,
        }
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]), \
             patch.object(DemoKellyRouter, "_first_block_reason", return_value=first_reason), \
             patch.object(DemoKellyRouter, "_demo_topdown_fallback_review", allow_review), \
             patch.object(DemoKellyRouter, "_demo_micro_discovery_review", micro_review), \
             patch.object(DemoKellyRouter, "_exploration_review", expl_review):
            return self.router.evaluate(
                decision or btc_decision(), {"approved_lot": 0.01}, account(), "BTCUSD#",
                {}, {}, specs(), 1.0, 5000.0, True, now=NOW,
            )

    def test_LE_bug_des_23_ordres__top_down_block_nest_plus_efface(self) -> None:
        """LE test. TOP_DOWN_READER_BLOCK + micro-discovery ALLOW = les 23 ordres
        reellement executes en v1, qui ont porte -31,75 USD sur -31,99 USD de perte."""
        result = self._evaluate("TOP_DOWN_READER_BLOCK")
        self.assertEqual(result.reason, "TOP_DOWN_READER_BLOCK")
        self.assertNotEqual(result.decision, "PASS")

    def test_aucun_mode_ne_leve_un_blocage_de_risque(self) -> None:
        for hard in (
            "MAX_SPREAD",
            "MAX_OPEN_TRADES_PER_SYMBOL",
            "MAX_OPEN_TRADES_TOTAL",
            "MAX_TRADES_PER_DAY_TOTAL",
            "SYMBOL_TRADE_COOLDOWN",
            "DEMO_MAX_RISK_PER_TRADE_EXCEEDED",
            "MISSING_SL_TP",
            "INVALID_SL_TP",
            "RR_BELOW_1_5",
            "SAFETY_GUARD_BLOCK",
            "MARKET_CLOSED",
            "DEMO_DAILY_LOSS_STOP",
            "DEMO_CONSECUTIVE_LOSS_STOP",
            "ACCOUNT_NOT_DEMO",
            "ALLOW_LIVE_TRADING_NOT_FALSE",
        ):
            with self.subTest(hard=hard):
                result = self._evaluate(hard)
                self.assertEqual(result.reason, hard, f"{hard} a ete efface par un override")

    def test_old_btc_mode_ne_leve_plus_un_blocage_dur(self) -> None:
        """old_btc_mode faisait `reason = None` INCONDITIONNEL — il effacait ~30 gates
        alors que son commentaire n'annoncait que SMC/MTFA/confluence."""
        result = self._evaluate(
            "MAX_SPREAD",
            decision=btc_decision(old_btc_mode="LOVABLE_BTC_OLD_SYSTEM"),
        )
        self.assertEqual(result.reason, "MAX_SPREAD")

    # ── CONTROLE NEGATIF : sans lui, ce fix "passerait" en bloquant tout ──

    def test_un_motif_SOFT_reste_bien_assouplissable(self) -> None:
        """Garde-fou anti-surcorrection. Si ce test echoue, le bot ne trade plus du
        tout et le correctif est un faux succes : bloquer 100 % des ordres ferait
        passer tous les tests ci-dessus."""
        result = self._evaluate("ADAPTIVE_CONFLUENCE_TOO_LOW")
        self.assertEqual(result.decision, "PASS")

    def test_les_confirmations_de_setup_restent_assouplissables(self) -> None:
        for soft in ("MTFA_FAIL", "SMC_FAIL", "M15_CONFIRMATION_FALSE", "M1_CONFIRMATION_FALSE"):
            with self.subTest(soft=soft):
                result = self._evaluate(soft)
                self.assertEqual(result.decision, "PASS", f"{soft} devrait rester assouplissable")


class TestP0A_ClassificationDesMotifs(unittest.TestCase):
    def test_defaut_dur__un_motif_inconnu_est_dur(self) -> None:
        """Le point clef du design : un motif de blocage AJOUTE PLUS TARD au routeur
        est dur par defaut. On veut se tromper du cote strict."""
        self.assertTrue(_is_hard_block("UN_NOUVEAU_GATE_INVENTE_DEMAIN"))

    def test_absence_de_blocage_nest_pas_un_blocage_dur(self) -> None:
        self.assertFalse(_is_hard_block(None))
        self.assertFalse(_is_hard_block(""))

    def test_la_liste_soft_ne_contient_aucun_motif_de_risque(self) -> None:
        """Verrou de revue : si quelqu'un ajoute un jour un motif de RISQUE a la liste
        soft, ce test le refuse."""
        interdits = {
            "MAX_SPREAD", "MAX_OPEN_TRADES_PER_SYMBOL", "MAX_OPEN_TRADES_TOTAL",
            "MAX_TRADES_PER_DAY_TOTAL", "SYMBOL_TRADE_COOLDOWN", "MISSING_SL_TP",
            "INVALID_SL_TP", "RR_BELOW_1_5", "SAFETY_GUARD_BLOCK", "MARKET_CLOSED",
            "DEMO_MAX_RISK_PER_TRADE_EXCEEDED", "DEMO_DAILY_LOSS_STOP",
            "DEMO_CONSECUTIVE_LOSS_STOP", "TOP_DOWN_READER_BLOCK",
            "ACCOUNT_NOT_DEMO", "ALLOW_LIVE_TRADING_NOT_FALSE",
        }
        self.assertEqual(SOFT_OVERRIDABLE_BLOCK_REASONS & interdits, frozenset())


if __name__ == "__main__":
    unittest.main()
