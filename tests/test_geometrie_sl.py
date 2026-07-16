# -*- coding: utf-8 -*-
"""MISSION_GEOMETRIE FIX 1 — SL structurel borné à min(structurel, k×ATR).

Tests T1-T5 (positifs + contrôle négatif) sur `_calc_sltp`, pur et déterministe.
Le clamp est un PLAFOND (n'active que si le structurel dépasse k×ATR), le TP suit
(refabriqué sur le risque borné, RR ≥ min_rr préservé).
"""
from __future__ import annotations

import math

from app.strategies.order_flow_execution_agent import _calc_sltp, _cap_risk_by_atr

CAP_USD = 22.60  # 0.25% x 9040$ — reference, non modifiee par le fix


# ── T1 : SELL, SL structurel large -> borné à 1.5×ATR ────────────────────────

def test_T1_sell_sl_borne_a_1_5_atr():
    # price 4000, vah 4035 -> sl structurel ~4039 (risque ~39$). ATR 4.0, k 1.5 -> cap 6.0
    price, sl, tp, rr = _calc_sltp("SELL", 4000.0, 3990.0, 3995.0, 4035.0, 3965.0,
                                   1.5, atr=4.0, sl_atr_cap_k=1.5)
    sl_dist = sl - price
    assert math.isclose(sl_dist, 6.0, abs_tol=1e-6)   # 1.5 x 4.0
    assert rr >= 1.5


# ── T2 (contrôle négatif) : structurel DÉJÀ < k×ATR -> inchangé ──────────────

def test_T2_structurel_deja_serre_inchange():
    # vah 4010 -> sl structurel ~4014 (risque ~14$). ATR 30, k 1.5 -> cap 45 > 14 -> INCHANGE
    price, sl, tp, rr = _calc_sltp("SELL", 4000.0, 3990.0, 3995.0, 4010.0, 3965.0,
                                   1.5, atr=30.0, sl_atr_cap_k=1.5)
    sl_dist = sl - price
    # structurel = (max(vah,price)+buffer) - price = 4010 + 4 - 4000 = 14
    assert math.isclose(sl_dist, 14.0, abs_tol=1e-6)   # borne inactive


def test_T2b_sans_atr_aucun_changement():
    # atr=None -> fail-safe, le SL structurel d'origine s'applique
    _, sl_none, _, _ = _calc_sltp("SELL", 4000.0, 3990.0, 3995.0, 4035.0, 3965.0, 1.5, atr=None)
    assert math.isclose(sl_none - 4000.0, 39.0, abs_tol=1e-6)   # structurel intact


# ── T3 : symétrie BUY ────────────────────────────────────────────────────────

def test_T3_buy_symetrie():
    # val 3965 -> sl structurel ~3961 (risque ~39$). ATR 4, k 1.5 -> cap 6, SL SOUS l'entree
    price, sl, tp, rr = _calc_sltp("BUY", 4000.0, 3990.0, 3995.0, 4035.0, 3965.0,
                                   1.5, atr=4.0, sl_atr_cap_k=1.5)
    assert sl < price                                   # SL sous l'entree en BUY
    assert math.isclose(price - sl, 6.0, abs_tol=1e-6)  # 1.5 x 4.0
    assert tp > price and rr >= 1.5


# ── T4 : le SL borné passe le cap de risque ──────────────────────────────────

def test_T4_borne_passe_le_cap_ou_le_structurel_echouait():
    _, sl, _, _ = _calc_sltp("SELL", 4000.0, 3990.0, 3995.0, 4035.0, 3965.0,
                             1.5, atr=4.0, sl_atr_cap_k=1.5)
    risk_borne = sl - 4000.0
    # structurel ~39$ > cap 22.60 (aurait ete rejete) ; borne 6$ < cap -> passe
    assert 39.0 > CAP_USD
    assert risk_borne < CAP_USD


# ── T5 : le TP est refabriqué sur le nouveau risque (pas l'ancienne cible) ────

def test_T5_tp_refabrique_sur_risque_borne():
    _, sl, tp, rr = _calc_sltp("SELL", 4000.0, 3990.0, 3995.0, 4035.0, 3965.0,
                               1.5, atr=4.0, sl_atr_cap_k=1.5)
    risk = sl - 4000.0                       # 6.0
    assert math.isclose(tp, 4000.0 - risk * 1.5, abs_tol=1e-6)   # TP proche (3991), PAS 3941
    assert math.isclose(rr, 1.5, abs_tol=1e-2)
    # controle : ce n'est PAS l'ancien TP lointain (structurel 39 x 1.5 = 58.5 -> 3941.5)
    assert not math.isclose(tp, 4000.0 - 39.0 * 1.5, abs_tol=1.0)


# ── le helper de clamp isolé ─────────────────────────────────────────────────

class TestCapHelper:
    def test_plafonne_quand_depasse(self):
        assert _cap_risk_by_atr(39.0, 4.0, 1.5) == 6.0

    def test_inchange_quand_sous_le_plafond(self):
        assert _cap_risk_by_atr(3.0, 4.0, 1.5) == 3.0

    def test_atr_absent_ou_nul_ne_touche_rien(self):
        assert _cap_risk_by_atr(39.0, None, 1.5) == 39.0
        assert _cap_risk_by_atr(39.0, 0.0, 1.5) == 39.0
        assert _cap_risk_by_atr(39.0, 4.0, 0.0) == 39.0
