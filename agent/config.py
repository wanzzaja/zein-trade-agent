"""Central configuration. Every value can be overridden through environment variables."""
import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional
    pass


def _env(name, default, cast=str):
    value = os.getenv(name)
    if value is None or value == "":
        return default
    if cast is bool:
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return cast(value)


# Ethereum mainnet token addresses used by Recall for spot trading.
TOKENS = {
    "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    "WBTC": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
    "LINK": "0x514910771AF9Ca656af840dff83E8264EcF986CA",
    "UNI": "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
    "AAVE": "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9",
}

# Recall token -> market-data symbol (Binance public data).
MARKET_SYMBOLS = {
    "WETH": "ETHUSDT",
    "WBTC": "BTCUSDT",
    "LINK": "LINKUSDT",
    "UNI": "UNIUSDT",
    "AAVE": "AAVEUSDT",
}


@dataclass
class Config:
    # --- connectivity ---
    recall_api_key: str = _env("RECALL_API_KEY", "")
    recall_api_url: str = _env("RECALL_API_URL", "https://api.sandbox.competitions.recall.network")
    market_data_url: str = _env("MARKET_DATA_URL", "https://data-api.binance.vision")
    dry_run: bool = _env("DRY_RUN", True, bool)

    # --- universe / timeframes ---
    tokens: list = field(default_factory=lambda: _env("TOKENS", "WETH,WBTC,LINK,UNI,AAVE").split(","))
    quote_token: str = "USDC"
    entry_tf: str = _env("ENTRY_TF", "4h")
    trend_tf: str = _env("TREND_TF", "1d")
    scan_interval_sec: int = _env("SCAN_INTERVAL_SEC", 300, int)

    # --- signal quality filters ---
    min_score: int = _env("MIN_SCORE", 65, int)       # 0-100 confluence score
    min_adx: float = _env("MIN_ADX", 20.0, float)     # avoid choppy, trendless markets
    min_rr: float = _env("MIN_RR", 2.0, float)        # reject setups whose real RR is lower
    target_rr: float = _env("TARGET_RR", 3.0, float)  # preferred final target in R
    max_risk_atr: float = _env("MAX_RISK_ATR", 3.0, float)  # stop too wide -> skip
    min_risk_atr: float = _env("MIN_RISK_ATR", 0.8, float)  # stop too tight -> widen to this
    min_risk_pct: float = _env("MIN_RISK_PCT", 0.003, float)  # stop never closer than 0.3% of price
    sl_atr_buffer: float = 0.25
    btc_filter: bool = _env("BTC_FILTER", True, bool)          # only buy alts while BTC trend is up
    btc_token: str = _env("BTC_TOKEN", "WBTC")
    rs_lookback: int = _env("RS_LOOKBACK", 30, int)            # bars for relative strength vs BTC
    vol_spike_mult: float = _env("VOL_SPIKE_MULT", 2.0, float)  # skip when ATR% > 2x its median

    # --- trade management ---
    breakeven_at_r: float = _env("BREAKEVEN_AT_R", 1.0, float)
    partial_at_r: float = _env("PARTIAL_AT_R", 1.5, float)
    partial_fraction: float = _env("PARTIAL_FRACTION", 0.5, float)
    trail_atr_mult: float = _env("TRAIL_ATR_MULT", 2.5, float)
    max_bars_in_trade: int = _env("MAX_BARS_IN_TRADE", 60, int)  # 60 x 4h = 10 days

    # --- risk ---
    risk_per_trade: float = _env("RISK_PER_TRADE", 0.01, float)    # 1% equity per trade
    max_position_pct: float = _env("MAX_POSITION_PCT", 0.25, float)
    max_open_positions: int = _env("MAX_OPEN_POSITIONS", 3, int)
    max_new_per_scan: int = _env("MAX_NEW_PER_SCAN", 2, int)  # signals in one scan are correlated
    daily_loss_limit: float = _env("DAILY_LOSS_LIMIT", 0.03, float)  # stop trading for the day
    loss_cooldown_bars: int = _env("LOSS_COOLDOWN_BARS", 6, int)
    fee_rate: float = _env("FEE_RATE", 0.002, float)  # per side, fee + slippage

    log_file: str = _env("LOG_FILE", "data/logs.txt")
    state_file: str = _env("STATE_FILE", "data/state.json")
    journal_file: str = _env("JOURNAL_FILE", "data/journal.json")
    snapshot_file: str = _env("SNAPSHOT_FILE", "data/snapshot.json")
    backtest_file: str = _env("BACKTEST_FILE", "data/backtest.json")

    # --- Zorra Terminal (web UI) ---
    ui_enabled: bool = _env("UI_ENABLED", True, bool)
    ui_host: str = _env("UI_HOST", "127.0.0.1")
    ui_port: int = _env("UI_PORT", 8787, int)


CONFIG = Config()
