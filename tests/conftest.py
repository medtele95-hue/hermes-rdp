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

_TEST_LOG_FILE = Path(__file__).resolve().parent / "__tmp_test_hermes.log"
os.environ.setdefault("HERMES_LOG_FILE", str(_TEST_LOG_FILE))
