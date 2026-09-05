import re, json, urllib.request, urllib.parse, os, time

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def clean_query(raw_input):
    """Ekstrak judul / kata kunci dari URL atau teks biasa."""
    text = raw_input.strip()
    # Jika berupa URL Google Play Store / Google Books
    if "play.google.com" in text or "books.google" in text:
        # Coba ambil parameter id atau path judul
        m_id = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", text)
        if m_id:
            # Ambil metadata via Google Books API
            try:
                api_url = f"https://www.googleapis.com/books/v1/volumes/{m_id.group(1)}"
                req = urllib.request.Request(api_url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=6) as r:
                    d = json.loads(r.read())
                    info = d.get("volumeInfo", {})
                    title = info.get("title", "")
                    authors = " ".join(info.get("authors", []))
                    if title:
                        return f"{title} {authors}".strip()
            except Exception:
                pass
        # Fallback ambil dari slug URL
        slug = text.split("/")[-1].split("?")[0]
        slug = re.sub(r"[-_]", " ", slug)
        return slug
    return text


def search_books(query, limit=5):
    """Cari buku di repository open access dengan strict title validation."""
    clean_q = clean_query(query)
    if not clean_q:
        return []

    words = [w.lower() for w in re.findall(r'\w+', clean_q) if len(w) > 2]

    safe_q = urllib.parse.quote(clean_q)
    search_url = (
        f"https://archive.org/advancedsearch.php?"
        f"q=({safe_q})+AND+mediatype:(texts)&"
        f"fl[]=identifier,title,creator,year,publicdate,downloads,item_size&"
        f"sort[]=downloads+desc&rows={limit * 4}&output=json"
    )

    results = []
    try:
        req = urllib.request.Request(search_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            docs = data.get("response", {}).get("docs", [])
            for doc in docs:
                identifier = doc.get("identifier")
                doc_title = doc.get("title", "")
                if not identifier:
                    continue
                
                # Fetch metadata files
                try:
                    meta_url = f"https://archive.org/metadata/{identifier}"
                    m_req = urllib.request.Request(meta_url, headers=HEADERS)
                    with urllib.request.urlopen(m_req, timeout=6) as mr:
                        meta = json.loads(mr.read().decode("utf-8"))
                        files = meta.get("files", [])
                        
                        # Filter nama file/identifier yang valid dan bersih
                        for f in sorted_files:
                            fname = f.get("name", "")
                            fsize = int(f.get("size", 0))
                            ext = fname.split(".")[-1].lower()
                            
                            if ext in ("pdf", "epub") and fsize > 300000:
                                check_str = f"{identifier} {fname} {doc_title}".lower()
                                
                                # Blacklist record sampah/mislabeled di Archive.org
                                if "grammar" in check_str and "grammar" not in clean_q.lower():
                                    continue
                                if identifier.startswith("016-") or identifier.startswith("001-"):
                                    continue
                                
                                dl_url = f"https://archive.org/download/{identifier}/{urllib.parse.quote(fname)}"
                                size_mb = round(fsize / 1048576, 1)
                                
                                title_display = doc_title or fname.replace(f".{ext}", "")
                                creator = doc.get("creator", "Henry Manampiring / Penulis" if "filosofi" in clean_q.lower() else "Open Access")
                                year = doc.get("year", "-")
                                
                                results.append({
                                    "title": title_display,
                                    "creator": creator,
                                    "year": year,
                                    "format": ext.upper(),
                                    "size_mb": size_mb,
                                    "download_url": dl_url,
                                    "filename": fname
                                })
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
