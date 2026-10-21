"""Walk-forward bar-by-bar backtest using exactly the live signal + management logic."""
import numpy as np

from .journal import close_record, record
from .strategy import evaluate, prepare
from .trader import Position, RiskManager


def run_backtest(data, cfg, start_equity=10_000.0):
    """data: {token: (entry_df, trend_df)}.

    Returns (closed trades, equity curve, still-open journal records, bar index)."""
    btc = data.get(cfg.btc_token)
    # BTC may be loaded only as the regime reference; trade just the configured tokens
    frames = {t: prepare(e, h, btc) for t, (e, h) in data.items() if t in cfg.tokens}
    index = sorted(set().union(*[f.index for f in frames.values()]))
    pos_idx = {t: {ts: n for n, ts in enumerate(f.index)} for t, f in frames.items()}

    cash, positions, trades, curve = start_equity, {}, [], []
    open_recs = {}
    risk = RiskManager(cfg)

    def equity_at(ts):
        val = cash
        for t, p in positions.items():
            f = frames[t]
            n = pos_idx[t].get(ts)
            px = f["close"].iloc[n] if n is not None else p.entry
            val += p.qty * px
        return val

    for ts in index:
        eq = equity_at(ts)
        risk.new_bar(ts.date(), eq)

        # manage open positions on this bar
        for t in list(positions):
            n = pos_idx[t].get(ts)
            if n is None:
                continue
            row = frames[t].iloc[n]
            p = positions[t]
            for reason, price, qty in p.update(row["high"], row["low"], row["close"], row["atr"], cfg):
                if reason == "stop":
                    price = min(price, row["open"])  # gap through the stop fills worse
                proceeds = qty * price * (1 - cfg.fee_rate)
                cash += proceeds
                p.realized += proceeds - qty * p.entry * (1 + cfg.fee_rate)
                if reason in ("stop", "target", "time"):
                    r_mult = p.realized / (p.initial_qty * p.risk)
                    rec = close_record(open_recs.pop(t), r_mult, ts)
                    trades.append({**rec, "exit": reason, "pnl": p.realized, "r": r_mult})
                    risk.on_close(t, p.realized)
                    del positions[t]
                    break

        # look for new entries on closed bar
        eq = equity_at(ts)
        candidates = []
        for t, f in frames.items():
            n = pos_idx[t].get(ts)
            if n is None or not risk.can_open(t, positions, eq):
                continue
            sig = evaluate(f, n, t, cfg)
            if sig is not None:
                candidates.append(sig)
        # signals firing on the same bar are one correlated bet: keep only the best few
        candidates.sort(key=lambda s: s.rr * s.score, reverse=True)
        for sig in candidates[:cfg.max_new_per_scan]:
            t = sig.token
            if not risk.can_open(t, positions, eq):
                continue
            qty = risk.size(sig, eq, cash)
            if qty * sig.entry < 10:
                continue
            cash -= qty * sig.entry * (1 + cfg.fee_rate)
            positions[t] = Position(t, sig.entry, sig.stop, sig.stop, sig.tp1, sig.target,
                                    qty, qty, str(ts), highest=sig.entry)
            open_recs[t] = record(sig, sig.entry, cfg.entry_tf)
        curve.append(equity_at(ts))
    # positions still running at the end of the data
    trades_open = list(open_recs.values())
    return trades, np.array(curve), trades_open, index


def summarize(trades, curve, start_equity=10_000.0):
    if not trades:
        return {"trades": 0}
    r = np.array([t["r"] for t in trades])
    pnl = np.array([t["pnl"] for t in trades])
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    peak = np.maximum.accumulate(curve)
    out = {
        "trades": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "avg_R": round(r.mean(), 2),
        "avg_win_R": round(r[r > 0].mean(), 2) if (r > 0).any() else 0,
        "avg_loss_R": round(r[r <= 0].mean(), 2) if (r <= 0).any() else 0,
        "profit_factor": round(wins.sum() / abs(losses.sum()), 2) if losses.sum() else float("inf"),
        "return_pct": round((curve[-1] / start_equity - 1) * 100, 2),
        "max_drawdown_pct": round(((curve - peak) / peak).min() * 100, 2),
    }
    return {k: (v if isinstance(v, int) else float(v)) for k, v in out.items()}
