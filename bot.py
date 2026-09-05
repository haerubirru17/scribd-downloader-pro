import os, re, json, time, shutil, tempfile, threading, urllib.request
from importlib.machinery import SourceFileLoader

# Load scribd core module + proxy-hunter core
MODULE_PATH = os.path.join(os.path.dirname(__file__), "scribd-downloader.py")
scribd = SourceFileLoader("scribd", MODULE_PATH).load_module()
ph = SourceFileLoader("ph", "/home/ubuntu/proxy-hunter/core.py").load_module()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

POOL_FILE = "/home/ubuntu/proxy-hunter/data/latest.json"
DATA_DIR = "/home/ubuntu/proxy-hunter/data"
MAX_TRIES = 8          # ponytail: toleransi 8 proxy per dokumen (rotasi otomatis jika ada proxy yang mendadak putus)

HUNT_LOCK = threading.Lock()   # ponytail: satu garap pada satu waktu
HUNTING = set()                # chat_id yang sedang /cari
DL_LOCK = threading.Lock()     # ponytail: global — Chrome makan 300-600MB, jangan paralel


def tg_req(method, data=None, files=None):
    url = f"{API_BASE}/{method}"
    timeout = 300 if files else 60
    if files:
        # Multipart form data upload
        boundary = f"----WebKitFormBoundary{int(time.time()*1000)}"
        body = bytearray()
        for k, v in (data or {}).items():
            body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        for field, (fname, fbytes) in files.items():
            body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{fname}\"\r\nContent-Type: application/pdf\r\n\r\n".encode())
            body.extend(fbytes)
            body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(url, data=bytes(body), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    elif data is not None:
        payload = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        print(f"Telegram API HTTPError ({method}) {e.code}: {err_body}")
        return None
    except Exception as e:
        print(f"Telegram API error ({method}): {e}")
        return None


def send_msg(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_req("sendMessage", payload)

# alias buat handler garap
send = send_msg
send_file = lambda chat_id, path, caption="": tg_req(
    "sendDocument", {"chat_id": chat_id, "caption": caption},
    files={"document": (os.path.basename(path), open(path, "rb").read())})


HISTORY_FILE = "/home/ubuntu/proxy-hunter/data/history.json"


def save_history(chat_id, filename, size_mb, file_id):
    """Simpan metadata dokumen ke history JSON per chat."""
    try:
        data = {}
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                data = json.load(f)
        cid = str(chat_id)
        user_hist = data.get(cid, [])
        # Format tanggal WIB (UTC+7)
        now_wib = time.strftime("%d/%m %H:%M", time.localtime(time.time() + 7 * 3600))
        entry = {
            "title": filename.replace(".pdf", ""),
            "size": f"{size_mb:.1f} MB",
            "file_id": file_id,
            "date": now_wib
        }
        # Simpan max 10 dokumen terakhir
        user_hist.insert(0, entry)
        data[cid] = user_hist[:10]
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        with open(HISTORY_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Error saving history: {e}")


def get_history(chat_id):
    """Ambil list history dokumen per chat."""
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                data = json.load(f)
                return data.get(str(chat_id), [])
    except Exception:
        pass
    return []


def history_keyboard(chat_id):
    hist = get_history(chat_id)
    buttons = []
    for i, item in enumerate(hist):
        btn_text = f"📄 {i+1}. {item['title'][:25]} ({item['size']})"
        buttons.append([{"text": btn_text, "callback_data": f"dl_hist_{i}"}])
    buttons.append([{"text": "🔙 Kembali ke Menu Utama", "callback_data": "btn_start"}])
    return {"inline_keyboard": buttons}


def back_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "🔙 Kembali ke Menu Utama", "callback_data": "btn_start"}]
        ]
    }


def after_download_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "📥 Unduh Dokumen Lain", "callback_data": "btn_start"},
                {"text": "📂 Direktori History", "callback_data": "btn_history"}
            ]
        ]
    }


def send_pdf(chat_id, filepath, caption="", reply_markup=None):
    filename = os.path.basename(filepath)
    with open(filepath, "rb") as f:
        file_bytes = f.read()
    payload = {
        "chat_id": chat_id,
        "caption": caption,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    return tg_req("sendDocument", payload, files={"document": (filename, file_bytes)})


# ==================== PROXY POOL ====================

def get_proxy_pool():
    """Proxy lolos Scribd, deduplikasi & urut tercepat. None kalau file stok tidak ada."""
    try:
        with open(POOL_FILE) as f:
            d = json.load(f)
    except Exception:
        return None
    
    # Ambil list proxy unik
    raw_pool = d.get("scribd", []) or d.get("both", [])
    seen = set()
    pool = []
    for item in raw_pool:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            p, lat = item[0], item[1]
            if p not in seen and lat <= 3.5:
                seen.add(p)
                pool.append((p, lat))
    
    pool.sort(key=lambda x: x[1])
    return [p for p, _ in pool]


def pool_age_hours():
    try:
        with open(POOL_FILE) as f:
            ts = json.load(f).get("ts", "")
        t = time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M"))
        return max(0, (time.time() - t) / 3600)
    except Exception:
        return None


# ==================== DOWNLOAD (dengan rotasi proxy) ====================

def send_action(chat_id, action="upload_document"):
    """Kirim animasi live action Telegram (typing, upload_document, dll)."""
    return tg_req("sendChatAction", {"chat_id": chat_id, "action": action})


def try_download(converted_url, filename, proxy):
    """Satu percobaan download lewat Chrome+proxy. Return (path, None) atau (None, alasan)."""
    # Gunakan RAM disk /dev/shm jika tersedia untuk I/O instan
    spool_root = "/dev/shm" if os.path.exists("/dev/shm") and os.access("/dev/shm", os.W_OK) else None
    out_dir = tempfile.mkdtemp(prefix="scribd-out-", dir=spool_root)
    out_path = os.path.join(out_dir, filename)
    with tempfile.TemporaryDirectory(prefix="scribd-prof-", dir=spool_root) as profile_dir:
        driver = None
        try:
            options = scribd.build_chrome_options(profile_dir)
            options.add_argument(f"--proxy-server=http://{proxy}")
            driver = scribd.webdriver.Chrome(options=options)
            
            # CDP Speed Boost: Blokir tracker, telemetry & script sampah pihak ketiga
            try:
                driver.execute_cdp_cmd("Network.enable", {})
                driver.execute_cdp_cmd("Network.setBlockedURLs", {
                    "urls": ["*analytics*", "*doubleclick*", "*facebook*", "*datadog*", "*sentry*", "*.gif", "*track*"]
                })
            except Exception:
                pass

            driver.set_page_load_timeout(90)
            driver.get(converted_url)
            time.sleep(1.5)

            scribd.hide_cookie_dialogs(driver)
            total_pages = driver.execute_script("return document.querySelectorAll('.outer_page').length;")
            if total_pages == 0:
                return None, "406/blok"  # WAF atau proxy mati — rotasi

            scribd.prepare_document_for_print(driver)
            scribd.inject_print_styles(driver)
            driver.execute_script("window.scrollTo(0, 0)")

            saved_path = scribd.save_pdf_pages_individually(driver, out_path)
            if not saved_path or not os.path.exists(saved_path):
                return None, "export gagal"
            return saved_path, None
        except Exception as e:
            return None, str(e)[:80]
        finally:
            if driver:
                driver.quit()


def main_keyboard(chat_id=None):
    buttons = [
        [
            {"text": "📊 Status Proxy", "callback_data": "btn_status"},
            {"text": "🎯 Garap Proxy", "callback_data": "btn_cari"}
        ]
    ]
    if chat_id and get_history(chat_id):
        buttons.append([{"text": "📂 Direktori History Unduhan", "callback_data": "btn_history"}])
    return {"inline_keyboard": buttons}


def download_scribd_pdf(url, progress=None, chat_id=None, status_msg_id=None):
    """Rotasi proxy: pre-cekat cepat → Chrome. Gagal → proxy berikutnya."""
    converted_url = scribd.convert_scribd_link(url)
    if converted_url == "Invalid Scribd URL":
        return None, "URL Scribd tidak valid. Gunakan format https://www.scribd.com/document/..."

    pool = get_proxy_pool()
    if not pool:
        return None, "POOL_EMPTY"

    pdf_filename = scribd.get_filename_from_url(url)
    errs, tried = [], 0
    for proxy in pool:
        if tried >= MAX_TRIES:
            break
        if not ph.test_proxy(proxy, "scribd"):  # reuse cek WAF dari core hunter
            continue  # proxy mati, skip diam-diam
        tried += 1
        msg_id = progress(tried, proxy) if progress else status_msg_id
        # progres halaman → edit pesan percobaan (throttle 12s)
        state = {"last": 0.0}
        chat_id_of_url = chat_id

        def page_cb(done, total, _proxy=proxy, _state=state):
            now = time.time()
            if not msg_id or (now - _state["last"] < 12 and done != total):
                return
            _state["last"] = now
            persen = int((done / total) * 100)
            tg_req("editMessageText", {
                "chat_id": chat_id_of_url, "message_id": msg_id,
                "text": f"📄 <b>Mengunduh Dokumen...</b>\n\n"
                        f"📊 Progress: <b>{done}/{total}</b> halaman ({persen}%)\n"
                        f"⚡ Proxy Aktif: <code>{_proxy}</code>\n"
                        f"⏳ <i>Memproses PDF kualitas tinggi...</i>",
                "parse_mode": "HTML"})

        scribd.PAGE_CB = page_cb
        try:
            path, err = try_download(converted_url, pdf_filename, proxy)
        finally:
            scribd.PAGE_CB = None
        if path:
            return path, None
        errs.append(f"{proxy}: {err}")

    return None, "Semua %d proxy gagal (%s)" % (tried, "; ".join(errs) if errs else "stok mati semua")


# ==================== GARAP PROXY ====================

def make_bar(current, total, length=10):
    filled = int(length * current / total) if total > 0 else 0
    return "█" * filled + "░" * (length - filled)


def _hunt_core(chat_id, announce=True):
    card_msg_id = None
    if announce:
        init_res = send_msg(
            chat_id,
            "🎯 <b>Garap Proxy Scribd Dimulai...</b>\n"
            "──────────────────────────\n"
            "🔍 Mengumpulkan ~5.700 IP proxy publik...\n"
            "⏳ <i>Menyiapkan validasi multi-threaded...</i>",
            reply_markup=back_keyboard()
        )
        card_msg_id = (init_res or {}).get("result", {}).get("message_id")

    if not HUNT_LOCK.acquire(blocking=False):
        if announce:
            send_msg(chat_id, "⏳ <b>Garap Sedang Berjalan</b>\nProses lain sedang berlangsung. Hasilnya akan langsung dipakai otomatis.", reply_markup=back_keyboard())
        return

    t_start = time.time()
    last_update = 0.0

    def progress_callback(checked, total, passed, elapsed):
        nonlocal last_update
        now = time.time()
        if not card_msg_id or (now - last_update < 4.0 and checked != total):
            return
        last_update = now

        pct = int(checked * 100 / total) if total > 0 else 0
        bar = make_bar(checked, total, 10)
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        time_str = f"{mins:02d}:{secs:02d}"

        card_text = (
            f"🎯 <b>Garap Proxy Scribd</b>\n"
            f"──────────────────────────\n"
            f"📊 Progres : [{bar}] <b>{pct}%</b>\n"
            f"🔍 Diperiksa : <b>{checked}/{total}</b> IP\n"
            f"⚡ Lolos WAF : <b>{passed}</b> Proxy Aktif\n"
            f"⏱ Waktu     : <code>{time_str}</code>\n"
            f"⏳ Status   : <i>Memvalidasi koneksi ke Scribd...</i>"
        )
        tg_req("editMessageText", {
            "chat_id": chat_id,
            "message_id": card_msg_id,
            "text": card_text,
            "parse_mode": "HTML",
            "reply_markup": back_keyboard()
        })

    try:
        h = ph.hunt(DATA_DIR, progress_cb=progress_callback if card_msg_id else None)
        total_elapsed = time.time() - t_start

        if not h or not h.get("scribd"):
            msg_fail = "❌ <b>Garap Gagal</b>\nTidak ada proxy yang lolos uji WAF. Silakan coba beberapa saat lagi."
            if card_msg_id:
                tg_req("editMessageText", {"chat_id": chat_id, "message_id": card_msg_id, "text": msg_fail, "parse_mode": "HTML", "reply_markup": back_keyboard()})
            else:
                send_msg(chat_id, msg_fail, reply_markup=back_keyboard())
            return

        summary_text = ph.format_summary(h, total_elapsed)
        after_hunt_markup = {
            "inline_keyboard": [
                [{"text": "📥 Unduh Dokumen Sekarang", "callback_data": "btn_start"}],
                [{"text": "📊 Cek Status Stok", "callback_data": "btn_status"}]
            ]
        }

        if card_msg_id:
            tg_req("editMessageText", {
                "chat_id": chat_id,
                "message_id": card_msg_id,
                "text": summary_text,
                "parse_mode": "HTML",
                "reply_markup": after_hunt_markup
            })
        else:
            send_msg(chat_id, summary_text, reply_markup=after_hunt_markup)

    except Exception as e:
        err_text = f"❌ <b>Error Garap Proxy:</b> {e}"
        if card_msg_id:
            tg_req("editMessageText", {"chat_id": chat_id, "message_id": card_msg_id, "text": err_text, "parse_mode": "HTML", "reply_markup": back_keyboard()})
        else:
            send_msg(chat_id, err_text, reply_markup=back_keyboard())
    finally:
        HUNT_LOCK.release()


def run_hunt(chat_id):
    if chat_id in HUNTING:
        send_msg(chat_id, "⏳ <b>Garap Sedang Berjalan</b>\nMohon tunggu hingga proses selesai.", reply_markup=back_keyboard())
        return
    HUNTING.add(chat_id)
    try:
        _hunt_core(chat_id)
    finally:
        HUNTING.discard(chat_id)


# ==================== WORKFLOW DOWNLOAD ====================

def run_download(chat_id, url, status_msg_id=None):
    if not DL_LOCK.acquire(blocking=False):
        if status_msg_id:
            tg_req("editMessageText", {
                "chat_id": chat_id, "message_id": status_msg_id,
                "text": "⏳ <b>Antrean Penuh</b>\nMasih ada unduhan yang sedang berjalan. Mohon tunggu sebentar ya.",
                "parse_mode": "HTML",
                "reply_markup": back_keyboard()
            })
        else:
            send_msg(chat_id, "⏳ <b>Antrean Penuh</b>\nMasih ada unduhan yang sedang berjalan. Mohon tunggu sebentar ya.", reply_markup=back_keyboard())
        return
    try:
        if not get_proxy_pool():
            if status_msg_id:
                tg_req("editMessageText", {
                    "chat_id": chat_id, "message_id": status_msg_id,
                    "text": "📭 <b>Stok Proxy Kosong</b>\nSedang menggarap proxy gratis otomatis (±5-6 menit)...",
                    "parse_mode": "HTML"
                })
            else:
                status_msg = send_msg(chat_id, "📭 <b>Stok Proxy Kosong</b>\nSedang menggarap proxy gratis otomatis (±5-6 menit)...")
                status_msg_id = (status_msg or {}).get("result", {}).get("message_id")
            
            _hunt_core(chat_id, announce=False)
            if not get_proxy_pool():
                msg_fail = "❌ Gagal mendapatkan proxy Scribd. Silakan coba lagi nanti."
                if status_msg_id:
                    tg_req("editMessageText", {"chat_id": chat_id, "message_id": status_msg_id, "text": msg_fail, "parse_mode": "HTML", "reply_markup": back_keyboard()})
                else:
                    send_msg(chat_id, msg_fail, reply_markup=back_keyboard())
                return

        if not status_msg_id:
            status_msg = send_msg(chat_id, "🔍 <b>Link Terdeteksi!</b>\nMenyiapkan sesi & menghubungkan ke proxy...")
            status_msg_id = (status_msg or {}).get("result", {}).get("message_id")
        else:
            tg_req("editMessageText", {
                "chat_id": chat_id, "message_id": status_msg_id,
                "text": "🔍 <b>Menyiapkan Unduhan...</b>\nMenghubungkan ke proxy tercepat...",
                "parse_mode": "HTML"
            })

        def prog(i, proxy):
            if status_msg_id:
                tg_req("editMessageText", {
                    "chat_id": chat_id, "message_id": status_msg_id,
                    "text": f"🔄 <b>Menghubungkan Dokumen...</b>\nPercobaan <b>{i}/{MAX_TRIES}</b> via <code>{proxy}</code>",
                    "parse_mode": "HTML"
                })
                return status_msg_id
            return None

        pdf_path, err = download_scribd_pdf(url, progress=prog, chat_id=chat_id, status_msg_id=status_msg_id)
        if err == "POOL_EMPTY":
            if status_msg_id:
                tg_req("editMessageText", {"chat_id": chat_id, "message_id": status_msg_id, "text": "📭 Stok proxy kosong. Jalankan <code>/cari</code> dulu ya.", "parse_mode": "HTML", "reply_markup": back_keyboard()})
            else:
                send_msg(chat_id, "📭 Stok proxy kosong. Jalankan <code>/cari</code> dulu ya.", reply_markup=back_keyboard())
        elif err:
            msg_fail = f"❌ <b>Gagal Mengunduh Dokumen</b>\n\nAlasan: {err}\n💡 <i>Gunakan tombol di bawah untuk memperbarui stok proxy.</i>"
            if status_msg_id:
                tg_req("editMessageText", {"chat_id": chat_id, "message_id": status_msg_id, "text": msg_fail, "parse_mode": "HTML", "reply_markup": main_keyboard(chat_id)})
            else:
                send_msg(chat_id, msg_fail, reply_markup=main_keyboard(chat_id))
        else:
            try:
                sz_mb = os.path.getsize(pdf_path) / 1048576
                if status_msg_id:
                    tg_req("editMessageText", {
                        "chat_id": chat_id, "message_id": status_msg_id,
                        "text": f"📦 <b>Dokumen Siap ({sz_mb:.1f} MB)</b>\n📤 Sedang mengirim file PDF ke chat...",
                        "parse_mode": "HTML"
                    })
                
                doc_title = os.path.basename(pdf_path).replace(".pdf", "")
                caption_text = (
                    f"✅ <b>Dokumen Scribd Berhasil Diunduh!</b>\n\n"
                    f"📄 <b>Judul:</b> {doc_title}\n"
                    f"📁 <b>Ukuran:</b> <code>{sz_mb:.1f} MB</code> (1 File Utuh)\n"
                    f"✨ <i>Kualitas HD Retina & Bebas Watermark</i>"
                )
                
                r = send_pdf(chat_id, pdf_path, caption=caption_text, reply_markup=after_download_keyboard())
                if not r or not r.get("ok"):
                    send_msg(chat_id, "❌ Gagal mengirim file ke Telegram. Silakan coba kirim ulang linknya.", reply_markup=back_keyboard())
                else:
                    # Simpan file_id ke history untuk instant re-download
                    doc_obj = r.get("result", {}).get("document", {})
                    file_id = doc_obj.get("file_id")
                    if file_id:
                        save_history(chat_id, os.path.basename(pdf_path), sz_mb, file_id)
                    
                    if status_msg_id:
                        tg_req("deleteMessage", {"chat_id": chat_id, "message_id": status_msg_id})
            finally:
                shutil.rmtree(os.path.dirname(pdf_path), ignore_errors=True)
    finally:
        DL_LOCK.release()


def status_text():
    pool = get_proxy_pool() or []
    age = pool_age_hours()
    age_s = f", digarap {age:.0f} jam lalu" if age is not None else ""
    if pool:
        fastest = pool[0]
        return (f"📊 <b>Status Scribd Downloader</b>\n\n"
                f"📄 Stok Proxy Aktif: <b>{len(pool)}</b>{age_s}\n"
                f"⚡ Proxy Tercepat: <code>{fastest}</code>\n\n"
                f"💡 <i>Kirim link dokumen Scribd kapan saja untuk langsung mengunduh.</i>")
    return ("📊 <b>Status Scribd Downloader</b>\n\n"
            "📄 Stok Proxy: <b>0 (Kosong)</b>\n\n"
            "Klik tombol di bawah untuk menggarap proxy gratis baru:")


def poll():
    if not BOT_TOKEN:
        print("ERROR: Set TELEGRAM_BOT_TOKEN environment variable first.")
        raise SystemExit(1)

    print("Scribd Telegram Bot running (polling)...")
    offset = 0
    while True:
        try:
            res = tg_req("getUpdates", {"offset": offset, "timeout": 25})
            if not res or not res.get("ok"):
                time.sleep(2)
                continue

            for update in res.get("result", []):
                offset = update["update_id"] + 1

                # Handle Inline Button Clicks (Callback Queries)
                cb = update.get("callback_query")
                if cb:
                    cb_id = cb["id"]
                    cb_data = cb.get("data", "")
                    cb_chat_id = cb["message"]["chat"]["id"]
                    tg_req("answerCallbackQuery", {"callback_query_id": cb_id})

                    if cb_data == "btn_start":
                        send_msg(cb_chat_id,
                                 "👋 <b>Scribd Downloader Pro</b>\n\n"
                                 "📄 <b>Cara Download:</b>\n"
                                 "Cukup kirimkan link dokumen Scribd langsung ke chat ini, contoh:\n"
                                 "<code>https://id.scribd.com/document/566968205/Judul-Dokumen</code>\n\n"
                                 "Bot akan otomatis memproses dokumen menjadi PDF kualitas tinggi (1 file utuh).",
                                 reply_markup=main_keyboard(cb_chat_id))
                    elif cb_data == "btn_status":
                        send_msg(cb_chat_id, status_text(), reply_markup=main_keyboard(cb_chat_id))
                    elif cb_data == "btn_cari":
                        if cb_chat_id in HUNTING:
                            send_msg(cb_chat_id, "⏳ Garap proxy sedang berjalan. Mohon tunggu...")
                        else:
                            threading.Thread(target=run_hunt, args=(cb_chat_id,), daemon=True).start()
                    elif cb_data == "btn_history":
                        hist = get_history(cb_chat_id)
                        if not hist:
                            send_msg(cb_chat_id, "📭 <b>Belum ada riwayat unduhan.</b>\nKirim link dokumen Scribd untuk mulai mengunduh!", reply_markup=main_keyboard(cb_chat_id))
                        else:
                            send_msg(cb_chat_id,
                                     "📂 <b>Direktori Riwayat Unduhan:</b>\n"
                                     "<i>Klik dokumen di bawah untuk mengunduh ulang secara instan:</i>",
                                     reply_markup=history_keyboard(cb_chat_id))
                    elif cb_data.startswith("dl_hist_"):
                        try:
                            idx = int(cb_data.replace("dl_hist_", ""))
                            hist = get_history(cb_chat_id)
                            if 0 <= idx < len(hist):
                                item = hist[idx]
                                tg_req("sendDocument", {
                                    "chat_id": cb_chat_id,
                                    "document": item["file_id"],
                                    "caption": f"⚡ <b>Instant Delivery dari Direktori:</b>\n📄 <b>{item['title']}</b> ({item['size']})\n🕒 Diunduh pada: <code>{item['date']}</code>",
                                    "parse_mode": "HTML",
                                    "reply_markup": json.dumps(after_download_keyboard())
                                })
                        except Exception as e:
                            print(f"Error handling history redownload: {e}")
                    continue

                msg = update.get("message") or update.get("channel_post")
                if not msg:
                    continue

                chat_id = msg["chat"]["id"]
                raw_text = (msg.get("text") or "").strip()

                if not raw_text:
                    continue

                text_lower = raw_text.lower()

                if text_lower.startswith("/start") or text_lower == "start":
                    send_msg(chat_id,
                             "👋 <b>Halo! Selamat Datang di Scribd Downloader Pro</b>\n\n"
                             "📄 <b>Cara Download:</b>\n"
                             "Cukup kirimkan link dokumen Scribd langsung ke chat ini, contoh:\n"
                             "<code>https://id.scribd.com/document/566968205/Judul-Dokumen</code>\n\n"
                             "Bot akan otomatis memproses dokumen menjadi PDF kualitas tinggi (1 file utuh).",
                             reply_markup=main_keyboard())
                    continue

                if text_lower.startswith("/status") or text_lower == "status":
                    send_msg(chat_id, status_text(), reply_markup=main_keyboard())
                    continue

                if text_lower.startswith("/cari") or text_lower == "cari":
                    if chat_id in HUNTING:
                        send_msg(chat_id, "⏳ Garap proxy sedang berjalan, mohon tunggu hingga selesai.")
                    else:
                        threading.Thread(target=run_hunt, args=(chat_id,), daemon=True).start()
                    continue

                urls = re.findall(r"https?://(?:[a-zA-Z0-9_-]+\.)?scribd\.com/(?:document|doc)/\d+[^\s]*", raw_text)
                if not urls:
                    send_msg(chat_id,
                             "❌ <b>Link Tidak Valid</b>\n"
                             "Mohon kirimkan link dokumen Scribd yang benar, contoh:\n"
                             "<code>https://id.scribd.com/document/123456789/Judul</code>",
                             reply_markup=main_keyboard(chat_id))
                    continue

                # Instant Feedback (<0.2s): Kirim aksi "typing/upload" di header Telegram + status card seketika
                send_action(chat_id, "typing")
                init_msg = send_msg(
                    chat_id,
                    "⚡ <b>Memproses Dokumen...</b>\n"
                    "──────────────────────────\n"
                    "🔗 Link Scribd terdeteksi\n"
                    "🌐 Memilih proxy tercepat & membuka sesi...",
                    reply_markup=back_keyboard()
                )
                init_msg_id = (init_msg or {}).get("result", {}).get("message_id")

                # Jalankan download di background thread
                threading.Thread(target=run_download, args=(chat_id, urls[0], init_msg_id), daemon=True).start()

        except Exception as e:
            print(f"Poll loop error: {e}")
            time.sleep(2)


if __name__ == "__main__":
    poll()
