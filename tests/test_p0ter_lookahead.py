"""P0-TER — LE TOP-DOWN ET L'ORDER-FLOW NE VOIENT PLUS LE FUTUR.

AUDIT_GEO_MATH.md a prouve que le lecteur top-down et le snapshot order-flow
travaillaient sur la bougie EN COURS (copy_rates_from_pos(...,0,...) -> .iloc[-1]).
Sweeps, BOS, FVG, order blocks, confirmations M15/M1 et le prix qui ancre
entry/SL/TP repeignaient tant que la bougie n'etait pas close — alors que FIX 1
(P0-BIS) a rendu le verdict top-down BLOQUANT.

Ces tests verrouillent la propriete : le verdict ne depend QUE des bougies
CLOTUREES. Chacun porte son controle negatif — on ne veut pas d'un systeme qui
ignore la bougie en cours en devenant sourd aux vrais signaux.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.services.top_down_market_reader import TopDownMarketReader
from app.strategies.gold_order_flow_cvd_vwap import evaluate_reader
from app.strategies.order_flow_execution_agent import _detect_fvg_bonus
from app.utils.candles import closed_frame

START = datetime(2026, 6, 1, tzinfo=timezone.utc)
# Horloge de decision volontairement POSTERIEURE a toutes les bougies des fixtures :
# on isole ainsi le comportement teste (le retrait de la bougie EN COURS) du cutoff
# `decision_time`, qui est un mecanisme distinct (as-of du replay, None en production).
NOW = START + timedelta(hours=6)


# ── outillage ───────────────────────────────────────────────────────────────

def _candle(idx: int, o: float, h: float, low: float, c: float, vol: int = 100) -> dict:
    return {
        "candle_time": START + timedelta(minutes=idx),
        "open": o, "high": h, "low": low, "close": c,
        "spread": 1, "tick_volume": vol,
    }


def _flat(n: int = 130, price: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame([_candle(i, price, price + 0.1, price - 0.1, price) for i in range(n)])


def _append(df: pd.DataFrame, o: float, h: float, low: float, c: float, vol: int = 100) -> pd.DataFrame:
    return pd.concat([df, pd.DataFrame([_candle(len(df), o, h, low, c, vol)])], ignore_index=True)


def _frames(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {tf: df.copy() for tf in ("D1", "H4", "H1", "M15", "M5", "M1")}


# Une bougie EN COURS violemment haussiere : elle casse toute structure plate.
BULL_SPIKE = (100.0, 112.0, 99.9, 110.0)


# ── closed_frame : le helper lui-meme ───────────────────────────────────────

class TestClosedFrame:
    def test_retire_la_derniere_bougie(self):
        df = _flat(5)
        assert len(closed_frame(df)) == 4
        assert closed_frame(df).iloc[-1]["candle_time"] == df.iloc[-2]["candle_time"]

    def test_ne_mutile_pas_l_original(self):
        df = _flat(5)
        closed_frame(df)
        assert len(df) == 5

    def test_entrees_degenerees(self):
        assert closed_frame(None).empty
        assert closed_frame(pd.DataFrame()).empty
        assert len(closed_frame(_flat(1))) == 1  # une seule bougie : on la garde


# ── T-LA1 : un signal visible SEULEMENT sur la bougie en cours ne compte pas ─

class TestLA1_SignalIntraBougie:
    def test_un_trigger_M1_present_uniquement_sur_la_bougie_EN_COURS_ne_declenche_pas(self):
        """Marche plat, puis une bougie EN COURS qui casse tout vers le haut.
        Avant P0-TER : .iloc[-1] la lisait -> m1_trigger=True sur un close qui
        n'existe pas encore. Le trigger M1 est une confirmation DURE du verdict."""
        reader = TopDownMarketReader()
        plat = _flat(130)
        avec_bougie_en_cours = _append(plat, *BULL_SPIKE)

        res = reader.evaluate("EURUSD", _frames(avec_bougie_en_cours), "BUY", 100.0, 99.0, 102.0, 1, 30, NOW)

        assert res["m1_trigger"] is False
        assert res["decision"] != "ALLOW_DEMO"
        assert "M1_ENTRY" in res["missing_confirmations"]

    def test_le_MEME_signal_compte_des_qu_il_a_CLOTURE(self):
        """Controle negatif : on ne rate pas le signal, on l'ATTEND. Une fois la
        bougie clotureee (une nouvelle bougie s'ouvre derriere), le trigger compte."""
        reader = TopDownMarketReader()
        plat = _flat(130)
        spike_cloture = _append(plat, *BULL_SPIKE)
        # une nouvelle bougie s'ouvre : le spike est desormais CLOTURE
        apres_cloture = _append(spike_cloture, 110.0, 110.1, 109.9, 110.0)

        res = reader.evaluate("EURUSD", _frames(apres_cloture), "BUY", 110.0, 109.0, 112.0, 1, 30, NOW)

        assert res["m1_trigger"] is True
        assert "M1_ENTRY" not in res["missing_confirmations"]


# ── T-LA2 : le verdict est STABLE pendant toute la bougie en cours ───────────

class TestLA2_VerdictStable:
    @pytest.mark.parametrize(
        "bougie_en_cours",
        [
            (100.0, 100.1, 99.9, 100.0),    # calme
            (100.0, 112.0, 99.9, 110.0),    # explosion haussiere
            (100.0, 100.1, 88.0, 90.0),     # effondrement
            (100.0, 130.0, 70.0, 100.0),    # range absurde
        ],
    )
    def test_le_verdict_ne_bouge_PAS_quoi_que_fasse_la_bougie_en_cours(self, bougie_en_cours):
        """LE test du repainting : deux evaluations dans la MEME bougie en cours,
        avec des prix intra-bougie radicalement differents, doivent rendre
        EXACTEMENT le meme verdict. Un setup ne peut plus apparaitre puis
        disparaitre pendant qu'il se decide."""
        reader = TopDownMarketReader()
        plat = _flat(130)
        reference = reader.evaluate("EURUSD", _frames(_append(plat, 100.0, 100.05, 99.95, 100.0)),
                                    "BUY", 100.0, 99.0, 102.0, 1, 30, NOW)
        variante = reader.evaluate("EURUSD", _frames(_append(plat, *bougie_en_cours)),
                                   "BUY", 100.0, 99.0, 102.0, 1, 30, NOW)

        for champ in ("decision", "top_down_status", "entry_readiness_score", "bos_choch",
                      "liquidity_sweep", "m15_confirmation", "m1_trigger", "m1_trigger_age",
                      "ob_fvg_present", "m5_context", "premium_discount_zone", "price_location"):
            assert variante[champ] == reference[champ], f"{champ} a repeint avec la bougie en cours"


# ── T-LA3 : controle negatif — un signal legitime passe comme avant ──────────

class TestLA3_ControleNegatif:
    def test_un_setup_legitime_sur_bougies_CLOTUREES_passe_toujours(self):
        """Le fix ne doit pas rendre le lecteur sourd : la suite complete
        (tests/test_top_down_market_reader.py) verifie qu'un setup aligne rend
        toujours ALLOW_DEMO. Ici on verrouille le point precis : le verdict est
        calcule sur les bougies cloturees, PAS absent."""
        from tests.test_top_down_market_reader import aligned_frames

        reader = TopDownMarketReader()
        res = reader.evaluate("EURUSD", aligned_frames("BUY"), "BUY", 123.4, 122.4, 125.6, 1, 30, NOW)

        assert res["decision"] == "ALLOW_DEMO"
        assert res["m15_confirmation"] is True
        assert res["m1_trigger"] is True


# ── T-LA4 : le snapshot order-flow ancre entry/SL/TP sur du CLOTURE ──────────

def _m5_gold(n: int = 80) -> pd.DataFrame:
    """Marche GOLD ordinaire, oscillant autour de 4000, derniere CLOTURE a 4000.00."""
    rows = []
    for i in range(n):
        base = 4000.0 + ((i % 8) - 4) * 0.5
        rows.append(_candle(i, base, base + 1.0, base - 1.0, base, vol=100 + (i % 5) * 10))
    rows[-1] = _candle(n - 1, 4000.0, 4001.0, 3999.0, 4000.00, vol=120)
    return pd.DataFrame(rows)


class TestLA4_SnapshotAncre:
    def test_le_prix_du_snapshot_est_le_close_CLOTURE_pas_le_tick_en_cours(self):
        """`price` du snapshot est l'ancre de entry/SL/TP de ORDER_FLOW (la strategie
        dominante : 107 des 108 ordres v1). Il doit valoir le close de la derniere
        bougie M5 CLOTUREE — valeur reproductible — et non le close en train de bouger."""
        m5 = _m5_gold()
        avec_bougie_en_cours = _append(m5, 4000.0, 4123.45, 3999.0, 4123.45)

        snap = evaluate_reader("GOLD#", {"M5": avec_bougie_en_cours})

        assert snap["price"] == pytest.approx(4000.00)   # et non 4123.45

    def test_les_niveaux_VAH_VAL_qui_fixent_le_SL_ne_repeignent_plus(self):
        """Le SL de ORDER_FLOW est structurel : sl = max(vah, price) + buffer.
        Si vah/val bougent avec la bougie en cours, le SL bouge a chaque tick."""
        m5 = _m5_gold()
        calme = evaluate_reader("GOLD#", {"M5": _append(m5, 4000.0, 4000.5, 3999.5, 4000.0)})
        violent = evaluate_reader("GOLD#", {"M5": _append(m5, 4000.0, 4250.0, 3800.0, 4200.0, vol=99999)})

        assert violent["price"] == calme["price"]
        assert violent["vah"] == calme["vah"]
        assert violent["val"] == calme["val"]
        assert violent["poc"] == calme["poc"]


# ── FVG : le bonus de score ne se laisse plus repeindre ─────────────────────

class TestFVGBonus:
    def test_un_FVG_qui_n_existe_que_sur_la_bougie_en_cours_ne_donne_pas_de_bonus(self):
        # a=[-3] high 100 ; c=[-1] low 105 -> gap haussier... mais c est EN COURS
        rows = [
            _candle(0, 99.0, 100.0, 98.0, 99.5),     # a (cloturee)
            _candle(1, 99.5, 101.0, 99.0, 100.5),    # b (cloturee)
            _candle(2, 106.0, 108.0, 105.0, 107.0),  # c — EN COURS : le gap est ici
        ]
        bonus, mid = _detect_fvg_bonus(pd.DataFrame(rows), "BUY")
        assert (bonus, mid) == (0, None)

    def test_un_FVG_sur_bougies_CLOTUREES_donne_bien_le_bonus(self):
        rows = [
            _candle(0, 99.0, 100.0, 98.0, 99.5),     # a
            _candle(1, 99.5, 101.0, 99.0, 100.5),    # b
            _candle(2, 106.0, 108.0, 105.0, 107.0),  # c — CLOTUREE : gap reel
            _candle(3, 107.0, 107.5, 106.5, 107.0),  # bougie en cours
        ]
        bonus, mid = _detect_fvg_bonus(pd.DataFrame(rows), "BUY")
        assert bonus == 10
        assert mid == pytest.approx((100.0 + 105.0) / 2)
