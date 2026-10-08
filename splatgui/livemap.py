"""Mode « carte en ligne » : la couverture calculée par SPLAT! affichée sur une carte OSM / IGN
interactive (tuiles chargées en ligne), avec le relief en arrière-plan.

Relief :
- « ign_estompage » : estompage IGN en ligne (France) ;
- « srtm » : ombrage calculé localement à partir des tuiles SRTM de terrain/srtm.
La carte SPLAT! (degrés : plate carrée) est reprojetée en Web Mercator par bandes horizontales.
Une page web Leaflet équivalente peut être produite pour le navigateur.
"""

import html
import json
import math
import shutil
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen, QPolygonF

from . import basemap, hillshade, layout
from .layers import LABEL_COLOR
from .mappicker import TILE, SlippyMap, to_latlon, to_pixel
from . import i18n
from .i18n import N_, tr

def estompage(light, altitude):
    """Gris d'estompage (multiplicatif) : 255 sur terrain plat, plus sombre à l'ombre."""
    flat = max(math.sin(math.radians(altitude)), 0.05)
    return (np.clip(0.10 + 0.90 * light / flat, 0, 1) * 255).astype(np.uint8)


RELIEF_SOURCES = {
    "": N_("Aucun"),
    "ign_estompage": N_("Estompage IGN (en ligne, France)"),
    "srtm": N_("Ombrage SRTM (calcul local)"),
}
SRTM_MIN_ZOOM = 8
MAX_VISIBLE_FEATURES = 4000   # au-delà, un calque est masqué (illisible à ce zoom)
STRIPS = 160


def draw_platecarree(painter, image, ref, zoom, origin_x, origin_y, strips=STRIPS):
    """Dessine une image en degrés (plate carrée, calée par `ref`) sur une surface Web Mercator
    au niveau `zoom`, dont le coin haut-gauche est le pixel mondial (origin_x, origin_y)."""
    west, south, east, north = ref.bounds()
    x0 = to_pixel(north, west, zoom)[0] - origin_x
    x1 = to_pixel(north, east, zoom)[0] - origin_x
    rows = image.height()
    step = max(1, math.ceil(rows / strips))
    lat_per_row = (north - south) / rows
    clip = painter.clipBoundingRect() if painter.hasClipping() else None
    for r0 in range(0, rows, step):
        r1 = min(rows, r0 + step)
        y_top = to_pixel(north - r0 * lat_per_row, west, zoom)[1] - origin_y
        y_bottom = to_pixel(north - r1 * lat_per_row, west, zoom)[1] - origin_y
        target = QRectF(x0, y_top, x1 - x0, y_bottom - y_top)
        if clip is not None and not clip.intersects(target):
            continue
        painter.drawImage(target, image, QRectF(0, r0, image.width(), r1 - r0))


def to_mercator_image(image, ref):
    """Reprojette une image plate carrée en Web Mercator (pour la page web). Renvoie (image,
    (sud, ouest, nord, est))."""
    west, south, east, north = ref.bounds()
    zoom = max(1, min(18, round(math.log2(image.width() * 360 / max(east - west, 1e-9) / TILE))))
    ox, oy = to_pixel(north, west, zoom)
    width = max(1, round(to_pixel(north, east, zoom)[0] - ox))
    height = max(1, round(to_pixel(south, west, zoom)[1] - oy))
    out = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(QColor(0, 0, 0, 0))
    painter = QPainter(out)
    draw_platecarree(painter, image, ref, zoom, ox, oy, strips=height)
    painter.end()
    return out, (south, west, north, east)


class LiveMap(SlippyMap):
    """Carte interactive : fond en ligne + relief + couverture SPLAT! + sites + légende."""

    hoveredText = pyqtSignal(str)
    statusText = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__("osm", 46.6, 2.4, 6, parent)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.base = "osm"
        self.base_opacity = 1.0
        self.relief = ""
        self.relief_opacity = 0.5
        self.shade_params = (315, 45, 3)
        self.coverage = None
        self.coverage_ref = None
        self.coverage_opacity = 0.8
        self.coverage_visible = True
        self.legend_entries = []
        self.vectors = []            # [{"key", "features", "color", "width", "font", "labels"}]
        self.vector_opacity = 0.9
        self._vector_sets = {}       # signature -> calques (pour le calcul des tuiles)
        self._vector_index = {}      # clé de calque -> index (emprises, centres, noms)
        self._compute_pool = ThreadPoolExecutor(max_workers=2)   # tuiles calculées (vecteurs, SRTM)
        self._label_cache = {}
        self._hidden_layers = []
        self.legend_scale = 1.0
        self.srtm_url = ""            # modèle d'URL des tuiles SRTM (téléchargement à la volée)
        self.show_legend = True
        self.show_scale = True
        self.hovered.connect(self._hover)

    # Réglages -------------------------------------------------------------------

    def set_layers(self, base, base_opacity, relief, relief_opacity, shade_params):
        if base and base != self.base:
            self.set_source(base)
        self.base = base
        self.base_opacity = base_opacity
        self.relief = relief
        self.relief_opacity = relief_opacity
        self.shade_params = tuple(shade_params)
        self.update()

    def set_coverage(self, image, ref, opacity, visible):
        self.coverage, self.coverage_ref = image, ref
        self.coverage_opacity, self.coverage_visible = opacity, visible
        self.update()

    def set_vectors(self, vectors, opacity):
        """Calques GeoJSON : dessinés en tuiles de 256 px calculées en arrière-plan."""
        self.vectors, self.vector_opacity = vectors, opacity
        for layer in vectors:
            self._layer_index(layer)
        signature = self._vector_signature()
        if signature:
            self._vector_sets[signature] = list(vectors)
        self.update()

    def _vector_signature(self, layers=None):
        layers = self.vectors if layers is None else layers
        if not layers:
            return ""
        return "vec|" + "|".join(f"{v['key']}#{v['color']}#{v['width']}" for v in layers)

    def _shown_vectors(self):
        """(calques affichés, libellés des calques masqués car trop denses dans la vue)."""
        lat_n, lon_w = to_latlon(self.cx - self.width() / 2, self.cy - self.height() / 2, self.zoom)
        lat_s, lon_e = to_latlon(self.cx + self.width() / 2, self.cy + self.height() / 2, self.zoom)
        shown, hidden = [], []
        for layer in self.vectors:
            bb = self._layer_index(layer)["bboxes"]
            count = int(np.count_nonzero((bb[:, 2] >= lon_w) & (bb[:, 0] <= lon_e) &
                                         (bb[:, 3] >= lat_s) & (bb[:, 1] <= lat_n)))
            if count > MAX_VISIBLE_FEATURES:
                hidden.append(layer.get("label", "calque"))
            else:
                shown.append(layer)
        signature = self._vector_signature(shown)
        if signature and signature not in self._vector_sets:
            self._vector_sets[signature] = shown
        return shown, hidden

    def _layer_index(self, layer):
        """Emprises, centres et noms des entités d'un calque (tableaux numpy, calculés une fois)."""
        key = layer["key"]
        if key not in self._vector_index:
            features = layer["features"]
            bboxes = np.array([f[3] for f in features], dtype=float).reshape(-1, 4)
            centers = np.array([f[4] for f in features], dtype=float).reshape(-1, 2)
            areas = np.array([f[5] for f in features], dtype=float)
            order = np.argsort(-areas)
            self._vector_index[key] = {"bboxes": bboxes, "centers": centers[order], "order": order,
                                       "spans": (bboxes[:, 2] - bboxes[:, 0])[order],
                                       "names": [features[i][0] for i in order],
                                       "kinds": [features[i][1] for i in order]}
        return self._vector_index[key]

    def _render_vector_tile(self, signature, z, x, y):
        """Tuile transparente avec les contours des entités qui la touchent."""
        image = QImage(TILE, TILE, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(0, 0, 0, 0))
        lat_n, lon_w = to_latlon(x * TILE, y * TILE, z)
        lat_s, lon_e = to_latlon((x + 1) * TILE, (y + 1) * TILE, z)
        margin = (lon_e - lon_w) * 0.02
        world = TILE * (2 ** z)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for layer in self._vector_sets.get(signature, []):
            index = self._layer_index(layer)
            bb = index["bboxes"]
            hits = np.nonzero((bb[:, 2] >= lon_w - margin) & (bb[:, 0] <= lon_e + margin) &
                              (bb[:, 3] >= lat_s - margin) & (bb[:, 1] <= lat_n + margin))[0]
            if not len(hits):
                continue
            pen = QPen(QColor(layer["color"]), layer["width"])
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            path = QPainterPath()
            for i in hits:
                _name, kind, arrays, *_rest = layer["features"][i]
                for arr in arrays:
                    lat = np.clip(arr[:, 1], -85.0511, 85.0511)
                    xs = (arr[:, 0] + 180.0) / 360.0 * world - x * TILE
                    ys = (1 - np.log(np.tan(np.radians(lat)) + 1 / np.cos(np.radians(lat))) / math.pi) / 2 \
                        * world - y * TILE
                    if kind == "point":
                        path.addEllipse(QPointF(xs[0], ys[0]), layer["width"] + 2, layer["width"] + 2)
                        continue
                    polygon = QPolygonF([QPointF(px, py) for px, py in zip(xs.tolist(), ys.tolist())])
                    if kind == "poly" and len(polygon):
                        polygon.append(polygon.first())
                    path.addPolygon(polygon)
            painter.drawPath(path)
        painter.end()
        return image

    def _tile(self, z, x, y, source=None):
        source = source or self.source
        if not (source.startswith("vec|") or source.startswith("srtm")):
            return super()._tile(z, x, y, source)
        key = (source, z, x % (2 ** z), y)
        if key in self._pixmaps:
            self._pixmaps.move_to_end(key)
            return self._pixmaps[key]
        if key not in self._pending and key not in self._missing:
            self._pending.add(key)
            self._compute_pool.submit(self._fetch, key)
        return None

    def _label_sprite(self, name, font, metrics, color, halo):
        """Étiquette (texte + halo blanc) mise en cache sous forme d'image."""
        key = (name, font.pointSizeF(), color.rgba())
        sprite = self._label_cache.get(key)
        if sprite is None:
            width = int(metrics.horizontalAdvance(name) + 6)
            height = int(metrics.height() + 4)
            sprite = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
            sprite.fill(QColor(0, 0, 0, 0))
            p = QPainter(sprite)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            text = QPainterPath()
            text.addText(3, 2 + metrics.ascent(), font, name)
            p.strokePath(text, halo)
            p.fillPath(text, color)
            p.end()
            if len(self._label_cache) > 4000:
                self._label_cache.clear()
            self._label_cache[key] = sprite
        return sprite

    def _draw_labels(self, painter, layers):
        """Noms des entités, placés sans chevauchement, des plus grandes aux plus petites."""
        left, top = self.cx - self.width() / 2, self.cy - self.height() / 2
        world = TILE * (2 ** self.zoom)
        cell = 48.0
        grid = {}          # case de la grille -> rectangles déjà placés (test de chevauchement local)

        def free(rect):
            cells = [(i, j) for i in range(int(rect.left() // cell), int(rect.right() // cell) + 1)
                     for j in range(int(rect.top() // cell), int(rect.bottom() // cell) + 1)]
            if any(rect.intersects(other) for c in cells for other in grid.get(c, ())):
                return False
            for c in cells:
                grid.setdefault(c, []).append(rect)
            return True

        for layer in layers:
            if not layer["labels"]:
                continue
            index = self._layer_index(layer)
            centers = index["centers"]
            if not len(centers):
                continue
            lat = np.clip(centers[:, 1], -85.0511, 85.0511)
            sx = (centers[:, 0] + 180.0) / 360.0 * world - left
            sy = (1 - np.log(np.tan(np.radians(lat)) + 1 / np.cos(np.radians(lat))) / math.pi) / 2 * world - top
            span_px = index["spans"] / 360.0 * world
            font = QFont()
            font.setPointSizeF(layer["font"])
            font.setBold(True)
            metrics = QFontMetricsF(font)
            char_w = metrics.averageCharWidth()
            lengths = np.array([len(n) for n in index["names"]])
            visible = np.nonzero((sx >= 0) & (sx <= self.width()) & (sy >= 0) & (sy <= self.height()) &
                                 (span_px >= lengths * char_w * 0.8))[0]
            halo = QPen(QColor(255, 255, 255, 220), 3)
            for i in visible[:600]:
                name = index["names"][i]
                if not name:
                    continue
                sprite = self._label_sprite(name, font, metrics, LABEL_COLOR, halo)
                rect = QRectF(sx[i] - sprite.width() / 2, sy[i] - sprite.height() / 2,
                              sprite.width(), sprite.height())
                if not free(rect):
                    continue
                painter.drawImage(rect.topLeft(), sprite)

    def fit(self, ref):
        """Cadre la vue sur l'emprise `ref` (zoom le plus grand qui la contient)."""
        west, south, east, north = ref.bounds()
        for zoom in range(18, 1, -1):
            x0, y0 = to_pixel(north, west, zoom)
            x1, y1 = to_pixel(south, east, zoom)
            if x1 - x0 <= self.width() * 0.95 and y1 - y0 <= self.height() * 0.95:
                break
        self.zoom = zoom
        self.cx, self.cy = (x0 + x1) / 2, (y0 + y1) / 2
        self._sync_controls()
        self.update()

    def mouseReleaseEvent(self, event):
        # Pas de point à placer dans ce mode : un clic sert seulement à se déplacer.
        if event.button() == Qt.MouseButton.LeftButton and self._press:
            self._press = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)

    def _hover(self, lat, lon):
        alt = hillshade.elevation_at(lat, lon)
        self.hoveredText.emit(f"{lat:.5f}, {lon:.5f}" + (tr(" — sol {alt:.0f} m", alt=alt) if alt is not None else ""))

    # Tuiles d'ombrage SRTM (calculées) ------------------------------------------

    def _srtm_key(self):
        return "srtm|%d|%d|%d" % self.shade_params

    def _fetch(self, key):
        source = key[0]
        if source.startswith("vec|"):
            try:
                image = self._render_vector_tile(source, key[1], key[2], key[3])
            except Exception:
                image = None
            self._signals.ready.emit((key, image))
            return
        if not source.startswith("srtm"):
            return super()._fetch(key)
        _src, z, x, y = key
        image = None
        try:
            _name, az, alt, exag = source.split("|")
            lat_n, lon_w = to_latlon(x * TILE, y * TILE, z)
            lat_s, lon_e = to_latlon((x + 1) * TILE, (y + 1) * TILE, z)
            step_lon, step_lat = (lon_e - lon_w) / TILE, (lat_n - lat_s) / TILE
            ref = basemap.GeoRef(lon_w + step_lon / 2, lat_n - step_lat / 2,
                                 lon_e - step_lon / 2, lat_s + step_lat / 2, TILE, TILE)
            if self.srtm_url:
                if hillshade.ensure_srtm(hillshade.tiles_for_bounds(lon_w, lat_s, lon_e, lat_n),
                                         self.srtm_url, self.statusText.emit):
                    self.statusText.emit(tr("Tuiles SRTM téléchargées (terrain/srtm)"))
            grid, found, _needed = hillshade.elevation_grid(ref)
            if found:
                light = hillshade.shade(grid, ref, float(az), float(alt), float(exag))
                gray = estompage(light, float(alt))
                rgba = np.empty((TILE, TILE, 4), dtype=np.uint8)
                rgba[..., 0] = rgba[..., 1] = rgba[..., 2] = gray
                rgba[..., 3] = 255
                image = QImage(rgba.data, TILE, TILE, TILE * 4, QImage.Format.Format_ARGB32).copy()
        except Exception:
            image = None
        self._signals.ready.emit((key, image))

    # Dessin --------------------------------------------------------------------------

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(255, 255, 255))
        if self.base:
            painter.setOpacity(self.base_opacity)
            self._draw_tiles(painter, self.base)
        relief = self.relief
        if relief == "srtm" and self.zoom < SRTM_MIN_ZOOM:
            relief = ""
        if relief:
            # L'estompage (gris) assombrit le fond selon le relief : mélange « produit ».
            painter.setOpacity(self.relief_opacity)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
            self._draw_tiles(painter, self._srtm_key() if relief == "srtm" else relief,
                             min(self.zoom, basemap.SOURCES.get(relief, {}).get("max_zoom", 18)))
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        if self.coverage is not None and self.coverage_visible:
            painter.setOpacity(self.coverage_opacity)
            draw_platecarree(painter, self.coverage, self.coverage_ref, self.zoom,
                             self.cx - self.width() / 2, self.cy - self.height() / 2)
        # Calques GeoJSON au-dessus des autres couches (sous les icônes et la légende).
        shown, self._hidden_layers = self._shown_vectors() if self.vectors else ([], [])
        signature = self._vector_signature(shown)
        if signature:
            painter.setOpacity(self.vector_opacity)
            self._draw_tiles(painter, signature)
            painter.setOpacity(self.vector_opacity)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._draw_labels(painter, shown)
        painter.setOpacity(1.0)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._draw_markers(painter)
        self._draw_decorations(painter)
        painter.end()

    def _draw_markers(self, painter):
        font = QFont()
        font.setPixelSize(13)
        font.setBold(True)
        metrics = QFontMetricsF(font)
        for lat, lon, icon, name in self.markers:
            p = self._screen(lat, lon)
            if icon is not None:
                painter.drawImage(QRectF(p.x() - 20, p.y() - 40, 40, 40), icon)
            path = QPainterPath()
            path.addText(p.x() - metrics.horizontalAdvance(name) / 2, p.y() + metrics.ascent() + 2, font, name)
            painter.strokePath(path, QPen(QColor(255, 255, 255, 230), 3))
            painter.fillPath(path, QColor(20, 20, 20))

    def _draw_decorations(self, painter):
        px = 12
        margin = 10
        if self.show_legend and self.legend_entries:
            lpx = px * self.legend_scale
            width, height = layout.legend_size(self.legend_entries, lpx)
            layout.draw_legend(painter, self.legend_entries,
                               QRectF(margin, self.height() - height - margin - 18, width, height), lpx)
        if self.show_scale:
            # Échelle au centre de la vue : un pixel Mercator couvre 360 / (256 × 2^z) degrés de
            # longitude, soit cos(φ) fois moins en distance réelle (calculé par draw_scale).
            lat, _lon = self.center()
            dx = 360.0 / (TILE * 2 ** self.zoom)
            ref = basemap.GeoRef(0.0, lat, dx * (self.width() - 1), lat, self.width(), 2)
            layout.draw_scale(painter, ref, self.width() - margin, self.height() - margin - 18, px,
                              self.width())
        attributions = []
        if self.base:
            attributions.append(tr(basemap.SOURCES[self.base]["attribution"]))
        if self.relief == "ign_estompage":
            attributions.append(tr(basemap.SOURCES["ign_estompage"]["attribution"]))
        elif self.relief == "srtm":
            attributions.append(tr("relief : SRTM (NASA)"))
        attributions.append(tr("couverture : SPLAT!"))
        text = " — ".join(attributions)
        small = QFont()
        small.setPixelSize(11)
        painter.setFont(small)
        m = QFontMetricsF(small)
        box = QRectF(self.width() - m.horizontalAdvance(text) - 12, self.height() - m.height() - 4,
                     m.horizontalAdvance(text) + 10, m.height() + 2)
        painter.fillRect(box, QColor(255, 255, 255, 210))
        painter.setPen(QColor(40, 40, 40))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        if self._hidden_layers:
            note = tr("Masqué à ce niveau de zoom (trop dense) : {names} — zoomez pour l'afficher",
                      names=", ".join(self._hidden_layers))
            box = QRectF(10, 10, m.horizontalAdvance(note) + 12, m.height() + 6)
            painter.fillRect(box, QColor(255, 248, 220, 230))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, note)


# --- Page web (navigateur) -------------------------------------------------------------

_LEAFLET = "https://unpkg.com/leaflet@1.9.4/dist/leaflet"


def _geojson(features, bounds):
    """FeatureCollection des entités qui recoupent `bounds` (ouest, sud, est, nord)."""
    west, south, east, north = bounds
    out = []
    for name, kind, arrays, (x0, y0, x1, y1), _center, _area in features:
        if x1 < west or x0 > east or y1 < south or y0 > north:
            continue
        coords = [[[round(float(x), 5), round(float(y), 5)] for x, y in arr] for arr in arrays]
        if kind == "poly":
            geometry = {"type": "MultiPolygon", "coordinates": [[ring] for ring in coords]}
        elif kind == "line":
            geometry = {"type": "MultiLineString", "coordinates": coords}
        else:
            geometry = {"type": "MultiPoint", "coordinates": [ring[0] for ring in coords]}
        out.append({"type": "Feature", "properties": {"nom": name}, "geometry": geometry})
    return {"type": "FeatureCollection", "features": out}


def srtm_relief(ref, shade_params, url_template, log=lambda _m: None, width=2000):
    """Ombrage du relief SRTM sur l'emprise `ref` (tuiles téléchargées si besoin), reprojeté en
    Web Mercator pour la page web. Renvoie (image, (sud, ouest, nord, est)) ou None."""
    west, south, east, north = ref.bounds()
    if url_template:
        hillshade.ensure_srtm(hillshade.tiles_for_bounds(west, south, east, north), url_template, log)
    step = (east - west) / width
    height = max(2, round((north - south) / step))
    grid_ref = basemap.GeoRef(west + step / 2, north - step / 2, east - step / 2, south + step / 2,
                              width, height)
    grid, found, _needed = hillshade.elevation_grid(grid_ref)
    if not found:
        return None
    light = hillshade.shade(grid, grid_ref, *shade_params)
    gray = estompage(light, shade_params[1])
    # Noir d'opacité (255 - gris) : même rendu qu'un mélange « produit », sans dépendre du
    # mode de fusion du navigateur ; transparent sur terrain plat.
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    rgba[..., 3] = 255 - gray
    image = QImage(rgba.data, width, height, width * 4, QImage.Format.Format_ARGB32).copy()
    return to_mercator_image(image, grid_ref)


def export_html(folder, coverage, ref, sites, icon_files, entries, base, relief, coverage_opacity, title,
                vectors=(), srtm=None, view_ref=None):
    """Écrit <folder>/index.html (Leaflet) + couverture reprojetée + icônes + calques GeoJSON
    (`vectors` : [(libellé, couleur, épaisseur, entités)], limités à l'emprise de la carte).
    Renvoie le chemin de la page."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    # Calques dans un script (une page ouverte depuis le disque ne peut pas lire un .geojson
    # local par fetch(), mais peut charger un script voisin).
    srtm_info = None
    if srtm is not None:
        srtm_image, (s_south, s_west, s_north, s_east) = srtm
        srtm_image.save(str(folder / "relief_srtm.png"), "PNG")
        srtm_info = {"bounds": [[s_south, s_west], [s_north, s_east]]}
    view_ref = view_ref or ref
    v_west, v_south, v_east, v_north = view_ref.bounds()
    vector_data = [{"label": label, "color": color, "width": width,
                    "data": _geojson(features, view_ref.bounds())}
                   for label, color, width, features in vectors]
    (folder / "calques.js").write_text("window.CALQUES = " + json.dumps(vector_data) + ";\n", encoding="utf-8")
    mercator, (south, west, north, east) = to_mercator_image(coverage, ref)
    mercator.save(str(folder / "couverture.png"), "PNG")
    markers = []
    for site, icon in zip(sites, icon_files):
        icon_name = None
        if icon and Path(icon).exists():
            icon_name = Path(icon).name
            shutil.copy2(icon, folder / icon_name)
        markers.append({"name": site["name"], "lat": site["lat"], "lon": site["lon"], "icon": icon_name})
    legend_html = []
    for index, entry in enumerate(entries):
        kind = entry[0]
        if kind == "icon":
            name = f"legende_{index}.png"
            entry[1].save(str(folder / name), "PNG")
            legend_html.append(f'<img class="ico" src="{name}">{html.escape(entry[2])}')
        elif kind == "title":
            legend_html.append(f"<b>{html.escape(entry[1])}</b>")
        elif kind == "note":
            legend_html.append(f"<small>{html.escape(entry[1])}</small>")
        elif kind in ("swatch", "line"):
            color = entry[1]
            color = color.name() if isinstance(color, QColor) else "#%02x%02x%02x" % tuple(color)
            style = "height:4px;margin-top:6px" if kind == "line" else "height:12px"
            legend_html.append(f'<span class="sw" style="background:{color};{style}"></span>{html.escape(entry[2])}')
    layers = {key: {"url": s["url"], "attribution": tr(s["attribution"]), "max": s["max_zoom"], "label": tr(s["label"])}
              for key, s in basemap.SOURCES.items()}
    page = f"""<!doctype html>
<html lang="{i18n.language()}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="{_LEAFLET}.css">
<script src="{_LEAFLET}.js"></script>
<script src="calques.js"></script>
<style>
  html, body, #map {{ height: 100%; margin: 0; }}
  .panel {{ background: rgba(255,255,255,.92); padding: 6px 10px; border-radius: 6px;
           font: 12px/1.5 system-ui, sans-serif; box-shadow: 0 1px 4px rgba(0,0,0,.3); }}
  .sw {{ display: inline-block; width: 18px; margin-right: 6px; vertical-align: top; border: 1px solid #555; }}
  .ico {{ width: 18px; height: 18px; margin-right: 6px; vertical-align: middle; }}
  .north {{ font: bold 15px system-ui, sans-serif; text-align: center; width: 34px; line-height: 1.1; }}
  .site-label {{ font: bold 12px system-ui, sans-serif; text-shadow: 0 0 3px #fff, 0 0 3px #fff; white-space: nowrap; }}
</style></head><body><div id="map"></div>
<script>
const LAYERS = {json.dumps(layers)};
const map = L.map('map');
const make = k => L.tileLayer(LAYERS[k].url, {{maxZoom: 19, maxNativeZoom: LAYERS[k].max,
                                                attribution: LAYERS[k].attribution}});
const bases = {{}};
for (const k of ['osm', 'opentopo', 'ign_plan', 'ign_ortho']) bases[LAYERS[k].label] = make(k);
bases[LAYERS['{base if base in ("osm", "opentopo", "ign_plan", "ign_ortho") else "osm"}'].label].addTo(map);
// Volet dédié au relief, entre les fonds (200) et les surcouches (400), fusionné en « produit ».
const reliefPane = map.createPane('relief');
reliefPane.style.zIndex = 250;
reliefPane.style.mixBlendMode = 'multiply';
const relief = L.tileLayer(LAYERS.ign_estompage.url, {{maxZoom: 19, maxNativeZoom: 18, opacity: 0.5,
                           pane: 'relief', attribution: LAYERS.ign_estompage.attribution}});
if ({json.dumps(relief == "ign_estompage")}) relief.addTo(map);
const srtm = {json.dumps(srtm_info)};
const srtmLayer = srtm ? L.imageOverlay('relief_srtm.png', srtm.bounds, {{opacity: 0.7, pane: 'relief',
                                         attribution: 'relief : SRTM (NASA)'}}) : null;
if (srtmLayer && {json.dumps(relief == "srtm")}) srtmLayer.addTo(map);
const bounds = [[{south}, {west}], [{north}, {east}]];
const coverage = L.imageOverlay('couverture.png', bounds, {{opacity: {coverage_opacity:.2f},
                                attribution: 'couverture : SPLAT!'}}).addTo(map);
const overlays = {{{json.dumps(tr('Estompage du relief (IGN)'))}: relief}};
if (srtmLayer) overlays[{json.dumps(tr('Ombrage du relief (SRTM)'))}] = srtmLayer;
const control = L.control.layers(bases, overlays, {{collapsed: false}}).addTo(map);
// Calques GeoJSON au-dessus des autres couches (overlayPane : 400), sous les marqueurs (600).
map.createPane('calques').style.zIndex = 450;
for (const v of (window.CALQUES || [])) {{
  const layer = L.geoJSON(v.data, {{pane: 'calques', style: {{color: v.color, weight: v.width, fill: false}},
    onEachFeature: (f, l) => f.properties.nom && l.bindTooltip(f.properties.nom, {{sticky: true}})}});
  layer.addTo(map); control.addOverlay(layer, v.label);
}}
control.addOverlay(coverage, {json.dumps(tr('Couverture radio (SPLAT!)'))});
for (const m of {json.dumps(markers)}) {{
  const icon = m.icon ? L.icon({{iconUrl: m.icon, iconSize: [36, 36], iconAnchor: [18, 36]}}) : new L.Icon.Default();
  L.marker([m.lat, m.lon], {{icon}}).addTo(map).bindTooltip(m.name, {{permanent: true, direction: 'bottom',
                                                                      className: 'site-label'}});
}}
L.control.scale({{metric: true, imperial: false}}).addTo(map);
const legend = L.control({{position: 'bottomleft'}});
legend.onAdd = () => {{
  const d = L.DomUtil.create('div', 'panel');
  d.innerHTML = {json.dumps("<br>".join(legend_html))} +
    '<br><label>{html.escape(tr('Opacité de la couverture'))} <input id="op" type="range" min="0" max="100" value="{round(coverage_opacity * 100)}"></label>';
  L.DomEvent.disableClickPropagation(d);
  return d;
}};
legend.addTo(map);
const north = L.control({{position: 'topleft'}});
north.onAdd = () => {{ const d = L.DomUtil.create('div', 'panel north'); d.innerHTML = 'N<br>▲'; return d; }};
north.addTo(map);
document.getElementById('op').addEventListener('input', e => coverage.setOpacity(e.target.value / 100));
map.fitBounds([[{v_south}, {v_west}], [{v_north}, {v_east}]]);
</script></body></html>
"""
    path = folder / "index.html"
    path.write_text(page, encoding="utf-8")
    return path
