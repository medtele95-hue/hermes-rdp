# -*- coding: utf-8 -*-
"""mission/FIX_KILLSWITCH_DATE.md (2026-07-08) — ROOT CAUSE of the reported
bug, found while investigating: this repo had NO test/production log
isolation. Every pytest run wrote real [DAILY_KILLSWITCH] (and everything
else) log lines straight into logs/hermes.log — the SAME file the live bot
writes to and SIMO reads to check production state.

Several tests deliberately use historical/synthetic dates as fixtures (e.g.
tests/test_bloc6_killswitch.py passes now=datetime(2026, 6, 1, 10, 0, ...)
to exercise the router integration path). Empirically reproduced: running
that ONE test writes
    [DAILY_KILLSWITCH] ... window=[2026-05-31T21:00:00+00:00 -> 2026-06-01T12:00:00+00:00]
into logs/hermes.log at the real wall-clock moment the test runs — byte-
for-byte the exact line this mission's bug report quoted. The kill-switch's
own date arithmetic was never wrong (see app/utils/broker_time.py and
app/services/daily_killswitch.py — both audited, both correct, both now
additionally hardened with an anti-regression guard). The apparent "bug"
was test output misread as live production state, made possible only by
this missing isolation.

Setting HERMES_LOG_FILE HERE, at collection time before any test module
(and therefore before app.logger, which reads this exact variable at
import time) is imported, redirects every pytest run to a dedicated test
log file — the production log can no longer be polluted by test fixtures,
present or future.
"""
import os
from pathlib import Path

import pytest

_TEST_LOG_FILE = Path(__file__).resolve().parent / "__tmp_test_hermes.log"
os.environ.setdefault("HERMES_LOG_FILE", str(_TEST_LOG_FILE))

# ── P0-TER (2026-07-14) — MEME CLASSE DE BUG QUE CI-DESSUS, AUTRE FICHIER ────
#
# `app/data/active_symbols.json` est un fichier de config VIVANT : le dashboard
# l'ecrit en production pour restreindre les symboles tradables (toggle sans
# redemarrage), et `demo_router._active_symbols_subset()` le RELIT a chaque appel.
#
# Or plusieurs tests (test_gold_only_invariant, test_coeur_p0) traversent le
# choke-point sans le patcher : ils lisaient donc l'etat REEL du bot. Constate en
# direct pendant cette mission — un toggle dashboard "GOLD seul" a 22:57 a fait
# virer 6 tests au ROUGE alors qu'aucune ligne de code n'avait bouge. L'inverse
# est plus grave encore : un test VERT ne prouvait rien, puisqu'il dependait d'un
# fichier que n'importe quel clic peut changer.
#
# On pointe vers un chemin INEXISTANT : _active_symbols_subset() retombe alors sur
# son defaut documente (fail-open sur SYMBOL_ALLOWLIST complete), qui est l'etat de
# reference que ces tests entendent verifier. Les tests qui veulent piloter ce
# fichier (test_active_symbols_subset, test_dashboard_actions) le patchent
# eux-memes : leur patch, plus interne, gagne.
#
# Le fichier de production n'est ni lu, ni ecrit, ni deplace.
_ABSENT_ACTIVE_SYMBOLS = Path(__file__).resolve().parent / "__tmp_active_symbols_never_created.json"


@pytest.fixture(autouse=True)
def _isolate_active_symbols_file(monkeypatch):
    # Import TARDIF, volontaire : app.logger lit HERMES_LOG_FILE a l'import, et cette
    # variable n'est posee que ci-dessus. Importer demo_router en tete de module
    # re-polluerait le log de production que ce conftest existe justement pour proteger.
    from app.mt5 import demo_router

    monkeypatch.setattr(demo_router, "ACTIVE_SYMBOLS_FILE", _ABSENT_ACTIVE_SYMBOLS)
