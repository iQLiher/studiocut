@echo off
setlocal EnableDelayedExpansion
title StudioCut - Installer

echo.
echo  ============================================
echo       StudioCut - Installer
echo  ============================================
echo.

:: 1. Check if Python 3.11 is installed
echo [1/4] Checking Python 3.11...
py -3.11 --version >nul 2>&1
if %errorlevel% neq 0 (
    echo        Not found. Installing with winget...
    winget install Python.Python.3.11 --silent --accept-package-agreements --accept-source-agreements
    if %errorlevel% neq 0 (
        echo.
        echo  ERROR: Could not install Python 3.11 automatically.
        echo  Download it manually from:
        echo  https://www.python.org/downloads/release/python-31110/
        echo  Do NOT check "Add to PATH" and run this installer again.
        echo.
        goto :error
    )
    echo        Python 3.11 installed successfully.
) else (
    echo        Already installed.
)

:: 2. Install base dependencies
echo.
echo [2/4] Installing dependencies...
py -3.11 -m pip install --upgrade pip -q
py -3.11 -m pip install flask flask-cors Pillow psutil GPUtil pyinstaller "rembg[cpu]"
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Failed to install base dependencies.
    goto :error
)
echo        Done.

:: 3. Detect NVIDIA GPU and install correct onnxruntime
echo.
echo [3/4] Detecting GPU...
nvidia-smi >nul 2>&1
if %errorlevel% equ 0 (
    echo        NVIDIA GPU detected. Installing onnxruntime-gpu...
    py -3.11 -m pip uninstall onnxruntime onnxruntime-gpu -y >nul 2>&1
    py -3.11 -m pip install onnxruntime-gpu
    if %errorlevel% neq 0 (
        echo        WARNING: Could not install onnxruntime-gpu. Falling back to CPU.
        py -3.11 -m pip install onnxruntime
    ) else (
        echo        onnxruntime-gpu installed. Checking CUDA...
        py -3.11 -c "import onnxruntime as ort; p=ort.get_available_providers(); print('        CUDA OK - GPU ready' if 'CUDAExecutionProvider' in p else '        WARNING: CUDA not available, falling back to CPU')"
    )
) else (
    echo        No NVIDIA GPU detected. Installing onnxruntime CPU...
    py -3.11 -m pip uninstall onnxruntime onnxruntime-gpu -y >nul 2>&1
    py -3.11 -m pip install onnxruntime
    echo        Done.
)

:: 4. Pre-download BiRefNet model
echo.
echo [4/4] Downloading BiRefNet model (~170MB, this may take a few minutes)...
echo        Do not close this window.
py -3.11 -c "from rembg import new_session; print('        Starting download...'); s=new_session('birefnet-general'); print('        Model ready.')"
if %errorlevel% neq 0 (
    echo        WARNING: Could not download the model now.
    echo        It will download automatically on the first image processed.
)

:: Update start.bat to use py -3.11
echo @echo off > start.bat
echo cd /d "%%~dp0" >> start.bat
echo py -3.11 app.py >> start.bat
echo pause >> start.bat

echo.
echo  ============================================
echo       Installation complete!
echo       Run start.bat to launch StudioCut
echo  ============================================
echo.
pause
exit /b 0

:error
echo.
echo  Installation failed. See the error above.
echo.
pause
exit /b 1
