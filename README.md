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
      utils.py         # data market + indikator
  ui/index.html        # tampilan Zorra Terminal
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
```

Semua parameter bisa diubah lewat env (lihat `agent/config.py`). Jalankan `--backtest` dulu setiap
kali mengubah parameter. Tidak ada strategi yang selalu profit; tujuannya expectancy positif
(win rate × rata-rata win > loss rate × rata-rata loss) dengan drawdown terkontrol.
