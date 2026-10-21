"""Signal scanner.

Long-only (Recall is spot trading). A signal is produced only when:
  1. the higher timeframe trend is up (regime filter),
  2. the market is trending, not chopping (ADX),
  3. a pullback-continuation or volume breakout setup triggers,
  4. the confluence score passes `min_score` (incl. relative strength vs BTC),
  5. the *real* reward:risk to the nearest overhead resistance is >= `min_rr`, and
  6. BTC's higher-TF trend is up (alts bleed together when BTC falls).
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


def prepare(entry_df, trend_df, btc=None, pivot_span=PIVOT_SPAN):
    """Compute indicators once and align higher-timeframe and BTC context onto entry bars.

    `btc` is an optional (entry_df, trend_df) pair for BTC. It drives the market
    regime filter (alts are only bought while BTC's higher-TF trend is up) and the
    relative-strength score (prefer coins outperforming BTC)."""
    e = add_indicators(entry_df)
    t = _trend_flags(add_indicators(trend_df))
    htf = t[["close_time", "htf_up", "htf_strong", "rsi"]].rename(columns={"rsi": "htf_rsi"})
    # only use a higher-TF candle once it has closed
    merged = _asof(e.reset_index(), htf).set_index("time")
    merged[["htf_up", "htf_strong"]] = merged[["htf_up", "htf_strong"]].fillna(False).astype(bool)

    if btc is not None:
        b_entry, b_trend = btc
        bt = _trend_flags(add_indicators(b_trend))[["close_time", "htf_up"]].rename(columns={"htf_up": "btc_up"})
        be = b_entry[["close_time", "close"]].rename(columns={"close": "btc_close"})
        merged = _asof(_asof(merged.reset_index(), bt), be).set_index("time")
        merged["btc_up"] = merged["btc_up"].fillna(False).astype(bool)
    else:
        merged["btc_up"] = True
        merged["btc_close"] = np.nan

    merged["atr_pct"] = merged["atr"] / merged["close"]
    merged["atr_pct_med"] = merged["atr_pct"].rolling(100, min_periods=20).median()

    win = 2 * pivot_span + 1
    merged["pivot_high"] = merged["high"] == merged["high"].rolling(win, center=True).max()
    merged["pivot_low"] = merged["low"] == merged["low"].rolling(win, center=True).min()
    return merged


def _trend_flags(t):
    t["htf_up"] = (t["close"] > t["ema50"]) & (t["ema50"] > t["ema50"].shift(3)) & (t["ema20"] > t["ema50"])
    t["htf_strong"] = t["htf_up"] & (t["close"] > t["ema200"])
    return t


def _asof(left, right):
    return pd.merge_asof(left.sort_values("close_time"), right.sort_values("close_time"),
                         on="close_time", direction="backward")


def relative_strength(df, i, lookback):
    """Token return minus BTC return over `lookback` bars; None without BTC data."""
    if i < lookback or not np.isfinite(df["btc_close"].iloc[i]) or not np.isfinite(df["btc_close"].iloc[i - lookback]):
        return None
    own = df["close"].iloc[i] / df["close"].iloc[i - lookback] - 1
    ref = df["btc_close"].iloc[i] / df["btc_close"].iloc[i - lookback] - 1
    return float(own - ref)


def snapshot(df, i, cfg):
    """Market state of bar `i` for the dashboard."""
    row = df.iloc[i]
    rs = relative_strength(df, i, cfg.rs_lookback)
    return {
        "price": float(row["close"]),
        "trend": "up" if row["htf_up"] else "down",
        "trend_strong": bool(row["htf_strong"]),
        "btc_up": bool(row["btc_up"]),
        "adx": round(float(row["adx"]), 1),
        "rsi": round(float(row["rsi"]), 1),
        "atr_pct": round(float(row["atr_pct"]) * 100, 2),
        "rs": None if rs is None else round(rs * 100, 2),
        "change": round(float(row["close"] / df["close"].iloc[max(0, i - 6)] - 1) * 100, 2),
    }


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


def _reject(why, reason):
    if why is not None:
        why.append(reason)
    return None


def evaluate(df, i, token, cfg, why=None):
    """Return a Signal for closed bar `i` of a prepared frame, or None.

    When `why` is a list, the rejection reason is appended to it (shown in the UI)."""
    if i < 210:
        return _reject(why, "not enough history")
    row = df.iloc[i]
    prev = df.iloc[i - 1]
    a = row["atr"]
    if not np.isfinite(a) or a <= 0:
        return _reject(why, "no ATR")

    # ---- hard filters ----
    if cfg.btc_filter and token != cfg.btc_token and not row["btc_up"]:
        return _reject(why, "BTC regime down")
    if not row["htf_up"]:
        return _reject(why, f"{cfg.trend_tf} trend down")
    if row["adx"] < cfg.min_adx or row["pdi"] <= row["mdi"]:
        return _reject(why, f"no trend (ADX {row['adx']:.0f})")
    if not (row["ema20"] > row["ema50"] > row["ema200"]):
        return _reject(why, "EMA not stacked")
    if row["rsi"] > 75:  # do not chase overextended candles
        return _reject(why, f"overbought (RSI {row['rsi']:.0f})")
    if (row["close"] - row["ema20"]) > 2.5 * a:
        return _reject(why, "too far from EMA20")
    med = row["atr_pct_med"]
    if np.isfinite(med) and row["atr_pct"] > cfg.vol_spike_mult * med:
        return _reject(why, "volatility spike")

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
        return _reject(why, "waiting for setup")

    # ---- confluence score (max 100) ----
    score, reasons = 20, [setup]
    if row["htf_strong"]:
        score += 15; reasons.append(f"{cfg.trend_tf}>EMA200")
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
    rs = relative_strength(df, i, cfg.rs_lookback)
    if rs is None or rs > 0:  # BTC itself (no reference) counts as the leader
        score += 10; reasons.append("RS>BTC" if rs is not None else "leader")
    if score < cfg.min_score:
        return _reject(why, f"score {score} < {cfg.min_score}")

    # ---- stop under structure, bounded by ATR ----
    entry = float(row["close"])
    stop = _structure_low(df, i) - cfg.sl_atr_buffer * a
    risk = entry - stop
    min_risk = max(cfg.min_risk_atr * a, cfg.min_risk_pct * entry)
    if risk < min_risk:  # a stop inside spread/noise turns slippage into multi-R losses
        stop = entry - min_risk
        risk = entry - stop
    if risk > cfg.max_risk_atr * a or risk <= 0:
        return _reject(why, "stop too wide")

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
        return _reject(why, f"RR {rr:.2f} < {cfg.min_rr} (resistance)")

    tp1 = entry + min(cfg.partial_at_r * risk, (target - entry) * 0.6)
    return Signal(token, df.index[i], setup, entry, float(stop), float(tp1), float(target),
                  float(a), int(score), float(rr), reasons)


def scan(token, entry_df, trend_df, cfg, btc=None):
    """Evaluate only the latest closed candle. Returns (signal, frame, reason)."""
    df = prepare(entry_df, trend_df, btc)
    why = []
    sig = evaluate(df, len(df) - 1, token, cfg, why)
    return sig, df, (why[0] if why else "signal")
