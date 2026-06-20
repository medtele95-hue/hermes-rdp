# GOLD Order Flow Router Fix

Date: 2026-06-08

## Summary

Fixed the final DEMO routing path for `GOLD_ORDER_FLOW_CVD_VWAP` so BTC-style micro-discovery confluence is observe-only for this strategy and cannot be the final blocker when the strategy's own execution gates pass.

Live safety was not changed. `mt5.order_send` remains confined to `app/mt5/demo_router.py`.

## Files Changed

- `app/config.py`
  - Added `GOLD_ORDER_FLOW_EXECUTION_ENABLED`.
  - Added `GOLD_ORDER_FLOW_MIN_CONFIDENCE`.
  - Added `GOLD_ORDER_FLOW_REQUIRE_DIVERGENCE`.
  - Added `STRICT_GOLD_ORDER_FLOW_TOPDOWN`.

- `.env`
  - Added the GOLD order-flow execution flags with safe DEMO defaults.

- `.env.example`
  - Documented the GOLD order-flow execution flags.

- `app/strategies/gold_order_flow_cvd_vwap.py`
  - Emits full order-flow raw payload for every GOLD cycle, including blocked/WAIT states.
  - Added flat reasoning fields for Supabase `strategy_signals` / `ai_decisions`.
  - Added order-flow diagnostic logs.

- `app/mt5/demo_router.py`
  - Keeps `GOLD_ORDER_FLOW_CVD_VWAP` GOLD-only.
  - Enforces order-flow-specific gates:
    - execution enabled
    - confidence >= 70
    - divergence present when required
    - valid entry/sl/tp
    - valid RR
    - normal DEMO account, symbol, spread, lot, cap, and safety gates
  - Prevents `MICRO_DISCOVERY_CONFLUENCE_TOO_LOW` from being a final blocker for this strategy.
  - Treats SMC/MTFA/top-down failures as warnings unless `STRICT_GOLD_ORDER_FLOW_TOPDOWN=true`.
  - Writes router raw payload with `router_decision` and `demo_gate_reason`.

- `tests/test_gold_order_flow_cvd_vwap_strategy.py`
  - Added proof tests for blocked payload emission.
  - Added proof tests for confidence and divergence gates.
  - Added proof test that low micro-discovery confluence cannot final-block order-flow when execution gates pass.

## Logs Added

- `[ORDER_FLOW] symbol=... POC=... VAH=... VAL=... VWAP=... CVD_SLOPE=... DELTA=... DIVERGENCE=...`
- `[ORDER_FLOW_PAYLOAD_EMITTED] strategy=GOLD_ORDER_FLOW_CVD_VWAP symbol=... status=...`
- Existing `[DEMO_GATE]` / `[ROUTER]` logs now report `GOLD_ORDER_FLOW_CVD_VWAP` PASS/BLOCK reasons from the order-flow gate path.

## Safety Status

- DEMO-only safety unchanged.
- `ALLOW_LIVE_TRADING=false` unchanged.
- Magic number remains `909002`.
- Max lot remains `0.01`.
- No standalone `docs/strategies/order_flow_mt5.py` loop was run or copied.
- No separate MT5 execution path was added.
- `mt5.order_send` production references remain only in `app/mt5/demo_router.py`.

## Test Results

Passed:

```text
C:\Users\Admin\Desktop\mt5_bridge\venv\Scripts\python.exe -m compileall app tests
```

Passed:

```text
C:\Users\Admin\Desktop\mt5_bridge\venv\Scripts\python.exe -m unittest tests.test_gold_order_flow_cvd_vwap_strategy -v
Ran 15 tests in 1.944s
OK
```

Full discovery was attempted twice. It timed out while still progressing through the existing long safety suite and did not report a failure before timeout:

```text
C:\Users\Admin\Desktop\mt5_bridge\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
timeout after 180s

C:\Users\Admin\Desktop\mt5_bridge\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
timeout after 600s
```

Static proof:

```text
rg -n "order_send" app --glob "!app/data/**"
app\mt5\demo_router.py:1934:        result = mt5.order_send(request)
app\mt5\demo_router.py:1979:        result = mt5.order_send(request)
app\mt5\demo_router.py:2025:        result = mt5.order_send(request)
```

## Scenarios Covered

- `GOLD_ORDER_FLOW_CVD_VWAP` runs only on GOLD/XAUUSD symbols.
- BTC/EUR/US100/JP225/ETH are blocked for this strategy.
- No signal without CVD divergence.
- No signal when price is not near POC/VAH/VAL/VWAP.
- Confidence below 70 blocks DEMO routing.
- Divergence `None` blocks DEMO routing.
- Full order-flow raw payload is emitted even when the router blocks execution.
- `MICRO_DISCOVERY_CONFLUENCE_TOO_LOW` cannot be the final blocker for `GOLD_ORDER_FLOW_CVD_VWAP` when order-flow execution gates pass.
- Blocked decisions do not reach `mt5.order_send`.
