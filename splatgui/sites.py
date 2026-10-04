"""Icônes des sites (dossier icons/) dessinées sur les cartes SPLAT!.

- Variantes de couleur de antenne_rouge.png (antenne_bleu.png, …) créées par changement de
  teinte : la luminosité et la transparence d'origine sont conservées.
- Les repères de SPLAT! (carré rouge 7×7 + nom en rouge 10 px plus bas) peuvent être effacés
  pour laisser place aux icônes.
"""

import colorsys
import math
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen

from .storage import PROJECT_DIR
from .i18n import tr

ICONS_DIR = PROJECT_DIR / "icons"
BASE_ICON = "antenne_rouge.png"

# Couleurs des copies de antenne_rouge.png (l'original).
VARIANTS = {
    "bleu": "#1e63d6",
    "vert": "#2e9e3e",
    "orange": "#f57c00",
    "violet": "#8e24aa",
    "jaune": "#f2c200",
    "cyan": "#00acc1",
    "rose": "#e91e8c",
    "noir": "#2b2b2b",
}
# Ordre des icônes attribuées automatiquement aux émetteurs successifs.
AUTO_ORDER = [BASE_ICON] + [f"antenne_{name}.png" for name in VARIANTS]

# Anciens noms (avant renommage de antenne4.png en antenne_rouge.png).
RENAMED = {"antenne4.png": BASE_ICON, **{f"antenne4_{n}.png": f"antenne_{n}.png" for n in VARIANTS}}


def current_name(name):
    """Nom actuel d'une icône citée par d'anciens réglages."""
    return RENAMED.get(name, name)

SPLAT_RED = (255, 0, 0)


def _argb_array(image):
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    ptr = image.constBits()
    ptr.setsize(image.sizeInBytes())
    arr = np.frombuffer(ptr, np.uint8).reshape(image.height(), image.bytesPerLine() // 4, 4)
    return arr[:, : image.width()].copy()


def _from_array(arr):
    h, w = arr.shape[:2]
    arr = np.ascontiguousarray(arr)
    return QImage(arr.data, w, h, w * 4, QImage.Format.Format_ARGB32).copy()


def recolor(image, color):
    """Remplace la teinte de l'icône par celle de `color` ; un pixel de la couleur dominante
    d'origine (rouge pur) devient exactement `color`, les pixels plus clairs le restent."""
    arr = _argb_array(image).astype(np.float32) / 255.0
    b, g, r = arr[..., 0], arr[..., 1], arr[..., 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    s = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    v = mx
    target = QColor(color)
    th, ts, tv = colorsys.rgb_to_hsv(target.redF(), target.greenF(), target.blueF())
    s2 = s * ts
    v2 = v * tv + (1 - s) * v * (1 - tv)     # les zones claires (peu saturées) restent claires
    # HSV -> RGB vectorisé pour une teinte unique th.
    i = int(th * 6) % 6
    f = th * 6 - int(th * 6)
    p = v2 * (1 - s2)
    q = v2 * (1 - s2 * f)
    t = v2 * (1 - s2 * (1 - f))
    rgb = [(v2, t, p), (q, v2, p), (p, v2, t), (p, q, v2), (t, p, v2), (v2, p, q)][i]
    out = arr.copy()
    out[..., 2], out[..., 1], out[..., 0] = rgb
    return _from_array((np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8))


def ensure_variants():
    """Crée les copies colorées de antenne_rouge.png manquantes. Renvoie la liste des fichiers créés."""
    base = ICONS_DIR / BASE_ICON
    if not base.exists():
        return []
    image = QImage(str(base))
    created = []
    for name, color in VARIANTS.items():
        target = ICONS_DIR / f"antenne_{name}.png"
        if target.exists() and target.stat().st_mtime >= base.stat().st_mtime:
            continue
        if recolor(image, color).save(str(target), "PNG"):
            created.append(target.name)
    return created


def list_icons():
    if not ICONS_DIR.is_dir():
        return []
    names = [p.name for p in ICONS_DIR.glob("*.png")]
    order = {name: i for i, name in enumerate(AUTO_ORDER)}
    return sorted(names, key=lambda n: (n in order, order.get(n, 0), n.lower()))


_icon_cache = {}


def icon(name, size):
    key = (name, size)
    if key not in _icon_cache:
        image = QImage(str(ICONS_DIR / name))
        if image.isNull():
            return None
        _icon_cache[key] = image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation)
    return _icon_cache[key]


# --- Sites d'une exécution ------------------------------------------------------

def run_sites(run_dir):
    """Sites écrits par l'application dans le dossier d'exécution (tx*_*.qth, rx_*.qth)."""
    from .splat import read_qth   # import local : splat importe terrain, qui importe storage

    run_dir = Path(run_dir)
    sites = []
    for path in sorted(run_dir.glob("tx*.qth")) + sorted(run_dir.glob("rx_*.qth")):
        try:
            site = read_qth(path)
        except (OSError, ValueError):
            continue
        site["role"] = "rx" if path.name.startswith("rx_") else "tx"
        sites.append(site)
    return sites


def _site_px(ref, site):
    dx, dy = ref.pixel_size()
    return (site["lon"] - ref.lon0) / dx, (site["lat"] - ref.lat0) / dy


def remove_splat_marks(image, ref, sites):
    """Efface les repères rouges de SPLAT! (carré + nom) autour des sites. Sans effet si le
    rouge sert aussi de couleur de contour ailleurs (cartes -L)."""
    arr = _argb_array(image)
    red = (arr[..., 2] == SPLAT_RED[0]) & (arr[..., 1] == SPLAT_RED[1]) & (arr[..., 0] == SPLAT_RED[2])
    mask = np.zeros_like(red)
    h, w = red.shape
    for site in sites:
        x, y = _site_px(ref, site)
        xi, yi = int(round(x)), int(round(y))
        half_text = 4 * len(site["name"]) + 3
        # Carte déjà corrigée (pixels moins hauts) : les repères sont étirés verticalement.
        dx, dy = ref.pixel_size()
        ky = abs(dx / dy) if dy else 1.0
        for x0, x1, y0, y1 in ((xi - 4, xi + 4, yi - 4 * ky, yi + 4 * ky),
                               (xi - half_text, xi + half_text, yi + 8 * ky, yi + 24 * ky)):
            y0, y1 = int(math.floor(y0)), int(math.ceil(y1))
            mask[max(0, y0):max(0, min(h, y1 + 1)), max(0, x0):max(0, min(w, x1 + 1))] = True
    marks = red & mask
    if not marks.any() or (red & ~mask).sum() > marks.sum():
        return image, False
    # Remplace chaque pixel effacé par la moyenne des pixels non rouges voisins (relief).
    ys, xs = np.nonzero(marks)
    for y, x in zip(ys, xs):
        y0, y1, x0, x1 = max(0, y - 4), min(h, y + 5), max(0, x - 4), min(w, x + 5)
        patch = arr[y0:y1, x0:x1]
        keep = ~(red[y0:y1, x0:x1])
        if keep.any():
            arr[y, x, :3] = patch[keep][:, :3].mean(axis=0)
    return _from_array(arr), True


def icon_names(sites, tx_icon, rx_icon):
    """Nom de fichier de l'icône de chaque site ("auto" : une couleur par émetteur)."""
    names, tx_index = [], 0
    for site in sites:
        if site["role"] == "tx":
            names.append(AUTO_ORDER[tx_index % len(AUTO_ORDER)] if tx_icon == "auto" else tx_icon)
            tx_index += 1
        else:
            names.append(rx_icon)
    return names


def draw_sites(image, ref, sites, tx_icon, rx_icon, size, labels):
    """Dessine les icônes (base de l'antenne au point du site) et, si demandé, les noms.
    `tx_icon` vaut "auto" pour une couleur différente par émetteur."""
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    font = QFont()
    font.setPixelSize(max(10, int(size * 0.32)))
    font.setBold(True)
    metrics = QFontMetricsF(font)
    for site, name in zip(sites, icon_names(sites, tx_icon, rx_icon)):
        x, y = _site_px(ref, site)
        pic = icon(name, size) if name else None
        if pic is None:
            painter.setPen(QPen(QColor("black"), 1))
            painter.setBrush(QColor("red"))
            painter.drawEllipse(QPointF(x, y), size / 8, size / 8)
        else:
            painter.drawImage(QRectF(x - pic.width() / 2, y - pic.height(), pic.width(), pic.height()), pic)
        if labels and site["name"]:
            text_w = metrics.horizontalAdvance(site["name"])
            path = QPainterPath()
            path.addText(x - text_w / 2, y + metrics.ascent() + 2, font, site["name"])
            painter.strokePath(path, QPen(QColor(255, 255, 255, 230), max(2.0, size / 16)))
            painter.fillPath(path, QColor(20, 20, 20))
    painter.end()
