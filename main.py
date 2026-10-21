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
    e = drop_unclosed(fetch_klines(CONFIG.market_data_url, sym, CONFIG.entry_tf, entry_limit))
    t = drop_unclosed(fetch_klines(CONFIG.market_data_url, sym, CONFIG.trend_tf, trend_limit))
    return e, t


def scan_all():
    signals, frames = [], {}
    for token in CONFIG.tokens:
        try:
            e, t = load_market(token)
            sig, df = scan(token, e, t, CONFIG)
            frames[token] = df
            if sig:
                signals.append(sig)
        except Exception as exc:  # one bad market must not stop the scan
            log.warning(f"scan {token} failed: {exc}")
    # best opportunities first: higher RR and confluence
    signals.sort(key=lambda s: (s.rr * s.score), reverse=True)
    return signals, frames


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
        signals, frames = scan_all()
        equity, cash = equity_and_cash(client, positions, frames)
        risk.new_bar(datetime.now(timezone.utc).date(), equity)

        for token, p in list(positions.items()):
            df = frames.get(token)
            if df is None or last_bar.get(token) == str(df.index[-1]):
                continue
            last_bar[token] = str(df.index[-1])
            row = df.iloc[-1]
            for reason, price, qty in p.update(row["high"], row["low"], row["close"], row["atr"], CONFIG):
                client.execute(token, CONFIG.quote_token, round(qty, 8), f"exit {reason} @ {price:.4f}")
                paper["cash"] += qty * price * (1 - CONFIG.fee_rate)
                p.realized += qty * (price * (1 - CONFIG.fee_rate) - p.entry * (1 + CONFIG.fee_rate))
                log.info(f"EXIT {token} {reason} qty={qty:.6f} price={price:.4f} pnl={p.realized:.2f}")
                if reason in ("stop", "target", "time"):
                    risk.on_close(token, p.realized)
                    del positions[token]
                    break

        for sig in signals:
            log.info(f"SIGNAL {sig.describe()}")
            if not risk.can_open(sig.token, positions, equity):
                continue
            qty = risk.size(sig, equity, cash)
            notional = qty * sig.entry
            if notional < 10:
                continue
            client.execute(CONFIG.quote_token, sig.token, round(notional, 2),
                           f"{sig.setup} score {sig.score} RR {sig.rr:.2f}")
            paper["cash"] -= notional
            cash -= notional
            positions[sig.token] = Position(sig.token, sig.entry, sig.stop, sig.stop, sig.tp1, sig.target,
                                            qty, qty, str(sig.time), highest=sig.entry)
            last_bar[sig.token] = str(sig.time)
            log.info(f"ENTRY {sig.token} qty={qty:.6f} notional={notional:.2f}")

        save_state(CONFIG.state_file, positions)
        time.sleep(CONFIG.scan_interval_sec)


def backtest(bars):
    ratio = {"15m": 16, "1h": 4, "4h": 1}.get(CONFIG.entry_tf, 4)
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
