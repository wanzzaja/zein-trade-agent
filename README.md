# zein-trade-agent
AI crypto trading agent for recall
zein-trade-agent/

  README.md
  requirements.txt
  main.py
  agent/
      __init__.py
      config.py
      strategy.py
      trader.py
      utils.py
  data/
      logs.txt

## Cara kerja signal scanner

Bot hanya mengambil posisi **long** (Recall = spot) dan sebuah sinyal harus lolos semua filter:

1. **Trend timeframe besar (4h)** naik: close > EMA50, EMA50 menanjak, EMA20 > EMA50.
2. **Market trending** (ADX ≥ 20, +DI > -DI) dan EMA20 > EMA50 > EMA200 di timeframe entry (1h).
3. **Setup**: *pullback* ke EMA20 + RSI reset + candle bullish menembus high sebelumnya, atau
   *breakout* Donchian 20 setelah squeeze Bollinger dengan volume > 1.5x rata-rata.
4. **Skor konfluensi** ≥ `MIN_SCORE` (HTF > EMA200, ADX kuat, volume, MACD, RSI sehat, close kuat).
5. **RR riil** ≥ `MIN_RR` (default 2.0, sudah dipotong fee/slippage): SL di bawah swing low (dibatasi
   0.8–3 ATR), target 3R tapi dipotong ke resistance besar terdekat. Kalau ruang ke resistance tidak cukup,
   sinyal **dibuang**, bukan dipaksa.

Manajemen posisi: SL ke break-even di +1R, jual 50% di TP1 (1.5R), sisanya trailing stop 2.5 ATR
sampai target, dengan batas waktu 72 bar. Risiko 1% equity per trade, maks 3 posisi, cooldown setelah loss,
dan bot berhenti entry kalau rugi harian ≥ 3%.

## Menjalankan

```bash
pip install -r requirements.txt
cp .env.example .env          # isi RECALL_API_KEY, set DRY_RUN=false kalau sudah yakin
python main.py --backtest     # uji di ~6000 candle 1h terakhir
python main.py --scan         # scan sekali
python main.py                # loop live
```

Semua parameter bisa diubah lewat env (lihat `agent/config.py`). Jalankan `--backtest` dulu setiap
kali mengubah parameter. Tidak ada strategi yang selalu profit; tujuannya expectancy positif
(win rate × rata-rata win > loss rate × rata-rata loss) dengan drawdown terkontrol.
