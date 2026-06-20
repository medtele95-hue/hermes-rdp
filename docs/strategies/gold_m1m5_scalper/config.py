# =====================================================================
#  config.py  —  XAUUSD DEMO scalper configuration
#  DEMO-ONLY. No live trading by default. (no MT5 import here on purpose)
# =====================================================================

SYMBOL          = "XAUUSD"
MAGIC           = 990501
DEMO_ONLY       = True      # hard guard: agent refuses to run on a non-demo account
EXECUTE_TRADES  = False     # analysis-only by default. True = place DEMO orders (hard-gated)

# --- Timeframes (mapped to mt5.TIMEFRAME_* inside mt5_agent/backtest) ---
TF_TREND  = "M5"
TF_ENTRY  = "M1"
M1_BARS   = 500
M5_BARS   = 400

# --- Trend filter (on M5) ---
EMA_TREND = (20, 50, 200)

# --- Micro entry (on M1) ---
EMA_FAST       = 9
EMA_SLOW       = 21
PULLBACK_ATR   = 0.5     # |close - EMA_SLOW| < PULLBACK_ATR * ATR  => pullback
CROSS_LOOKBACK = 3       # EMA9/21 cross must have happened within last N bars
SETUP_WINDOW   = 8       # phases may occur across up to N bars (NOT same candle)

# --- RSI ---
RSI_PERIOD      = 14
RSI_BUY         = (52, 68)
RSI_SELL        = (32, 48)
RSI_BUY_REJECT  = 70
RSI_SELL_REJECT = 30

# --- ATR (volatility band) ---
ATR_PERIOD      = 14
ATR_BAND        = (0.25, 0.85)   # rolling-quantile band; reject too low / too high
ATR_BAND_WINDOW = 200

# --- Fractal swings ---
FRACTAL = 2

# --- Scoring (sum = 100; TRADE if >= MIN_SCORE_TRADE) ---
W_TREND, W_CROSS, W_RSI, W_ATR, W_STRUCT, W_SWEEP, W_SPREAD, W_SESSION = 20, 15, 10, 10, 15, 20, 5, 5
MIN_SCORE_TRADE = 75
MIN_SCORE_WAIT  = 60

# --- SL / TP ---
SL_ATR_MULT  = 0.3
TP1_R        = 1.0
TP2_R        = 1.5
BE_AT_R      = 1.0      # move SL to breakeven at +1R (live trade management)
TRAIL_AFTER_R= 0.8      # start trailing after +0.8R

# --- Spread filter (points) ---
MAX_SPREAD_POINTS = 30
SPREAD_DYN_MULT   = 1.5
SPREAD_AVG_WINDOW = 50

# --- Sessions (UTC hours) ---
LONDON  = (7, 12)
NEWYORK = (12, 21)

# --- Risk management ---
RISK_PCT           = 0.5
MAX_DAILY_LOSS_PCT = 3.0
MAX_DD_PCT         = 10.0
MAX_CONSEC_LOSSES  = 3
MAX_OPEN_TRADES    = 1
COST_R             = 0.02   # backtest per-trade cost in R (spread/slippage proxy)
