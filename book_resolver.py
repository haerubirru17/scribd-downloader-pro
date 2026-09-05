import re, json, urllib.request, urllib.parse, os, time

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def clean_query(raw_input):
    """Ekstrak judul bersih dari URL Google Play Books atau teks pencarian."""
    text = raw_input.strip()
    if "play.google.com" in text or "books.google" in text:
        try:
            req = urllib.request.Request(text, headers={
                "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
                "Accept-Language": "id-ID,id;q=0.9,en;q=0.8"
            })
            with urllib.request.urlopen(req, timeout=8) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
                og_match = re.search(r'<meta property=["\']og:title["\'] content=["\']([^"\']+)["\']', html)
                if og_match:
                    raw_title = og_match.group(1)
                    # Bersihkan suffix Google Play
                    clean_t = re.sub(r'\s*-\s*Buku di Google Play.*', '', raw_title, flags=re.IGNORECASE)
                    clean_t = re.sub(r'\s*-\s*Books on Google Play.*', '', clean_t, flags=re.IGNORECASE)
                    clean_t = re.sub(r'\s*oleh\s+.*', '', clean_t, flags=re.IGNORECASE)
                    clean_t = re.sub(r'\s*by\s+.*', '', clean_t, flags=re.IGNORECASE)
                    if clean_t.strip():
                        return clean_t.strip()
        except Exception:
            pass

    # Bersihkan prefix perintah
    for prefix in ("/buku ", "buku ", "/cari ", "cari "):
        if text.lower().startswith(prefix):
            text = text[len(prefix):].strip()
            break
            
    return text


def search_books(query, limit=5):
    """Cari buku di repository open access dengan fuzzy & strict matching."""
    clean_q = clean_query(query)
    if not clean_q:
        return []

    # Bersihkan karakter spesial untuk pencarian Archive.org
    safe_terms = " ".join(re.findall(r'[a-zA-Z0-9]+', clean_q))
    if not safe_terms:
        safe_terms = clean_q

    safe_q = urllib.parse.quote(safe_terms)
    search_url = (
        f"https://archive.org/advancedsearch.php?"
        f"q=({safe_q})+AND+mediatype:(texts)&"
        f"fl[]=identifier,title,creator,year,publicdate,downloads,item_size&"
        f"sort[]=downloads+desc&rows={limit * 5}&output=json"
    )

    results = []
    seen_identifiers = set()

    try:
        req = urllib.request.Request(search_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            docs = data.get("response", {}).get("docs", [])
            
            for doc in docs:
                identifier = doc.get("identifier")
                doc_title = doc.get("title", "")
                if not identifier or identifier in seen_identifiers:
                    continue
                
                # Fetch metadata files
                try:
                    meta_url = f"https://archive.org/metadata/{identifier}"
                    m_req = urllib.request.Request(meta_url, headers=HEADERS)
                    with urllib.request.urlopen(m_req, timeout=6) as mr:
                        meta = json.loads(mr.read().decode("utf-8"))
                        files = meta.get("files", [])
                        
                        # Prioritaskan PDF, lalu EPUB
                        sorted_files = sorted(
                            files,
                            key=lambda x: (
                                0 if x.get("name", "").lower().endswith(".pdf") else (
                                1 if x.get("name", "").lower().endswith(".epub") else 2)
                            )
                        )

                        for f in sorted_files:
                            fname = f.get("name", "")
                            fsize = int(f.get("size", 0))
                            ext = fname.split(".")[-1].lower()
                            
                            # Filter hanya file buku valid
                            if ext in ("pdf", "epub") and fsize > 150000:  # > 150KB
                                check_str = f"{identifier} {fname} {doc_title}".lower()
                                
                                # Filter entri sampah/mislabeled jika mencari buku non-grammar
                                if "grammar" in check_str and "grammar" not in clean_q.lower():
                                    continue
                                if identifier.startswith("016-") and "grammar" in check_str:
                                    continue
                                
                                dl_url = f"https://archive.org/download/{identifier}/{urllib.parse.quote(fname)}"
                                size_mb = round(fsize / 1048576, 1)
                                
                                title_display = doc_title or fname.replace(f".{ext}", "")
                                creator = doc.get("creator") or meta.get("metadata", {}).get("creator", "Penulis / Open Access")
                                year = doc.get("year") or meta.get("metadata", {}).get("year", "-")
                                
                                results.append({
                                    "title": title_display,
                                    "creator": creator,
                                    "year": year,
                                    "format": ext.upper(),
                                    "size_mb": size_mb,
                                    "download_url": dl_url,
                                    "filename": fname
                                })
                                seen_identifiers.add(identifier)
                                break
                except Exception:
                    continue
                
                if len(results) >= limit:
                    break
    except Exception as e:
        print(f"Error searching archive.org: {e}")

    return results


def download_book_stream(download_url, output_path, progress_cb=None):
    """Download direct stream chunk by chunk."""
    req = urllib.request.Request(download_url, headers=HEADERS)
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=30) as r:
        total_bytes = int(r.headers.get("Content-Length", 0))
        downloaded = 0
        chunk_size = 1024 * 512  # 512 KB
        
        with open(output_path, "wb") as f:
            while True:
                chunk = r.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb and total_bytes > 0:
                    pct = int(downloaded * 100 / total_bytes)
                    progress_cb(downloaded, total_bytes, pct, time.time() - t0)

    return os.path.exists(output_path) and os.path.getsize(output_path) > 0


if __name__ == "__main__":
    # Self-test unit
    print("Testing Book Resolver...")
    res = search_books("Filosofi Teras", limit=2)
    print(f"Found {len(res)} results:")
    for b in res:
        print(f"• [{b['format']}] {b['title']} - {b['creator']} ({b['size_mb']} MB)")
        print(f"  URL: {b['download_url'][:80]}...")
    assert len(res) > 0, "No results returned"
    print("ALL TESTS PASSED.")
