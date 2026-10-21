# Zorra Terminal by ZIUZ

AI crypto trading agent untuk Recall, dengan dashboard web **ZORRA TERMINAL**.

```
zein-trade-agent/
  main.py              # live loop, --scan, --backtest, --ui
  agent/
      config.py        # semua parameter (bisa di-override lewat env)
      strategy.py      # signal scanner + filter RR
      trader.py        # Recall API, manajemen posisi, risk manager
      backtest.py      # backtest bar-per-bar dengan logika yang sama dengan live
      journal.py       # jurnal sinyal (format kolom sama dengan export ZIUZ)
      dashboard.py     # server web Zorra Terminal (tanpa dependency tambahan)
      meme.py          # meme coin screener (SOL, BSC, Base, ETH)
      utils.py         # data market + indikator
  ui/index.html        # tampilan Zorra Terminal
  tests/               # python -m unittest discover tests
  data/                # log, state, jurnal, snapshot, hasil backtest
```

## Zorra Terminal (dashboard)

Saat bot jalan (`python main.py`), dashboard otomatis aktif di **http://127.0.0.1:8787**.
Isinya: equity curve, total R, win rate, profit factor, distribusi R, market scanner (trend, ADX, RSI,
ATR%, relative strength vs BTC, dan alasan kenapa sebuah coin belum dapat sinyal), posisi terbuka dengan
rail SL → entry → TP1 → TP, jurnal sinyal dengan filter dan export CSV, serta tab **BACKTEST**.

- `python main.py --ui` membuka dashboard saja (membaca file di `data/`).
- Untuk akses dari HP/VPS, set `UI_HOST=0.0.0.0`. Dashboard tidak punya login, jadi jangan buka ke
  internet publik tanpa proteksi (firewall, VPN, atau reverse proxy dengan password).

## Meme Screener (Solana, BNB Chain, Base, Ethereum)

Menu **MEME SCREENER** di dashboard (atau `python main.py --meme`) mencari meme coin berkualitas:

1. **Penemuan**: pool trending & baru (GeckoTerminal) + token yang dipromosikan/di-boost (DexScreener).
2. **Filter pasar**: likuiditas ≥ $15k, market cap $50k–$30M, umur 2 jam–90 hari, volume 24j ≥ $20k,
   ≥ 150 transaksi. Token di bawah 2 jam dilewati karena itu zona sniper dan rug.
3. **Cek keamanan**: GoPlus + simulasi beli/jual honeypot.is untuk BSC/Base/ETH, RugCheck untuk Solana.
4. **Skor 0–100**: keamanan kontrak 35, distribusi holder 25, likuiditas 15, momentum 20, komunitas 5.
   Grade A ≥ 85, B ≥ 70, C ≥ 55.

**Langsung ditolak** (red flag): honeypot, pajak > 10%, mint/freeze authority aktif, owner masih bisa
mint/pause/ubah pajak, kontrak tidak terverifikasi, fungsi berbahaya (hidden owner, ubah saldo,
selfdestruct), creator pernah membuat honeypot, LP terkunci < 50%, top 10 holder > 45%, satu wallet > 15%,
dev > 10%, token sudah di-rug.

Top holder dihitung **tanpa** pool LP, alamat burn dan token yang dikunci, supaya angkanya mencerminkan
whale sungguhan. Momentum memakai rasio beli/jual 1 jam, akselerasi volume (1 jam vs rata-rata 24 jam),
turnover (volume > 30x likuiditas = indikasi wash trading), tren harga dan jumlah pembeli unik.

Ada juga **cek token manual**: paste contract address untuk dicek lengkap. Semua ambang bisa diubah lewat
env `MEME_*` (lihat `agent/config.py`). `MEME_AUTO_MINUTES=30` membuat bot live ikut scan otomatis.

> Skor mengukur kualitas dan risiko, **bukan ramalan harga**. Kebanyakan meme coin tetap berakhir di nol.
> Pakai uang yang siap hilang, ambil profit bertahap, dan cek ulang sendiri sebelum membeli.
> API yang dipakai versi gratis dengan rate limit, jadi satu scan butuh sekitar 2–4 menit.

## Cara kerja signal scanner

Bot hanya mengambil posisi **long** (Recall = spot) dan sebuah sinyal harus lolos semua filter:

1. **Trend timeframe besar (1d)** naik: close > EMA50, EMA50 menanjak, EMA20 > EMA50.
2. **Market trending** (ADX ≥ 20, +DI > -DI) dan EMA20 > EMA50 > EMA200 di timeframe entry (4h). Timeframe 15m/1h tidak dipakai: di data hasil sinyal sebelumnya keduanya rugi.
3. **Setup**: *pullback* ke EMA20 + RSI reset + candle bullish menembus high sebelumnya, atau
   *breakout* Donchian 20 setelah squeeze Bollinger dengan volume > 1.5x rata-rata.
4. **Skor konfluensi** ≥ `MIN_SCORE` (HTF > EMA200, ADX kuat, volume, MACD, RSI sehat, close kuat).
5. **Regime BTC**: altcoin hanya dibeli saat trend 1d BTC naik (altcoin cenderung ikut jatuh saat BTC
   turun), dan coin yang lebih kuat dari BTC (relative strength 30 bar) dapat skor tambahan.
6. **Volatilitas normal**: sinyal dilewati saat ATR% > 2x median-nya (candle berita / spike).
7. **RR riil** ≥ `MIN_RR` (default 2.0, sudah dipotong fee/slippage): SL di bawah swing low (dibatasi
   0.8–3 ATR dan minimal 0.3% dari harga, supaya SL tidak lebih kecil dari spread), target 3R tapi dipotong ke resistance besar terdekat. Kalau ruang ke resistance tidak cukup,
   sinyal **dibuang**, bukan dipaksa.

Manajemen posisi: SL ke break-even di +1R, jual 50% di TP1 (1.5R), sisanya trailing stop 2.5 ATR
sampai target, dengan batas waktu 60 bar (10 hari). Risiko 1% equity per trade, maks 3 posisi,
maks 2 entry baru per scan (sinyal yang muncul bersamaan saling berkorelasi), cooldown setelah loss,
dan bot berhenti entry kalau rugi harian ≥ 3%.

Saat live: SL dicek terhadap harga terkini (tidak menunggu candle 4h tutup), candle tempat entry tidak
pernah dipakai untuk cek SL/TP, dan entry dibatalkan kalau harga sudah lari > 0.25R dari harga sinyal.

## Menjalankan

```bash
pip install -r requirements.txt
cp .env.example .env          # isi RECALL_API_KEY, set DRY_RUN=false kalau sudah yakin
python main.py --backtest     # uji di ~6000 candle 4h terakhir
python main.py --scan         # scan sekali
python main.py                # loop live + dashboard di http://127.0.0.1:8787
python main.py --ui           # dashboard saja
python main.py --meme         # meme screener di terminal (--chains solana,bsc,base,ethereum)
python -m unittest discover tests
```

Semua parameter bisa diubah lewat env (lihat `agent/config.py`). Jalankan `--backtest` dulu setiap
kali mengubah parameter. Tidak ada strategi yang selalu profit; tujuannya expectancy positif
(win rate × rata-rata win > loss rate × rata-rata loss) dengan drawdown terkontrol.
