# GOLD LIQUIDITY HUNTER PRO — الخوارزمية الرياضية الدقيقة
**للتطبيق على MT5 (XAUUSD / GOLD#) — مرجع رياضي دقيق**

> ⚠️ **تنبيه قبل التطبيق:** هاد الوثيقة هي الـ algo الدقيق كيما طلبتي. ولكن الـ backtest على الذهب الحقيقي **ماورّاش edge مثبت**: النسخة الكاملة عطات 8 صفقات/سنتين (ساعي، خاسرة) و 0 (يومي). صادق عليها على بيانات الـ broker ديالك بـ walk-forward **قبل** ما تثق فيها. الـ delta هنا **proxy** (tick volume)، ماشي order flow حقيقي.

---

## رموز
- لكل شمعة: `O, H, L, C, V` (open, high, low, close, tick-volume).
- `L` = pivot length = 15. `i` = index الحالي.

## 1. مؤشرات أساسية
```
TR_t   = max(H_t - L_t, |H_t - C_{t-1}|, |L_t - C_{t-1}|)
ATR_t  = Wilder_EMA(TR, 14)            # α = 1/14
avgVol_t = SMA(V, 50)
rangeHigh_t = highest(H, 100)
rangeLow_t  = lowest(L, 100)
mid_t = (rangeHigh_t + rangeLow_t) / 2
```

## 2. Pivots (مؤكدة بتأخير L شمعات)
```
PivotHigh عند k  ⟺  H_k = max(H[k-L .. k+L])
PivotLow  عند k  ⟺  L_k = min(L[k-L .. k+L])
```
الـ pivot عند `k` كيتأكد غير ف الشمعة `i = k + L` (باش متكونش lookahead).

## 3. إنشاء المناطق (Zones)

### BSL (Buy-Side Liquidity — فوق، swing high)
عند تأكيد pivot high (k = i − L):
```
pHigh = H_k
pBot  = max(C_k, O_k)
minH  = ATR_k · atrMult            # atrMult = 0.5
if (pHigh - pBot) < minH:  pBot = pHigh - minH
pivot_size = pHigh - pBot
top = pHigh ; bottom = pBot
capacity = avgVol_k · zoneCapacity # zoneCapacity = 5.0
deltas = [0, 0, 0, 0]
```

### SSL (Sell-Side Liquidity — تحت, swing low)
```
pLow = L_k
pTop = min(C_k, O_k)
minH = ATR_k · atrMult
if (pTop - pLow) < minH:  pTop = pLow + minH
pivot_size = pTop - pLow
top = pTop ; bottom = pLow
capacity = avgVol_k · zoneCapacity
deltas = [0, 0, 0, 0]
```

### فلتر التداخل (Overlap filter)
منطقتين كيتداخلو إيلا `NOT (new.top < old.bottom OR new.bottom > old.top)`.
- BSL جديد يتداخل مع BSL قديم غير-مكنوس: إيلا `new.pHigh > old.top` → حيّد القديم؛ وإلا تجاهل الجديد.
- SSL جديد: إيلا `new.pLow < old.bottom` → حيّد القديم؛ وإلا تجاهل الجديد.
- احتفظ بأقصى 10 مناطق غير-مكنوسة لكل جهة.

## 4. الكوادرنتات (4 Quadrants)
كل منطقة مقسومة 4 أجزاء متساوية، `h = pivot_size / 4`:
```
quadrant_j = [bottom + j·h ,  bottom + (j+1)·h]   ,  j = 0,1,2,3
```
- BSL: الكوادرنت الخارجي (نحو الـ sweep) = **j = 3** (الأعلى).
- SSL: الكوادرنت الخارجي = **j = 0** (الأسفل).

## 5. Delta Proxy (لكل شمعة)
```
barRange = H - L
barDelta = 0                      if barRange = 0
         = V · (C - O) / barRange otherwise
```
> هادا **delta_proxy** على أساس tick-volume — ماشي order flow حقيقي.

## 6. تداخل الشمعة مع كوادرنت (Bar overlap)
```
oT = min(H_t, quadrant_top)
oB = max(L_t, quadrant_bottom)
overlap = oT - oB   if oT > oB   else 0
```

## 7. معالجة كل منطقة غير-مكنوسة (كل شمعة)
```
hit = false
for j in 0..3:
    ov = overlap(bar_t, quadrant_j)
    if ov > 0:
        hit = true
        ovR = ov / barRange
        deltas[j]      += barDelta · ovR
        volume_traded  += V_t · ovR
if hit and (was_hit = false):  test_count += 1
swept =  (H_t > top)      للـ BSL
      =  (L_t < bottom)   للـ SSL
if not swept:
    health_pct = max(0, 100 − volume_traded / capacity · 100)
else:
    sweep_count += 1
    signal = evalReversal(...)      # القسم 8
    status = SWEPT
was_hit = hit
```

## 8. تقييم الانعكاس (evalReversal) — بالضبط
```
totalD   = Σ deltas
absTotalD = Σ |deltas[j]|
if absTotalD ≤ 0:  return NONE

outerIdx = 3 (BSL)  |  0 (SSL)
outerD   = deltas[outerIdx]
isSweeping   = (H_t > top) BSL | (L_t < bottom) SSL
closesInside = bottom ≤ C_t ≤ top
midPt        = (top + bottom) / 2
closesRej    = (C_t < midPt) BSL | (C_t > midPt) SSL

# الترتيب مهم — أول إشارة كتطبّق هي اللي تتحسب:

A) ABS (absorption):
   if isSweeping AND [ (BSL AND outerD < 0) OR (SSL AND outerD > 0) ]:
       r = |outerD| / absTotalD
       if r > 0.2  → ABS

B) EXH (exhaustion):
   if (no signal) AND isSweeping:
       r = |outerD| / absTotalD
       if r < 0.1  → EXH

C) DIV (delta divergence):
   if (no signal) AND closesInside:
       r = |outerD| / absTotalD
       if r > 0.6 AND [ (BSL AND outerD > 0) OR (SSL AND outerD < 0) ]  → DIV

D) REJ (snapback rejection):
   if (no signal) AND isSweeping AND closesRej:
       bRatio = |barDelta| / V_t
       if [ (BSL AND barDelta < 0) OR (SSL AND barDelta > 0) ] AND bRatio > 0.2  → REJ
```

## 9. Premium / Discount
```
zone_mid = (top + bottom) / 2
PREMIUM  if zone_mid > mid_t
DISCOUNT if zone_mid ≤ mid_t
```

## 10. النجوم (Strength stars)
```
volScore  = min(volume_traded / avgVol_t, 3) / 3
pivScore  = min(pivot_size   / ATR14_t , 3) / 3
combined  = 0.6·volScore + 0.4·pivScore
stars     = min( round(combined · 4) + 1 , 5 )
```

## 11. Liquidity Score (0–100)
```
score = 0
stars ≥ 3                         → +20
test_count ≤ 1 (first touch)      → +15
sweep detected                    → +20
signal ∈ {ABS, REJ}               → +20
premium/discount يطابق الاتجاه     → +10
delta_proxy يأكد الإشارة           → +10
RR ≥ 2                            → +5
```

## 12. خطة RR و الـ SL/TP
```
slDist = top − bottom

BSL / SELL:
   sl = top + slDist · 0.3
   entry = C_t
   risk  = sl − entry
   tp = entry − risk · RR          # RR = 2.0
   direction = SELL (−1)

SSL / BUY:
   sl = bottom − slDist · 0.3
   entry = C_t
   risk  = entry − sl
   tp = entry + risk · RR
   direction = BUY (+1)
```

## 13. شروط التنفيذ (Executable — demo فقط)
صفقة حقيقية تتفتح **غير** إيلا:
```
signal ∈ {ABS, REJ}              # EXH, DIV = observer فقط
BSL → SELL  &  premium_discount = PREMIUM
SSL → BUY   &  premium_discount = DISCOUNT
stars ≥ 3
liquidity_score ≥ 75
RR ≥ 2.0
spread OK
time gate PASS  &  NOT bad-liquidity-hour
SL/TP صالحين
final_lot ≤ 0.01
ماكاينش صفقة GOLD مفتوحة (max 1)
الـ daily-loss circuit breaker ماشي مقفول
الحساب DEMO  &  live trading = false
```

---

## ملاحظات حاسمة للتطبيق على MT5
1. **`V` = tick-volume ف MT5**، ماشي حجم حقيقي. كل منطق الـ delta (ABS/EXH/DIV/REJ) مبني عليه → هو **proxy**. على الذهب (OTC) أقل موثوقية.
2. **بلا lookahead:** الـ pivot كيتأكد ف `i = k + L`. ماتستعملش `H_k` كـ pivot قبل هاد الشمعة.
3. **الإشارات على الشمعة المغلوقة** فقط (ماتقيّمش على الشمعة الحية باش متعملش repaint).
4. **صادق قبل ما تبني:** شغّل walk-forward على بيانات الـ broker ديالك. الـ backtest ديالنا = ماكاينش edge مثبت + إشارات نادرة بزاف.

*وثيقة تعليمية — ماشي نصيحة مالية.*
