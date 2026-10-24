"""Import de modèles numériques de terrain fournis par l'utilisateur : GeoTIFF (.tif, .tiff) et
ESRI ASCII Grid (.asc), convertis en tuiles SDF de SPLAT! (1° × 1°).

Systèmes de coordonnées reconnus (reprojection intégrée, sans bibliothèque externe) :
- géographiques (lon/lat) : WGS84, ETRS89, RGF93, NAD83 (écarts de datum inférieurs au mètre) ;
- Lambert 93 (EPSG 2154) et coniques conformes CC42 à CC50 (EPSG 3942 à 3950) ;
- UTM WGS84 (EPSG 326xx / 327xx), ETRS89 (258xx) et NAD83 (269xx).
Le système vient des clés GeoTIFF, ou du fichier .prj voisin d'un .asc ; sans .prj, une grille
en mètres dans l'emprise du Lambert 93 est supposée en Lambert 93 (dalles IGN RGE ALTO, BD ALTI).

Plusieurs fichiers (dalles IGN, par exemple) sont assemblés : chaque tuile SDF touchée est
échantillonnée dans tous les fichiers qui la recouvrent, le plus fin d'abord. Les données plus
fines que la maille SDF sont d'abord moyennées. Hors des fichiers, la tuile garde le relief du
SDF déjà présent dans le dossier, sinon celui de Copernicus GLO-30 (téléchargé), sinon 0 m.
"""

import math
import re
import struct
import urllib.error
import zlib
from pathlib import Path

import numpy as np

from . import dem, terrain
from .i18n import tr

SUFFIXES = (".tif", ".tiff", ".asc")

# --- Projections -------------------------------------------------------------------------------

GRS80 = (6378137.0, 1 / 298.257222101)
WGS84 = (6378137.0, 1 / 298.257223563)


class Geographic:
    """Coordonnées lon/lat en degrés."""
    name = "lon/lat"
    metric = False

    def forward(self, lon, lat):
        return lon, lat

    def inverse(self, x, y):
        return x, y


class LambertConformal:
    """Projection conique conforme de Lambert à deux parallèles (EPSG 9802)."""
    metric = True

    def __init__(self, name, lat0, lon0, lat1, lat2, x0, y0, ellipsoid=GRS80):
        self.name = name
        a, f = ellipsoid
        self.e = e = math.sqrt(f * (2 - f))
        self.lon0, self.x0, self.y0 = math.radians(lon0), x0, y0

        def m(phi):
            return math.cos(phi) / math.sqrt(1 - (e * math.sin(phi)) ** 2)

        p0, p1, p2 = map(math.radians, (lat0, lat1, lat2))
        self.n = (math.log(m(p1)) - math.log(m(p2))) / (math.log(self._t(p1)) - math.log(self._t(p2)))
        self.aF = a * m(p1) / (self.n * self._t(p1) ** self.n)
        self.r0 = self.aF * self._t(p0) ** self.n

    def _t(self, phi):
        e = self.e
        sin = np.sin(phi)
        return np.tan(np.pi / 4 - phi / 2) / ((1 - e * sin) / (1 + e * sin)) ** (e / 2)

    def forward(self, lon, lat):
        r = self.aF * self._t(np.radians(lat)) ** self.n
        theta = self.n * (np.radians(lon) - self.lon0)
        return self.x0 + r * np.sin(theta), self.y0 + self.r0 - r * np.cos(theta)

    def inverse(self, x, y):
        dx, dy = np.asarray(x, float) - self.x0, self.r0 - (np.asarray(y, float) - self.y0)
        r = np.hypot(dx, dy)
        t = (r / self.aF) ** (1 / self.n)
        lon = np.arctan2(dx, dy) / self.n + self.lon0
        e = self.e
        phi = np.pi / 2 - 2 * np.arctan(t)
        for _ in range(8):
            sin = np.sin(phi)
            phi = np.pi / 2 - 2 * np.arctan(t * ((1 - e * sin) / (1 + e * sin)) ** (e / 2))
        return np.degrees(lon), np.degrees(phi)


class TransverseMercator:
    """Mercator transverse (séries de Krüger à l'ordre 4, précision millimétrique en UTM)."""
    metric = True

    def __init__(self, name, lon0, k0=0.9996, x0=500000.0, y0=0.0, ellipsoid=WGS84):
        self.name = name
        a, f = ellipsoid
        n = f / (2 - f)
        self.lon0, self.k0, self.x0, self.y0 = math.radians(lon0), k0, x0, y0
        self.e = 2 * math.sqrt(n) / (1 + n)
        self.A = a / (1 + n) * (1 + n ** 2 / 4 + n ** 4 / 64)
        self.alpha = (n / 2 - 2 * n ** 2 / 3 + 5 * n ** 3 / 16 + 41 * n ** 4 / 180,
                      13 * n ** 2 / 48 - 3 * n ** 3 / 5 + 557 * n ** 4 / 1440,
                      61 * n ** 3 / 240 - 103 * n ** 4 / 140,
                      49561 * n ** 4 / 161280)
        self.beta = (n / 2 - 2 * n ** 2 / 3 + 37 * n ** 3 / 96 - n ** 4 / 360,
                     n ** 2 / 48 + n ** 3 / 15 - 437 * n ** 4 / 1440,
                     17 * n ** 3 / 480 - 37 * n ** 4 / 840,
                     4397 * n ** 4 / 161280)
        self.delta = (2 * n - 2 * n ** 2 / 3 - 2 * n ** 3 + 116 * n ** 4 / 45,
                      7 * n ** 2 / 3 - 8 * n ** 3 / 5 - 227 * n ** 4 / 45,
                      56 * n ** 3 / 15 - 136 * n ** 4 / 35,
                      4279 * n ** 4 / 630)

    def forward(self, lon, lat):
        phi, dl = np.radians(lat), np.radians(lon) - self.lon0
        t = np.sinh(np.arctanh(np.sin(phi)) - self.e * np.arctanh(self.e * np.sin(phi)))
        xi, eta = np.arctan2(t, np.cos(dl)), np.arctanh(np.sin(dl) / np.sqrt(1 + t * t))
        x, y = eta.copy(), xi.copy()
        for j, a in enumerate(self.alpha, 1):
            x += a * np.cos(2 * j * xi) * np.sinh(2 * j * eta)
            y += a * np.sin(2 * j * xi) * np.cosh(2 * j * eta)
        return self.x0 + self.k0 * self.A * x, self.y0 + self.k0 * self.A * y

    def inverse(self, x, y):
        xi = (np.asarray(y, float) - self.y0) / (self.k0 * self.A)
        eta = (np.asarray(x, float) - self.x0) / (self.k0 * self.A)
        xi1, eta1 = xi.copy(), eta.copy()
        for j, b in enumerate(self.beta, 1):
            xi1 -= b * np.sin(2 * j * xi) * np.cosh(2 * j * eta)
            eta1 -= b * np.cos(2 * j * xi) * np.sinh(2 * j * eta)
        chi = np.arcsin(np.sin(xi1) / np.cosh(eta1))
        phi = chi.copy()
        for j, d in enumerate(self.delta, 1):
            phi += d * np.sin(2 * j * chi)
        lon = self.lon0 + np.arctan2(np.sinh(eta1), np.cos(xi1))
        return np.degrees(lon), np.degrees(phi)


GEOGRAPHIC_CODES = {4326, 4258, 4171, 4269, 4019, 4167}


def projection(epsg):
    """Projection d'un code EPSG reconnu, sinon None."""
    if epsg in GEOGRAPHIC_CODES:
        return Geographic()
    if epsg == 2154:
        return LambertConformal("Lambert 93", 46.5, 3.0, 49.0, 44.0, 700000.0, 6600000.0)
    if 3942 <= epsg <= 3950:
        zone = epsg - 3900
        return LambertConformal(f"CC{zone}", zone, 3.0, zone - 0.75, zone + 0.75, 1700000.0,
                                (zone - 41) * 1000000.0 + 200000.0)
    for base, south, label, ellipsoid in ((32600, False, "WGS84", WGS84), (32700, True, "WGS84", WGS84),
                                          (25800, False, "ETRS89", GRS80), (26900, False, "NAD83", GRS80)):
        zone = epsg - base
        if 1 <= zone <= 60:
            return TransverseMercator(f"{label} UTM {zone}{'S' if south else 'N'}", -183.0 + 6 * zone,
                                      y0=10000000.0 if south else 0.0, ellipsoid=ellipsoid)
    return None


def projection_from_wkt(text):
    """Projection d'un fichier .prj (WKT) : code EPSG s'il est indiqué, sinon d'après les noms."""
    codes = re.findall(r'AUTHORITY\[\s*"EPSG"\s*,\s*"?(\d+)"?\s*\]', text, re.I)
    if codes and text.lstrip().upper().startswith(("PROJCS", "PROJCRS")):
        found = projection(int(codes[-1]))
        if found:
            return found
    flat = re.sub(r"[\s_\-/]+", " ", text).lower()
    if not re.match(r'\s*proj', text, re.I):
        if re.match(r'\s*geog', text, re.I):
            return Geographic()
        return None
    if "lambert 93" in flat or "lambert93" in flat:
        return projection(2154)
    match = re.search(r"\bcc ?(4[2-9]|50)\b", flat)
    if match and "rgf93" in flat:
        return projection(3900 + int(match.group(1)))
    match = re.search(r"utm zone (\d{1,2}) ?([ns])?", flat)
    if match:
        zone, south = int(match.group(1)), match.group(2) == "s"
        if "etrs" in flat:
            return projection(25800 + zone)
        if "nad83" in flat or "nad 1983" in flat:
            return projection(26900 + zone)
        return projection((32700 if south else 32600) + zone)
    return None


def guess_projection(extent):
    """Système supposé d'une grille sans description : lon/lat si l'emprise le permet, Lambert 93
    si elle tombe dans celle de la France métropolitaine."""
    west, north, east, south = extent
    if -180 <= west < east <= 360 and -90 <= south < north <= 90:
        return Geographic()
    if 0 < west < east < 1300000 and 6000000 < south < north < 7200000:
        return projection(2154)
    return None


# --- Fichiers ----------------------------------------------------------------------------------

class Raster:
    """Grille d'altitudes géoréférencée : `extent` (ouest, nord, est, sud) et pas (dx, dy) dans
    les unités de `proj` ; `window` renvoie (tableau avec NaN hors données, ouest, nord, dx, dy)."""

    def lonlat_box(self):
        """Emprise en lon/lat (ouest, sud, est, nord), d'après le contour reprojeté."""
        west, north, east, south = self.extent
        steps = np.linspace(0, 1, 33)
        xs = np.concatenate([west + (east - west) * steps, np.full(33, east),
                             east - (east - west) * steps, np.full(33, west)])
        ys = np.concatenate([np.full(33, north), north - (north - south) * steps,
                             np.full(33, south), south + (north - south) * steps])
        lon, lat = self.proj.inverse(xs, ys)
        return float(np.min(lon)), float(np.min(lat)), float(np.max(lon)), float(np.max(lat))

    def tiles(self):
        west, south, east, north = self.lonlat_box()
        eps = 1e-9
        return [(lat, lon) for lat in range(math.floor(south), math.ceil(north - eps))
                for lon in range(math.floor(west), math.ceil(east - eps))]


class GeoTiffRaster(Raster):
    def __init__(self, path):
        self.path = Path(path)
        self.cog = dem.Cog(dem.LocalFile(path))
        self.extent = self.cog.extent
        base = self.cog.levels[0]
        self.dx = (self.extent[2] - self.extent[0]) / base.width
        self.dy = (self.extent[1] - self.extent[3]) / base.height
        keys = self.cog.geokeys
        self.assumed = False
        if 3072 in keys:                                  # ProjectedCSTypeGeoKey
            code = keys[3072]
            self.proj = projection(code)
        elif keys.get(1024) == 2 or 2048 in keys:         # modèle géographique
            code = keys.get(2048, 4326)
            self.proj = Geographic() if code in GEOGRAPHIC_CODES or code == 32767 else None
        else:
            code, self.proj = None, None
        if self.proj is None:
            prj = self.path.with_suffix(".prj")
            if prj.exists():
                self.proj = projection_from_wkt(prj.read_text(errors="replace"))
            elif code in (None, 32767):
                self.proj = guess_projection(self.extent)
                self.assumed = True
        if self.proj is None:
            raise ValueError(tr("système de coordonnées non pris en charge (EPSG {code}) ; reconnus : lon/lat, "
                                "Lambert 93, CC42 à CC50, UTM", code=code if code else "?"))

    def window(self, west, north, east, south, step):
        level = self.cog.level_for(step)
        data, x0, y0, dx, dy = self.cog.window(level, west, north, east, south)
        data = data.astype(np.float64)
        if self.cog.nodata is not None:
            data[np.isclose(data, self.cog.nodata, rtol=1e-6, atol=1e-6)] = np.nan
        data[data < -1000] = np.nan                       # valeurs de remplissage courantes (-9999, -32768…)
        return data, x0, y0, dx, dy


class AsciiGridRaster(Raster):
    def __init__(self, path):
        self.path = Path(path)
        header = {}
        with open(path, "rb") as fh:
            self.offset = 0
            while True:
                line = fh.readline()
                parts = line.split()
                if len(parts) != 2 or not parts[0][:1].isalpha():
                    break
                header[parts[0].decode("ascii", "replace").lower()] = float(parts[1])
                self.offset = fh.tell()
        try:
            self.cols, self.rows = int(header["ncols"]), int(header["nrows"])
            self.dx = header.get("cellsize", header.get("dx"))
            self.dy = header.get("cellsize", header.get("dy"))
            if "xllcorner" in header:
                west = header["xllcorner"]
            else:
                west = header["xllcenter"] - self.dx / 2
            if "yllcorner" in header:
                south = header["yllcorner"]
            else:
                south = header["yllcenter"] - self.dy / 2
        except (KeyError, TypeError):
            raise ValueError(tr("en-tête ESRI ASCII Grid incomplet (ncols, nrows, xllcorner, yllcorner, cellsize)"))
        self.nodata = header.get("nodata_value")
        self.extent = (west, south + self.rows * self.dy, west + self.cols * self.dx, south)
        prj = self.path.with_suffix(".prj")
        self.assumed = False
        if prj.exists():
            self.proj = projection_from_wkt(prj.read_text(errors="replace"))
            if self.proj is None:
                raise ValueError(tr("système de coordonnées du .prj non pris en charge ; reconnus : lon/lat, "
                                    "Lambert 93, CC42 à CC50, UTM"))
        else:
            self.proj = guess_projection(self.extent)
            self.assumed = True
            if self.proj is None:
                raise ValueError(tr("système de coordonnées inconnu : ajoutez un fichier .prj à côté du .asc"))
        self._data = None

    def _load(self):
        if self._data is None:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                values = np.fromstring(fh.read().decode("ascii", "replace"), dtype=np.float64, sep=" ")
            if values.size < self.rows * self.cols:
                raise ValueError(tr("{path} : grille incomplète", path=self.path.name))
            data = values[:self.rows * self.cols].reshape(self.rows, self.cols)
            if self.nodata is not None:
                data[data == self.nodata] = np.nan
            data[data < -1000] = np.nan
            self._data = data
        return self._data

    def window(self, west, north, east, south, step):
        data = self._load()
        x0, y0 = self.extent[0], self.extent[1]
        c0 = max(0, int(math.floor((west - x0) / self.dx)) - 1)
        c1 = min(self.cols, int(math.ceil((east - x0) / self.dx)) + 1)
        r0 = max(0, int(math.floor((y0 - north) / self.dy)) - 1)
        r1 = min(self.rows, int(math.ceil((y0 - south) / self.dy)) + 1)
        return data[r0:r1, c0:c1], x0 + c0 * self.dx, y0 - r0 * self.dy, self.dx, self.dy

    def release(self):
        self._data = None


def open_raster(path):
    if Path(path).suffix.lower() == ".asc":
        return AsciiGridRaster(path)
    return GeoTiffRaster(path)


def find_files(inputs):
    """Fichiers GeoTIFF / ASCII Grid désignés directement ou contenus dans des dossiers."""
    files = []
    for item in map(Path, inputs):
        if item.is_dir():
            files += sorted(p for p in item.rglob("*") if p.suffix.lower() in SUFFIXES)
        else:
            files.append(item)
    return files


# --- Échantillonnage ---------------------------------------------------------------------------

def _block_mean(window, factor):
    """Moyenne par blocs factor × factor (trous ignorés) : données plus fines que la maille SDF."""
    data, west, north, dx, dy = window
    h, w = (data.shape[0] // factor) * factor, (data.shape[1] // factor) * factor
    if not h or not w:
        return window
    blocks = data[:h, :w].reshape(h // factor, factor, w // factor, factor)
    valid = ~np.isnan(blocks)
    count = valid.sum(axis=(1, 3))
    total = np.where(valid, blocks, 0.0).sum(axis=(1, 3))
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(count > 0, total / np.maximum(count, 1), np.nan)
    return mean, west, north, dx * factor, dy * factor


def _sample_xy(window, x, y):
    """Interpolation bilinéaire aux points (x, y) ; NaN hors de la fenêtre ou près d'un trou."""
    data, west, north, dx, dy = window
    h, w = data.shape
    col = (x - west) / dx - 0.5
    row = (north - y) / dy - 0.5
    outside = (col < -0.5) | (col > w - 0.5) | (row < -0.5) | (row > h - 0.5)
    col, row = np.clip(col, 0, w - 1), np.clip(row, 0, h - 1)
    c0, r0 = np.floor(col).astype(int), np.floor(row).astype(int)
    c1, r1 = np.minimum(c0 + 1, w - 1), np.minimum(r0 + 1, h - 1)
    fx, fy = col - c0, row - r0
    value = ((data[r0, c0] * (1 - fx) + data[r0, c1] * fx) * (1 - fy)
             + (data[r1, c0] * (1 - fx) + data[r1, c1] * fx) * fy)
    value[outside] = np.nan
    return value


def _node_step(raster, tile, hd):
    """Maille SDF exprimée dans les unités du fichier (la plus petite des deux directions)."""
    step = 1 / (dem.nodes(hd) - 1)
    if not raster.proj.metric:
        return step
    # Mètres : la maille en longitude, la plus courte, au bord de la tuile le plus proche du pôle
    return step * 111320.0 * max(0.05, math.cos(math.radians(max(abs(tile[0]), abs(tile[0] + 1)))))


def fill_from(raster, grid, tile, hd, band=512):
    """Remplit les nœuds encore vides (NaN) de `grid` avec les altitudes du fichier ; renvoie le
    nombre de nœuds remplis."""
    lats, lons = dem._node_coords(tile, hd)
    west, south, east, north = raster.lonlat_box()
    rows = np.nonzero((lats >= south) & (lats <= north))[0]
    cols = np.nonzero((lons >= west) & (lons <= east))[0]
    if not rows.size or not cols.size:
        return 0
    step = _node_step(raster, tile, hd)
    filled = 0
    west_x, north_y, east_x, south_y = raster.extent
    for start in range(0, rows.size, band):           # par bandes de lignes : mémoire bornée en HD
        r = rows[start:start + band]
        lon, lat = np.meshgrid(lons[cols], lats[r])
        x, y = raster.proj.forward(lon, lat)
        inside = (x >= west_x) & (x <= east_x) & (y >= south_y) & (y <= north_y)
        if not inside.any():
            continue
        part = raster.window(float(x[inside].min()), float(y[inside].max()), float(x[inside].max()),
                             float(y[inside].min()), step)
        factor = int(step / min(part[3], part[4]))
        if factor >= 2:
            part = _block_mean(part, factor)
        values = _sample_xy(part, x, y)
        target = grid[np.ix_(r, cols)]
        todo = np.isnan(target) & ~np.isnan(values)
        target[todo] = values[todo]
        grid[np.ix_(r, cols)] = target
        filled += int(todo.sum())
    return filled


def read_sdf_nodes(path, hd):
    """Nœuds d'un SDF existant (n × n, du nord au sud et d'ouest en est) ; la rangée nord et la
    colonne est, absentes du fichier, sont recopiées de leurs voisines."""
    n = dem.nodes(hd)
    with open(path, "rb") as fh:
        values = np.array(fh.read().split()[4:], dtype=np.float64)
    ippd = n - 1
    if values.size < ippd * ippd:
        return None
    inner = values[:ippd * ippd].reshape(ippd, ippd)[::-1, ::-1]
    grid = np.empty((n, n))
    grid[1:, :-1] = inner
    grid[0, :-1] = inner[0]
    grid[:, -1] = grid[:, -2]
    return grid


def _base_grid(tile, hd, sdf_dir, log, cancel):
    """Relief de complément hors des fichiers : SDF présent, sinon Copernicus, sinon None (0 m)."""
    path = terrain.sdf_file(sdf_dir, tile, hd)
    if terrain.sdf_exists(sdf_dir, tile, hd):
        grid = read_sdf_nodes(path, hd)
        if grid is not None:
            log("    " + tr("complété par le SDF existant") + "\n")
            return grid
    try:
        grid = dem.copernicus_grid(tile, hd, log, cancel)
    except (OSError, urllib.error.URLError, ValueError, zlib.error) as exc:
        log("    " + tr("Copernicus indisponible ({exc}) : 0 m hors des fichiers", exc=exc) + "\n")
        return None
    if grid is not None:
        log("    " + tr("complété par Copernicus GLO-30") + "\n")
    return grid


# --- Conversion --------------------------------------------------------------------------------

def scan(files, log):
    """Ouvre les fichiers (en-têtes seulement) ; renvoie (rasters, échecs)."""
    rasters, failed = [], []
    for path in files:
        try:
            raster = open_raster(path)
            rasters.append(raster)
            note = tr(" (supposé, pas de .prj)") if raster.assumed else ""
            log(tr("{name} : {crs}{note}, maille {step:g}", name=Path(path).name, crs=raster.proj.name,
                   note=note, step=raster.dx) + "\n")
        except (OSError, ValueError, KeyError, IndexError, struct.error) as exc:
            failed.append(Path(path))
            log(tr("Échec de {src} : {exc}\n", src=Path(path).name, exc=exc))
    return rasters, failed


def import_files(files, sdf_dir, hd, log=print, cancel=lambda: False, progress=lambda n, total: None):
    """Convertit des fichiers GeoTIFF / ASCII Grid en tuiles SDF ; résumé
    {"converted": [SDF écrits], "failed": [fichiers illisibles]}."""
    Path(sdf_dir).mkdir(parents=True, exist_ok=True)
    rasters, failed = scan(files, log)
    tiles = {}
    for raster in sorted(rasters, key=lambda r: min(r.dx, r.dy) * (1 if r.proj.metric else 111320.0)):
        for tile in raster.tiles():
            tiles.setdefault(tile, []).append(raster)
    summary = {"converted": [], "failed": failed}
    progress(0, len(tiles))
    n = dem.nodes(hd)
    for index, (tile, sources) in enumerate(sorted(tiles.items()), 1):
        if cancel():
            raise terrain.Cancelled()
        name = terrain.tile_name(*tile)
        log(tr("Tuile {name} : {n} fichier(s)", name=name, n=len(sources)) + "\n")
        grid = np.full((n, n), np.nan)
        filled = 0
        for raster in sources:
            if cancel():
                raise terrain.Cancelled()
            try:
                filled += fill_from(raster, grid, tile, hd)
            except (OSError, ValueError, zlib.error) as exc:
                log("    " + tr("Échec de {src} : {exc}", src=raster.path.name, exc=exc) + "\n")
            finally:
                if isinstance(raster, AsciiGridRaster):
                    raster.release()
        if not filled:
            log("    " + tr("aucune donnée dans la tuile") + "\n")
            progress(index, len(tiles))
            continue
        missing = np.isnan(grid)
        share = 100.0 * filled / grid.size
        if missing.any():
            log("    " + tr("{share:.1f} % de la tuile couverte par les fichiers", share=share) + "\n")
            base = _base_grid(tile, hd, sdf_dir, log, cancel)
            grid[missing] = base[missing] if base is not None else 0.0
        target = terrain.sdf_file(sdf_dir, tile, hd)
        dem.write_sdf(target, grid, tile, hd)
        summary["converted"].append(target)
        log(f"  → {target.name}\n")
        progress(index, len(tiles))
    return summary
