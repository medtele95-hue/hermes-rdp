# HERMES Demo Execution Pipeline — Phase 1 Audit

**Date:** 2026-06-11  
**Phase:** 1 — Audit Only (ZERO code changes)  
**Scope:** Strategy registry, candidate flow, GOLD/EUR NO_ALLOWED_EXECUTION_CANDIDATE root cause, EUR_EMA_RSI_ATR_CROSSOVER signal shape, DEMO_ADAPTIVE_FALLBACK / DEMO_MICRO_DISCOVERY gate analysis, order_send centralization, ORDER_FLOW_READER vs ORDER_FLOW_EXECUTION_AGENT.

---

## Section 0 — Safety Invariants (Re-confirmed)

| Invariant | Status |
|---|---|
| `allow_live_trading=False` always | CONFIRMED — config.py enforces; DemoRouter hard gate |
| `demo_only=True` always | CONFIRMED — DemoRouter hard gate (demo_router.py:1221) |
| `DEMO_MAX_LOT=0.01` never exceeded | CONFIRMED — demo_router.py:1228 caps at `min(demo_max_lot, 0.01)` |
| No second `run_cycle()` loop | CONFIRMED — single loop in main.py |
| `mt5.order_send` only in demo_router.py | CONFIRMED — see §6 |
| Observation strategies never route to execution | CONFIRMED — registry.py + setup_hunter.py policy block |
| ORDER_FLOW_READER never hard-blocks | CONFIRMED — confluence_engine.py:221 "Returns a value in [-5, +5]. Never hard-blocks execution." |
| Confluence engine does not execute | CONFIRMED — confluence_engine.py:7-9 |

---

## 1 — Strategy Registry and Current Mode/Classification

### Files inspected
- `app/strategies/registry.py` (110 lines)
- `app/services/strategy_manager.py` (146 lines)

### registry.py — ACTIVE_EXECUTION_STRATEGIES (`app/strategies/registry.py:19`)

```
SIMO_ATM_BREAKOUT
BTC_SCALPING_AGENT
EUR_EMA_RSI_ATR_CROSSOVER
GOLD_LIQUIDITY_HUNTER_PRO
GOLD_M1_M5_EMA_SWEEP_SCALPER
GOLD_ORDER_FLOW_CVD_VWAP          ← added since last memory snapshot
ORDER_FLOW_EXECUTION_AGENT        ← added since last memory snapshot
```

### registry.py — OBSERVATION_STRATEGIES (`app/strategies/registry.py:31`)

```
BREAKOUT_RETEST, TREND_CONTINUATION_BREAKDOWN, CRT_TBS_REVERSAL,
AMD_FVG_IFVG_REVERSAL, FIB_OTE_RETEST, QUANT_STATISTICAL_PULLBACK,
QUANT_PRO_REGIME_SWITCHING, ORDER_FLOW_READER, EMA_PULLBACK,
SECOND_ENTRY, SCALPING_AGENT
```

### registry.py — Symbol Allow-Lists

| Symbol | Allowed Execution Strategies |
|---|---|
| GOLD# / XAUUSD | SIMO_ATM_BREAKOUT, GOLD_LIQUIDITY_HUNTER_PRO, GOLD_M1_M5_EMA_SWEEP_SCALPER, GOLD_ORDER_FLOW_CVD_VWAP, ORDER_FLOW_EXECUTION_AGENT |
| EURUSD | SIMO_ATM_BREAKOUT, EUR_EMA_RSI_ATR_CROSSOVER, ORDER_FLOW_EXECUTION_AGENT |
| BTCUSD# | SIMO_ATM_BREAKOUT, BTC_SCALPING_AGENT, ORDER_FLOW_EXECUTION_AGENT |

(`app/strategies/registry.py:48-58`)

### strategy_manager.py — Additions vs registry.py

`app/services/strategy_manager.py` mirrors the same ACTIVE_EXECUTION set (7 strategies) and adds to OBSERVATION_ONLY: `TOP_DOWN_MARKET_READER`, `SMC_TAGGER`, `MTFA`, `MTF_STRUCTURE`, `BIG_SETUP_DETECTOR`.

`_DISABLED_STRATEGIES` frozenset (strategy_manager.py:40): `SECOND_ENTRY`, `SCALPING_AGENT` — must never reach DemoRouter.

### Delta vs Previous Memory Snapshot

Memory snapshot was missing two strategies now present in ACTIVE_EXECUTION_STRATEGIES: `GOLD_ORDER_FLOW_CVD_VWAP` and `ORDER_FLOW_EXECUTION_AGENT`. Both are present in both `registry.py` and `strategy_manager.py`. Memory updated separately.

---

## 2 — Candidate Flow for BTC, GOLD, EUR

### Source: `app/agents/setup_hunter.py:55-147`

**Common pre-filter path:**

1. `analysis["strategy_signals"]` provides all raw strategy signals.
2. **GOLD filter** (`setup_hunter.py:68-78`): signals whose strategy is NOT in `ALLOWED_GOLD_EXECUTION_STRATEGIES` are dropped with `skipped_gold_generics++`. Log: `[GOLD_ROUTER] generic_candidates_skipped=N reason=GOLD_GENERIC_STRATEGY_DISABLED`.
3. **EUR filter** (`setup_hunter.py:79-91`): same pattern for `ALLOWED_EUR_EXECUTION_STRATEGIES`. Log: `[EUR_ROUTER] generic_candidates_skipped=N reason=EUR_GENERIC_STRATEGY_DISABLED`.
4. `_ema_confirmation()` builds optional EMA_PULLBACK confirmation boost (+5 points).
5. `_candidate()` builds one candidate dict per remaining signal, calling `SafetyGuard`, `BigSetupDetector`, `_failed_gates()`, `_execution_policy_block_reason()`.
6. Candidates are sorted by role rank (ENTRY=3, CONFIRMATION=2, OBSERVER=1) then edge_score/setup_score descending.
7. `executable_ready` = candidates where `execution_candidate=True AND demo_eligible=True`.

**BTCUSD# path:**
- Strategies considered: BTC_SCALPING_AGENT, SIMO_ATM_BREAKOUT, ORDER_FLOW_EXECUTION_AGENT.
- `BTC_SCALPING_AGENT` bypasses SMC/MTFA/m15/m1/confluence gates when `_btc_scalping_ready()` is True (`setup_hunter.py:892`, variable `btc_scalping_ready`).
- `QUANT_STATISTICAL_PULLBACK` is policy-blocked for BTC with reason `BTC_PULLBACK_DISABLED_PENDING_MATH_AUDIT` (`setup_hunter.py:549`).
- If no `executable_ready` exists, falls to `candidates[0]` (best available, not forced NONE).

**GOLD# path:**
- Strategies considered (after filter): GOLD_LIQUIDITY_HUNTER_PRO, GOLD_M1_M5_EMA_SWEEP_SCALPER, GOLD_ORDER_FLOW_CVD_VWAP, ORDER_FLOW_EXECUTION_AGENT, SIMO_ATM_BREAKOUT.
- GOLD_ORDER_FLOW_CVD_VWAP also requires `gold_order_flow_execution_enabled=True` and required fields present (`setup_hunter.py:186-190`).
- ORDER_FLOW_EXECUTION_AGENT requires `order_flow_execution_enabled=True` (`setup_hunter.py:191-193`).
- If no `executable_ready` → `_empty_candidate(symbol, ..., "NO_ALLOWED_EXECUTION_CANDIDATE")` (`setup_hunter.py:119-120`).

**EURUSD path:**
- Strategies considered (after filter): EUR_EMA_RSI_ATR_CROSSOVER, SIMO_ATM_BREAKOUT, ORDER_FLOW_EXECUTION_AGENT.
- Same `NO_ALLOWED_EXECUTION_CANDIDATE` pattern as GOLD if no executable_ready exists.

---

## 3 — Root Cause: GOLD/EUR Return best=NONE / NO_ALLOWED_EXECUTION_CANDIDATE

### Key code path (`app/agents/setup_hunter.py:117-122`)

```python
if executable_ready:
    best = executable_ready[0]
elif policy_controlled or gold_symbol or _is_eur_symbol(symbol, broker_symbol):   # line 119
    best = self._empty_candidate(symbol, broker_symbol, base_decision, time_gate, "NO_ALLOWED_EXECUTION_CANDIDATE")
else:
    best = candidates[0] if candidates else self._empty_candidate(...)
```

**Root cause:** For any GOLD or EUR symbol, the code takes the `elif` branch whenever `executable_ready` is empty. BTC does not take this branch; it falls to `candidates[0]` if available. This is intentional: GOLD and EUR must have a fully eligible candidate (all gates passed) or they return NONE. BTC can surface a "best available" candidate even when blocked.

**What makes `executable_ready` empty for GOLD/EUR:**

A candidate is `demo_eligible=True` only if `_failed_gates()` returns `[]` AND `executable=True`. The gates that most commonly block GOLD/EUR:

| Gate | Condition | File:Line |
|---|---|---|
| `SMC_SCORE_LT_70` | smc_score < 70 (unless strategy bypasses) | `setup_hunter.py:912` |
| `WAITING_FOR_SMC_PASS` | smc_status != "PASS" | `setup_hunter.py:914` |
| `MTFA_SCORE_LT_60` | mtfa_score < 60 | `setup_hunter.py:922` |
| `WAITING_FOR_M15_CONFIRMATION` | m15_confirmation=False | `setup_hunter.py:931` |
| `WAITING_FOR_M1_TRIGGER` | m1_entry_confirmation=False | `setup_hunter.py:933` |
| `RR_TOO_LOW` | rr < 1.5 | `setup_hunter.py:843` |
| `ORDER_FLOW_EXECUTION_DISABLED` | order_flow_execution_enabled=False | `setup_hunter.py:193` |
| `ORDER_FLOW_ENTRY_DISABLED` | gold_order_flow_execution_enabled=False | `setup_hunter.py:186` |

GOLD_LIQUIDITY_HUNTER_PRO, GOLD_M1_M5_EMA_SWEEP_SCALPER have their own per-strategy score thresholds (`gold_min_liquidity_score`, `gold_m1m5_min_score_strict/relaxed`). These bypass confluence but require their own score gate.

**Summary:** GOLD/EUR return `NO_ALLOWED_EXECUTION_CANDIDATE` when either (a) no strategy signal is present in the allowed set, or (b) all present signals have at least one failing gate (SMC, MTFA, m15/m1 trigger, score threshold, RR, or feature flag disabled).

---

## 4 — EUR_EMA_RSI_ATR_CROSSOVER: Signal Shape Audit

### File: `app/services/eur_ema_rsi_atr_strategy.py`

**Finding: emits a FULL candidate, not a bare BUY/SELL decision.**

When the strategy fires (EMA cross detected, RSI filter passed, RR valid), `_payload()` returns a rich outer dict containing:

| Field | Value when BUY/SELL | Value when blocked/WAIT |
|---|---|---|
| `signal` | `"BUY"` or `"SELL"` | `"WAIT"` |
| `direction` | `"BUY"` or `"SELL"` | `"WAIT"` |
| `entry` | ATR-computed price | `None` |
| `sl` | ATR-computed level | `None` |
| `tp` | ATR-computed level | `None` |
| `risk_reward` / `reward_risk` | computed RR | `None` |
| `confidence` | `1.0` | `0.0` |
| `m15_confirmation` | `True` | `False` |
| `m1_entry_confirmation` | `True` | `False` |
| `big_setup_grade` / `grade` | `"A"` | `"D"` |
| `eur_ema_rsi_atr.decision` | `"BUY"` or `"SELL"` or `"BLOCK"` | `"WAIT"` |

**`eur_ema_rsi_atr.decision`** (nested dict, line 273) can be `"BLOCK"` (invalid RR case, line 115). But the **outer** `signal`/`direction` fields are always mapped through `_payload()` line 265: `direction = decision if decision in {"BUY", "SELL"} else "WAIT"`. Thus the outer signal is never "BLOCK".

**Implication for setup_hunter.py:** `raw_signal` reads `signal.get("signal")` which is `"BUY"` or `"SELL"` or `"WAIT"`. The `"BLOCK"` value is invisible to SetupHunter. When the nested `decision="BLOCK"`, the outer `signal="WAIT"` → `demo_eligible=False` → `NO_ALLOWED_EXECUTION_CANDIDATE`.

**Conclusion:** EUR_EMA_RSI_ATR_CROSSOVER correctly emits a full candidate. The nested `decision` field may say "BLOCK" but SetupHunter only reads the outer `signal` field which is always sanitized to BUY/SELL/WAIT. No bug here, but the dual-field pattern (inner `decision` vs outer `signal`) could be a source of confusion.

---

## 5 — DEMO_ADAPTIVE_FALLBACK / DEMO_MICRO_DISCOVERY Gate Analysis

### Source: `app/mt5/demo_router.py`

Both modes are opt-in via config flags. Neither is the default path.

### DEMO_ADAPTIVE_FALLBACK

**Enable flag:** `hermes_demo_topdown_fallback_mode` (default: False)  
**Primary method:** `_demo_topdown_fallback_review()` — `demo_router.py:1151`  
**Mode string set at:** `demo_router.py:502`

**Gate behavior:**

| Gate | Status |
|---|---|
| Top-down `AVOID` | Hard-blocks → `TOP_DOWN_READER_BLOCK` |
| MT5 connected, DEMO account, lot cap (≤0.01), spread, market open, SL/TP, RR, trade caps | All ENFORCED via `_demo_topdown_fallback_hard_block_reason()` (demo_router.py:1211) |
| SMC STRONG FAIL (smc_status=FAIL AND smc_score<50) | BLOCKS → `SMC_STRONG_FAIL` |
| MTFA STRONG FAIL (mtfa_status=FAIL AND mtfa_score<50) | BLOCKS → `MTFA_STRONG_FAIL` |
| Adaptive confluence (hermes_adaptive_confluence_enabled required) | BLOCKS if not enabled or score below threshold/55 |
| m15 OR m1 trigger confirmation | BLOCKS → `NO_ENTRY_TRIGGER_CONFIRMATION` if neither present (GOLD: unless score ≥ 65) |
| SMC soft fail (score 50-69, status≠PASS) | **BYPASSED** → warning only (`SMC_FAIL_WARNING`) |
| MTFA soft fail (score 50-59, status≠PASS) | **BYPASSED** → warning only (`MTFA_FAIL_WARNING`) |
| Top-down WAIT / FAIL (non-AVOID) | **BYPASSED** → warning only |
| Confluence < threshold but SMC/MTFA soft-ok | **BYPASSED** via adaptive_confluence path |

**Summary:** DEMO_ADAPTIVE_FALLBACK bypasses soft SMC/MTFA failures and top-down WAIT/FAIL states. It does NOT bypass: hard safety gates, top-down AVOID, SMC/MTFA strong failures (score<50), adaptive confluence score gate, m15/m1 trigger gate.

### DEMO_MICRO_DISCOVERY

**Enable flag:** `hermes_demo_micro_discovery_mode` (default: False)  
**Primary method:** `_demo_micro_discovery_review()` — `demo_router.py:1305`  
**Mode string set at:** `demo_router.py:505`

**Gate behavior vs DEMO_ADAPTIVE_FALLBACK:**

| Gate | FALLBACK | MICRO_DISCOVERY |
|---|---|---|
| Hard gates (MT5/DEMO/lot/spread/market/SL/RR/caps) | ENFORCED | ENFORCED |
| SMC STRONG FAIL | BLOCKS | **ALLOWED** (demo_router.py:1365) |
| MTFA STRONG FAIL | BLOCKS | **ALLOWED** (demo_router.py:1366) |
| Top-down AVOID | BLOCKS | BLOCKS |
| Adaptive confluence required | Yes, ≥threshold AND ≥55 | Replaced by final_confluence_score ≥ 60 (demo_router.py:1374) |
| SMC minimum threshold | n/a | EURUSD: ≥50; GOLD/BTC: ≥40 (demo_router.py:1390-1400) |
| MTFA minimum threshold | n/a | EURUSD: ≥50; GOLD/BTC: ≥35 (demo_router.py:1397-1400) |
| m15 OR m1 trigger | BLOCKS | BLOCKS (same check) |

**Does DEMO_MICRO_DISCOVERY bypass confluence, SMC, MTFA, or top-down gates?**

- **Top-down gate:** `AVOID` still blocks. WAIT/FAIL does NOT block (no check vs AVOID).
- **Confluence gate:** NOT fully bypassed. Requires `final_confluence_score ≥ 60`.
- **SMC gate:** STRONG FAIL (status=FAIL AND score<50) is allowed through. Soft fail also allowed. Minimum score still applied (40/50).
- **MTFA gate:** Same — strong fail allowed through, minimum (35/50) still applied.
- **m15/m1 trigger:** NOT bypassed.

**Does DEMO_ADAPTIVE_FALLBACK bypass confluence, SMC, MTFA, or top-down gates?**

- **Top-down gate:** `AVOID` still blocks.
- **Confluence gate:** Requires `hermes_adaptive_confluence_enabled` + score ≥ symbol_threshold + ≥ 55.
- **SMC gate:** Strong fail (<50) blocks. Soft fail allowed (warning).
- **MTFA gate:** Same as SMC.
- **m15/m1 trigger:** NOT bypassed.

---

## 6 — order_send Location Verification

### Command run
```
rg -n "order_send" app tests --glob "!app/data/**"
```

### Production files (`app/`) — ALL occurrences in `app/mt5/demo_router.py` only

| File | Line | Context |
|---|---|---|
| `app/mt5/demo_router.py` | 2035 | `result = mt5.order_send(request)` — main _send_order() |
| `app/mt5/demo_router.py` | 2159 | `result = mt5.order_send(request)` — pending order send |
| `app/mt5/demo_router.py` | 2205 | `result = mt5.order_send(request)` — quick exit |
| `app/mt5/demo_router.py` | 2251 | `result = mt5.order_send(request)` — position modify |
| `app/mt5/demo_router.py` | 3969 | `result = mt5.order_send(remove_request)` — SIMO_ATM pending removal |

**No other production `app/` file contains `mt5.order_send`.**

### Reference/docs files (not production)
- `docs/strategies/scalp_quick_exit_mt5.py:64,143` — reference strategy, not imported
- `docs/strategies/order_flow_mt5.py:405` — reference strategy, not imported
- `docs/strategies/gold_m1m5_scalper/mt5_agent.py:80` — reference strategy, not imported

### Test files (patch-only, no direct calls)
All test occurrences are `patch("app.mt5.demo_router.mt5.order_send", ...)` mock patches, not direct calls. Two safety tests (`test_strategy_registry_full.py:356`, `test_simo_atm_breakout_integration.py:143`, `test_ingest_circuit_breaker.py:246`) verify the centralization invariant programmatically.

**SAFETY INVARIANT CONFIRMED:** `mt5.order_send` exists only in `app/mt5/demo_router.py`.

---

## 7 — ORDER_FLOW_READER vs ORDER_FLOW_EXECUTION_AGENT

### ORDER_FLOW_READER

| Property | Value |
|---|---|
| Registry mode | `OBSERVATION_ONLY` (`app/strategies/registry.py:40`) |
| `route_allowed` | `False` (`strategy_manager.py:143`) |
| Confluence role | ±5 bonus only — `_order_flow_bonus()` in `app/agents/confluence_engine.py:218` |
| Hard-block capability | **None** — returns `[-5, +5]`, never a block |
| Current behavior | Builds reader snapshot via `evaluate_reader()` in `gold_order_flow_cvd_vwap.py:68`. Emits `ORDER_FLOW_READER_OBSERVE_ONLY` status. Never routes to execution. |
| Feature flag | `order_flow_reader_enabled` (default: True) — disabling returns `ORDER_FLOW_READER_DISABLED` snapshot |

**ORDER_FLOW_READER behavior in practice:** When `gold_order_flow_execution_enabled=False` (the default), `gold_order_flow_cvd_vwap.evaluate()` calls `evaluate_reader()` then returns a `_reader_signal_from_snapshot()` dict with `signal="WAIT"` and `order_flow_entry_enabled=False`. The reader snapshot is observable data only.

### ORDER_FLOW_EXECUTION_AGENT

| Property | Value |
|---|---|
| Registry mode | `ACTIVE_EXECUTION` (`app/strategies/registry.py:27`) |
| `route_allowed` | `True` |
| Feature flag | `order_flow_execution_enabled` (default: **False**) |
| Current status | **DISABLED by default.** Returns `_wait("ORDER_FLOW_EXECUTION_DISABLED")` when flag is False (`app/strategies/order_flow_execution_agent.py:31-33`) |
| Allowed symbols | BTCUSD, BTCUSD#, GOLD, GOLD#, XAUUSD, EURUSD (ALLOWED_SYMBOLS_DEFAULT) |
| When enabled | Emits full candidate with `order_flow_execution_agent_score`, `direction`, `entry`, `sl`, `tp`. Min score: `order_flow_min_score` (default 75). Min RR: `order_flow_min_rr` (default 1.5). |
| Policy block in setup_hunter | `ORDER_FLOW_EXECUTION_DISABLED` added to `failed` when `order_flow_execution_enabled=False` (`setup_hunter.py:191-195`) |

**Key distinction:** ORDER_FLOW_READER observes and enriches. ORDER_FLOW_EXECUTION_AGENT is a gated entry strategy that is currently inactive (flag defaults to False) and would require explicit opt-in to trade.

---

## 8 — Lovable Ingest / Trade Sync Issues

**OUT OF SCOPE** per Phase 1 audit instructions. No analysis performed on `app/services/ingest_client.py`, `test_ingest_circuit_breaker.py`, or Supabase schema.

---

## Files Inspected

| File | Lines | Purpose |
|---|---|---|
| `app/strategies/registry.py` | 110 | Strategy classification, allow-lists |
| `app/services/strategy_manager.py` | 146 | SIMO discovery, mode/route helpers |
| `app/agents/setup_hunter.py` | 1019 | Candidate evaluation, filtering, scoring |
| `app/services/eur_ema_rsi_atr_strategy.py` | 377 | EUR strategy signal shape |
| `app/strategies/order_flow_execution_agent.py` | ~382 | ORDER_FLOW_EXECUTION_AGENT behavior |
| `app/strategies/gold_order_flow_cvd_vwap.py` | ~365 | GOLD_ORDER_FLOW and ORDER_FLOW_READER |
| `app/agents/confluence_engine.py` | ~242 | ORDER_FLOW_READER ±5 bonus rule |
| `app/mt5/demo_router.py` | 3969+ | DEMO_ADAPTIVE_FALLBACK, DEMO_MICRO_DISCOVERY, order_send |
| `app/agents/hermes_5min_agent.py` | ~265 | Strategy signal aggregation |
| `tests/test_strategy_registry_full.py` | 366+ | order_send location tests |
| `tests/test_simo_atm_breakout_integration.py` | 150 | order_send invariant test |
| `tests/test_ingest_circuit_breaker.py` | 254 | order_send invariant test |
| `docs/strategies/scalp_quick_exit_mt5.py` | 145 | Reference only (docs/) |
| `docs/strategies/order_flow_mt5.py` | 405+ | Reference only (docs/) |
| `docs/strategies/gold_m1m5_scalper/mt5_agent.py` | 80+ | Reference only (docs/) |

---

## Compile Check

```
python -m compileall app tests
```

All files compiled successfully. No syntax errors detected in `app/` or `tests/`.

---

## Summary of Findings

| # | Finding | Severity |
|---|---|---|
| 1 | Memory snapshot was stale — GOLD_ORDER_FLOW_CVD_VWAP and ORDER_FLOW_EXECUTION_AGENT are now in ACTIVE_EXECUTION | Info |
| 2 | GOLD/EUR NO_ALLOWED_EXECUTION_CANDIDATE is by design (setup_hunter.py:119) — forced when `executable_ready` empty | Confirmed |
| 3 | EUR_EMA_RSI_ATR_CROSSOVER emits full candidate; inner `decision` field can be "BLOCK" but outer `signal` is always BUY/SELL/WAIT | Info |
| 4 | DEMO_ADAPTIVE_FALLBACK bypasses soft SMC/MTFA failures; hard gates and AVOID remain intact | Documented |
| 5 | DEMO_MICRO_DISCOVERY additionally bypasses SMC/MTFA STRONG FAIL; confluence minimum (60) still required | Documented |
| 6 | order_send exclusively in app/mt5/demo_router.py (5 call sites) — invariant holds | PASS |
| 7 | ORDER_FLOW_READER is observation-only ±5 bonus; ORDER_FLOW_EXECUTION_AGENT is disabled by default | Confirmed |
| 8 | Lovable ingest/trade sync issues deferred as out of scope | Out of scope |

---

## Zero-Code-Change Confirmation

**No application code was modified during this audit.** All findings are based on read-only file inspection and `python -m compileall` (compile-check only, no bytecode retained). The strategy registry, DemoRouter, Supabase schema, and frontend are unchanged.

## Safety Confirmation

All eight Section 0 safety invariants are confirmed intact. Live trading remains disabled. `order_send` remains centralized in `app/mt5/demo_router.py`. No observation strategy routes to execution. ORDER_FLOW_EXECUTION_AGENT defaults to disabled.
