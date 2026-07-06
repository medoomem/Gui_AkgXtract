# Universal Archive Extractor (AkgXtract)

A direct, lightweight utility to download and extract archives on-the-fly directly from a URL, bypassing the need to store temporary compressed files on your disk.

Supported formats include: **ZIP** (including zstd/deflate64), **RAR**, **TAR**, **7Z**, **GZ**, and **BZ2**.

---

## How It Works

* **ZIP Mode (HTTP Range Requests):** The tool uses range requests to read the archive's central directory directly from the URL. It can jump around the file to download and extract specific blocks, supporting multi-threaded parallel downloads.
* **Stream Mode (RAR/TAR/Other):** For sequential formats that don't support range requests, it streams the archive dynamically over network pipes (`curl` to `bsdtar`) to write files straight to your directory in real-time.

---

## Running From Source

If you prefer not to run pre-compiled executables, you can run the tool directly using your local Python interpreter. 

### Prerequisites
Make sure you have [Python 3.10+](https://www.python.org/downloads/) installed on your system.

Install the required Python dependencies:
```bash
pip install requests remotezip tqdm zstandard zipfile_deflate64 customtkinter
```

---

### Option A: Use the CLI Directly (No Compilation Required)
This is the fastest, most transparent way to use the tool from source. You can run the extraction engine directly from your terminal without compiling anything.

```bash
# Basic Usage:
python build2.py <URL> [output_directory]

# Example:
python build2.py https://example.com/game.zip D:\Games --workers 4
```

**Common CLI Options:**
* `--skip-existing` or `-s`: Skip files that already exist with the correct size.
* `--workers N` or `-w N`: Number of parallel connections to use (ZIP only).
* `--force-stream`: Force streaming mode (curl | bsdtar) even for ZIP archives.
* `--info` or `-i`: Show archive information without downloading (ZIP only).

---

### Option B: Run the GUI From Source
Because the GUI (`gui.py`) spawns the extraction engine as a background subprocess, **you must build the CLI executable first** before launching the GUI.

1. **Compile the CLI backend:**
   Run your repository's build script to package `build2.py` into the required `backend/akgxtract.exe` binary:
   ```bash
   python build2.py
   ```
2. **Launch the GUI:**
   Once the backend binary is built and placed in the target directory, start the user interface:
   ```bash
   python gui.py
   ```

---

## Compiling From Source (Nuitka Compilation)

If you want to package the Python code into optimized standalone binaries yourself, you can compile them using **Nuitka**. Nuitka translates Python modules into C++ code and compiles them into machine-native executables.

### Compiler Prerequisites
Nuitka requires a C++ compiler to generate binaries:
* **Windows:** [Visual Studio (MSVC)](https://visualstudio.microsoft.com/vs/features/cplusplus/) or [MinGW-w64](https://www.mingw-w64.org/). (If no compiler is found on Windows, Nuitka will automatically prompt to download and configure MinGW on the first execution).
* **Linux:** GCC or Clang (`sudo apt install build-essential` on Debian/Ubuntu).

Ensure Nuitka is installed in your Python environment:
```bash
pip install nuitka
```

### 1. Compiling the CLI Backend
The GUI launcher expects the compiled CLI executable (`akgxtract.exe` or `akgxtract`) to reside inside the `backend` folder relative to `gui.py`.

* **Automated Build:**
  ```bash
  python build2.py
  ```
* **Manual Nuitka Command:**
  If you prefer to compile the CLI manually, run:
  ```bash
  nuitka --standalone --onefile --output-dir=backend --output-filename=akgxtract build2.py
  ```

### 2. Compiling the GUI Frontend
To package the GUI frontend into a standalone application that runs without spawning an unnecessary terminal console, you must include the Tkinter plugin and bundle the CustomTkinter assets:

```bash
nuitka --standalone \
       --onefile \
       --windows-console-mode=disable \
       --enable-plugin=tk-inter \
       --include-data-dir=customtkinter=customtkinter \
       --output-filename=guiextract \
       gui.py
```

*Note: For the compiled GUI to work, place the compiled `akgxtract` binary inside a folder named `backend/` located in the same directory as the compiled `guiextract` binary.*

---

## Trust & Security Transparency

In open-source software, trust is important. Because this utility handles downloads and extractions, some security suites or automated sandboxes may flag compiled releases with generic heuristics.

### Why does the compiled package contain DLLs?
The pre-compiled standalone package is bundled using **Nuitka** (which compiles Python to C++). It includes:
* **`libcrypto-3.dll` & `libssl-3.dll`:** Standard OpenSSL libraries required by Python's network modules (`requests`, `urllib3`) to safely handle secure HTTPS connections.
* **`curl.exe` & `bsdtar.exe`:** Standard, widely trusted command-line tools used by the operating system to stream sequential archives.

### Verifying the Pre-Compiled Setup
If you choose to use the pre-compiled version, you can verify its integrity:

* **Setup File MD5 Hash:** `426d4d8065ed7be258f7b78bfc555993`
* **VirusTotal Scan:** [View VirusTotal Analysis](https://www.virustotal.com/gui/file/79d7136f1cd6ccd76617727b618ceeda4fb10eff907835c7962511a1435f9bb5)

---

## Configuration & Usage Tips

* **Force Stream Mode vs. Force Zip Mode:**
  * For sequential or large archives (especially `.rar`), tick **Force Stream** mode (or use `--force-stream` in the CLI). This relies on binary pipes and is often significantly faster than standard methods.
  * For `.zip` archives where you want to download missing or specific files, you can use range requests, though latency may vary based on your connection to the host server.
* **Parallel Workers:** In standard ZIP mode, you can adjust the thread slider to use parallel connections for faster block retrieval.

---

## Dependencies
This project utilizes several open-source libraries:
* [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) (UI)
* [remotezip](https://github.com/gtsystem/remotezip) (Range requests)
* [tqdm](https://github.com/tqdm/tqdm) (Progress indicators)
* [zstandard](https://github.com/indygreg/python-zstandard) (ZSTD decompression)

---

## Disclaimer
This tool is provided for educational and personal administrative use. Please ensure you comply with the terms of service of any file hosting networks you interact with.
