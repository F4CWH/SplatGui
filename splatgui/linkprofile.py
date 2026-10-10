"""Profil de liaison point à point, calculé et tracé par SPLAT!Gui (sans gnuplot).

Le relief est lu dans les fichiers SDF utilisés par SPLAT! pour le calcul (sursol compris),
le long de l'arc de grand cercle entre l'émetteur et le récepteur. Le profil indique :
    - le relief, rehaussé de la courbure terrestre (rayon effectif = k × rayon terrestre, k = -m) ;
    - la ligne de visée entre les antennes ;
    - la première zone de Fresnel et le dégagement exigé (-fz, 60 % par défaut ; fréquence -f,
      sinon celle des paramètres ITM) ;
    - le sursol uniforme -gc, ajouté au relief hors des extrémités comme le fait SPLAT! ;
    - le dégagement minimal et les obstacles.
"""

import math
from functools import lru_cache
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPalette, QPen, QPolygonF
from PyQt6.QtWidgets import QWidget

from . import dem, terrain
from .i18n import tr

EARTH_RADIUS_M = 6371000.0
FEET = 0.3048
MILE = 1609.344


def units(profile):
    """Unités d'affichage selon -metric (calculs toujours en mètres) :
    (facteur et unité des hauteurs, facteur et unité des distances)."""
    if profile.get("metric", True):
        return 1.0, "m", 1000.0, "km"
    return FEET, "ft", MILE, "mi"


# --- Relief ---------------------------------------------------------------------------------

@lru_cache(maxsize=8)
def read_sdf(path):
    """Grille d'un fichier SDF, du nord au sud et d'ouest en est, sans la rangée nord ni la
    colonne est (ippd × ippd ; la rangée 0 correspond à la latitude nord - 1 maille)."""
    with open(path, "rb") as fh:
        values = np.array(fh.read().split()[4:], dtype=np.int32)
    ippd = math.isqrt(values.size)
    return values[:ippd * ippd].reshape(ippd, ippd)[::-1, ::-1]


def _sdf_path(sdf_dir, tile, hd):
    path = terrain.sdf_file(sdf_dir, tile, hd)
    if path.exists():
        return path
    other = Path(str(path).replace("_359_0", "_359_360"))      # tuile E000 des versions ≤ 1.2.0
    return other if other.exists() else None


def ground_elevations(lats, lons, sdf_dirs, hd):
    """Altitudes (m) aux points donnés, maille la plus proche comme SPLAT! ; 0 hors des SDF.
    `sdf_dirs` : dossiers consultés dans l'ordre (dossier du calcul, puis -d)."""
    out = np.zeros(len(lats))
    lons = ((np.asarray(lons) + 180) % 360) - 180
    tiles = np.floor(np.column_stack([lats, lons])).astype(int)
    for tile in {tuple(t) for t in tiles.tolist()}:
        mask = (tiles[:, 0] == tile[0]) & (tiles[:, 1] == tile[1])
        path = next((p for p in (_sdf_path(d, tile, hd) for d in sdf_dirs if d) if p), None)
        if path is None:
            continue
        grid = read_sdf(str(path))
        ippd = grid.shape[0]
        rows = np.rint((tile[0] + 1 - np.asarray(lats)[mask]) * ippd).astype(int) - 1     # rangée 0 = nord - 1
        cols = np.rint((lons[mask] - tile[1]) * ippd).astype(int)
        out[mask] = grid[np.clip(rows, 0, ippd - 1), np.clip(cols, 0, ippd - 1)]
    return out


# --- Géométrie ------------------------------------------------------------------------------

def great_circle(lat1, lon1, lat2, lon2, count):
    """(latitudes, longitudes, distance totale en m, azimut en degrés) de `count` points."""
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    d = 2 * math.asin(math.sqrt(math.sin((p2 - p1) / 2) ** 2
                                + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2))
    azimuth = math.degrees(math.atan2(math.sin(l2 - l1) * math.cos(p2),
                                      math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(l2 - l1))) % 360
    f = np.linspace(0, 1, count)
    if d < 1e-12:
        return np.full(count, lat1), np.full(count, lon1), 0.0, azimuth
    a, b = np.sin((1 - f) * d) / math.sin(d), np.sin(f * d) / math.sin(d)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    return (np.degrees(np.arctan2(z, np.hypot(x, y))), np.degrees(np.arctan2(y, x)), d * EARTH_RADIUS_M, azimuth)


def site_height_m(site):
    height = float(site.get("height", 0))
    return height * FEET if site.get("height_unit", "m") != "m" else height


def _number(params, key):
    try:
        return float(str(params["values"].get(key, "")).replace(",", "."))
    except ValueError:
        return None


def compute(params, tx, run_dir=None, sdf_dir=None):
    """Profil entre l'émetteur `tx` et le récepteur des paramètres ; dict de tableaux et de
    valeurs, ou None si les deux sites sont confondus."""
    rx = params["rx_site"]
    hd = params["variant"] == "hd"
    step = 30.0 if hd else 90.0
    lat1, lon1, lat2, lon2 = float(tx["lat"]), float(tx["lon"]), float(rx["lat"]), float(rx["lon"])
    _, _, length, _ = great_circle(lat1, lon1, lat2, lon2, 2)
    if length < 1.0:
        return None
    count = int(min(4000, max(50, length / step * 2)))
    lats, lons, length, azimuth = great_circle(lat1, lon1, lat2, lon2, count)
    ground = ground_elevations(lats, lons, [str(run_dir) if run_dir else None, sdf_dir], hd)
    # Sursol intégré au relief : aux extrémités (sites), SPLAT! lit le sol nu des tuiles corrigées.
    if dem.uses_dem(params) and params["clutter"].get("enabled") and params.get("auto_terrain"):
        for index, site in ((0, tx), (-1, rx)):
            classes = _site_class(site, hd)
            if classes is not None:
                ground[index] -= dem.clutter_heights(np.array([[classes]]), params["clutter"])[0, 0]
    distance = np.linspace(0, length, count)
    k = _number(params, "m") or 1.0
    bulge = distance * (length - distance) / (2 * k * EARTH_RADIUS_M)
    terrain_line = ground + bulge
    clutter = _number(params, "gc") or 0.0            # -gc : m, ou pieds sans -metric
    if clutter > 0:
        terrain_line[1:-1] += clutter if params.get("metric", True) else clutter * FEET
    fz = _number(params, "fz")
    fz = fz / 100 if fz and fz > 0 else 0.6
    h_tx, h_rx = site_height_m(tx), site_height_m(rx)
    a_tx, a_rx = ground[0] + h_tx, ground[-1] + h_rx
    los = a_tx + (a_rx - a_tx) * distance / length
    frequency = _number(params, "f") or (float(params["lrp"]["frequency"]) if params["lrp"].get("enabled") else None)
    if frequency:
        wavelength = 299.792458 / frequency                       # m (fréquence en MHz)
        fresnel = np.sqrt(wavelength * distance * (length - distance) / length)
    else:
        fresnel = np.zeros(count)
    clearance = los - terrain_line
    inner = slice(1, -1)
    worst = int(np.argmin(clearance[inner])) + 1 if count > 2 else 0
    ratio = np.where(fresnel > 0, clearance / np.where(fresnel > 0, fresnel, 1), np.inf)
    worst_ratio = int(np.argmin(ratio[inner])) + 1 if count > 2 and frequency else worst
    obstructed = clearance[inner] < 0
    tilt = math.degrees(math.atan2(a_rx - a_tx - length ** 2 / (2 * k * EARTH_RADIUS_M), length))
    return {
        "distance": distance, "ground": ground, "terrain": terrain_line, "bulge": bulge, "los": los,
        "fresnel": fresnel, "clearance": clearance, "length": length, "azimuth": azimuth, "k": k,
        "frequency": frequency, "tilt": tilt, "tx": tx, "rx": rx, "h_tx": h_tx, "h_rx": h_rx,
        "metric": bool(params.get("metric", True)), "fz": fz,
        "worst": worst, "worst_ratio": worst_ratio,
        "min_clearance": float(clearance[worst]),
        "min_ratio": float(ratio[worst_ratio]) if frequency else None,
        "obstructions": int(np.count_nonzero(obstructed)),
        "first_obstruction": float(distance[inner][obstructed][0]) if obstructed.any() else None,
    }


def _site_class(site, hd):
    """Classe WorldCover à l'emplacement d'un site (grilles déjà en cache), ou None."""
    lat, lon = float(site["lat"]), ((float(site["lon"]) + 180) % 360) - 180
    tile = (math.floor(lat), math.floor(lon))
    if not dem._cache_path("worldcover", tile, hd, ".cls.gz").exists():
        return None
    classes = dem.worldcover_classes(tile, hd, lambda _text: None, lambda: False)
    n = classes.shape[0]
    return classes[int(round((tile[0] + 1 - lat) * (n - 1))), int(round((lon - tile[1]) * (n - 1)))]


def verdict(profile):
    """(texte, couleur) : état de la liaison d'après le dégagement."""
    if profile["obstructions"]:
        return tr("Visibilité obstruée"), QColor(192, 57, 43)
    ratio = profile["min_ratio"]
    if ratio is None:
        return tr("Visibilité directe"), QColor(30, 132, 73)
    if ratio >= profile.get("fz", 0.6):
        return (tr("Dégagée (≥ {pct:g} % de la 1re zone de Fresnel)", pct=profile.get("fz", 0.6) * 100),
                QColor(30, 132, 73))
    return tr("Visibilité directe, Fresnel partiellement obstruée"), QColor(211, 132, 0)


# --- Tracé ----------------------------------------------------------------------------------

class ProfileView(QWidget):
    """Graphe du profil ; le survol affiche distance, altitude et dégagement."""

    hovered = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.profile = None
        self.cursor_index = None
        self.setMouseTracking(True)
        self.setMinimumHeight(240)

    def set_profile(self, profile):
        self.profile = profile
        self.cursor_index = None
        self.update()

    # Repère ------------------------------------------------------------------------
    def _frame(self, width, height):
        p = self.profile
        metrics = QFontMetrics(self.font())
        left, right, top, bottom = metrics.horizontalAdvance("00000 m") + 14, 16, 34, metrics.height() + 26
        upper = np.maximum(p["los"], p["terrain"] + 0) + p["fresnel"]
        y_min = float(min(p["terrain"].min(), (p["los"] - p["fresnel"]).min()))
        y_max = float(max(upper.max(), p["terrain"].max()))
        pad = max(5.0, (y_max - y_min) * 0.08)
        y_min, y_max = math.floor((y_min - pad) / 10) * 10, math.ceil((y_max + pad) / 10) * 10
        rect = QRectF(left, top, max(10, width - left - right), max(10, height - top - bottom))
        return rect, y_min, y_max

    def _point(self, rect, y_min, y_max, distance, value):
        x = rect.left() + rect.width() * distance / self.profile["length"]
        y = rect.bottom() - rect.height() * (value - y_min) / (y_max - y_min)
        return QPointF(x, y)

    def paintEvent(self, _event):
        painter = QPainter(self)
        self.render_to(painter, self.width(), self.height())

    def render_to(self, painter, width, height):
        palette = self.palette()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(QRectF(0, 0, width, height), palette.color(QPalette.ColorRole.Base))
        text_color = palette.color(QPalette.ColorRole.Text)
        if not self.profile:
            painter.setPen(text_color)
            painter.drawText(QRectF(0, 0, width, height), Qt.AlignmentFlag.AlignCenter,
                             tr("Profil disponible après un calcul point à point."))
            return
        p = self.profile
        rect, y_min, y_max = self._frame(width, height)
        point = lambda d, v: self._point(rect, y_min, y_max, d, v)
        d = p["distance"]

        # Grille et graduations
        grid_pen = QPen(palette.color(QPalette.ColorRole.Mid), 1, Qt.PenStyle.DotLine)
        painter.setFont(self.font())
        h_factor, h_unit, d_factor, d_unit = units(p)
        for value in _ticks(y_min / h_factor, y_max / h_factor, 6):        # graduations en m ou en ft
            y = point(0, value * h_factor).y()
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            painter.setPen(text_color)
            painter.drawText(QRectF(0, y - 8, rect.left() - 6, 16),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{value:g} {h_unit}")
        # Distances : km / mi, ou m / ft pour une liaison courte.
        unit, label = (d_factor, d_unit) if p["length"] >= 2 * d_factor else (h_factor, h_unit)
        for value in _ticks(0, p["length"] / unit, 8):
            x = point(value * unit, y_min).x()
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            painter.setPen(text_color)
            painter.drawText(QRectF(x - 40, rect.bottom() + 4, 80, 16), Qt.AlignmentFlag.AlignHCenter,
                             f"{value:g} {label}")

        # Zone de Fresnel (1re zone et dégagement exigé -fz)
        if p["frequency"]:
            upper = [point(x, v) for x, v in zip(d, p["los"] + p["fresnel"])]
            lower = [point(x, v) for x, v in zip(d[::-1], (p["los"] - p["fresnel"])[::-1])]
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(63, 127, 208, 45))
            painter.drawPolygon(QPolygonF(upper + lower))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(63, 127, 208, 160), 1, Qt.PenStyle.DashLine))
            for sign in (1, -1):
                painter.drawPolyline(QPolygonF([point(x, v) for x, v in zip(d, p["los"] + sign * p["fz"] * p["fresnel"])]))

        # Relief (rehaussé de la courbure) et courbure seule
        ground = QPainterPath(point(d[0], y_min))
        for x, v in zip(d, p["terrain"]):
            ground.lineTo(point(x, v))
        ground.lineTo(point(d[-1], y_min))
        ground.closeSubpath()
        painter.setPen(QPen(QColor(110, 84, 50), 1.2))
        painter.setBrush(QColor(176, 146, 96, 200))
        painter.drawPath(ground)
        painter.setPen(QPen(QColor(120, 120, 120), 1, Qt.PenStyle.DashLine))
        painter.drawPolyline(QPolygonF([point(x, y_min + v) for x, v in zip(d, p["bulge"])]))

        # Ligne de visée et antennes
        status, color = verdict(p)
        painter.setPen(QPen(color, 2))
        painter.drawLine(point(0, p["los"][0]), point(p["length"], p["los"][-1]))
        painter.setPen(QPen(text_color, 2))
        for x, base, top_ in ((0, p["terrain"][0], p["los"][0]), (p["length"], p["terrain"][-1], p["los"][-1])):
            painter.drawLine(point(x, base), point(x, top_))
        if p["obstructions"] or (p["min_ratio"] is not None and p["min_ratio"] < p["fz"]):
            worst = p["worst"] if p["obstructions"] else p["worst_ratio"]
            painter.setPen(QPen(color, 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(point(d[worst], p["terrain"][worst]), 5, 5)

        # En-tête : sites
        bold = QFont(self.font())
        bold.setBold(True)
        painter.setFont(bold)
        painter.setPen(text_color)
        painter.drawText(QRectF(rect.left(), 6, rect.width() / 2, 20), Qt.AlignmentFlag.AlignLeft,
                         f"{p['tx']['name']} ({p['h_tx'] / h_factor:.4g} {h_unit})")
        painter.drawText(QRectF(rect.center().x(), 6, rect.width() / 2, 20), Qt.AlignmentFlag.AlignRight,
                         f"{p['rx']['name']} ({p['h_rx'] / h_factor:.4g} {h_unit})")
        painter.setFont(self.font())

        # Curseur
        if self.cursor_index is not None:
            i = self.cursor_index
            x = point(d[i], y_min).x()
            painter.setPen(QPen(palette.color(QPalette.ColorRole.Highlight), 1))
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            painter.setBrush(palette.color(QPalette.ColorRole.Highlight))
            painter.drawEllipse(point(d[i], p["terrain"][i]), 3, 3)

    def mouseMoveEvent(self, event):
        if not self.profile:
            return
        rect, _, _ = self._frame(self.width(), self.height())
        f = (event.position().x() - rect.left()) / rect.width()
        if not 0 <= f <= 1:
            self.leaveEvent(None)
            return
        p = self.profile
        i = int(round(f * (len(p["distance"]) - 1)))
        self.cursor_index = i
        h_factor, h_unit, d_factor, d_unit = units(p)
        text = tr("{d:.2f} {du} — sol {g:.0f} {hu} — visée {l:.0f} {hu} — dégagement {c:+.1f} {hu}",
                  d=p["distance"][i] / d_factor, g=p["ground"][i] / h_factor, l=p["los"][i] / h_factor,
                  c=p["clearance"][i] / h_factor, du=d_unit, hu=h_unit)
        if p["frequency"] and p["fresnel"][i] > 0:
            text += tr(" ({r:.0%} de F1)", r=p["clearance"][i] / p["fresnel"][i])
        self.hovered.emit(text)
        self.update()

    def leaveEvent(self, _event):
        self.cursor_index = None
        self.hovered.emit("")
        self.update()

    def image(self, width=1600, height=700):
        """Profil rendu en image (export PNG)."""
        image = QImage(width, height, QImage.Format.Format_ARGB32)
        cursor, self.cursor_index = self.cursor_index, None
        painter = QPainter(image)
        font = QFont(self.font())
        font.setPixelSize(15)
        painter.setFont(font)
        saved = self.font()
        self.setFont(font)
        self.render_to(painter, width, height)
        painter.end()
        self.setFont(saved)
        self.cursor_index = cursor
        return image


def _ticks(low, high, count):
    """Graduations « rondes » entre low et high."""
    span = high - low
    if span <= 0:
        return [low]
    raw = span / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    start = math.ceil(low / step) * step
    return [round(start + n * step, 6) for n in range(int((high - start) / step + 1e-9) + 1)]


def summary(profile):
    """Résumé en texte riche (HTML) du profil."""
    status, color = verdict(profile)
    h_factor, h_unit, d_factor, d_unit = units(profile)
    parts = [f"<b style='color:{color.name()}'>{status}</b>",
             tr("distance {d:.2f} {du}", d=profile["length"] / d_factor, du=d_unit),
             tr("azimut {a:.1f}°", a=profile["azimuth"]),
             tr("inclinaison {t:+.2f}°", t=profile["tilt"]),
             tr("dégagement minimal {c:+.1f} {hu} à {d:.2f} {du}", c=profile["min_clearance"] / h_factor,
                d=profile["distance"][profile["worst"]] / d_factor, hu=h_unit, du=d_unit)]
    if profile["min_ratio"] is not None:
        parts.append(tr("{r:.0%} de F1 au plus défavorable", r=profile["min_ratio"]))
    if profile["frequency"]:
        parts.append(tr("{f:g} MHz", f=profile["frequency"]))
    parts.append(tr("k = {k:g}", k=profile["k"]))
    return " · ".join(parts)
