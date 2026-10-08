"""Ombrage du relief (hillshade) calculé à partir des tuiles SRTM de terrain/srtm.

L'altitude est échantillonnée au centre de chaque pixel de la carte (GeoRef), puis
l'ombrage est calculé pour un soleil donné (azimut, hauteur) avec une exagération verticale.
"""

import math
import threading
from collections import OrderedDict

import numpy as np
from PyQt6.QtGui import QImage

from . import terrain
from .i18n import tr

METERS_PER_DEG_LAT = 110_574.0
METERS_PER_DEG_LON = 111_320.0

_tiles = OrderedDict()   # (lat, lon) -> tableau numpy (n, n) ; tuiles absentes non mémorisées
_tiles_lock = threading.Lock()  # cache partagé par l'interface et les calculs en arrière-plan
_MAX_TILES = 9


def _tile(lat, lon):
    key = (lat, lon)
    with _tiles_lock:
        if key in _tiles:
            _tiles.move_to_end(key)
            return _tiles[key]
    # Une tuile absente est recherchée à chaque fois : elle peut être téléchargée entre-temps
    # (calcul SPLAT!, autre source).
    path = terrain.find_srtm(key)
    array = None
    if path is not None:
        try:
            data = terrain.read_hgt(path)
            size = int(round(math.sqrt(len(data) // 2)))
            if size * size * 2 == len(data):
                array = np.frombuffer(data, dtype=">i2").reshape(size, size).astype(np.float32)
                array[array < -1000] = 0.0      # trous de données SRTM (-32768)
        except (OSError, ValueError, StopIteration):
            array = None
    if array is not None:
        with _tiles_lock:
            _tiles[key] = array
            while len(_tiles) > _MAX_TILES:
                _tiles.popitem(last=False)
    return array


def elevation_grid(ref):
    """Altitudes (m) au centre des pixels de la carte ; 0 là où aucune tuile n'est disponible.
    Renvoie (grille, nombre de tuiles trouvées, nombre de tuiles nécessaires)."""
    lons = ref.lon0 + np.arange(ref.width) * (ref.lon1 - ref.lon0) / max(ref.width - 1, 1)
    lats = ref.lat0 + np.arange(ref.height) * (ref.lat1 - ref.lat0) / max(ref.height - 1, 1)
    grid = np.zeros((ref.height, ref.width), dtype=np.float32)
    found = needed = 0
    for tile_lat in range(math.floor(lats.min()), math.floor(lats.max()) + 1):
        rows = np.nonzero((lats >= tile_lat) & (lats < tile_lat + 1))[0]
        if not len(rows):
            continue
        for tile_lon in range(math.floor(lons.min()), math.floor(lons.max()) + 1):
            cols = np.nonzero((lons >= tile_lon) & (lons < tile_lon + 1))[0]
            if not len(cols):
                continue
            needed += 1
            tile = _tile(tile_lat, tile_lon)
            if tile is None:
                continue
            found += 1
            n = tile.shape[0] - 1
            # Ligne 0 de la tuile = bord nord ; colonne 0 = bord ouest. Interpolation bilinéaire.
            fy = (tile_lat + 1 - lats[rows]) * n
            fx = (lons[cols] - tile_lon) * n
            y0 = np.clip(np.floor(fy).astype(int), 0, n - 1)
            x0 = np.clip(np.floor(fx).astype(int), 0, n - 1)
            wy = (fy - y0)[:, None]
            wx = (fx - x0)[None, :]
            top = tile[np.ix_(y0, x0)] * (1 - wx) + tile[np.ix_(y0, x0 + 1)] * wx
            bottom = tile[np.ix_(y0 + 1, x0)] * (1 - wx) + tile[np.ix_(y0 + 1, x0 + 1)] * wx
            grid[np.ix_(rows, cols)] = top * (1 - wy) + bottom * wy
    return grid, found, needed


def shade(grid, ref, azimuth, altitude, exaggeration):
    """Éclairement (0..1) par la méthode classique de Horn simplifiée (gradient centré)."""
    lat_mid = math.radians((ref.lat0 + ref.lat1) / 2)
    dx_deg, dy_deg = ref.pixel_size()
    dx = abs(dx_deg) * METERS_PER_DEG_LON * math.cos(lat_mid)
    dy = abs(dy_deg) * METERS_PER_DEG_LAT
    z = grid * float(exaggeration)
    gy, gx = np.gradient(z, dy, dx)          # gy : vers le sud (lignes), gx : vers l'est
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)             # direction de la pente descendante, depuis le nord
    az = math.radians(azimuth)
    alt = math.radians(altitude)
    lit = math.sin(alt) * np.cos(slope) + math.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    return np.clip(lit, 0.0, 1.0)


def shade_image(light, altitude, strength=2.5):
    """Image grise pour un mélange « lumière douce » : 128 = neutre (terrain plat)."""
    flat = math.sin(math.radians(altitude))
    gray = 128.0 + (light - flat) * 255.0 * strength
    gray = np.clip(gray, 0, 255).astype(np.uint8)
    h, w = gray.shape
    rgba = np.empty((h, w, 4), dtype=np.uint8)
    rgba[..., 0] = rgba[..., 1] = rgba[..., 2] = gray
    rgba[..., 3] = 255
    image = QImage(rgba.data, w, h, w * 4, QImage.Format.Format_ARGB32)
    return image.copy()


def elevation_at(lat, lon):
    """Altitude (m) en un point, par interpolation bilinéaire, ou None sans tuile SRTM."""
    tile_lat, tile_lon = math.floor(lat), math.floor(lon)
    tile = _tile(tile_lat, tile_lon)
    if tile is None:
        return None
    n = tile.shape[0] - 1
    fy, fx = (tile_lat + 1 - lat) * n, (lon - tile_lon) * n
    y0, x0 = min(int(fy), n - 1), min(int(fx), n - 1)
    wy, wx = fy - y0, fx - x0
    top = tile[y0, x0] * (1 - wx) + tile[y0, x0 + 1] * wx
    bottom = tile[y0 + 1, x0] * (1 - wx) + tile[y0 + 1, x0 + 1] * wx
    return float(top * (1 - wy) + bottom * wy)


_download_locks = {}
_locks_guard = threading.Lock()


def tiles_for_bounds(west, south, east, north):
    """Tuiles SRTM (coin sud-ouest, degrés entiers) couvrant une emprise."""
    return [(lat, lon) for lat in range(math.floor(south), math.floor(north - 1e-9) + 1)
            for lon in range(math.floor(west), math.floor(east - 1e-9) + 1)]


def ensure_srtm(tiles, url_template, log=lambda _m: None, cancel=lambda: False):
    """Télécharge dans terrain/srtm les tuiles absentes (une seule fois même si plusieurs
    fils d'exécution les demandent). Renvoie le nombre de tuiles téléchargées."""
    count = 0
    for tile in tiles:
        if terrain.find_srtm(tile) is not None or terrain.marker_valid(terrain._unavailable_marker(tile)):
            continue
        with _locks_guard:
            lock = _download_locks.setdefault(tile, threading.Lock())
        with lock:
            if terrain.find_srtm(tile) is not None or terrain.marker_valid(terrain._unavailable_marker(tile)):
                continue
            terrain.SRTM_DIR.mkdir(parents=True, exist_ok=True)
            log(tr("Téléchargement de la tuile SRTM {tile}…", tile=terrain.tile_name(*tile)))
            try:
                if terrain.download(tile, url_template, lambda _m: None, cancel):
                    count += 1
            except Exception as exc:          # réseau indisponible, etc.
                log(tr("Tuile SRTM {tile} indisponible : {exc}", tile=terrain.tile_name(*tile), exc=exc))
                continue
    return count
