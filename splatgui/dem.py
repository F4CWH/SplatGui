"""Relief à haute précision et sursol : production directe des fichiers SDF de SPLAT!.

Sources d'altitude (une tuile = 1° × 1°, échantillonnée aux nœuds 3" ou 1" comme le SRTM) :
    srtm        tuiles SRTM (terrain/srtm, téléchargées comme pour srtm2sdf)
    copernicus  Copernicus GLO-30 (DSM mondial 1", GeoTIFF « cloud optimized » sur AWS)
    ign         IGN RGE ALTO (MNT France, sol nu), service WMS-R de la Géoplateforme ;
                hors de France, complété par Copernicus

Sursol : classes d'occupation du sol ESA WorldCover 2021 (10 m, GeoTIFF « cloud optimized »)
converties en hauteurs (arbres, bâti, arbustes) et ajoutées au relief. Les mailles des sites
sont ramenées au sol nu à chaque calcul (copies corrigées des tuiles dans le dossier du calcul,
que SPLAT! lit avant le dossier -d) : une antenne de 10 m en forêt reste sous la canopée, comme
avec l'option -gc de SPLAT!.

Les SDF sont écrits ici, sans srtm2sdf (même format, vérifié valeur par valeur) :
4 lignes d'en-tête (ouest max, nord min, ouest min, nord max) puis, du sud au nord et d'est en
ouest, les altitudes en mètres, sans la rangée nord ni la colonne est (communes aux voisines).
"""

import gzip
import math
import os
import struct
import urllib.error
import urllib.request
import zlib
from pathlib import Path

import numpy as np

from . import terrain
from .i18n import N_, tr

SOURCES = {
    "srtm": N_("SRTM (NASA, 30 m)"),
    "copernicus": N_("Copernicus GLO-30 (ESA, 30 m)"),
    "ign": N_("IGN RGE ALTO (France, sol nu)"),
}
CACHE = {name: terrain.TERRAIN_DIR / name for name in ("copernicus", "ign", "worldcover")}
COPERNICUS_URL = ("https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/"
                  "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif")
WORLDCOVER_URL = ("https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
                  "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif")
IGN_URL = ("https://data.geopf.fr/wms-r/wms?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap"
           "&LAYERS=ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES&STYLES=&CRS=EPSG:4326"
           "&BBOX={south},{west},{north},{east}&WIDTH={size}&HEIGHT={size}&FORMAT=image/x-bil;bits=32")
USER_AGENT = "SPLAT!Gui"

# Classes ESA WorldCover → hauteur de sursol (clé de réglage).
CLUTTER_CLASSES = {10: "trees", 95: "trees", 50: "built", 20: "shrubs"}
DEFAULT_CLUTTER = {"enabled": False, "trees": 15.0, "built": 10.0, "shrubs": 2.0}


def nodes(hd):
    """Nombre de nœuds par côté d'une tuile (bords compris)."""
    return 3601 if hd else 1201


# --- Lecture de GeoTIFF « cloud optimized » par requêtes partielles -----------------------

class RemoteFile:
    """Fichier distant lu par plages d'octets (HTTP Range)."""

    def __init__(self, url, cancel):
        self.url, self.cancel = url, cancel

    def read(self, offset, length):
        if self.cancel():
            raise terrain.Cancelled()
        request = urllib.request.Request(self.url, headers={"Range": f"bytes={offset}-{offset + length - 1}",
                                                            "User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
        if response.status == 200:            # serveur sans Range : tout le fichier
            data = data[offset:offset + length]
        return data


_TYPES = {1: "B", 2: "B", 3: "H", 4: "I", 6: "b", 7: "B", 8: "h", 9: "i", 11: "f", 12: "d", 16: "Q", 17: "q"}
_DTYPES = {(1, 8): np.uint8, (2, 8): np.int8, (1, 16): np.uint16, (2, 16): np.int16, (1, 32): np.uint32,
           (2, 32): np.int32, (3, 32): np.float32, (3, 64): np.float64}


class CogLevel:
    """Une image (pleine résolution ou aperçu) d'un GeoTIFF tuilé."""

    def __init__(self, tags):
        self.width, self.height = tags[256][0], tags[257][0]
        self.tile_w, self.tile_h = tags[322][0], tags[323][0]
        self.offsets, self.counts = tags[324], tags[325]
        self.compression = tags.get(259, (1,))[0]
        self.predictor = tags.get(317, (1,))[0]
        bits = tags.get(258, (8,))[0]
        self.dtype = np.dtype(_DTYPES[(tags.get(339, (1,))[0], bits)]).newbyteorder("<")
        self.across = math.ceil(self.width / self.tile_w)


class Cog:
    """GeoTIFF tuilé (classique, petit-boutiste), avec ses aperçus. Géoréférencement : étendue
    en degrés (ouest, nord, est, sud) de la pleine résolution, commune à tous les niveaux."""

    def __init__(self, source):
        self.source = source
        head = source.read(0, 65536)
        if head[:4] != b"II*\x00":
            raise ValueError(tr("GeoTIFF non pris en charge (BigTIFF ou gros-boutiste)."))
        self.levels = []
        offset = struct.unpack("<I", head[4:8])[0]
        geo = None
        while offset:
            tags, offset = self._ifd(head, offset)
            self.levels.append(CogLevel(tags))
            if geo is None:
                geo = tags
        scale, tie = geo[33550], geo[33922]
        keys = geo.get(34735, ())
        point = any(keys[i] == 1025 and keys[i + 3] == 2 for i in range(4, len(keys), 4))
        base = self.levels[0]
        # Coins de l'étendue : en « PixelIsPoint », le point de calage est le centre du pixel.
        west = tie[3] - (scale[0] / 2 if point else 0)
        north = tie[4] + (scale[1] / 2 if point else 0)
        self.extent = (west, north, west + base.width * scale[0], north - base.height * scale[1])

    def _ifd(self, head, offset):
        def fetch(position, size):
            if position + size <= len(head):
                return head[position:position + size]
            return self.source.read(position, size)

        count = struct.unpack("<H", fetch(offset, 2))[0]
        entries = fetch(offset + 2, count * 12 + 4)
        tags = {}
        for n in range(count):
            tag, kind, number, value = struct.unpack("<HHII", entries[n * 12:n * 12 + 12])
            if kind not in _TYPES and kind != 5:
                continue
            size = struct.calcsize("<" + _TYPES.get(kind, "II")) * number
            raw = entries[n * 12 + 8:n * 12 + 12] if size <= 4 else fetch(value, size)
            if kind == 5:                                 # rationnels : non utilisés
                continue
            tags[tag] = struct.unpack("<" + _TYPES[kind] * number, raw[:size])
        return tags, struct.unpack("<I", entries[count * 12:count * 12 + 4])[0]

    def level_for(self, step_deg):
        """Le niveau le moins détaillé dont la maille reste inférieure ou égale à `step_deg`."""
        span = self.extent[2] - self.extent[0]
        fine = [lvl for lvl in self.levels if span / lvl.width <= step_deg * 1.0001]
        return min(fine, key=lambda lvl: lvl.width) if fine else self.levels[0]

    def _tile(self, level, index):
        count = level.counts[index]
        th, tw = level.tile_h, level.tile_w
        if not count:
            return np.zeros((th, tw), level.dtype)
        raw = self.source.read(level.offsets[index], count)
        if level.compression in (8, 32946):
            raw = zlib.decompress(raw)
        elif level.compression != 1:
            raise ValueError(tr("Compression TIFF {n} non prise en charge.", n=level.compression))
        if level.predictor == 3:                          # prédicteur flottant (plans d'octets)
            planes = np.frombuffer(raw, np.uint8).reshape(th, level.dtype.itemsize * tw)
            planes = np.cumsum(planes, axis=1, dtype=np.uint8).reshape(th, level.dtype.itemsize, tw)
            return planes.transpose(0, 2, 1).copy().view(level.dtype.newbyteorder(">")).reshape(th, tw)
        data = np.frombuffer(raw, level.dtype).reshape(th, tw)
        if level.predictor == 2:
            data = np.cumsum(data, axis=1, dtype=level.dtype)
        return data

    def window(self, level, west, north, east, south):
        """Pixels du niveau couvrant l'étendue demandée : (tableau, ouest, nord, pas lon, pas lat),
        coordonnées du coin nord-ouest du premier pixel."""
        x0e, y0e, x1e, y1e = self.extent
        dx, dy = (x1e - x0e) / level.width, (y0e - y1e) / level.height
        c0 = max(0, int(math.floor((west - x0e) / dx)) - 1)
        c1 = min(level.width, int(math.ceil((east - x0e) / dx)) + 1)
        r0 = max(0, int(math.floor((y0e - north) / dy)) - 1)
        r1 = min(level.height, int(math.ceil((y0e - south) / dy)) + 1)
        out = np.zeros((r1 - r0, c1 - c0), level.dtype)
        for tr_ in range(r0 // level.tile_h, (r1 - 1) // level.tile_h + 1):
            for tc in range(c0 // level.tile_w, (c1 - 1) // level.tile_w + 1):
                tile = self._tile(level, tr_ * level.across + tc)
                ty, tx = tr_ * level.tile_h, tc * level.tile_w
                ys, ye = max(r0, ty), min(r1, ty + level.tile_h)
                xs, xe = max(c0, tx), min(c1, tx + level.tile_w)
                out[ys - r0:ye - r0, xs - c0:xe - c0] = tile[ys - ty:ye - ty, xs - tx:xe - tx]
        return out, x0e + c0 * dx, y0e - r0 * dy, dx, dy


def _node_coords(tile, hd):
    """Latitudes (du nord au sud) et longitudes (d'ouest en est) des nœuds d'une tuile."""
    n = nodes(hd)
    lat, lon = tile
    return lat + 1 - np.arange(n) / (n - 1), lon + np.arange(n) / (n - 1)


def _sample(window, lats, lons, nearest=False):
    """Échantillonne une fenêtre (tableau, ouest, nord, pas) aux nœuds : bilinéaire, ou plus
    proche voisin pour des classes."""
    data, west, north, dx, dy = window
    y = (north - lats) / dy - 0.5                    # coordonnées en pixels (centres)
    x = (lons - west) / dx - 0.5
    h, w = data.shape
    if nearest:
        rows = np.clip(np.rint(y).astype(int), 0, h - 1)
        cols = np.clip(np.rint(x).astype(int), 0, w - 1)
        return data[np.ix_(rows, cols)]
    y = np.clip(y, 0, h - 1)
    x = np.clip(x, 0, w - 1)
    r0, c0 = np.floor(y).astype(int), np.floor(x).astype(int)
    r1, c1 = np.minimum(r0 + 1, h - 1), np.minimum(c0 + 1, w - 1)
    fy, fx = (y - r0)[:, None], (x - c0)[None, :]
    d = data.astype(np.float64)
    top = d[np.ix_(r0, c0)] * (1 - fx) + d[np.ix_(r0, c1)] * fx
    bottom = d[np.ix_(r1, c0)] * (1 - fx) + d[np.ix_(r1, c1)] * fx
    return top * (1 - fy) + bottom * fy


# --- Grilles d'altitude par source ------------------------------------------------------

def _cache_path(kind, tile, hd, suffix=".hgt.gz"):
    return CACHE[kind] / f"{terrain.tile_name(*tile)}_{'1' if hd else '3'}s{suffix}"


def _load_grid(path, dtype, n):
    with gzip.open(path, "rb") as fh:
        return np.frombuffer(fh.read(), dtype).reshape(n, n)


def _save_grid(path, grid):
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    with gzip.open(partial, "wb", compresslevel=6) as fh:
        fh.write(np.ascontiguousarray(grid).tobytes())
    os.replace(partial, path)


def _absent(kind, tile, hd):
    return _cache_path(kind, tile, hd, ".absent")


def fill_voids(grid, void):
    """Remplace les trous (masque `void`) par la moyenne des voisins valides, de proche en proche."""
    grid = grid.astype(np.float64)
    void = void.copy()
    for _ in range(200):
        if not void.any():
            break
        valid = ~void
        padded = np.pad(np.where(valid, grid, 0.0), 1)
        weight = np.pad(valid.astype(np.float64), 1)
        total = sum(padded[1 + dy:padded.shape[0] - 1 + dy, 1 + dx:padded.shape[1] - 1 + dx]
                    for dy in (-1, 0, 1) for dx in (-1, 0, 1))
        count = sum(weight[1 + dy:weight.shape[0] - 1 + dy, 1 + dx:weight.shape[1] - 1 + dx]
                    for dy in (-1, 0, 1) for dx in (-1, 0, 1))
        fill = void & (count > 0)
        grid[fill] = total[fill] / count[fill]
        void &= ~fill
    grid[void] = 0.0
    return grid


def srtm_grid(tile, hd, url_template, log, cancel):
    """Tuile SRTM (téléchargée au besoin) en grille d'altitudes, ou None (pas de données)."""
    path = terrain.find_srtm(tile)
    if hd and path is not None and len(terrain.read_hgt(path)) != terrain.SRTM1_SIZE:
        path = None                                   # tuile 3" déposée à la main : inutilisable en HD
    if path is None:
        if terrain.marker_valid(terrain._unavailable_marker(tile)):
            return None
        path = terrain.download(tile, url_template, log, cancel)
        if path is None:
            return None
    data = terrain.read_hgt(path)
    if len(data) == terrain.SRTM1_SIZE:
        grid = np.frombuffer(data, ">i2").reshape(3601, 3601)
        grid = grid if hd else grid[::3, ::3]
    elif len(data) == terrain.SRTM3_SIZE and not hd:
        grid = np.frombuffer(data, ">i2").reshape(1201, 1201)
    else:
        raise RuntimeError(tr("{name} : SPLAT! HD nécessite une tuile SRTM 1\" (3601×3601).",
                              name=terrain.tile_name(*tile)))
    void = grid == -32768
    return fill_voids(grid, void) if void.any() else grid.astype(np.float64)


def copernicus_grid(tile, hd, log, cancel):
    path = _cache_path("copernicus", tile, hd)
    n = nodes(hd)
    if path.exists():
        return _load_grid(path, "<f4", n).astype(np.float64)
    if terrain.marker_valid(_absent("copernicus", tile, hd)):
        return None
    lat, lon = tile
    url = COPERNICUS_URL.format(ns="N" if lat >= 0 else "S", lat=abs(lat), ew="E" if lon >= 0 else "W", lon=abs(lon))
    log("  " + tr("Copernicus {name} : {url}", name=terrain.tile_name(*tile), url=url) + "\n")
    try:
        cog = Cog(RemoteFile(url, cancel))
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):                    # pas de tuile : mer
            _absent("copernicus", tile, hd).parent.mkdir(parents=True, exist_ok=True)
            _absent("copernicus", tile, hd).write_text(url, encoding="utf-8")
            log("    " + tr("Pas de données (mer).") + "\n")
            return None
        raise
    lats, lons = _node_coords(tile, hd)
    level = cog.level_for(1 / (n - 1))
    window = cog.window(level, lon, lat + 1, lon + 1, lat)
    grid = _sample(window, lats, lons).astype(np.float32)
    _save_grid(path, grid)
    return grid.astype(np.float64)


def ign_grid(tile, hd, log, cancel):
    """RGE ALTO (Géoplateforme), complété par Copernicus hors de la zone couverte."""
    path = _cache_path("ign", tile, hd)
    n = nodes(hd)
    if path.exists():
        grid = _load_grid(path, "<f4", n).astype(np.float64)
    else:
        lat, lon = tile
        half = 0.5 / (n - 1)                           # centres de pixels sur les nœuds
        url = IGN_URL.format(south=lat - half, west=lon - half, north=lat + 1 + half, east=lon + 1 + half, size=n)
        log("  " + tr("IGN RGE ALTO {name} ({n} × {n} points)…", name=terrain.tile_name(*tile), n=n) + "\n")
        if cancel():
            raise terrain.Cancelled()
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        chunks = []
        with urllib.request.urlopen(request, timeout=300) as response:
            kind = response.headers.get("Content-Type", "")
            while chunk := response.read(256 * 1024):     # par morceaux : annulable en cours de route
                if cancel():
                    raise terrain.Cancelled()
                chunks.append(chunk)
        data = b"".join(chunks)
        if "bil" not in kind or len(data) != n * n * 4:
            raise RuntimeError(tr("Réponse inattendue du service IGN ({kind}, {size} octets).",
                                  kind=kind, size=len(data)))
        grid = np.frombuffer(data, "<f4").reshape(n, n)
        _save_grid(path, grid)
        grid = grid.astype(np.float64)
    # Hors zone, le service renvoie -99999 ; autour des trous, ses valeurs interpolées restent très
    # négatives. Point le plus bas de France : environ -10 m.
    void = grid < -50
    if void.all():
        log("    " + tr("Hors de la zone couverte par l'IGN : Copernicus.") + "\n")
        return copernicus_grid(tile, hd, log, cancel)
    if void.any():
        other = copernicus_grid(tile, hd, log, cancel)
        grid[void] = other[void] if other is not None else 0.0
    return grid


def worldcover_classes(tile, hd, log, cancel):
    """Classes ESA WorldCover aux nœuds de la tuile (0 = pas de données)."""
    path = _cache_path("worldcover", tile, hd, ".cls.gz")
    n = nodes(hd)
    if path.exists():
        return _load_grid(path, np.uint8, n)
    lat, lon = tile
    base_lat, base_lon = 3 * math.floor(lat / 3), 3 * math.floor(lon / 3)
    name = f"{'N' if base_lat >= 0 else 'S'}{abs(base_lat):02d}{'E' if base_lon >= 0 else 'W'}{abs(base_lon):03d}"
    url = WORLDCOVER_URL.format(tile=name)
    log("  " + tr("Occupation du sol {name} : {url}", name=terrain.tile_name(*tile), url=url) + "\n")
    try:
        cog = Cog(RemoteFile(url, cancel))
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):
            return np.zeros((n, n), np.uint8)
        raise
    lats, lons = _node_coords(tile, hd)
    level = cog.level_for(1 / (n - 1))
    classes = _sample(cog.window(level, lon, lat + 1, lon + 1, lat), lats, lons, nearest=True).astype(np.uint8)
    _save_grid(path, classes)
    return classes


def clutter_heights(classes, clutter):
    heights = np.zeros(classes.shape, np.float64)
    for code, key in CLUTTER_CLASSES.items():
        heights[classes == code] = float(clutter.get(key, 0.0))
    return heights


# --- Écriture des SDF --------------------------------------------------------------------

def write_sdf(path, grid, tile, hd):
    """Écrit une grille (n × n, du nord au sud et d'ouest en est) au format SDF de SPLAT!."""
    lat, lon = tile
    west_min = (-(lon + 1)) % 360
    ippd = nodes(hd) - 1
    values = np.rint(grid).astype(np.int32)[ippd:0:-1, ippd - 1::-1]     # sud → nord, est → ouest
    header = f"{west_min + 1}\n{lat}\n{west_min}\n{lat + 1}\n"
    partial = Path(str(path) + ".part")
    with open(partial, "w", encoding="ascii", newline="\n") as fh:
        fh.write(header)
        fh.write("\n".join(map(str, values.ravel().tolist())))
        fh.write("\n")
    os.replace(partial, path)


def config_key(source, clutter):
    """Suffixe du dossier SDF d'une configuration (source, sursol)."""
    if not clutter.get("enabled"):
        return source
    heights = "-".join(f"{float(clutter.get(k, 0)):g}" for k in ("trees", "built", "shrubs"))
    return f"{source}-sursol-{heights}"


def uses_dem(params):
    """Vrai si le relief est produit ici (autre source que le SRTM, ou sursol)."""
    return params.get("relief_source", "srtm") != "srtm" or params.get("clutter", {}).get("enabled", False)


def sdf_dir_for(params):
    return terrain.TERRAIN_DIR / f"sdf-{config_key(params.get('relief_source', 'srtm'), params.get('clutter', {}))}"


def tile_grids(tile, hd, source, clutter, url_template, log, cancel):
    """(relief nu, sursol) d'une tuile, ou (None, None) sans données."""
    if source == "copernicus":
        bare = copernicus_grid(tile, hd, log, cancel)
    elif source == "ign":
        bare = ign_grid(tile, hd, log, cancel)
    else:
        bare = srtm_grid(tile, hd, url_template, log, cancel)
    if bare is None:
        return None, None
    extra = clutter_heights(worldcover_classes(tile, hd, log, cancel), clutter) if clutter.get("enabled") else None
    return bare, extra


def ensure_tiles(tiles, hd, sdf_dir, source, clutter, url_template, log, cancel=lambda: False,
                 progress=lambda done, total: None):
    """Comme terrain.ensure_tiles, pour les SDF produits ici. Renvoie un résumé (dict de listes)."""
    Path(sdf_dir).mkdir(parents=True, exist_ok=True)
    summary = {"present": [], "converted": [], "unavailable": [], "failed": []}
    for index, tile in enumerate(tiles):
        progress(index, len(tiles))
        if cancel():
            raise terrain.Cancelled()
        name = terrain.tile_name(*tile)
        if terrain.sdf_exists(sdf_dir, tile, hd):
            summary["present"].append(name)
            continue
        try:
            bare, extra = tile_grids(tile, hd, source, clutter, url_template, log, cancel)
            if bare is None:
                summary["unavailable"].append(name)
                continue
            path = terrain.sdf_file(sdf_dir, tile, hd)
            write_sdf(path, bare + extra if extra is not None else bare, tile, hd)
            log(f"  → {path.name}\n")
            summary["converted"].append(name)
        except terrain.Cancelled:
            raise
        except (OSError, urllib.error.URLError, ValueError, RuntimeError, zlib.error) as exc:
            log("  " + tr("Relief {name} impossible : {exc}", name=name, exc=exc) + "\n")
            summary["failed"].append(name)
    progress(len(tiles), len(tiles))
    return summary


def write_site_tiles(run_dir, sites, hd, source, clutter, url_template, log, cancel=lambda: False):
    """Sursol : copies des tuiles contenant les sites, avec la maille de chaque site ramenée
    au sol nu, écrites dans le dossier du calcul (lues par SPLAT! avant le dossier -d).
    Les tuiles déjà écrites dans ce dossier (relance du calcul) sont conservées.
    Renvoie les fichiers écrits."""
    if not clutter.get("enabled"):
        return []
    n = nodes(hd)
    by_tile = {}
    for site in sites:
        lat, lon = float(site["lat"]), float(site["lon"])
        lon = ((lon + 180) % 360) - 180
        tile_lat, tile_lon = math.floor(lat), math.floor(lon)
        row = int(round((tile_lat + 1 - lat) * (n - 1)))
        col = int(round((lon - tile_lon) * (n - 1)))
        # Le SDF d'une tuile n'a ni la rangée nord ni la colonne est : ces nœuds sont ceux
        # (rangée sud, colonne ouest) des tuiles voisines, que l'on corrige à la place.
        if row == 0 and tile_lat < 89:
            tile_lat, row = tile_lat + 1, n - 1
        if col == n - 1:
            tile_lon, col = ((tile_lon + 1 + 180) % 360) - 180, 0
        by_tile.setdefault((tile_lat, tile_lon), []).append((row, col))
    written = []
    for tile, cells in by_tile.items():
        path = Path(run_dir) / terrain.sdf_file("", tile, hd).name
        if path.exists():
            continue
        try:
            bare, extra = tile_grids(tile, hd, source, clutter, url_template, log, cancel)
        except terrain.Cancelled:
            raise
        except (OSError, urllib.error.URLError, ValueError, RuntimeError, zlib.error) as exc:
            log("  " + tr("Relief {name} impossible : {exc}", name=terrain.tile_name(*tile), exc=exc) + "\n")
            continue
        if bare is None or extra is None:
            continue
        extra = extra.copy()
        for row, col in cells:
            extra[row, col] = 0.0
        write_sdf(path, bare + extra, tile, hd)
        written.append(path)
        log("  " + tr("Sursol retiré à l'emplacement des sites : {name}", name=path.name) + "\n")
    return written


def describe(params):
    """Libellé court de la configuration du relief, pour la console."""
    text = tr(SOURCES.get(params.get("relief_source", "srtm"), SOURCES["srtm"]))
    clutter = params.get("clutter", {})
    if clutter.get("enabled"):
        text += " + " + tr("sursol (arbres {trees:g} m, bâti {built:g} m, arbustes {shrubs:g} m)",
                           trees=float(clutter["trees"]), built=float(clutter["built"]),
                           shrubs=float(clutter["shrubs"]))
    return text
