#!/usr/bin/env python3
"""
Unified game archive extractor
Handles ZIP (with ZIP64, zstd, and deflate) and RAR files directly from URL.

ZIP Normal mode : HTTP range requests -> seeks directly to file offsets, supports parallel workers
ZIP Stream mode : Pure-Python sequential parser with ZIP64 extra block parsing + TQDM output + Resumable Network Stream
RAR/TAR Mode    : curl | bsdtar streaming pipe

Requirements:
    pip install remotezip tqdm zstandard requests zipfile_deflate64
"""

import argparse
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

try:
    import zipfile_deflate64  # Patches standard zipfile to support Deflate64
except ImportError:
    pass

# Force Windows to use UTF-8 so progress output never crashes
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
    sys.exit("[ERROR] tqdm not installed. Run: pip install tqdm")

try:
    from remotezip import RemoteZip
except ImportError:
    sys.exit("[ERROR] remotezip not installed. Run: pip install remotezip")

try:
    import zstandard as zstd
except ImportError:
    zstd = None


BANNER = """
  ┌──────────────────────────────────────────┐
  │       Universal Archive Extractor         │
  │   ZIP  RAR  TAR  7Z  GZ  BZ2  ZSTD ...   │
  └──────────────────────────────────────────┘"""

LOCAL_FILE_HEADER_SIG = b"PK\x03\x04"
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
    base_dir = os.path.dirname(__file__)
    p = Path(base_dir) / name
    if p.exists(): return str(p)

    exe_dir = os.path.dirname(sys.executable)
    p2 = Path(exe_dir) / name
    if p2.exists(): return str(p2)

    found = shutil.which(name)
    return found if found else None


def find_curl() -> str | None:
    return _find("curl.exe")


def find_bsdtar() -> str | None:
    return _find("bsdtar.exe")


# ─── Archive type detection ───────────────────────────────────────────────────

def detect_type(url: str) -> str:
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


# ─── Byte Feeder helper for Sequential ZIP stream ─────────────────────────────

class ByteFeeder:
    def __init__(self, chunk_iter, initial: bytes = b""):
        self._iter = chunk_iter
        self._buf = bytearray(initial)

    def _fill(self, n):
        while len(self._buf) < n:
            try:
                chunk = next(self._iter)
            except StopIteration:
                break
            self._buf += chunk

    def read(self, n):
        self._fill(n)
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data


# ─── Resumable HTTP Stream Generator ──────────────────────────────────────────

def resumable_chunk_generator(url: str, total_size: int, progress_callback, chunk_size: int = 256 * 1024):
    """
    Yields network chunks sequentially. If the TCP connection breaks, 
    it automatically reconnects with an HTTP Range header to resume from the exact byte.
    """
    bytes_downloaded = 0
    max_retries = 9999
    retries_left = max_retries
    backoff = 2
    
    while True:
        if total_size > 0 and bytes_downloaded >= total_size:
            break
            
        try:
            headers = {}
            if bytes_downloaded > 0:
                headers["Range"] = f"bytes={bytes_downloaded}-"
                
            r = SESSION.get(url, stream=True, timeout=30, headers=headers)
            r.raise_for_status()
            
            # Reset exponential backoff on a successful socket connection
            backoff = 2
            retries_left = max_retries
            
            has_data = False
            for chunk in r.iter_content(chunk_size=chunk_size):
                if chunk:
                    has_data = True
                    chunk_len = len(chunk)
                    bytes_downloaded += chunk_len
                    progress_callback(chunk_len)
                    yield chunk
            r.close()
            
            if total_size > 0 and bytes_downloaded >= total_size:
                break
            if not has_data:
                # Naturally reached the EOF of the stream
                break
                
        except Exception as e:
            if total_size > 0 and bytes_downloaded >= total_size:
                break
                
            tqdm.write(f"\n  [WARN] Connection dropped at {fmt_size(bytes_downloaded)}. Reconnecting in {backoff}s... (Error: {e})")
            retries_left -= 1
            if retries_left < 0:
                tqdm.write("  [ERROR] Exceeded maximum connection retries.")
                raise e
                
            time.sleep(backoff)
            backoff = min(30, backoff * 2)


def parse_zip64_extra(extra_data: bytes, uncomp_32: int, comp_32: int) -> tuple[int, int]:
    uncomp_size = uncomp_32
    comp_size = comp_32
    idx = 0
    while idx + 4 <= len(extra_data):
        header_id, block_size = struct.unpack_from("<HH", extra_data, idx)
        idx += 4
        if header_id == 0x0001:  # ZIP64 extra field signature
            local_idx = idx
            if uncomp_32 == 0xFFFFFFFF and local_idx + 8 <= idx + block_size:
                uncomp_size = struct.unpack_from("<Q", extra_data, local_idx)[0]
                local_idx += 8
            if comp_32 == 0xFFFFFFFF and local_idx + 8 <= idx + block_size:
                comp_size = struct.unpack_from("<Q", extra_data, local_idx)[0]
                local_idx += 8
            break
        idx += block_size
    return uncomp_size, comp_size


def make_decompressor(method: int):
    if method == 0:
        class _Passthrough:
            def decompress(self, chunk): return chunk
        return _Passthrough()
    if method == 8:
        return zlib.decompressobj(-15)
    if method == 93:
        if zstd is None:
            raise RuntimeError("Zstandard entry found but 'zstandard' is missing. Run: pip install zstandard")
        return zstd.ZstdDecompressor().decompressobj()
    raise RuntimeError(f"Unsupported compression method: {method}")


# ─── ZIP Mode 1: Pure-Python Sequential ZIP64 Streaming ───────────────────────

def extract_zip_stream(url: str, output_dir: Path, skip_existing: bool, quiet: bool) -> None:
    compressed_total = get_compressed_size(url)
    
    if not quiet:
        print(f"\n  Mode    : ZIP (Pure-Python Sequential Stream + ZIP64 + Auto-Resume)")
        print(f"  Archive : {fmt_size(compressed_total)} compressed")
        print(f"  Output  : {output_dir}\n")

    t0 = time.time()

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

    bytes_downloaded = 0
    def progress_callback(chunk_len):
        nonlocal bytes_downloaded
        bytes_downloaded += chunk_len
        pbar.update(chunk_len)

    # Initialize the resilient chunk generator
    resumable_iter = resumable_chunk_generator(url, compressed_total, progress_callback)
    feeder = ByteFeeder(resumable_iter)

    total_files = 0
    total_bytes_uncompressed = 0
    has_errors = False

    try:
        while True:
            sig = feeder.read(4)
            if not sig or len(sig) < 4:
                break
            if sig != LOCAL_FILE_HEADER_SIG:
                break  # Hit Central Directory or tail records

            header = feeder.read(26)
            if len(header) < 26:
                break
                
            (version, flags, method, mtime, mdate, crc32,
             comp_size, uncomp_size, fname_len, extra_len) = struct.unpack("<HHHHHIIIHH", header)

            if flags & 0x08:
                tqdm.write("  [!] Skipped: Data descriptor present (flag 0x08). Sizes are unknown.")
                sys.exit("\n  [ERROR] Sequential streaming cannot parse ZIPs with streaming data descriptors (flag 0x08) upfront. "
                         "Please run without '--force-stream' to use HTTP Range requests mode.")

            fname_bytes = feeder.read(fname_len)
            fname = fname_bytes.decode("utf-8", errors="replace")
            extra_data = feeder.read(extra_len)

            # Process ZIP64 metadata
            if comp_size == 0xFFFFFFFF or uncomp_size == 0xFFFFFFFF:
                uncomp_size, comp_size = parse_zip64_extra(extra_data, uncomp_size, comp_size)

            dest = output_dir / fname

            if fname.endswith("/"):
                dest.mkdir(parents=True, exist_ok=True)
                continue

            # Skip existing files accurately
            if skip_existing and dest.exists() and dest.stat().st_size == uncomp_size:
                read_so_far = 0
                while read_so_far < comp_size:
                    take = min(256 * 1024, comp_size - read_so_far)
                    feeder.read(take)
                    read_so_far += take
                total_files += 1
                if not quiet:
                    tqdm.write(f"  [SKIPPED] {fname}")
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)

            try:
                decompressor = make_decompressor(method)
            except RuntimeError as e:
                tqdm.write(f"  [FAIL] {fname}: {e}")
                has_errors = True
                read_so_far = 0
                while read_so_far < comp_size:
                    take = min(256 * 1024, comp_size - read_so_far)
                    feeder.read(take)
                    read_so_far += take
                continue

            read_so_far = 0
            written = 0
            READ_CHUNK = 256 * 1024

            try:
                with open(dest, "wb") as f:
                    while read_so_far < comp_size:
                        take = min(READ_CHUNK, comp_size - read_so_far)
                        chunk = feeder.read(take)
                        if not chunk:
                            break
                        read_so_far += len(chunk)
                        
                        out = decompressor.decompress(chunk)
                        if out:
                            f.write(out)
                            written += len(out)

                    if hasattr(decompressor, 'flush'):
                        try:
                            out = decompressor.flush()
                            if out:
                                f.write(out)
                                written += len(out)
                        except Exception:
                            pass
                total_files += 1
                total_bytes_uncompressed += written
            except Exception as e:
                tqdm.write(f"  [WRITE ERROR] {fname}: {e}")
                has_errors = True
                # Skip remaining corrupted payload bytes to maintain stream index synchronization
                remaining = comp_size - read_so_far
                if remaining > 0:
                    feeder.read(remaining)

            # Format metrics area for GUI updates
            ratio = total_bytes_uncompressed / bytes_downloaded if bytes_downloaded > 0 else 1.0
            est_final = compressed_total * ratio
            pbar.set_postfix_str(
                f"Out: {fmt_size(total_bytes_uncompressed)} | "
                f"Ratio: {ratio:.4f}x | "
                f"Est. Total: {fmt_size(est_final)}",
                refresh=True
            )

    except Exception as e:
        tqdm.write(f"  [STREAM ERROR] {e}")
        has_errors = True
    finally:
        pbar.close()

    elapsed = time.time() - t0
    speed = bytes_downloaded / elapsed if elapsed > 0 else 0
    ok = not has_errors

    box("  Extraction Complete" if ok else "  Finished with errors", [
        ("Status", "OK" if ok else "ERROR"),
        ("Files", f"{total_files:,}"),
        ("Compressed", fmt_size(bytes_downloaded)),
        ("Extracted", fmt_size(total_bytes_uncompressed)),
        ("Final Ratio", f"{(total_bytes_uncompressed/bytes_downloaded):.4f}x" if bytes_downloaded else "N/A"),
        ("Time", fmt_duration(elapsed)),
        ("Speed", f"{fmt_size(int(speed))}/s"),
        ("Saved to", str(output_dir)[:38]),
    ])


# ─── ZIP Mode 2: Multi-threaded Range Request Extraction ─────────────────────

_pool_url: str = ""
_tl = threading.local()


def _get_conn() -> RemoteZip:
    if not getattr(_tl, "conn", None):
        _tl.conn = RemoteZip(_pool_url, session=SESSION)
    return _tl.conn


def _reset_conn() -> None:
    _tl.conn = None


def _extract_zstd(entry, dest: Path) -> None:
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
            if entry.compress_type == 93:
                _extract_zstd(entry, dest)
            else:
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


# ─── RAR / TAR Mode: curl | bsdtar stream extraction ─────────────────────────

def get_dir_size(path: Path) -> int:
    total = 0
    try:
        if not path.exists(): return 0
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
    compressed_total = get_compressed_size(url)
    
    if not quiet:
        print(f"\n  Mode    : {archive_type.upper()} (Resumable Stream + Predictive Engine)")
        print(f"  Archive : {fmt_size(compressed_total)} compressed")
        print(f"  Output  : {output_dir}\n")

    bsdtar_args = [bsdtar, "-xf", "-", "-C", str(output_dir)]
    if skip_existing: bsdtar_args.insert(1, "-k")
    
    bsdtar_proc = subprocess.Popen(
        bsdtar_args, stdin=subprocess.PIPE, 
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=1024*1024
    )

    def _bsdtar_err():
        for line in bsdtar_proc.stderr:
            if not quiet: tqdm.write(f"  [bsdtar] {line.decode().strip()}")
    threading.Thread(target=_bsdtar_err, daemon=True).start()

    bytes_received = 0
    last_disk_check_time = 0
    current_disk_size = 0
    last_stable_out = 0
    stable_ratio = 1.0
    t0 = time.time()

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

                now = time.time()
                if now - last_disk_check_time > 4:
                    new_disk_size = get_dir_size(output_dir)
                    if new_disk_size > last_stable_out:
                        stable_ratio = new_disk_size / bytes_received
                        last_stable_out = new_disk_size
                    current_disk_size = new_disk_size
                    last_disk_check_time = now

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
            time.sleep(2)
            continue

    pbar.close()
    
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


# ─── CLI Entrypoint ───────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        prog="extract",
        description="Extract ZIP or RAR archives directly from a URL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
                   help="Force streaming mode even for ZIP")
    p.add_argument("--quiet",          "-q", action="store_true",
                   help="Suppress progress output")

    args = p.parse_args()

    if not args.quiet:
        print(BANNER)

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if not args.quiet:
        print(f"\n  Detecting archive type...")
    archive_type = "zip" if args.force_zip else detect_type(args.url)
    if not args.quiet:
        print(f"  Type    : {archive_type.upper()}")

    # Info Command
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

    use_stream = args.force_stream or archive_type in ("rar", "tar", "unknown")

    if use_stream:
        if archive_type == "zip":
            # Run the sequential ZIP parser with resumable Range capabilities
            extract_zip_stream(args.url, output_dir, args.skip_existing, args.quiet)
        else:
            # Fall back to bsdtar stream reader for RAR / TAR
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
        # Range-request multi-threaded ZIP mode
        extract_zip(args.url, output_dir,
                    args.skip_existing, args.workers, args.retry, args.quiet)


if __name__ == "__main__":
    main()
