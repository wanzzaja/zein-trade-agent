"""Meme screener tests. Fixtures mirror the public API response shapes, no network needed.

Run: python -m unittest discover tests
"""
import copy
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from dataclasses import replace

from agent import meme
from agent.config import CONFIG
from agent.dashboard import serve

NOW_MS = time.time() * 1000
SOL_GOOD = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
SOL_MINT = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"
BSC_GOOD = "0x1111111111111111111111111111111111111111"
BSC_HONEY = "0x2222222222222222222222222222222222222222"
BSC_PAIR = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def pair(chain, address, symbol, liq=80_000, mcap=600_000, vol24=400_000, vol1=40_000,
         buys1=180, sells1=110, buys24=2600, sells24=2100, age_h=40, h6=12.0, h24=35.0, pair_addr=None):
    return {
        "chainId": chain, "dexId": "raydium" if chain == "solana" else "pancakeswap",
        "url": f"https://dexscreener.com/{chain}/{pair_addr or 'pool'}",
        "pairAddress": pair_addr or f"pool-{symbol}",
        "baseToken": {"address": address, "name": symbol.title(), "symbol": symbol},
        "quoteToken": {"address": "x", "name": "Wrapped", "symbol": "WSOL" if chain == "solana" else "WBNB"},
        "priceUsd": "0.0006",
        "txns": {"m5": {"buys": 10, "sells": 8}, "h1": {"buys": buys1, "sells": sells1},
                 "h6": {"buys": 800, "sells": 600}, "h24": {"buys": buys24, "sells": sells24}},
        "volume": {"m5": 3000, "h1": vol1, "h6": 150_000, "h24": vol24},
        "priceChange": {"m5": 0.5, "h1": 4.0, "h6": h6, "h24": h24},
        "liquidity": {"usd": liq, "base": 1, "quote": 1},
        "fdv": mcap, "marketCap": mcap,
        "pairCreatedAt": NOW_MS - age_h * 3.6e6,
        "info": {"imageUrl": "https://cdn.dexscreener.com/x.png",
                 "websites": [{"label": "Website", "url": "https://example.org"}],
                 "socials": [{"type": "twitter", "url": "https://x.com/example"},
                             {"type": "telegram", "url": "https://t.me/example"}]},
    }


GOPLUS_CLEAN = {
    "buy_tax": "0", "sell_tax": "0", "is_honeypot": "0", "is_open_source": "1", "is_mintable": "0",
    "owner_address": "0x0000000000000000000000000000000000000000", "creator_percent": "0.004",
    "owner_percent": "0", "holder_count": "3120", "transfer_pausable": "0", "slippage_modifiable": "0",
    "hidden_owner": "0", "can_take_back_ownership": "0", "owner_change_balance": "0", "selfdestruct": "0",
    "cannot_sell_all": "0", "is_blacklisted": "0", "is_proxy": "0",
    "dex": [{"name": "PancakeV2", "pair": BSC_PAIR}],
    "holders": [
        {"address": BSC_PAIR, "percent": "0.12", "is_locked": 0, "is_contract": 1},
        {"address": "0x000000000000000000000000000000000000dead", "percent": "0.30", "is_locked": 0},
        {"address": "0x3333333333333333333333333333333333333333", "percent": "0.025", "is_locked": 0},
        {"address": "0x4444444444444444444444444444444444444444", "percent": "0.02", "is_locked": 0},
        {"address": "0x5555555555555555555555555555555555555555", "percent": "0.015", "is_locked": 0},
        {"address": "0x6666666666666666666666666666666666666666", "percent": "0.05", "is_locked": 1},
    ],
    "lp_holders": [
        {"address": "0x000000000000000000000000000000000000dead", "percent": "0.97", "is_locked": 0},
        {"address": "0x7777777777777777777777777777777777777777", "percent": "0.03", "is_locked": 0},
    ],
}
GOPLUS_HONEY = dict(GOPLUS_CLEAN, is_honeypot="1", sell_tax="0.99",
                    owner_address="0x8888888888888888888888888888888888888888", is_mintable="1")

RUG_CLEAN = {
    "mint": SOL_GOOD, "creator": "Dev1111111111111111111111111111111111111111",
    "creatorBalance": 2_000_000, "mintAuthority": None, "freezeAuthority": None,
    "token": {"supply": 1_000_000_000, "decimals": 6, "mintAuthority": None, "freezeAuthority": None},
    "tokenMeta": {"mutable": False},
    "topHolders": [
        {"address": "PoolVaultA", "owner": "PoolAuthority", "amount": 150_000_000, "pct": 15.0},
        {"address": "acc1", "owner": "w1", "amount": 28_000_000, "pct": 2.8},
        {"address": "acc2", "owner": "w2", "amount": 20_000_000, "pct": 2.0},
        {"address": "acc3", "owner": "w3", "amount": 15_000_000, "pct": 1.5, "insider": False},
    ],
    "markets": [{"pubkey": "PoolAuthority", "marketType": "raydium", "liquidityA": "PoolVaultA",
                 "liquidityB": "PoolVaultB", "lp": {"lpLockedPct": 100, "baseUSD": 40000, "quoteUSD": 40000}}],
    "totalHolders": 4200, "graphInsidersDetected": 0, "rugged": False, "score_normalised": 3,
    "risks": [{"name": "Low amount of LP Providers", "level": "warn"}],
}
RUG_MINT = dict(copy.deepcopy(RUG_CLEAN), mint=SOL_MINT, mintAuthority="Auth111", freezeAuthority="Frz111")


class FakeHttp:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None):
        self.calls.append(url)
        if url.endswith("/token-profiles/latest/v1"):
            return [{"chainId": "solana", "tokenAddress": SOL_GOOD}, {"chainId": "tron", "tokenAddress": "T123"}]
        if "/token-boosts/" in url:
            return [{"chainId": "bsc", "tokenAddress": BSC_GOOD, "amount": 100}]
        if "/networks/" in url:
            net = url.split("/networks/")[1].split("/")[0]
            ids = {"solana": [SOL_GOOD, SOL_MINT], "bsc": [BSC_HONEY]}.get(net, [])
            return {"data": [{"attributes": {"transactions": {"h24": {"buyers": 900, "sellers": 500}}},
                              "relationships": {"base_token": {"data": {"id": f"{net}_{a}"}}}} for a in ids]}
        if "/tokens/v1/solana/" in url:
            return [pair("solana", SOL_GOOD, "GOOD"), pair("solana", SOL_MINT, "MINTY")]
        if "/tokens/v1/bsc/" in url:
            return [pair("bsc", BSC_GOOD, "SAFU", pair_addr=BSC_PAIR), pair("bsc", BSC_HONEY, "TRAP")]
        if "/tokens/v1/" in url:
            return []
        if "rugcheck" in url:
            return RUG_CLEAN if SOL_GOOD in url else RUG_MINT
        if "/token_security/56" in url:
            addr = params["contract_addresses"]
            return {"code": 1, "result": {addr.lower(): GOPLUS_CLEAN if addr == BSC_GOOD else GOPLUS_HONEY}}
        if "honeypot.is" in url:
            return {"simulationSuccess": True, "honeypotResult": {"isHoneypot": params["address"] == BSC_HONEY},
                    "simulationResult": {"buyTax": 0, "sellTax": 0}}
        raise AssertionError("unexpected url " + url)


def cfg_for(tmp):
    return replace(CONFIG, meme_file=os.path.join(tmp, "meme.json"), meme_chains=["solana", "bsc"])


def market(chain, address, **kw):
    m = meme.market_from_pairs([pair(chain, address, "TKN", **kw)], NOW_MS)
    m.update(chain=chain, address=address, sources={"trending"})
    return m


class MarketTests(unittest.TestCase):
    def test_aggregates_pairs_and_socials(self):
        a = pair("solana", SOL_GOOD, "GOOD", liq=60_000, vol24=100_000, age_h=10)
        b = pair("solana", SOL_GOOD, "GOOD", liq=20_000, vol24=50_000, age_h=30)
        m = meme.market_from_pairs([a, b], NOW_MS)
        self.assertEqual(m["liquidity"], 80_000)
        self.assertEqual(m["volume"]["h24"], 150_000)
        self.assertAlmostEqual(m["age_h"], 30, places=3)  # oldest pair
        self.assertEqual(m["socials"]["twitter"], "https://x.com/example")
        self.assertEqual(m["txns"]["h1"], {"buys": 360, "sells": 220})

    def test_prefilter(self):
        self.assertIsNone(meme.prefilter(market("solana", SOL_GOOD), CONFIG))
        self.assertIn("likuiditas", meme.prefilter(market("solana", SOL_GOOD, liq=5_000), CONFIG))
        self.assertIn("umur", meme.prefilter(market("solana", SOL_GOOD, age_h=0.5), CONFIG))
        self.assertIn("market cap >", meme.prefilter(market("solana", SOL_GOOD, mcap=90_000_000), CONFIG))

    def test_address_validation(self):
        self.assertTrue(meme.valid_address("solana", SOL_GOOD))
        self.assertTrue(meme.valid_address("base", BSC_GOOD))
        self.assertFalse(meme.valid_address("base", SOL_GOOD))
        self.assertFalse(meme.valid_address("solana", "0x123"))
        self.assertFalse(meme.valid_address("tron", BSC_GOOD))


class SecurityTests(unittest.TestCase):
    def test_goplus_clean(self):
        sec = meme.norm_goplus(GOPLUS_CLEAN, [BSC_PAIR])
        self.assertTrue(sec["renounced"])
        self.assertEqual(sec["lp_locked_pct"], 97.0)
        # pair, burn and locked holders are excluded: 2.5 + 2 + 1.5
        self.assertAlmostEqual(sec["top10_pct"], 6.0)
        self.assertAlmostEqual(sec["top1_pct"], 2.5)
        self.assertAlmostEqual(sec["dev_pct"], 0.4)
        self.assertEqual(sec["buy_tax"], 0)
        self.assertEqual(sec["danger"], [])

    def test_goplus_owner_controlled_is_fatal(self):
        sec = meme.norm_goplus(GOPLUS_HONEY, [BSC_PAIR])
        self.assertFalse(sec["renounced"])
        self.assertTrue(sec["honeypot"])
        self.assertIn("supply bisa dicetak (mint)", sec["danger"])
        self.assertAlmostEqual(sec["sell_tax"], 99)

    def test_honeypot_is_overrides(self):
        sec = meme.norm_goplus(dict(GOPLUS_CLEAN, is_honeypot=""), [])
        self.assertIsNone(sec["honeypot"])
        meme.merge_honeypot_is(sec, {"honeypotResult": {"isHoneypot": True}, "simulationResult": {"sellTax": 100}})
        self.assertTrue(sec["honeypot"])
        self.assertEqual(sec["sell_tax"], 100)

    def test_rugcheck_clean(self):
        sec = meme.norm_rugcheck(RUG_CLEAN)
        self.assertFalse(sec["mintable"])
        self.assertFalse(sec["freezable"])
        self.assertEqual(sec["lp_locked_pct"], 100)
        self.assertAlmostEqual(sec["top10_pct"], 6.3)  # pool vault excluded
        self.assertAlmostEqual(sec["dev_pct"], 0.2)
        self.assertEqual(sec["holders"], 4200)

    def test_rugcheck_pump_bonding_curve(self):
        r = copy.deepcopy(RUG_CLEAN)
        r["markets"] = [{"pubkey": "Curve", "marketType": "pump_fun", "liquidityA": "CurveVault"}]
        sec = meme.norm_rugcheck(r)
        self.assertTrue(sec["bonding_curve"])
        self.assertIsNone(sec["lp_locked_pct"])


class ScoreTests(unittest.TestCase):
    def test_clean_solana_token_grades_well(self):
        r = meme.evaluate(market("solana", SOL_GOOD), meme.norm_rugcheck(RUG_CLEAN), CONFIG)
        self.assertEqual(r["verdict"], "pass", r["reasons"])
        self.assertIn(r["grade"], ("A", "B"))
        self.assertIn("EARLY", r["tags"])
        self.assertIn("FRESH", r["tags"])

    def test_mint_authority_rejects(self):
        r = meme.evaluate(market("solana", SOL_MINT), meme.norm_rugcheck(RUG_MINT), CONFIG)
        self.assertEqual(r["verdict"], "reject")
        self.assertEqual(r["grade"], "F")
        self.assertTrue(any("Mint authority" in x for x in r["reasons"]))
        self.assertTrue(any("Freeze authority" in x for x in r["reasons"]))

    def test_unlocked_lp_and_whales_reject(self):
        sec = meme.norm_rugcheck(RUG_CLEAN)
        sec.update(lp_locked_pct=20, top10_pct=60, top1_pct=22, dev_pct=14)
        r = meme.evaluate(market("solana", SOL_GOOD), sec, CONFIG)
        labels = " ".join(r["reasons"])
        for word in ("LP lock", "Top 10", "Holder terbesar", "Wallet dev"):
            self.assertIn(word, labels)

    def test_missing_security_is_not_a_pass(self):
        r = meme.evaluate(market("bsc", BSC_GOOD), None, CONFIG)
        self.assertEqual(r["verdict"], "reject")

    def test_wash_trading_and_late_pump_warn(self):
        m = market("solana", SOL_GOOD, vol24=5_000_000, liq=80_000, h24=2500)
        r = meme.evaluate(m, meme.norm_rugcheck(RUG_CLEAN), CONFIG)
        details = " ".join(c["detail"] for c in r["checks"] if c["status"] == "warn")
        self.assertIn("wash trading", details)
        self.assertIn("pucuk", details)

    def test_score_bounds(self):
        best = meme.evaluate(market("solana", SOL_GOOD), meme.norm_rugcheck(RUG_CLEAN), CONFIG)
        self.assertLessEqual(best["score"], 100)
        self.assertGreaterEqual(best["score"], 0)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = cfg_for(self.tmp)
        self.real_http = meme.http
        meme.http = FakeHttp()

    def tearDown(self):
        meme.http = self.real_http

    def test_full_scan(self):
        data = meme.run_scan(self.cfg)
        by_symbol = {r["symbol"]: r for r in data["results"]}
        self.assertEqual(set(by_symbol), {"GOOD", "MINTY", "SAFU", "TRAP"})
        self.assertEqual(by_symbol["GOOD"]["verdict"], "pass")
        self.assertEqual(by_symbol["SAFU"]["verdict"], "pass")
        self.assertEqual(by_symbol["MINTY"]["verdict"], "reject")
        self.assertEqual(by_symbol["TRAP"]["verdict"], "reject")
        self.assertEqual(by_symbol["GOOD"]["buyers_h24"], 900)  # from GeckoTerminal
        self.assertEqual(data["counts"]["passed"], 2)
        self.assertEqual(data["status"], "idle")
        self.assertEqual(data["results"][0]["verdict"], "pass")  # passes sorted first
        # unsupported chain from DexScreener profiles is ignored
        self.assertFalse(any("tron" in c for c in meme.http.calls))
        with open(self.cfg.meme_file) as f:
            saved = json.load(f)
        self.assertEqual(len(saved["results"]), 4)

    def test_manual_check_is_kept_across_scans(self):
        r = meme.screen_token(self.cfg, "bsc", BSC_GOOD.upper().replace("0X", "0x"))
        self.assertEqual(r["symbol"], "SAFU")
        meme.run_scan(self.cfg)
        with open(self.cfg.meme_file) as f:
            saved = json.load(f)
        self.assertEqual(saved["manual"][0]["address"], BSC_GOOD)
        with self.assertRaises(ValueError):
            meme.screen_token(self.cfg, "solana", "not-an-address")


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = replace(cfg_for(self.tmp), ui_port=0, journal_file=os.path.join(self.tmp, "j.json"),
                           snapshot_file=os.path.join(self.tmp, "s.json"),
                           backtest_file=os.path.join(self.tmp, "b.json"))
        self.server = serve(self.cfg)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.real_http = meme.http
        meme.http = FakeHttp()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        meme.http = self.real_http

    def post(self, path, body, ctype="application/json"):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": ctype}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_check_endpoint(self):
        status, body = self.post("/api/meme/check", {"chain": "solana", "address": SOL_GOOD})
        self.assertEqual(status, 200)
        self.assertEqual(body["symbol"], "GOOD")
        with urllib.request.urlopen(self.base + "/api/meme") as r:
            self.assertEqual(json.load(r)["manual"][0]["symbol"], "GOOD")

    def test_rejects_non_json_posts(self):
        status, body = self.post("/api/meme/scan", {}, ctype="text/plain")
        self.assertEqual(status, 400)

    def test_rejects_bad_address(self):
        status, body = self.post("/api/meme/check", {"chain": "bsc", "address": "<script>"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
