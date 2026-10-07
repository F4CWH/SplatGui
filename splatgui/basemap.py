"""Fonds de carte OSM / IGN superposés aux cartes SPLAT!.

Les cartes .ppm de SPLAT! sont en projection « plate carrée » (lon/lat linéaires), calées
par le fichier .geo écrit avec -geo. Les tuiles OSM/IGN sont en Web Mercator : on
assemble une mosaïque puis on la reprojette ligne par ligne sur la grille de la carte.

Tuiles en cache dans terrain/tiles/<source>/<z>/<x>/<y>.<ext>.
"""

import math
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter

from . import __version__
from .terrain import TERRAIN_DIR
from .i18n import N_, tr

TILES_DIR = TERRAIN_DIR / "tiles"
TILE = 256
MAX_TILES = 144
USER_AGENT = f"SPLAT!Gui/{__version__} (interface pour SPLAT!, cache local des tuiles)"

_IGN = ("https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER={layer}"
        "&STYLE=normal&TILEMATRIXSET=PM&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&FORMAT={fmt}")

SOURCES = {
    "osm": {
        "label": "OpenStreetMap",
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "ext": "png", "max_zoom": 18, "workers": 2,   # politique d'usage des serveurs OSM
        "attribution": N_("© les contributeurs d'OpenStreetMap (ODbL)"),
    },
    "ign_plan": {
        "label": N_("IGN – Plan IGN"),
        "url": _IGN.replace("{layer}", "GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2").replace("{fmt}", "image/png"),
        "ext": "png", "max_zoom": 18, "workers": 6,
        "attribution": N_("© IGN – Géoplateforme, Plan IGN"),
    },
    "ign_ortho": {
        "label": N_("IGN – Photographies aériennes"),
        "url": _IGN.replace("{layer}", "ORTHOIMAGERY.ORTHOPHOTOS").replace("{fmt}", "image/jpeg"),
        "ext": "jpg", "max_zoom": 18, "workers": 6,
        "attribution": N_("© IGN – Géoplateforme, BD ORTHO®"),
    },
    "opentopo": {
        "label": N_("OpenTopoMap (OSM + relief)"),
        "url": "https://a.tile.opentopomap.org/{z}/{x}/{y}.png",
        "ext": "png", "max_zoom": 17, "workers": 2,
        "attribution": N_("© OpenTopoMap (CC-BY-SA), © les contributeurs d'OpenStreetMap"),
    },
    # Couche de relief seulement (estompage), superposée à un fond : pas proposée comme fond.
    "ign_estompage": {
        "label": N_("IGN – Estompage du relief"),
        "url": _IGN.replace("{layer}", "ELEVATION.ELEVATIONGRIDCOVERAGE.SHADOW")
                   .replace("STYLE=normal", "STYLE=estompage_grayscale").replace("{fmt}", "image/png"),
        "ext": "png", "max_zoom": 18, "workers": 6, "relief_only": True,
        "attribution": N_("© IGN – Géoplateforme, estompage"),
    },
}


def basemap_sources():
    """Sources utilisables comme fond de carte (hors couches de relief seules)."""
    return {key: item for key, item in SOURCES.items() if not item.get("relief_only")}

SEA_RGB = (0, 0, 170)   # couleur du niveau de la mer dans les cartes SPLAT! (fait partie du relief)


class Cancelled(Exception):
    pass


# --- Géoréférencement ------------------------------------------------------------

class GeoRef:
    """Coordonnées des centres des pixels (0, 0) et (largeur-1, hauteur-1)."""

    def __init__(self, lon0, lat0, lon1, lat1, width, height):
        self.lon0, self.lat0, self.lon1, self.lat1 = lon0, lat0, lon1, lat1
        self.width, self.height = width, height

    def __repr__(self):
        return (f"GeoRef(lon {self.lon0:.4f}→{self.lon1:.4f}, lat {self.lat0:.4f}→{self.lat1:.4f}, "
                f"{self.width}×{self.height})")

    def pixel_size(self):
        """Taille d'un pixel en degrés (lon, lat)."""
        return ((self.lon1 - self.lon0) / max(self.width - 1, 1),
                (self.lat1 - self.lat0) / max(self.height - 1, 1))

    def bounds(self):
        """(ouest, sud, est, nord) des bords extérieurs de l'image."""
        dx, dy = self.pixel_size()
        return (self.lon0 - dx / 2, self.lat1 + dy / 2, self.lon1 + dx / 2, self.lat0 - dy / 2)


def _from_geo(path):
    points, size = [], None
    for line in Path(path).read_text(encoding="latin-1").splitlines():
        parts = line.split()
        if parts[:1] == ["TIEPOINT"] and len(parts) >= 5:
            points.append(tuple(float(p) for p in parts[1:5]))
        elif parts[:1] == ["IMAGESIZE"] and len(parts) >= 3:
            size = int(parts[1]), int(parts[2])
    if len(points) < 2 or not size:
        return None
    (x0, y0, lon0, lat0), (x1, y1, lon1, lat1) = points[:2]
    if x1 == x0 or y1 == y0:
        return None
    # Ramène aux pixels (0, 0) et (W-1, H-1).
    sx, sy = (lon1 - lon0) / (x1 - x0), (lat1 - lat0) / (y1 - y0)
    w, h = size
    return GeoRef(lon0 - x0 * sx, lat0 - y0 * sy, lon0 + (w - 1 - x0) * sx, lat0 + (h - 1 - y0) * sy, w, h)


def _from_kml(path, width, height):
    text = Path(path).read_text(encoding="latin-1")
    box = {}
    for key in ("north", "south", "east", "west"):
        match = re.search(rf"<{key}>\s*([-\d.]+)\s*</{key}>", text)
        if not match:
            return None
        box[key] = float(match.group(1))
    return GeoRef(box["west"], box["north"], box["east"], box["south"], width, height)


def _from_console(path, width, height):
    """Anciennes exécutions sans .geo : emprise des régions chargées par SPLAT!."""
    regions = re.findall(r'"(?:[^"]*[/\\])?(-?\d+)[_:](-?\d+)[_:](\d+)[_:](\d+)(?:-hd)?(?:\.sdf)?"',
                         Path(path).read_text(encoding="utf-8", errors="replace"))
    if not regions:
        return None
    lats = [int(r[0]) for r in regions] + [int(r[1]) for r in regions]
    wests = [int(r[2]) for r in regions] + [int(r[3]) for r in regions]
    west_lon = -max(wests)
    east_lon = -min(wests)
    if west_lon < -180:
        west_lon, east_lon = west_lon + 360, east_lon + 360
    north, south = max(lats), min(lats)
    dx, dy = (east_lon - west_lon) / width, (north - south) / height
    return GeoRef(west_lon + dx / 2, north - dy / 2, east_lon - dx / 2, south + dy / 2, width, height)


def georef_for(image_path):
    """Géoréférencement d'une carte SPLAT!, ou None (graphes, légendes…)."""
    path = Path(image_path)
    if path.suffix.lower() != ".ppm" or path.stem.endswith("-ck"):
        return None
    image = QImage(str(path))
    if image.isNull():
        return None
    width, height = image.width(), image.height()
    geo = path.with_suffix(".geo")
    if geo.exists():
        ref = _from_geo(geo)
        if ref and (ref.width, ref.height) == (width, height):
            return ref
    kml = path.with_suffix(".kml")
    if kml.exists():
        ref = _from_kml(kml, width, height)
        if ref:
            return ref
    console = path.parent / "console.log"
    if console.exists():
        return _from_console(console, width, height)
    return None


# --- Tuiles Web Mercator -------------------------------------------------------

def _merc_x(lon, z):
    return (lon + 180.0) / 360.0 * (2 ** z)


def _merc_y(lat, z):
    lat = max(min(lat, 85.0511), -85.0511)
    rad = math.radians(lat)
    return (1 - math.log(math.tan(rad) + 1 / math.cos(rad)) / math.pi) / 2 * (2 ** z)


def tile_range(ref, z):
    west, south, east, north = ref.bounds()
    x0, x1 = int(math.floor(_merc_x(west, z))), int(math.floor(_merc_x(east, z)))
    y0, y1 = int(math.floor(_merc_y(north, z))), int(math.floor(_merc_y(south, z)))
    return x0, x1, y0, y1


def choose_zoom(ref, max_zoom, max_tiles=MAX_TILES):
    """Niveau de zoom dont la résolution approche celle de la carte, dans la limite de tuiles."""
    west, _south, east, _north = ref.bounds()
    px_per_deg = ref.width / max(east - west, 1e-9)
    z = int(math.ceil(math.log2(max(px_per_deg * 360 / TILE, 1))))
    z = max(1, min(z, max_zoom))
    while z > 1:
        x0, x1, y0, y1 = tile_range(ref, z)
        if (x1 - x0 + 1) * (y1 - y0 + 1) <= max_tiles:
            break
        z -= 1
    return z


def _tile_path(source_key, z, x, y):
    return TILES_DIR / source_key / str(z) / str(x % (2 ** z)) / f"{y}.{SOURCES[source_key]['ext']}"


def download_tile(source_key, z, x, y, cancel=lambda: False):
    """Met la tuile en cache si besoin. Renvoie son chemin, ou None si hors couverture."""
    path = _tile_path(source_key, z, x, y)
    absent = path.with_suffix(".absent")
    if path.exists():
        return path
    if absent.exists():
        return None
    if cancel():
        raise Cancelled()
    url = SOURCES[source_key]["url"].format(z=z, x=x % (2 ** z), y=y)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 404):    # hors couverture (ex. IGN hors France)
            absent.touch()
            return None
        raise
    if not data:
        return None
    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path


def build_basemap(source_key, ref, log=lambda _m: None, cancel=lambda: False):
    """Fond de carte reprojeté sur la grille de la carte SPLAT! (QImage ARGB32)."""
    source = SOURCES[source_key]
    z = choose_zoom(ref, source["max_zoom"])
    x0, x1, y0, y1 = tile_range(ref, z)
    coords = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
    log(tr("{source} : zoom {z}, {n} tuile(s)", source=tr(source["label"]), z=z, n=len(coords)))

    paths = {}
    with ThreadPoolExecutor(max_workers=source["workers"]) as pool:
        futures = {pool.submit(download_tile, source_key, z, x, y, cancel): (x, y) for x, y in coords}
        try:
            for done, future in enumerate(as_completed(futures), start=1):
                paths[futures[future]] = future.result()
                if done % 8 == 0 or done == len(coords):
                    log(tr("{source} : {done}/{n} tuiles", source=tr(source["label"]), done=done, n=len(coords)))
        except BaseException:
            for future in futures:
                future.cancel()
            raise

    mosaic = QImage((x1 - x0 + 1) * TILE, (y1 - y0 + 1) * TILE, QImage.Format.Format_ARGB32_Premultiplied)
    mosaic.fill(QColor(0, 0, 0, 0))
    painter = QPainter(mosaic)
    drawn = 0
    for (x, y), path in paths.items():
        tile = QImage(str(path)) if path else QImage()
        if not tile.isNull():
            painter.drawImage((x - x0) * TILE, (y - y0) * TILE, tile)
            drawn += 1
    painter.end()
    if not drawn:
        raise RuntimeError(tr("Aucune tuile {source} disponible pour cette zone.", source=tr(source["label"])))

    # Reprojection : en x, lon et Mercator sont tous deux linéaires ; en y, une ligne
    # de la carte (latitude constante) correspond à une ligne de la mosaïque.
    out = QImage(ref.width, ref.height, QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(QColor(0, 0, 0, 0))
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    west, _s, east, _n = ref.bounds()
    sx0 = (_merc_x(west, z) - x0) * TILE
    sx1 = (_merc_x(east, z) - x0) * TILE
    dlat = (ref.lat1 - ref.lat0) / max(ref.height - 1, 1)
    for row in range(ref.height):
        lat_top = ref.lat0 + (row - 0.5) * dlat
        lat_bottom = ref.lat0 + (row + 0.5) * dlat
        sy0 = (_merc_y(lat_top, z) - y0) * TILE
        sy1 = (_merc_y(lat_bottom, z) - y0) * TILE
        painter.drawImage(QRectF(0, row, ref.width, 1), mosaic, QRectF(sx0, sy0, sx1 - sx0, sy1 - sy0))
    painter.end()
    return out


# --- Composition ----------------------------------------------------------------

def _rgba_view(image):
    """Vue numpy (H, W, 4) en ordre BGRA d'un QImage 32 bits (copie)."""
    ptr = image.constBits()
    ptr.setsize(image.sizeInBytes())
    arr = np.frombuffer(ptr, np.uint8).reshape(image.height(), image.bytesPerLine() // 4, 4)
    return arr[:, : image.width()].copy()


def coverage_layer(splat_image):
    """Calque des pixels colorés de SPLAT! (couverture, sites) ; le relief gris et la mer
    deviennent transparents."""
    image = splat_image.convertToFormat(QImage.Format.Format_ARGB32)
    arr = _rgba_view(image)
    b, g, r = arr[..., 0], arr[..., 1], arr[..., 2]
    relief = ((r == g) & (g == b)) | ((r == SEA_RGB[0]) & (g == SEA_RGB[1]) & (b == SEA_RGB[2]))
    arr[..., 3] = np.where(relief, 0, 255).astype(np.uint8)
    layer = QImage(arr.data, image.width(), image.height(), image.width() * 4, QImage.Format.Format_ARGB32)
    return layer.copy()   # détache des données numpy


def relief_layer(splat_image, coverage, elevation=None):
    """Relief seul : la carte SPLAT! dont les zones de couverture sont remplacées par le gris
    du relief qui s'y trouverait. Ce gris est déduit des altitudes SRTM (`elevation`, même
    grille que l'image) en apprenant, sur les zones visibles, la correspondance
    altitude -> gris utilisée par SPLAT! ; sans altitudes, un gris clair neutre est utilisé."""
    image = splat_image.convertToFormat(QImage.Format.Format_ARGB32)
    arr = _rgba_view(image)
    covered = _rgba_view(coverage.convertToFormat(QImage.Format.Format_ARGB32))[..., 3] > 0
    if not covered.any():
        return image
    b, g, r = arr[..., 0].astype(np.int16), arr[..., 1].astype(np.int16), arr[..., 2].astype(np.int16)
    gray = (r == g) & (g == b)
    fill = np.full(covered.sum(), 225, dtype=np.float32)
    if elevation is not None and elevation.shape == covered.shape:
        known = gray & ~covered
        if known.sum() > 500:
            elev_known, gray_known = elevation[known], r[known].astype(np.float32)
            edges = np.unique(np.percentile(elev_known, np.linspace(0, 100, 65)))
            if len(edges) > 2:
                index = np.clip(np.searchsorted(edges, elev_known) - 1, 0, len(edges) - 2)
                centers, values = [], []
                for i in range(len(edges) - 1):
                    sel = index == i
                    if sel.any():
                        centers.append(float(np.median(elev_known[sel])))
                        values.append(float(np.median(gray_known[sel])))
                fill = np.interp(elevation[covered], centers, values)
    arr[covered, 0] = arr[covered, 1] = arr[covered, 2] = np.clip(fill, 0, 255).astype(np.uint8)
    arr[..., 3] = 255
    out = QImage(arr.data, image.width(), image.height(), image.width() * 4, QImage.Format.Format_ARGB32)
    return out.copy()


def write_world_file(image_path, ref):
    """Fichier de calage .pgw (WGS84, EPSG:4326) pour ouvrir l'export dans un SIG."""
    dx, dy = ref.pixel_size()
    Path(image_path).with_suffix(".pgw").write_text(
        f"{dx:.10f}\n0\n0\n{dy:.10f}\n{ref.lon0:.10f}\n{ref.lat0:.10f}\n", encoding="ascii")



# --- Cadrage sur le tracé ---------------------------------------------------------

def content_box(coverage):
    """Rectangle (x0, y0, x1, y1) des pixels non transparents du calque de couverture."""
    image = coverage.convertToFormat(QImage.Format.Format_ARGB32)
    alpha = _rgba_view(image)[..., 3]
    ys, xs = np.nonzero(alpha)
    if not len(xs):
        return None
    # On ignore 0,5 % des pixels de chaque côté : quelques points isolés en bord de carte
    # ne doivent pas empêcher le cadrage.
    x0, x1 = np.percentile(xs, [0.5, 99.5])
    y0, y1 = np.percentile(ys, [0.5, 99.5])
    return int(x0), int(y0), int(x1), int(y1)


def crop(splat_image, ref, box, margin=0.10, target=1600, max_scale=8, min_size=60):
    """Recadre la carte autour de `box` (avec une marge) et l'agrandit sans lissage, pour
    obtenir un fond de carte plus détaillé. Renvoie (image, GeoRef) ou None si inutile."""
    x0, y0, x1, y1 = box
    width, height = x1 - x0 + 1, y1 - y0 + 1
    pad = int(max(width, height) * margin) + 4
    size = max(width, height) + 2 * pad
    size = max(size, min_size)
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    left = max(0, min(cx - size // 2, ref.width - size))
    top = max(0, min(cy - size // 2, ref.height - size))
    w, h = min(size, ref.width - left), min(size, ref.height - top)
    if w >= ref.width * 0.9 and h >= ref.height * 0.9:
        return None    # le tracé occupe déjà presque toute la carte
    return crop_box(splat_image, ref, left, top, w, h, target, max_scale)


def crop_box(splat_image, ref, left, top, w, h, target=1600, max_scale=8):
    """Extrait le rectangle de pixels (left, top, w, h), agrandi sans lissage d'un facteur
    entier pour approcher `target` pixels. Renvoie (image, GeoRef)."""
    left, top = max(0, int(left)), max(0, int(top))
    w, h = max(1, min(int(w), ref.width - left)), max(1, min(int(h), ref.height - top))
    scale = max(1, min(max_scale, target // max(w, h)))
    image = splat_image.copy(left, top, w, h).scaled(
        w * scale, h * scale, transformMode=Qt.TransformationMode.FastTransformation)
    dx, dy = ref.pixel_size()
    lon0 = ref.lon0 + dx * (left + 0.5 / scale - 0.5)
    lat0 = ref.lat0 + dy * (top + 0.5 / scale - 0.5)
    new_w, new_h = w * scale, h * scale
    return image, GeoRef(lon0, lat0, lon0 + dx / scale * (new_w - 1),
                         lat0 + dy / scale * (new_h - 1), new_w, new_h)


# --- Rapport hauteur/largeur -----------------------------------------------------

def aspect_factor(ref):
    """Rapport largeur/hauteur réel d'un pixel (en km) : 1 pour une carte aux bonnes proportions.
    Pour une carte SPLAT! (pixels carrés en degrés) il vaut cos(latitude du centre), car un
    degré de longitude ne mesure que cos(φ) × 111 km."""
    dx, dy = ref.pixel_size()
    cos_lat = max(math.cos(math.radians((ref.lat0 + ref.lat1) / 2)), 0.05)
    # 1° de longitude à l'équateur = 111,32 km ; 1° de latitude ≈ 110,574 km.
    return abs(dx) * 111.32 * cos_lat / max(abs(dy) * 110.574, 1e-12)


def correct_aspect(image, ref):
    """Comprime la carte horizontalement pour respecter les distances réelles au centre.
    Renvoie (image, GeoRef) ; le calage reste exact (même emprise, pixels plus larges)."""
    factor = aspect_factor(ref)
    width = max(1, round(image.width() * factor))
    if width == image.width():
        return image, ref
    scaled = image.scaled(width, image.height(), Qt.AspectRatioMode.IgnoreAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
    west, _south, east, _north = ref.bounds()
    dx = (east - west) / width
    return scaled, GeoRef(west + dx / 2, ref.lat0, east - dx / 2, ref.lat1, width, ref.height)


def stretch_to_proportions(image, ref):
    """Corrige les proportions en agrandissant la hauteur, sans lissage : aucune colonne n'est
    perdue et les couleurs de SPLAT! restent exactes. Renvoie (image, GeoRef)."""
    factor = aspect_factor(ref)
    if abs(factor - 1) < 0.005:
        return image, ref
    if factor > 1:   # pixels trop larges (cas rare) : on agrandit plutôt la largeur
        width, height = round(image.width() * factor), image.height()
    else:
        width, height = image.width(), round(image.height() / factor)
    scaled = image.scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio,
                          Qt.TransformationMode.FastTransformation)
    west, south, east, north = ref.bounds()
    dx, dy = (east - west) / width, (south - north) / height
    return scaled, GeoRef(west + dx / 2, north + dy / 2, east - dx / 2, south - dy / 2, width, height)


def write_geo(ppm_path, ref):
    """Fichier de calage .geo au format de SPLAT! / Xastir (relu par georef_for)."""
    lines = [
        f"FILENAME\t{Path(ppm_path).name}",
        "#\t\tX\tY\tLong\t\tLat",
        f"TIEPOINT\t0\t0\t{ref.lon0:.6f}\t\t{ref.lat0:.6f}",
        f"TIEPOINT\t{ref.width - 1}\t{ref.height - 1}\t{ref.lon1:.6f}\t\t{ref.lat1:.6f}",
        f"IMAGESIZE\t{ref.width}\t{ref.height}",
        "#", "# Généré par SPLAT!Gui (proportions réelles)", "#",
    ]
    Path(ppm_path).with_suffix(".geo").write_text("\n".join(lines) + "\n", encoding="latin-1")


# Formats de cadrage (largeur / hauteur, en kilomètres réels). None : emprise de SPLAT!.
FRAME_RATIOS = [
    (N_("4:3 (paysage)"), 4 / 3),
    (N_("Libre (emprise de SPLAT!)"), None),
    (N_("1:1 (carré)"), 1.0),
    ("3:2", 3 / 2),
    ("16:9", 16 / 9),
    ("3:4 (portrait)", 3 / 4),
]
DEFAULT_FRAME_RATIO = 4 / 3


def site_content_box(image, ref, sites):
    """Zone utile (couverture, trajet) de la carte, élargie aux sites, en pixels, ou None."""
    box = content_box(coverage_layer(image))
    dx, dy = ref.pixel_size()
    for site in sites:
        sx = (site["lon"] - ref.lon0) / dx
        sy = (site["lat"] - ref.lat0) / dy
        if 0 <= sx < ref.width and 0 <= sy < ref.height:
            box = box or (int(sx), int(sy), int(sx), int(sy))
            # Le cadre inclut toujours les sites (et la place de leurs icônes).
            box = (min(box[0], int(sx) - 8), min(box[1], int(sy) - 16),
                   max(box[2], int(sx) + 8), max(box[3], int(sy) + 8))
    return box


def frame_box(ref, ratio, box=None, fit_content=True, width_km=0.0, margin=1.15):
    """Rectangle (left, top, w, h) en pixels de `ref` dont le rapport largeur/hauteur vaut
    `ratio` en kilomètres réels, centré sur `box` (zone utile) ou sur la carte.
    Taille : `width_km` si > 0, sinon la zone utile + 15 % (fit_content), sinon le plus grand
    rectangle contenu dans la carte. Renvoie aussi (largeur_km, hauteur_km)."""
    dx, dy = ref.pixel_size()
    px_h_km = abs(dy) * 110.574
    if box:
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    else:
        cx, cy = ref.width / 2, ref.height / 2
    # La largeur d'un pixel en km dépend de la latitude : on la prend au centre du cadre
    # (deux passes, car le cadre peut être décalé pour rester dans la carte).
    center_lat = ref.lat0 + cy * dy
    for _ in range(2):
        px_w_km = abs(dx) * 111.32 * math.cos(math.radians(center_lat))
        map_w_km, map_h_km = ref.width * px_w_km, ref.height * px_h_km
        max_w_km = min(map_w_km, map_h_km * ratio)
        if width_km and width_km > 0:
            w_km = width_km
        elif box and fit_content:
            w_km = max((box[2] - box[0]) * px_w_km, (box[3] - box[1]) * px_h_km * ratio) * margin
        else:
            w_km = max_w_km
        w_km = max(1.0, min(w_km, max_w_km))
        h_km = w_km / ratio
        w, h = w_km / px_w_km, h_km / px_h_km
        left = min(max(cx - w / 2, 0), ref.width - w)
        top = min(max(cy - h / 2, 0), ref.height - h)
        center_lat = ref.lat0 + (top + h / 2 - 0.5) * dy
    # Valeurs non arrondies : le découpage se fait à une précision inférieure au pixel.
    return (left, top, w, h), (w_km, h_km)


def render_box(image, ref, left, top, w, h, out_w, out_h):
    """Rééchantillonne (au plus proche voisin : couleurs de SPLAT! exactes) le rectangle
    (left, top, w, h) — en pixels, non entiers — vers une image out_w × out_h.
    Renvoie (image, GeoRef) ; l'emprise géographique est exactement celle du rectangle."""
    out = QImage(max(1, int(out_w)), max(1, int(out_h)), QImage.Format.Format_RGB32)
    out.fill(QColor(255, 255, 255))
    painter = QPainter(out)
    painter.drawImage(QRectF(0, 0, out.width(), out.height()), image, QRectF(left, top, w, h))
    painter.end()
    dx, dy = ref.pixel_size()
    west = ref.lon0 + (left - 0.5) * dx
    north = ref.lat0 + (top - 0.5) * dy
    east, south = west + w * dx, north + h * dy
    odx, ody = (east - west) / out.width(), (south - north) / out.height()
    return out, GeoRef(west + odx / 2, north + ody / 2, east - odx / 2, south - ody / 2,
                       out.width(), out.height())


def force_ratio(image, ref, ratio, smooth=True):
    """Ajuste l'image (déjà aux proportions réelles) à un rapport largeur/hauteur exact en
    pixels ; l'emprise géographique est conservée (écart de taille de pixel < 1 %)."""
    width = max(1, round(image.height() * ratio))
    if width == image.width():
        return image, ref
    mode = Qt.TransformationMode.SmoothTransformation if smooth else Qt.TransformationMode.FastTransformation
    scaled = image.scaled(width, image.height(), Qt.AspectRatioMode.IgnoreAspectRatio, mode)
    west, south, east, north = ref.bounds()
    dx = (east - west) / width
    return scaled, GeoRef(west + dx / 2, ref.lat0, east - dx / 2, ref.lat1, width, ref.height)


CORRECTED_SUFFIX = "_proportions"


def write_corrected_map(ppm_path, formats=("ppm",), ratio=None, sites=(), fit_content=True,
                        width_km=0.0, blur_sigma=0.0):
    """Écrit la carte aux bonnes proportions à côté de l'originale :
    <carte>_proportions.ppm (+ .geo) et/ou <carte>_proportions.png (+ .pgw).
    Avec `ratio` (largeur/hauteur en km), la carte est d'abord découpée à ce format autour
    de la zone utile. Renvoie la liste des fichiers écrits."""
    ppm_path = Path(ppm_path)
    if ppm_path.stem.endswith(CORRECTED_SUFFIX):
        return []
    ref = georef_for(ppm_path)
    image = QImage(str(ppm_path))
    if ref is None or image.isNull():
        return []
    if ratio:
        # Zone utile calculée sans les repères rouges de SPLAT! (comme à l'affichage).
        from .sites import remove_splat_marks
        unmarked, _removed = remove_splat_marks(image, ref, sites) if sites else (image, False)
        box = site_content_box(unmarked, ref, sites)
        (left, top, w, h), _size = frame_box(ref, ratio, box, fit_content, width_km)
        # Résolution horizontale de SPLAT! conservée ; hauteur déduite du format.
        out_w = max(1, round(w))
        image, ref = render_box(image, ref, left, top, w, h, out_w, round(out_w / ratio))
    if blur_sigma > 0:
        # Couverture floutée sur le relief reconstitué (les couleurs ne sont plus celles,
        # exactes, de SPLAT!) ; sigma en pixels SPLAT! ≈ pixels de cette image.
        from . import hillshade
        from .sites import remove_splat_marks
        # Les repères de SPLAT! (sites, noms) restent nets : ils sont retirés avant le flou,
        # puis recopiés par-dessus.
        unmarked = remove_splat_marks(image, ref, sites)[0] if sites else image
        marks = None
        if sites:
            before = _rgba_view(image.convertToFormat(QImage.Format.Format_ARGB32))
            after = _rgba_view(unmarked.convertToFormat(QImage.Format.Format_ARGB32))
            changed = np.any(before[..., :3] != after[..., :3], axis=2)
            if changed.any():
                before[..., 3] = np.where(changed, 255, 0).astype(np.uint8)
                h, w = changed.shape
                marks = QImage(np.ascontiguousarray(before).data, w, h, w * 4,
                               QImage.Format.Format_ARGB32).copy()
        coverage = coverage_layer(unmarked)
        grid, found, _needed = hillshade.elevation_grid(ref)
        relief = relief_layer(unmarked, coverage, grid if found else None)
        image = relief.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        painter = QPainter(image)
        painter.drawImage(0, 0, blur(coverage, blur_sigma))
        if marks is not None:
            painter.drawImage(0, 0, marks)
        painter.end()
        image = image.convertToFormat(QImage.Format.Format_RGB32)
    written = []
    base = ppm_path.with_name(ppm_path.stem + CORRECTED_SUFFIX)
    if "ppm" in formats:
        stretched, new_ref = stretch_to_proportions(image, ref)
        if ratio:
            stretched, new_ref = force_ratio(stretched, new_ref, ratio, smooth=False)
        target = base.with_suffix(".ppm")
        if stretched.convertToFormat(QImage.Format.Format_RGB888).save(str(target), "PPM"):
            write_geo(target, new_ref)
            written.append(target)
    if "png" in formats:
        corrected, new_ref = correct_aspect(image, ref)
        if ratio:
            corrected, new_ref = force_ratio(corrected, new_ref, ratio)
        target = base.with_suffix(".png")
        if corrected.save(str(target), "PNG"):
            write_world_file(target, new_ref)
            written.append(target)
    return written


def _box_blur(arr, radius, axis):
    """Moyenne glissante de largeur 2 × radius + 1 le long d'un axe (bords transparents)."""
    if radius < 1:
        return arr
    pad = [(0, 0)] * arr.ndim
    pad[axis] = (radius + 1, radius)
    c = np.cumsum(np.pad(arr, pad), axis=axis)
    upper = np.take(c, np.arange(2 * radius + 1, c.shape[axis]), axis=axis)
    lower = np.take(c, np.arange(0, c.shape[axis] - 2 * radius - 1), axis=axis)
    return (upper - lower) / (2 * radius + 1)


def blur(image, sigma):
    """Flou d'écart type `sigma` pixels (couleurs prémultipliées : pas de halo sombre)."""
    if sigma < 0.5:
        return image
    src = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    arr = _rgba_view(src).astype(np.float32)
    box = int(round(math.sqrt(12 * sigma * sigma / 3 + 1)))
    radius = max(1, box // 2)
    for _ in range(3):
        arr = _box_blur(arr, radius, 0)
        arr = _box_blur(arr, radius, 1)
    out = np.ascontiguousarray(np.clip(arr + 0.5, 0, 255).astype(np.uint8))
    h, w = out.shape[:2]
    return QImage(out.data, w, h, w * 4, QImage.Format.Format_ARGB32_Premultiplied).copy()
