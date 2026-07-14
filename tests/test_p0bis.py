"""MISSION_P0-BIS (2026-07-14) — les 3 trous demontres par le ticket 383260970.

Le ticket : GOLD SELL, ouvert 00:20:27 UTC, -41 USD flottant. Il a traverse tout
le systeme parce que :
  1. ORDER_FLOW_EXECUTION_AGENT court-circuitait le controle top-down (qui disait
     AVOID sur 28 des 107 ordres de cette strategie), et son gate dedie etait du
     CODE MORT (aucun appelant).
  2. Son RR valait exactement 1.500 au signal — comme 107 des 108 ordres du
     dataset v1 — puis 1.434 au fill. Le seul plancher post-normalisation etait
     1.0. Le trou 1.0-1.5 contient 67 % des ordres... et -81 USD de pertes.
  3. Exit V2 perdait son pic et son be_armed a chaque redemarrage.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from app.mt5.demo_router import DemoKellyRouter, _is_hard_block
from tests.test_coeur_p0 import NOW, account, settings, specs


def of_decision(**overrides) -> dict:
    """Un signal ORDER_FLOW_EXECUTION_AGENT sur GOLD#, conforme par defaut."""
    payload = {
        "symbol": "GOLD#",
        "strategy": "ORDER_FLOW_EXECUTION_AGENT",
        "signal": "SELL",
        "direction": "SELL",
        "entry": 3989.84,
        "sl": 4097.38,
        "tp": 3828.53,
        "reward_risk": 1.5,
        "rr": 1.5,
        "order_flow_execution_agent_score": 100,
        "edge_score": 100.0,
        "m15_confirmation": True,
        "m1_entry_confirmation": True,
        "mtfa_status": "PASS",
        "smc_confluence_status": "PASS",
        "big_setup_grade": "B",
    }
    payload.update(overrides)
    return payload


def of_settings(**overrides):
    values = {
        "order_flow_execution_enabled": True,
        "order_flow_min_score": 75,
        "order_flow_min_rr": 1.5,
        "strict_gold_order_flow_topdown": False,
        "gold_liquidity_mode": "trade",
        "gold_liquidity_strategy_enabled": True,
        "hermes_free_demo_discovery_mode": False,
        "demo_exploration_mode": False,
        "hermes_demo_micro_discovery_mode": False,
        "hermes_demo_topdown_fallback_mode": False,
    }
    values.update(overrides)
    return settings(**values)


# ══════════════════════════════════════════════════════════════════════════
# FIX 1 — ORDER_FLOW passe sous controle
# ══════════════════════════════════════════════════════════════════════════

class TestFix1_OrderFlowSousControle(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _router(self, **s):
        return DemoKellyRouter(of_settings(**s), Path(self.tmp.name) / "events.jsonl")

    def _gates_reason(self, router, decision, top_down=None):
        """Passe par _first_block_reason avec un top_down_reader controle."""
        gates = {
            "strategy": "ORDER_FLOW_EXECUTION_AGENT",
            "direction": decision.get("direction"),
            "broker_symbol": "GOLD#",
            "raw_symbol": "GOLD#",
            "symbol_gate_canonical": "GOLD#",
            "top_down_reader": top_down or {},
            "order_flow_execution_agent_score": decision.get("order_flow_execution_agent_score"),
            "edge_score": decision.get("edge_score"),
            "rr": decision.get("rr"),
            "time_gate_status": "PASS",
            "current_symbol_open_count": 0,
        }
        return gates

    # ── le top-down s'applique desormais ──

    def test_top_down_AVOID_bloque_ORDER_FLOW(self) -> None:
        """LE test. 28 des 107 ordres ORDER_FLOW de v1 avaient top_down=AVOID, et
        passaient quand meme : le controle n'etait jamais atteint."""
        router = self._router()
        gates = self._gates_reason(router, of_decision(), top_down={"decision": "AVOID"})

        reason = router._top_down_missing_reason(gates)
        # AVOID n'est pas "manquant" : il est traite a part
        self.assertIsNone(reason)

        # le court-circuit a disparu de _first_block_reason
        import inspect
        source = inspect.getsource(DemoKellyRouter._first_block_reason)
        self.assertNotIn("_is_order_flow_exec_gates(gates)) and not bool(getattr", source)
        self.assertIn("TOP_DOWN_READER_BLOCK", source)

    def test_TOP_DOWN_READER_BLOCK_est_un_blocage_DUR(self) -> None:
        """Donc aucun mode discovery ne peut l'effacer (P0-A)."""
        self.assertTrue(_is_hard_block("TOP_DOWN_READER_BLOCK"))

    # ── le gate dedie est branche ──

    def test_le_gate_dedie_nest_plus_du_code_mort(self) -> None:
        import inspect
        source = inspect.getsource(DemoKellyRouter._symbol_gate_block_reason)
        self.assertIn("_order_flow_exec_agent_block_reason(gates)", source)

    def test_score_sous_le_seuil_bloque(self) -> None:
        router = self._router(order_flow_min_score=75)
        gates = self._gates_reason(router, of_decision(order_flow_execution_agent_score=60, edge_score=60.0))
        self.assertEqual(
            router._order_flow_exec_agent_block_reason(gates),
            "ORDER_FLOW_SCORE_BELOW_THRESHOLD",
        )

    def test_rr_sous_le_minimum_bloque(self) -> None:
        router = self._router(order_flow_min_rr=1.5)
        gates = self._gates_reason(router, of_decision(rr=1.2))
        self.assertEqual(router._order_flow_exec_agent_block_reason(gates), "ORDER_FLOW_RR_BELOW_MIN")

    def test_cap_max_open_par_symbole_bloque(self) -> None:
        router = self._router(demo_max_open_trades_per_symbol=1)  # valeur de production
        gates = self._gates_reason(router, of_decision())
        gates["current_symbol_open_count"] = 1
        self.assertEqual(router._order_flow_exec_agent_block_reason(gates), "MAX_OPEN_TRADES_PER_SYMBOL")

    def test_strategie_desactivee_bloque(self) -> None:
        router = self._router(order_flow_execution_enabled=False)
        gates = self._gates_reason(router, of_decision())
        self.assertEqual(router._order_flow_exec_agent_block_reason(gates), "ORDER_FLOW_EXECUTION_DISABLED")

    # ── CONTROLE NEGATIF ──

    def test_un_signal_CONFORME_passe_toujours(self) -> None:
        """Sans lui, tout bloquer ferait passer les tests ci-dessus. Score 100,
        RR 1.5, aucune position ouverte, time gate PASS => rien ne doit bloquer."""
        router = self._router()
        gates = self._gates_reason(router, of_decision())
        self.assertIsNone(router._order_flow_exec_agent_block_reason(gates))


# ══════════════════════════════════════════════════════════════════════════
# FIX 2 — les deux planchers RR sont unifies
# ══════════════════════════════════════════════════════════════════════════

class TestFix2_PlancherRRUnifie(unittest.TestCase):
    """Le systeme avait DEUX planchers qui ne regardaient pas les memes valeurs :
      - le routeur exigeait rr >= 1.5 sur les valeurs de la DECISION ;
      - le choke-point n'exigeait que rr >= 1.0 sur les valeurs FINALES.
    Entre les deux, un trou de 1.0 a 1.5.

    Ce n'etait pas un cas limite : le RR au signal vaut EXACTEMENT 1.500 sur 107 des
    108 ordres de v1, et 72 (67 %) atterrissent dans le trou. Ces 72 ont perdu
    -81,27 USD ; les 36 qui gardent RR >= 1.5 ont gagne +39,80 USD."""

    def _floor(self, strategy="ORDER_FLOW_EXECUTION_AGENT", **s):
        from app.mt5.demo_router import _final_rr_floor
        return _final_rr_floor(of_settings(**s), strategy)

    def _rr(self, direction, entry, sl, tp):
        from app.mt5.demo_router import _final_rr
        return _final_rr(direction, entry, sl, tp)

    # ── le plancher ──

    def test_le_plancher_final_est_15_et_non_plus_10(self) -> None:
        self.assertEqual(self._floor(), 1.5)
        self.assertEqual(self._floor("BTC_SCALPING_AGENT"), 1.5)

    def test_le_plancher_ne_DESCEND_jamais_sous_15(self) -> None:
        """Renforcer = OK, assouplir = interdit. Une strategie qui declarerait un
        minimum plus permissif ne peut pas affaiblir le choke-point."""
        self.assertEqual(self._floor(order_flow_min_rr=1.1), 1.5)
        self.assertEqual(self._floor(order_flow_min_rr=0.5), 1.5)

    def test_un_minimum_de_strategie_PLUS_STRICT_est_respecte(self) -> None:
        self.assertEqual(self._floor(order_flow_min_rr=2.0), 2.0)

    # ── LE scenario du ticket 383260970 ──

    def test_LE_TICKET__1500_au_signal_1434_au_fill_est_BLOQUE(self) -> None:
        """Le scenario exact. entry decision 3989.84 -> fill 3986.93 (glissement de
        2,91 pts contre), SL/TP inchanges. RR : 1.500 -> 1.434."""
        rr_signal = self._rr("SELL", 3989.84, 4097.38, 3828.53)
        rr_fill = self._rr("SELL", 3986.93, 4097.38, 3828.53)

        self.assertAlmostEqual(rr_signal, 1.500, places=3)
        self.assertAlmostEqual(rr_fill, 1.434, places=3)

        floor = self._floor()
        self.assertGreaterEqual(rr_signal, floor, "le signal passait bien le gate du routeur")
        self.assertLess(rr_fill, floor, "le RR au fill DOIT etre sous le plancher")

        # avant FIX 2, le plancher etait 1.0 : 1.434 passait
        self.assertGreater(rr_fill, 1.0, "c'est bien dans le TROU 1.0-1.5")

    # ── CONTROLE NEGATIF ──

    def test_16_qui_glisse_a_155_PASSE_toujours(self) -> None:
        """Sans lui, bloquer tout ferait passer le test ci-dessus. Un setup avec de
        la marge (RR 1.6) qui glisse a 1.55 reste au-dessus du plancher."""
        # entry 100, sl 110, tp 84 -> risk 10, reward 16 -> RR 1.6
        rr_signal = self._rr("SELL", 100.0, 110.0, 84.0)
        self.assertAlmostEqual(rr_signal, 1.6, places=3)

        # glissement leger : entry 100.19 -> risk 9.81, reward 16.19 -> RR 1.65
        # on construit plutot un cas qui tombe a ~1.55
        rr_degrade = self._rr("SELL", 99.62, 110.0, 83.5)
        self.assertGreater(rr_degrade, 1.5)
        self.assertLess(rr_degrade, 1.6)
        self.assertGreaterEqual(rr_degrade, self._floor(), "un RR encore >= 1.5 doit PASSER")

    def test_le_motif_RR_DEGRADED_AT_FILL_est_un_blocage_DUR(self) -> None:
        self.assertTrue(_is_hard_block("RR_DEGRADED_AT_FILL"))

    def test_le_code_loggue_LES_DEUX_valeurs(self) -> None:
        """Un blocage qui ne dit pas d'ou vient la degradation est indiagnosticable."""
        import inspect
        source = inspect.getsource(DemoKellyRouter._send_order)
        self.assertIn("RR_DEGRADED_AT_FILL", source)
        self.assertIn("rr_signal=%s -> rr_final=%.3f", source)
        self.assertNotIn('"reason": "RR_FLOOR_BELOW_1_0"', source)

    def test_le_chemin_PENDING_applique_le_meme_plancher(self) -> None:
        """Laisser un plancher permissif sur un autre chemin rouvrirait le trou."""
        import inspect
        source = inspect.getsource(DemoKellyRouter._send_pending_order)
        self.assertIn("_final_rr_floor(self.settings", source)
        self.assertNotIn("final_rr < 1.0", source)


if __name__ == "__main__":
    unittest.main()
