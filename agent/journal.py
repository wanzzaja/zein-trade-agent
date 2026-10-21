"""Signal journal and dashboard snapshot, shared between the bot and the UI.

Records use the same columns as the ZIUZ signal export so results stay comparable."""
import csv
import json
import math
import os

FIELDS = ["time", "tf", "coin", "dir", "entry", "sl", "tp", "score", "exitMode",
          "setup", "status", "R", "exitTime"]
SCRATCH_R = 0.15  # |R| below this is a scratch, not a win or loss


def status_of(r):
    if r is None:
        return "open"
    if r >= SCRATCH_R:
        return "win"
    if r <= -SCRATCH_R:
        return "loss"
    return "scratch"


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _clean(obj):
    """Replace NaN/inf (invalid JSON for browsers) with None and numpy scalars with Python ones."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):
        obj = obj.item()
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def write_json(path, data):
    """Atomic write so the UI never reads a half-written file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(_clean(data), f, default=str)
    os.replace(tmp, path)


def record(sig, entry, tf, time=None):
    return {
        "time": str(time or sig.time), "tf": tf, "coin": sig.token, "dir": "long",
        "entry": entry, "sl": sig.stop, "tp": sig.target, "score": sig.score,
        "exitMode": "trail", "setup": sig.setup, "status": "open", "R": None, "exitTime": None,
    }


def close_record(rec, r, exit_time):
    rec["R"] = round(float(r), 4)
    rec["status"] = status_of(rec["R"])
    rec["exitTime"] = str(exit_time)
    return rec


class Journal:
    def __init__(self, path):
        self.path = path
        self.records = read_json(path, [])

    def open(self, sig, entry, tf, time=None):
        self.records.append(record(sig, entry, tf, time))
        self.save()

    def close(self, coin, r, exit_time):
        for rec in reversed(self.records):
            if rec["coin"] == coin and rec["status"] == "open":
                close_record(rec, r, exit_time)
                break
        self.save()

    def save(self):
        write_json(self.path, self.records)


def to_csv(records, fh):
    w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
    w.writeheader()
    for rec in records:
        w.writerow({k: ("" if rec.get(k) is None else rec.get(k)) for k in FIELDS})


def stats(records):
    closed = [r for r in records if r.get("R") is not None]
    rs = [float(r["R"]) for r in closed]
    gains = sum(x for x in rs if x > 0)
    losses = -sum(x for x in rs if x < 0)
    by_tf = {}
    for r in closed:
        by_tf.setdefault(r["tf"], []).append(float(r["R"]))
    streak = worst = 0
    for x in rs:
        streak = streak + 1 if x < 0 else 0
        worst = max(worst, streak)
    return {
        "trades": len(closed),
        "open": sum(1 for r in records if r.get("status") == "open"),
        "sum_r": round(sum(rs), 2),
        "avg_r": round(sum(rs) / len(rs), 2) if rs else 0,
        "win_rate": round(100 * sum(1 for r in closed if r["status"] == "win") / len(closed), 1) if closed else 0,
        "profit_factor": round(gains / losses, 2) if losses else (None if not gains else 99.0),
        "best_r": round(max(rs), 2) if rs else 0,
        "worst_r": round(min(rs), 2) if rs else 0,
        "loss_streak": worst,
        "by_tf": {k: round(sum(v), 2) for k, v in by_tf.items()},
    }
