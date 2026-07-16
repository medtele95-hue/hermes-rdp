# -*- coding: utf-8 -*-
"""MISSION_GEOMETRIE FIX 2 — Exit V2 laisse courir (trailing ATR au lieu du floor $ fixe).

T6-T9 (positifs + contrôles négatifs). Le breakeven reste le filet dur ; seul le
GAP du trailing devient ATR-based (figé à l'armement = floor monotone). Sans ATR
(atr_price=None) → ancien comportement (fail-safe), donc les tests Exit V2
EXISTANTS restent verts sans modification.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.exit_v2 import ExitV2Config, evaluate_exit_v2

ATR = 4.25  # GOLD M5 : usd_per_unit=1.0 -> atr_usd=4.25, gap (mult 1.0) = 4.25$


def _pos(profit, ticket=1):
    return SimpleNamespace(ticket=ticket, profit=profit, type=1, price_current=4000.0,
                           symbol="GOLD#", volume=0.01, price_open=4006.0)


def _info():
    return SimpleNamespace(trade_tick_value=1.0, trade_tick_size=0.01)


CFG = ExitV2Config()  # be_arm 2.0, be_floor 0.10, trail_start 2.0, trail_gap 1.2, trail_atr_mult 1.0


# ── T6 : un gagnant n'est plus coupé à +0.3 R — il COURT ─────────────────────

def test_T6_gagnant_court_avec_trailing_atr():
    st: dict = {}
    evaluate_exit_v2(_pos(3.0), None, _info(), CFG, st, atr_price=ATR)   # pic 3.0, BE armé
    a = evaluate_exit_v2(_pos(1.5), None, _info(), CFG, st, atr_price=ATR)  # recul modéré
    assert a["action"] == "NONE"          # floor ATR = 3-4.25 -> BE 0.10 ; 1.5 > 0.10 -> COURT

def test_T6b_l_ancien_floor_aurait_COUPE_au_meme_point():
    # contraste : sans ATR (comportement historique), le même recul FERME
    st: dict = {}
    evaluate_exit_v2(_pos(3.0), None, _info(), CFG, st)   # atr_price=None -> gap 1.20$
    a = evaluate_exit_v2(_pos(1.5), None, _info(), CFG, st)
    assert a["action"] == "CLOSE"         # floor 3-1.20=1.80 ; 1.5 <= 1.80 -> coupe (l'ancien mal)


# ── T7 : un gagnant qui recule de >1×ATR sous son pic SORT (protégé) ─────────

def test_T7_recul_superieur_a_1_atr_sort_au_trailing():
    st: dict = {}
    evaluate_exit_v2(_pos(6.0), None, _info(), CFG, st, atr_price=ATR)   # pic 6.0, gap figé 4.25
    a = evaluate_exit_v2(_pos(1.7), None, _info(), CFG, st, atr_price=ATR)  # recul 4.3 > 1xATR
    assert a["action"] == "CLOSE"
    assert a["reason"] == "EXIT_V2_TRAIL_FLOOR"   # floor 6-4.25=1.75 ; 1.7 <= 1.75


# ── T8 (contrôle négatif) : un perdant qui n'arme jamais le BE ───────────────

def test_T8_perdant_jamais_arme_exit_v2_ne_ferme_pas():
    st: dict = {}
    a = evaluate_exit_v2(_pos(-3.0), None, _info(), CFG, st, atr_price=ATR)
    assert a["action"] == "NONE"          # pas de floor -> Exit V2 ne touche pas ; le SL borné (broker) ferme
    assert a["be_armed"] is False


# ── T9 : BE armé -> ne redonne JAMAIS le trade au risque plein ──────────────

def test_T9_be_arme_protege_au_breakeven_pas_au_risque_plein():
    st: dict = {}
    evaluate_exit_v2(_pos(2.5), None, _info(), CFG, st, atr_price=ATR)   # arme BE
    a = evaluate_exit_v2(_pos(0.05), None, _info(), CFG, st, atr_price=ATR)  # retombe vers 0
    assert a["action"] == "CLOSE"
    assert a["reason"] == "EXIT_V2_BE_FLOOR"   # ferme a +0.10, JAMAIS au SL plein


# ── monotonicité : le gap ATR est figé à l'armement ─────────────────────────

def test_gap_atr_fige_a_l_armement_meme_si_atr_change():
    st: dict = {}
    evaluate_exit_v2(_pos(6.0), None, _info(), CFG, st, atr_price=4.25)   # gap figé 4.25
    evaluate_exit_v2(_pos(5.0), None, _info(), CFG, st, atr_price=10.0)   # ATR change -> ignoré
    assert abs(st[1]["trail_gap_usd"] - 4.25) < 1e-6   # figé, floor strictement monotone


# ── sans ATR : comportement historique intact (fail-safe) ────────────────────

def test_sans_atr_comportement_historique():
    st: dict = {}
    evaluate_exit_v2(_pos(3.0), None, _info(), CFG, st)          # atr_price omis
    a = evaluate_exit_v2(_pos(1.7), None, _info(), CFG, st)
    # floor historique 3-1.20=1.80 ; 1.7 <= 1.80 -> CLOSE (ancien comportement)
    assert a["action"] == "CLOSE"
