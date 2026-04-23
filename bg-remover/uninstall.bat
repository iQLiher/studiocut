@echo off
setlocal EnableDelayedExpansion
title StudioCut - Uninstaller

echo.
echo  ============================================
echo       StudioCut - Uninstaller
echo  ============================================
echo.
echo  This will uninstall all StudioCut dependencies
echo  from Python 3.11. Python itself will NOT be removed.
echo.
set /p CONFIRM="  Are you sure? (Y/N): "
if /i "%CONFIRM%" neq "Y" (
    echo  Cancelled.
    pause
    exit /b 0
)

echo.
echo [1/2] Uninstalling packages...
py -3.11 -m pip uninstall -y flask flask-cors Pillow psutil GPUtil pyinstaller rembg onnxruntime onnxruntime-gpu opencv-python-headless numpy pooch huggingface-hub safetensors scipy tqdm requests Werkzeug Jinja2 click itsdangerous MarkupSafe certifi charset-normalizer urllib3 idna
echo        Done.

echo.
echo [2/2] Clearing BiRefNet model cache...
set "CACHE_DIR=%USERPROFILE%\.u2net"
if exist "%CACHE_DIR%" (
    rmdir /s /q "%CACHE_DIR%"
    echo        Model cache removed.
) else (
    echo        No model cache found.
)

echo.
echo  ============================================
echo       Uninstall complete.
echo  ============================================
echo.
pause
