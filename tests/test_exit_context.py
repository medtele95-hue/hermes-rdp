# -*- coding: utf-8 -*-
"""SPEC_EXIT_CONTEXT_WRITER — tests du patch de capture du contexte de sortie.

Chaque exigence de la §6 avec un cas POSITIF et un CONTROLE NEGATIF :
- exit_v2 : be_arm_time/price posés A l'armement seulement (décision INCHANGÉE)
- tracker : mfe/mae price+time posés au NOUVEL extrême seulement
- _apply_exit_context : fusion + version=1 + present ; fail-silent si contexte absent
- append-only : les lignes existantes restent BYTE-IDENTIQUES
- intégration : une VRAIE ligne outcome écrite avec exit_mechanism + contexte

Données 100 % synthétiques, jamais la prod.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.decision_dataset import (
    DecisionDataset,
    OutcomeTracker,
    _update_excursion,
)
from app.services.exit_v2 import ExitV2Config, evaluate_exit_v2

NOW = datetime(2026, 7, 15, 14, 48, 42, tzinfo=timezone.utc)
NOW2 = NOW + timedelta(minutes=1)
NOW3 = NOW + timedelta(minutes=2)


def _pos(profit, ticket=1, side="SELL", price_current=4050.0, entry=4060.0):
    return SimpleNamespace(
        ticket=ticket, profit=profit, type=(1 if side == "SELL" else 0),
        price_current=price_current, symbol="GOLD#", volume=0.01, price_open=entry,
    )


# ── exit_v2 : métadonnée d'armement, DÉCISION inchangée ──────────────────────

class TestExitV2ArmMetadata:
    def test_be_arm_time_et_price_poses_a_l_armement(self):
        state: dict = {}
        # profit 2.5 >= be_arm_usd 2.0 -> arme
        evaluate_exit_v2(_pos(2.5, price_current=4050.0), None, None, ExitV2Config(), state, now=NOW)
        st = state[1]
        assert st["be_armed"] is True
        assert st["be_arm_time"] == NOW.isoformat()
        assert st["be_arm_price"] == 4050.0

    def test_controle_negatif_pas_d_armement_sous_le_seuil(self):
        state: dict = {}
        evaluate_exit_v2(_pos(1.0), None, None, ExitV2Config(), state, now=NOW)
        st = state[1]
        assert st["be_armed"] is False
        assert st["be_arm_time"] is None
        assert st["be_arm_price"] is None

    def test_now_None_n_horodate_pas_mais_arme_quand_meme(self):
        """Appelant historique (5 args) : la décision d'armer est identique,
        seule l'horodatage manque."""
        state: dict = {}
        evaluate_exit_v2(_pos(2.5), None, None, ExitV2Config(), state)  # pas de now
        assert state[1]["be_armed"] is True
        assert state[1]["be_arm_time"] is None

    def test_la_DECISION_close_est_inchangee_par_now(self):
        """Preuve directe : le champ `now` ne change RIEN à l'action. Un profit
        qui a piqué puis rechute sous le plancher trailing -> CLOSE, avec ou sans now."""
        # peak 3.0 armé, retombe à 1.0 : trail floor = peak(3) - gap(1.2) = 1.8 > 1.0 -> CLOSE
        s1: dict = {}
        evaluate_exit_v2(_pos(3.0), None, None, ExitV2Config(), s1, now=NOW)
        a1 = evaluate_exit_v2(_pos(1.0), None, None, ExitV2Config(), s1, now=NOW2)
        s2: dict = {}
        evaluate_exit_v2(_pos(3.0), None, None, ExitV2Config(), s2)  # sans now
        a2 = evaluate_exit_v2(_pos(1.0), None, None, ExitV2Config(), s2)
        assert a1["action"] == a2["action"] == "CLOSE"
        assert a1["reason"] == a2["reason"]  # même mécanisme


# ── tracker : prix + temps des extrêmes ──────────────────────────────────────

def _item(direction="SELL", entry=4000.0):
    return {
        "direction": direction, "entry": entry, "mfe": 0.0, "mae": 0.0,
        "mfe_price": None, "mfe_time": None, "mae_price": None, "mae_time": None,
    }


class TestExcursionPriceTime:
    def test_mfe_price_et_time_poses_au_nouvel_extreme_favorable(self):
        item = _item("SELL", 4000.0)
        # SELL favorable = prix baisse -> 3996 : excursion +4
        assert _update_excursion(item, 3996.0, NOW) is True
        assert item["mfe"] == 4.0 and item["mfe_price"] == 3996.0 and item["mfe_time"] == NOW.isoformat()

    def test_mae_price_et_time_poses_au_nouvel_extreme_adverse(self):
        item = _item("SELL", 4000.0)
        assert _update_excursion(item, 4003.0, NOW) is True   # adverse +3 contre
        assert item["mae"] == -3.0 and item["mae_price"] == 4003.0 and item["mae_time"] == NOW.isoformat()

    def test_controle_negatif_un_extreme_moindre_n_ecrase_pas(self):
        item = _item("SELL", 4000.0)
        _update_excursion(item, 3990.0, NOW)     # mfe +10 @3990
        changed = _update_excursion(item, 3995.0, NOW2)  # +5 < 10 : pas un nouvel extrême
        assert changed is False
        assert item["mfe"] == 10.0 and item["mfe_price"] == 3990.0 and item["mfe_time"] == NOW.isoformat()

    def test_controle_negatif_gagnant_monotone_n_a_pas_de_MAE(self):
        item = _item("SELL", 4000.0)
        _update_excursion(item, 3998.0, NOW)     # que du favorable
        _update_excursion(item, 3995.0, NOW2)
        assert item["mae"] == 0.0 and item["mae_price"] is None and item["mae_time"] is None


# ── _apply_exit_context : fusion + frontière + fail-silent ───────────────────

def _tracker(tmp_path):
    return OutcomeTracker(DecisionDataset(path=tmp_path / "d.jsonl"))


class TestApplyExitContext:
    def test_fusion_du_contexte_et_frontiere(self, tmp_path):
        tr = _tracker(tmp_path)
        outcome = {"ticket": 1}
        ctx = {
            "exit_mechanism": "EXIT_V2_BE_FLOOR", "be_armed": True,
            "be_arm_time": NOW.isoformat(), "be_arm_price": 4050.0,
            "peak_usd": 3.1, "floor_usd": 0.10, "spread_at_exit": 0.33,
            "atr_at_exit": 12.5, "momentum_at_exit": -80.0, "cvd_at_exit": -1200.0,
            "cvd_divergence_at_exit": None, "dist_to_structure_at_exit": {"pdc": 0.2},
            "exit_context_captured_at": NOW.isoformat(),
        }
        tr._apply_exit_context(outcome, 1, lambda t: ctx)
        assert outcome["exit_context_version"] == 1
        assert outcome["exit_context_present"] is True
        assert outcome["exit_mechanism"] == "EXIT_V2_BE_FLOOR"
        assert outcome["be_armed"] is True
        assert outcome["spread_at_exit"] == 0.33
        assert outcome["dist_to_structure_at_exit"] == {"pdc": 0.2}

    def test_controle_negatif_contexte_absent_restart_fail_silent(self, tmp_path):
        tr = _tracker(tmp_path)
        outcome = {"ticket": 2}
        tr._apply_exit_context(outcome, 2, lambda t: None)   # restart : rien de stampé
        assert outcome["exit_context_version"] == 1          # frontière quand même posée
        assert outcome["exit_context_present"] is False
        assert "exit_mechanism" not in outcome               # pas de champ contexte

    def test_controle_negatif_fn_qui_leve_ne_casse_jamais(self, tmp_path):
        tr = _tracker(tmp_path)
        outcome = {"ticket": 3}

        def _boom(_):
            raise ValueError("boom")

        tr._apply_exit_context(outcome, 3, _boom)            # jamais d'exception
        assert outcome["exit_context_present"] is False
        assert outcome["exit_context_version"] == 1

    def test_pas_de_fn_appelants_historiques(self, tmp_path):
        tr = _tracker(tmp_path)
        outcome = {"ticket": 4}
        tr._apply_exit_context(outcome, 4, None)
        assert outcome["exit_context_present"] is False


# ── append-only : les lignes existantes restent BYTE-IDENTIQUES ──────────────

class TestAppendOnly:
    def test_les_lignes_existantes_ne_sont_jamais_reecrites(self, tmp_path):
        ds = DecisionDataset(path=tmp_path / "d.jsonl")
        ds.record_outcome({"key": "T1", "ticket": 1, "outcome": "CLOSED_EXPERT",
                           "pnl_reconciled": 2.0, "exit_context_version": 1})
        before = (tmp_path / "d.jsonl").read_bytes()
        ds.record_outcome({"key": "T2", "ticket": 2, "outcome": "CLOSED_EXPERT",
                           "pnl_reconciled": 3.0, "exit_context_version": 1})
        after = (tmp_path / "d.jsonl").read_bytes()
        assert after.startswith(before)                       # préfixe byte-identique
        assert after.count(b"\n") == 2                        # exactement 2 lignes


# ── intégration : une VRAIE ligne outcome écrite avec le contexte ────────────

class TestIntegrationOutcomeLine:
    def test_une_ligne_outcome_reelle_porte_mecanisme_et_contexte(self, tmp_path):
        """Chemin de prod complet : register -> excursions -> _outcome_from_deals
        -> _apply_exit_context -> record_outcome -> relecture du JSONL écrit."""
        ds = DecisionDataset(path=tmp_path / "d.jsonl")
        tr = OutcomeTracker(ds)
        tr.register({
            "row_type": "decision", "symbol": "GOLD#", "broker_symbol": "GOLD#",
            "direction": "SELL", "entry": 4057.61, "exec_quality.fill_price": 4059.23,
            "sl": 4066.89, "tp": 4043.69, "order_success": True, "ticket": 386029828,
            "recorded_at": "2026-07-15T14:28:19+00:00", "setup_id": "sid-1",
        })
        item = tr._open["T386029828"]
        _update_excursion(item, 4054.48, NOW)     # meilleur favorable
        _update_excursion(item, 4064.05, NOW2)    # pire adverse (a frôlé le SL)

        info = {"state": "CLOSED", "close_price": 4056.58,
                "closed_at": "2026-07-15T14:48:42+00:00", "outcome": "CLOSED_EXPERT",
                "close_reason": "EXPERT", "pnl_reconciled": 2.65, "deals_count": 2}
        ctx = {
            "exit_mechanism": "EXIT_V2_TRAIL_FLOOR", "be_armed": True,
            "be_arm_time": "2026-07-15T14:40:00+00:00", "be_arm_price": 4055.0,
            "peak_usd": 3.13, "floor_usd": 1.93, "spread_at_exit": 0.33,
            "atr_at_exit": 12.4, "momentum_at_exit": -95.0, "cvd_at_exit": -1300.0,
            "cvd_divergence_at_exit": "bear", "dist_to_structure_at_exit": {"pdc_atr": 0.24},
            "exit_context_captured_at": "2026-07-15T14:48:42+00:00",
        }
        outcome = tr._outcome_from_deals(item, info)
        tr._apply_exit_context(outcome, "386029828", lambda t: ctx)
        assert ds.record_outcome(outcome) is True

        # relire la VRAIE ligne écrite
        lines = (tmp_path / "d.jsonl").read_text(encoding="utf-8").strip().splitlines()
        row = json.loads(lines[-1])
        assert row["row_type"] == "outcome"
        assert row["exit_mechanism"] == "EXIT_V2_TRAIL_FLOOR"
        assert row["exit_context_version"] == 1
        assert row["exit_context_present"] is True
        assert row["be_armed"] is True and row["be_arm_price"] == 4055.0
        assert row["fill_price"] == 4059.23            # base fill réelle
        assert row["mfe_price"] == 4054.48 and row["mae_price"] == 4064.05
        assert row["spread_at_exit"] == 0.33 and row["atr_at_exit"] == 12.4
        assert row["momentum_at_exit"] == -95.0
