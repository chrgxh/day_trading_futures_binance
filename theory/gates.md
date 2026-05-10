# Entry Gates — `ema_trend_momentum`

All five gates must pass simultaneously before a position is opened.
Exit logic is independent — none of these gates affect when a position is closed.

---

## Gate 1 — EMA Alignment (15m fast/slow)

**What it does**

Computes a fast EMA (default 9) and a slow EMA (default 21) on the 15m close prices.
A long requires `fast > slow`; a short requires `fast < slow`.
No fresh crossover is needed — an already-aligned state is enough. This means
the bot can enter on cold-start or immediately after a position closes without
waiting for the next crossover candle.

**Why it matters**

It answers: "is short-term price momentum bullish or bearish right now?"
EMAs weight recent prices more heavily than SMAs, so they react faster to
reversals while still smoothing out single-candle noise.

**Limitations**

- EMAs lag. The signal always comes after the move has already started. A
  9/21 EMA pair on 15m is roughly 1–3 candles behind a turn.
- In a sideways market the fast and slow lines crisscross repeatedly. Each
  crossing fires a fresh entry gate — this is why the other gates exist.
- Widening the periods reduces whipsaws but increases lag. The 9/21 pair is
  a reasonable balance for 15m; if you switch to 1h or 4h bars, consider
  9/26 or 12/26.

---

## Gate 2 — 1h 200 EMA Trend Filter

**What it does**

Resamples the 15m candle buffer into 1h bars and computes a 200-period EMA
on those hourly closes. A long requires price to be above this EMA; a short
requires price to be below it.

No second WebSocket stream is needed — the 1h bars are derived entirely from
the sub-hourly buffer already in memory.

**Why it matters**

It answers: "is the macro trend on our side?"
Trading against a 200 EMA on the hourly chart is a well-documented losing
strategy. This gate prevents counter-trend entries by requiring that the
higher-timeframe structure agrees with the 15m signal.

**Limitations**

- The 200 EMA is very slow to turn. After a major trend reversal, price can
  trade on the wrong side of the 200 EMA for dozens of candles before the
  average catches up. During that window the gate blocks every entry even if
  the new trend is clearly underway.
- In prolonged consolidation the 200 EMA flattens and price oscillates above
  and below it. The "above/below" check becomes effectively coin-flip.
- The resampled 1h bars at the buffer boundary may be slightly different from
  Binance's native 1h OHLCV depending on where the 15m REST prefetch cut off.
  This is a cosmetic edge case and has no material impact on the EMA value
  because a single candle difference in a 200-period EMA is negligible.

---

## Gate 3 — RVOL (Relative Volume)

**What it does**

Computes the average volume of the previous `volume_lookback` candles
(default 20) and checks whether the current candle's volume exceeds it by
`volume_multiplier` (default 1.2×).

**Why it matters**

It answers: "is there unusual participation behind this move?"
Price moves on below-average volume tend not to follow through. A volume spike
signals that a meaningful number of market participants are actively pushing
price in this direction, which increases the probability that the EMA
alignment reflects a real directional commitment rather than drift.

**Limitations**

- Volume spikes can be caused by news, macro events, or large liquidations
  that produce a single violent candle followed by an immediate reversal. High
  volume is a necessary condition for a real move, not a sufficient one.
- In very liquid markets (BTC/ETH) volume is noisy. A 1.2× threshold is
  intentionally loose. Raising it to 1.5–2.0× will reduce false positives at
  the cost of missing legitimate entries.
- RVOL is measured on the current (still-forming) candle at tick time. By
  design in this bot the strategy only runs on closed candles, so the volume
  figure is final when evaluated.
- The rolling baseline uses the previous 20 candles, not all-time history.
  During extended high-volatility periods (e.g. a prolonged squeeze), the
  baseline rises and subsequent candles no longer look exceptional even though
  volume remains elevated.

---

## Gate 4 — RSI Momentum Zone

**What it does**

Computes RSI (Wilder's smoothing, default period 14) on the 15m closes.
Long entry requires RSI in `[rsi_long_low, rsi_long_high]` (default 50–70).
Short entry requires RSI in `[rsi_short_low, rsi_short_high]` (default 30–50).

These same RSI params also drive the exit: RSI ≥ 80 force-exits a long,
RSI ≤ 20 force-exits a short.

**Why it matters**

It answers: "is momentum confirming the direction, and are we not already
overextended?"
The 50 midline check ensures momentum is actually positive (long) or negative
(short). The upper cap at 70 (long) / lower cap at 30 (short) avoids chasing
entries at overbought/oversold extremes where a snap-back is more likely.

**Limitations**

- You will always miss the first leg of a strong move. On a genuine breakout
  RSI often blows past 70 immediately. The gate will block entry until a
  pullback brings RSI back into the 50–70 zone — by which point the easy
  money is gone.
- RSI can be pinned in the momentum zone for a long time during a strong
  trend, meaning the gate passes on every candle. The other gates then
  determine whether you actually enter. This is correct behavior.
- RSI diverges from price action at major turns — it's a lagging momentum
  indicator, not a leading reversal signal. Do not interpret a gate failure
  here as a prediction that price will reverse.
- Period 14 on 15m is ~3.5 hours of history. It reflects very short-term
  momentum. If you switch to a higher interval (1h, 4h), RSI 14 spans a much
  longer time horizon and the 50–70 bands remain appropriate.

---

## Gate 5 — ADX Regime Filter

**What it does**

Computes ADX (Wilder's smoothing, default period 14) on the 15m candles using
highs, lows, and closes. Entry is blocked when `ADX < min_adx` (default 25).

ADX only gates entry — it has no role in exit decisions. Once in a position,
a declining ADX does not trigger a close.

**Why it matters**

It answers: "is the market in a trending regime right now, or is it ranging?"
ADX measures trend *strength*, not direction. A high ADX (>25) means price is
making directional progress regardless of whether that direction is up or
down. A low ADX (<20) means the market is oscillating without conviction.

EMA crossovers in low-ADX environments are almost entirely noise. The fast
and slow lines crisscross repeatedly with no follow-through. This gate is the
primary filter for avoiding whipsaw losses — it blocks entries during the
market conditions where EMA-based strategies perform worst.

Thresholds as a rough guide:
- ADX < 20 — ranging, avoid entirely
- ADX 20–25 — weak trend forming, marginal
- ADX 25–40 — confirmed trend, ideal entry window
- ADX > 40 — strong trend, late-stage; valid but expect shallower remaining move
- ADX > 50 — extreme trend, near exhaustion in many cases

**Limitations**

- ADX lags heavily. It needs `2 * period` candles before producing its first
  value, and then Wilder-smooths the DX series again. On 15m bars with
  period=14, meaningful ADX readings start at candle ~29 and remain sluggish
  to react to regime changes. You will enter late into trending moves.
- ADX stays elevated after a trend ends. A market that just finished a strong
  move can show ADX > 25 for several candles into the subsequent range. The
  gate will still pass while the trend indicator is lying about current
  conditions.
- ADX measures both up-trends and down-trends with the same number. A market
  crashing hard shows high ADX. The trend direction is handled by the other
  gates (EMA alignment, 1h 200 EMA) — ADX alone tells you nothing about which
  way to trade.
- In very fast-moving markets (1m, 3m), ADX period 14 spans only 14 minutes
  and becomes noisy. At 15m it spans 3.5 hours, which is a reasonable lookback
  for regime classification. At 1h it spans 14 hours — still reasonable but
  slower to react.
- Setting `min_adx` too high (e.g. 35) will filter out most entries and reduce
  trade frequency significantly. Backtesting the threshold against your
  specific symbol and interval is the only reliable way to find the right value.

---

## Combined Gate Interaction

The gates are evaluated left-to-right in the code but there is no short-circuit
ordering — all five are always computed. The practical failure modes by gate
combination:

| Failing gates | Likely market condition |
|---|---|
| Only ADX | Sideways range with temporary EMA alignment |
| ADX + RVOL | Dead, thin market with no participation |
| ADX + RSI | Ranging + extended RSI without a real trend |
| 1h trend + EMA | Counter-trend 15m signal against macro structure |
| RVOL only | EMA aligned and trending, but the move has no volume behind it |

The ADX and 1h trend gates tend to fail together during range-bound markets.
The RVOL gate is the most independent — it fails when price moves quietly
without participation, which none of the other gates catch.
