import os, re, json, time, shutil, tempfile, threading, urllib.request
from importlib.machinery import SourceFileLoader

# Load scribd core module + proxy-hunter core + book resolver
MODULE_PATH = os.path.join(os.path.dirname(__file__), "scribd-downloader.py")
scribd = SourceFileLoader("scribd", MODULE_PATH).load_module()
ph = SourceFileLoader("ph", "/home/ubuntu/proxy-hunter/core.py").load_module()
book_res = SourceFileLoader("book_res", os.path.join(os.path.dirname(__file__), "book_resolver.py")).load_module()

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
                return None, "Dokumen kosong / Takedown DMCA"

            scribd.prepare_document_for_print(driver)
            scribd.inject_print_styles(driver)
            driver.execute_script("window.scrollTo(0, 0)")

            saved_path = scribd.save_pdf_pages_individually(driver, out_path)
            if not saved_path or not os.path.exists(saved_path) or os.path.getsize(saved_path) < 100000:
                return None, "File PDF tidak valid / 0 halaman"
            return saved_path, None
        except Exception as e:
            return None, str(e)[:80]
        finally:
            if driver:
                driver.quit()


def main_keyboard(chat_id=None):
    buttons = [
        [
            {"text": "📄 Mode Scribd", "callback_data": "btn_mode_scribd"},
            {"text": "📚 Mode E-Book", "callback_data": "btn_mode_ebook"}
        ],
        [
            {"text": "📂 Riwayat Unduhan", "callback_data": "btn_history"},
            {"text": "⚙️ Status & Proxy", "callback_data": "btn_status"}
        ]
    ]
    return {"inline_keyboard": buttons}


def main_menu_text():
    return (
        "🤖 <b>Universal Book & Document Downloader</b>\n"
        "───────────────────────────────\n"
        "Satu bot untuk semua kebutuhan bacaanmu:\n\n"
        "📄 <b>1. Scribd Downloader</b>\n"
        "Tempel link dokumen Scribd ➔ PDF HD Utuh (Bebas Watermark & Sensor).\n\n"
        "📚 <b>2. E-Book Library Resolver</b>\n"
        "Ketik judul/penulis atau tempel link Google Books ➔ Download PDF / EPUB instan.\n\n"
        "💡 <i>Ketik judul buku atau tempel link langsung ke chat kapan saja.</i>"
    )


def scribd_guide_text():
    return (
        "📄 <b>Scribd Downloader Pro</b>\n"
        "───────────────────────────────\n"
        "Kirim link dokumen Scribd langsung ke chat ini:\n"
        "👉 <i>Contoh:</i>\n"
        "<code>https://id.scribd.com/document/566968205/Judul</code>\n\n"
        "✨ <b>Keunggulan:</b>\n"
        "• 1 File PDF Utuh (Bebas Potongan)\n"
        "• Resolusi High-DPI 2x Retina\n"
        "• Anti-Paywall & Render Font Lengkap\n"
        "• Kompresi Cerdas Standar Premium (~10–15 MB)"
    )


def ebook_guide_text():
    return (
        "📚 <b>E-Book Library Resolver</b>\n"
        "───────────────────────────────\n"
        "Ketik judul buku, nama penulis, atau link Google Play Books:\n"
        "👉 <i>Contoh:</i>\n"
        "• <code>Filosofi Teras</code>\n"
        "• <code>Atomic Habits</code>\n"
        "• <code>Tere Liye</code>\n\n"
        "✨ <b>Keunggulan:</b>\n"
        "• Pilihan Format PDF / EPUB Asli\n"
        "• Download Langsung dalam 2–4 Detik\n"
        "• Koleksi Jutaan Buku Open Access"
    )


# Cache hasil pencarian buku sementara (key: chat_id_idx)
BOOK_SEARCH_CACHE = {}


def handle_book_search(chat_id, raw_input):
    """Cari e-book secara PARALEL di Open Archive dan Scribd secara serentak."""
    # 1. Ekstrak metadata info dari link atau query
    meta = book_res.get_google_book_info(raw_input)
    book_title = meta.get("title") or raw_input.strip()
    book_author = meta.get("author")
    author_str = f"\n✍️ <b>Penulis:</b> {book_author}" if book_author else ""

    status_msg = send_msg(
        chat_id,
        f"📖 <b>Informasi Buku Terdeteksi:</b>\n"
        f"──────────────────────────\n"
        f"📚 <b>Judul:</b> {book_title}{author_str}\n\n"
        f"⚡ <i>Menelusuri Open Archive & Scribd secara serentak...</i>",
        reply_markup=back_keyboard()
    )
    status_id = (status_msg or {}).get("result", {}).get("message_id")

    # 2. Parallel Dual-Search (Archive.org & Scribd jalan bersamaan)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as executor:
        f_archive = executor.submit(book_res.search_books, book_title, book_author, 5)
        f_scribd = executor.submit(book_res.search_scribd_book, book_title, book_author)
        results = f_archive.result()
        scribd_candidates = f_scribd.result()

    # 3. Prioritas 1: Jika ada di Archive.org (EPUB/PDF Asli Instan) -> Tampilkan Opsi Unduh
    if results:
        buttons = []
        text_lines = [
            f"📚 <b>E-Book Ditemukan & Siap Unduh!</b>\n"
            f"──────────────────────────\n"
            f"📖 <b>Judul:</b> {book_title}{author_str}\n\n"
            f"<i>Pilih versi & format dokumen di bawah:</i>\n"
        ]
        for i, b in enumerate(results):
            cache_key = f"{chat_id}_{i}"
            BOOK_SEARCH_CACHE[cache_key] = b
            text_lines.append(f"<b>{i+1}. {b['title'][:38]}</b>")
            text_lines.append(f"   📁 {b['format']} ({b['size_mb']} MB) • 📅 {b['year']}")
            
            btn_label = f"📥 Unduh {b['format']} ({b['size_mb']} MB)"
            buttons.append([{"text": btn_label, "callback_data": f"dl_book_{i}"}])

        buttons.append([{"text": "🔙 Kembali ke Menu Utama", "callback_data": "btn_start"}])

        card_text = "\n".join(text_lines)
        if status_id:
            tg_req("editMessageText", {"chat_id": chat_id, "message_id": status_id, "text": card_text, "parse_mode": "HTML", "reply_markup": {"inline_keyboard": buttons}})
        else:
            send_msg(chat_id, card_text, reply_markup={"inline_keyboard": buttons})
        return

    # 4. Prioritas 2: Jika tidak ada di Archive tapi ada di Scribd -> Tampilkan Kartu Pilihan Dokumen Scribd
    if scribd_candidates:
        buttons = []
        text_lines = [
            f"📚 <b>Dokumen Ditemukan di Scribd!</b>\n"
            f"──────────────────────────\n"
            f"📖 <b>Target:</b> {book_title}{author_str}\n\n"
            f"<i>Pilih dokumen yang sesuai berdasarkan cuplikan isi:</i>\n"
        ]
        for i, doc in enumerate(scribd_candidates):
            cache_key = f"{chat_id}_scribd_{i}"
            BOOK_SEARCH_CACHE[cache_key] = doc
            text_lines.append(f"<b>{i+1}. {doc['title'][:40]}</b>")
            text_lines.append(f"   💬 <i>\"{doc.get('snippet', '')}\"</i>\n")
            
            btn_label = f"📥 Unduh Pilihan {i+1}"
            buttons.append([{"text": btn_label, "callback_data": f"dl_scbook_{i}"}])

        buttons.append([{"text": "🔙 Kembali ke Menu Utama", "callback_data": "btn_start"}])

        card_text = "\n".join(text_lines)
        if status_id:
            tg_req("editMessageText", {"chat_id": chat_id, "message_id": status_id, "text": card_text, "parse_mode": "HTML", "reply_markup": {"inline_keyboard": buttons}})
        else:
            send_msg(chat_id, card_text, reply_markup={"inline_keyboard": buttons})
        return

    # 5. Jika kedua sumber tidak menemukan dokumen
    msg_not_found = (
        f"❌ <b>Dokumen Belum Tersedia</b>\n"
        f"──────────────────────────\n"
        f"📖 <b>Judul:</b> {book_title}{author_str}\n\n"
        f"ℹ️ <b>Penyebab:</b>\n"
        f"Buku ini belum tersedia di arsip publik maupun Scribd.\n\n"
        f"💡 <b>Tips:</b>\n"
        f"Coba cari dengan kata kunci judul lain atau nama penulis."
    )
    if status_id:
        tg_req("editMessageText", {"chat_id": chat_id, "message_id": status_id, "text": msg_not_found, "parse_mode": "HTML", "reply_markup": back_keyboard()})
    else:
        send_msg(chat_id, msg_not_found, reply_markup=back_keyboard())


def handle_book_download(chat_id, idx):
    """Download stream file e-book dan kirim ke Telegram."""
    cache_key = f"{chat_id}_{idx}"
    book_info = BOOK_SEARCH_CACHE.get(cache_key)
    if not book_info:
        send_msg(chat_id, "❌ Sesi pencarian telah kedaluwarsa. Silakan cari ulang bukunya.", reply_markup=main_keyboard(chat_id))
        return

    title = book_info["title"]
    fmt = book_info["format"].lower()
    dl_url = book_info["download_url"]
    
    status_msg = send_msg(
        chat_id,
        f"⚡ <b>Mengunduh E-Book...</b>\n"
        f"──────────────────────────\n"
        f"📖 <b>{title[:35]}</b>\n"
        f"📁 Format: <code>{fmt.upper()}</code> ({book_info['size_mb']} MB)\n"
        f"⏳ <i>Mengambil data dari server arsip...</i>",
        reply_markup=back_keyboard()
    )
    status_id = (status_msg or {}).get("result", {}).get("message_id")

    spool_dir = "/dev/shm" if os.path.exists("/dev/shm") and os.access("/dev/shm", os.W_OK) else "/tmp"
    safe_filename = re.sub(r'[\\/*?:"<>|]', "", title)[:50] + f".{fmt}"
    dest_path = os.path.join(spool_dir, safe_filename)

    try:
        ok = book_res.download_book_stream(dl_url, dest_path)
        if not ok or not os.path.exists(dest_path):
            send_msg(chat_id, "❌ Gagal mengunduh file dari server arsip. Silakan coba link mirror lain.", reply_markup=back_keyboard())
            return

        sz_mb = round(os.path.getsize(dest_path) / 1048576, 1)
        if status_id:
            tg_req("editMessageText", {
                "chat_id": chat_id, "message_id": status_id,
                "text": f"📦 <b>File Siap ({sz_mb} MB)</b>\n📤 Mengunggah ke Telegram...",
                "parse_mode": "HTML"
            })

        caption_text = (
            f"✅ <b>E-Book Berhasil Diunduh!</b>\n\n"
            f"📖 <b>Judul:</b> {title}\n"
            f"✍️ <b>Penulis:</b> {book_info['creator']}\n"
            f"📁 <b>Ukuran:</b> <code>{sz_mb} MB</code> ({fmt.upper()})\n"
            f"✨ <i>Buku lengkap & bebas watermark</i>"
        )

        r = send_pdf(chat_id, dest_path, caption=caption_text, reply_markup=after_download_keyboard())
        if r and r.get("ok"):
            doc_obj = r.get("result", {}).get("document", {})
            file_id = doc_obj.get("file_id")
            if file_id:
                save_history(chat_id, safe_filename, sz_mb, file_id)
            if status_id:
                tg_req("deleteMessage", {"chat_id": chat_id, "message_id": status_id})
        else:
            send_msg(chat_id, "❌ Gagal mengirim file ke Telegram.", reply_markup=back_keyboard())

    except Exception as e:
        send_msg(chat_id, f"❌ Error download e-book: {e}", reply_markup=back_keyboard())
    finally:
        if os.path.exists(dest_path):
            try: os.remove(dest_path)
            except Exception: pass


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
        return (f"⚙️ <b>Status Sistem & Proxy</b>\n"
                f"───────────────────────────────\n"
                f"🌐 Proxy Aktif : <b>{len(pool)} IP</b> (Lolos WAF{age_s})\n"
                f"⚡ Tercepat    : <code>{fastest}</code>\n"
                f"🖥 Engine      : <b>VPS Worker 16GB (Active)</b>\n\n"
                f"💡 <i>Gunakan tombol di bawah untuk menyegarkan stok proxy:</i>")
    return ("⚙️ <b>Status Sistem & Proxy</b>\n"
            "───────────────────────────────\n"
            "🌐 Proxy Aktif : <b>0 (Kosong)</b>\n\n"
            "Klik tombol di bawah untuk menggarap proxy gratis baru:")


def status_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "🎯 Garap Proxy Baru", "callback_data": "btn_cari"}],
            [{"text": "🔙 Menu Utama", "callback_data": "btn_start"}]
        ]
    }


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
                        send_msg(cb_chat_id, main_menu_text(), reply_markup=main_keyboard(cb_chat_id))
                    elif cb_data == "btn_mode_scribd":
                        send_msg(cb_chat_id, scribd_guide_text(), reply_markup=back_keyboard())
                    elif cb_data == "btn_mode_ebook":
                        send_msg(cb_chat_id, ebook_guide_text(), reply_markup=back_keyboard())
                    elif cb_data == "btn_status":
                        send_msg(cb_chat_id, status_text(), reply_markup=status_keyboard())
                    elif cb_data == "btn_cari":
                        if cb_chat_id in HUNTING:
                            send_msg(cb_chat_id, "⏳ Garap proxy sedang berjalan. Mohon tunggu...", reply_markup=back_keyboard())
                        else:
                            threading.Thread(target=run_hunt, args=(cb_chat_id,), daemon=True).start()
                    elif cb_data == "btn_history":
                        hist = get_history(cb_chat_id)
                        if not hist:
                            send_msg(cb_chat_id, "📭 <b>Belum ada riwayat unduhan.</b>\nKirim link dokumen Scribd atau cari buku untuk mulai mengunduh!", reply_markup=main_keyboard(cb_chat_id))
                        else:
                            send_msg(cb_chat_id,
                                     "📂 <b>Direktori Riwayat Unduhan:</b>\n"
                                     "<i>Klik dokumen di bawah untuk mengunduh ulang secara instan:</i>",
                                     reply_markup=history_keyboard(cb_chat_id))
                    elif cb_data.startswith("dl_scbook_"):
                        try:
                            idx = int(cb_data.replace("dl_scbook_", ""))
                            cache_key = f"{cb_chat_id}_scribd_{idx}"
                            doc_item = BOOK_SEARCH_CACHE.get(cache_key)
                            if doc_item:
                                status_msg = send_msg(
                                    cb_chat_id,
                                    f"⚡ <b>Memproses Dokumen Scribd...</b>\n"
                                    f"──────────────────────────\n"
                                    f"📖 <b>{doc_item['title']}</b>\n"
                                    f"🌐 Menghubungkan ke proxy & membuka sesi HD...",
                                    reply_markup=back_keyboard()
                                )
                                s_id = (status_msg or {}).get("result", {}).get("message_id")
                                threading.Thread(target=run_download, args=(cb_chat_id, doc_item["url"], s_id), daemon=True).start()
                            else:
                                send_msg(cb_chat_id, "❌ Sesi telah kedaluwarsa. Silakan cari ulang bukunya.", reply_markup=main_keyboard(cb_chat_id))
                        except Exception as e:
                            print(f"Error triggering scribd book download: {e}")
                    elif cb_data.startswith("dl_book_"):
                        try:
                            idx = int(cb_data.replace("dl_book_", ""))
                            threading.Thread(target=handle_book_download, args=(cb_chat_id, idx), daemon=True).start()
                        except Exception as e:
                            print(f"Error triggering book download: {e}")
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
                    send_msg(chat_id, main_menu_text(), reply_markup=main_keyboard(chat_id))
                    continue

                if text_lower.startswith("/status") or text_lower == "status":
                    send_msg(chat_id, status_text(), reply_markup=status_keyboard())
                    continue

                if text_lower == "/cari" or text_lower == "cari":
                    if chat_id in HUNTING:
                        send_msg(chat_id, "⏳ Garap proxy sedang berjalan, mohon tunggu hingga selesai.", reply_markup=back_keyboard())
                    else:
                        threading.Thread(target=run_hunt, args=(chat_id,), daemon=True).start()
                    continue

                # 1. Cek Link Scribd
                urls_scribd = re.findall(r"https?://(?:[a-zA-Z0-9_-]+\.)?scribd\.com/(?:document|doc)/\d+[^\s]*", raw_text)
                if urls_scribd:
                    send_action(chat_id, "typing")
                    init_msg = send_msg(
                        chat_id,
                        "⚡ <b>Memproses Dokumen Scribd...</b>\n"
                        "──────────────────────────\n"
                        "🔗 Link Scribd terdeteksi\n"
                        "🌐 Memilih proxy tercepat & membuka sesi...",
                        reply_markup=back_keyboard()
                    )
                    init_msg_id = (init_msg or {}).get("result", {}).get("message_id")
                    threading.Thread(target=run_download, args=(chat_id, urls_scribd[0], init_msg_id), daemon=True).start()
                    continue

                # 2. Cek Link Google Play Books / Google Books
                if "play.google.com/store/books" in raw_text or "books.google" in raw_text:
                    threading.Thread(target=handle_book_search, args=(chat_id, raw_text), daemon=True).start()
                    continue

                # 3. Smart Fallback: Semua input teks lain (atau /buku, /cari <kata>) otomatis masuk ke pencarian E-Book
                q_clean = raw_text
                for prefix in ("/buku ", "buku ", "/cari ", "cari "):
                    if text_lower.startswith(prefix):
                        q_clean = raw_text[len(prefix):].strip()
                        break
                
                if q_clean:
                    threading.Thread(target=handle_book_search, args=(chat_id, q_clean), daemon=True).start()
                else:
                    send_msg(chat_id, main_menu_text(), reply_markup=main_keyboard(chat_id))

        except Exception as e:
            print(f"Poll loop error: {e}")
            time.sleep(2)


if __name__ == "__main__":
    poll()
