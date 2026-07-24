#!/usr/bin/env python3
import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
import subprocess
import threading
import queue
import os
import sys
import re
from pathlib import Path

# ─── Theme & Palette ──────────────────────────────────────────────────────────

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

C = {
    "bg":          "#08090d",  # Deep void background
    "panel":       "#0f121d",  # Dark glass panel
    "card":        "#151928",  # Elevated card surface
    "raised":      "#1e2438",  # Input field surface
    "border":      "#272d47",  # Container border
    "cyan":        "#00f0ff",  # Speed & primary accent
    "cyan_hover":  "#00c8d6",
    "purple":      "#8b5cf6",  # Ratio & secondary accents
    "purple_hover":"#7c3aed",
    "emerald":     "#10b981",  # Disk / Success accent
    "rose":        "#f43f5e",  # Stop button & errors
    "amber":       "#f59e0b",  # ETA & warning color
    "text":        "#f1f5f9",  # Crisp bright white text
    "sub":         "#94a3b8",  # Muted labels
    "muted":       "#64748b",  # Secondary labels
    "dim":         "#1e293b",  # Progress bar track
}

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _kill_process_tree(pid: int):
    """ Recursively terminates a process and all child processes (curl.exe, bsdtar.exe) """
    if sys.platform == "win32":
        try:
            import psutil
            parent = psutil.Process(pid)
            for child in parent.children(recursive=True):
                try: child.kill()
                except Exception: pass
            parent.kill()
            return
        except ImportError:
            pass

        # Fallback for Windows process tree kill without popups
        try:
            subprocess.call(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
            )
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(pid), 9)
        except Exception:
            pass

def get_resource_path(relative_path):
    """ Finds bundled files in Nuitka Onefile / standalone """
    base_path = os.path.dirname(__file__)
    return os.path.join(base_path, relative_path)

def get_icon_path(relative_path):
    return get_resource_path(relative_path)

def find_exe() -> str:
    exe_name = "akgxtract.exe" if sys.platform == "win32" else "extract"
    bundled = get_resource_path(os.path.join("backend", exe_name))
    if os.path.exists(bundled):
        return bundled
    return exe_name

def _popen_kwargs() -> dict:
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kw

def strip_ansi(text: str) -> str:
    return re.sub(r'\x1b\[[0-9;]*[mK]', '', text)

# ─── Custom UI Components ─────────────────────────────────────────────────────

class SectionHeader(ctk.CTkFrame):
    def __init__(self, parent, title: str, **kw):
        super().__init__(parent, fg_color="transparent", **kw)
        ctk.CTkLabel(self, text=title, font=("Segoe UI", 11, "bold"),
                     text_color=C["sub"]).pack(side="left")
        ctk.CTkFrame(self, height=1, fg_color=C["border"]).pack(
            side="left", fill="x", expand=True, padx=(10, 0), pady=1)

class StatCard(ctk.CTkFrame):
    def __init__(self, parent, title: str, accent_color: str, **kw):
        super().__init__(parent, fg_color=C["card"], corner_radius=10,
                          border_width=1, border_color=C["border"], **kw)
        self._accent = accent_color
        ctk.CTkLabel(self, text=title, font=("Segoe UI", 10, "bold"),
                     text_color=C["muted"]).pack(anchor="w", pady=(8, 0), padx=12)
        self._val = ctk.CTkLabel(self, text="—", font=("Consolas", 18, "bold"),
                                  text_color=self._accent)
        self._val.pack(anchor="w", pady=(2, 8), padx=12)

    def set(self, text: str, color: str = None):
        self._val.configure(text=text, text_color=color or self._accent)

class StyledProgress(ctk.CTkFrame):
    def __init__(self, parent, title: str, color: str = None, **kw):
        super().__init__(parent, fg_color="transparent", **kw)
        self._color = color or C["cyan"]

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x")
        self._lbl = ctk.CTkLabel(top, text=title, font=("Segoe UI", 12, "bold"),
                                  text_color=C["text"], anchor="w")
        self._lbl.pack(side="left")
        self._pct = ctk.CTkLabel(top, text="0%", font=("Consolas", 14, "bold"),
                                  text_color=self._color, anchor="e")
        self._pct.pack(side="right")

        self._bar = ctk.CTkProgressBar(self, height=10, corner_radius=5,
                                        fg_color=C["dim"], progress_color=self._color)
        self._bar.pack(fill="x", pady=(4, 4))
        self._bar.set(0)

        self._detail = ctk.CTkLabel(self, text="Idle", font=("Consolas", 11),
                                     text_color=C["sub"], anchor="w")
        self._detail.pack(fill="x")

    def update(self, pct: float, detail: str = "", title: str = ""):
        pct = max(0.0, min(1.0, pct))
        self._bar.set(pct)
        self._pct.configure(text=f"{int(pct * 100)}%")
        if detail: self._detail.configure(text=detail)
        if title:  self._lbl.configure(text=title)

    def reset(self):
        self._bar.set(0)
        self._pct.configure(text="0%")
        self._detail.configure(text="Idle")

class Spinner(ctk.CTkLabel):
    _FRAMES = ["◐", "◓", "◑", "◒"]
    def __init__(self, parent, **kw):
        super().__init__(parent, text="", font=("Consolas", 18),
                         text_color=C["cyan"], **kw)
        self._idx = 0
        self._active = False
    def start(self): self._active = True; self._tick()
    def stop(self, final: str = ""): self._active = False; self.configure(text=final)
    def _tick(self):
        if not self._active: return
        self.configure(text=self._FRAMES[self._idx % 4])
        self._idx += 1
        self.after(120, self._tick)

# ─── Main GUI Application ─────────────────────────────────────────────────────

class ExtractGUI(ctk.CTk):
    _RE_TQDM = re.compile(r"(Total|Files|Extracting)[:\s]+(\d+)%.*?([\d\.]+\s*\w+)\s*/\s*([\d\.]+\s*\w+).*?\[([^\]]+)\]")
    _RE_POST = re.compile(r"Out:\s*([^\|]+)\s*\|\s*Ratio:\s*([^\|]+)\s*\|\s*Est\. Total:\s*(.+)")
    _RE_BOX  = re.compile(r"│\s{2}([^:│]+?)\s*:\s*([^│\n]+?)\s*│")
    _RE_RATE = re.compile(r"([\d\.]+\s*[KMGT]?B/s)")
    _RE_ETA  = re.compile(r"\d+:\d+<(\d{2}:\d{2})")

    def __init__(self):
        super().__init__()
        self.title("Universal Archive Extractor")
        self.geometry("1020x670")
        self.resizable(True, True)
        self.minsize(950, 600)
        self.configure(fg_color=C["bg"])

        self._exe     = find_exe()
        self._proc    = None
        self._q       = queue.Queue()
        self._running = False

        self._build_ui()
        self.after(100, self._poll)

        icon_path = get_icon_path("downloader_icon.ico")
        if os.path.exists(icon_path):
            try: self.iconbitmap(icon_path)
            except Exception: pass

    def _build_ui(self):
        # ── Header Bar ────────────────────────────────────────────────────────
        header = ctk.CTkFrame(self, fg_color=C["panel"], corner_radius=0, height=60)
        header.pack(fill="x")
        header.pack_propagate(False)

        ctk.CTkLabel(header, text="⬡", font=("Segoe UI", 24, "bold"), text_color=C["cyan"]).pack(side="left", padx=(20, 10))
        ctk.CTkLabel(header, text="UNIVERSAL EXTRACTOR", font=("Segoe UI", 16, "bold"), text_color=C["text"]).pack(side="left")
        ctk.CTkLabel(header, text="v1.1.3.0", font=("Segoe UI", 10, "bold"), fg_color=C["raised"], text_color=C["sub"], corner_radius=6, padx=8, pady=2).pack(side="left", padx=12)

        self._spinner = Spinner(header)
        self._spinner.pack(side="right", padx=20)
        self._mode_pill = ctk.CTkLabel(header, text="READY", font=("Segoe UI", 11, "bold"), fg_color=C["card"], text_color=C["sub"], corner_radius=6, padx=10, pady=4)
        self._mode_pill.pack(side="right", padx=10)

        # ── Main 2-Column Grid Layout ─────────────────────────────────────────
        main_grid = ctk.CTkFrame(self, fg_color="transparent")
        main_grid.pack(fill="both", expand=True, padx=20, pady=15)
        main_grid.columnconfigure(0, weight=4) # Left Column (Inputs)
        main_grid.columnconfigure(1, weight=5) # Right Column (Dashboard)
        main_grid.rowconfigure(0, weight=1)

        # ── LEFT PANEL: CONFIGURATION & INPUTS ────────────────────────────────
        left_panel = ctk.CTkFrame(main_grid, fg_color="transparent")
        left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        # 1. Source URL
        SectionHeader(left_panel, "SOURCE ARCHIVE URL").pack(fill="x", pady=(0, 8))
        src_card = ctk.CTkFrame(left_panel, fg_color=C["panel"], corner_radius=10, border_width=1, border_color=C["border"])
        src_card.pack(fill="x", pady=(0, 15))

        url_row = ctk.CTkFrame(src_card, fg_color="transparent")
        url_row.pack(fill="x", padx=12, pady=12)
        self._url = tk.StringVar()
        ctk.CTkEntry(url_row, textvariable=self._url, placeholder_text="Paste direct download URL (ZIP, RAR, TAR, 7Z...)",
                       height=36, font=("Segoe UI", 12), fg_color=C["raised"], border_color=C["border"]).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(url_row, text="Paste", width=65, height=36, font=("Segoe UI", 12, "bold"),
                      fg_color=C["raised"], hover_color=C["card"], text_color=C["cyan"], command=self._paste).pack(side="right")

        # 2. Destination Folder
        SectionHeader(left_panel, "TARGET DESTINATION").pack(fill="x", pady=(0, 8))
        dest_card = ctk.CTkFrame(left_panel, fg_color=C["panel"], corner_radius=10, border_width=1, border_color=C["border"])
        dest_card.pack(fill="x", pady=(0, 15))

        dest_row = ctk.CTkFrame(dest_card, fg_color="transparent")
        dest_row.pack(fill="x", padx=12, pady=12)
        self._out = tk.StringVar(value=str(Path.cwd() / "extracted"))
        ctk.CTkEntry(dest_row, textvariable=self._out, height=36, font=("Segoe UI", 12),
                       fg_color=C["raised"], border_color=C["border"]).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(dest_row, text="Browse", width=65, height=36, font=("Segoe UI", 12, "bold"),
                      fg_color=C["purple"], hover_color=C["purple_hover"], command=self._browse).pack(side="right")

        # 3. Extraction Options
        SectionHeader(left_panel, "EXTRACTION MODE & OPTIONS").pack(fill="x", pady=(0, 8))
        opt_card = ctk.CTkFrame(left_panel, fg_color=C["panel"], corner_radius=10, border_width=1, border_color=C["border"])
        opt_card.pack(fill="x", pady=(0, 15))

        # Checkboxes (Force Stream Checked by default!)
        self._fstrm = tk.BooleanVar(value=True)  # DEFAULT TRUE!
        self._skip  = tk.BooleanVar(value=True)  # DEFAULT TRUE!
        self._fzip  = tk.BooleanVar(value=False)
        self._info  = tk.BooleanVar(value=False)

        chk_grid = ctk.CTkFrame(opt_card, fg_color="transparent")
        chk_grid.pack(fill="x", padx=12, pady=10)

        ctk.CTkCheckBox(chk_grid, text="Force Stream Mode (curl | bsdtar)", variable=self._fstrm,
                        command=self._toggle_workers_state, font=("Segoe UI", 12, "bold"),
                        text_color=C["cyan"], fg_color=C["cyan"], checkmark_color=C["bg"]).grid(row=0, column=0, columnspan=2, sticky="w", pady=6)

        ctk.CTkCheckBox(chk_grid, text="Skip Existing Files (Auto-Resume)", variable=self._skip,
                        font=("Segoe UI", 12), text_color=C["text"], fg_color=C["cyan"]).grid(row=1, column=0, sticky="w", pady=6)

        ctk.CTkCheckBox(chk_grid, text="Force ZIP Mode", variable=self._fzip,
                        font=("Segoe UI", 12), text_color=C["text"], fg_color=C["cyan"]).grid(row=2, column=0, sticky="w", pady=6)

        ctk.CTkCheckBox(chk_grid, text="Show Info Only", variable=self._info,
                        font=("Segoe UI", 12), text_color=C["text"], fg_color=C["cyan"]).grid(row=2, column=1, sticky="w", pady=6)

        # Worker Slider
        sl_frame = ctk.CTkFrame(opt_card, fg_color="transparent")
        sl_frame.pack(fill="x", padx=12, pady=(0, 12))
        
        sl_top = ctk.CTkFrame(sl_frame, fg_color="transparent")
        sl_top.pack(fill="x")
        ctk.CTkLabel(sl_top, text="Parallel Connections:", font=("Segoe UI", 11), text_color=C["sub"]).pack(side="left")
        self._worker_lbl = ctk.CTkLabel(sl_top, text="Stream Active", font=("Consolas", 11, "bold"), text_color=C["sub"])
        self._worker_lbl.pack(side="right")

        self._workers = tk.IntVar(value=4)
        self._worker_slider = ctk.CTkSlider(sl_frame, from_=1, to=32, number_of_steps=31,
                                             variable=self._workers, command=self._on_slider_change)
        self._worker_slider.pack(fill="x", pady=(4, 0))

        # Initial slider state setup
        self._toggle_workers_state()

        # 4. Action Control Buttons
        btn_box = ctk.CTkFrame(left_panel, fg_color="transparent")
        btn_box.pack(fill="x", side="bottom", pady=(10, 0))

        self._start_btn = ctk.CTkButton(btn_box, text="START EXTRACTION", height=48,
                                         font=("Segoe UI", 14, "bold"), fg_color=C["cyan"],
                                         hover_color=C["cyan_hover"], text_color=C["bg"], command=self._start)
        self._start_btn.pack(side="left", fill="x", expand=True, padx=(0, 8))

        self._stop_btn = ctk.CTkButton(btn_box, text="STOP", height=48, width=110,
                                        font=("Segoe UI", 13, "bold"), fg_color=C["raised"],
                                        hover_color=C["rose"], text_color=C["text"], state="disabled", command=self._stop)
        self._stop_btn.pack(side="right")

        # ── RIGHT PANEL: LIVE DASHBOARD & STATS ───────────────────────────────
        right_panel = ctk.CTkFrame(main_grid, fg_color="transparent")
        right_panel.grid(row=0, column=1, sticky="nsew", padx=(10, 0))

        # 1. Hero 2x2 Stats Grid
        SectionHeader(right_panel, "LIVE METRICS").pack(fill="x", pady=(0, 8))
        stats_grid = ctk.CTkFrame(right_panel, fg_color="transparent")
        stats_grid.pack(fill="x", pady=(0, 15))
        stats_grid.columnconfigure((0, 1), weight=1)

        self._c_speed  = StatCard(stats_grid, "SPEED", C["cyan"])
        self._c_speed.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=(0, 8))

        self._c_eta    = StatCard(stats_grid, "ETA", C["amber"])
        self._c_eta.grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=(0, 8))

        self._c_ratio  = StatCard(stats_grid, "RATIO", C["purple"])
        self._c_ratio.grid(row=1, column=0, sticky="ew", padx=(0, 4))

        self._c_disk   = StatCard(stats_grid, "DISK WRITTEN", C["emerald"])
        self._c_disk.grid(row=1, column=1, sticky="ew", padx=(4, 0))

        # 2. Dual Progress Bars
        SectionHeader(right_panel, "PROGRESS DISPATCH").pack(fill="x", pady=(0, 8))
        prog_card = ctk.CTkFrame(right_panel, fg_color=C["panel"], corner_radius=10, border_width=1, border_color=C["border"])
        prog_card.pack(fill="x", pady=(0, 15))

        self._pb_total = StyledProgress(prog_card, "STREAM PROGRESS", color=C["cyan"])
        self._pb_total.pack(fill="x", padx=12, pady=12)

        self._pb_files = StyledProgress(prog_card, "FILE BATCH", color=C["purple"])
        self._pb_files.pack(fill="x", padx=12, pady=(0, 12))

        # 3. Terminal Console Drawer
        SectionHeader(right_panel, "CONSOLE LOGS").pack(fill="x", pady=(0, 8))
        log_card = ctk.CTkFrame(right_panel, fg_color=C["panel"], corner_radius=10, border_width=1, border_color=C["border"])
        log_card.pack(fill="both", expand=True)

        log_hdr = ctk.CTkFrame(log_card, fg_color="transparent", height=28)
        log_hdr.pack(fill="x", padx=8, pady=(6, 0))

        ctk.CTkLabel(log_hdr, text="STDOUT PIPE", font=("Consolas", 10, "bold"), text_color=C["muted"]).pack(side="left", padx=4)
        ctk.CTkButton(log_hdr, text="Clear", width=50, height=20, font=("Segoe UI", 10), fg_color="transparent", hover_color=C["raised"], text_color=C["sub"], command=self._clear_log).pack(side="right")
        ctk.CTkButton(log_hdr, text="Copy", width=50, height=20, font=("Segoe UI", 10), fg_color="transparent", hover_color=C["raised"], text_color=C["sub"], command=self._copy_log).pack(side="right", padx=2)

        self._log = ctk.CTkTextbox(log_card, font=("Consolas", 10), fg_color=C["bg"], text_color=C["text"], border_width=0)
        self._log.pack(fill="both", expand=True, padx=8, pady=8)
        self._log.configure(state="disabled")

    # ── Interactivity & Event Handlers ────────────────────────────────────────

    def _toggle_workers_state(self):
        """ Dynamically enables/disables worker slider based on Force Stream """
        if self._fstrm.get():
            self._worker_slider.configure(state="disabled", progress_color=C["dim"], button_color=C["muted"])
            self._worker_lbl.configure(text="Single Pipe (Stream)", text_color=C["sub"])
        else:
            self._worker_slider.configure(state="normal", progress_color=C["cyan"], button_color=C["cyan"])
            self._worker_lbl.configure(text=f"{int(self._workers.get())} Threads", text_color=C["cyan"])

    def _on_slider_change(self, val):
        if not self._fstrm.get():
            self._worker_lbl.configure(text=f"{int(val)} Threads", text_color=C["cyan"])

    def _paste(self):
        try:
            txt = self.clipboard_get().strip()
            if txt: self._url.set(txt)
        except Exception: pass

    def _browse(self):
        d = filedialog.askdirectory()
        if d: self._out.set(d)

    def _write_log(self, text):
        self._log.configure(state="normal")
        self._log.insert("end", text + "\n")
        self._log.see("end")
        self._log.configure(state="disabled")

    def _clear_log(self):
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")

    def _copy_log(self):
        """ Native event copy selection (avoids low-level win32 clipboard hooks) """
        try:
            self._log.configure(state="normal")
            self._log.tag_add("sel", "1.0", "end")
            self._log.focus_set()
            self.event_generate("<<Copy>>")
            self._log.configure(state="disabled")
        except Exception: pass

    # ── Subprocess Execution & Extraction Logic ───────────────────────────────

    def _start(self):
        url = self._url.get().strip()
        out = self._out.get().strip()
        if not url:
            self._write_log("[!] ERROR: URL field is empty.")
            return

        self._clear_log()
        self._running = True
        self._start_btn.configure(state="disabled", fg_color=C["raised"], text_color=C["sub"])
        self._stop_btn.configure(state="normal", fg_color=C["rose"])
        self._spinner.start()
        self._mode_pill.configure(text="EXTRACTING...", fg_color=C["card"], text_color=C["amber"])

        self._c_speed.set("Starting...")
        self._c_eta.set("—")
        self._c_ratio.set("—")
        self._c_disk.set("—")
        self._pb_total.reset()
        self._pb_files.reset()

        if self._exe.endswith(".py"):
            cmd = [sys.executable, self._exe, url, out]
        else:
            cmd = [self._exe, url, out]

        if self._skip.get():  cmd.append("--skip-existing")
        if self._info.get():  cmd.append("--info")
        if self._fzip.get():  cmd.append("--force-zip")
        if self._fstrm.get(): cmd.append("--force-stream")
        cmd += ["--workers", str(int(self._workers.get()))]

        self._write_log(f"[*] Executing command: {' '.join(cmd)}")
        threading.Thread(target=self._run, args=(cmd,), daemon=True).start()
    

    
    
    def _stop(self):
            if self._proc:
                try:
                    self._stop_btn.configure(state="disabled", fg_color=C["raised"])
                    
                    # Perform full process tree termination
                    _kill_process_tree(self._proc.pid)
                    
                    self._write_log("\n[!] Process tree and streaming pipes killed by user.")
                    self._mode_pill.configure(text="STOPPED", fg_color=C["card"], text_color=C["rose"])
                    self._spinner.stop()
                except Exception as e:
                    self._write_log(f"\n[!] Error during stop: {str(e)}")

    def _run(self, cmd):
        try:
            env = os.environ.copy()
            env.pop("_MEIPASS2", None); env.pop("_MEIPASS", None)
            env["PYTHONIOENCODING"] = "utf-8"; env["PYTHONUNBUFFERED"] = "1"

            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                shell=(True if " " in self._exe else False), env=env, **_popen_kwargs()
            )
            for line in iter(self._proc.stdout.readline, ''):
                if line: self._q.put(("line", line))
            self._proc.wait()
            self._q.put(("done", self._proc.returncode))
        except Exception as e:
            self._q.put(("error", str(e)))

    def _poll(self):
        try:
            while True:
                msg, val = self._q.get_nowait()
                if msg == "line":
                    self._handle_line(val)
                elif msg == "done":
                    self._running = False
                    self._start_btn.configure(state="normal", fg_color=C["cyan"], text_color=C["bg"])
                    self._stop_btn.configure(state="disabled", fg_color=C["raised"])
                    self._spinner.stop()
                    if self._mode_pill.cget("text") != "STOPPED":
                        if val == 0:
                            self._mode_pill.configure(text="FINISHED ✓", fg_color=C["card"], text_color=C["emerald"])
                        else:
                            self._mode_pill.configure(text=f"EXIT {val}", fg_color=C["card"], text_color=C["rose"])
                elif msg == "error":
                    self._write_log(f"Process Error: {val}")
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _handle_line(self, raw_line):
        line = strip_ansi(raw_line).strip()
        if not line: return

        # Parse TQDM Progress Lines
        m = self._RE_TQDM.search(line)
        if m:
            kind, pct, cur, total, meta = m.groups()
            val = int(pct) / 100.0
            detail_text = f"{cur} / {total}"

            pm = self._RE_POST.search(meta)
            if pm:
                out_size, ratio, est_total = pm.groups()
                self._c_ratio.set(ratio)
                self._c_disk.set(out_size)
                detail_text += f"   •   Est. Final: {est_total}"

            if kind in ("Total", "Extracting"):
                self._pb_total.update(val, detail_text)
                rate = self._RE_RATE.search(meta)
                eta  = self._RE_ETA.search(meta)
                if rate: self._c_speed.set(rate.group(1))
                if eta:  self._c_eta.set(eta.group(1))
            else:
                self._pb_files.update(val, detail_text)
            return

        # Parse Summary Box Lines
        bm = self._RE_BOX.search(line)
        if bm:
            label, value = bm.group(1).lower(), bm.group(2).strip()
            if "speed" in label:    self._c_speed.set(value)
            elif "ratio" in label:  self._c_ratio.set(value)
            elif "extracted" in label or "total size" in label: self._c_disk.set(value)
            self._write_log(line)
            return

        if "Mode" in line and ":" in line:
            self._mode_pill.configure(text=line.split(":")[-1].strip().upper())

        box_chars = set("┌─┐│└┘├┤ ")
        if not all(c in box_chars for c in line):
            self._write_log(line)

# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ExtractGUI().mainloop()