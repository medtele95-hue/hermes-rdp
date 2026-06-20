//+------------------------------------------------------------------+
//|                                              FibConfluence.mq5    |
//|        Fibonacci Golden-Zone signals filtered by confluence:     |
//|        Market Structure + Volume + Liquidity + Order-Flow proxy  |
//|                                                                  |
//|  NOTE: This is a decision-support tool, NOT a guaranteed system. |
//|  "Order flow" here is a PROXY (close position in candle range),  |
//|  not real footprint/delta data. Volume = TICK volume unless your |
//|  broker provides real exchange volume. Backtest before using.    |
//+------------------------------------------------------------------+
#property copyright "FibConfluence"
#property version   "1.00"
#property description "Fibonacci golden-pocket signals confirmed by structure, volume, liquidity sweep & order-flow proxy."
#property indicator_chart_window
#property indicator_buffers 2
#property indicator_plots   2

//--- Buy arrow
#property indicator_label1  "Buy"
#property indicator_type1   DRAW_ARROW
#property indicator_color1  clrDeepSkyBlue
#property indicator_width1  2
//--- Sell arrow
#property indicator_label2  "Sell"
#property indicator_type2   DRAW_ARROW
#property indicator_color2  clrOrangeRed
#property indicator_width2  2

//============================= INPUTS ==============================
input group "=== Swing / Fibonacci ==="
input int     InpLookback      = 30;     // Swing detection lookback (bars)
input double  InpZoneTop       = 0.618;  // Golden zone start (0.618)
input double  InpZoneBottom    = 0.786;  // Golden zone end   (0.786)
input bool    InpDrawFibo      = true;   // Draw the Fibonacci object
input bool    InpDrawZone      = true;   // Shade the golden pocket

input group "=== Confluence filters (each is +1 score) ==="
input bool    InpUseStructure  = true;   // Market structure (HH/HL vs LH/LL)
input bool    InpUseVolume     = true;   // Volume spike
input bool    InpUseLiquidity  = true;   // Liquidity sweep
input bool    InpUseOrderFlow  = true;   // Order-flow PROXY (close in range)
input int     InpMinConfluence = 2;      // Min confirmations required (0-4)

input group "=== Filter parameters ==="
input int     InpStructWindow  = 15;     // Window for structure comparison
input int     InpVolPeriod     = 20;     // Volume average period
input double  InpVolMult       = 1.5;    // Volume spike multiplier
input int     InpLiqLookback   = 10;     // Lookback for liquidity pool

input group "=== Alerts ==="
input bool    InpAlertPopup    = true;   // Popup alert
input bool    InpAlertPush     = false;  // Push notification to phone
input bool    InpShowComment   = true;   // Show status on chart

//============================ BUFFERS ==============================
double BuyBuffer[];
double SellBuffer[];

//--- object names
const string FIBO_NAME = "FibConf_Fibo";
const string ZONE_NAME = "FibConf_Zone";

//--- state
datetime g_lastAlertBar = 0;

//+------------------------------------------------------------------+
int OnInit()
{
   SetIndexBuffer(0, BuyBuffer,  INDICATOR_DATA);
   SetIndexBuffer(1, SellBuffer, INDICATOR_DATA);

   PlotIndexSetInteger(0, PLOT_ARROW, 233);            // up arrow
   PlotIndexSetInteger(1, PLOT_ARROW, 234);            // down arrow
   PlotIndexSetDouble (0, PLOT_EMPTY_VALUE, EMPTY_VALUE);
   PlotIndexSetDouble (1, PLOT_EMPTY_VALUE, EMPTY_VALUE);

   ArraySetAsSeries(BuyBuffer,  true);
   ArraySetAsSeries(SellBuffer, true);

   IndicatorSetString(INDICATOR_SHORTNAME, "FibConfluence");
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   ObjectDelete(0, FIBO_NAME);
   ObjectDelete(0, ZONE_NAME);
   Comment("");
}

//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
{
   //--- timeseries indexing: index 0 = current bar, 1 = last closed
   ArraySetAsSeries(time,        true);
   ArraySetAsSeries(open,        true);
   ArraySetAsSeries(high,        true);
   ArraySetAsSeries(low,         true);
   ArraySetAsSeries(close,       true);
   ArraySetAsSeries(tick_volume, true);

   //--- biggest offset we read behind a bar
   int maxOffset = InpLookback;
   if(2*InpStructWindow > maxOffset) maxOffset = 2*InpStructWindow;
   if(InpVolPeriod + 1  > maxOffset) maxOffset = InpVolPeriod + 1;
   if(InpLiqLookback+1  > maxOffset) maxOffset = InpLiqLookback + 1;

   int maxIndex = rates_total - maxOffset - 2;
   if(maxIndex < 1) return(rates_total);

   if(prev_calculated == 0)
   {
      ArrayInitialize(BuyBuffer,  EMPTY_VALUE);
      ArrayInitialize(SellBuffer, EMPTY_VALUE);
   }

   //--- bars to (re)evaluate, newest first; never the forming bar 0
   int start;
   if(prev_calculated == 0) start = maxIndex;
   else { start = (rates_total - prev_calculated) + 1; if(start > maxIndex) start = maxIndex; }

   for(int j = start; j >= 1; j--)
      EvaluateBar(j, time, open, high, low, close, tick_volume);

   //--- draw fibo + zone for the latest leg (relative to bar 1)
   int hh = 2, ll = 2;
   for(int i = 2; i <= 1 + InpLookback; i++)
   {
      if(high[i] > high[hh]) hh = i;
      if(low[i]  < low[ll])  ll = i;
   }
   double sHigh = high[hh], sLow = low[ll], rng = sHigh - sLow;
   if(rng > 0)
   {
      bool legUp = (hh < ll);
      double top, bot;
      if(legUp){ top = sHigh - rng*InpZoneTop; bot = sHigh - rng*InpZoneBottom; }
      else     { bot = sLow  + rng*InpZoneTop; top = sLow  + rng*InpZoneBottom; }

      if(InpDrawFibo) DrawFibo(legUp, time[hh], sHigh, time[ll], sLow);
      if(InpDrawZone) DrawZone(time[(hh < ll ? hh : ll)], time[0], top, bot);

      if(InpShowComment)
         Comment(StringFormat("FibConfluence  |  Leg: %s  |  Golden pocket: %s - %s  |  MinConf: %d",
                 (legUp ? "UP (look BUY)" : "DOWN (look SELL)"),
                 DoubleToString(MathMin(top,bot), _Digits),
                 DoubleToString(MathMax(top,bot), _Digits),
                 InpMinConfluence));
   }

   return(rates_total);
}

//+------------------------------------------------------------------+
//| Evaluate one closed bar j and set/clear its arrow                |
//+------------------------------------------------------------------+
void EvaluateBar(const int j,
                 const datetime &time[], const double &open[], const double &high[],
                 const double &low[], const double &close[], const long &tick_volume[])
{
   BuyBuffer[j]  = EMPTY_VALUE;
   SellBuffer[j] = EMPTY_VALUE;

   //--- 1) recent swing leg (from bars older than j)
   int hh = j+1, ll = j+1;
   for(int i = j+1; i <= j+InpLookback; i++)
   {
      if(high[i] > high[hh]) hh = i;
      if(low[i]  < low[ll])  ll = i;
   }
   double swingHigh = high[hh], swingLow = low[ll], range = swingHigh - swingLow;
   if(range <= 0) return;
   bool legUp = (hh < ll);              // high more recent -> up-leg -> BUY context

   //--- 2) fib levels of that leg
   double zTop, zBot, lvl618;
   if(legUp)
   { zTop = swingHigh - range*InpZoneTop; zBot = swingHigh - range*InpZoneBottom; lvl618 = swingHigh - range*0.618; }
   else
   { zBot = swingLow  + range*InpZoneTop; zTop = swingLow  + range*InpZoneBottom; lvl618 = swingLow  + range*0.618; }

   double o = open[j], h = high[j], l = low[j], c = close[j];
   double br = h - l; if(br <= 0) br = _Point;

   //--- 3) market structure (HH/HL vs LH/LL)
   bool bullStruct = false, bearStruct = false;
   {
      int N = InpStructWindow;
      double rHH = high[j+1], rLL = low[j+1], pHH = high[j+N+1], pLL = low[j+N+1];
      for(int i = j+1;   i <= j+N;   i++){ if(high[i] > rHH) rHH = high[i]; if(low[i] < rLL) rLL = low[i]; }
      for(int i = j+N+1; i <= j+2*N; i++){ if(high[i] > pHH) pHH = high[i]; if(low[i] < pLL) pLL = low[i]; }
      bullStruct = (rHH > pHH && rLL > pLL);
      bearStruct = (rHH < pHH && rLL < pLL);
   }

   //--- 4) volume spike (tick volume)
   bool volSpike = false;
   {
      double s = 0; for(int i = j+1; i <= j+InpVolPeriod; i++) s += (double)tick_volume[i];
      double a = s / InpVolPeriod;
      volSpike = ((double)tick_volume[j] > a*InpVolMult);
   }

   //--- 5) order-flow PROXY: where the candle closed inside its range
   double cp = (c - l) / br;            // 0 = closed at low, 1 = closed at high
   bool ofBull = (cp >= 0.66);
   bool ofBear = (cp <= 0.34);

   //--- 6) liquidity sweep of the prior pool
   double sL = low[j+1], sH = high[j+1];
   for(int i = j+1; i <= j+InpLiqLookback; i++){ if(low[i] < sL) sL = low[i]; if(high[i] > sH) sH = high[i]; }
   bool liqBull = (l < sL && c > sL);   // swept lows then reclaimed
   bool liqBear = (h > sH && c < sH);   // swept highs then rejected

   //=================== BUY (up-leg retracement) ===================
   if(legUp && c > swingLow)
   {
      bool touched   = (l <= zTop);                 // wicked into golden pocket
      bool rejection = (c > lvl618 && c > o);       // closed back above 61.8, bullish
      if(touched && rejection)
      {
         int sc = 0;
         if(InpUseStructure && bullStruct) sc++;
         if(InpUseVolume    && volSpike)   sc++;
         if(InpUseLiquidity && liqBull)    sc++;
         if(InpUseOrderFlow && ofBull)     sc++;
         if(sc >= InpMinConfluence)
         {
            BuyBuffer[j] = l - br*0.5;
            AlertIfNew(j, "BUY", sc, MathMin(zTop,zBot), MathMax(zTop,zBot), time);
         }
      }
   }

   //=================== SELL (down-leg retracement) ================
   if(!legUp && c < swingHigh)
   {
      bool touched   = (h >= zBot);                 // wicked up into golden pocket
      bool rejection = (c < lvl618 && c < o);       // closed back below 61.8, bearish
      if(touched && rejection)
      {
         int sc = 0;
         if(InpUseStructure && bearStruct) sc++;
         if(InpUseVolume    && volSpike)   sc++;
         if(InpUseLiquidity && liqBear)    sc++;
         if(InpUseOrderFlow && ofBear)     sc++;
         if(sc >= InpMinConfluence)
         {
            SellBuffer[j] = h + br*0.5;
            AlertIfNew(j, "SELL", sc, MathMin(zTop,zBot), MathMax(zTop,zBot), time);
         }
      }
   }
}

//+------------------------------------------------------------------+
void AlertIfNew(const int j, const string dir, const int sc,
                const double zlo, const double zhi, const datetime &time[])
{
   if(j != 1) return;                       // alert only on the last CLOSED bar
   if(time[1] == g_lastAlertBar) return;    // once per bar
   g_lastAlertBar = time[1];

   string msg = StringFormat("%s %s  |  FibConfluence score=%d/4  |  pocket %s-%s",
                _Symbol, dir, sc,
                DoubleToString(zlo, _Digits), DoubleToString(zhi, _Digits));
   if(InpAlertPopup) Alert(msg);
   if(InpAlertPush)  SendNotification(msg);
   Print(msg);
}

//+------------------------------------------------------------------+
void DrawFibo(const bool legUp, datetime tHigh, double pHigh, datetime tLow, double pLow)
{
   datetime t1, t2; double p1, p2;
   if(legUp){ t1 = tLow;  p1 = pLow;  t2 = tHigh; p2 = pHigh; }  // A=low,  B=high
   else     { t1 = tHigh; p1 = pHigh; t2 = tLow;  p2 = pLow;  }  // A=high, B=low

   if(ObjectFind(0, FIBO_NAME) < 0)
   {
      ObjectCreate(0, FIBO_NAME, OBJ_FIBO, 0, t1, p1, t2, p2);
      ObjectSetInteger(0, FIBO_NAME, OBJPROP_COLOR, clrSilver);
      ObjectSetInteger(0, FIBO_NAME, OBJPROP_LEVELS, 7);
      double lv[7] = {0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0};
      for(int i = 0; i < 7; i++)
      {
         color lc = (i == 4 || i == 5) ? clrGold : clrGray;   // highlight 61.8 & 78.6
         ObjectSetDouble (0, FIBO_NAME, OBJPROP_LEVELVALUE, i, lv[i]);
         ObjectSetInteger(0, FIBO_NAME, OBJPROP_LEVELCOLOR, i, lc);
         ObjectSetString (0, FIBO_NAME, OBJPROP_LEVELTEXT,  i, DoubleToString(lv[i]*100, 1)+"%");
      }
   }
   ObjectMove(0, FIBO_NAME, 0, t1, p1);
   ObjectMove(0, FIBO_NAME, 1, t2, p2);
}

//+------------------------------------------------------------------+
void DrawZone(datetime tLeft, datetime tRight, double top, double bot)
{
   if(ObjectFind(0, ZONE_NAME) < 0)
   {
      ObjectCreate(0, ZONE_NAME, OBJ_RECTANGLE, 0, tLeft, top, tRight, bot);
      ObjectSetInteger(0, ZONE_NAME, OBJPROP_COLOR, clrGold);
      ObjectSetInteger(0, ZONE_NAME, OBJPROP_FILL,  true);
      ObjectSetInteger(0, ZONE_NAME, OBJPROP_BACK,  true);
   }
   ObjectMove(0, ZONE_NAME, 0, tLeft,  top);
   ObjectMove(0, ZONE_NAME, 1, tRight, bot);
}
//+------------------------------------------------------------------+
