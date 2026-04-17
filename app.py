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
SETTINGS_FILE = BASE_DIR / "settings.json"

for d in (INBOX, PROCESSING, OUTPUT):
    d.mkdir(exist_ok=True)

ALLOWED = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}

app = Flask(__name__)
CORS(app)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

# ── Configuración global ──────────────────────────────────────────────────────
DEFAULT_SETTINGS = {
    "format":   "JPEG",
    "quality":  85,
    "autostart": False
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
        canvas = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    else:
        x0, y0, x1, y1 = bbox
        obj = img.crop((x0, y0, x1, y1))
        obj_w, obj_h = obj.size

        # 15% de margen en cada lado → el objeto ocupa el 70% del canvas
        canvas_size = int(round(max(obj_w, obj_h) / 0.70))
        canvas = Image.new("RGBA", (canvas_size, canvas_size), (255, 255, 255, 255))

        # Centrar el objeto exactamente
        paste_x = (canvas_size - obj_w) // 2
        paste_y = (canvas_size - obj_h) // 2
        canvas.paste(obj, (paste_x, paste_y), mask=obj)

    # Fondo blanco siempre — convertir a RGB antes de guardar
    final = canvas.convert("RGB").resize((2000, 2000), Image.LANCZOS)
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
    print("[INFO] Cargando modelo rembg...")
    remover = None
    try:
        from rembg import new_session, remove
        session = new_session("birefnet-general")
        remover = lambda data: remove(data, session=session)
        print("[INFO] Modelo cargado.")
    except Exception as e:
        print(f"[WARN] rembg no disponible: {e}")

    while True:
        try:
            for img_path in sorted(INBOX.iterdir()):
                if img_path.suffix.lower() not in ALLOWED:
                    continue
                proc_path = PROCESSING / img_path.name
                shutil.move(str(img_path), str(proc_path))
                print(f"[INFO] Procesando: {proc_path.name}")
                try:
                    with open(proc_path, "rb") as f:
                        data = f.read()
                    if remover:
                        rgba = remover(data)
                        result, ext = postprocess(rgba)
                    else:
                        result, ext = data, proc_path.suffix.lstrip(".")
                    out_name = proc_path.stem + "_nobg." + ext
                    with open(OUTPUT / out_name, "wb") as f:
                        f.write(result)
                    proc_path.unlink()
                    print(f"[OK] {out_name}")
                except Exception as e:
                    import traceback; traceback.print_exc()
                    shutil.move(str(proc_path), str(OUTPUT / (proc_path.stem + "_error" + proc_path.suffix)))
        except Exception as e:
            print(f"[ERR] worker: {e}")
        time.sleep(2)

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
        name   = secure_filename(f.filename)
        unique = f"{Path(name).stem}_{int(time.time()*1000)}{ext}"
        f.save(str(INBOX / unique))
        saved.append(unique)
    if not saved: return jsonify({"error": "Formato no permitido"}), 400
    return jsonify({"queued": saved, "count": len(saved)})

@app.route("/status")
def status():
    inbox      = [p.name for p in INBOX.iterdir()      if p.suffix.lower() in ALLOWED]
    processing = [p.name for p in PROCESSING.iterdir() if p.suffix.lower() in ALLOWED]
    output     = sorted([p.name for p in OUTPUT.iterdir()], reverse=True)
    return jsonify({"inbox": inbox, "processing": processing, "output": output,
                    "counts": {"inbox": len(inbox), "processing": len(processing), "output": len(output)}})

@app.route("/output/<filename>")
def get_output(filename):
    return send_from_directory(str(OUTPUT), filename)

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
    s = load_settings()
    s["autostart"] = get_autostart_status()
    s["platform"]  = platform.system()
    return jsonify(s)

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
    save_settings(s)
    settings = s
    return jsonify({"ok": True, "settings": s})

# ── Arranque ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    t = threading.Thread(target=process_worker, daemon=True)
    t.start()
    print("╔════════════════════════════════════════╗")
    print("║  StudioCut  →  http://localhost:5000   ║")
    print("╚════════════════════════════════════════╝")
    app.run(host="0.0.0.0", port=5000, debug=False)
