#!/usr/bin/env python3
"""
Unified game archive extractor
Upgraded Dual-Engine Direct Streaming Extractor:
 - ZIP / ZIP64 : "Index-First" Sequential Stream Engine (Zero desync, Flag 0x08 immune + .akg_resume)
 - RAR / TAR   : C-Speed Native Streaming Pipe (curl --http1.1 | bsdtar)

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

# Force Windows to use UTF-8 so progress output never crashes
if sys.stdout is not None:
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

try:
    import zipfile_deflate64
except ImportError:
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
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})


# ─── Formatting & Path Safety ─────────────────────────────────────────────────

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


def safe_path(p: Path) -> Path:
    """ Bypasses Windows 260-character MAX_PATH limit so deep game files are never skipped """
    if os.name == "nt":
        p_abs = str(p.resolve())
        if not p_abs.startswith("\\\\?\\"):
            return Path("\\\\?\\" + p_abs)
    return p


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

    tools_dir = Path(base_dir) / "Tools used" / name
    if tools_dir.exists(): return str(tools_dir)

    exe_dir = os.path.dirname(sys.executable)
    p2 = Path(exe_dir) / name
    if p2.exists(): return str(p2)

    found = shutil.which(name)
    return found if found else None


def find_curl() -> str | None:
    return _find("curl.exe") or _find("curl")


def find_bsdtar() -> str | None:
    return _find("bsdtar.exe") or _find("bsdtar")


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


# ─── Resilient Network Stream Feeder ──────────────────────────────────────────

class ResilientStreamReader:
    """
    Feeds bytes sequentially from a single HTTP stream.
    Automatically reconnects with HTTP Range headers if proxy drops connection midway.
    """
    def __init__(self, url: str, total_size: int, start_offset: int = 0, pbar=None):
        self.url = url
        self.total_size = total_size
        self.bytes_read = start_offset
        self.pbar = pbar
        self._buf = bytearray()
        self._resp = None
        self._iter = None
        self._connect()

    def _connect(self):
        retries = 9999
        backoff = 2
        while retries > 0:
            try:
                headers = {}
                if self.bytes_read > 0:
                    headers["Range"] = f"bytes={self.bytes_read}-"
                self._resp = SESSION.get(self.url, stream=True, timeout=30, headers=headers)
                self._resp.raise_for_status()
                self._iter = self._resp.iter_content(chunk_size=128 * 1024)
                return
            except Exception as e:
                tqdm.write(f"\n  [WARN] Connection dropped at {fmt_size(self.bytes_read)}. Reconnecting in {backoff}s... ({e})")
                time.sleep(backoff)
                backoff = min(30, backoff * 2)
                retries -= 1
        raise ConnectionError("Exceeded maximum connection retries.")

    def read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            try:
                chunk = next(self._iter)
                if not chunk:
                    raise StopIteration
                self._buf += chunk
                chunk_len = len(chunk)
                self.bytes_read += chunk_len
                if self.pbar:
                    self.pbar.update(chunk_len)
            except (StopIteration, requests.exceptions.RequestException):
                if self.total_size and self.bytes_read >= self.total_size:
                    break
                self._connect()

        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def skip_bytes(self, n: int):
        left = n
        while left > 0:
            take = min(256 * 1024, left)
            chunk = self.read_exact(take)
            if not chunk:
                break
            left -= len(chunk)


# ─── ZIP Mode: Index-First Sequential Stream (With .akg_resume) ───────────────

def extract_zip_index_stream(url: str, output_dir: Path, skip_existing: bool, quiet: bool) -> None:
    compressed_total = get_compressed_size(url)
    resume_file = output_dir / ".akg_resume"
    
    start_offset = 0
    total_bytes_uncompressed = 0
    start_entry_idx = 0

    if not quiet:
        print(f"\n  Mode    : ZIP (Index-First Sequential Stream + State-Tracking Resume)")
        print(f"  [*] Reading master Central Directory index upfront...")

    try:
        with RemoteZip(url, session=SESSION) as rz:
            raw_entries = rz.infolist()
            # Sort files in strict physical order as stored on disk
            entries = sorted(raw_entries, key=lambda e: e.header_offset)
    except Exception as e:
        sys.exit(f"\n  [ERROR] Failed to fetch ZIP index: {e}\n")

    # Load previous stream state if resuming
    if skip_existing and resume_file.exists():
        try:
            with open(resume_file, "r") as rf:
                data = rf.read().strip()
                if "," in data:
                    parts = data.split(",")
                    saved_offset = int(parts[0])
                    total_bytes_uncompressed = int(parts[1])
                else:
                    saved_offset = int(data)
            
            # Snap to the exact file header where we left off
            for idx, e in enumerate(entries):
                if e.header_offset >= saved_offset:
                    start_entry_idx = idx
                    start_offset = e.header_offset
                    break

            if not quiet and start_offset > 0:
                tqdm.write(f"  [*] Resuming directly from file #{start_entry_idx+1} ({fmt_size(start_offset)} saved)...")
        except Exception:
            start_offset = 0
            total_bytes_uncompressed = 0
            start_entry_idx = 0

    files_to_get = [e for e in entries if not e.filename.endswith("/")]
    total_uncompressed = sum(e.file_size for e in files_to_get)

    if not quiet:
        print(f"  Archive : {fmt_size(compressed_total)} compressed")
        print(f"  Files   : {len(files_to_get):,} files ({fmt_size(total_uncompressed)} uncompressed)")
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

    if start_offset > 0:
        pbar.update(start_offset)

    reader = ResilientStreamReader(url, compressed_total, start_offset=start_offset, pbar=pbar)
    current_stream_pos = start_offset
    total_files = start_entry_idx
    has_errors = False

    try:
        # Iterate from the unextracted files onwards
        for i in range(start_entry_idx, len(entries)):
            entry = entries[i]
            fname = entry.filename
            dest = safe_path(output_dir / fname)

            # Directory entry
            if fname.endswith("/"):
                dest.mkdir(parents=True, exist_ok=True)
                continue

            # 1. Skip gap/padding/descriptors between previous file and this file's header
            if current_stream_pos < entry.header_offset:
                skip_len = entry.header_offset - current_stream_pos
                reader.skip_bytes(skip_len)
                current_stream_pos += skip_len

            # 2. Read the 30-byte local header
            hdr = reader.read_exact(30)
            current_stream_pos += 30
            if hdr[:4] != LOCAL_FILE_HEADER_SIG:
                tqdm.write(f"  [WARN] Header offset shift at {fname}, resynchronizing...")

            fname_len, extra_len = struct.unpack_from("<HH", hdr, 26)

            # 3. Skip local filename and extra fields
            if (fname_len + extra_len) > 0:
                reader.skip_bytes(fname_len + extra_len)
                current_stream_pos += (fname_len + extra_len)

            # Canonical sizes from Central Directory (bypasses all Flag 0x08 bugs!)
            comp_size = entry.compress_size
            uncomp_size = entry.file_size
            method = entry.compress_type

            # Skip existing files accurately
            if skip_existing and dest.exists() and dest.stat().st_size == uncomp_size:
                reader.skip_bytes(comp_size)
                current_stream_pos += comp_size
                total_files += 1
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)

            # Decompressor setup
            if method == 0:
                decompressor = None
            elif method == 8:
                decompressor = zlib.decompressobj(-15)
            elif method == 93:
                if zstd is None:
                    raise RuntimeError("Zstandard required. Run: pip install zstandard")
                decompressor = zstd.ZstdDecompressor().decompressobj()
            else:
                tqdm.write(f"  [FAIL] {fname}: Unsupported compression method {method}")
                reader.skip_bytes(comp_size)
                current_stream_pos += comp_size
                has_errors = True
                continue

            bytes_left = comp_size
            written = 0

            with open(dest, "wb") as f:
                while bytes_left > 0:
                    take = min(256 * 1024, bytes_left)
                    chunk = reader.read_exact(take)
                    if not chunk:
                        break
                    current_stream_pos += len(chunk)
                    bytes_left -= len(chunk)

                    if decompressor:
                        out = decompressor.decompress(chunk)
                        if out:
                            f.write(out)
                            written += len(out)
                    else:
                        f.write(chunk)
                        written += len(chunk)

                if decompressor and hasattr(decompressor, "flush"):
                    out = decompressor.flush()
                    if out:
                        f.write(out)
                        written += len(out)

            total_files += 1
            total_bytes_uncompressed += written

            # Save state to .akg_resume
            try:
                next_offset = entries[i+1].header_offset if (i + 1 < len(entries)) else current_stream_pos
                with open(resume_file, "w") as rf:
                    rf.write(f"{next_offset},{total_bytes_uncompressed}")
            except Exception:
                pass

            # Update metrics matching gui.py regex _RE_POST
            ratio = total_bytes_uncompressed / current_stream_pos if current_stream_pos > 0 else 1.0
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
    speed = (current_stream_pos - start_offset) / elapsed if elapsed > 0 else 0
    ok = not has_errors

    # Clean up the state tracking file on successful exit
    if ok and resume_file.exists():
        try:
            resume_file.unlink()
        except Exception:
            pass

    box("  Extraction Complete" if ok else "  Finished with errors", [
        ("Status", "OK" if ok else "ERROR"),
        ("Files", f"{total_files:,}"),
        ("Compressed", fmt_size(current_stream_pos)),
        ("Extracted", fmt_size(total_bytes_uncompressed)),
        ("Final Ratio", f"{(total_bytes_uncompressed/current_stream_pos):.4f}x" if current_stream_pos else "N/A"),
        ("Time", fmt_duration(elapsed)),
        ("Speed", f"{fmt_size(int(speed))}/s"),
        ("Saved to", str(output_dir)[:38]),
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

    output_dir.mkdir(parents=True, exist_ok=True)

    bsdtar_args = [bsdtar, "-xf", "-", "-C", str(output_dir)]
    if skip_existing: bsdtar_args.insert(1, "-k")
    
    bsdtar_proc = subprocess.Popen(
        bsdtar_args, stdin=subprocess.PIPE, 
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=1024*1024
    )

    def _bsdtar_err():
        for line in bsdtar_proc.stderr:
            if not quiet: tqdm.write(f"  [bsdtar] {line.decode('utf-8', errors='replace').strip()}")
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
        curl_args = [
            curl, "-L", "-k", "--http1.1", "-#",
            "--keepalive-time", "5",
            "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "-r", f"{bytes_received}-",
            url
        ]
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
    elapsed = time.time() - t0
    speed = bytes_received / elapsed if elapsed > 0 else 0
    
    box("  Extraction Complete" if ok else "  Finished with errors", [
        ("Status",     "OK" if ok else "ERROR"),
        ("Compressed", fmt_size(bytes_received)),
        ("Extracted",  fmt_size(final_size)),
        ("Final Ratio", f"{(final_size/bytes_received):.4f}x" if bytes_received else "N/A"),
        ("Time",       fmt_duration(elapsed)),
        ("Speed",      f"{fmt_size(int(speed))}/s"),
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

    # Route Engines
    if archive_type == "zip":
        # Index-First Sequential Streamer (Never desyncs, immune to Flag 0x08 & FastFiles + .akg_resume)
        extract_zip_index_stream(args.url, output_dir, args.skip_existing, args.quiet)
    else:
        # Fall back to native C stream reader (curl | bsdtar) for RAR / TAR
        curl   = find_curl()
        bsdtar = find_bsdtar()
        missing = [n for n, v in [("curl", curl), ("bsdtar", bsdtar)] if not v]
        if missing:
            print(f"\n  [ERROR] Not found: {', '.join(missing)}")
            print("  Build the exe with build.py to bundle them automatically.\n")
            sys.exit(1)
        extract_stream(args.url, output_dir, archive_type,
                       curl, bsdtar, args.skip_existing, args.quiet)


if __name__ == "__main__":
    main()