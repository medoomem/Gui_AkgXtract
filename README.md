# Universal Archive Extractor (AkgXtract)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)](https://isocpp.org/)

A lightweight utility that downloads and extracts archives on-the-fly directly from a URL without saving temporary compressed files to your hard drive.

Supported formats: **ZIP** (including Zstandard and Deflate64), **RAR**, **TAR**, **7Z**, **GZ**, and **BZ2**.

---

## Code Transparency & Security

If you prefer not to run pre-compiled binary releases from the internet, **all code in this repository is 100% open-source**. 

You can inspect `build2.py` (CLI extraction engine) and `gui.py` (CustomTkinter UI) directly, audit every network request, and compile your own clean executable locally using the instructions below.

---

## Option 1: Run directly from Source (No Compilation Required)

The fastest and most transparent way to use this tool is to run the raw Python scripts directly.

### 1. Install Dependencies
Ensure you have **Python 3.10+** installed. Install the required Python packages:

```bash
pip install requests remotezip tqdm zstandard zipfile_deflate64 customtkinter pillow
```

### 2. Run the CLI Engine
```bash
# Basic usage:
python build2.py <URL> [output_directory]

# Example with 4 parallel download threads:
python build2.py https://example.com/game_archive.zip D:\Games --workers 4
```

### 3. Run the GUI
To launch the user interface directly:
```bash
python gui.py
```

---

## Option 2: Compile Your Own Standalone Executable

If you want to package the code into a standalone `.exe` folder that runs without needing Python installed on other PCs, you can compile it yourself using **Nuitka** (which converts Python code into native C++).

### Prerequisites
1. **Python 3.10+**
2. **Nuitka:**
   ```bash
   pip install nuitka
   ```
3. **C++ Compiler:** On Windows, Nuitka utilizes Visual Studio (MSVC) or MinGW. If no compiler is installed, Nuitka will automatically download a portable GCC compiler on your first build.

---

### Step-by-Step Self-Compilation Guide

#### Step 1: Compile the CLI Backend (`akgxtract.exe`)
Compile the extraction script into a standalone backend binary folder:

```bash
python -m nuitka --standalone \
                 --assume-yes-for-downloads \
                 --output-dir=build_backend \
                 --output-filename=akgxtract.exe \
                 build2.py
```

#### Step 2: Compile the GUI Frontend (`guiextract.exe`)
Compile the GUI interface without a console window:

```bash
python -m nuitka --standalone \
                 --assume-yes-for-downloads \
                 --windows-console-mode=disable \
                 --enable-plugin=tk-inter \
                 --include-package-data=customtkinter \
                 --windows-icon-from-ico=downloader_icon.ico \
                 --output-dir=build_gui \
                 --output-filename=guiextract.exe \
                 gui.py
```

#### Step 3: Assemble Your Local Executable Folder
The GUI app expects the CLI backend and network utilities to live inside a `backend/` subfolder. 

Run these commands in PowerShell to create your complete output folder (`dist\guiextract`):

```powershell
# 1. Create target folder structure
New-Item -ItemType Directory -Force -Path "dist\guiextract\backend"

# 2. Copy compiled GUI files
Copy-Item -Path "build_gui\gui.dist\*" -Destination "dist\guiextract\" -Recurse -Force
Copy-Item -Path "downloader_icon.ico" -Destination "dist\guiextract\" -Force

# 3. Copy compiled CLI Backend into backend/ subfolder
Copy-Item -Path "build_backend\build2.dist\*" -Destination "dist\guiextract\backend\" -Recurse -Force

# 4. Copy streaming utilities (curl.exe, bsdtar.exe, msys DLLs) into backend/
Copy-Item -Path "Tools used\*" -Destination "dist\guiextract\backend\" -Recurse -Force
```

#### Step 4: Run Your Freshly Built App!
Navigate to `dist/guiextract/` and launch your newly compiled executable:

```cmd
dist\guiextract\guiextract.exe
```

---

## Final Compiled Directory Layout

Once compiled, your self-built standalone directory will look like this:

```
dist/guiextract/
├── guiextract.exe          # Main GUI Executable (Self-Compiled)
├── downloader_icon.ico     # Window Icon
├── CustomTkinter Assets/   # UI Themes & Fonts
├   ...
└── backend/
    ├── akgxtract.exe       # CLI Extraction Backend (Self-Compiled)
    ├── curl.exe            # Network Streaming Tool
    ├── bsdtar.exe          # Stream Extraction Engine
    ├── msys-*.dll          # MSYS2 Pipe Communication Libraries
    └── ...
```

---

## Understanding Included Binaries (`Tools used/`)

To stream sequential archives (like `.rar` or `.tar`) directly over the network without temporary disk storage, the engine pipes raw network bytes from **`curl.exe`** into **`bsdtar.exe`**.

* **`curl.exe` & `bsdtar.exe`:** Standard, trusted open-source CLI utilities used for high-speed network streaming and extraction.
* **`msys-*.dll`:** Standard MSYS2 runtime libraries required by `curl` and `bsdtar` to process binary stdin/stdout streams without Windows line-ending corruption.

---

## License

This project is licensed under the [MIT License](LICENSE).