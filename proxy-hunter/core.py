"""Core proxy-hunter: gather free proxies, test against Scribd WAF & chat.b.ai CF."""
import re, json, time, urllib.request

SOURCES = [
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
]
# sukses = kondisi yang GAGAL dari IP VPS langsung (itulah kenapa butuh proxy)
TARGETS = {
    "scribd": ("https://www.scribd.com/embeds/888163707/content",
               lambda code, body: code == 200),
    "bai":    ("https://chat.b.ai/api/auth/providers",
               lambda code, body: code == 200 and b"tronlink" in body),
}


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


def test_proxy(proxy, target):
    url, ok = TARGETS[target]
    h = urllib.request.build_opener(urllib.request.ProxyHandler(
        {"http": f"http://{proxy}", "https": f"http://{proxy}"}))
    h.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0")]
    t = time.time()
    try:
        r = h.open(url, timeout=10)
        body = r.read(400)
        if ok(r.status, body):
            return round(time.time() - t, 1)
    except Exception:
        pass
    return None


def hunt(data_dir):
    """Garap semua sumber. Simpan latest.json + txt di data_dir. Return dict hasil."""
    proxies = gather_proxies()
    if not proxies:
        return None
    from concurrent.futures import ThreadPoolExecutor
    lat = {}  # ponytail: cache per (target,proxy) — hasil /cari dipakai ulang tanpa dobel koneksi

    def t_scribd(p): lat[("s", p)] = test_proxy(p, "scribd")
    def t_bai(p):    lat[("b", p)] = test_proxy(p, "bai")
    # ponytail: pool 200 sync — ~6k proxy selesai <6 menit; per-target pool kalau butuh kecepatan
    with ThreadPoolExecutor(200) as ex:
        list(ex.map(t_scribd, proxies))
        list(ex.map(t_bai, proxies))

    res = {"both": [], "bai": [], "scribd": []}
    for p in proxies:
        s, b = lat.get(("s", p)), lat.get(("b", p))
        if s and b: res["both"].append((p, max(s, b)))
        elif b:     res["bai"].append((p, b))
        elif s:     res["scribd"].append((p, s))
    for k in res:
        res[k].sort(key=lambda x: x[1])
    out = {"ts": time.strftime("%Y-%m-%d %H:%M"), "total": len(proxies),
           "both": res["both"], "bai": res["bai"], "scribd": res["scribd"]}
    import os
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "latest.json"), "w") as f:
        json.dump(out, f)
    return out


def save_txt(h, data_dir):
    path = os.path.join(data_dir, f"proxy-lolos-{h['ts'].replace(' ','_').replace(':','')}.txt")
    with open(path, "w") as f:
        f.write(f"# Hasil garap {h['ts']} — total {h['total']} dicek\n")
        f.write("\n[LOLOS SEMUA TARGET - scribd + b.ai]\n")
        f.writelines(f"{p}\n" for p, _ in h["both"])
        f.write("\n[B.AI SAJA]\n");  f.writelines(f"{p}\n" for p, _ in h["bai"])
        f.write("\n[SCRIBD SAJA]\n"); f.writelines(f"{p}\n" for p, _ in h["scribd"])
    return path


def format_summary(h):
    b = "\n".join(f"<code>{p}</code> ({l}s)" for p, l in h["both"][:10]) or "—"
    t = "\n".join(f"<code>{p}</code>" for p, _ in h["bai"][:5]) or "—"
    return (f"🎯 <b>Hasil Garap Proxy</b>\n"
            f"Total dicek: <b>{h['total']}</b>\n"
            f"✅ Lolos dua-duanya: <b>{len(h['both'])}</b>\n"
            f"🌐 Lolos B.ai saja: {len(h['bai'])} | 📄 Scribd saja: {len(h['scribd'])}\n\n"
            f"<b>Top proxy (semua target):</b>\n{b}\n\n"
            f"🤖 <b>B.ai saja:</b>\n{t}\n\n"
            f"File lengkap terlampir 📎\nWaktu garap: {h['ts']} WIB-srv")


if __name__ == "__main__":
    # self-check: garap kecil & verify hasil tersimpan
    import sys, os
    d = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ph-check"
    h = hunt(d)
    assert h and h["total"] > 100, "gather gagal"
    assert os.path.exists(os.path.join(d, "latest.json")), "latest.json tidak tersimpan"
    print(f"OK total={h['total']} both={len(h['both'])} bai={len(h['bai'])} scribd={len(h['scribd'])}")
