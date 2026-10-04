"""Choix des coordonnées d'un site en désignant un point sur une carte OSM / IGN.

- Carte « glissante » : tuiles Web Mercator (cache terrain/tiles, partagé avec les fonds de
  carte), glisser pour se déplacer, molette pour zoomer, clic pour placer le point.
- Recherche de lieu : Base Adresse Nationale (France) et Nominatim (OpenStreetMap).
- Altitude du sol sous le curseur lue dans les tuiles SRTM de terrain/srtm.
"""

import json
import math
import urllib.parse
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from PyQt6.QtCore import QObject, QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from . import basemap, hillshade
from .i18n import tr

TILE = 256
MIN_ZOOM, MAX_ZOOM = 2, 18


def _world(z):
    return TILE * (2 ** z)


def to_pixel(lat, lon, z):
    lat = max(min(lat, 85.0511), -85.0511)
    rad = math.radians(lat)
    x = (lon + 180.0) / 360.0 * _world(z)
    y = (1 - math.log(math.tan(rad) + 1 / math.cos(rad)) / math.pi) / 2 * _world(z)
    return x, y


def to_latlon(x, y, z):
    lon = x / _world(z) * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / _world(z)))))
    return lat, lon


def search_places(query):
    """[(libellé, lat, lon)] : adresses françaises (Base Adresse Nationale) et lieux OSM
    (Nominatim : communes, sommets, lieux-dits, partout dans le monde). Une requête contenant
    un numéro est traitée comme une adresse (BAN d'abord), sinon comme un lieu (OSM d'abord)."""
    results = []
    headers = {"User-Agent": basemap.USER_AGENT}
    q = urllib.parse.quote(query)
    sources = [
        (f"https://api-adresse.data.gouv.fr/search/?q={q}&limit=5",
         lambda d: [(f["properties"]["label"], f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0])
                    for f in d.get("features", [])]),
        (f"https://nominatim.openstreetmap.org/search?q={q}&format=json&limit=5",
         lambda d: [(item["display_name"], float(item["lat"]), float(item["lon"])) for item in d]),
    ]
    if not any(ch.isdigit() for ch in query):
        sources.reverse()
    for url, parse in sources:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
                results += parse(json.loads(r.read().decode("utf-8")))
        except (OSError, ValueError, KeyError):
            continue
    return results


class _TileSignals(QObject):
    ready = pyqtSignal(object)    # clé (source, z, x, y)


class SlippyMap(QWidget):
    """Carte déplaçable et zoomable. Émet `picked(lat, lon)` au clic et `hovered(lat, lon)`."""

    picked = pyqtSignal(float, float)
    hovered = pyqtSignal(float, float)

    def __init__(self, source, lat, lon, zoom, parent=None):
        super().__init__(parent)
        self.setMinimumSize(760, 520)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.source = source
        self.zoom = zoom
        self.cx, self.cy = to_pixel(lat, lon, zoom)
        self.point = None             # (lat, lon) choisi
        self.markers = []             # [(lat, lon, QImage ou None, nom)]
        self._pixmaps = OrderedDict()
        self._pending = set()
        self._missing = set()
        self._signals = _TileSignals()
        self._signals.ready.connect(self._tile_ready)
        self._pool = ThreadPoolExecutor(max_workers=basemap.SOURCES[source]["workers"])
        self._press = None

    def shutdown(self):
        self._pool.shutdown(wait=False, cancel_futures=True)

    def set_source(self, source):
        self.source = source
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._pool = ThreadPoolExecutor(max_workers=basemap.SOURCES[source]["workers"])
        self._pending.clear()
        self.update()

    def center_on(self, lat, lon, zoom=None):
        if zoom is not None:
            self.zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        self.cx, self.cy = to_pixel(lat, lon, self.zoom)
        self.update()

    def center(self):
        return to_latlon(self.cx, self.cy, self.zoom)

    def set_point(self, lat, lon):
        self.point = (lat, lon)
        self.update()

    # Tuiles -----------------------------------------------------------------

    def _fetch(self, key):
        source, z, x, y = key
        try:
            path = basemap.download_tile(source, z, x, y)
        except Exception:
            path = None
        self._signals.ready.emit((key, str(path) if path else None))

    def _tile_ready(self, payload):
        key, path = payload
        self._pending.discard(key)
        if isinstance(path, QImage):          # tuile calculée (ex. ombrage SRTM)
            image = path
        else:
            image = QImage(path) if path else QImage()
        if image.isNull():
            self._missing.add(key)
        else:
            self._pixmaps[key] = QPixmap.fromImage(image)
            while len(self._pixmaps) > 600:
                self._pixmaps.popitem(last=False)
        self.update()

    def _tile(self, z, x, y, source=None):
        key = (source or self.source, z, x % (2 ** z), y)
        if key in self._pixmaps:
            self._pixmaps.move_to_end(key)
            return self._pixmaps[key]
        if key not in self._pending and key not in self._missing:
            self._pending.add(key)
            self._pool.submit(self._fetch, key)
        return None

    def _parent_tile(self, z, x, y, source=None):
        """Morceau agrandi d'une tuile de niveau inférieur déjà chargée (en attendant)."""
        for up in range(1, 5):
            pz = z - up
            if pz < 0:
                break
            key = (source or self.source, pz, (x >> up) % (2 ** pz), y >> up)
            if key in self._pixmaps:
                size = TILE >> up
                sx, sy = (x % (1 << up)) * size, (y % (1 << up)) * size
                return self._pixmaps[key], QRectF(sx, sy, size, size)
        return None

    # Dessin -----------------------------------------------------------------

    def _screen(self, lat, lon):
        x, y = to_pixel(lat, lon, self.zoom)
        return QPointF(x - self.cx + self.width() / 2, y - self.cy + self.height() / 2)

    def _draw_tiles(self, painter, source=None, zoom=None):
        """Dessine les tuiles visibles d'une source (en attendant : tuile parente agrandie)."""
        zoom = self.zoom if zoom is None else zoom
        left = self.cx - self.width() / 2
        top = self.cy - self.height() / 2
        n = 2 ** zoom
        for ty in range(int(math.floor(top / TILE)), int(math.floor((top + self.height()) / TILE)) + 1):
            if not 0 <= ty < n:
                continue
            for tx in range(int(math.floor(left / TILE)), int(math.floor((left + self.width()) / TILE)) + 1):
                target = QRectF(tx * TILE - left, ty * TILE - top, TILE, TILE)
                pixmap = self._tile(zoom, tx, ty, source)
                if pixmap is not None:
                    painter.drawPixmap(target, pixmap, QRectF(0, 0, pixmap.width(), pixmap.height()))
                else:
                    parent = self._parent_tile(zoom, tx % n, ty, source)
                    if parent:
                        painter.drawPixmap(target, parent[0], parent[1])

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(225, 225, 225))
        self._draw_tiles(painter)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont()
        font.setPixelSize(13)
        font.setBold(True)
        metrics = QFontMetricsF(font)
        for lat, lon, icon, name in self.markers:
            p = self._screen(lat, lon)
            if icon is not None:
                painter.drawImage(QRectF(p.x() - 18, p.y() - 36, 36, 36), icon)
            else:
                painter.setBrush(QColor("red"))
                painter.drawEllipse(p, 5, 5)
            path = QPainterPath()
            path.addText(p.x() - metrics.horizontalAdvance(name) / 2, p.y() + metrics.ascent() + 2, font, name)
            painter.strokePath(path, QPen(QColor(255, 255, 255, 230), 3))
            painter.fillPath(path, QColor(20, 20, 20))
        if self.point:
            p = self._screen(*self.point)
            painter.setPen(QPen(QColor(255, 255, 255), 5))
            painter.drawLine(QPointF(p.x() - 14, p.y()), QPointF(p.x() + 14, p.y()))
            painter.drawLine(QPointF(p.x(), p.y() - 14), QPointF(p.x(), p.y() + 14))
            painter.setPen(QPen(QColor(220, 0, 0), 2))
            painter.drawLine(QPointF(p.x() - 14, p.y()), QPointF(p.x() + 14, p.y()))
            painter.drawLine(QPointF(p.x(), p.y() - 14), QPointF(p.x(), p.y() + 14))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(p, 7, 7)
        attribution = tr(basemap.SOURCES[self.source]["attribution"])
        small = QFont()
        small.setPixelSize(11)
        painter.setFont(small)
        m = QFontMetricsF(small)
        box = QRectF(self.width() - m.horizontalAdvance(attribution) - 10, self.height() - m.height() - 4,
                     m.horizontalAdvance(attribution) + 8, m.height() + 2)
        painter.fillRect(box, QColor(255, 255, 255, 200))
        painter.setPen(QColor(40, 40, 40))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, attribution)
        painter.end()

    # Souris ------------------------------------------------------------------

    def _latlon_at(self, pos):
        return to_latlon(self.cx + pos.x() - self.width() / 2, self.cy + pos.y() - self.height() / 2, self.zoom)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = (event.position(), self.cx, self.cy, False)

    def mouseMoveEvent(self, event):
        self.hovered.emit(*self._latlon_at(event.position()))
        if self._press:
            start, cx, cy, moved = self._press
            delta = event.position() - start
            if moved or abs(delta.x()) + abs(delta.y()) > 4:
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.cx, self.cy = cx - delta.x(), cy - delta.y()
                self._press = (start, cx, cy, True)
                self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._press:
            moved = self._press[3]
            self._press = None
            self.setCursor(Qt.CursorShape.CrossCursor)
            if not moved:
                lat, lon = self._latlon_at(event.position())
                self.set_point(lat, lon)
                self.picked.emit(lat, lon)

    def wheelEvent(self, event):
        step = 1 if event.angleDelta().y() > 0 else -1
        new_zoom = max(MIN_ZOOM, min(MAX_ZOOM, self.zoom + step))
        if new_zoom == self.zoom:
            return
        # Zoom autour du point sous la souris.
        pos = event.position()
        lat, lon = self._latlon_at(pos)
        self.zoom = new_zoom
        x, y = to_pixel(lat, lon, new_zoom)
        self.cx, self.cy = x - pos.x() + self.width() / 2, y - pos.y() + self.height() / 2
        self.update()


class SitePickerDialog(QDialog):
    """Désignation d'un point sur une carte ; renvoie la latitude et la longitude choisies."""

    def __init__(self, title, lat, lon, markers, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.settings = settings
        picker = settings.setdefault("picker", {})
        source = picker.get("source", "osm")
        if source not in basemap.SOURCES:
            source = "osm"

        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Adresse, commune, lieu-dit, sommet…"))
        self.search.returnPressed.connect(self._search)
        search_button = QPushButton(tr("Rechercher"))
        search_button.clicked.connect(self._search)
        self.results = QComboBox()
        self.results.setMinimumWidth(260)
        self.results.activated.connect(self._go_result)
        self.source = QComboBox()
        for key, item in basemap.basemap_sources().items():
            self.source.addItem(tr(item["label"]), key)
        self.source.setCurrentIndex(max(0, self.source.findData(source)))

        top = QHBoxLayout()
        top.addWidget(self.search, 2)
        top.addWidget(search_button)
        top.addWidget(self.results, 2)
        top.addWidget(QLabel(tr("Fond :")))
        top.addWidget(self.source)

        zoom = int(picker.get("zoom", 13))
        self.map = SlippyMap(source, lat, lon, zoom)
        self.map.markers = markers
        self.map.set_point(lat, lon)
        self.map.picked.connect(self._picked)
        self.map.hovered.connect(self._hovered)
        self.source.currentIndexChanged.connect(lambda _i: self.map.set_source(self.source.currentData()))

        self.lat = QDoubleSpinBox()
        self.lon = QDoubleSpinBox()
        for box, limit in ((self.lat, 90), (self.lon, 180)):
            box.setRange(-limit, limit)
            box.setDecimals(6)
            box.setSingleStep(0.001)
            box.setKeyboardTracking(False)
        self.lat.setValue(lat)
        self.lon.setValue(lon)
        self.lat.valueChanged.connect(self._typed)
        self.lon.valueChanged.connect(self._typed)
        self.altitude = QLabel()
        self.cursor_info = QLabel()
        self.cursor_info.setStyleSheet("color: gray;")
        self._update_altitude(lat, lon)

        bottom = QHBoxLayout()
        bottom.addWidget(QLabel(tr("Latitude")))
        bottom.addWidget(self.lat)
        bottom.addWidget(QLabel(tr("Longitude (Est +)")))
        bottom.addWidget(self.lon)
        bottom.addWidget(self.altitude)
        bottom.addStretch()
        bottom.addWidget(self.cursor_info)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        help_text = QLabel(tr("Clic : placer le point — glisser : se déplacer — molette : zoomer"))
        help_text.setStyleSheet("color: gray;")

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.map, 1)
        layout.addLayout(bottom)
        footer = QHBoxLayout()
        footer.addWidget(help_text)
        footer.addStretch()
        footer.addWidget(buttons)
        layout.addLayout(footer)
        self.resize(1100, 760)

    def _update_altitude(self, lat, lon):
        alt = hillshade.elevation_at(lat, lon)
        self.altitude.setText("   " + (tr("Altitude du sol : {alt:.0f} m", alt=alt) if alt is not None
                                     else tr("Altitude du sol : (tuile SRTM absente)")))

    def _picked(self, lat, lon):
        for box, value in ((self.lat, lat), (self.lon, lon)):
            box.blockSignals(True)
            box.setValue(round(value, 6))
            box.blockSignals(False)
        self._update_altitude(lat, lon)

    def _typed(self, _value):
        self.map.set_point(self.lat.value(), self.lon.value())
        self.map.center_on(self.lat.value(), self.lon.value())
        self._update_altitude(self.lat.value(), self.lon.value())

    def _hovered(self, lat, lon):
        alt = hillshade.elevation_at(lat, lon)
        self.cursor_info.setText(tr("Curseur : {lat:.5f}, {lon:.5f}", lat=lat, lon=lon) + (f" — {alt:.0f} m" if alt is not None else ""))

    def _search(self):
        text = self.search.text().strip()
        if not text:
            return
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            places = search_places(text)
        finally:
            self.unsetCursor()
        self.results.clear()
        for label, lat, lon in places:
            self.results.addItem(label, (lat, lon))
        if places:
            self._go_result(0)
        else:
            self.results.addItem(tr("(aucun résultat)"))

    def _go_result(self, index):
        data = self.results.itemData(index)
        if data:
            lat, lon = data
            self.map.center_on(lat, lon, max(self.map.zoom, 14))
            self.map.set_point(lat, lon)
            self._picked(lat, lon)

    def coordinates(self):
        return self.lat.value(), self.lon.value()

    def done(self, result):
        self.settings["picker"] = {"source": self.source.currentData(), "zoom": self.map.zoom}
        self.map.shutdown()
        super().done(result)
