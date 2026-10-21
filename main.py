"""Zein trade agent.

  python main.py              # live loop (DRY_RUN=true by default)
  python main.py --scan       # scan once and print signals
  python main.py --backtest   # backtest on recent history
"""
import argparse
import time
from datetime import datetime, timezone

from agent.backtest import run_backtest, summarize
from agent.config import CONFIG, MARKET_SYMBOLS
from agent.strategy import scan
from agent.trader import Position, RecallClient, RiskManager, load_state, save_state
from agent.utils import drop_unclosed, fetch_history, fetch_klines, get_logger

log = get_logger(CONFIG.log_file)
paper = {"cash": 10_000.0}  # simulated USDC balance used when DRY_RUN is on


def load_market(token, entry_limit=600, trend_limit=400):
    sym = MARKET_SYMBOLS[token]
    raw = fetch_klines(CONFIG.market_data_url, sym, CONFIG.entry_tf, entry_limit)
    t = drop_unclosed(fetch_klines(CONFIG.market_data_url, sym, CONFIG.trend_tf, trend_limit))
    return drop_unclosed(raw), t, float(raw["close"].iloc[-1])


def scan_all():
    signals, frames, prices = [], {}, {}
    for token in CONFIG.tokens:
        try:
            e, t, prices[token] = load_market(token)
            sig, df = scan(token, e, t, CONFIG)
            frames[token] = df
            if sig:
                signals.append(sig)
        except Exception as exc:  # one bad market must not stop the scan
            log.warning(f"scan {token} failed: {exc}")
    # best opportunities first: higher RR and confluence
    signals.sort(key=lambda s: (s.rr * s.score), reverse=True)
    return signals, frames, prices


def close_position(client, risk, positions, token, reason, price, qty):
    p = positions[token]
    client.execute(token, CONFIG.quote_token, round(qty, 8), f"exit {reason} @ {price:.4f}")
    paper["cash"] += qty * price * (1 - CONFIG.fee_rate)
    p.realized += qty * (price * (1 - CONFIG.fee_rate) - p.entry * (1 + CONFIG.fee_rate))
    log.info(f"EXIT {token} {reason} qty={qty:.6f} price={price:.4f} pnl={p.realized:.2f}")
    if reason != "tp1":
        risk.on_close(token, p.realized)
        del positions[token]


def equity_and_cash(client, positions, frames):
    if CONFIG.dry_run or not CONFIG.recall_api_key:
        cash = paper["cash"]
        held = sum(p.qty * float(frames[p.token]["close"].iloc[-1]) for p in positions.values() if p.token in frames)
        return cash + held, cash
    bal = client.balances()
    return client.portfolio_value(), bal.get(CONFIG.quote_token, 0.0)


def live():
    client = RecallClient(CONFIG, log)
    risk = RiskManager(CONFIG)
    positions = load_state(CONFIG.state_file)
    log.info(f"agent started dry_run={CONFIG.dry_run} tokens={CONFIG.tokens}")
    # never re-process the bar a position was opened on (e.g. after a restart)
    last_bar = {t: p.opened_at for t, p in positions.items()}

    while True:
        signals, frames, prices = scan_all()
        equity, cash = equity_and_cash(client, positions, frames)
        risk.new_bar(datetime.now(timezone.utc).date(), equity)

        for token, p in list(positions.items()):
            # intrabar stop: do not wait up to a full candle for the close
            price = prices.get(token)
            if price is not None and price <= p.stop:
                close_position(client, risk, positions, token, "stop", price, p.qty)
                continue
            df = frames.get(token)
            # bars are only processed after the entry bar, never the bar the entry came from
            if df is None or last_bar.get(token) == str(df.index[-1]):
                continue
            last_bar[token] = str(df.index[-1])
            row = df.iloc[-1]
            # Position.update already reduces qty on tp1; close_position only books the fill
            for reason, px, qty in p.update(row["high"], row["low"], row["close"], row["atr"], CONFIG):
                close_position(client, risk, positions, token, reason, px, qty)
                if token not in positions:
                    break

        opened = 0
        for sig in signals:
            log.info(f"SIGNAL {sig.describe()}")
            if opened >= CONFIG.max_new_per_scan or not risk.can_open(sig.token, positions, equity):
                continue
            price = prices.get(sig.token, sig.entry)
            # the order fills at today's price, not the candle close: skip chased or broken setups
            if price > sig.entry + 0.25 * sig.risk or price <= sig.stop:
                log.info(f"SKIP {sig.token}: price {price:.4f} moved away from signal entry {sig.entry:.4f}")
                continue
            qty = risk.size(sig, equity, cash)
            notional = qty * price
            if notional < 10:
                continue
            client.execute(CONFIG.quote_token, sig.token, round(notional, 2),
                           f"{sig.setup} score {sig.score} RR {sig.rr:.2f}")
            paper["cash"] -= notional
            cash -= notional
            positions[sig.token] = Position(sig.token, price, sig.stop, sig.stop, sig.tp1, sig.target,
                                            qty, qty, str(sig.time), highest=sig.entry)
            last_bar[sig.token] = str(sig.time)
            opened += 1
            log.info(f"ENTRY {sig.token} qty={qty:.6f} notional={notional:.2f}")

        save_state(CONFIG.state_file, positions)
        time.sleep(CONFIG.scan_interval_sec)


def backtest(bars):
    minutes = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}
    ratio = max(1, minutes[CONFIG.trend_tf] // minutes[CONFIG.entry_tf])
    data = {}
    for token in CONFIG.tokens:
        sym = MARKET_SYMBOLS[token]
        e = fetch_history(CONFIG.market_data_url, sym, CONFIG.entry_tf, bars)
        t = fetch_history(CONFIG.market_data_url, sym, CONFIG.trend_tf, bars // ratio + 300)
        data[token] = (e, t)
        log.info(f"loaded {token}: {len(e)} {CONFIG.entry_tf} bars")
    trades, curve = run_backtest(data, CONFIG)
    stats = summarize(trades, curve)
    for k, v in stats.items():
        print(f"{k:>18}: {v}")
    by_exit = {}
    for tr in trades:
        by_exit[tr["exit"]] = by_exit.get(tr["exit"], 0) + 1
    print(f"{'exits':>18}: {by_exit}")
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--bars", type=int, default=6000)
    args = ap.parse_args()
    if args.backtest:
        backtest(args.bars)
    elif args.scan:
        sigs, _ = scan_all()
        print("\n".join(s.describe() for s in sigs) or "no signal passed the filters")
    else:
        live()
