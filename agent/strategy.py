"""Signal scanner.

Long-only (Recall is spot trading). A signal is produced only when:
  1. the higher timeframe trend is up (regime filter),
  2. the market is trending, not chopping (ADX),
  3. a pullback-continuation or volume breakout setup triggers,
  4. the confluence score passes `min_score`, and
  5. the *real* reward:risk to the nearest overhead resistance is >= `min_rr`.
Point 5 is what lifts RR: the stop sits under structure, and setups whose
target would run into resistance before `min_rr` are discarded instead of taken.
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .utils import add_indicators


@dataclass
class Signal:
    token: str
    time: pd.Timestamp
    setup: str
    entry: float
    stop: float
    tp1: float
    target: float
    atr: float
    score: int
    rr: float
    reasons: list = field(default_factory=list)

    @property
    def risk(self):
        return self.entry - self.stop

    def describe(self):
        return (f"{self.token} {self.setup} score={self.score} entry={self.entry:.4f} "
                f"SL={self.stop:.4f} TP1={self.tp1:.4f} TP={self.target:.4f} RR={self.rr:.2f} "
                f"[{', '.join(self.reasons)}]")


PIVOT_SPAN = 8  # bars each side: only major swing highs count as resistance


def prepare(entry_df, trend_df, pivot_span=PIVOT_SPAN):
    """Compute indicators once and align the higher-timeframe trend onto entry bars."""
    e = add_indicators(entry_df)
    t = add_indicators(trend_df)
    t["htf_up"] = (t["close"] > t["ema50"]) & (t["ema50"] > t["ema50"].shift(3)) & (t["ema20"] > t["ema50"])
    t["htf_strong"] = t["htf_up"] & (t["close"] > t["ema200"])
    htf = t[["close_time", "htf_up", "htf_strong", "rsi"]].rename(columns={"rsi": "htf_rsi"})
    # only use a higher-TF candle once it has closed
    merged = pd.merge_asof(
        e.reset_index().sort_values("close_time"),
        htf.sort_values("close_time"),
        on="close_time", direction="backward",
    ).set_index("time")
    merged[["htf_up", "htf_strong"]] = merged[["htf_up", "htf_strong"]].fillna(False).astype(bool)

    win = 2 * pivot_span + 1
    merged["pivot_high"] = merged["high"] == merged["high"].rolling(win, center=True).max()
    merged["pivot_low"] = merged["low"] == merged["low"].rolling(win, center=True).min()
    return merged


def _resistance_above(df, i, price, lookback=300, span=PIVOT_SPAN, leg=30):
    """Nearest confirmed major pivot high above `price`.

    Highs of the current trend leg (last `leg` bars) are ignored: a continuation
    setup is expected to break them, so they should not cap the target."""
    lo = max(0, i - lookback)
    hi = i - span  # pivots after this are not confirmed yet at bar i
    if hi <= lo:
        return None
    floor = max(price, float(df["high"].iloc[max(0, i - leg):i + 1].max()))
    window = df.iloc[lo:hi]
    levels = window.loc[window["pivot_high"] & (window["high"] > floor), "high"]
    return float(levels.min()) if len(levels) else None


def _structure_low(df, i, lookback=12):
    return float(df["low"].iloc[max(0, i - lookback):i + 1].min())


def evaluate(df, i, token, cfg):
    """Return a Signal for closed bar `i` of a prepared frame, or None."""
    if i < 210:
        return None
    row = df.iloc[i]
    prev = df.iloc[i - 1]
    a = row["atr"]
    if not np.isfinite(a) or a <= 0:
        return None

    # ---- hard filters ----
    if not row["htf_up"]:
        return None
    if row["adx"] < cfg.min_adx or row["pdi"] <= row["mdi"]:
        return None
    if not (row["ema20"] > row["ema50"] > row["ema200"]):
        return None
    if row["rsi"] > 75:  # do not chase overextended candles
        return None
    if (row["close"] - row["ema20"]) > 2.5 * a:
        return None

    # ---- setups ----
    setup = None
    recent = df.iloc[i - 4:i + 1]
    touched_ema = (recent["low"] <= recent["ema20"] + 0.2 * a).any() and (recent["close"] > recent["ema50"]).all()
    rsi_reset = recent["rsi"].min() < 50 and row["rsi"] > prev["rsi"] and row["rsi"] > 45
    bullish_trigger = row["close"] > row["open"] and row["close"] > prev["high"]
    if touched_ema and rsi_reset and bullish_trigger:
        setup = "pullback"

    squeeze = df["bb_width"].iloc[i - 20:i].min() <= df["bb_width"].iloc[i - 120:i].quantile(0.25)
    breakout = (row["close"] > row["dc_high"] and row["volume"] > 1.5 * row["vol_ma"]
                and (row["close"] - row["low"]) > 0.6 * (row["high"] - row["low"]))
    if setup is None and breakout and squeeze:
        setup = "breakout"
    if setup is None:
        return None

    # ---- confluence score ----
    score, reasons = 30, [setup]
    if row["htf_strong"]:
        score += 15; reasons.append("HTF>EMA200")
    if row["adx"] >= 25:
        score += 10; reasons.append(f"ADX{row['adx']:.0f}")
    if row["volume"] > 1.2 * row["vol_ma"]:
        score += 10; reasons.append("volume")
    if row["macd_hist"] > 0 and row["macd_hist"] > prev["macd_hist"]:
        score += 10; reasons.append("MACD+")
    if 50 <= row["rsi"] <= 68:
        score += 10; reasons.append(f"RSI{row['rsi']:.0f}")
    if 45 <= row.get("htf_rsi", 50) <= 70:
        score += 5; reasons.append("HTF-RSI ok")
    if (row["close"] - row["low"]) > 0.7 * (row["high"] - row["low"]):
        score += 10; reasons.append("strong close")
    if score < cfg.min_score:
        return None

    # ---- stop under structure, bounded by ATR ----
    entry = float(row["close"])
    stop = _structure_low(df, i) - cfg.sl_atr_buffer * a
    risk = entry - stop
    min_risk = max(cfg.min_risk_atr * a, cfg.min_risk_pct * entry)
    if risk < min_risk:  # a stop inside spread/noise turns slippage into multi-R losses
        stop = entry - min_risk
        risk = entry - stop
    if risk > cfg.max_risk_atr * a or risk <= 0:
        return None

    # ---- target capped by overhead resistance; enforce real RR ----
    target = entry + cfg.target_rr * risk
    resistance = _resistance_above(df, i, entry * 1.001)
    if resistance is not None and resistance < target:
        target = resistance - 0.1 * a
        reasons.append("TP@resistance")
    # account for round-trip costs when judging RR
    cost = entry * cfg.fee_rate * 2
    rr = (target - entry - cost) / (risk + cost)
    if rr < cfg.min_rr:
        return None

    tp1 = entry + min(cfg.partial_at_r * risk, (target - entry) * 0.6)
    return Signal(token, df.index[i], setup, entry, float(stop), float(tp1), float(target),
                  float(a), int(score), float(rr), reasons)


def scan(token, entry_df, trend_df, cfg):
    """Evaluate only the latest closed candle."""
    df = prepare(entry_df, trend_df)
    return evaluate(df, len(df) - 1, token, cfg), df
