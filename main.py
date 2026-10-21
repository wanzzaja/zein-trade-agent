"""Zein trade agent with the Zorra Terminal dashboard.

  python main.py              # live loop + dashboard (DRY_RUN=true by default)
  python main.py --scan       # scan once and print signals
  python main.py --backtest   # backtest on recent history (also shown in the dashboard)
  python main.py --ui         # dashboard only, reads the files the bot wrote
"""
import argparse
import time
from datetime import datetime, timezone

from agent.backtest import run_backtest, summarize
from agent.config import CONFIG, MARKET_SYMBOLS
from agent.dashboard import serve
from agent.journal import Journal, read_json, write_json
from agent.strategy import scan, snapshot
from agent.trader import Position, RecallClient, RiskManager, load_state, save_state
from agent.utils import drop_unclosed, fetch_history, fetch_klines, get_logger

log = get_logger(CONFIG.log_file)
paper = {"cash": 10_000.0}  # simulated USDC balance used when DRY_RUN is on
MAX_EQUITY_POINTS = 2000


def is_paper():
    return CONFIG.dry_run or not CONFIG.recall_api_key


def load_market(token, entry_limit=600, trend_limit=400):
    sym = MARKET_SYMBOLS[token]
    raw = fetch_klines(CONFIG.market_data_url, sym, CONFIG.entry_tf, entry_limit)
    t = drop_unclosed(fetch_klines(CONFIG.market_data_url, sym, CONFIG.trend_tf, trend_limit))
    return drop_unclosed(raw), t, float(raw["close"].iloc[-1])


def scan_all():
    """Scan every token. Returns (signals best-first, frames, live prices, scanner rows)."""
    markets = {}
    for token in dict.fromkeys([CONFIG.btc_token] + CONFIG.tokens):
        try:
            markets[token] = load_market(token)
        except Exception as exc:  # one bad market must not stop the scan
            log.warning(f"load {token} failed: {exc}")
    btc = markets.get(CONFIG.btc_token)
    btc = (btc[0], btc[1]) if btc else None

    signals, frames, prices, rows = [], {}, {}, []
    for token in CONFIG.tokens:
        if token not in markets:
            continue
        e, t, prices[token] = markets[token]
        try:
            sig, df, reason = scan(token, e, t, CONFIG, btc)
        except Exception as exc:
            log.warning(f"scan {token} failed: {exc}")
            continue
        frames[token] = df
        row = {"coin": token, **snapshot(df, len(df) - 1, CONFIG), "price": prices[token], "status": reason}
        if sig:
            signals.append(sig)
            row["signal"] = {"setup": sig.setup, "score": sig.score, "rr": round(sig.rr, 2),
                             "entry": sig.entry, "sl": sig.stop, "tp": sig.target, "reasons": sig.reasons}
        rows.append(row)
    # best opportunities first: higher RR and confluence
    signals.sort(key=lambda s: (s.rr * s.score), reverse=True)
    return signals, frames, prices, rows


def close_position(client, risk, journal, positions, token, reason, price, qty):
    p = positions[token]
    client.execute(token, CONFIG.quote_token, round(qty, 8), f"exit {reason} @ {price:.4f}")
    paper["cash"] += qty * price * (1 - CONFIG.fee_rate)
    p.realized += qty * (price * (1 - CONFIG.fee_rate) - p.entry * (1 + CONFIG.fee_rate))
    log.info(f"EXIT {token} {reason} qty={qty:.6f} price={price:.4f} pnl={p.realized:.2f}")
    if reason != "tp1":
        risk.on_close(token, p.realized)
        journal.close(token, p.realized / (p.initial_qty * p.risk), datetime.now(timezone.utc).isoformat())
        del positions[token]


def equity_and_cash(client, positions, prices):
    if is_paper():
        held = sum(p.qty * prices.get(p.token, p.entry) for p in positions.values())
        return paper["cash"] + held, paper["cash"]
    bal = client.balances()
    return client.portfolio_value(), bal.get(CONFIG.quote_token, 0.0)


def position_view(p, price):
    r_now = (price - p.entry) / p.risk if p.risk else 0
    return {"coin": p.token, "entry": p.entry, "stop": p.stop, "initial_stop": p.initial_stop,
            "tp1": p.tp1, "target": p.target, "qty": p.qty, "price": price, "r_now": round(r_now, 2),
            "partial": p.partial_done, "opened_at": p.opened_at, "bars": p.bars,
            "pnl": round(p.realized + p.qty * (price - p.entry), 2)}


def write_snapshot(prev, rows, positions, prices, equity, cash, btc_up):
    now = datetime.now(timezone.utc).isoformat()
    curve = (prev.get("equity_curve") or [])[-MAX_EQUITY_POINTS + 1:] + [[now, round(equity, 2)]]
    snap = {
        "updated_at": now,
        "mode": "PAPER" if is_paper() else "LIVE",
        "tf": CONFIG.entry_tf, "trend_tf": CONFIG.trend_tf,
        "btc_up": btc_up,
        "equity": round(equity, 2), "cash": round(cash, 2), "paper_cash": paper["cash"],
        "start_equity": prev.get("start_equity", round(equity, 2)),
        "equity_curve": curve,
        "scanner": rows,
        "positions": [position_view(p, prices.get(t, p.entry)) for t, p in positions.items()],
        "config": {"min_rr": CONFIG.min_rr, "target_rr": CONFIG.target_rr, "min_score": CONFIG.min_score,
                   "risk_per_trade": CONFIG.risk_per_trade, "max_open": CONFIG.max_open_positions,
                   "btc_filter": CONFIG.btc_filter},
    }
    write_json(CONFIG.snapshot_file, snap)
    return snap


def live():
    client = RecallClient(CONFIG, log)
    risk = RiskManager(CONFIG)
    journal = Journal(CONFIG.journal_file)
    positions = load_state(CONFIG.state_file)
    snap = read_json(CONFIG.snapshot_file, {})
    paper["cash"] = snap.get("paper_cash", paper["cash"])
    if CONFIG.ui_enabled:
        serve(CONFIG, log)
    log.info(f"agent started mode={'PAPER' if is_paper() else 'LIVE'} tokens={CONFIG.tokens}")
    # never re-process the bar a position was opened on (e.g. after a restart)
    last_bar = {t: p.opened_at for t, p in positions.items()}

    while True:
        signals, frames, prices, rows = scan_all()
        equity, cash = equity_and_cash(client, positions, prices)
        risk.new_bar(datetime.now(timezone.utc).date(), equity)

        for token, p in list(positions.items()):
            # intrabar stop: do not wait up to a full candle for the close
            price = prices.get(token)
            if price is not None and price <= p.stop:
                close_position(client, risk, journal, positions, token, "stop", price, p.qty)
                continue
            df = frames.get(token)
            # bars are only processed after the entry bar, never the bar the entry came from
            if df is None or last_bar.get(token) == str(df.index[-1]):
                continue
            last_bar[token] = str(df.index[-1])
            row = df.iloc[-1]
            # Position.update already reduces qty on tp1; close_position only books the fill
            for reason, px, qty in p.update(row["high"], row["low"], row["close"], row["atr"], CONFIG):
                close_position(client, risk, journal, positions, token, reason, px, qty)
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
                                            qty, qty, str(sig.time), highest=price)
            journal.open(sig, price, CONFIG.entry_tf, datetime.now(timezone.utc).isoformat())
            last_bar[sig.token] = str(sig.time)
            opened += 1
            log.info(f"ENTRY {sig.token} qty={qty:.6f} notional={notional:.2f}")

        save_state(CONFIG.state_file, positions)
        equity, cash = equity_and_cash(client, positions, prices)
        btc_frame = frames.get(CONFIG.btc_token)
        btc_up = bool(btc_frame["htf_up"].iloc[-1]) if btc_frame is not None else None
        snap = write_snapshot(snap, rows, positions, prices, equity, cash, btc_up)
        time.sleep(CONFIG.scan_interval_sec)


def backtest(bars):
    minutes = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}
    ratio = max(1, minutes[CONFIG.trend_tf] // minutes[CONFIG.entry_tf])
    data = {}
    for token in dict.fromkeys([CONFIG.btc_token] + CONFIG.tokens):
        sym = MARKET_SYMBOLS[token]
        e = fetch_history(CONFIG.market_data_url, sym, CONFIG.entry_tf, bars)
        t = fetch_history(CONFIG.market_data_url, sym, CONFIG.trend_tf, bars // ratio + 300)
        data[token] = (e, t)
        log.info(f"loaded {token}: {len(e)} {CONFIG.entry_tf} bars")
    trades, curve, still_open, index = run_backtest(data, CONFIG)
    save_backtest(trades, curve, still_open, index)
    stats = summarize(trades, curve)
    for k, v in stats.items():
        print(f"{k:>18}: {v}")
    by_exit = {}
    for tr in trades:
        by_exit[tr["exit"]] = by_exit.get(tr["exit"], 0) + 1
    print(f"{'exits':>18}: {by_exit}")
    print(f"results saved to {CONFIG.backtest_file}; open the dashboard with: python main.py --ui")
    return stats


def save_backtest(trades, curve, still_open, index):
    step = max(1, len(curve) // MAX_EQUITY_POINTS)
    write_json(CONFIG.backtest_file, {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tf": CONFIG.entry_tf, "trend_tf": CONFIG.trend_tf, "tokens": CONFIG.tokens,
        "summary": summarize(trades, curve),
        "records": [{k: v for k, v in t.items() if k != "r"} for t in trades] + still_open,
        "equity_curve": [[str(index[i]), round(float(curve[i]), 2)] for i in range(0, len(curve), step)],
    })


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--ui", action="store_true")
    ap.add_argument("--bars", type=int, default=6000)
    args = ap.parse_args()
    if args.backtest:
        backtest(args.bars)
    elif args.scan:
        sigs, _, _, rows = scan_all()
        for r in rows:
            print(f"{r['coin']:>6} {r['price']:>12.4f}  {r['status']}")
        print("\n".join(s.describe() for s in sigs) or "no signal passed the filters")
    elif args.ui:
        serve(CONFIG, log, background=False)
    else:
        live()
