import os
import sys
import time
import shutil
import threading
import platform
import json
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename

# ── Carpetas ──────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
INBOX      = BASE_DIR / "1_inbox"
PROCESSING = BASE_DIR / "2_processing"
OUTPUT     = BASE_DIR / "3_output"
RAW        = BASE_DIR / "4_raw"
SETTINGS_FILE = BASE_DIR / "settings.json"

for d in (INBOX, PROCESSING, OUTPUT, RAW):
    d.mkdir(exist_ok=True)

ALLOWED = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}

app = Flask(__name__)
CORS(app)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

# ── Configuración global ──────────────────────────────────────────────────────
DEFAULT_SETTINGS = {
    "format":   "JPEG",
    "quality":  85,
    "autostart": False,
    "bg_color": "#ffffff",
    "resolution": 2000
}

def load_settings():
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text())
            return {**DEFAULT_SETTINGS, **data}
        except:
            pass
    return DEFAULT_SETTINGS.copy()

def save_settings(s):
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))

settings = load_settings()

# ── Model loading state ───────────────────────────────────────────────────────
model_status = {"state": "idle", "progress": 0, "message": ""}


# ── Autostart helpers ─────────────────────────────────────────────────────────
APP_NAME   = "StudioCut"
START_SCRIPT = str(BASE_DIR / ("start.bat" if platform.system() == "Windows" else "start_linux.sh"))

def get_autostart_status():
    if platform.system() == "Windows":
        import winreg
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
            winreg.QueryValueEx(key, APP_NAME)
            winreg.CloseKey(key)
            return True
        except:
            return False
    elif platform.system() == "Darwin":
        plist = Path.home() / "Library/LaunchAgents/com.studiocut.app.plist"
        return plist.exists()
    return False

def set_autostart(enable):
    if platform.system() == "Windows":
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'cmd /c "{START_SCRIPT}"')
        else:
            try: winreg.DeleteValue(key, APP_NAME)
            except: pass
        winreg.CloseKey(key)
    elif platform.system() == "Darwin":
        plist_path = Path.home() / "Library/LaunchAgents/com.studiocut.app.plist"
        if enable:
            plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.studiocut.app</string>
    <key>ProgramArguments</key>
    <array><string>/bin/bash</string><string>{START_SCRIPT}</string></array>
    <key>RunAtLoad</key><true/>
    <key>WorkingDirectory</key><string>{BASE_DIR}</string>
</dict>
</plist>"""
            plist_path.write_text(plist_content)
        else:
            if plist_path.exists(): plist_path.unlink()

# ── Postprocesado ─────────────────────────────────────────────────────────────
def refine_alpha(img_rgba):
    """
    Limpia el canal alpha de BiRefNet:
    - Rellena huecos internos pequeños (closing morfológico)
    - Elimina ruido exterior (opening morfológico leve)
    - Suaviza bordes con Gaussian controlado
    - Garantiza que interiores sólidos queden a 255 y exteriores a 0
    """
    from PIL import ImageFilter, ImageChops
    import numpy as np
    from PIL import Image as PILImage

    r, g, b, a = img_rgba.split()
    a_arr = np.array(a, dtype=np.float32)

    # 1. Closing: dilatar luego erosionar → rellena huecos internos
    a_pil = PILImage.fromarray(a_arr.astype(np.uint8))
    a_dilated  = a_pil.filter(ImageFilter.MaxFilter(5))
    a_closed   = PILImage.fromarray(np.array(a_dilated)).filter(ImageFilter.MinFilter(5))

    # 2. Opening leve: erosionar luego dilatar → quita islas de ruido exteriores
    a_eroded   = a_pil.filter(ImageFilter.MinFilter(3))
    a_opened   = PILImage.fromarray(np.array(a_eroded)).filter(ImageFilter.MaxFilter(3))

    a_closed_arr = np.array(a_closed, dtype=np.float32)
    a_opened_arr = np.array(a_opened, dtype=np.float32)

    # 3. Combinar: para píxeles casi opacos usar closing; para semitransparentes usar opened
    w = np.clip(a_arr / 255.0, 0, 1)
    a_combined = w * a_closed_arr + (1 - w) * a_opened_arr

    # 4. Suavizado de bordes (no afecta centros sólidos ni exteriores limpios)
    a_smooth = PILImage.fromarray(a_combined.astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.7))
    a_sm_arr = np.array(a_smooth, dtype=np.float32)

    # 5. Hard clamp: muy opaco → 255, muy transparente → 0; zona media → suavizado
    a_final = np.where(a_arr > 220, 255.0,
              np.where(a_arr < 15,  0.0,
                       a_sm_arr))

    img_rgba.putalpha(PILImage.fromarray(a_final.astype(np.uint8)))
    return img_rgba


def postprocess(rgba_bytes):
    from PIL import Image
    import io

    s = load_settings()
    fmt     = s.get("format", "JPEG").upper()
    quality = int(s.get("quality", 85))

    img = Image.open(io.BytesIO(rgba_bytes)).convert("RGBA")

    # Refinar el alpha antes de recortar
    img = refine_alpha(img)

    bbox = img.split()[3].getbbox()

    if bbox is None:
        size = max(img.width, img.height)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    else:
        x0, y0, x1, y1 = bbox
        obj = img.crop((x0, y0, x1, y1))
        obj_w, obj_h = obj.size

        # 15% de margen en cada lado → el objeto ocupa el 70% del canvas
        canvas_size = int(round(max(obj_w, obj_h) / 0.70))
        canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))

        # Centrar el objeto exactamente
        paste_x = (canvas_size - obj_w) // 2
        paste_y = (canvas_size - obj_h) // 2
        canvas.paste(obj, (paste_x, paste_y), mask=obj)

    # Aplicar color de fondo según ajustes
    bg = s.get("bg_color", "#ffffff")

    if bg == "transparent":
        # Mantener canal alpha — forzar PNG
        res = int(s.get("resolution", 2000))
        final = canvas.resize((res, res), Image.LANCZOS)
        buf = io.BytesIO()
        final.save(buf, "PNG", optimize=True)
        return buf.getvalue(), "png"
    else:
        # Color sólido (blanco u otro hex)
        try:
            from PIL import ImageColor
            rgb = ImageColor.getrgb(bg)
        except Exception:
            rgb = (255, 255, 255)
        bg_layer = Image.new("RGB", canvas.size, rgb)
        bg_layer.paste(canvas, mask=canvas.split()[3])
        res = int(s.get("resolution", 2000))
        final = bg_layer.resize((res, res), Image.LANCZOS)
        buf = io.BytesIO()
        if fmt == "PNG":
            final.save(buf, "PNG", optimize=True)
        elif fmt == "WEBP":
            final.save(buf, "WEBP", quality=quality, method=6)
        else:  # JPEG
            final.save(buf, "JPEG", quality=quality, optimize=True)
        return buf.getvalue(), fmt.lower()

# ── Worker ────────────────────────────────────────────────────────────────────
def process_worker():
    import traceback, datetime
    remover = None

    log_path = BASE_DIR / "studiocut.log"
    def log(msg):
        try:
            print(msg)
        except Exception:
            pass
        try:
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}\n")
        except Exception:
            pass

    try:
        log("[INFO] Worker thread iniciado.")
        log(f"[INFO] Sistema: {platform.system()} | Python: {sys.version.split()[0]}")
        log(f"[INFO] INBOX: {INBOX} | existe: {INBOX.exists()}")
        log(f"[INFO] PROCESSING: {PROCESSING} | existe: {PROCESSING.exists()}")
        log(f"[INFO] OUTPUT: {OUTPUT} | existe: {OUTPUT.exists()}")
    except Exception as e:
        pass

    log("[INFO] Cargando modelo rembg (birefnet-general)...")
    global model_status
    model_status = {"state": "loading", "progress": 0, "message": "Iniciando descarga del modelo..."}

    try:
        from rembg import new_session, remove
        import tqdm as tqdm_module
        import tqdm.auto

        # Patch tqdm to capture download progress
        _orig_tqdm = tqdm_module.tqdm
        class LogTqdm(_orig_tqdm):
            def update(self, n=1):
                super().update(n)
                try:
                    if self.total and self.total > 0:
                        pct = int(self.n / self.total * 100)
                        mb_done = round(self.n / 1024 / 1024, 1)
                        mb_total = round(self.total / 1024 / 1024, 1)
                        msg = f"Descargando modelo: {mb_done} MB / {mb_total} MB ({pct}%)"
                        model_status["progress"] = pct
                        model_status["message"] = msg
                        log(f"[INFO] {msg}")
                except Exception:
                    pass

        tqdm_module.tqdm = LogTqdm
        tqdm_module.auto.tqdm = LogTqdm
        try:
            import tqdm.notebook
            tqdm_module.notebook.tqdm = LogTqdm
        except Exception:
            pass

        model_status["message"] = "Cargando sesión rembg..."
        session = new_session('birefnet-general', providers=['CUDAExecutionProvider', 'CPUExecutionProvider'])
        remover = lambda data: remove(data, session=session)
        model_status = {"state": "ready", "progress": 100, "message": "Modelo cargado correctamente."}
        log("[INFO] Modelo cargado correctamente.")
    except Exception as e:
        model_status = {"state": "error", "progress": 0, "message": str(e)}
        log(f"[WARN] rembg no disponible: {e}")

    while True:
        try:
            candidates = [p for p in INBOX.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED]
            if candidates:
                log(f"[INFO] {len(candidates)} archivo(s) en Inbox")
            for img_path in sorted(candidates):
                proc_path = PROCESSING / img_path.name
                try:
                    shutil.move(str(img_path), str(proc_path))
                except Exception as e:
                    log(f"[ERR] No se pudo mover {img_path.name}: {e}")
                    continue
                log(f"[INFO] Procesando: {proc_path.name}")
                try:
                    with open(proc_path, "rb") as f:
                        data = f.read()

                    # Resolve dynamic paths from settings
                    s = load_settings()
                    raw_dir = Path(s.get("raw_path", "").strip()) if s.get("raw_path", "").strip() else RAW
                    out_dir = Path(s.get("output_path", "").strip()) if s.get("output_path", "").strip() else OUTPUT
                    raw_dir.mkdir(parents=True, exist_ok=True)
                    out_dir.mkdir(parents=True, exist_ok=True)

                    # Save RAW copy with -RAW suffix
                    raw_name = proc_path.stem + "-RAW" + proc_path.suffix
                    try:
                        with open(raw_dir / raw_name, "wb") as f:
                            f.write(data)
                    except Exception as e:
                        log(f"[WARN] No se pudo guardar RAW: {e}")

                    if remover:
                        rgba = remover(data)
                        result, ext = postprocess(rgba)
                    else:
                        result, ext = data, proc_path.suffix.lstrip(".")
                    out_name = proc_path.stem + "_nobg." + ext
                    with open(out_dir / out_name, "wb") as f:
                        f.write(result)
                    try:
                        proc_path.unlink()
                    except Exception:
                        pass
                    log(f"[OK] {out_name} | RAW: {raw_name}")
                except Exception as e:
                    log(f"[ERR] Error procesando {proc_path.name}: {e}")
                    traceback.print_exc()
                    try:
                        shutil.move(str(proc_path), str(OUTPUT / (proc_path.stem + "_error" + proc_path.suffix)))
                    except Exception:
                        pass
        except Exception as e:
            log(f"[ERR] worker loop: {e}")
            try:
                import io as _io
                buf = _io.StringIO()
                traceback.print_exc(file=buf)
                log(buf.getvalue())
            except Exception:
                pass
        time.sleep(2)


import random
import string

def _gen_name():
    """Genera un nombre único tipo XXXX-00 (4 letras + 2 dígitos)."""
    while True:
        letters = ''.join(random.choices(string.ascii_uppercase, k=4))
        digits  = ''.join(random.choices(string.digits, k=2))
        name    = f"{letters}-{digits}"
        # Evitar colisiones con archivos existentes en cualquier carpeta
        if not any((d / (name + ext)).exists() for d in (INBOX, PROCESSING, OUTPUT) for ext in ALLOWED):
            return name

# ── Rutas ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload():
    files = request.files.getlist("images")
    if not files: return jsonify({"error": "No files"}), 400
    saved = []
    for f in files:
        if not f.filename: continue
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED: continue
        unique = _gen_name() + ext
        f.save(str(INBOX / unique))
        saved.append(unique)
    if not saved: return jsonify({"error": "Formato no permitido"}), 400
    return jsonify({"queued": saved, "count": len(saved)})

@app.route("/status")
def status():
    try:
        inbox      = [p.name for p in INBOX.iterdir()      if p.suffix.lower() in ALLOWED]
        processing = [p.name for p in PROCESSING.iterdir() if p.suffix.lower() in ALLOWED]
        output     = sorted([p.name for p in OUTPUT.iterdir()], reverse=True)
        return jsonify({"inbox": inbox, "processing": processing, "output": output,
                        "counts": {"inbox": len(inbox), "processing": len(processing), "output": len(output)}})
    except Exception as e:
        return jsonify({"error": str(e), "inbox": [], "processing": [], "output": [], "counts": {"inbox":0,"processing":0,"output":0}}), 500

@app.route("/output/<filename>")
def get_output(filename):
    return send_from_directory(str(OUTPUT), filename)

@app.route("/ping")
def ping():
    return "", 204

@app.route("/local-ip")
def local_ip():
    import socket
    try:
        # Conectar a una IP externa (sin enviar datos) para averiguar qué interfaz usa el SO
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "127.0.0.1"
    return jsonify({"ip": ip, "url": f"http://{ip}:5000"})

@app.route("/delete/<filename>", methods=["DELETE"])
def delete_output(filename):
    path = OUTPUT / secure_filename(filename)
    if path.exists():
        path.unlink()
        return jsonify({"deleted": filename})
    return jsonify({"error": "No encontrado"}), 404

@app.route("/settings", methods=["GET"])
def get_settings():
    try:
        s = load_settings()
        s["autostart"] = get_autostart_status()
        s["platform"]  = platform.system()
        s["output_path_resolved"] = str(Path(s["output_path"]).resolve()) if s.get("output_path","").strip() else str(OUTPUT.resolve())
        s["raw_path_resolved"]    = str(Path(s["raw_path"]).resolve())    if s.get("raw_path","").strip()    else str(RAW.resolve())
        return jsonify(s)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/settings", methods=["POST"])
def post_settings():
    global settings
    data = request.json or {}
    s = load_settings()
    if "format"  in data: s["format"]  = data["format"]
    if "quality" in data: s["quality"] = int(data["quality"])
    if "autostart" in data:
        try: set_autostart(bool(data["autostart"]))
        except Exception as e: return jsonify({"error": str(e)}), 500
        s["autostart"] = bool(data["autostart"])
    if "bg_color" in data: s["bg_color"] = data["bg_color"]
    if "output_path" in data:
        p = data["output_path"].strip()
        s["output_path"] = p if p else ""
    if "raw_path" in data:
        p = data["raw_path"].strip()
        s["raw_path"] = p if p else ""
    if "resolution" in data:
        try:
            r = int(data["resolution"])
            s["resolution"] = r if r > 0 else 2000
        except: pass
    save_settings(s)
    settings = s
    return jsonify({"ok": True, "settings": s})


@app.route("/model-status")
def get_model_status():
    return jsonify(model_status)

@app.route("/log")
def get_log():
    try:
        log_path = BASE_DIR / "studiocut.log"
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="replace").strip().split("\n")
            return jsonify({"lines": lines[-100:]})  # last 100 lines
        return jsonify({"lines": ["No hay logs todavía."]})
    except Exception as e:
        return jsonify({"lines": [f"Error leyendo log: {e}"]}), 500

@app.route("/stats")
def system_stats():
    try:
        import psutil
        cpu   = psutil.cpu_percent(interval=0.2)
        vm    = psutil.virtual_memory()
        if platform.system() == "Darwin":
            ram_used = round((vm.active + vm.wired) / 1024**3, 1)
            ram_pct  = round((vm.active + vm.wired) / vm.total * 100, 1)
        else:
            ram_used = round(vm.used / 1024**3, 1)
            ram_pct  = vm.percent
        ram_total = round(vm.total / 1024**3, 1)

        gpu_data = []
        try:
            import GPUtil
            for g in GPUtil.getGPUs():
                gpu_data.append({
                    "name":      g.name,
                    "load":      round(g.load * 100, 1),
                    "mem_used":  round(g.memoryUsed  / 1024, 1),
                    "mem_total": round(g.memoryTotal / 1024, 1),
                    "mem_pct":   round(g.memoryUsed / g.memoryTotal * 100, 1) if g.memoryTotal else 0,
                    "temp":      g.temperature,
                })
        except Exception:
            pass

        try:
            cpu_temp = None
            temps = psutil.sensors_temperatures() if hasattr(psutil, "sensors_temperatures") else {}
            for key in ("coretemp", "k10temp", "cpu_thermal"):
                if key in temps and temps[key]:
                    cpu_temp = round(temps[key][0].current, 1)
                    break
        except Exception:
            cpu_temp = None

        return jsonify({
            "cpu":  {"pct": cpu, "temp": cpu_temp},
            "ram":  {"used": ram_used, "total": ram_total, "pct": ram_pct},
            "gpu":  gpu_data,
        })
    except Exception as e:
        return jsonify({"error": str(e), "cpu": {"pct": 0, "temp": None}, "ram": {"used": 0, "total": 0, "pct": 0}, "gpu": []}), 500

# ── Arranque ──────────────────────────────────────────────────────────────────
@app.route("/browse-folder", methods=["POST"])
def browse_folder():
    try:
        import subprocess, sys
        if platform.system() == "Windows":
            script = (
                'Add-Type -AssemblyName System.Windows.Forms;'
                '$f=New-Object System.Windows.Forms.FolderBrowserDialog;'
                '$f.ShowDialog()|Out-Null;$f.SelectedPath'
            )
            result = subprocess.run(
                ["powershell", "-Command", script],
                capture_output=True, text=True
            )
            path = result.stdout.strip()
        elif platform.system() == "Darwin":
            result = subprocess.run(
                ["osascript", "-e", 'tell application "Finder" to set f to (choose folder) as string'],
                capture_output=True, text=True
            )
            path = result.stdout.strip()
            if path:
                result2 = subprocess.run(["osascript", "-e", f'POSIX path of "{path}"'],
                    capture_output=True, text=True)
                path = result2.stdout.strip()
        else:
            result = subprocess.run(
                ["zenity", "--file-selection", "--directory"],
                capture_output=True, text=True
            )
            path = result.stdout.strip()
        if path:
            return jsonify({"ok": True, "path": path})
        return jsonify({"ok": False, "path": ""})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


if __name__ == "__main__":
    # Fix Windows console UTF-8 encoding
    if platform.system() == "Windows":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
        os.system("chcp 65001 > nul 2>&1")

    t = threading.Thread(target=process_worker, daemon=True)
    t.start()
    print("StudioCut -> http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
