"""P0-TER FIX 2 — LE WRITER N'EMPOISONNE PLUS LA CALIBRATION.

Trois poisons prouves par AUDIT_GEO_MATH, tous dans le pur ENREGISTREMENT
(zero effet decisionnel — mais c'est ce dataset qui calibrera la v2) :

  1. les sorties VIRTUELLES etaient labellisees au MID -> un demi-spread de biais
     OPTIMISTE des deux cotes (TP touche trop tot, SL evite trop tard) : le dataset
     sur-etiquette les WIN. GOLD 0,15 ; BTC 11,25.
  2. `atr_percentile` etait un SMA alors que les 14 sites ATR du coeur sont en
     Wilder : le percentile de regime classait une serie d'une autre nature.
  3. `spread_to_atr` etait mort (None sur 9 953/9 953) parce que `atr` etait mort
     (None sur 10 605/10 605) — et la formule portait un piege d'unite x100 latent.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.services.decision_dataset import (
    CORE_FIX_LEVEL,
    DecisionDataset,
    OutcomeTracker,
    _exit_side_price,
    atr_percentile,
    atr_percentile_wilder,
    build_decision_row,
)

START = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _candles(n: int, base: float, spread_points: float = 30.0) -> pd.DataFrame:
    rows = []
    for i in range(n):
        px = base + ((i % 7) - 3) * (base * 0.0005)
        rows.append({
            "candle_time": START + timedelta(minutes=5 * i),
            "open": px, "high": px + base * 0.001, "low": px - base * 0.001, "close": px,
            "spread": spread_points, "tick_volume": 100,
        })
    return pd.DataFrame(rows)


def _tracker(tmp_path, direction: str, entry: float, sl: float, tp: float) -> OutcomeTracker:
    """Un refus VIRTUEL en suivi : c'est la population labellisee par simulation
    TP/SL (les positions REELLES, elles, sont labellisees par les deals MT5)."""
    tracker = OutcomeTracker(DecisionDataset(path=tmp_path / "d.jsonl"))
    tracker.register({
        "row_type": "decision", "symbol": "GOLD#", "broker_symbol": "GOLD#",
        "direction": direction, "entry": entry, "sl": sl, "tp": tp,
        "order_success": False, "recorded_at": START.isoformat(),
    })
    return tracker


# ── T-W1 : la touche TP/SL est evaluee du BON cote du spread ────────────────

class TestW1_LabelBidAsk:
    def test_le_bon_cote_du_spread(self):
        # BUY : on se solde au BID. SELL : on rachete a l'ASK.
        assert _exit_side_price({"bid": 99.0, "ask": 99.3}, "BUY") == (99.0, "bid_ask")
        assert _exit_side_price({"bid": 99.0, "ask": 99.3}, "SELL") == (99.3, "bid_ask")

    def test_un_float_reste_accepte_et_se_declare_honnetement_mid(self):
        assert _exit_side_price(100.0, "BUY") == (100.0, "mid")

    def test_LE_test_le_mid_disait_RIEN_le_spread_reel_dit_LOSS(self, tmp_path):
        """LE test de la mission. SELL GOLD, SL a 4010. Le MID (4009.925) n'a pas
        touche le SL -> l'ancien writer ne voyait rien et laissait courir. L'ASK
        (4010.15), lui, l'a bel et bien touche : un SELL se rachete a l'ask. C'est
        une PERTE, et le dataset doit l'ecrire."""
        tracker = _tracker(tmp_path, "SELL", entry=4000.0, sl=4010.0, tp=3985.0)

        closed = tracker.update({"GOLD#": {"bid": 4009.7, "ask": 4010.15}}, now_utc=START)

        assert len(closed) == 1
        assert closed[0]["outcome"] == "SL_HIT"
        assert closed[0]["label_method"] == "bid_ask"
        assert closed[0]["close_price"] == pytest.approx(4010.15)  # l'ask, pas le mid

    def test_le_MID_aurait_laisse_passer_la_meme_perte(self, tmp_path):
        """Controle du controle : avec l'ANCIEN comportement (un float = le mid),
        la meme cotation ne declenche RIEN. C'est la preuve que le fix change bien
        quelque chose de reel, et pas seulement une etiquette."""
        tracker = _tracker(tmp_path, "SELL", entry=4000.0, sl=4010.0, tp=3985.0)

        mid = (4009.7 + 4010.15) / 2.0  # 4009.925 < 4010 : le SL parait intact
        closed = tracker.update({"GOLD#": mid}, now_utc=START)

        assert closed == []

    def test_controle_negatif_un_vrai_TP_reste_un_TP(self, tmp_path):
        """Le fix ne rend pas le writer aveugle aux gains : un TP franchement touche
        du bon cote reste un TP_HIT."""
        tracker = _tracker(tmp_path, "SELL", entry=4000.0, sl=4010.0, tp=3985.0)

        closed = tracker.update({"GOLD#": {"bid": 3984.0, "ask": 3984.3}}, now_utc=START)

        assert closed[0]["outcome"] == "TP_HIT"
        assert closed[0]["close_price"] == pytest.approx(3984.3)  # l'ask

    def test_un_BUY_se_solde_au_bid(self, tmp_path):
        tracker = _tracker(tmp_path, "BUY", entry=4000.0, sl=3990.0, tp=4015.0)

        # l'ask (3990.15) n'a pas touche le SL, mais le BID (3989.9) si :
        # un BUY se revend au bid.
        closed = tracker.update({"GOLD#": {"bid": 3989.9, "ask": 3990.15}}, now_utc=START)

        assert closed[0]["outcome"] == "SL_HIT"
        assert closed[0]["close_price"] == pytest.approx(3989.9)


# ── T-W2 : atr_percentile en Wilder, distinct du SMA ────────────────────────

class TestW2_AtrWilder:
    def test_wilder_et_SMA_ne_donnent_PAS_le_meme_percentile(self):
        """Preuve numerique que la migration change reellement la mesure : une serie
        avec un choc de volatilite. Wilder garde la memoire du choc (lissage
        exponentiel), le SMA l'oublie des qu'il sort de la fenetre."""
        rows = []
        for i in range(120):
            amp = 40.0 if i == 60 else 1.0  # un choc unique au milieu
            rows.append({
                "candle_time": START + timedelta(minutes=5 * i),
                "open": 4000.0, "high": 4000.0 + amp, "low": 4000.0 - amp, "close": 4000.0,
                "spread": 30.0, "tick_volume": 100,
            })
        df = pd.DataFrame(rows)

        sma_pct = atr_percentile(df)
        wilder_pct = atr_percentile_wilder(df)

        assert sma_pct is not None and wilder_pct is not None
        assert wilder_pct != sma_pct

    def test_le_writer_ecrit_wilder_et_PLUS_le_SMA(self):
        """Les deux colonnes ne doivent JAMAIS coexister : melanger deux definitions
        de l'ATR dans le meme dataset le rendrait inexploitable."""
        row = build_decision_row({"symbol": "GOLD#"}, extras={"frames": {"M5": _candles(120, 4000.0)}})

        assert row["regime.atr_percentile_wilder"] is not None
        assert "regime.atr_percentile" not in row

    def test_le_percentile_ignore_la_bougie_EN_COURS(self):
        """Un percentile de regime calcule sur une bougie qui bouge encore n'est pas
        reproductible : deux enregistrements dans la meme bougie donneraient deux
        regimes differents."""
        base = _candles(120, 4000.0)

        def _avec(o, h, low, c):
            return pd.concat([base, pd.DataFrame([{
                "candle_time": START + timedelta(minutes=5 * 120),
                "open": o, "high": h, "low": low, "close": c,
                "spread": 30.0, "tick_volume": 100,
            }])], ignore_index=True)

        violente = _avec(4000.0, 4200.0, 3800.0, 4150.0)
        calme = _avec(4000.0, 4000.5, 3999.5, 4000.0)

        assert atr_percentile_wilder(violente) == atr_percentile_wilder(calme)


# ── T-W3 : spread_to_atr alimente, avec la BONNE unite ──────────────────────

class TestW3_SpreadToAtr:
    def test_la_conversion_points_vers_prix_se_fait_bien(self):
        """LE piege d'unite x100. GOLD : spread 30 POINTS, point = 0.01 -> 0,30 en
        PRIX. Le ratio doit se calculer sur 0,30 — pas sur 30 (ce qui donnerait un
        facteur 100 selon la cle disponible dans l'evenement)."""
        row = build_decision_row(
            {"symbol": "GOLD#", "spread_at_send_points": 30.0, "symbol_specs": {"point": 0.01}},
            extras={"frames": {"M5": _candles(120, 4000.0)}},
        )

        assert row["spread_points"] == 30.0
        assert row["spread_source"] == "AT_SEND"
        assert row["atr"] is not None and row["atr"] > 0
        assert row["spread_to_atr"] == pytest.approx(0.30 / row["atr"])
        assert row["spread_to_atr"] < 1.0  # ordre de grandeur : PAS le x100

    def test_l_atr_n_est_plus_une_colonne_morte(self):
        """`atr` etait None sur 10 605/10 605 lignes v1 : l'evenement ne l'a jamais
        porte. Le writer le CALCULE desormais (Wilder, M5 cloturees)."""
        row = build_decision_row({"symbol": "GOLD#"}, extras={"frames": {"M5": _candles(120, 4000.0)}})

        assert row["atr"] is not None
        assert row["atr"] > 0

    def test_une_decision_REFUSEE_a_quand_meme_un_spread(self):
        """Une decision refusee n'a pas de `spread_at_send_points` (rien n'a ete
        envoye). On retombe sur le spread de la derniere bougie CLOTUREE, et la
        provenance est ecrite noir sur blanc — pas de melange silencieux de sources."""
        row = build_decision_row(
            {"symbol": "GOLD#", "symbol_specs": {"point": 0.01}},
            extras={"frames": {"M5": _candles(120, 4000.0, spread_points=42.0)}},
        )

        assert row["spread_points"] == 42.0
        assert row["spread_source"] == "M5_CANDLE"
        assert row["spread_to_atr"] is not None

    def test_sans_point_broker_on_n_invente_pas(self):
        """Pas de `point` -> pas de conversion possible -> None. On refuse de deviner :
        deviner est exactement ce qui aurait produit le facteur x100."""
        row = build_decision_row(
            {"symbol": "GOLD#", "spread_at_send_points": 30.0},
            extras={"frames": {"M5": _candles(120, 4000.0)}},
        )

        assert row["spread_points"] == 30.0
        assert row["spread_to_atr"] is None


# ── borne temporelle de la collecte ─────────────────────────────────────────

def test_les_lignes_portent_la_borne_core_fix_level():
    """Marqueur machine de la bascule : `core_fix_level` absent = v2 pre-P0-TER
    (decisions repeintes, labels au mid) ; present = systeme veridique."""
    row = build_decision_row({"symbol": "GOLD#"}, extras={"frames": {"M5": _candles(120, 4000.0)}})

    assert row["core_fix_level"] == CORE_FIX_LEVEL == "P0TER"
