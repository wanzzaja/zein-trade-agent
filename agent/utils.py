"""Logging, market data and technical indicators."""
import logging
import time

import numpy as np
import pandas as pd
import requests


def get_logger(log_file="data/logs.txt"):
    logger = logging.getLogger("zein")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in (logging.StreamHandler(), logging.FileHandler(log_file)):
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    return logger


def fetch_klines(base_url, symbol, interval, limit=500, end_time=None, retries=3):
    """Fetch OHLCV candles from a Binance-compatible public endpoint."""
    params = {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
    if end_time is not None:
        params["endTime"] = int(end_time)
    for attempt in range(retries):
        try:
            resp = requests.get(f"{base_url}/api/v3/klines", params=params, timeout=15)
            resp.raise_for_status()
            break
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    rows = resp.json()
    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "qav", "trades", "tbv", "tqv", "ignore",
    ])
    df = df[["open_time", "open", "high", "low", "close", "volume", "close_time"]]
    df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].astype(float)
    df["time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df.set_index("time")


def fetch_history(base_url, symbol, interval, total):
    """Page backwards to get more than 1000 candles (for backtests)."""
    frames, end = [], None
    while total > 0:
        df = fetch_klines(base_url, symbol, interval, limit=min(total, 1000), end_time=end)
        if df.empty:
            break
        frames.append(df)
        total -= len(df)
        end = df["open_time"].iloc[0] - 1
        if len(df) < 1000:
            break
    out = pd.concat(frames).sort_index()
    return out[~out.index.duplicated()]


def drop_unclosed(df):
    """Remove the still-forming candle so signals never repaint."""
    now_ms = time.time() * 1000
    return df[df["close_time"] < now_ms]


# ---------------- indicators ----------------

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def atr(df, n=14):
    prev = df["close"].shift()
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"] - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(df, n=14):
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    a = atr(df, n)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / a
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / a
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean().fillna(0), plus_di, minus_di


def add_indicators(df):
    df = df.copy()
    df["ema20"] = ema(df["close"], 20)
    df["ema50"] = ema(df["close"], 50)
    df["ema200"] = ema(df["close"], 200)
    df["rsi"] = rsi(df["close"])
    df["atr"] = atr(df)
    df["adx"], df["pdi"], df["mdi"] = adx(df)
    df["vol_ma"] = df["volume"].rolling(20).mean()
    mid = df["close"].rolling(20).mean()
    std = df["close"].rolling(20).std()
    df["bb_width"] = (4 * std) / mid
    df["dc_high"] = df["high"].rolling(20).max().shift(1)
    macd = ema(df["close"], 12) - ema(df["close"], 26)
    df["macd_hist"] = macd - ema(macd, 9)
    return df


def swing_highs(df, left=3, right=3):
    """Confirmed pivot highs (needs `right` closed bars after the pivot)."""
    h = df["high"]
    win = h.rolling(left + right + 1, center=True).max()
    return h[(h == win)].dropna()


def swing_lows(df, left=3, right=3):
    low = df["low"]
    win = low.rolling(left + right + 1, center=True).min()
    return low[(low == win)].dropna()
