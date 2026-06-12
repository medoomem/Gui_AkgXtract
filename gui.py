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


# ─── Theme ────────────────────────────────────────────────────────────────────

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

C = {
    "bg":      "#0b0d14",
    "panel":   "#111420",
    "card":    "#161929",
    "raised":  "#1c2035",
    "border":  "#252942",
    "border2": "#2f3458",
    "cyan":    "#00d4ff",
    "cyan_d":  "#0099bb",
    "purple":  "#7c5cfc",
    "purple_d":"#5a3fd4",
    "green":   "#00e5a0",
    "red":     "#ff4b6e",
    "yellow":  "#ffc94d",
    "text":    "#dde3f5", # Bright Text
    "sub":     "#8892b0",
    "muted":   "#4a5278",
    "dim":     "#2a2f4e",
}

# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_resource_path(relative_path):
    """ Finds bundled files in Nuitka Onefile """
    base_path = os.path.dirname(__file__)
    return os.path.join(base_path, relative_path)

def find_exe() -> str:
    exe_name = "akgxtract.exe" if sys.platform == "win32" else "extract"
    # Look in 'backend' instead of 'bin'
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

# ─── Reusable widgets ─────────────────────────────────────────────────────────

class SectionLabel(ctk.CTkFrame):
    def __init__(self, parent, title: str, **kw):
        super().__init__(parent, fg_color="transparent", **kw)
        ctk.CTkLabel(self, text=title, font=("Consolas", 11, "bold"),
                     text_color=C["muted"]).pack(side="left")
        ctk.CTkFrame(self, height=1, fg_color=C["border"]).pack(
            side="left", fill="x", expand=True, padx=(10, 0), pady=1)

class StyledProgress(ctk.CTkFrame):
    def __init__(self, parent, label: str, color: str = None, **kw):
        super().__init__(parent, fg_color="transparent", **kw)
        self._color = color or C["cyan"]

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x")
        self._lbl = ctk.CTkLabel(top, text=label, font=("Consolas", 14, "bold"),
                                  text_color=C["sub"], anchor="w")
        self._lbl.pack(side="left")
        self._pct = ctk.CTkLabel(top, text="0%", font=("Consolas", 16, "bold"),
                                  text_color=self._color, anchor="e")
        self._pct.pack(side="right")

        # Increased height from 6 to 12
        self._bar = ctk.CTkProgressBar(self, height=12, corner_radius=6,
                                        fg_color=C["dim"],
                                        progress_color=self._color)
        self._bar.pack(fill="x", pady=(5, 5))
        self._bar.set(0)

        # Changed text_color from C["muted"] to C["text"] for visibility
        self._detail = ctk.CTkLabel(self, text="", font=("Consolas", 12),
                                     text_color=C["text"], anchor="w")
        self._detail.pack(fill="x")

    def update(self, pct: float, detail: str = "", label: str = ""):
        pct = max(0.0, min(1.0, pct))
        self._bar.set(pct)
        self._pct.configure(text=f"{int(pct * 100)}%")
        if detail: self._detail.configure(text=detail)
        if label:  self._lbl.configure(text=label)

    def reset(self):
        self._bar.set(0)
        self._pct.configure(text="0%")
        self._detail.configure(text="")

class StatCard(ctk.CTkFrame):
    def __init__(self, parent, label: str, **kw):
        super().__init__(parent, fg_color=C["card"], corner_radius=10,
                          border_width=1, border_color=C["border"], **kw)
        ctk.CTkLabel(self, text=label, font=("Consolas", 11, "bold"),
                     text_color=C["muted"]).pack(pady=(10, 0), padx=10)
        # Increased value font size to 18
        self._val = ctk.CTkLabel(self, text="—", font=("Consolas", 18, "bold"),
                                  text_color=C["cyan"])
        self._val.pack(pady=(2, 10), padx=10)

    def set(self, text: str, color: str = None):
        self._val.configure(text=text, text_color=color or C["cyan"])

class Spinner(ctk.CTkLabel):
    _FRAMES = ["◐", "◓", "◑", "◒"]
    def __init__(self, parent, **kw):
        super().__init__(parent, text="", font=("Consolas", 16),
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

# ─── Main window ─────────────────────────────────────────────────────────────


def get_icon_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

class ExtractGUI(ctk.CTk):
    _RE_TQDM = re.compile(r"(Total|Files|Extracting)[:\s]+(\d+)%.*?([\d\.]+\s*\w+)\s*/\s*([\d\.]+\s*\w+).*?\[([^\]]+)\]")
    _RE_POST = re.compile(r"Out:\s*([^\|]+)\s*\|\s*Ratio:\s*([^\|]+)\s*\|\s*Est\. Total:\s*(.+)")
    _RE_BOX = re.compile(r"│\s{2}([^:│]+?)\s*:\s*([^│\n]+?)\s*│")
    _RE_RATE = re.compile(r"([\d\.]+\s*[KMGT]?B/s)")
    _RE_ETA = re.compile(r"\d+:\d+<(\d{2}:\d{2})")

    def __init__(self):
        super().__init__()
        self.title("Universal Archive Extractor")
        self.geometry("1000x900") # Made slightly larger
        self.configure(fg_color=C["bg"])

        self._exe       = find_exe()
        self._proc      = None
        self._q         = queue.Queue()
        self._running   = False
        self._build_ui()
        self.after(100, self._poll)
        # 2. ADD THIS LINE HERE to set the title bar icon
        icon_path = get_icon_path("downloader_icon.ico")
        if os.path.exists(icon_path):
            self.iconbitmap(icon_path)

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color=C["panel"], corner_radius=0, height=70)
        header.pack(fill="x")
        ctk.CTkLabel(header, text="⬡", font=("Consolas", 28), text_color=C["cyan"]).pack(side="left", padx=25)
        ctk.CTkLabel(header, text="UNIVERSAL EXTRACTOR", font=("Consolas", 22, "bold")).pack(side="left")
        
        self._spinner = Spinner(header)
        self._spinner.pack(side="right", padx=25)
        self._mode_lbl = ctk.CTkLabel(header, text="", font=("Consolas", 12), text_color=C["sub"])
        self._mode_lbl.pack(side="right", padx=10)

        body = ctk.CTkScrollableFrame(self, fg_color=C["bg"], corner_radius=0)
        body.pack(fill="both", expand=True)
        pad = ctk.CTkFrame(body, fg_color="transparent")
        pad.pack(fill="both", expand=True, padx=30, pady=25)

        # Source Section
        SectionLabel(pad, "SOURCE & DESTINATION").pack(fill="x", pady=(0, 15))
        src_box = ctk.CTkFrame(pad, fg_color=C["panel"], border_width=1, border_color=C["border"], corner_radius=12)
        src_box.pack(fill="x", pady=(0, 25))

        row1 = ctk.CTkFrame(src_box, fg_color="transparent")
        row1.pack(fill="x", padx=20, pady=(20, 10))
        self._url = tk.StringVar()
        ctk.CTkEntry(row1, textvariable=self._url, placeholder_text="Archive URL...", height=40, font=("Consolas", 14), fg_color=C["raised"]).pack(side="left", fill="x", expand=True, padx=(0, 10))
        ctk.CTkButton(row1, text="Paste", width=80, height=40, font=("Consolas", 13, "bold"), command=self._paste).pack(side="right")

        row2 = ctk.CTkFrame(src_box, fg_color="transparent")
        row2.pack(fill="x", padx=20, pady=(0, 20))
        self._out = tk.StringVar(value=str(Path.cwd() / "extracted"))
        ctk.CTkEntry(row2, textvariable=self._out, height=40, font=("Consolas", 14), fg_color=C["raised"]).pack(side="left", fill="x", expand=True, padx=(0, 10))
        ctk.CTkButton(row2, text="Browse", width=80, height=40, font=("Consolas", 13, "bold"), fg_color=C["purple"], hover_color=C["purple_d"], command=self._browse).pack(side="right")

        # Options
        SectionLabel(pad, "OPTIONS").pack(fill="x", pady=(0, 15))
        opt_box = ctk.CTkFrame(pad, fg_color=C["panel"], border_width=1, border_color=C["border"], corner_radius=12)
        opt_box.pack(fill="x", pady=(0, 25))

        self._skip, self._info, self._fzip, self._fstrm = [tk.BooleanVar() for _ in range(4)]
        grid = ctk.CTkFrame(opt_box, fg_color="transparent")
        grid.pack(fill="x", padx=20, pady=20)
        
        checks = [("Skip Existing", self._skip), ("Show Info Only", self._info), ("Force ZIP", self._fzip), ("Force Stream", self._fstrm)]
        for i, (txt, var) in enumerate(checks):
            ctk.CTkCheckBox(grid, text=txt, variable=var, font=("Consolas", 14)).grid(row=i//2, column=i%2, sticky="w", pady=8, padx=20)

        self._workers = tk.IntVar(value=4)
        row_spin = ctk.CTkFrame(opt_box, fg_color="transparent")
        row_spin.pack(fill="x", padx=25, pady=(0, 20))
        ctk.CTkLabel(row_spin, text="Parallel Workers:", font=("Consolas", 13)).pack(side="left", padx=(0, 15))
        ctk.CTkSlider(row_spin, from_=1, to=32, number_of_steps=31, variable=self._workers, width=300).pack(side="left")
        ctk.CTkLabel(row_spin, textvariable=self._workers, font=("Consolas", 15, "bold"), text_color=C["cyan"]).pack(side="left", padx=15)

        # Stats Cards
        stats_row = ctk.CTkFrame(pad, fg_color="transparent")
        stats_row.pack(fill="x", pady=(0, 25))
        self._c_speed = StatCard(stats_row, "SPEED")
        self._c_speed.pack(side="left", fill="x", expand=True, padx=4)
        self._c_ratio = StatCard(stats_row, "RATIO")
        self._c_ratio.pack(side="left", fill="x", expand=True, padx=4)
        self._c_eta = StatCard(stats_row, "ETA")
        self._c_eta.pack(side="left", fill="x", expand=True, padx=4)
        self._c_status = StatCard(stats_row, "STATUS")
        self._c_status.pack(side="left", fill="x", expand=True, padx=4)

        # Progress
        SectionLabel(pad, "PROGRESS").pack(fill="x", pady=(0, 15))
        prog_box = ctk.CTkFrame(pad, fg_color=C["panel"], border_width=1, border_color=C["border"], corner_radius=12)
        prog_box.pack(fill="x", pady=(0, 25))
        self._pb_total = StyledProgress(prog_box, "TOTAL PROGRESS", color=C["cyan"])
        self._pb_total.pack(fill="x", padx=20, pady=20)
        self._pb_files = StyledProgress(prog_box, "FILE PROGRESS", color=C["purple"])
        self._pb_files.pack(fill="x", padx=20, pady=(0, 20))

        # Log
        self._log = ctk.CTkTextbox(pad, height=180, font=("Consolas", 11), fg_color=C["card"], border_width=1, border_color=C["border"])
        self._log.pack(fill="x", pady=(0, 25))
        self._log.configure(state="disabled")

        # Buttons
        btn_row = ctk.CTkFrame(pad, fg_color="transparent")
        btn_row.pack(fill="x")
        self._start_btn = ctk.CTkButton(btn_row, text="START EXTRACTION", height=55, font=("Consolas", 16, "bold"), fg_color=C["cyan"], text_color=C["bg"], command=self._start)
        self._start_btn.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self._stop_btn = ctk.CTkButton(btn_row, text="STOP", height=55, width=150, font=("Consolas", 14, "bold"), fg_color=C["raised"], state="disabled", command=self._stop)
        self._stop_btn.pack(side="right")

    # ── Logic ─────────────────────────────────────────────────────────────────

    def _paste(self):
        try: self._url.set(self.clipboard_get().strip())
        except: pass
    def _browse(self):
        d = filedialog.askdirectory()
        if d: self._out.set(d)
    def _write_log(self, text):
        self._log.configure(state="normal")
        self._log.insert("end", text + "\n")
        self._log.see("end")
        self._log.configure(state="disabled")
    def _clear_log(self):
        self._log.configure(state="normal"); self._log.delete("1.0", "end"); self._log.configure(state="disabled")

    def _start(self):
        url = self._url.get().strip()
        out = self._out.get().strip()
        if not url: self._write_log("[!] ERROR: URL is empty."); return
        
        self._clear_log()
        self._running = True
        self._start_btn.configure(state="disabled")
        self._stop_btn.configure(state="normal")
        self._spinner.start()
        self._c_status.set("Starting...", C["yellow"])
        self._pb_total.reset(); self._pb_files.reset()

        cmd = [self._exe, url, out]
        if self._skip.get(): cmd.append("--skip-existing")
        if self._info.get(): cmd.append("--info")
        if self._fzip.get(): cmd.append("--force-zip")
        if self._fstrm.get(): cmd.append("--force-stream")
        cmd += ["--workers", str(int(self._workers.get()))]

        self._write_log(f"[*] Executing: {' '.join(cmd)}")
        threading.Thread(target=self._run, args=(cmd,), daemon=True).start()

    def _stop(self):
        if self._proc:
            try:
                self._stop_btn.configure(state="disabled")
                if sys.platform == "win32":
                    subprocess.call(['taskkill', '/F', '/T', '/PID', str(self._proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    self._proc.terminate()
                self._write_log("\n[!] User aborted process.")
                self._c_status.set("Stopped", C["red"])
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
        except Exception as e: self._q.put(("error", str(e)))

    def _poll(self):
        try:
            while True:
                msg, val = self._q.get_nowait()
                if msg == "line": self._handle_line(val)
                elif msg == "done":
                    self._running = False
                    self._start_btn.configure(state="normal"); self._stop_btn.configure(state="disabled")
                    self._spinner.stop()
                    if self._c_status._val.cget("text") != "Stopped":
                        self._c_status.set("Finished ✓" if val == 0 else f"Exit {val}", C["green"] if val == 0 else C["red"])
                elif msg == "error": self._write_log(f"Process Error: {val}")
        except queue.Empty: pass
        self.after(100, self._poll)

    def _handle_line(self, raw_line):
        line = strip_ansi(raw_line).strip()
        if not line: return

        m = self._RE_TQDM.search(line)
        if m:
            kind, pct, cur, total, meta = m.groups()
            val = int(pct) / 100.0
            detail_text = f"{cur} / {total}"
            
            pm = self._RE_POST.search(meta)
            if pm:
                out_size, ratio, est_total = pm.groups()
                self._c_ratio.set(ratio)
                # Formatting the predictive metrics clearly
                detail_text += f"   ➤  Disk: {out_size}   |   Est. Final: {est_total}"

            if kind in ("Total", "Extracting"):
                self._pb_total.update(val, detail_text)
                rate = self._RE_RATE.search(meta)
                eta = self._RE_ETA.search(meta)
                if rate: self._c_speed.set(rate.group(1))
                if eta: self._c_eta.set(eta.group(1))
            else:
                self._pb_files.update(val, detail_text)
            return

        bm = self._RE_BOX.search(line)
        if bm:
            label, value = bm.group(1).lower(), bm.group(2).strip()
            if "speed" in label: self._c_speed.set(value)
            elif "ratio" in label: self._c_ratio.set(value)
            self._write_log(line); return

        if "Mode" in line and ":" in line: self._mode_lbl.configure(text=line.split(":")[-1].strip())
        
        box_chars = set("┌─┐│└┘├┤ ")
        if not all(c in box_chars for c in line): self._write_log(line)

if __name__ == "__main__":
    ExtractGUI().mainloop()
