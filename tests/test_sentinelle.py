# -*- coding: utf-8 -*-
"""Tests de la SENTINELLE — chaque regle A/B/C/D avec un cas qui DOIT alerter et
un controle negatif qui NE DOIT PAS. Donnees 100 % synthetiques, jamais la prod.

Principe verifie partout : la sentinelle ne crie jamais faux. Les cas negatifs
sont aussi importants que les positifs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sentinelle import rules
from sentinelle.rules import RED, ORANGE

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _exec(**kw) -> dict:
    base = {
        "ticket": "900001", "symbol": "GOLD#", "direction": "BUY",
        "entry": 4000.0, "exec_quality.fill_price": 4000.0, "sl": 3990.0, "tp": 4015.0,
        "collection_version": 2, "core_fix_level": "P0TER",
        "fallback_decision": None, "recorded_at": NOW.isoformat(),
    }
    base.update(kw)
    return base


# ── A1 : RR au fill < 1.5 ────────────────────────────────────────────────────

class TestA1:
    def test_alerte_si_rr_fill_sous_plancher(self):
        # BUY entry/fill 4000, sl 3990 (risk 10), tp 4012 (reward 12) -> RR 1.2
        a = rules.check_A1_rr_fill([_exec(tp=4012.0)])
        assert len(a) == 1 and a[0].rule == "A1" and a[0].severity == RED

    def test_le_fill_qui_slippe_sous_le_plancher_est_capte(self):
        # RR au SIGNAL = 1.5 (sl 3990, tp 4015, entry 3990+? ) mais le FILL a slippe
        # entry 4000 -> fill 4002 : risk 12, reward 13 -> RR 1.083
        a = rules.check_A1_rr_fill([_exec(entry=4000.0, **{"exec_quality.fill_price": 4002.0}, sl=3990.0, tp=4015.0)])
        assert len(a) == 1 and a[0].rule == "A1"

    def test_pas_d_alerte_si_rr_conforme(self):
        # RR = 1.5 pile (risk 10, reward 15)
        assert rules.check_A1_rr_fill([_exec(sl=3990.0, tp=4015.0)]) == []

    def test_tolerance_flottante_ne_declenche_pas(self):
        # RR = 1.495 (juste sous 1.5 mais dans la tolerance de 0.01)
        a = rules.check_A1_rr_fill([_exec(sl=3990.0, tp=4014.95)])
        assert a == []


# ── A2 : SL/TP du mauvais cote ───────────────────────────────────────────────

class TestA2:
    def test_alerte_buy_sl_au_dessus(self):
        a = rules.check_A2_sltp_side([_exec(direction="BUY", entry=4000.0, sl=4010.0, tp=4020.0)])
        assert len(a) == 1 and a[0].severity == RED

    def test_alerte_sell_tp_au_dessus(self):
        a = rules.check_A2_sltp_side([_exec(direction="SELL", entry=4000.0, sl=3990.0, tp=4010.0)])
        assert len(a) == 1

    def test_pas_d_alerte_geometrie_correcte_buy(self):
        assert rules.check_A2_sltp_side([_exec(direction="BUY", entry=4000.0, sl=3990.0, tp=4015.0)]) == []

    def test_pas_d_alerte_geometrie_correcte_sell(self):
        assert rules.check_A2_sltp_side([_exec(direction="SELL", entry=4000.0, sl=4010.0, tp=3985.0)]) == []


# ── A3 : execute malgre BLOCK (ROUGE prioritaire) ────────────────────────────

class TestA3:
    def test_alerte_execute_malgre_block(self):
        a = rules.check_A3_executed_despite_block([
            _exec(fallback_decision="BLOCK", fallback_block_reason="TOP_DOWN_READER_BLOCK")
        ])
        assert len(a) == 1 and a[0].rule == "A3" and a[0].severity == RED
        assert "TOP_DOWN_READER_BLOCK" in a[0].message

    def test_pas_d_alerte_si_fallback_pas_block(self):
        assert rules.check_A3_executed_despite_block([_exec(fallback_decision="PASS")]) == []
        assert rules.check_A3_executed_despite_block([_exec(fallback_decision=None)]) == []


# ── A4 : position nue (sans SL ou TP) ────────────────────────────────────────

class TestA4:
    def test_alerte_sl_absent(self):
        a = rules.check_A4_naked_positions([{"ticket": "1", "symbol": "GOLD#", "sl": 0.0, "tp": 4015.0}])
        assert len(a) == 1 and a[0].severity == RED

    def test_alerte_tp_absent(self):
        a = rules.check_A4_naked_positions([{"ticket": "1", "symbol": "GOLD#", "sl": 3990.0, "tp": 0.0}])
        assert len(a) == 1

    def test_pas_d_alerte_position_protegee(self):
        assert rules.check_A4_naked_positions([{"ticket": "1", "symbol": "GOLD#", "sl": 3990.0, "tp": 4015.0}]) == []


# ── A5 : marqueur de version pre-P0TER ───────────────────────────────────────

class TestA5:
    def test_alerte_collection_version_non_2(self):
        a = rules.check_A5_version_marker([_exec(collection_version=1)])
        assert len(a) == 1 and a[0].severity == ORANGE

    def test_alerte_core_fix_level_absent(self):
        a = rules.check_A5_version_marker([_exec(core_fix_level=None)])
        assert len(a) == 1

    def test_pas_d_alerte_marqueurs_ok(self):
        assert rules.check_A5_version_marker([_exec(collection_version=2, core_fix_level="P0TER")]) == []


# ── B1 : deal ferme sans outcome > 15 min ────────────────────────────────────

class TestB1:
    def test_alerte_cloture_ancienne_sans_outcome(self):
        deals = [{"position_id": "555", "close_time": NOW - timedelta(minutes=20)}]
        a = rules.check_B1_closed_without_outcome(deals, {}, NOW)
        assert len(a) == 1 and a[0].rule == "B1" and a[0].severity == RED

    def test_pas_d_alerte_si_outcome_present(self):
        deals = [{"position_id": "555", "close_time": NOW - timedelta(minutes=20)}]
        assert rules.check_B1_closed_without_outcome(deals, {"555": {"pnl_reconciled": -3.0}}, NOW) == []

    def test_pas_d_alerte_cloture_recente_grace_15min(self):
        deals = [{"position_id": "555", "close_time": NOW - timedelta(minutes=5)}]
        assert rules.check_B1_closed_without_outcome(deals, {}, NOW) == []


# ── B2 : couverture outcomes < 90 % ──────────────────────────────────────────

class TestB2:
    def test_alerte_couverture_faible(self):
        # 6 executes, 3 avec outcome -> 50 %
        ex = [_exec(ticket=str(i)) for i in range(6)]
        oc = {"0": {}, "1": {}, "2": {}}
        a = rules.check_B2_outcome_coverage(ex, oc)
        assert len(a) == 1 and a[0].rule == "B2"
        assert "50%" in a[0].message

    def test_pas_d_alerte_couverture_pleine(self):
        ex = [_exec(ticket=str(i)) for i in range(6)]
        oc = {str(i): {} for i in range(6)}
        assert rules.check_B2_outcome_coverage(ex, oc) == []

    def test_echantillon_trop_petit_ne_declenche_pas(self):
        # 2 executes, 0 outcome -> 0 % MAIS < seuil d'echantillon -> pas d'alerte
        ex = [_exec(ticket="0"), _exec(ticket="1")]
        assert rules.check_B2_outcome_coverage(ex, {}) == []


# ── B3 : dataset fige > 2h en session ────────────────────────────────────────

class TestB3:
    def test_alerte_fige_en_session(self):
        a = rules.check_B3_dataset_stall(NOW - timedelta(hours=3), NOW, market_open=True, killswitch_triggered=False)
        assert len(a) == 1 and a[0].rule == "B3"

    def test_pas_d_alerte_hors_session(self):
        assert rules.check_B3_dataset_stall(NOW - timedelta(hours=5), NOW, market_open=False, killswitch_triggered=False) == []

    def test_pas_d_alerte_si_killswitch_declenche(self):
        assert rules.check_B3_dataset_stall(NOW - timedelta(hours=5), NOW, market_open=True, killswitch_triggered=True) == []

    def test_pas_d_alerte_si_activite_recente(self):
        assert rules.check_B3_dataset_stall(NOW - timedelta(minutes=30), NOW, market_open=True, killswitch_triggered=False) == []


# ── B4 : compteur de pertes bloque a 0 ───────────────────────────────────────

class TestB4:
    def test_alerte_pertes_reelles_mais_compteur_zero(self):
        a = rules.check_B4_counter_stuck(deals_losses_today=3, logged_losses=0,
                                         logged_daily_pnl=0.0, killswitch_log_age_min=2.0)
        assert len(a) == 1 and a[0].rule == "B4" and a[0].severity == RED

    def test_pas_d_alerte_si_compteur_coherent(self):
        assert rules.check_B4_counter_stuck(3, logged_losses=3, logged_daily_pnl=-30.0, killswitch_log_age_min=2.0) == []

    def test_pas_d_alerte_si_aucune_perte(self):
        assert rules.check_B4_counter_stuck(0, logged_losses=0, logged_daily_pnl=0.0, killswitch_log_age_min=2.0) == []

    def test_pas_d_alerte_si_ligne_killswitch_trop_vieille(self):
        # le bot n'a peut-etre pas encore recompte -> on ne conclut pas
        assert rules.check_B4_counter_stuck(3, logged_losses=0, logged_daily_pnl=0.0, killswitch_log_age_min=120.0) == []

    def test_pas_d_alerte_si_pas_de_ligne_killswitch(self):
        assert rules.check_B4_counter_stuck(3, logged_losses=None, logged_daily_pnl=None, killswitch_log_age_min=None) == []


# ── C1 : position orpheline ──────────────────────────────────────────────────

class TestC1:
    def test_alerte_position_absente_du_dataset(self):
        pos = [{"ticket": "999", "symbol": "GOLD#", "magic": 909002}]
        a = rules.check_C1_orphan_positions(pos, known_tickets={"111", "222"})
        assert len(a) == 1 and a[0].rule == "C1" and a[0].severity == RED

    def test_pas_d_alerte_position_connue(self):
        pos = [{"ticket": "999", "symbol": "GOLD#", "magic": 909002}]
        assert rules.check_C1_orphan_positions(pos, known_tickets={"999"}) == []


# ── C2 : ecart P&L dataset vs deal MT5 ───────────────────────────────────────

class TestC2:
    def test_alerte_ecart_pnl(self):
        oc = {"555": {"pnl_reconciled": -9.95}}
        mt5 = {"555": 9.95}  # signe inverse -> ecart 19.9
        a = rules.check_C2_pnl_mismatch(oc, mt5)
        assert len(a) == 1 and a[0].rule == "C2" and a[0].severity == RED

    def test_pas_d_alerte_pnl_coherent(self):
        oc = {"555": {"pnl_reconciled": -9.95}}
        mt5 = {"555": -9.96}  # ecart 0.01 < tolerance
        assert rules.check_C2_pnl_mismatch(oc, mt5) == []

    def test_pas_d_alerte_si_pnl_manquant(self):
        assert rules.check_C2_pnl_mismatch({"555": {"pnl_reconciled": None}}, {"555": -9.95}) == []


# ── D1 : hermes.log muet > 20 min ────────────────────────────────────────────

class TestD1:
    def test_alerte_log_muet(self):
        a = rules.check_D1_log_silence(NOW - timedelta(minutes=25), NOW)
        assert len(a) == 1 and a[0].rule == "D1"

    def test_pas_d_alerte_log_actif(self):
        assert rules.check_D1_log_silence(NOW - timedelta(minutes=5), NOW) == []

    def test_pas_d_alerte_si_mtime_inconnu(self):
        assert rules.check_D1_log_silence(None, NOW) == []


# ── combinateur : integration + signatures anti-bruit ────────────────────────

class TestEvaluateAll:
    def test_un_paquet_sain_ne_produit_aucune_anomalie(self):
        facts = {
            "new_executed": [_exec()],
            "executed_24h": [_exec(ticket=str(i)) for i in range(6)],
            "outcomes_by_ticket": {str(i): {"pnl_reconciled": -1.0} for i in range(6)},
            "open_positions": [{"ticket": "0", "symbol": "GOLD#", "sl": 3990.0, "tp": 4015.0, "magic": 909002}],
            "closing_deals": [],
            "mt5_pnl_by_ticket": {},
            "known_tickets": {"0", "900001"},
            "last_decision_time": NOW - timedelta(minutes=10),
            "log_mtime": NOW - timedelta(minutes=2),
            "market_open": True,
            "killswitch_triggered": False,
            "deals_losses_today": 0,
            "logged_losses": 0, "logged_daily_pnl": 0.0, "killswitch_log_age_min": 2.0,
        }
        assert rules.evaluate_all(facts, NOW) == []

    def test_signature_stable_pour_le_cooldown(self):
        a = rules.check_A3_executed_despite_block([_exec(ticket="42", fallback_decision="BLOCK")])[0]
        assert a.signature == "A3:42"

    def test_plusieurs_regles_se_cumulent(self):
        # un trade a la fois A3 (block) ET A2 (mauvais cote) ET A5 (version)
        bad = _exec(ticket="7", fallback_decision="BLOCK", direction="BUY",
                    entry=4000.0, sl=4010.0, tp=4020.0, collection_version=1)
        facts = {"new_executed": [bad]}
        rulenames = {a.rule for a in rules.evaluate_all(facts, NOW)}
        assert {"A2", "A3", "A5"} <= rulenames
