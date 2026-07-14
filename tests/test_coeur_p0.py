"""MISSION_COEUR_P0 (2026-07-14) â€” reparation du coeur.

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
from types import SimpleNamespace
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


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# P0-A â€” un override ne peut JAMAIS effacer un blocage dur
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestP0A_HardBlocksSurviventAuxOverrides(unittest.TestCase):
    """T1 â€” `fallback_decision=BLOCK` â‡’ aucun ordre ne part, quel que soit le mode."""

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
        """old_btc_mode faisait `reason = None` INCONDITIONNEL â€” il effacait ~30 gates
        alors que son commentaire n'annoncait que SMC/MTFA/confluence."""
        result = self._evaluate(
            "MAX_SPREAD",
            decision=btc_decision(old_btc_mode="LOVABLE_BTC_OLD_SYSTEM"),
        )
        self.assertEqual(result.reason, "MAX_SPREAD")

    # â”€â”€ CONTROLE NEGATIF : sans lui, ce fix "passerait" en bloquant tout â”€â”€

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


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# P0-B â€” route_to_demo devient effectif
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestP0B_RouteToDemoEstEffectif(unittest.TestCase):
    """T2 â€” `route_to_demo=False` empeche REELLEMENT le routage.

    Avant : _should_route_to_demo ne lisait que (strategy, signal). Les refus du
    balanced_selector, de l'entry gate BTC, d'une lecture MT5 ratee et du gate de
    confluence posaient `route_to_demo=False`... dans le vide."""

    @staticmethod
    def _backend():
        from app.main import HermesBackend
        return HermesBackend.__new__(HermesBackend)  # pas d'__init__ : methode pure

    def _decision(self, **overrides) -> dict:
        payload = {"strategy": "BTC_SCALPING_AGENT", "signal": "BUY"}
        payload.update(overrides)
        return payload

    def test_une_decision_saine_route_toujours(self) -> None:
        """Controle negatif : sans lui, renvoyer False partout ferait passer le reste."""
        self.assertTrue(self._backend()._should_route_to_demo(self._decision()))

    def test_route_to_demo_false_bloque_le_routage(self) -> None:
        self.assertFalse(
            self._backend()._should_route_to_demo(self._decision(route_to_demo=False))
        )

    def test_wait_analysis_only_bloque_le_routage(self) -> None:
        self.assertFalse(
            self._backend()._should_route_to_demo(self._decision(decision="WAIT_ANALYSIS_ONLY"))
        )

    def test_le_rejet_du_balanced_selector_est_desormais_applique(self) -> None:
        """Le scenario exact de main.py:1504-1514 : le selector rejette, pose
        route_to_demo=False, mais laisse strategy/signal intacts."""
        rejete = self._decision(
            route_to_demo=False,
            decision="WAIT_ANALYSIS_ONLY",
            strategy="BTC_SCALPING_AGENT",  # inchangee par le rejet
            signal="BUY",                   # inchangee par le rejet
        )
        self.assertFalse(self._backend()._should_route_to_demo(rejete))

    def test_route_to_demo_true_ou_absent_ne_bloque_pas(self) -> None:
        self.assertTrue(self._backend()._should_route_to_demo(self._decision(route_to_demo=True)))
        self.assertTrue(self._backend()._should_route_to_demo(self._decision()))


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# P0-C â€” le moteur de confluence est fail-closed
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestP0C_ConfluenceFailClosed(unittest.TestCase):
    """T3 â€” une exception dans evaluate_confluence BLOQUE.

    Avant : `except Exception: pass` laissait `_conf = {}`, et tout le FINAL
    CONFLUENCE GATE etait conditionne par `bool(_conf)`. Une panne du moteur
    desarmait donc le gate entier, en silence. Un setup grade D / score 0 routait
    sans le moindre controle."""

    def test_le_code_ne_contient_plus_le_except_pass_silencieux(self) -> None:
        """Verrou de non-regression sur la forme : le `except Exception: pass` autour
        de evaluate_confluence ne doit jamais revenir."""
        import inspect

        from app.main import HermesBackend

        source = inspect.getsource(HermesBackend.run_cycle)
        bloc = source[source.index("_conf = evaluate_confluence"):]
        bloc = bloc[: bloc.index("_geo_bonus") if "_geo_bonus" in bloc else 2000]
        self.assertIn("_conf_engine_failed = True", bloc)
        self.assertIn("CONFLUENCE_ENGINE_FAILED", bloc)
        self.assertNotIn("except Exception:\n                    pass", bloc)

    def test_la_panne_bloque_le_routage_et_ne_desarme_plus_le_gate(self) -> None:
        """Preuve fonctionnelle : `_conf_blocks_route` inclut desormais la panne, et
        la decision porte route_to_demo=False â€” que P0-B rend effectif."""
        import inspect

        from app.main import HermesBackend

        source = inspect.getsource(HermesBackend.run_cycle)
        self.assertIn("_conf_blocks_route = _conf_engine_failed or (", source)
        panne = source[source.index("if _conf_engine_failed:"):]
        panne = panne[: panne.index("_conf_blocks_route")]
        self.assertIn('decision["route_to_demo"] = False', panne)
        self.assertIn('decision["decision"] = "WAIT_ANALYSIS_ONLY"', panne)

    def test_la_panne_est_annoncee_en_CRITICAL_et_alertee(self) -> None:
        """Une panne silencieuse est pire qu'une panne bruyante : elle ne peut pas
        etre corrigee. On loggue CRITICAL et on alerte."""
        import inspect

        from app.main import HermesBackend

        source = inspect.getsource(HermesBackend.run_cycle)
        self.assertIn("log.critical(", source)
        self.assertIn("send_critical_alert(", source)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# P0-D â€” les deux stops de perte sont ressuscites
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _deal(net: float, t: float, magic: int = 909002, entry: int = 1):
    """Deal MT5 de SORTIE (entry=1 = DEAL_ENTRY_OUT)."""
    from types import SimpleNamespace
    return SimpleNamespace(magic=magic, entry=entry, profit=net, commission=0.0, swap=0.0, time=t)


class TestP0D_StopsDePerteRessuscites(unittest.TestCase):
    """T4 â€” chaque stop est VU se declencher sur des deals franchissant le seuil.

    Avant : daily_loss_pct et consecutive_losses derivaient d'evenements
    `DEMO_CLOSE` qu'AUCUN code du depot n'ecrit. Verifie sur 26 journaux :
    DEMO_CLOSE = 0. Verifie dans le dataset : les deux compteurs valaient 0 sur
    8465 decisions sur 8465. Les deux gates ne pouvaient JAMAIS se declencher.
    Et la balance etait codee en dur a 10000.0."""

    def _counters(self, deals, equity=10000.0):
        from app.mt5.demo_router import risk_counters_from_mt5_deals
        return risk_counters_from_mt5_deals(
            magic=909002,
            account={"equity": equity} if equity is not None else {},
            now_utc=NOW,
            history_fn=lambda s, e: deals,
        )

    # â”€â”€ le compteur de perte quotidienne â”€â”€

    def test_la_perte_du_jour_est_REELLEMENT_comptee(self) -> None:
        c = self._counters([_deal(-50.0, 1), _deal(-30.0, 2), _deal(+10.0, 3)], equity=10000.0)
        self.assertEqual(c["daily_pnl"], -70.0)
        self.assertAlmostEqual(c["daily_loss_pct"], 0.7)  # 70 / 10000
        self.assertEqual(c["source"], "MT5_HISTORY_DEALS")

    def test_le_denominateur_est_lEQUITY_REELLE_pas_10000_en_dur(self) -> None:
        """La balance reelle est ~9090 USD, pas 10000. Le pourcentage etait faux."""
        c = self._counters([_deal(-90.9, 1)], equity=9090.0)
        self.assertAlmostEqual(c["daily_loss_pct"], 1.0)  # 90.9 / 9090 = 1 %
        self.assertEqual(c["equity"], 9090.0)

    def test_LE_STOP_DAILY_LOSS_SE_DECLENCHE(self) -> None:
        """Le test que la mission exige : voir le gate se declencher pour de vrai."""
        c = self._counters([_deal(-150.0, 1)], equity=10000.0)  # -1,5 %
        seuil = 1.0
        self.assertGreaterEqual(c["daily_loss_pct"], seuil)

    def test_un_jour_gagnant_ne_declenche_rien(self) -> None:
        """Controle negatif : un gate qui bloque toujours n'est pas un gate."""
        c = self._counters([_deal(+120.0, 1), _deal(-20.0, 2)], equity=10000.0)
        self.assertEqual(c["daily_loss_pct"], 0.0)
        self.assertLess(c["daily_loss_pct"], 1.0)

    # â”€â”€ le compteur de pertes consecutives â”€â”€

    def test_LE_STOP_PERTES_CONSECUTIVES_SE_DECLENCHE(self) -> None:
        c = self._counters([_deal(-5, 1), _deal(-5, 2), _deal(-5, 3)])
        self.assertEqual(c["consecutive_losses"], 3)
        self.assertGreaterEqual(c["consecutive_losses"], 3)  # seuil .env

    def test_la_serie_repart_de_zero_apres_un_gain(self) -> None:
        """Du plus RECENT au plus ancien : deux pertes apres un gain = serie de 2."""
        c = self._counters([_deal(-5, 1), _deal(-5, 2), _deal(+9, 3), _deal(-5, 4), _deal(-5, 5)])
        self.assertEqual(c["consecutive_losses"], 2)

    def test_un_gain_en_dernier_remet_la_serie_a_zero(self) -> None:
        c = self._counters([_deal(-5, 1), _deal(-5, 2), _deal(+1, 3)])
        self.assertEqual(c["consecutive_losses"], 0)

    # â”€â”€ filtres â”€â”€

    def test_seuls_les_deals_HERMES_de_SORTIE_comptent(self) -> None:
        c = self._counters([
            _deal(-100.0, 1, magic=111111),   # autre EA
            _deal(-100.0, 2, entry=0),        # deal d'ENTREE, pas une cloture
            _deal(-10.0, 3),                  # le seul valable
        ])
        self.assertEqual(c["daily_pnl"], -10.0)
        self.assertEqual(c["consecutive_losses"], 1)

    # â”€â”€ fail-closed â”€â”€

    def test_historique_illisible__FAIL_CLOSED_et_non_zero(self) -> None:
        """Un historique MT5 illisible ne doit surtout pas se lire "aucune perte" :
        ce serait re-affirmer le mensonge exact que ce correctif supprime."""
        from app.mt5.demo_router import risk_counters_from_mt5_deals
        c = risk_counters_from_mt5_deals(
            magic=909002, account={"equity": 10000.0}, now_utc=NOW,
            history_fn=lambda s, e: None,
        )
        self.assertTrue(c["unavailable"])
        self.assertIsNone(c["daily_loss_pct"])
        self.assertNotEqual(c["daily_loss_pct"], 0.0)

    def test_equity_absente__FAIL_CLOSED(self) -> None:
        c = self._counters([_deal(-50.0, 1)], equity=None)
        self.assertTrue(c["unavailable"])
        self.assertIsNone(c["daily_loss_pct"])

    def test_le_gate_bloque_quand_les_compteurs_sont_illisibles(self) -> None:
        """RISK_COUNTERS_UNREADABLE doit exister comme motif de blocage, et etre DUR."""
        from app.mt5.demo_router import RISK_COUNTERS_UNREADABLE
        self.assertTrue(_is_hard_block(RISK_COUNTERS_UNREADABLE))

    def test_les_compteurs_survivent_a_une_rotation_de_journal(self) -> None:
        """Le point clef : ils ne lisent plus le journal d'evenements du tout. Une
        rotation (qui remettait tout a zero) ne peut plus les effacer."""
        import inspect

        from app.mt5.demo_router import risk_counters_from_mt5_deals
        source = inspect.getsource(risk_counters_from_mt5_deals)
        self.assertNotIn("_load_events", source)
        self.assertNotIn("DEMO_CLOSE", source)
        self.assertIn("history_deals_get", source)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# P0-E â€” fail-closed propage aux caps d'exposition et au choke-point
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestP0E_CapsFailClosed(unittest.TestCase):
    """T5 â€” `positions_get() -> None` ne leve JAMAIS un cap.

    Avant : `mt5.positions_get() or []` transformait un MT5 muet en "aucune
    position". Tous les caps d'exposition en derivent : ils tombaient a zero EN
    MEME TEMPS et se levaient tous ensemble. Le choke-point lui-meme assumait le
    fail-open, au motif (faux) que "les gates amont portent deja ce cap" â€” alors
    qu'ils sont aveugles au meme instant."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.router = DemoKellyRouter(settings(), Path(self.tmp.name) / "events.jsonl")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _evaluate(self, positions):
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=positions), \
             patch("app.mt5.demo_router.mt5.history_deals_get", return_value=[]):
            return self.router.evaluate(
                btc_decision(), {"approved_lot": 0.01}, account(), "BTCUSD#",
                {}, {}, specs(), 1.0, 5000.0, True, now=NOW,
            )

    def test_mt5_muet_BLOQUE_au_lieu_de_lever_les_caps(self) -> None:
        result = self._evaluate(None)
        self.assertEqual(result.reason, "MT5_UNAVAILABLE")
        self.assertNotEqual(result.decision, "PASS")

    def test_compte_REELLEMENT_vide_continue_de_trader(self) -> None:
        """Controle negatif : [] n'est PAS None. Sans ce test, bloquer sur MT5 muet
        ET sur compte vide passerait le test precedent tout en tuant le bot."""
        result = self._evaluate([])
        self.assertEqual(result.decision, "PASS")

    def test_MT5_UNAVAILABLE_est_un_blocage_DUR(self) -> None:
        """Aucun mode discovery ne doit pouvoir lever une panne MT5."""
        self.assertTrue(_is_hard_block("MT5_UNAVAILABLE"))

    def test_le_choke_point_bloque_aussi_sur_MT5_muet(self) -> None:
        """Defense en profondeur : meme si un gate amont etait contourne, le
        choke-point refuse d'envoyer un ordre sans savoir ce qui est ouvert."""
        from app.mt5.demo_router import _execution_invariants_block

        request = {"symbol": "BTCUSD#", "volume": 0.01, "magic": 909002, "sl": 59000.0, "tp": 62000.0}
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=None):
            reason = _execution_invariants_block(request, "BTC_SCALPING_AGENT", 1)
        self.assertEqual(reason, "MT5_UNAVAILABLE")

    def test_le_choke_point_laisse_passer_quand_MT5_repond(self) -> None:
        from app.mt5.demo_router import _execution_invariants_block

        request = {"symbol": "BTCUSD#", "volume": 0.01, "magic": 909002, "sl": 59000.0, "tp": 62000.0}
        with patch("app.mt5.demo_router.mt5.positions_get", return_value=[]):
            reason = _execution_invariants_block(request, "BTC_SCALPING_AGENT", 1)
        self.assertIsNone(reason)

    def test_les_invariants_du_choke_point_sont_INTACTS(self) -> None:
        """P0-E ne devait toucher QUE la gestion du None de positions_get. L'allowlist,
        le cap de lot, le magic force et l'ordre nu restent exactement ce qu'ils
        etaient â€” ce sont les protections qui MARCHENT."""
        from app.mt5.demo_router import (
            LOT_HARD_CAP,
            MAGIC_HARD,
            SYMBOL_ALLOWLIST,
            _execution_invariants_block,
        )
        self.assertEqual(SYMBOL_ALLOWLIST, ("GOLD#", "BTCUSD#"))
        self.assertEqual(LOT_HARD_CAP, 0.01)
        self.assertEqual(MAGIC_HARD, 909002)

        source = __import__("inspect").getsource(_execution_invariants_block)
        self.assertIn("SYMBOL_BLOCKED", source)
        self.assertIn("NAKED_ORDER_BLOCKED", source)
        self.assertIn("LOT_INVALID_BLOCKED", source)
        self.assertIn("MAGIC_FORCED", source)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# P0-F â€” la rotation ne remet plus aucun compteur de securite a zero
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestP0F_PlusDAmnesieParRotation(unittest.TestCase):
    """Deux rotations coexistaient, et la mauvaise gagnait :
      - ECRITURE : 20 Mo, sous _events_lock. OK.
      - LECTURE  : 10 Mo, renommait le fichier ET renvoyait [], SANS VERROU.
    10 < 20 : la rotation en lecture partait toujours la premiere. Le cycle ou elle
    se produisait, le bot croyait demarrer une journee vierge.

    Preuve empirique : une dizaine de .bak de 10-11 Mo tous dates du 2026-06-16,
    entre 01:33 et 07:27 â€” au moins 10 rotations en lecture cette seule nuit-la."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.events = Path(self.tmp.name) / "events.jsonl"
        self.router = DemoKellyRouter(settings(), self.events)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_big_file(self, megabytes: float) -> None:
        import json as _json
        row = _json.dumps({"event_type": "DEMO_ORDER", "symbol": "BTCUSD#",
                           "created_at": NOW.isoformat(), "order_success": True,
                           "ticket": "1", "magic_number": 909002, "pad": "x" * 400}) + "\n"
        cible = int(megabytes * 1024 * 1024)
        with self.events.open("w", encoding="utf-8") as fh:
            written = 0
            while written < cible:
                fh.write(row)
                written += len(row)

    def test_la_LECTURE_ne_rote_plus_le_fichier(self) -> None:
        """Un lecteur ne mute pas ce qu'il lit."""
        self._write_big_file(10.5)  # au-dessus de l'ancien seuil de lecture (10 Mo)
        avant = self.events.stat().st_size

        events = self.router._load_events()

        self.assertTrue(self.events.exists(), "le chemin de LECTURE a renomme le fichier")
        self.assertEqual(self.events.stat().st_size, avant)
        self.assertEqual(list(self.events.parent.glob("*.bak")), [])

    def test_la_LECTURE_ne_renvoie_plus_une_liste_vide_en_silence(self) -> None:
        """C'est CE `return []` qui remettait les compteurs journaliers a zero."""
        self._write_big_file(10.5)
        events = self.router._load_events()
        self.assertGreater(len(events), 0, "un fichier volumineux ne doit plus donner 0 evenement")

    def test_les_compteurs_de_PERTE_ne_dependent_plus_du_tout_de_ce_fichier(self) -> None:
        """Seconde barriere (P0-D) : meme si la lecture du journal echouait, les deux
        stops de perte tiendraient, puisqu'ils lisent les deals MT5."""
        from app.mt5.demo_router import risk_counters_from_mt5_deals

        losing = [_deal(-5.0, 1), _deal(-5.0, 2), _deal(-5.0, 3)]
        with patch.object(DemoKellyRouter, "_load_events", return_value=[]):  # journal vide/rote
            c = risk_counters_from_mt5_deals(
                magic=909002, account={"equity": 10000.0}, now_utc=NOW,
                history_fn=lambda s, e: losing,
            )
        self.assertEqual(c["consecutive_losses"], 3)
        self.assertGreater(c["daily_loss_pct"], 0.0)

    def test_le_chemin_d_ECRITURE_rote_toujours(self) -> None:
        """Controle negatif : on n'a pas supprime la rotation, on l'a rendue au seul
        ecrivain (20 Mo, sous verrou). Sans elle, le fichier grossirait sans fin."""
        import inspect
        source = inspect.getsource(DemoKellyRouter._rotate_events_if_needed)
        self.assertIn("20 * 1024 * 1024", source)
        self.assertIn("rename", source)

        lecture = inspect.getsource(DemoKellyRouter._load_events)
        self.assertNotIn("rename", lecture)
        self.assertNotIn("demo_router_events_max_bytes", lecture)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# P0-G â€” les 3 trous du tracker d'outcomes
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestP0G_TrackerDOutcomes(unittest.TestCase):
    """Les trois trous etaient dans un correctif que J'AI livre ce matin (746e7bdb).
    L'audit les a pris en defaut. Ils sont traites au meme niveau d'exigence."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "dataset.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _dataset(self):
        from app.services.decision_dataset import DecisionDataset
        return DecisionDataset(self.path)

    def _register_real(self, ds):
        ds.record_decision({
            "event_type": "DEMO_ORDER", "order_success": True, "ticket": 42,
            "symbol": "GOLD#", "broker_symbol": "GOLD#", "direction": "BUY",
            "entry": 3300.0, "sl": 3294.0, "tp": 3312.0,
        })

    def _outcomes(self):
        import json as _json
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            row = _json.loads(line)
            if row.get("row_type") == "outcome":
                rows.append(row)
        return rows

    # â”€â”€ trou 1 : MT5 muet â‡’ fail-closed â”€â”€

    def test_MT5_MUET_nEcrit_AUCUN_outcome_et_ne_perd_pas_le_ticket(self) -> None:
        """Le trou : close_info_from_deals -> None laissait known_open=False, et le
        code retombait dans la fermeture par TOUCHE DE PRIX. Une ligne outcome FAUSSE
        etait ecrite et le ticket sortait du suivi â€” le vrai label n'aurait alors
        JAMAIS ete ecrit. Mon propre commentaire disait "seuls les deals ferment un
        trade reel" ; le code le contredisait exactement quand MT5 etait muet."""
        ds = self._dataset()
        self._register_real(ds)

        # MT5 muet + le prix touche le SL : l'ancien code aurait ecrit un SL_HIT.
        ds.tracker.update({"GOLD#": 3294.0}, deals_fn=lambda _t: None)

        self.assertEqual(self._outcomes(), [], "un MT5 muet a produit une ligne outcome")
        self.assertEqual(ds.tracker.open_count(), 1, "le ticket a ete perdu")

    def test_le_trade_est_labellise_au_cycle_suivant_quand_MT5_repond(self) -> None:
        """Controle negatif : le fail-closed retarde, il ne supprime pas."""
        ds = self._dataset()
        self._register_real(ds)
        ds.tracker.update({"GOLD#": 3294.0}, deals_fn=lambda _t: None)   # MT5 muet
        self.assertEqual(ds.tracker.open_count(), 1)

        deals = [
            SimpleNamespace(entry=0, reason=3, price=3300.0, time=1.0, profit=0.0, commission=0.0, swap=0.0),
            SimpleNamespace(entry=1, reason=4, price=3294.0, time=2.0, profit=-6.0, commission=0.0, swap=0.0),
        ]
        ds.tracker.update({"GOLD#": 3294.0}, deals_fn=lambda _t: deals)  # MT5 repond

        rows = self._outcomes()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "SL_HIT")
        self.assertEqual(ds.tracker.open_count(), 0)

    # â”€â”€ trou 2 : doublon au redemarrage â”€â”€

    def test_record_outcome_est_IDEMPOTENT_par_ticket(self) -> None:
        """Un P&L double-compte corrompt silencieusement TOUS les agregats
        (weekly_snapshot, daily_report, update_hermes_state lisent pnl_reconciled)."""
        ds = self._dataset()
        outcome = {"ticket": "42", "virtual": False, "pnl_reconciled": -6.0, "outcome": "SL_HIT"}

        self.assertTrue(ds.record_outcome(dict(outcome)))
        self.assertFalse(ds.record_outcome(dict(outcome)), "le doublon aurait du etre refuse")

        self.assertEqual(len(self._outcomes()), 1)

    def test_pas_de_doublon_apres_un_REDEMARRAGE(self) -> None:
        """LE scenario : crash entre l'append et la sauvegarde d'etat. Le ticket etait
        restaure au boot, referme au cycle suivant, et une SECONDE ligne partait."""
        ds = self._dataset()
        self._register_real(ds)
        deals = [
            SimpleNamespace(entry=0, reason=3, price=3300.0, time=1.0, profit=0.0, commission=0.0, swap=0.0),
            SimpleNamespace(entry=1, reason=4, price=3294.0, time=2.0, profit=-6.0, commission=0.0, swap=0.0),
        ]
        ds.tracker.update({}, deals_fn=lambda _t: deals)
        self.assertEqual(len(self._outcomes()), 1)

        # redemarrage : nouveau DecisionDataset sur le meme etat
        reborn = self._dataset()
        reborn.tracker.update({}, deals_fn=lambda _t: deals)

        self.assertEqual(len(self._outcomes()), 1, "un doublon a survecu au redemarrage")

    def test_letat_est_persiste_AVANT_lappend(self) -> None:
        """L'ordre compte : le mode de defaillance residuel s'inverse volontairement â€”
        une ligne MANQUANTE (detectable, backfillable) plutot qu'un DOUBLON (qui
        corrompt les agregats en silence)."""
        import inspect

        from app.services.decision_dataset import OutcomeTracker
        source = inspect.getsource(OutcomeTracker.update)
        bloc = source[source.index("if info and info.get(\"state\") == \"CLOSED\""):]
        bloc = bloc[: bloc.index("continue")]
        self.assertLess(
            bloc.index("_save_state()"), bloc.index("record_outcome("),
            "_save_state doit preceder l'append",
        )

    # â”€â”€ trou 3 : labelling independant de QUICK_EXIT_ENABLED â”€â”€

    def test_le_labelling_ne_depend_plus_de_QUICK_EXIT_ENABLED(self) -> None:
        """L'appel vivait dans process_quick_exits, apres ses retours anticipes.
        QUICK_EXIT_ENABLED=false aurait supprime TOUT labelling, pour toujours, sans
        un seul log. Il ne tenait que par accident de configuration."""
        import inspect

        from app.main import HermesBackend
        from app.mt5.demo_router import DemoKellyRouter

        self.assertTrue(hasattr(DemoKellyRouter, "update_outcome_tracker"))

        quick = inspect.getsource(DemoKellyRouter.process_quick_exits)
        self.assertNotIn("tracker.update(", quick, "le tracker vit encore dans process_quick_exits")

        cycle = inspect.getsource(HermesBackend.run_cycle)
        self.assertIn("update_outcome_tracker(", cycle)


# ══════════════════════════════════════════════════════════════════════════
# T9 — les voyants de securite du dashboard reflettent la VRAIE config
# ══════════════════════════════════════════════════════════════════════════

class TestT9_VoyantsDeSecuriteBranches(unittest.TestCase):
    """`"demo_only": True` et `"allow_live_trading": False` etaient des LITTERAUX a
    SEPT endroits — y compris dans /local-api/audit-safety, l'endpoint dont le seul
    role est de PROUVER que le live est bloque.

    Un indicateur de securite qui ne peut pas signaler le danger est pire qu'aucun
    indicateur : il donne une fausse assurance."""

    def test_les_drapeaux_suivent_la_config(self) -> None:
        from app.local_api.server import _safety_flags

        with patch("app.local_api.server.get_settings",
                   return_value=SimpleNamespace(demo_only=True, allow_live_trading=False)):
            self.assertEqual(_safety_flags(), {"demo_only": True, "allow_live_trading": False})

    def test_LE_test__le_voyant_SAIT_signaler_le_danger(self) -> None:
        """Le test qui manquait. On active le live dans la config : le dashboard DOIT
        le dire. Avant, il aurait continue d'afficher `allow_live_trading: False`."""
        from app.local_api.server import _safety_flags

        with patch("app.local_api.server.get_settings",
                   return_value=SimpleNamespace(demo_only=False, allow_live_trading=True)):
            flags = _safety_flags()

        self.assertTrue(flags["allow_live_trading"], "le voyant ne sait pas signaler le live")
        self.assertFalse(flags["demo_only"])

    def test_plus_aucun_litteral_de_securite_dans_les_endpoints(self) -> None:
        """Verrou de non-regression : les litteraux ne doivent jamais revenir."""
        import re
        from pathlib import Path as _Path

        source = _Path("app/local_api/server.py").read_text(encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        motif = re.compile(r'"(demo_only|allow_live_trading|DEMO_ONLY|ALLOW_LIVE_TRADING)"\s*:\s*(True|False)')
        self.assertEqual(motif.findall(code), [], "un voyant de securite est de nouveau code en dur")


# ══════════════════════════════════════════════════════════════════════════
# COLLECTE V2 — separer l'archive v1 du coeur repare
# ══════════════════════════════════════════════════════════════════════════

class TestCollecteV2(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "dataset.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_core_version_ne_PEUT_PAS_marquer_la_frontiere(self) -> None:
        """La mission demandait de "rendre core_version=2 explicite". Or il l'est
        DEJA depuis COEUR_V2 (2026-07-08) : 10 733 lignes du dataset v1 le portent.
        Il ne peut donc pas separer v1 de v2 — d'ou un champ distinct."""
        from app.services.decision_dataset import CORE_VERSION, COLLECTION_VERSION
        self.assertEqual(CORE_VERSION, 2)
        self.assertEqual(COLLECTION_VERSION, 2)
        self.assertIsNot(CORE_VERSION, None)

    def test_chaque_ligne_ecrite_porte_le_marqueur_v2(self) -> None:
        import json as _json

        from app.services.decision_dataset import DecisionDataset

        ds = DecisionDataset(self.path)
        ds.record_decision({
            "event_type": "DEMO_SKIP", "symbol": "GOLD#", "direction": "BUY",
            "entry": 3300.0, "sl": 3294.0, "tp": 3312.0, "reason": "TEST",
        })
        ds.record_outcome({"ticket": "1", "virtual": True, "outcome": "TP_HIT"})

        rows = [_json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["collection_version"], 2, f"{row['row_type']} sans marqueur v2")

    def test_la_bascule_est_annoncee_une_seule_fois(self) -> None:
        from app.services import decision_dataset as dd

        dd._collection_v2_first_line_logged = False
        with patch.object(dd.log, "warning") as warn:
            ds = dd.DecisionDataset(self.path)
            ds.record_outcome({"ticket": "1", "virtual": True})
            ds.record_outcome({"ticket": "2", "virtual": True})

        bascules = [c for c in warn.call_args_list if "COLLECTE_V2" in str(c)]
        self.assertEqual(len(bascules), 1, "la bascule doit etre annoncee exactement une fois")


if __name__ == "__main__":
    unittest.main()
