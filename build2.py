#!/usr/bin/env python3
"""
build2.py — Universal game archive extractor
Handles ZIP (with zstd/deflate64) and RAR files directly from URL.

ZIP mode : HTTP range requests → jumps to each file directly, supports --skip-existing
RAR mode : curl | bsdtar streaming pipe (must stream whole archive)


Requirements:
    pip install remotezip tqdm zstandard requests

Build into exe (bundles curl + bsdtar):
    python build.py

Usage:
    extract <url> [output_dir] [options]
"""

import zipfile_deflate64  # Patches zipfile to support Deflate64
import argparse
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


# FORCE Windows to use UTF-8 so the banner and progress bars never crash
if sys.stdout is not None:
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

try:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    sys.exit("[ERROR] requests not installed. Run: pip install requests remotezip tqdm zstandard")

try:
    from tqdm import tqdm
except ImportError:
    sys.exit("[ERROR] tqdm not installed. Run: pip install requests remotezip tqdm zstandard")

try:
    from remotezip import RemoteZip
except ImportError:
    sys.exit("[ERROR] remotezip not installed. Run: pip install requests remotezip tqdm zstandard")

try:
    import zstandard as zstd
except ImportError:
    sys.exit("[ERROR] zstandard not installed. Run: pip install zstandard")







BANNER = """
  ┌──────────────────────────────────────────┐
  │       Universal Archive Extractor         │
  │   ZIP  RAR  TAR  7Z  GZ  BZ2  ZSTD ...   │
  └──────────────────────────────────────────┘"""

COMPRESSION_NAMES = {
    0:  "Stored",
    8:  "Deflate",
    9:  "Deflate64",
    12: "BZip2",
    14: "LZMA",
    93: "Zstandard",
    95: "XZ",
    99: "AES",
}

SESSION = requests.Session()
SESSION.verify = False
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})


# ─── Formatting ───────────────────────────────────────────────────────────────

def fmt_size(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.4f} {u}"
        n /= 1024
    return f"{n:.4f} PB"


def fmt_duration(s: float) -> str:
    if s < 60:
        return f"{s:.1f}s"
    m, s = divmod(int(s), 60)
    return f"{m}m {s}s" if m < 60 else f"{m // 60}h {m % 60}m {s}s"


def box(title: str, rows: list[tuple], width: int = 46) -> None:
    print(f"\n  ┌{'─'*(width+2)}┐")
    print(f"  │  {title:<{width}}│")
    print(f"  ├{'─'*(width+2)}┤")
    for label, value in rows:
        content = f"{label:<14}: {value}"
        print(f"  │  {content:<{width}}│")
    print(f"  └{'─'*(width+2)}┘")


# ─── Tool locator ─────────────────────────────────────────────────────────────

def _find(name: str) -> str | None:
    # 1. Check bundled folder
    base_dir = os.path.dirname(__file__)
    p = Path(base_dir) / name
    if p.exists(): return str(p)

    # 2. Check EXE folder
    exe_dir = os.path.dirname(sys.executable)
    p2 = Path(exe_dir) / name
    if p2.exists(): return str(p2)

    # 3. Fallback to system path
    found = shutil.which(name)
    return found if found else None


def find_curl() -> str | None:
    return _find("curl.exe")


def find_bsdtar() -> str | None:
    return _find("bsdtar.exe")


# ─── Archive type detection ───────────────────────────────────────────────────

def detect_type(url: str) -> str:
    """Detect ZIP vs RAR by magic bytes, fall back to URL extension."""
    try:
        r = SESSION.get(url, stream=True, timeout=15)
        chunk = next(r.iter_content(8), b"")
        r.close()
        if chunk[:4] == b"PK\x03\x04" or chunk[:4] == b"PK\x05\x06":
            return "zip"
        if chunk[:4] == b"Rar!":
            return "rar"
    except Exception:
        pass
    url_l = url.lower().split("?")[0]
    if url_l.endswith(".zip"):  return "zip"
    if url_l.endswith(".rar"):  return "rar"
    if ".tar" in url_l:        return "tar"
    return "unknown"


def get_compressed_size(url: str) -> int:
    try:
        r = SESSION.head(url, allow_redirects=True, timeout=15)
        return int(r.headers.get("content-length", 0))
    except Exception:
        return 0


# ─── ZIP: range-request extraction ───────────────────────────────────────────

_pool_url: str = ""
_tl = threading.local()


def _get_conn() -> RemoteZip:
    if not getattr(_tl, "conn", None):
        _tl.conn = RemoteZip(_pool_url, session=SESSION)
    return _tl.conn


def _reset_conn() -> None:
    _tl.conn = None


def _extract_zstd(entry, dest: Path) -> None:
    """Fetch raw zstd-compressed bytes via range request and decompress."""
    h_start = entry.header_offset
    fetch_end = h_start + 30 + len(entry.filename.encode()) + 512
    r = SESSION.get(_pool_url, headers={"Range": f"bytes={h_start}-{fetch_end}"})
    r.raise_for_status()
    hdr = r.content
    if hdr[:4] != b"PK\x03\x04":
        raise ValueError("Bad local file header")
    fname_len = struct.unpack_from("<H", hdr, 26)[0]
    extra_len = struct.unpack_from("<H", hdr, 28)[0]
    data_start = h_start + 30 + fname_len + extra_len
    data_end   = data_start + entry.compress_size - 1

    r2 = SESSION.get(_pool_url, headers={"Range": f"bytes={data_start}-{data_end}"}, stream=True)
    r2.raise_for_status()
    dctx = zstd.ZstdDecompressor()
    with open(dest, "wb") as f:
        with dctx.stream_reader(r2.raw) as reader:
            shutil.copyfileobj(reader, f)


def _extract_zip_entry(entry, output_dir: Path, flat: bool,
                        retries: int, dir_lock: threading.Lock) -> tuple:
    name = entry.filename
    dest = output_dir / Path(name).name if flat else output_dir / name
    with dir_lock:
        dest.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(retries + 1):
        try:
            # Custom handling for Zstandard (method 93) – must stay
            if entry.compress_type == 93:
                _extract_zstd(entry, dest)
            else:
                # For all other methods (including Deflate64 after patching)
                conn = _get_conn()
                if flat:
                    with conn.open(name) as src, open(dest, "wb") as f:
                        shutil.copyfileobj(src, f)
                else:
                    conn.extract(name, str(output_dir))
            return True, name, entry.file_size
        except Exception as exc:
            _reset_conn()
            if attempt < retries:
                time.sleep(1.5 ** attempt)
            else:
                return False, name, str(exc)


def extract_zip(url: str, output_dir: Path, skip_existing: bool,
                workers: int, retries: int, quiet: bool) -> None:
    global _pool_url
    _pool_url = url

    if not quiet:
        print("\n  Mode    : ZIP (HTTP range requests — jumps directly to each file)")
        print("  Fetching central directory...")

    try:
        with RemoteZip(url, session=SESSION) as z:
            all_entries = z.infolist()
    except Exception as e:
        sys.exit(f"\n  [ERROR] {e}\n")

    entries = [e for e in all_entries if not e.filename.endswith("/")]
    total_bytes = sum(e.file_size for e in entries)

    if not quiet:
        methods = {}
        for e in entries:
            m = COMPRESSION_NAMES.get(e.compress_type, f"method {e.compress_type}")
            methods[m] = methods.get(m, 0) + 1
        method_str = ", ".join(f"{v}× {k}" for k, v in methods.items())
        print(f"\n  Files   : {len(entries):,}")
        print(f"  Size    : {fmt_size(total_bytes)} uncompressed")
        print(f"  Methods : {method_str}")
        print(f"  Output  : {output_dir}")
        if workers > 1:
            print(f"  Workers : {workers}")

    # Skip-existing
    to_get, skipped = [], 0
    for e in entries:
        dest = output_dir / Path(e.filename)
        if skip_existing and dest.exists() and dest.stat().st_size == e.file_size:
            skipped += 1
        else:
            to_get.append(e)

    if not to_get:
        print("\n  All files already exist — nothing to do.\n")
        return

    if not quiet and skipped:
        print(f"  Skipping: {skipped:,} existing files")

    work_bytes = sum(e.file_size for e in to_get)
    outer = tqdm(total=work_bytes, unit="B", unit_scale=True, unit_divisor=1024,
                 desc="  Total", colour="cyan", ncols=80, position=0,
                 leave=True, disable=quiet)
    inner = tqdm(total=len(to_get), unit="file",
                 desc="  Files", colour="magenta", ncols=80, position=1,
                 leave=True, disable=quiet)

    lock, dir_lock = threading.Lock(), threading.Lock()
    stats = {"ok": 0, "fail": 0, "bytes": 0}

    def do(entry):
        if not quiet:
            inner.set_description(f"  {Path(entry.filename).name[:36]:<36}")
        ok, name, result = _extract_zip_entry(entry, output_dir, False, retries, dir_lock)
        with lock:
            if ok:
                stats["ok"] += 1; stats["bytes"] += result
            else:
                stats["fail"] += 1
                tqdm.write(f"  [FAIL] {name}: {result}")
        outer.update(entry.file_size)
        inner.update(1)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(do, to_get))
    elapsed = time.time() - t0
    outer.close(); inner.close()

    speed = stats["bytes"] / elapsed if elapsed > 0 else 0
    box("  Extraction Complete", [
        ("Extracted",  f"{stats['ok']:,} files"),
        ("Skipped",    f"{skipped:,} files (already existed)") if skipped else ("", ""),
        ("Failed",     f"{stats['fail']:,} files") if stats["fail"] else ("", ""),
        ("Total size", fmt_size(stats["bytes"])),
        ("Time",       fmt_duration(elapsed)),
        ("Speed",      f"{fmt_size(int(speed))}/s"),
        ("Saved to",   str(output_dir)[:38]),
    ])


# ─── RAR / TAR / unknown: curl | bsdtar streaming ────────────────────────────


def get_dir_size(path: Path) -> int:
    """
    Recursively calculate the total logical size of files in a directory.
    Handles potential Windows permission/access errors during extraction.
    """
    total = 0
    try:
        if not path.exists():
            return 0
        for entry in os.scandir(path):
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat().st_size
                elif entry.is_dir(follow_symlinks=False):
                    total += get_dir_size(Path(entry.path))
            except (PermissionError, FileNotFoundError):
                continue
    except Exception:
        pass
    return total

def extract_stream(url: str, output_dir: Path, archive_type: str,
                   curl: str, bsdtar: str, skip_existing: bool, quiet: bool) -> None:
    """
    Extracts via a resumable stream with a high-fidelity tqdm progress bar
    tracking real-time compression ratios and predicted final sizes.
    """
    compressed_total = get_compressed_size(url)
    
    if not quiet:
        print(f"\n  Mode    : {archive_type.upper()} (Resumable Stream + Predictive Engine)")
        print(f"  Archive : {fmt_size(compressed_total)} compressed")
        print(f"  Output  : {output_dir}\n")

    # 1. Initialize Extractor
    bsdtar_args = [bsdtar, "-xf", "-", "-C", str(output_dir)]
    if skip_existing: bsdtar_args.insert(1, "-k")
    
    bsdtar_proc = subprocess.Popen(
        bsdtar_args, stdin=subprocess.PIPE, 
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=1024*1024
    )

    # Error listener for bsdtar
    def _bsdtar_err():
        for line in bsdtar_proc.stderr:
            if not quiet: tqdm.write(f"  [bsdtar] {line.decode().strip()}")
    threading.Thread(target=_bsdtar_err, daemon=True).start()

    # 2. Tracking & TQDM Setup
    bytes_received = 0
    last_disk_check_time = 0
    current_disk_size = 0
    last_stable_out = 0
    stable_ratio = 1.0
    t0 = time.time()

    # Create the 'Cool' Progress Bar
    # bar_format gives us that clean, professional look
    pbar = tqdm(
        total=compressed_total,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        colour="cyan",
        desc="  Extracting",
        leave=True,
        disable=quiet,
        bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}{postfix}]"
    )

    # 3. Main Stream Loop
    while bytes_received < compressed_total:
        curl_args = [curl, "-L", "-k", "--http1.1", "-r", f"{bytes_received}-", url]
        curl_proc = subprocess.Popen(curl_args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        
        try:
            while True:
                chunk = curl_proc.stdout.read(1024 * 64)
                if not chunk: break
                
                bsdtar_proc.stdin.write(chunk)
                chunk_len = len(chunk)
                bytes_received += chunk_len
                pbar.update(chunk_len)

                # Predictive Math & Disk Scan
                now = time.time()
                if now - last_disk_check_time > 4: # Check every 4s
                    new_disk_size = get_dir_size(output_dir)
                    if new_disk_size > last_stable_out:
                        stable_ratio = new_disk_size / bytes_received
                        last_stable_out = new_disk_size
                    current_disk_size = new_disk_size
                    last_disk_check_time = now

                    # Update the 'Cool' metrics area
                    est_ratio = max(stable_ratio, 1.0)
                    est_final = compressed_total * est_ratio
                    pbar.set_postfix_str(
                        f"Out: {fmt_size(current_disk_size)} | "
                        f"Ratio: {stable_ratio:.4f}x | "
                        f"Est. Total: {fmt_size(est_final)}",
                        refresh=True
                    )

            curl_proc.wait()
            if curl_proc.returncode == 0: break
            else: raise Exception("Stream Interrupted")

        except Exception:
            # If we crash, pbar stays where it is, curl reconnects and continues
            time.sleep(2)
            continue

    pbar.close()
    
    # 4. Finalize
    try: bsdtar_proc.stdin.close()
    except: pass
    bsdtar_proc.wait()
    
    final_size = get_dir_size(output_dir)
    ok = bsdtar_proc.returncode == 0
    
    box("  Extraction Complete" if ok else "  Finished with errors", [
        ("Status",    "OK" if ok else "ERROR"),
        ("Compressed", fmt_size(bytes_received)),
        ("Extracted",  fmt_size(final_size)),
        ("Final Ratio", f"{(final_size/bytes_received):.4f}x" if bytes_received else "N/A"),
        ("Time",       fmt_duration(time.time() - t0)),
        ("Saved to",   str(output_dir)[:38]),
    ])


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        prog="extract",
        description="Extract ZIP or RAR archives directly from a URL — no temp file needed.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
how it works:
  ZIP  → HTTP range requests jump to each file's offset directly
         Supports zstd (method 93), deflate, stored, bzip2, lzma
         --skip-existing only downloads truly missing files

  RAR  → streams curl | bsdtar pipe (entire archive, binary-safe)
  TAR  → same as RAR

examples:
  extract https://example.com/game.zip  D:\\Games
  extract https://example.com/game.rar  D:\\Games
  extract https://example.com/game.zip  D:\\Games  --skip-existing
  extract https://example.com/game.zip  D:\\Games  --workers 4
  extract https://example.com/game.zip  --info
        """,
    )

    p.add_argument("url",        help="URL of the archive")
    p.add_argument("output_dir", nargs="?", default="./extracted",
                   help="Output directory (default: ./extracted)")
    p.add_argument("--info",           "-i", action="store_true",
                   help="Show archive info without extracting (ZIP only)")
    p.add_argument("--skip-existing",  "-s", action="store_true",
                   help="Skip files that already exist with correct size")
    p.add_argument("--workers",        "-w", metavar="N", type=int, default=1,
                   help="Parallel connections for ZIP (default: 1)")
    p.add_argument("--retry",          metavar="N", type=int, default=2,
                   help="Retries per file for ZIP (default: 2)")
    p.add_argument("--force-zip",      action="store_true",
                   help="Force ZIP mode even if not detected")
    p.add_argument("--force-stream",   action="store_true",
                   help="Force streaming mode (curl|bsdtar) even for ZIP")
    p.add_argument("--quiet",          "-q", action="store_true",
                   help="Suppress progress output")

    args = p.parse_args()

    if not args.quiet:
        print(BANNER)

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Detect archive type
    if not args.quiet:
        print(f"\n  Detecting archive type...")
    archive_type = "zip" if args.force_zip else detect_type(args.url)
    if not args.quiet:
        print(f"  Type    : {archive_type.upper()}")

    # --info (ZIP only)
    if args.info:
        if archive_type != "zip":
            print(f"\n  [WARN] --info only works for ZIP. RAR has no remote index.\n")
            sys.exit(0)
        global _pool_url
        _pool_url = args.url
        try:
            with RemoteZip(args.url, session=SESSION) as z:
                entries = [e for e in z.infolist() if not e.filename.endswith("/")]
        except Exception as e:
            sys.exit(f"\n  [ERROR] {e}\n")

        total_unc  = sum(e.file_size    for e in entries)
        total_comp = sum(e.compress_size for e in entries)
        methods = {}
        for e in entries:
            m = COMPRESSION_NAMES.get(e.compress_type, f"method {e.compress_type}")
            methods[m] = methods.get(m, 0) + 1

        print(f"\n  {'FILE':<60} {'METHOD':<12} {'SIZE':>10}")
        print(f"  {'─'*60} {'─'*12} {'─'*10}")
        for e in entries:
            m = COMPRESSION_NAMES.get(e.compress_type, f"method {e.compress_type}")
            print(f"  {e.filename[:60]:<60} {m:<12} {fmt_size(e.file_size):>10}")

        box("  Archive Info", [
            ("Files",      f"{len(entries):,}"),
            ("Compressed", fmt_size(total_comp)),
            ("Extracted",  fmt_size(total_unc)),
            ("Methods",    ", ".join(f"{v}× {k}" for k, v in methods.items())),
        ])
        return

    # Choose extraction mode
    use_stream = args.force_stream or archive_type in ("rar", "tar", "unknown")

    if use_stream:
        curl   = find_curl()
        bsdtar = find_bsdtar()
        missing = [n for n, v in [("curl", curl), ("bsdtar", bsdtar)] if not v]
        if missing:
            print(f"\n  [ERROR] Not found: {', '.join(missing)}")
            print("  Build the exe with build.py to bundle them automatically.\n")
            sys.exit(1)
        extract_stream(args.url, output_dir, archive_type,
                       curl, bsdtar, args.skip_existing, args.quiet)
    else:
        extract_zip(args.url, output_dir,
                    args.skip_existing, args.workers, args.retry, args.quiet)


if __name__ == "__main__":
    main()
