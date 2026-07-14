# -*- coding: utf-8 -*-
"""AUDIT READ ONLY — tests des formules de reference ET verification des
fonctions REELLES de l'app contre elles. Aucune modification de production.

Executer :  python -m pytest audit_geo_math/test_audit_formulas.py -q
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit_geo_math import (  # noqa: E402
    ref_loss_per_lot,
    ref_lot_for_risk,
    ref_pnl,
    ref_rr,
    ref_touch_price,
    ref_wilder_atr,
)

# specs REELLES relevees le 2026-07-14 via mt5.symbol_info (lecture seule)
GOLD = {"tick_size": 0.01, "tick_value": 1.0, "step": 0.01, "vmin": 0.01, "vmax": 50.0}
BTC = {"tick_size": 0.01, "tick_value": 0.01, "step": 0.01, "vmin": 0.01, "vmax": 80.0}


# ── RR : geometrie orientee BUY/SELL ─────────────────────────────────────────

class TestRR:
    def test_buy_gagnant(self):
        assert math.isclose(ref_rr("BUY", 100.0, 95.0, 110.0), 2.0)

    def test_sell_gagnant(self):
        assert math.isclose(ref_rr("SELL", 100.0, 105.0, 90.0), 2.0)

    def test_le_ticket_383260970(self):
        """Le cas reel : SELL, signal 1.500 -> fill 1.434."""
        assert math.isclose(ref_rr("SELL", 3989.84, 4097.38, 3828.53), 1.5, abs_tol=5e-4)
        assert math.isclose(ref_rr("SELL", 3986.93, 4097.38, 3828.53), 1.434, abs_tol=5e-4)

    def test_sl_du_mauvais_cote_est_invalide(self):
        assert ref_rr("BUY", 100.0, 105.0, 110.0) is None   # SL au-dessus en BUY
        assert ref_rr("SELL", 100.0, 95.0, 90.0) is None    # SL en dessous en SELL

    def test_non_calculable(self):
        assert ref_rr("BUY", None, 95.0, 110.0) is None
        assert ref_rr("BUY", float("nan"), 95.0, 110.0) is None
        assert ref_rr("WAIT", 100.0, 95.0, 110.0) is None

    def test_la_fonction_REELLE_de_l_app_est_conforme(self):
        """demo_router._final_rr contre la reference, sur 4 cas."""
        from app.mt5.demo_router import _final_rr
        for d, e, s, t in (("BUY", 100.0, 95.0, 110.0), ("SELL", 100.0, 105.0, 90.0),
                           ("SELL", 3986.93, 4097.38, 3828.53), ("BUY", 63000.0, 62000.0, 65000.0)):
            assert math.isclose(_final_rr(d, e, s, t), ref_rr(d, e, s, t), rel_tol=1e-9)

    def test_app_final_rr_geometrie_inversee(self):
        """_final_rr sur un SL du mauvais cote : la reference dit None."""
        from app.mt5.demo_router import _final_rr
        got = _final_rr("BUY", 100.0, 105.0, 110.0)
        # constat, pas exigence : documenter le comportement reel
        assert got is None or got < 0  # negatif ou None, jamais un RR "valide"


# ── conversions USD : jamais profit = Δprix x lot ────────────────────────────

class TestConversions:
    def test_gold_1_point_1_usd_au_lot_001(self):
        assert math.isclose(ref_loss_per_lot(4000.0, 3999.0, **{k: GOLD[k] for k in ("tick_size", "tick_value")}) * 0.01, 1.0)

    def test_btc_1_point_1_cent_au_lot_001(self):
        assert math.isclose(ref_loss_per_lot(64000.0, 63999.0, **{k: BTC[k] for k in ("tick_size", "tick_value")}) * 0.01, 0.01)

    def test_asymetrie_gold_btc_7pour1(self):
        """SL medians v1 : GOLD 50.32 pts, BTC 689.98 pts. Au lot 0.01 :
        GOLD = 50.32 USD, BTC = 6.90 USD. Le MEME cap 0.01 porte 7.3x le risque."""
        g = ref_loss_per_lot(4000.0, 4000.0 - 50.32, GOLD["tick_size"], GOLD["tick_value"]) * 0.01
        b = ref_loss_per_lot(64000.0, 64000.0 - 689.98, BTC["tick_size"], BTC["tick_value"]) * 0.01
        assert math.isclose(g, 50.32, abs_tol=0.01)
        assert math.isclose(b, 6.90, abs_tol=0.01)
        assert g / b > 7.0

    def test_tick_value_nul_ne_divise_pas_par_zero(self):
        assert ref_loss_per_lot(100.0, 99.0, 0.0, 1.0) is None
        assert ref_loss_per_lot(100.0, 99.0, 0.01, 0.0) is None


# ── sizing : floor, volume_min, bornes ───────────────────────────────────────

class TestSizing:
    def test_sizing_nominal_gold(self):
        # equity 10000, risque 0.25% = 25 USD ; SL 50 pts -> 50 USD/0.01 lot
        # -> lot = 25/5000 = 0.005 -> floor step 0.01 -> < volume_min -> None
        lot = ref_lot_for_risk(10000.0, 0.25, 4000.0, 3950.0, GOLD["tick_size"], GOLD["tick_value"],
                               GOLD["step"], GOLD["vmin"], GOLD["vmax"])
        assert lot is None  # le lot minimum risquerait 50 USD > 25 demandes

    def test_le_lot_min_qui_depasse_le_risque_est_refuse(self):
        """LE piege du sizing : forcer volume_min quand le calcul donne moins,
        c'est risquer PLUS que demande. La reponse correcte est 'ne pas trader'."""
        lot = ref_lot_for_risk(1000.0, 0.1, 4000.0, 3900.0, GOLD["tick_size"], GOLD["tick_value"],
                               GOLD["step"], GOLD["vmin"], GOLD["vmax"])
        assert lot is None

    def test_arrondi_floor_jamais_au_dessus(self):
        # risque 100 USD, SL 30 pts gold -> 100/3000 lot = 0.0333 -> floor = 0.03
        lot = ref_lot_for_risk(10000.0, 1.0, 4000.0, 3970.0, GOLD["tick_size"], GOLD["tick_value"],
                               GOLD["step"], GOLD["vmin"], GOLD["vmax"])
        assert math.isclose(lot, 0.03)
        # verification : 0.03 lot x 30 pts x 100 USD/lot/pt = 90 <= 100 demande
        assert ref_loss_per_lot(4000.0, 3970.0, GOLD["tick_size"], GOLD["tick_value"]) * lot <= 100.0

    def test_borne_volume_max(self):
        lot = ref_lot_for_risk(10_000_000.0, 10.0, 64000.0, 63990.0, BTC["tick_size"], BTC["tick_value"],
                               BTC["step"], BTC["vmin"], BTC["vmax"])
        assert lot == BTC["vmax"]

    def test_equity_nulle_ou_absente(self):
        assert ref_lot_for_risk(0.0, 1.0, 100.0, 99.0, 0.01, 1.0, 0.01, 0.01, 50.0) is None
        assert ref_lot_for_risk(None, 1.0, 100.0, 99.0, 0.01, 1.0, 0.01, 0.01, 50.0) is None


# ── P&L : signes BUY/SELL ────────────────────────────────────────────────────

class TestPnl:
    def test_buy_gagnant_perdant(self):
        assert ref_pnl("BUY", 4000.0, 4010.0, 0.01, GOLD["tick_size"], GOLD["tick_value"]) == 10.0
        assert ref_pnl("BUY", 4000.0, 3990.0, 0.01, GOLD["tick_size"], GOLD["tick_value"]) == -10.0

    def test_sell_gagnant_perdant(self):
        assert ref_pnl("SELL", 4000.0, 3990.0, 0.01, GOLD["tick_size"], GOLD["tick_value"]) == 10.0
        assert ref_pnl("SELL", 4000.0, 4010.0, 0.01, GOLD["tick_size"], GOLD["tick_value"]) == -10.0

    def test_le_ticket_reel(self):
        """383260970 : SELL GOLD fill 3986.93, ferme 4026.55 -> ~-39.6 USD a 0.01."""
        pnl = ref_pnl("SELL", 3986.93, 4026.55, 0.01, GOLD["tick_size"], GOLD["tick_value"])
        assert math.isclose(pnl, -39.62, abs_tol=0.01)


# ── ATR Wilder vs SMA ────────────────────────────────────────────────────────

class TestWilder:
    def test_wilder_differe_du_rolling_mean(self):
        """Preuve numerique que Wilder != SMA : serie avec un choc de volatilite."""
        highs = [10.0] * 15 + [20.0] + [10.0] * 15
        lows = [9.0] * 15 + [8.0] + [9.0] * 15
        closes = [9.5] * 15 + [15.0] + [9.5] * 15
        wilder = ref_wilder_atr(highs, lows, closes, 14)
        # SMA des 14 derniers TR (le choc est sorti de la fenetre)
        trs = []
        for i in range(1, len(closes)):
            trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
        sma = sum(trs[-14:]) / 14
        # Wilder garde la memoire du choc ; le SMA l'a oublie
        assert wilder > sma

    def test_atr_percentile_du_dataset_est_un_SMA(self):
        """CONSTAT D'AUDIT — CORRIGE DEPUIS, par P0-TER (2026-07-14).

        L'audit avait releve que decision_dataset.atr_percentile utilisait
        tr.rolling(period).mean() — un SMA — alors que le Coeur V2 avait migre les
        ATR decisionnels vers Wilder. Le writer ecrit desormais
        `regime.atr_percentile_wilder`. L'ancienne fonction SMA est conservee mais
        N'EST PLUS APPELEE : les 10 486 lignes v1 qui la portent restent lisibles.

        Ce test verrouille la correction : le SMA existe encore, mais le WRITER ne
        l'ecrit plus."""
        import inspect

        from app.services.decision_dataset import atr_percentile, build_decision_row

        src = inspect.getsource(atr_percentile)
        assert "rolling(period).mean()" in src          # la fonction v1 existe toujours

        row = build_decision_row({"symbol": "GOLD#"})   # ... mais le writer l'ignore
        assert "regime.atr_percentile" not in row
        assert "regime.atr_percentile_wilder" in row


# ── bid/ask : quel cote touche un niveau ─────────────────────────────────────

class TestTouchSide:
    def test_buy_sort_au_bid_sell_sort_a_l_ask(self):
        assert ref_touch_price("BUY", "SL", bid=99.0, ask=99.3) == 99.0
        assert ref_touch_price("SELL", "SL", bid=99.0, ask=99.3) == 99.3

    def test_le_simulateur_virtuel_de_l_app_utilise_le_MID(self):
        """CONSTAT D'AUDIT — CORRIGE DEPUIS, par P0-TER (2026-07-14).

        L'audit avait releve que update_outcome_tracker passait (bid+ask)/2 au
        tracker : la touche SL d'un SELL etait testee au mid au lieu de l'ask, ce qui
        sous-estimait les SL touches d'un demi-spread (GOLD 0,15 ; BTC 11,25) et
        sur-etiquetait donc les WIN du dataset d'apprentissage.

        Le routeur transmet desormais bid ET ask ; le tracker choisit le cote selon
        la direction, conformement a la reference `ref_touch_price`."""
        import inspect

        from app.mt5.demo_router import DemoKellyRouter
        from app.services.decision_dataset import _exit_side_price

        src = inspect.getsource(DemoKellyRouter.update_outcome_tracker)
        assert "(bid + ask) / 2.0" not in src
        assert '{"bid": bid, "ask": ask}' in src

        # et le tracker sort bien du BON cote — la reference de l'audit fait foi
        quote = {"bid": 99.0, "ask": 99.3}
        assert _exit_side_price(quote, "BUY") == (ref_touch_price("BUY", "SL", **quote), "bid_ask")
        assert _exit_side_price(quote, "SELL") == (ref_touch_price("SELL", "SL", **quote), "bid_ask")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
