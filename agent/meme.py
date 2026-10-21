"""Meme coin screener for Solana, BNB Chain, Base and Ethereum.

Pipeline
  1. discover  - trending + new pools (GeckoTerminal) and promoted profiles/boosts (DexScreener)
  2. market    - liquidity, market cap, volume, txns, age and socials (DexScreener)
  3. prefilter - drop illiquid, inactive, too young or too large tokens before spending API calls
  4. security  - GoPlus + honeypot.is simulation on EVM chains, RugCheck on Solana
  5. score     - hard red flags reject a token outright; the rest get a 0-100 quality score from
                 contract safety (35), holder distribution (25), liquidity (15), momentum (20)
                 and community (5)

The score ranks quality and risk. It is not a price prediction: most meme coins still go to zero.
"""
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from .journal import read_json, write_json

CHAINS = {
    "solana": {"label": "SOL", "gecko": "solana", "goplus": None, "honeypot": None,
               "explorer": "https://solscan.io/token/"},
    "bsc": {"label": "BSC", "gecko": "bsc", "goplus": "56", "honeypot": 56,
            "explorer": "https://bscscan.com/token/"},
    "base": {"label": "BASE", "gecko": "base", "goplus": "8453", "honeypot": 8453,
             "explorer": "https://basescan.org/token/"},
    "ethereum": {"label": "ETH", "gecko": "eth", "goplus": "1", "honeypot": 1,
                 "explorer": "https://etherscan.io/token/"},
}
# majors, stables and wrapped natives are never meme candidates
MAJORS = {"SOL", "WSOL", "ETH", "WETH", "BNB", "WBNB", "BTC", "WBTC", "CBBTC", "USDC", "USDT", "DAI",
          "BUSD", "FDUSD", "USDE", "USD1", "PYUSD", "TUSD", "JITOSOL", "MSOL", "STETH", "WSTETH", "CBETH"}
BURN_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "0xdead000000000000000042069420694206942069",
}
EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
SOL_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
WINDOWS = ("m5", "h1", "h6", "h24")


def valid_address(chain, address):
    if chain not in CHAINS or not isinstance(address, str):
        return False
    return bool(SOL_RE.match(address) if chain == "solana" else EVM_RE.match(address))


def norm_address(chain, address):
    return address if chain == "solana" else address.lower()


def num(v, default=0.0):
    try:
        f = float(v)
        return f if f == f else default  # NaN -> default
    except (TypeError, ValueError):
        return default


def opt_num(v):
    return None if v in (None, "") else num(v, None)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ http

class Http:
    """requests wrapper with a per-host minimum interval (free API rate limits) and retries."""

    INTERVAL = {"api.dexscreener.com": 1.1, "api.geckoterminal.com": 2.2, "api.gopluslabs.io": 2.2,
                "api.rugcheck.xyz": 1.2, "api.honeypot.is": 1.0}

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "zorra-terminal/1.0"})
        self.last = {}
        self.locks = {}
        self.guard = threading.Lock()

    def _throttle(self, host):
        with self.guard:
            lock = self.locks.setdefault(host, threading.Lock())
        with lock:
            wait = self.INTERVAL.get(host, 0.5) - (time.time() - self.last.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            self.last[host] = time.time()

    def get(self, url, params=None):
        host = urlparse(url).netloc
        for attempt in range(3):
            self._throttle(host)
            try:
                r = self.session.get(url, params=params, timeout=20)
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 404:
                return None
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 2:
                    r.raise_for_status()
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        return None


http = Http()


# ------------------------------------------------------------------ discovery

def discover(cfg, chains, errors):
    """Candidate tokens: {(chain, address): {"sources": set, "buyers_h24": int|None, ...}}."""
    found = {}

    def add(chain, address, source, attrs=None):
        if chain not in chains or not address or not valid_address(chain, address):
            return
        entry = found.setdefault((chain, norm_address(chain, address)), {"sources": set()})
        entry["sources"].add(source)
        tx = ((attrs or {}).get("transactions") or {}).get("h24") or {}
        if tx.get("buyers") is not None:
            entry["buyers_h24"] = int(num(tx.get("buyers")))
            entry["sellers_h24"] = int(num(tx.get("sellers")))

    for path, source in (("/token-profiles/latest/v1", "profile"),
                         ("/token-boosts/latest/v1", "boost"),
                         ("/token-boosts/top/v1", "boost")):
        try:
            items = http.get(cfg.dexscreener_url + path) or []
            for item in items if isinstance(items, list) else []:
                add(item.get("chainId"), item.get("tokenAddress"), source)
        except Exception as exc:
            errors.append(f"dexscreener {path}: {exc}")

    for chain in chains:
        net = CHAINS[chain]["gecko"]
        for kind, source in (("trending_pools", "trending"), ("new_pools", "new")):
            try:
                data = http.get(f"{cfg.geckoterminal_url}/networks/{net}/{kind}", {"page": 1}) or {}
            except Exception as exc:
                errors.append(f"geckoterminal {net}/{kind}: {exc}")
                continue
            for pool in data.get("data") or []:
                token_id = (((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
                if token_id.startswith(net + "_"):
                    add(chain, token_id[len(net) + 1:], source, pool.get("attributes"))
    return found


# ------------------------------------------------------------------ market data

def _social_url(kind, value):
    if not value:
        return None
    if value.startswith("http"):
        return value
    if kind == "twitter":
        return "https://x.com/" + value.lstrip("@")
    if kind == "telegram":
        return "https://t.me/" + value.lstrip("@")
    return None


def parse_socials(pairs):
    s = {"twitter": None, "telegram": None, "website": None, "discord": None}
    for p in pairs:
        info = p.get("info") or {}
        for w in info.get("websites") or []:
            url = w.get("url") if isinstance(w, dict) else w
            if url and not s["website"]:
                s["website"] = url
        for so in info.get("socials") or []:
            kind = (so.get("type") or so.get("platform") or "").lower()
            kind = "twitter" if kind in ("twitter", "x") else kind
            if kind in s and not s[kind]:
                s[kind] = _social_url(kind, so.get("url") or so.get("handle"))
    return s


def market_from_pairs(pairs, now_ms=None):
    """Aggregate every DexScreener pair of one token (volume/liquidity summed, age from the oldest)."""
    if not pairs:
        return None
    now_ms = now_ms or time.time() * 1000
    main = max(pairs, key=lambda p: num((p.get("liquidity") or {}).get("usd")))

    def total(field, window):
        return sum(num((p.get(field) or {}).get(window)) for p in pairs)

    txns = {w: {k: int(sum(num(((p.get("txns") or {}).get(w) or {}).get(k)) for p in pairs))
                for k in ("buys", "sells")} for w in WINDOWS}
    created = [num(p.get("pairCreatedAt")) for p in pairs if p.get("pairCreatedAt")]
    base = main.get("baseToken") or {}
    icon = next(((p.get("info") or {}).get("imageUrl") for p in pairs if (p.get("info") or {}).get("imageUrl")), None)
    return {
        "symbol": base.get("symbol") or "?",
        "name": base.get("name") or "",
        "icon": icon,
        "dex": main.get("dexId"),
        "url": main.get("url"),
        "pair": main.get("pairAddress"),
        "pairs": [p.get("pairAddress") for p in pairs if p.get("pairAddress")],
        "price": num(main.get("priceUsd")),
        "mcap": num(main.get("marketCap")) or num(main.get("fdv")),
        "fdv": num(main.get("fdv")),
        "liquidity": total("liquidity", "usd"),
        "volume": {w: total("volume", w) for w in WINDOWS},
        "txns": txns,
        "change": {w: opt_num((main.get("priceChange") or {}).get(w)) for w in WINDOWS},
        "age_h": (now_ms - min(created)) / 3.6e6 if created else None,
        "socials": parse_socials(pairs),
        "boosts": int(num((main.get("boosts") or {}).get("active"))),
    }


def fetch_markets(cfg, chain, addresses):
    grouped = {}
    for i in range(0, len(addresses), 30):  # endpoint takes up to 30 addresses
        batch = addresses[i:i + 30]
        pairs = http.get(f"{cfg.dexscreener_url}/tokens/v1/{chain}/{','.join(batch)}") or []
        if isinstance(pairs, dict):
            pairs = pairs.get("pairs") or []
        for p in pairs:
            base = (p.get("baseToken") or {}).get("address")
            if p.get("chainId") == chain and base:
                grouped.setdefault(norm_address(chain, base), []).append(p)
    return {k: market_from_pairs(v) for k, v in grouped.items()}


def prefilter(m, cfg):
    """Cheap market checks. Returns a rejection reason or None."""
    if m["symbol"].upper() in MAJORS:
        return "bukan meme coin (major/stable)"
    if m["liquidity"] < cfg.meme_min_liquidity:
        return f"likuiditas < ${cfg.meme_min_liquidity:,.0f}"
    if m["mcap"] < cfg.meme_min_mcap:
        return f"market cap < ${cfg.meme_min_mcap:,.0f}"
    if m["mcap"] > cfg.meme_max_mcap:
        return f"market cap > ${cfg.meme_max_mcap:,.0f} (ruang naik terbatas)"
    if m["age_h"] is not None and m["age_h"] < cfg.meme_min_age_hours:
        return f"umur < {cfg.meme_min_age_hours:g} jam (zona sniper/rug)"
    if m["age_h"] is not None and m["age_h"] > cfg.meme_max_age_days * 24:
        return f"umur > {cfg.meme_max_age_days:g} hari"
    if m["volume"]["h24"] < cfg.meme_min_volume_24h:
        return f"volume 24j < ${cfg.meme_min_volume_24h:,.0f}"
    tx = m["txns"]["h24"]
    if tx["buys"] + tx["sells"] < cfg.meme_min_txns_24h:
        return f"transaksi 24j < {cfg.meme_min_txns_24h}"
    return None


# ------------------------------------------------------------------ security

def _empty_security(source):
    return {"source": source, "honeypot": None, "buy_tax": None, "sell_tax": None, "renounced": None,
            "mintable": None, "freezable": None, "open_source": None, "danger": [], "warnings": [],
            "lp_locked_pct": None, "bonding_curve": False, "top10_pct": None, "top1_pct": None,
            "dev_pct": None, "holders": None, "insiders": None, "rugged": False, "risk_score": None}


def _pct_fraction(v):
    """GoPlus reports shares and taxes as fractions ("0.05" = 5%)."""
    f = opt_num(v)
    if f is None:
        return None
    return f * 100 if f <= 1 else f


def norm_goplus(d, pairs=()):
    sec = _empty_security("goplus")
    flag = lambda k: str(d.get(k, "")) == "1"
    known = lambda k: str(d.get(k, "")) in ("0", "1")

    owner = (d.get("owner_address") or "").lower()
    sec["renounced"] = owner == "" or owner in BURN_ADDRESSES
    sec["honeypot"] = flag("is_honeypot") if known("is_honeypot") else None
    sec["buy_tax"] = _pct_fraction(d.get("buy_tax"))
    sec["sell_tax"] = _pct_fraction(d.get("sell_tax"))
    sec["open_source"] = flag("is_open_source") if known("is_open_source") else None
    sec["mintable"] = flag("is_mintable") if known("is_mintable") else None
    sec["freezable"] = flag("transfer_pausable") if known("transfer_pausable") else None

    always_fatal = {
        "hidden_owner": "hidden owner",
        "can_take_back_ownership": "owner bisa diambil kembali",
        "owner_change_balance": "owner bisa mengubah saldo",
        "selfdestruct": "fungsi selfdestruct",
        "cannot_sell_all": "tidak bisa jual semua token",
        "honeypot_with_same_creator": "creator pernah membuat honeypot",
        "is_airdrop_scam": "airdrop scam",
    }
    # dangerous only while someone still controls the contract
    owner_fatal = {
        "is_mintable": "supply bisa dicetak (mint)",
        "transfer_pausable": "transfer bisa di-pause",
        "slippage_modifiable": "pajak bisa diubah owner",
        "personal_slippage_modifiable": "pajak per-wallet bisa diubah",
    }
    risky = {
        "is_blacklisted": "ada fungsi blacklist",
        "is_proxy": "kontrak proxy (logika bisa di-upgrade)",
        "external_call": "memanggil kontrak eksternal",
        "trading_cooldown": "ada cooldown trading",
        "anti_whale_modifiable": "batas anti-whale bisa diubah",
    }
    sec["danger"] += [label for k, label in always_fatal.items() if flag(k)]
    for k, label in owner_fatal.items():
        if flag(k):
            (sec["warnings"] if sec["renounced"] else sec["danger"]).append(label)
    for k, label in risky.items():
        if flag(k) and not (sec["renounced"] and k in ("is_blacklisted", "trading_cooldown", "anti_whale_modifiable")):
            sec["warnings"].append(label)
    if d.get("is_open_source") == "0":
        sec["danger"].append("kontrak tidak terverifikasi (closed source)")

    lp = d.get("lp_holders") or []
    if lp:
        sec["lp_locked_pct"] = round(sum(
            num(h.get("percent")) for h in lp
            if str(h.get("is_locked")) == "1" or (h.get("address") or "").lower() in BURN_ADDRESSES) * 100, 2)

    pools = {a.lower() for a in pairs if a} | {(x.get("pair") or "").lower() for x in d.get("dex") or []}
    holders = [h for h in d.get("holders") or []
               if (h.get("address") or "").lower() not in pools | BURN_ADDRESSES
               and str(h.get("is_locked")) != "1"]
    shares = sorted((num(h.get("percent")) * 100 for h in holders), reverse=True)
    if d.get("holders") is not None:
        sec["top10_pct"] = round(sum(shares[:10]), 2)
        sec["top1_pct"] = round(shares[0], 2) if shares else 0.0
    dev = [v for v in (_pct_fraction(d.get("creator_percent")), _pct_fraction(d.get("owner_percent"))) if v is not None]
    sec["dev_pct"] = round(max(dev), 2) if dev else None
    sec["holders"] = int(num(d.get("holder_count"))) if d.get("holder_count") not in (None, "") else None
    return sec


def merge_honeypot_is(sec, hp):
    """honeypot.is simulates a buy and a sell; a failed sell overrides GoPlus."""
    if not hp:
        return sec
    if (hp.get("honeypotResult") or {}).get("isHoneypot") is True:
        sec["honeypot"] = True
    elif hp.get("simulationSuccess") and sec["honeypot"] is None:
        sec["honeypot"] = False
    sim = hp.get("simulationResult") or {}
    for key, field in (("buy_tax", "buyTax"), ("sell_tax", "sellTax")):
        v = opt_num(sim.get(field))
        if v is not None:
            sec[key] = max(v, sec[key] or 0)
    return sec


def norm_rugcheck(r):
    sec = _empty_security("rugcheck")
    token = r.get("token") or {}
    mint_auth = r.get("mintAuthority", token.get("mintAuthority"))
    freeze_auth = r.get("freezeAuthority", token.get("freezeAuthority"))
    sec["renounced"] = not mint_auth
    sec["mintable"] = bool(mint_auth)
    sec["freezable"] = bool(freeze_auth)
    sec["honeypot"] = None  # SPL tokens cannot block sells without a freeze authority
    sec["rugged"] = r.get("rugged") is True
    sec["risk_score"] = r.get("score_normalised")

    fee = num((r.get("transferFee") or {}).get("pct"))
    sec["buy_tax"] = sec["sell_tax"] = fee
    if mint_auth:
        sec["danger"].append("mint authority masih aktif")
    if freeze_auth:
        sec["danger"].append("freeze authority aktif (wallet bisa dibekukan)")
    if fee > 0:
        sec["warnings"].append(f"token-2022 transfer fee {fee:g}%")
    if (r.get("tokenMeta") or {}).get("mutable"):
        sec["warnings"].append("metadata masih bisa diubah")
    for risk in r.get("risks") or []:
        name = risk.get("name") or ""
        if risk.get("level") == "danger" and "authority" not in name.lower():
            sec["warnings"].append(f"RugCheck: {name}")

    markets = r.get("markets") or []
    pools = set()
    for m in markets:
        pools |= {m.get("pubkey"), m.get("liquidityA"), m.get("liquidityB")}
        if "pump" in (m.get("marketType") or "") and "swap" not in (m.get("marketType") or ""):
            sec["bonding_curve"] = True
    pools.discard(None)
    if markets and not sec["bonding_curve"]:
        def depth(m):
            lp = m.get("lp") or {}
            return num(lp.get("baseUSD")) + num(lp.get("quoteUSD"))
        main = max(markets, key=depth)
        locked = opt_num((main.get("lp") or {}).get("lpLockedPct"))
        sec["lp_locked_pct"] = round(locked, 2) if locked is not None else None

    supply = num(token.get("supply"))
    holders = [h for h in r.get("topHolders") or []
               if h.get("address") not in pools and h.get("owner") not in pools]

    def share(h):
        if supply and h.get("amount") is not None:
            return num(h.get("amount")) / supply * 100
        return num(h.get("pct"))

    shares = sorted((share(h) for h in holders), reverse=True)
    if r.get("topHolders") is not None:
        sec["top10_pct"] = round(sum(shares[:10]), 2)
        sec["top1_pct"] = round(shares[0], 2) if shares else 0.0
    creator = r.get("creator")
    if supply and r.get("creatorBalance") is not None:
        sec["dev_pct"] = round(num(r.get("creatorBalance")) / supply * 100, 2)
    elif creator:
        sec["dev_pct"] = round(sum(share(h) for h in holders if h.get("owner") == creator), 2)
    sec["holders"] = int(num(r.get("totalHolders"))) if r.get("totalHolders") is not None else None
    insiders = r.get("graphInsidersDetected")
    if insiders is None:
        insiders = sum(1 for h in r.get("topHolders") or [] if h.get("insider"))
    sec["insiders"] = int(num(insiders))
    return sec


def fetch_security(cfg, chain, address, market):
    if chain == "solana":
        report = http.get(f"{cfg.rugcheck_url}/tokens/{address}/report")
        return norm_rugcheck(report) if report else None
    data = http.get(f"{cfg.goplus_url}/token_security/{CHAINS[chain]['goplus']}",
                    {"contract_addresses": address}) or {}
    result = data.get("result") or {}
    raw = result.get(address.lower()) or next(iter(result.values()), None)
    if not raw:
        return None
    sec = norm_goplus(raw, market.get("pairs") or [])
    try:
        hp = http.get(f"{cfg.honeypot_url}/v2/IsHoneypot",
                      {"address": address, "chainID": CHAINS[chain]["honeypot"]})
        merge_honeypot_is(sec, hp)
    except Exception:
        sec["warnings"].append("simulasi honeypot.is gagal")
    return sec


# ------------------------------------------------------------------ scoring

def score_token(m, sec, cfg):
    """Return (score 0-100, grade, checks, reject reasons, tags)."""
    checks, reject, tags = [], [], []
    pts = 0.0

    def add(group, label, status, detail, points=0):
        nonlocal pts
        pts += points
        checks.append({"group": group, "label": label, "status": status, "detail": detail})

    def fail(group, label, detail):
        reject.append(f"{label}: {detail}")
        add(group, label, "fail", detail)

    chain = m["chain"]
    tax = None
    # ---- contract safety (35) ----
    if sec is None:
        fail("security", "Data keamanan", "tidak tersedia, token tidak bisa diverifikasi")
        sec = _empty_security("none")
    else:
        if sec["rugged"]:
            fail("security", "Rug", "RugCheck menandai token ini sudah di-rug")
        if sec["honeypot"]:
            fail("security", "Honeypot", "simulasi jual gagal, token tidak bisa dijual")
        elif sec["honeypot"] is False:
            add("security", "Honeypot", "pass", "simulasi beli & jual berhasil")
        elif chain != "solana":
            add("security", "Honeypot", "warn", "simulasi jual tidak tersedia")

        taxes = [t for t in (sec["buy_tax"], sec["sell_tax"]) if t is not None]
        tax = max(taxes) if taxes else None
        tax_txt = f"beli {sec['buy_tax'] or 0:g}% / jual {sec['sell_tax'] or 0:g}%"
        if tax is None:
            add("security", "Pajak", "warn", "tidak diketahui")
        elif tax > 10:
            fail("security", "Pajak", tax_txt)
        elif tax <= 1:
            add("security", "Pajak", "pass", tax_txt, 8)
        elif tax <= 3:
            add("security", "Pajak", "warn", tax_txt, 3)
        elif tax <= cfg.meme_max_tax:
            add("security", "Pajak", "warn", tax_txt)
        else:
            add("security", "Pajak", "warn", tax_txt + " (tinggi)")

        if chain == "solana":
            if sec["mintable"]:
                fail("security", "Mint authority", "masih aktif, supply bisa dicetak")
            else:
                add("security", "Mint authority", "pass", "sudah dicabut", 7)
            if sec["freezable"]:
                fail("security", "Freeze authority", "aktif, wallet holder bisa dibekukan")
            else:
                add("security", "Freeze authority", "pass", "tidak ada", 5)
        else:
            if sec["renounced"]:
                add("security", "Ownership", "pass", "owner renounced", 7)
            elif sec["renounced"] is False:
                add("security", "Ownership", "warn", "owner masih aktif", 2)
            if not sec["mintable"] and not sec["freezable"]:
                add("security", "Mint / pause", "pass", "tidak bisa mint & pause", 5)
        for label in dict.fromkeys(sec["danger"]):
            if not (chain == "solana" and "authority" in label):  # already reported above
                fail("security", "Fungsi berbahaya", label)
        if sec["warnings"]:
            for w in dict.fromkeys(sec["warnings"]):
                add("security", "Peringatan", "warn", w)
            pts += max(0, 5 - 2 * len(sec["warnings"]))
        elif not sec["danger"]:
            add("security", "Fungsi kontrak", "pass", "tidak ada fungsi berisiko", 5)

        lp = sec["lp_locked_pct"]
        if sec["bonding_curve"]:
            add("security", "Likuiditas", "warn", "masih di bonding curve (belum migrasi DEX)", 6)
        elif lp is None:
            add("security", "LP lock", "warn", "status lock/burn LP tidak diketahui")
        elif lp >= 95:
            add("security", "LP lock", "pass", f"{lp:.0f}% LP di-burn/lock", 10)
        elif lp >= cfg.meme_min_lp_locked:
            add("security", "LP lock", "pass", f"{lp:.0f}% LP di-burn/lock", 7)
        elif lp < 50:
            fail("security", "LP lock", f"hanya {lp:.0f}% LP terkunci (dev bisa tarik likuiditas)")
        else:
            add("security", "LP lock", "warn", f"{lp:.0f}% LP terkunci", 2)

    # ---- holder distribution (25) ----
    top10, top1, dev = sec["top10_pct"], sec["top1_pct"], sec["dev_pct"]
    if top10 is None:
        add("distribution", "Top 10 holder", "warn", "data holder tidak tersedia")
    elif top10 > 45:
        fail("distribution", "Top 10 holder", f"{top10:.1f}% supply (terlalu terpusat)")
    elif top10 <= 15:
        add("distribution", "Top 10 holder", "pass", f"{top10:.1f}% supply", 10)
    elif top10 <= 25:
        add("distribution", "Top 10 holder", "pass", f"{top10:.1f}% supply", 7)
    elif top10 <= cfg.meme_max_top10:
        add("distribution", "Top 10 holder", "warn", f"{top10:.1f}% supply", 3)
    else:
        add("distribution", "Top 10 holder", "warn", f"{top10:.1f}% supply (whale berat)")
    if top1 is not None:
        if top1 > 15:
            fail("distribution", "Holder terbesar", f"1 wallet pegang {top1:.1f}%")
        elif top1 <= 3:
            add("distribution", "Holder terbesar", "pass", f"{top1:.1f}%", 5)
        elif top1 <= 5:
            add("distribution", "Holder terbesar", "pass", f"{top1:.1f}%", 3)
        else:
            add("distribution", "Holder terbesar", "warn", f"{top1:.1f}%")
    if dev is None:
        add("distribution", "Wallet dev", "warn", "kepemilikan dev tidak diketahui")
    elif dev > 10:
        fail("distribution", "Wallet dev", f"dev pegang {dev:.1f}% (risiko dump)")
    elif dev <= 1:
        add("distribution", "Wallet dev", "pass", f"dev pegang {dev:.2f}%", 6)
    elif dev <= 2:
        add("distribution", "Wallet dev", "warn", f"dev pegang {dev:.1f}%", 3)
    elif dev <= cfg.meme_max_dev:
        add("distribution", "Wallet dev", "warn", f"dev pegang {dev:.1f}%")
    else:
        add("distribution", "Wallet dev", "warn", f"dev pegang {dev:.1f}% (tinggi)")
    holders = sec["holders"]
    if holders is not None:
        if holders >= 2000:
            add("distribution", "Jumlah holder", "pass", f"{holders:,}", 4)
        elif holders >= 500:
            add("distribution", "Jumlah holder", "pass", f"{holders:,}", 3)
        elif holders >= 150:
            add("distribution", "Jumlah holder", "warn", f"{holders:,}", 1)
        else:
            add("distribution", "Jumlah holder", "warn", f"hanya {holders:,}")
    if sec["insiders"]:
        add("distribution", "Insider / bundle", "warn", f"{sec['insiders']} wallet insider terdeteksi")
        pts -= 5

    # ---- liquidity (15) ----
    liq, mcap = m["liquidity"], m["mcap"]
    ratio = liq / mcap * 100 if mcap else 0
    if liq >= 100_000:
        add("liquidity", "Likuiditas", "pass", f"${liq:,.0f}", 6)
    elif liq >= 40_000:
        add("liquidity", "Likuiditas", "pass", f"${liq:,.0f}", 4)
    else:
        add("liquidity", "Likuiditas", "warn", f"${liq:,.0f} (tipis)", 2)
    if ratio >= 10:
        add("liquidity", "Likuiditas / MCap", "pass", f"{ratio:.1f}%", 6)
    elif ratio >= 5:
        add("liquidity", "Likuiditas / MCap", "pass", f"{ratio:.1f}%", 4)
    else:
        add("liquidity", "Likuiditas / MCap", "warn", f"{ratio:.1f}% (mudah di-dump)", 1 if ratio >= 2 else 0)
    if mcap <= 5_000_000:
        add("liquidity", "Market cap", "pass", f"${mcap:,.0f} (ruang naik besar)", 3)
    else:
        add("liquidity", "Market cap", "pass", f"${mcap:,.0f}", 1)

    # ---- momentum (20) ----
    tx1, tx24 = m["txns"]["h1"], m["txns"]["h24"]
    buy_ratio = tx1["buys"] / max(1, tx1["sells"])
    if tx1["buys"] + tx1["sells"] < 10:
        add("momentum", "Buy/sell 1j", "warn", "aktivitas 1 jam sepi")
    elif 1.1 <= buy_ratio <= 4:
        add("momentum", "Buy/sell 1j", "pass", f"{tx1['buys']} beli / {tx1['sells']} jual", 5)
    elif buy_ratio >= 1:
        add("momentum", "Buy/sell 1j", "pass", f"{tx1['buys']} beli / {tx1['sells']} jual", 2)
    else:
        add("momentum", "Buy/sell 1j", "warn", f"jual dominan ({tx1['buys']}/{tx1['sells']})")
    vol = m["volume"]
    accel = vol["h1"] / (vol["h24"] / 24) if vol["h24"] else 0
    if accel >= 1.5:
        add("momentum", "Akselerasi volume", "pass", f"volume 1j = {accel:.1f}x rata-rata", 4)
    elif accel >= 1:
        add("momentum", "Akselerasi volume", "pass", f"{accel:.1f}x rata-rata", 2)
    else:
        add("momentum", "Akselerasi volume", "warn", f"{accel:.1f}x rata-rata (melambat)")
    turnover = vol["h24"] / liq if liq else 0
    if turnover > 30:
        add("momentum", "Turnover", "warn", f"volume {turnover:.0f}x likuiditas (indikasi wash trading)")
    elif turnover >= 0.5:
        add("momentum", "Turnover", "pass", f"volume {turnover:.1f}x likuiditas", 4)
    else:
        add("momentum", "Turnover", "warn", f"volume {turnover:.1f}x likuiditas (sepi)")
    ch = m["change"]
    c6, c24 = ch.get("h6"), ch.get("h24")
    if c24 is not None and c24 > 1000:
        add("momentum", "Tren harga", "warn", f"+{c24:.0f}% 24j, sudah terbang (risiko beli di pucuk)")
    elif (c24 or 0) > 0 and (c6 or 0) > 0:
        add("momentum", "Tren harga", "pass", f"6j {c6:+.1f}% · 24j {c24:+.1f}%", 4)
    elif (c6 or 0) > 0:
        add("momentum", "Tren harga", "pass", f"6j {c6:+.1f}% (rebound)", 2)
    else:
        add("momentum", "Tren harga", "warn", f"6j {(c6 or 0):+.1f}% · 24j {(c24 or 0):+.1f}%")
    buyers = m.get("buyers_h24")
    if buyers is not None:
        if buyers >= 300:
            add("momentum", "Pembeli unik 24j", "pass", f"{buyers:,} wallet", 3)
        else:
            add("momentum", "Pembeli unik 24j", "warn", f"{buyers:,} wallet")

    # ---- community (5) ----
    s = m["socials"]
    present = [k for k in ("twitter", "telegram", "website") if s.get(k)]
    community = (2 if s.get("twitter") else 0) + (1 if s.get("telegram") else 0) + (1 if s.get("website") else 0)
    community += 1 if ("profile" in m.get("sources", ()) or m.get("boosts")) else 0
    if present:
        add("community", "Sosial", "pass" if len(present) >= 2 else "warn", ", ".join(present), community)
    else:
        add("community", "Sosial", "warn", "tidak ada X / Telegram / website")

    # ---- age ----
    age = m["age_h"]
    if age is not None and age > 30 * 24:
        add("momentum", "Umur", "warn", f"{age / 24:.0f} hari (potensi multi-x lebih kecil)")
        pts -= 5

    score = int(round(max(0, min(100, pts))))
    grade = "F" if reject else "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 55 else "D"

    if not reject:
        if mcap < 1_000_000:
            tags.append("EARLY")
        if age is not None and age <= 72:
            tags.append("FRESH")
        if accel >= 2 and buy_ratio >= 1.3 and (ch.get("h1") or 0) > 0:
            tags.append("MOMENTUM")
        if (sec["renounced"] and not sec["warnings"] and not sec["danger"] and (tax or 0) <= 1
                and (sec["bonding_curve"] or (sec["lp_locked_pct"] or 0) >= 95)):
            tags.append("SAFE CONTRACT")
        if top10 is not None and top10 > 25:
            tags.append("WHALE HEAVY")
    return score, grade, checks, reject, tags


def evaluate(m, sec, cfg):
    score, grade, checks, reject, tags = score_token(m, sec, cfg)
    chain = m["chain"]
    tx1 = m["txns"]["h1"]
    return {
        "chain": chain, "chain_label": CHAINS[chain]["label"], "address": m["address"],
        "symbol": m["symbol"], "name": m["name"], "icon": m["icon"], "dex": m["dex"],
        "url": m["url"], "explorer": CHAINS[chain]["explorer"] + m["address"],
        "price": m["price"], "mcap": m["mcap"], "liquidity": m["liquidity"],
        "liq_ratio": round(m["liquidity"] / m["mcap"] * 100, 2) if m["mcap"] else None,
        "volume24": m["volume"]["h24"], "volume1h": m["volume"]["h1"],
        "txns_h1": tx1, "txns_h24": m["txns"]["h24"], "change": m["change"], "age_h": m["age_h"],
        "socials": m["socials"], "sources": sorted(m.get("sources", ())), "buyers_h24": m.get("buyers_h24"),
        "security": None if sec is None else {k: sec[k] for k in (
            "source", "top10_pct", "top1_pct", "dev_pct", "lp_locked_pct", "buy_tax", "sell_tax",
            "holders", "renounced", "bonding_curve", "insiders", "risk_score")},
        "score": score, "grade": grade, "verdict": "reject" if reject else "pass",
        "reasons": reject, "checks": checks, "tags": tags, "checked_at": now_iso(),
    }


# ------------------------------------------------------------------ orchestration

_scan_lock = threading.Lock()
_file_lock = threading.Lock()


def _save(cfg, **updates):
    """Read-modify-write so a manual check and a running scan never overwrite each other."""
    with _file_lock:
        data = read_json(cfg.meme_file, {})
        data.update(updates)
        write_json(cfg.meme_file, data)
        return data


def scan_running():
    return _scan_lock.locked()


def start_scan_async(cfg, chains=None, log=None):
    if _scan_lock.locked():
        return False
    threading.Thread(target=run_scan, args=(cfg, chains, log), daemon=True).start()
    return True


def run_scan(cfg, chains=None, log=None):
    if not _scan_lock.acquire(blocking=False):
        return None
    try:
        return _run_scan(cfg, [c for c in (chains or cfg.meme_chains) if c in CHAINS], log)
    except Exception as exc:
        _save(cfg, status="idle", errors=[f"scan gagal: {exc}"])
        if log:
            log.warning(f"meme scan failed: {exc}")
        return None
    finally:
        _scan_lock.release()


def _run_scan(cfg, chains, log):
    errors = []
    _save(cfg, status="scanning", started_at=now_iso(), chains=chains, errors=[],
          progress={"stage": "discover", "done": 0, "total": 0})
    found = discover(cfg, chains, errors)

    _save(cfg, progress={"stage": "market", "done": 0, "total": len(found)})
    markets = {}
    for chain in chains:
        addrs = [a for (c, a) in found if c == chain]
        if not addrs:
            continue
        try:
            markets[chain] = fetch_markets(cfg, chain, addrs)
        except Exception as exc:
            errors.append(f"dexscreener {chain}: {exc}")

    candidates, skipped = [], {}
    for (chain, addr), meta in found.items():
        m = (markets.get(chain) or {}).get(addr)
        if not m:
            continue
        m.update(chain=chain, address=addr, sources=meta["sources"],
                 buyers_h24=meta.get("buyers_h24"), sellers_h24=meta.get("sellers_h24"))
        reason = prefilter(m, cfg)
        if reason:
            skipped[reason.split(" (")[0]] = skipped.get(reason.split(" (")[0], 0) + 1
            continue
        candidates.append(m)
    # most active first: the security budget goes to tokens people are actually trading
    candidates.sort(key=lambda m: m["volume"]["h24"], reverse=True)
    candidates = candidates[:cfg.meme_max_checks]

    results = []
    for i, m in enumerate(candidates):
        try:
            sec = fetch_security(cfg, m["chain"], m["address"], m)
        except Exception as exc:
            errors.append(f"security {m['symbol']}: {exc}")
            sec = None
        results.append(evaluate(m, sec, cfg))
        if i % 5 == 4:
            _save(cfg, progress={"stage": "security", "done": i + 1, "total": len(candidates)})

    results.sort(key=lambda r: (r["verdict"] == "pass", r["score"]), reverse=True)
    counts = {"discovered": len(found), "with_market": sum(len(v) for v in markets.values()),
              "checked": len(results), "passed": sum(r["verdict"] == "pass" for r in results),
              "grade_a": sum(r["grade"] == "A" for r in results), "skipped": skipped}
    data = _save(cfg, status="idle", updated_at=now_iso(), results=results, counts=counts,
                 errors=errors[:20], progress={"stage": "done", "done": len(results), "total": len(results)})
    if log:
        log.info(f"meme scan: {counts['discovered']} found, {counts['checked']} checked, {counts['passed']} passed")
    return data


def screen_token(cfg, chain, address):
    """Full check of one token the user pasted (no prefilter, but its findings are shown)."""
    if not valid_address(chain, address):
        raise ValueError("alamat token tidak valid untuk chain ini")
    address = norm_address(chain, address)
    m = fetch_markets(cfg, chain, [address]).get(address)
    if not m:
        raise ValueError("token tidak ditemukan di DexScreener")
    m.update(chain=chain, address=address, sources={"manual"})
    sec = fetch_security(cfg, chain, address, m)
    result = evaluate(m, sec, cfg)
    note = prefilter(m, cfg)
    if note:
        result["checks"].insert(0, {"group": "market", "label": "Filter pasar", "status": "warn", "detail": note})
    with _file_lock:
        data = read_json(cfg.meme_file, {})
        manual = [r for r in data.get("manual") or [] if r["address"] != address]
        data["manual"] = ([result] + manual)[:20]
        write_json(cfg.meme_file, data)
    return result
