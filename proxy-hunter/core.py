"""Core proxy-hunter: gather free proxies & test specifically against Scribd WAF."""
import os, re, json, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

SOURCES = [
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
]

SCRIBD_TARGET_URL = "https://www.scribd.com/embeds/888163707/content"


def gather_proxies():
    out = set()
    for src in SOURCES:
        try:
            raw = urllib.request.urlopen(src, timeout=20).read().decode(errors="replace")
            out |= {re.sub(r"^https?://", "", l.strip()) for l in raw.splitlines()
                    if re.match(r"^(https?://)?\d{1,3}(\.\d{1,3}){3}:\d+$", l.strip())}
        except Exception as e:
            print(f"source fail {src}: {e}")
    return list(out)


def test_proxy(proxy, target="scribd"):
    """Test proxy response time against Scribd embed WAF (verifikasi body asli Scribd)."""
    h = urllib.request.build_opener(urllib.request.ProxyHandler(
        {"http": f"http://{proxy}", "https": f"http://{proxy}"}))
    h.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0")]
    t = time.time()
    try:
        r = h.open(SCRIBD_TARGET_URL, timeout=8)
        body = r.read(1500)
        # Validasi konten asli Scribd (bukan halaman block/iklan proxy)
        if r.status == 200 and (b"scribd" in body.lower() or b"outer_page" in body or b"docmanager" in body.lower()):
            return max(0.1, round(time.time() - t, 1))
    except Exception:
        pass
    return None


def hunt(data_dir, progress_cb=None):
    """Garap proxy khusus Scribd dengan live progress callback."""
    proxies = gather_proxies()
    if not proxies:
        return None

    total = len(proxies)
    passed_list = []
    checked_count = 0
    t_start = time.time()

    def check_one(p):
        nonlocal checked_count
        lat = test_proxy(p)
        checked_count += 1
        if lat is not None and lat <= 3.5:
            passed_list.append((p, lat))
        if progress_cb and (checked_count % 75 == 0 or checked_count == total):
            try:
                progress_cb(checked_count, total, len(passed_list), time.time() - t_start)
            except Exception:
                pass

    # 250 threads concurrent checking (selesai ~1.5 - 2 menit)
    with ThreadPoolExecutor(250) as ex:
        list(ex.map(check_one, proxies))

    passed_list.sort(key=lambda x: x[1])
    
    out = {
        "ts": time.strftime("%Y-%m-%d %H:%M"),
        "total": total,
        "scribd": passed_list,
        "both": passed_list,  # kompatibilitas schema
        "bai": []
    }

    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "latest.json"), "w") as f:
        json.dump(out, f, indent=2)

    return out


def save_txt(h, data_dir):
    path = os.path.join(data_dir, f"proxy-scribd-{h['ts'].replace(' ','_').replace(':','')}.txt")
    with open(path, "w") as f:
        f.write(f"# Hasil Garap Proxy Khusus Scribd ({h['ts']}) — Total {h['total']} Dicek\n\n")
        f.writelines(f"{p} ({lat}s)\n" for p, lat in h["scribd"])
    return path


def format_summary(h, elapsed_sec=0):
    mins = int(elapsed_sec // 60)
    secs = int(elapsed_sec % 60)
    waktu_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
    
    top_str = "\n".join(f"• <code>{p}</code> ({lat}s)" for p, lat in h["scribd"][:8]) or "—"
    
    return (
        f"✅ <b>Garap Proxy Scribd Selesai!</b>\n"
        f"──────────────────────────\n"
        f"📊 Total Dicek  : <b>{h['total']} IP</b>\n"
        f"⚡ Lolos Scribd : <b>{len(h['scribd'])} Proxy</b> (Latensi ≤3.5s)\n"
        f"⏱ Waktu Garap  : <code>{waktu_str}</code>\n\n"
        f"🏆 <b>Top Proxy Tercepat:</b>\n{top_str}\n\n"
        f"💡 <i>Stok proxy otomatis diperbarui dan langsung siap digunakan.</i>"
    )
