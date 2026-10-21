"""Recall API client, position management and risk management."""
import json
import os
from dataclasses import asdict, dataclass

import requests

from .config import TOKENS


class RecallClient:
    def __init__(self, cfg, logger):
        self.cfg = cfg
        self.log = logger
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {cfg.recall_api_key}",
            "Content-Type": "application/json",
        })

    def _get(self, path, **params):
        r = self.session.get(f"{self.cfg.recall_api_url}{path}", params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def balances(self):
        """Return {symbol: amount} for the agent's wallet."""
        data = self._get("/api/agent/balances")
        out = {}
        for b in data.get("balances", []):
            out[b.get("symbol", b.get("tokenAddress"))] = float(b.get("amount", 0))
        return out

    def portfolio_value(self):
        data = self._get("/api/agent/portfolio")
        return float(data.get("totalValue", 0))

    def execute(self, from_symbol, to_symbol, amount, reason):
        payload = {
            "fromToken": TOKENS[from_symbol],
            "toToken": TOKENS[to_symbol],
            "amount": str(amount),
            "reason": reason[:200],
        }
        if self.cfg.dry_run or not self.cfg.recall_api_key:
            self.log.info(f"[DRY-RUN] trade {payload}")
            return {"success": True, "dryRun": True}
        r = self.session.post(f"{self.cfg.recall_api_url}/api/trade/execute", json=payload, timeout=30)
        if not r.ok:
            self.log.error(f"trade failed {r.status_code}: {r.text}")
            r.raise_for_status()
        return r.json()


@dataclass
class Position:
    token: str
    entry: float
    stop: float
    initial_stop: float
    tp1: float
    target: float
    qty: float
    initial_qty: float
    opened_at: str
    bars: int = 0
    partial_done: bool = False
    highest: float = 0.0
    realized: float = 0.0  # quote PnL already booked from partial exits

    @property
    def risk(self):
        return self.entry - self.initial_stop

    def update(self, high, low, close, atr, cfg):
        """Advance one closed bar. Returns list of (reason, price, qty) exits.

        The stop is checked first, so a bar that hits both stop and target is
        counted as a loss (conservative, avoids optimistic backtests)."""
        exits = []
        self.bars += 1
        if low <= self.stop:
            exits.append(("stop", self.stop, self.qty))
            return exits

        self.highest = max(self.highest, high)
        r_now = (self.highest - self.entry) / self.risk
        cost_pad = self.entry * cfg.fee_rate * 2

        if r_now >= cfg.breakeven_at_r:
            self.stop = max(self.stop, self.entry + cost_pad)

        if not self.partial_done and high >= self.tp1:
            q = self.initial_qty * cfg.partial_fraction
            exits.append(("tp1", self.tp1, q))
            self.qty -= q
            self.partial_done = True
            self.stop = max(self.stop, self.entry + cost_pad)

        if self.partial_done:
            self.stop = max(self.stop, self.highest - cfg.trail_atr_mult * atr)

        if high >= self.target:
            exits.append(("target", self.target, self.qty))
        elif self.bars >= cfg.max_bars_in_trade:
            exits.append(("time", close, self.qty))
        return exits


class RiskManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.day = None
        self.day_start_equity = None
        self.cooldown = {}

    def new_bar(self, day, equity):
        if day != self.day:
            self.day, self.day_start_equity = day, equity
        for k in list(self.cooldown):
            self.cooldown[k] -= 1
            if self.cooldown[k] <= 0:
                del self.cooldown[k]

    def can_open(self, token, open_positions, equity):
        if token in open_positions or token in self.cooldown:
            return False
        if len(open_positions) >= self.cfg.max_open_positions:
            return False
        if self.day_start_equity and equity < self.day_start_equity * (1 - self.cfg.daily_loss_limit):
            return False
        return True

    def size(self, signal, equity, cash):
        """Quantity so that hitting the stop loses `risk_per_trade` of equity."""
        risk_cash = equity * self.cfg.risk_per_trade
        per_unit = signal.risk + signal.entry * self.cfg.fee_rate * 2
        qty = risk_cash / per_unit
        max_notional = min(equity * self.cfg.max_position_pct, cash * 0.98)
        return max(0.0, min(qty, max_notional / signal.entry))

    def on_close(self, token, pnl):
        if pnl < 0:
            self.cooldown[token] = self.cfg.loss_cooldown_bars


def load_state(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        raw = json.load(f)
    return {k: Position(**v) for k, v in raw.items()}


def save_state(path, positions):
    with open(path, "w") as f:
        json.dump({k: asdict(v) for k, v in positions.items()}, f, indent=2)
