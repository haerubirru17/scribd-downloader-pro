# Scribd Downloader Pro (Telegram Bot)

Bot Telegram cerdas untuk mengunduh dokumen Scribd secara otomatis menjadi 1 file PDF utuh dengan kualitas High-DPI 2x Retina, bebas watermark, anti-paywall, dan dilengkapi rotasi proxy otomatis.

## ✨ Fitur Utama
- **1-Card Live UI**: Update status proses dinamis di 1 bubble pesan (Live % & Anti-Spam).
- **High-DPI Retina (2x Scale)**: Teks & gambar dokumen jernih dan tajam saat di-zoom.
- **Adaptive Quality (Target ~10-15MB)**: Menggunakan kompresi citra adaptif standar Scribd Premium tanpa mengurangi ketajaman teks.
- **Anti-Paywall & Multi-Language Support**: Dilengkapi paket font Unicode/Arab lengkap (Amiri, Scheherazade, Noto) tanpa glitch kotak hitam.
- **Direktori History Cerdas**: Re-download instan via file_id Telegram dalam 0.1 detik tanpa membebani storage server.
- **Free Proxy Hunter Core**: Rotasi otomatis proxy publik gratis untuk menembus WAF Fastly/Signal Sciences.

## 🚀 Cara Menjalankan
1. Clone repository:
   ```bash
   git clone https://github.com/haerubirru17/scribd-downloader-pro.git
   cd scribd-downloader-pro
   ```
2. Buat file `.env`:
   ```bash
   echo "TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN_HERE" > .env
   ```
3. Install dependensi & jalankan:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   python bot.py
   ```
