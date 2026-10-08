"""Calques vectoriels GeoJSON (dossier geojson/) dessinés sur les cartes SPLAT!.

Les fichiers « <nom>-<tolérance>m.geojson » (ex. communes-50m.geojson) sont regroupés en un
calque par nom ; le niveau de simplification est choisi selon la taille des pixels de la carte.
Les autres fichiers .geojson forment chacun un calque.
"""

import json
import re
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QPolygonF

from .storage import PROJECT_DIR
from .i18n import N_, tr

GEOJSON_DIR = PROJECT_DIR / "geojson"
LABEL_COLOR = QColor(0, 0, 0)          # noms des entités : noir (halo blanc)
MAX_FILE_SIZE = 120 * 1024 * 1024     # au-delà, fichier trop lourd pour être chargé en mémoire
METERS_PER_DEG = 111_320.0

STYLES = {   # couleur, épaisseur (px), taille des étiquettes
    "regions": ("#6a1b9a", 3.0, 13),
    "departements": ("#0d47a1", 2.0, 11),
    "communes": ("#424242", 0.8, 8),
}
PALETTE = ["#c62828", "#2e7d32", "#ef6c00", "#00838f", "#ad1457", "#4e342e"]
LABELS = {"regions": N_("Régions"), "departements": N_("Départements"), "communes": N_("Communes")}

_LEVEL_RE = re.compile(r"^(?P<name>.+?)-(?P<tol>\d+)m$")
_cache = {}   # chemin -> liste d'entités chargées


def discover():
    """{calque: {"label", "levels": {tolérance_m: chemin}, "color", "width", "font"}}."""
    found = {}
    if not GEOJSON_DIR.is_dir():
        return found
    for path in sorted(GEOJSON_DIR.glob("*.geojson")):
        if path.stat().st_size > MAX_FILE_SIZE:
            continue
        match = _LEVEL_RE.match(path.stem)
        name, tol = (match.group("name"), int(match.group("tol"))) if match else (path.stem, 0)
        layer = found.setdefault(name, {"label": tr(LABELS.get(name, name)), "levels": {}})
        layer["levels"][tol] = path
    for index, (name, layer) in enumerate(found.items()):
        color, width, font = STYLES.get(name, (PALETTE[index % len(PALETTE)], 1.5, 9))
        layer.update(color=color, width=width, font=font)
    order = list(STYLES)
    return dict(sorted(found.items(), key=lambda kv: (order.index(kv[0]) if kv[0] in order else 99, kv[0])))


def choose_level(layer, pixel_m):
    """Fichier le plus simplifié dont la tolérance reste sous la taille d'un pixel."""
    levels = sorted(layer["levels"])
    suitable = [tol for tol in levels if tol <= pixel_m]
    return layer["levels"][suitable[-1] if suitable else levels[0]]


def _rings(geometry):
    kind, coords = geometry.get("type"), geometry.get("coordinates")
    if kind == "Polygon":
        return "poly", coords
    if kind == "MultiPolygon":
        return "poly", [ring for polygon in coords for ring in polygon]
    if kind == "LineString":
        return "line", [coords]
    if kind == "MultiLineString":
        return "line", coords
    if kind == "Point":
        return "point", [[coords]]
    if kind == "MultiPoint":
        return "point", [[c] for c in coords]
    return None, []


def load(path):
    """Entités d'un fichier GeoJSON : (nom, kind, [anneaux numpy], bbox, centre, aire)."""
    path = Path(path)
    if path in _cache:
        return _cache[path]
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    features = []
    stack = [data]
    while stack:
        item = stack.pop()
        if item.get("type") == "FeatureCollection":
            stack.extend(reversed(item.get("features", [])))
            continue
        geometry = item.get("geometry") if item.get("type") == "Feature" else item
        if not geometry:
            continue
        if geometry.get("type") == "GeometryCollection":
            stack.extend({"type": "Feature", "properties": item.get("properties"), "geometry": g}
                         for g in geometry.get("geometries", []))
            continue
        kind, rings = _rings(geometry)
        arrays = [np.asarray(r, dtype=float)[:, :2] for r in rings if len(r)]
        if not kind or not arrays:
            continue
        allpts = np.concatenate(arrays)
        lon_min, lat_min = allpts.min(axis=0)
        lon_max, lat_max = allpts.max(axis=0)
        biggest = max(arrays, key=len)
        props = item.get("properties") or {}
        name = props.get("nom") or props.get("name") or props.get("NOM") or ""
        features.append((str(name), kind, arrays, (lon_min, lat_min, lon_max, lat_max),
                         tuple(biggest.mean(axis=0)), (lon_max - lon_min) * (lat_max - lat_min)))
    _cache[path] = features
    return features


def draw(image, ref, features, color, width, font_size, labels):
    """Dessine les entités sur `image` (QImage) calée par `ref` (GeoRef)."""
    dx, dy = ref.pixel_size()
    west, south, east, north = ref.bounds()

    def to_px(arr):
        return (arr[:, 0] - ref.lon0) / dx, (arr[:, 1] - ref.lat0) / dy

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    qcolor = QColor(color)
    pen = QPen(qcolor, width)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    visible = []
    for feature in features:
        name, kind, arrays, (x0, y0, x1, y1), center, _area = feature
        if x1 < west or x0 > east or y1 < south or y0 > north:
            continue
        visible.append(feature)
        for arr in arrays:
            xs, ys = to_px(arr)
            if kind == "point":
                painter.drawEllipse(QPointF(xs[0], ys[0]), width + 2, width + 2)
                continue
            polygon = QPolygonF([QPointF(x, y) for x, y in zip(xs.tolist(), ys.tolist())])
            if kind == "poly":
                painter.drawPolygon(polygon)
            else:
                painter.drawPolyline(polygon)

    if labels and visible:
        font = QFont()
        font.setPointSizeF(font_size)
        font.setBold(True)
        metrics = QFontMetricsF(font)
        painter.setFont(font)
        halo = QPen(QColor(255, 255, 255, 220), 3)
        placed = []
        for name, kind, arrays, (x0, y0, x1, y1), (cx, cy), _area in sorted(visible, key=lambda f: -f[5]):
            if not name:
                continue
            px, py = (cx - ref.lon0) / dx, (cy - ref.lat0) / dy
            text_w, text_h = metrics.horizontalAdvance(name), metrics.height()
            span = (x1 - x0) / dx
            if kind == "poly" and span < text_w * 0.8:
                continue   # entité trop petite à cette échelle
            rect = QRectF(px - text_w / 2, py - text_h / 2, text_w, text_h)
            if not QRectF(0, 0, image.width(), image.height()).contains(rect.center()):
                continue
            if any(rect.intersects(other) for other in placed):
                continue
            placed.append(rect)
            path = QPainterPath()
            path.addText(rect.left(), rect.top() + metrics.ascent(), font, name)
            painter.strokePath(path, halo)
            painter.fillPath(path, LABEL_COLOR)
    painter.end()
