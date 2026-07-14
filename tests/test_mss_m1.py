from __future__ import annotations

import math
import pandas as pd
import pytest
from types import SimpleNamespace

from app.strategies.order_flow_execution_agent import _check_mss_m1


# P0-TER (2026-07-14) : _check_mss_m1 lit desormais la derniere bougie CLOTUREE.
# Les frames de production (copy_rates_from_pos(...,0,...)) portent TOUJOURS une
# bougie en cours en derniere ligne : les fixtures la fournissent donc aussi.
# `live=` est cette bougie en cours — elle est volontairement CONTRADICTOIRE avec
# le verdict attendu, ce qui prouve que le verdict ne la lit plus.
LIVE_CONTRADICTS_SELL = (150.0, 149.0, 151.0)  # close/low/high : rien d'un lower low
LIVE_CONTRADICTS_BUY = (50.0, 49.0, 51.0)      # rien d'un higher high


def _m1(closes, lows=None, highs=None, live=None):
    lows = lows or [c - 1.0 for c in closes]
    highs = highs or [c + 1.0 for c in closes]
    closes, lows, highs = list(closes), list(lows), list(highs)
    if live is not None:
        closes.append(live[0])
        lows.append(live[1])
        highs.append(live[2])
    return pd.DataFrame({"close": closes, "low": lows, "high": highs})


# ── 1. Données insuffisantes ────────────────────────────────────────────────

def test_none_df_returns_none():
    assert _check_mss_m1(None, "SELL") is None


def test_empty_df_returns_none():
    assert _check_mss_m1(pd.DataFrame(), "SELL") is None


def test_too_few_candles_returns_none():
    # 5 lignes = 4 clôturées + 1 en cours → moins de 5 clôturées → None
    df = _m1([100.0, 99.0, 98.0, 97.0], live=LIVE_CONTRADICTS_SELL)
    assert _check_mss_m1(df, "SELL") is None


# ── 2. SELL — Lower Low confirmé ────────────────────────────────────────────

def test_sell_mss_confirmed_lower_low():
    # sur les 5 clôturées : lows[-4:-1] = [103, 102, 101] → recent_low = 101
    # dernier close CLÔTURÉ = 95.0 < 101 → True, malgré une bougie en cours à 150.
    closes = [105.0, 104.0, 103.0, 102.0, 95.0]
    lows =   [104.0, 103.0, 102.0, 101.0, 94.0]
    highs =  [106.0, 105.0, 104.0, 103.0, 96.0]
    df = _m1(closes, lows, highs, live=LIVE_CONTRADICTS_SELL)
    assert _check_mss_m1(df, "SELL") is True


def test_sell_mss_not_confirmed_close_above_recent_low():
    # dernier close clôturé = 103.5 > recent_low = 101 → False
    closes = [105.0, 104.0, 103.0, 102.0, 103.5]
    lows =   [104.0, 103.0, 102.0, 101.0, 102.5]
    highs =  [106.0, 105.0, 104.0, 103.0, 104.5]
    df = _m1(closes, lows, highs, live=LIVE_CONTRADICTS_SELL)
    assert _check_mss_m1(df, "SELL") is False


# ── 3. BUY — Higher High confirmé ───────────────────────────────────────────

def test_buy_mss_confirmed_higher_high():
    # dernier close clôturé = 110.0 > recent_high = max(102,103,104) = 104 → True
    closes = [100.0, 101.0, 102.0, 103.0, 110.0]
    lows =   [ 99.0, 100.0, 101.0, 102.0, 109.0]
    highs =  [101.0, 102.0, 103.0, 104.0, 111.0]
    df = _m1(closes, lows, highs, live=LIVE_CONTRADICTS_BUY)
    assert _check_mss_m1(df, "BUY") is True


def test_buy_mss_not_confirmed_close_below_recent_high():
    # dernier close clôturé = 103.0, recent_high = max(102,103,104) = 104 → False
    closes = [100.0, 101.0, 102.0, 103.0, 103.0]
    lows =   [ 99.0, 100.0, 101.0, 102.0, 102.0]
    highs =  [101.0, 102.0, 103.0, 104.0, 104.0]
    df = _m1(closes, lows, highs, live=LIVE_CONTRADICTS_BUY)
    assert _check_mss_m1(df, "BUY") is False


# ── 3bis. P0-TER — le MSS ne se laisse PLUS repeindre ────────────────────────

def test_mss_ignore_un_lower_low_qui_n_existe_que_sur_la_bougie_EN_COURS():
    """LE test du look-ahead : la bougie en cours casse le plus bas récent, mais
    elle n'est pas clôturée. Avant P0-TER, `.iloc[-1]` la lisait et confirmait le
    MSS — un BLOCAGE DUR levé par un close qui pouvait ne jamais exister."""
    closes = [105.0, 104.0, 103.0, 102.0, 103.5]  # clôturées : aucun lower low
    lows =   [104.0, 103.0, 102.0, 101.0, 102.5]
    highs =  [106.0, 105.0, 104.0, 103.0, 104.5]
    live_qui_casse = (90.0, 89.0, 91.0)  # close 90 < min(lows récents) → "MSS !"
    df = _m1(closes, lows, highs, live=live_qui_casse)
    assert _check_mss_m1(df, "SELL") is False  # et non True


def test_mss_confirme_a_la_cloture_suivante_si_le_signal_persiste():
    """Contrôle négatif du précédent : une fois la bougie clôturée, le même MSS
    est bien confirmé. On ne rate pas le signal — on l'attend."""
    closes = [105.0, 104.0, 103.0, 102.0, 103.5, 90.0]  # le 90 a clôturé
    lows =   [104.0, 103.0, 102.0, 101.0, 102.5, 89.0]
    highs =  [106.0, 105.0, 104.0, 103.0, 104.5, 91.0]
    df = _m1(closes, lows, highs, live=LIVE_CONTRADICTS_SELL)
    assert _check_mss_m1(df, "SELL") is True


# ── 4. Intégration dans evaluate() ─────────────────────────────────────────

def _snapshot():
    return {
        "price": 100.0, "vwap": 98.0, "poc": 98.5,
        "vah": 101.0, "val": 96.0,
        "cvd_slope": -1.0, "delta_proxy": -500.0, "divergence": None,
        "created_at": None,
    }


def _settings(**extra):
    ns = SimpleNamespace(
        order_flow_execution_enabled=True,
        order_flow_min_score=0,
        order_flow_min_rr=1.0,
        order_flow_cooldown_minutes=0,
        order_flow_allowed_symbols="",
    )
    for k, v in extra.items():
        setattr(ns, k, v)
    return ns


def test_evaluate_mss_none_sets_m1_entry_confirmation_true():
    from app.strategies.order_flow_execution_agent import evaluate
    frames = {
        "order_flow_snapshot": _snapshot(),
        # No "M1" key → _mss=None → m1_entry_confirmation=True
    }
    result = evaluate("BTCUSD#", frames, settings=_settings())
    if result["status"] == "WAIT":
        pytest.skip("setup not detected with these fixtures")
    assert result["m1_entry_confirmation"] is True
    assert result["m1_trigger_status"] == "PASS"


def test_evaluate_mss_false_score_below_85_returns_wait():
    from app.strategies.order_flow_execution_agent import evaluate
    # Force a SELL setup at VAH: price > vah - prox, delta < 0, divergence=bear
    snap = {
        "price": 101.0, "vwap": 99.0, "poc": 99.5,
        "vah": 101.2, "val": 97.0,
        "cvd_slope": -2.0, "delta_proxy": -600.0, "divergence": "bear",
        "created_at": None,
    }
    # M1 : 5 bougies CLÔTURÉES + 1 en cours (P0-TER). Dernier close clôturé (102)
    # > recent_low (100) → mss=False pour un SELL.
    m1_df = _m1(
        [103.0, 102.0, 101.0, 100.0, 102.0],
        [102.0, 101.0, 100.0,  99.0, 101.0],
        [104.0, 103.0, 102.0, 101.0, 103.0],
        live=LIVE_CONTRADICTS_SELL,
    )
    frames = {"order_flow_snapshot": snap, "M1": m1_df}
    # WEEKEND session → session_ok=False → score=80 (below 85 threshold)
    result = evaluate("BTCUSD#", frames, context={"session_name": "WEEKEND"}, settings=_settings())
    assert result["status"] == "WAIT"
    assert result["reason"] == "ORDER_FLOW_MSS_NOT_CONFIRMED"
