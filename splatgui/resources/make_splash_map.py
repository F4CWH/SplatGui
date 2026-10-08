"""Régénère le fond de l'écran de démarrage (splash_map.png) : calcul SPLAT! de champ
électrique sur l'Île-de-France, rendu par l'application (relief SRTM, couverture), limites des
sans limites administratives, mis à la taille finale (960 × 540).

Utilisation, depuis la racine du projet :  python splatgui/resources/make_splash_map.py
(les profils, réglages et historique de l'utilisateur ne sont pas modifiés)."""
import sys, tempfile, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
T = Path(tempfile.mkdtemp(prefix="splash_"))
OUT = Path(__file__).resolve().parent / "splash_map.png"
from splatgui import storage
storage.DATA_DIR = T/"data"; storage.PROFILES_DIR = T/"data/profiles"
storage.SETTINGS_FILE = T/"data/settings.json"; storage.HISTORY_FILE = T/"data/history.json"
s = storage.load_settings(); s["runs_dir"] = str(T/"runs")
s["overlay"] = {"source": "", "display_mode": "image", "relief_source": "srtm", "relief_opacity": 85,
                "exaggeration": 6, "layers": [], "labels": True, "layer_opacity": 95, "layer_colors": {"regions": "#1a237e", "departements": "#283593"},
                "coverage_opacity": 55, "coverage_blur": 3, "crop": True, "frame_ratio": 16 / 9,
                "frame_width_km": 0, "legend": False, "scale": False, "site_icons": False, "hide_marks": True}
storage.save_settings(s)
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer, Qt
import splatgui.app as A
app = QApplication([]); app.setStyle("Fusion")
w = A.MainWindow(); w.resize(1500, 920); w.show()
p = w.get_params(); p["mode"] = "pathloss"; p["values"]["R"] = "200"; p["values"]["db"] = "58"
p["lrp"].update(frequency=446.0, erp=20000.0, climate=5)
p["tx_sites"] = [storage.default_site("EIFFEL", 48.8584, 2.2945, 300.0)]
w.set_params(p); w.run()
st = {"phase": 0, "t": time.time()}
def settled():
    return (not w._busy() and not w._overlay_timer.isActive() and w.basemap_worker is None and not w.tasks
            and time.time() - st["t"] > 2)
def tick():
    if st["phase"] == 0 and not w._busy():
        print(w.history[0].get("status"), flush=True)
        w.image_combo.setCurrentIndex(w.image_combo.findText("carte.ppm")); st["phase"], st["t"] = 1, time.time()
    elif st["phase"] == 1 and settled():
        img = w.current_composite
        print("composite", img.width(), img.height(), w.frame_km, flush=True)
        small = img.scaled(960, 540, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        small.save(str(OUT))
        w.close(); app.quit(); return
    QTimer.singleShot(500, tick)
QTimer.singleShot(500, tick); QTimer.singleShot(1500000, app.quit); app.exec()
