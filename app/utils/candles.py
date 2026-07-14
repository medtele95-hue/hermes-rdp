"""Bougie en cours : le helper partage (P0-TER, 2026-07-14).

MT5DataReader.get_candles() appelle copy_rates_from_pos(symbol, tf, 0, count).
La position 0 est la bougie EN COURS DE FORMATION. Elle est donc la DERNIERE
ligne de tous les frames que ce repo fait circuler, et son OHLC bouge a chaque
tick jusqu'a la cloture.

Tout verdict calcule sur cette ligne REPEINT : un sweep/BOS/FVG peut apparaitre
a 09:03:20 et avoir disparu a la cloture de 09:05 — l'ordre etant deja parti.
AUDIT_GEO_MATH.md l'a chiffre : le close M5 GOLD bouge encore de ~3,9 pts
(mediane, 8,1 en p90) pendant la bougie, contre une bande de detection de
setup de ~10 pts. Le bruit est du meme ordre que le signal.

closed_frame() retire cette ligne. C'est la version partagee du helper
`_closed_frame` que gold_liquidity_hunter, gold_m1m5, fib_confluence,
wsp_overlay et quant_statistical_pullback portaient chacun en prive — et
avaient tous ecrit correctement. Le top-down et l'order-flow, eux, ne
l'avaient pas.
"""
from __future__ import annotations

from typing import Any

import pandas as pd


def closed_frame(value: Any) -> pd.DataFrame:
    """Renvoie `value` prive de sa bougie EN COURS (la derniere ligne).

    Une entree vide/inutilisable donne un frame vide. Une entree d'UNE seule
    ligne est renvoyee telle quelle : retirer la seule bougie disponible
    donnerait un frame vide, et tous les appelants gardent deja une longueur
    minimale (>= 3 a >= 100 selon le site) — ils rejetteront d'eux-memes.
    """
    if value is None:
        return pd.DataFrame()
    df = value if isinstance(value, pd.DataFrame) else pd.DataFrame(value)
    if df.empty:
        return pd.DataFrame()
    if len(df) <= 1:
        return df.copy()
    return df.iloc[:-1].copy()
