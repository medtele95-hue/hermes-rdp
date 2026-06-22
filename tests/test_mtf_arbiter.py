from __future__ import annotations


def test_d1_wins_h4_disagreement():
    from app.mt5.mtf_arbiter import arbitrate_mtf
    result = arbitrate_mtf("BEARISH", "BULLISH", "BEARISH", "BEAR")
    assert result["direction"] == "BULLISH"
    assert result["source"] == "D1"
    assert result["conflict"] is True


def test_h4_wins_when_d1_range():
    from app.mt5.mtf_arbiter import arbitrate_mtf
    result = arbitrate_mtf("BEARISH", "RANGE", "BULLISH", "BULL")
    assert result["direction"] == "BEARISH"
    assert result["source"] == "H4"


def test_range_follows_momentum_before_smc():
    from app.mt5.mtf_arbiter import arbitrate_mtf
    result = arbitrate_mtf("RANGE", "RANGE", "BEARISH", "BULL")
    assert result["direction"] == "BULLISH"
    assert result["source"] == "MOMENTUM"


def test_range_without_momentum_falls_back_to_smc():
    from app.mt5.mtf_arbiter import arbitrate_mtf
    result = arbitrate_mtf("RANGE", "RANGE", "BEARISH", None)
    assert result["direction"] == "BEARISH"
    assert result["source"] == "SMC"


def test_all_unknown_returns_neutral():
    from app.mt5.mtf_arbiter import arbitrate_mtf
    result = arbitrate_mtf("UNKNOWN", "UNKNOWN", "UNKNOWN", None)
    assert result["direction"] == "NEUTRAL"
    assert result["source"] == "NONE"
