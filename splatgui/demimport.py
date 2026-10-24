"""Import de modèles numériques de terrain fournis par l'utilisateur : GeoTIFF (.tif, .tiff),
ESRI ASCII Grid (.asc), Surfer (.grd), grilles binaires avec en-tête .hdr (BIL / BIP / BSQ, .flt, ENVI),
semis de points XYZ en grille (.xyz, .csv, .txt) et tuiles SRTM (N48E002.hgt, .hgt.gz, .zip),
convertis en tuiles SDF de SPLAT! (1° × 1°).

Systèmes de coordonnées reconnus (reprojection intégrée, sans bibliothèque externe) :
- géographiques (lon/lat) : WGS84, ETRS89, RGF93, NAD83 (écarts de datum inférieurs au mètre) ;
- Lambert 93 (EPSG 2154) et coniques conformes CC42 à CC50 (EPSG 3942 à 3950) ;
- UTM WGS84 (EPSG 326xx / 327xx), ETRS89 (258xx) et NAD83 (269xx) ;
- LAEA Europe (EPSG 3035, projection d'EU-DEM) ;
- NTF Lambert I à IV, zones (EPSG 27561 à 27564) et cartographiques (27571 à 27574, dont le
  Lambert II étendu 27572), datum NTF ramené au WGS84 par la translation IGN à trois paramètres
  (1 à 2 m près, sans effet à l'échelle d'une maille SDF) ;
- Belgique : Lambert 2008 (EPSG 3812) et Lambert 72 (31370, datum BD72 à sept paramètres) ;
- Suisse : LV95 (EPSG 2056) et LV03 (21781), projection cylindrique conforme oblique de swisstopo.
Le système vient des clés GeoTIFF, de l'en-tête ENVI, ou du fichier .prj voisin ; sans .prj,
une grille en mètres dans l'emprise du Lambert 93, du Lambert II étendu ou du LV95 est supposée
dans ce système (dalles IGN RGE ALTI, BD ALTI, swissALTI3D).

Plusieurs fichiers (dalles IGN, par exemple) sont assemblés : chaque tuile SDF touchée est
échantillonnée dans tous les fichiers qui la recouvrent, le plus fin d'abord. Les données plus
fines que la maille SDF sont d'abord moyennées. Hors des fichiers, la tuile garde le relief du
SDF déjà présent dans le dossier, sinon celui de Copernicus GLO-30 (téléchargé), sinon 0 m.
"""

import math
import re
import struct
import urllib.error
import zipfile
import zlib
from pathlib import Path

import numpy as np

from . import dem, terrain
from .i18n import tr

SUFFIXES = (".tif", ".tiff", ".asc", ".xyz", ".bil", ".bip", ".bsq", ".flt", ".grd")
RAW_SUFFIXES = (".bil", ".bip", ".bsq", ".flt")
RAW_HDR_SUFFIXES = (".dem", ".img")                     # seulement avec un .hdr voisin (GTOPO30, ENVI)
XYZ_SUFFIXES = (".xyz", ".csv", ".txt")                  # .csv et .txt : seulement choisis un par un
HGT_RE = re.compile(r"([NS])(\d{2})([EW])(\d{3})", re.I)

# --- Projections -------------------------------------------------------------------------------

GRS80 = (6378137.0, 1 / 298.257222101)
WGS84 = (6378137.0, 1 / 298.257223563)
CLARKE_1880_IGN = (6378249.2, 1 - 6356515.0 / 6378249.2)
PARIS = 2.33722917                                       # méridien de Paris (2° 20' 14,025" E)
INTERNATIONAL_1924 = (6378388.0, 1 / 297.0)
BESSEL_1841 = (6377397.155, 1 / 299.1528128)
NTF_SHIFT = (-168.0, -60.0, 320.0)                       # NTF → WGS84, translation IGN (m)
# Zones Lambert NTF : latitude d'origine (°), facteur d'échelle, X0 et Y0 de la zone ; en
# cartographique (Lambert « carto », dont le II étendu), Y0 augmenté de zone × 1 000 km.
NTF_ZONES = {1: (49.5, 0.999877341, 600000.0, 200000.0),
             2: (46.8, 0.99987742, 600000.0, 200000.0),
             3: (44.1, 0.999877499, 600000.0, 200000.0),
             4: (42.165, 0.99994471, 234.358, 185861.369)}


class Geographic:
    """Coordonnées lon/lat en degrés."""
    name = "lon/lat"
    metric = False

    def forward(self, lon, lat):
        return lon, lat

    def inverse(self, x, y):
        return x, y


class LambertConformal:
    """Projection conique conforme de Lambert à deux parallèles (EPSG 9802), ou tangente au
    parallèle d'origine avec un facteur d'échelle k0 si lat1 est None (EPSG 9801)."""
    metric = True

    def __init__(self, name, lat0, lon0, lat1, lat2, x0, y0, ellipsoid=GRS80, k0=1.0):
        self.name = name
        a, f = ellipsoid
        self.e = e = math.sqrt(f * (2 - f))
        self.lon0, self.x0, self.y0 = math.radians(lon0), x0, y0

        def m(phi):
            return math.cos(phi) / math.sqrt(1 - (e * math.sin(phi)) ** 2)

        p0 = math.radians(lat0)
        if lat1 is None:
            self.n = math.sin(p0)
            self.aF = a * k0 * m(p0) / (self.n * self._t(p0) ** self.n)
        else:
            p1, p2 = math.radians(lat1), math.radians(lat2)
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


class LambertAzimuthal:
    """Projection azimutale équivalente de Lambert, ellipsoïdale (EPSG 9820)."""
    metric = True

    def __init__(self, name, lat0, lon0, x0, y0, ellipsoid=GRS80):
        self.name = name
        a, f = ellipsoid
        self.e2 = e2 = f * (2 - f)
        self.e = math.sqrt(e2)
        self.lon0, self.x0, self.y0 = math.radians(lon0), x0, y0
        phi0 = math.radians(lat0)
        self.qp = self._q(math.pi / 2)
        self.beta0 = math.asin(self._q(phi0) / self.qp)
        self.rq = a * math.sqrt(self.qp / 2)
        self.d = a * math.cos(phi0) / math.sqrt(1 - e2 * math.sin(phi0) ** 2) / (self.rq * math.cos(self.beta0))
        # Série : latitude authalique → latitude géodésique
        self.series = (e2 / 3 + 31 * e2 ** 2 / 180 + 517 * e2 ** 3 / 5040,
                       23 * e2 ** 2 / 360 + 251 * e2 ** 3 / 3780,
                       761 * e2 ** 3 / 45360)

    def _q(self, phi):
        e, sin = self.e, np.sin(phi)
        return (1 - self.e2) * (sin / (1 - self.e2 * sin * sin) - np.log((1 - e * sin) / (1 + e * sin)) / (2 * e))

    def forward(self, lon, lat):
        beta = np.arcsin(np.clip(self._q(np.radians(lat)) / self.qp, -1, 1))
        dl = np.radians(lon) - self.lon0
        sb0, cb0 = math.sin(self.beta0), math.cos(self.beta0)
        b = self.rq * np.sqrt(2 / (1 + sb0 * np.sin(beta) + cb0 * np.cos(beta) * np.cos(dl)))
        return (self.x0 + b * self.d * np.cos(beta) * np.sin(dl),
                self.y0 + b / self.d * (cb0 * np.sin(beta) - sb0 * np.cos(beta) * np.cos(dl)))

    def inverse(self, x, y):
        dx, dy = np.asarray(x, float) - self.x0, np.asarray(y, float) - self.y0
        rho = np.maximum(np.hypot(dx / self.d, self.d * dy), 1e-9)
        c = 2 * np.arcsin(np.clip(rho / (2 * self.rq), -1, 1))
        sb0, cb0 = math.sin(self.beta0), math.cos(self.beta0)
        beta = np.arcsin(np.clip(np.cos(c) * sb0 + self.d * dy * np.sin(c) * cb0 / rho, -1, 1))
        lon = self.lon0 + np.arctan2(dx * np.sin(c), self.d * rho * cb0 * np.cos(c) - self.d ** 2 * dy * sb0 * np.sin(c))
        phi = beta.copy()
        for j, k in enumerate(self.series, 1):
            phi += k * np.sin(2 * j * beta)
        return np.degrees(lon), np.degrees(phi)


class SwissObliqueMercator:
    """Projection cylindrique conforme oblique de la Suisse (EPSG 9815, formules de swisstopo :
    ellipsoïde → sphère conforme → Mercator oblique)."""
    metric = True

    def __init__(self, name, x0, y0, ellipsoid=BESSEL_1841):
        self.name, self.x0, self.y0 = name, x0, y0
        a, f = ellipsoid
        e2 = f * (2 - f)
        self.e = e = math.sqrt(e2)
        phi0 = math.radians(46 + 57 / 60 + 8.66 / 3600)
        self.lon0 = math.radians(7 + 26 / 60 + 22.5 / 3600)
        self.r = a * math.sqrt(1 - e2) / (1 - e2 * math.sin(phi0) ** 2)
        self.alpha = math.sqrt(1 + e2 / (1 - e2) * math.cos(phi0) ** 4)
        self.b0 = math.asin(math.sin(phi0) / self.alpha)
        self.k = (math.log(math.tan(math.pi / 4 + self.b0 / 2))
                  - self.alpha * math.log(math.tan(math.pi / 4 + phi0 / 2))
                  + self.alpha * e / 2 * math.log((1 + e * math.sin(phi0)) / (1 - e * math.sin(phi0))))

    def forward(self, lon, lat):
        phi, e = np.radians(lat), self.e
        s = (self.alpha * np.log(np.tan(np.pi / 4 + phi / 2))
             - self.alpha * e / 2 * np.log((1 + e * np.sin(phi)) / (1 - e * np.sin(phi))) + self.k)
        b = 2 * (np.arctan(np.exp(s)) - np.pi / 4)
        l = self.alpha * (np.radians(lon) - self.lon0)
        lb = np.arctan2(np.sin(l), math.sin(self.b0) * np.tan(b) + math.cos(self.b0) * np.cos(l))
        bb = np.arcsin(math.cos(self.b0) * np.sin(b) - math.sin(self.b0) * np.cos(b) * np.cos(l))
        return self.x0 + self.r * lb, self.y0 + self.r / 2 * np.log((1 + np.sin(bb)) / (1 - np.sin(bb)))

    def inverse(self, x, y):
        lb = (np.asarray(x, float) - self.x0) / self.r
        bb = 2 * (np.arctan(np.exp((np.asarray(y, float) - self.y0) / self.r)) - np.pi / 4)
        b = np.arcsin(math.cos(self.b0) * np.sin(bb) + math.sin(self.b0) * np.cos(bb) * np.cos(lb))
        l = np.arctan2(np.sin(lb), math.cos(self.b0) * np.cos(lb) - math.sin(self.b0) * np.tan(bb))
        e, phi = self.e, b.copy()
        base = (np.log(np.tan(np.pi / 4 + b / 2)) - self.k) / self.alpha
        for _ in range(8):
            s = base + e * np.log(np.tan(np.pi / 4 + np.arcsin(e * np.sin(phi)) / 2))
            phi = 2 * np.arctan(np.exp(s)) - np.pi / 2
        return np.degrees(self.lon0 + l / self.alpha), np.degrees(phi)


def _geocentric(lon, lat, ellipsoid):
    a, f = ellipsoid
    e2 = f * (2 - f)
    lon, lat = np.radians(lon), np.radians(lat)
    n = a / np.sqrt(1 - e2 * np.sin(lat) ** 2)
    return n * np.cos(lat) * np.cos(lon), n * np.cos(lat) * np.sin(lon), n * (1 - e2) * np.sin(lat)


def _geodetic(x, y, z, ellipsoid):
    a, f = ellipsoid
    e2 = f * (2 - f)
    p = np.hypot(x, y)
    lat = np.arctan2(z, p * (1 - e2))
    for _ in range(6):
        n = a / np.sqrt(1 - e2 * np.sin(lat) ** 2)
        h = p / np.cos(lat) - n
        lat = np.arctan2(z, p * (1 - e2 * n / (n + h)))
    return np.degrees(np.arctan2(y, x)), np.degrees(lat)


class DatumShift:
    """Projection définie sur un autre datum que WGS84 / RGF93 : `shift` est la transformation
    du datum local vers WGS84, translation (tx, ty, tz) en mètres, éventuellement suivie des
    rotations (rx, ry, rz) en secondes d'arc et du facteur d'échelle en ppm (Helmert, convention
    « coordinate frame » de l'EPSG). Altitudes ellipsoïdales ignorées (effet millimétrique)."""

    def __init__(self, proj, ellipsoid, shift):
        self.proj, self.ellipsoid = proj, ellipsoid
        self.name, self.metric = proj.name, proj.metric
        tx, ty, tz, rx, ry, rz, ppm = (tuple(shift) + (0.0,) * 4)[:7]
        self.t = np.array([tx, ty, tz])
        rx, ry, rz = (math.radians(v / 3600) for v in (rx, ry, rz))
        self.m = (1 + ppm * 1e-6) * np.array([[1, rz, -ry], [-rz, 1, rx], [ry, -rx, 1]])
        self.m_inv = np.linalg.inv(self.m)

    def _apply(self, matrix, x, y, z):
        return (matrix[0, 0] * x + matrix[0, 1] * y + matrix[0, 2] * z,
                matrix[1, 0] * x + matrix[1, 1] * y + matrix[1, 2] * z,
                matrix[2, 0] * x + matrix[2, 1] * y + matrix[2, 2] * z)

    def forward(self, lon, lat):
        x, y, z = _geocentric(lon, lat, WGS84)
        x, y, z = self._apply(self.m_inv, x - self.t[0], y - self.t[1], z - self.t[2])
        return self.proj.forward(*_geodetic(x, y, z, self.ellipsoid))

    def inverse(self, x, y):
        x, y, z = self._apply(self.m, *_geocentric(*self.proj.inverse(x, y), self.ellipsoid))
        return _geodetic(x + self.t[0], y + self.t[1], z + self.t[2], WGS84)


GEOGRAPHIC_CODES = {4326, 4258, 4171, 4269, 4019, 4167}


def projection(epsg):
    """Projection d'un code EPSG reconnu, sinon None."""
    if epsg in GEOGRAPHIC_CODES:
        return Geographic()
    if epsg == 2154:
        return LambertConformal("Lambert 93", 46.5, 3.0, 49.0, 44.0, 700000.0, 6600000.0)
    if epsg == 3035:
        return LambertAzimuthal("LAEA Europe", 52.0, 10.0, 4321000.0, 3210000.0)
    if epsg == 3812:
        return LambertConformal("Lambert belge 2008", 50 + 47 / 60 + 52.134 / 3600, 4 + 21 / 60 + 33.177 / 3600,
                                49 + 50 / 60, 51 + 10 / 60, 649328.0, 665262.0)
    if epsg == 31370:
        lambert = LambertConformal("Lambert belge 72", 90.0, 4 + 22 / 60 + 2.952 / 3600,
                                   51 + 10 / 60 + 0.00204 / 3600, 49 + 50 / 60 + 0.00204 / 3600,
                                   150000.013, 5400088.438, INTERNATIONAL_1924)
        # BD72 → WGS84 (EPSG 15929)
        return DatumShift(lambert, INTERNATIONAL_1924,
                          (-106.8686, 52.2978, -103.7239, -0.3366, 0.457, -1.8422, -1.2747))
    if epsg == 2056:                                     # CH1903+ / LV95
        return DatumShift(SwissObliqueMercator("LV95", 2600000.0, 1200000.0), BESSEL_1841,
                          (674.374, 15.056, 405.346))
    if epsg == 21781:                                    # CH1903 / LV03
        return DatumShift(SwissObliqueMercator("LV03", 600000.0, 200000.0), BESSEL_1841,
                          (674.4, 15.1, 405.3))
    # NTF Lambert : 2756x zones, 2757x cartographiques ; 2759x et 2758x, anciens codes des mêmes
    for base, carto in ((27561, False), (27591, False), (27571, True), (27581, True)):
        if base <= epsg <= base + 3:
            zone = epsg - base + 1
            lat0, k0, x0, y0 = NTF_ZONES[zone]
            roman = ("I", "II", "III", "IV")[zone - 1]
            if zone == 2 and carto:
                name = tr("NTF Lambert II étendu")
            else:
                name = f"NTF Lambert {roman}" + (" carto" if carto else "")
            lambert = LambertConformal(name, lat0, PARIS, None, None, x0, y0 + zone * 1000000.0 if carto else y0,
                                       CLARKE_1880_IGN, k0=k0)
            return DatumShift(lambert, CLARKE_1880_IGN, NTF_SHIFT)
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
    if "ntf" in flat or "clarke 1880 ign" in flat or "nouvelle triangulation" in flat:
        code = _ntf_from_wkt(flat)
        if code:
            return projection(code)
    if "lv95" in flat or "ch1903+" in flat:
        return projection(2056)
    if "lv03" in flat or "ch1903" in flat:
        return projection(21781)
    if "lambert 2008" in flat or "lambert2008" in flat:
        return projection(3812)
    if "lambert 72" in flat or "lambert72" in flat or "belge 1972" in flat or "bd72" in flat:
        return projection(31370)
    if "laea" in flat or "lambert azimuthal equal area" in flat:
        return projection(3035) if "etrs" in flat or "europe" in flat else None
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


def _ntf_from_wkt(flat):
    """Code EPSG d'une projection Lambert NTF décrite en WKT (texte aplati) : zone d'après la
    latitude d'origine (degrés ou grades) ou le nom, cartographique d'après la fausse ordonnée."""
    zone = None
    lat = re.search(r'(?:latitude of origin|latitude of natural origin|standard parallel 1)"?\s*,\s*([-\d.]+)', flat)
    if lat:
        value = float(lat.group(1))
        zone = next((z for z, (lat0, *_) in NTF_ZONES.items()
                     if min(abs(value - lat0), abs(value - lat0 / 0.9)) < 0.01), None)
    named = re.search(r"\b(nord|centre|sud|corse)\b", flat)
    if zone is None:
        roman = re.search(r"(?:lambert|france|zone) (iv|iii|ii|i)\b", flat)
        if named:
            zone = ("nord", "centre", "sud", "corse").index(named.group(1)) + 1
        elif roman:
            zone = ("i", "ii", "iii", "iv").index(roman.group(1)) + 1
        else:
            return None
    northing = re.search(r'false northing"?\s*,\s*([-\d.]+)', flat)
    carto = float(northing.group(1)) > 1000000 if northing else not named
    return (27570 if carto else 27560) + zone


def guess_projection(extent):
    """Système supposé d'une grille sans description : lon/lat si l'emprise le permet, Lambert 93
    ou Lambert II étendu si elle tombe dans celle de la France métropolitaine en ce système, LV95
    dans celle de la Suisse (seuls systèmes aux emprises sans ambiguïté)."""
    west, north, east, south = extent
    if -180 <= west < east <= 360 and -90 <= south < north <= 90:
        return Geographic()
    if 0 < west < east < 1300000 and 6000000 < south < north < 7200000:
        return projection(2154)
    if 0 < west < east < 1300000 and 1600000 < south < north < 2800000:
        return projection(27572)
    if 2400000 < west < east < 2900000 and 1000000 < south < north < 1350000:
        return projection(2056)
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

    def _index_window(self, west, north, east, south):
        """Lignes et colonnes (r0, r1, c0, c1) couvrant un rectangle, avec une maille de marge."""
        x0, y0 = self.extent[0], self.extent[1]
        c0 = max(0, int(math.floor((west - x0) / self.dx)) - 1)
        c1 = min(self.cols, int(math.ceil((east - x0) / self.dx)) + 1)
        r0 = max(0, int(math.floor((y0 - north) / self.dy)) - 1)
        r1 = min(self.rows, int(math.ceil((y0 - south) / self.dy)) + 1)
        return r0, r1, c0, c1

    def tiles(self):
        west, south, east, north = self.lonlat_box()
        eps = 1e-6                                        # ~0,1 m : bord calé sur un degré entier
        return [(lat, lon) for lat in range(math.floor(south + eps), math.ceil(north - eps))
                for lon in range(math.floor(west + eps), math.ceil(east - eps))]


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
                                "Lambert 93, CC42 à CC50, UTM, LAEA Europe, NTF Lambert I à IV, Lambert belge 72 / 2008, suisse LV03 / LV95", code=code if code else "?"))

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
        self.proj, self.assumed = _side_projection(path, self.extent)
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
        r0, r1, c0, c1 = self._index_window(west, north, east, south)
        return data[r0:r1, c0:c1], self.extent[0] + c0 * self.dx, self.extent[1] - r0 * self.dy, self.dx, self.dy

    def release(self):
        self._data = None


def _side_projection(path, extent):
    """Système d'une grille sans géoréférencement interne : fichier .prj voisin, sinon supposé
    d'après l'emprise ; renvoie (projection, supposé)."""
    prj = Path(path).with_suffix(".prj")
    if prj.exists():
        proj = projection_from_wkt(prj.read_text(errors="replace"))
        if proj is None:
            raise ValueError(tr("système de coordonnées du .prj non pris en charge ; reconnus : lon/lat, "
                                "Lambert 93, CC42 à CC50, UTM, LAEA Europe, NTF Lambert I à IV, Lambert belge 72 / 2008, suisse LV03 / LV95"))
        return proj, False
    proj = guess_projection(extent)
    if proj is None:
        raise ValueError(tr("système de coordonnées inconnu : ajoutez un fichier .prj à côté de {name}",
                            name=Path(path).name))
    return proj, True


class XyzRaster(Raster):
    """Semis de points « x y z » (ou « lon lat z ») sur une grille régulière, un point par ligne :
    séparateurs espace, tabulation, virgule ou point-virgule (virgule décimale admise avec ce
    dernier), lignes d'en-tête ignorées, points manquants = trous. Les points sont les centres des
    mailles. Le fichier est relu à chaque besoin plutôt que gardé en mémoire."""

    def __init__(self, path):
        self.path = Path(path)
        data = self._grid()
        self.rows, self.cols = data.shape
        self.proj, self.assumed = _side_projection(path, self.extent)
        self._data = None

    def _points(self):
        with open(self.path, "rb") as fh:
            text = fh.read().decode("latin-1")
        lines = text.splitlines()
        start = 0
        while start < len(lines) and not re.match(r"\s*[-+]?\.?\d", lines[start]):
            start += 1
        if start == len(lines):
            raise ValueError(tr("aucun point x y z lisible"))
        body = "\n".join(lines[start:])
        if ";" in lines[start]:
            body = body.replace(",", ".").replace(";", " ")
        elif "," in lines[start]:
            body = body.replace(",", " ")
        ncol = len(body.split("\n", 1)[0].split())
        if ncol < 3:
            raise ValueError(tr("trois colonnes attendues (x y z)"))
        values = np.fromstring(body, dtype=np.float64, sep=" ")
        if values.size < ncol or values.size % ncol:
            raise ValueError(tr("{path} : lignes de longueurs inégales", path=self.path.name))
        values = values.reshape(-1, ncol)
        return values[:, 0], values[:, 1], values[:, 2]

    @staticmethod
    def _step(coords):
        unique = np.unique(coords)
        if unique.size < 2:
            raise ValueError(tr("points non alignés sur une grille régulière"))
        diffs = np.diff(unique)
        diffs = diffs[diffs > (unique[-1] - unique[0]) * 1e-9]
        return float(np.median(diffs)), float(unique[0]), float(unique[-1])

    def _grid(self):
        x, y, z = self._points()
        dx, x_min, x_max = self._step(x)
        dy, y_min, y_max = self._step(y)
        col = (x - x_min) / dx
        row = (y_max - y) / dy
        ci, ri = np.rint(col).astype(np.int64), np.rint(row).astype(np.int64)
        if max(np.abs(col - ci).max(), np.abs(row - ri).max()) > 0.25:
            raise ValueError(tr("points non alignés sur une grille régulière"))
        cols, rows = int(ci.max()) + 1, int(ri.max()) + 1
        if rows * cols > 4 * x.size + 1000 or np.unique(ri * cols + ci).size != x.size:
            raise ValueError(tr("points non alignés sur une grille régulière"))
        grid = np.full((rows, cols), np.nan)
        grid[ri, ci] = z
        grid[grid < -1000] = np.nan                       # valeurs de remplissage (-9999, -99999…)
        self.dx, self.dy = dx, dy
        self.extent = (x_min - dx / 2, y_max + dy / 2, x_min + (cols - 0.5) * dx, y_max - (rows - 0.5) * dy)
        return grid

    def window(self, west, north, east, south, step):
        if self._data is None:
            self._data = self._grid()
        r0, r1, c0, c1 = self._index_window(west, north, east, south)
        return (self._data[r0:r1, c0:c1], self.extent[0] + c0 * self.dx, self.extent[1] - r0 * self.dy,
                self.dx, self.dy)

    def release(self):
        self._data = None


ENVI_TYPES = {1: "u1", 2: "i2", 3: "i4", 4: "f4", 5: "f8", 12: "u2", 13: "u4"}


def _header_path(path):
    """En-tête .hdr d'une grille brute : dalle.hdr ou dalle.img.hdr."""
    path = Path(path)
    for candidate in (path.with_suffix(".hdr"), path.with_name(path.name + ".hdr")):
        if candidate.exists():
            return candidate
    return None


class RawRaster(Raster):
    """Grille binaire décrite par un en-tête .hdr : BIL / BIP / BSQ d'ESRI (GTOPO30, SRTM30,
    exports ArcGIS), grille flottante ESRI (.flt) ou image ENVI. Une seule bande (les trois
    entrelacements sont alors identiques) ; lue à la demande, ligne à ligne."""

    def __init__(self, path):
        self.path = Path(path)
        hdr = _header_path(path)
        if hdr is None:
            raise ValueError(tr("en-tête .hdr introuvable à côté de {name}", name=self.path.name))
        text = hdr.read_text(errors="replace")
        self.proj, self.row_bytes, self.nodata = None, None, None
        try:
            if text.lstrip()[:4].upper() == "ENVI":
                self._envi(text)
            else:
                self._esri(text)
        except (KeyError, IndexError) as exc:
            raise ValueError(tr("en-tête .hdr incomplet ({key})", key=exc))
        if self.bands != 1:
            raise ValueError(tr("fichier à plusieurs bandes non pris en charge (une seule bande d'altitude attendue)"))
        self.row_bytes = self.row_bytes or self.cols * self.dtype.itemsize
        if self.offset + self.row_bytes * self.rows > self.path.stat().st_size:
            raise ValueError(tr("{path} : fichier plus court que ne l'annonce l'en-tête", path=self.path.name))
        west, north = self.extent[0], self.extent[1]
        self.extent = (west, north, west + self.cols * self.dx, north - self.rows * self.dy)
        if self.proj is None:
            self.proj, self.assumed = _side_projection(path, self.extent)
        else:
            self.assumed = False

    def _esri(self, text):
        h = {}
        for line in text.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                h[parts[0].lower()] = parts[1].strip()

        def num(key, default=None):
            return float(h[key]) if key in h else default

        flt = self.path.suffix.lower() == ".flt"
        self.rows, self.cols = int(num("nrows")), int(num("ncols"))
        self.bands = int(num("nbands", 1))
        bits = int(num("nbits", 32 if flt else 8))
        pixeltype = h.get("pixeltype", "float" if flt else "").lower()
        # Altitudes : entiers signés par défaut (GTOPO30 n'indique pas PIXELTYPE)
        kind = "f" if pixeltype.startswith("float") else "u" if pixeltype.startswith("unsigned") or bits == 8 else "i"
        if (kind, bits) not in (("u", 8), ("i", 16), ("u", 16), ("i", 32), ("u", 32), ("f", 32), ("f", 64)):
            raise ValueError(tr("Type de données non pris en charge ({bits} bits).", bits=bits))
        big = h.get("byteorder", "I").upper().startswith("M")
        self.dtype = np.dtype((">" if big else "<") + kind + str(bits // 8))
        self.offset = int(num("skipbytes", 0))
        self.row_bytes = int(num("bandrowbytes", 0) or num("totalrowbytes", 0)) or None
        self.nodata = num("nodata", num("nodata_value"))
        if "xllcorner" in h or "xllcenter" in h:          # en-tête de grille flottante (.flt)
            self.dx = self.dy = num("cellsize")
            west = num("xllcorner") if "xllcorner" in h else num("xllcenter") - self.dx / 2
            south = num("yllcorner") if "yllcorner" in h else num("yllcenter") - self.dy / 2
            self.extent = (west, south + self.rows * self.dy)
        elif "ulxmap" in h:                               # centre de la maille nord-ouest
            self.dx, self.dy = num("xdim"), num("ydim")
            self.extent = (num("ulxmap") - self.dx / 2, num("ulymap") + self.dy / 2)
        else:
            world = self._world_file()
            if world is None:
                raise ValueError(tr("géoréférencement absent (ULXMAP / ULYMAP dans le .hdr ou fichier de calage .blw)"))
            a, d, b, e, c, f = world
            if b or d:
                raise ValueError(tr("grille tournée non prise en charge"))
            self.dx, self.dy = a, -e
            self.extent = (c - a / 2, f - e / 2)

    def _world_file(self):
        suffix = self.path.suffix.lower()
        for ext in (suffix[:2] + suffix[-1] + "w", suffix + "w", ".wld"):  # .blw, .bilw, .wld
            world = self.path.with_suffix(ext)
            if world.exists():
                values = [float(v) for v in world.read_text().split()[:6]]
                if len(values) == 6:
                    return values
        return None

    def _envi(self, text):
        h = {k.strip().lower(): v.strip()
             for k, v in re.findall(r"^\s*([^=\n]+?)\s*=\s*(\{[^}]*\}|[^\n]*)", text, re.M)}
        self.cols, self.rows = int(h["samples"]), int(h["lines"])
        self.bands = int(h.get("bands", 1))
        self.offset = int(h.get("header offset", 0))
        code = int(h["data type"])
        if code not in ENVI_TYPES:
            raise ValueError(tr("Type de données ENVI non pris en charge ({code}).", code=code))
        big = h.get("byte order", "0").strip() == "1"
        self.dtype = np.dtype((">" if big else "<") + ENVI_TYPES[code])
        if "data ignore value" in h:
            self.nodata = float(h["data ignore value"])
        info = [v.strip() for v in h.get("map info", "").strip("{} ").split(",")]
        if len(info) < 7:
            raise ValueError(tr("géoréférencement absent (map info dans le .hdr)"))
        name = info[0].lower()
        refx, refy, easting, northing, self.dx, self.dy = map(float, info[1:7])
        # Le pixel de référence est compté à partir de 1, (1, 1) étant le coin nord-ouest
        self.extent = (easting - (refx - 1) * self.dx, northing + (refy - 1) * self.dy)
        if "coordinate system string" in h:
            self.proj = projection_from_wkt(h["coordinate system string"].strip("{} "))
        elif name.startswith("geographic"):
            self.proj = Geographic()
        elif name == "utm" and len(info) >= 9:
            zone, south = int(info[7]), info[8].lower().startswith("s")
            datum = info[9].lower() if len(info) > 9 else "wgs-84"
            if "1983" in datum or "nad83" in datum:
                self.proj = projection(26900 + zone)
            elif "etrs" in datum:
                self.proj = projection(25800 + zone)
            else:
                self.proj = projection((32700 if south else 32600) + zone)

    def window(self, west, north, east, south, step):
        r0, r1, c0, c1 = self._index_window(west, north, east, south)
        size = self.dtype.itemsize
        raw = np.memmap(self.path, dtype=np.uint8, mode="r", offset=self.offset, shape=(self.rows, self.row_bytes))
        try:
            rows = np.ascontiguousarray(raw[r0:r1, c0 * size:c1 * size])
        finally:
            del raw
        data = rows.view(self.dtype).astype(np.float64)
        if self.nodata is not None:
            data[np.isclose(data, self.nodata, rtol=1e-6, atol=1e-6)] = np.nan
        data[data < -1000] = np.nan                       # valeurs de remplissage (-9999, -32768…)
        return data, self.extent[0] + c0 * self.dx, self.extent[1] - r0 * self.dy, self.dx, self.dy


SURFER_BLANK = 1.70141e38


class SurferRaster(Raster):
    """Grille Surfer (.grd) : ASCII « DSAA », binaire Surfer 6 « DSBB » ou Surfer 7 « DSRB ».
    Les valeurs sont aux nœuds, de la rangée sud à la rangée nord ; vide = 1,70141e38."""

    def __init__(self, path):
        self.path = Path(path)
        with open(path, "rb") as fh:
            magic = fh.read(4)
            if magic == b"DSBB":
                cols, rows = struct.unpack("<hh", fh.read(4))
                x_lo, x_hi, y_lo, y_hi = struct.unpack("<4d", fh.read(32))[:4]
                fh.read(16)                               # zmin, zmax
                self.kind, self.offset, self.dtype = "bin", fh.tell(), np.dtype("<f4")
            elif magic == b"DSRB":
                cols = rows = None
                fh.seek(0)
                while True:
                    head = fh.read(8)
                    if len(head) < 8:
                        raise ValueError(tr("grille Surfer 7 sans section de données"))
                    tag, size = struct.unpack("<4sI", head)
                    if tag == b"GRID":
                        values = struct.unpack("<ii8d", fh.read(72))
                        rows, cols, x_lo, y_lo, dx, dy, _, _, rotation, blank = values
                        if rotation:
                            raise ValueError(tr("grille tournée non prise en charge"))
                        x_hi, y_hi = x_lo + (cols - 1) * dx, y_lo + (rows - 1) * dy
                        fh.seek(size - 72, 1)
                    elif tag == b"DATA" and rows:
                        self.kind, self.offset, self.dtype = "bin", fh.tell(), np.dtype("<f8")
                        break
                    else:                                 # en-tête DSRB, failles (FLTI)…
                        fh.seek(size, 1)
            elif magic == b"DSAA":
                fh.readline()
                cols, rows = (int(v) for v in fh.readline().split()[:2])
                x_lo, x_hi = (float(v) for v in fh.readline().split()[:2])
                y_lo, y_hi = (float(v) for v in fh.readline().split()[:2])
                fh.readline()
                self.kind, self.offset = "text", fh.tell()
            elif magic[:3] == b"CDF" or magic[1:4] == b"HDF":
                raise ValueError(tr("grille GMT / NetCDF non prise en charge (seules les grilles Surfer le sont)"))
            else:
                raise ValueError(tr("grille Surfer attendue (DSAA, DSBB ou DSRB)"))
        if cols < 2 or rows < 2:
            raise ValueError(tr("grille Surfer trop petite"))
        self.rows, self.cols = rows, cols
        self.dx, self.dy = (x_hi - x_lo) / (cols - 1), (y_hi - y_lo) / (rows - 1)
        self.extent = (x_lo - self.dx / 2, y_hi + self.dy / 2, x_hi + self.dx / 2, y_lo - self.dy / 2)
        self.proj, self.assumed = _side_projection(path, self.extent)
        self._data = None

    def _load(self):
        if self._data is None:
            count = self.rows * self.cols
            if self.kind == "text":
                with open(self.path, "rb") as fh:
                    fh.seek(self.offset)
                    values = np.fromstring(fh.read().decode("ascii", "replace"), dtype=np.float64, sep=" ")
            else:
                values = np.fromfile(self.path, dtype=self.dtype, count=count, offset=self.offset).astype(np.float64)
            if values.size < count:
                raise ValueError(tr("{path} : grille incomplète", path=self.path.name))
            data = values[:count].reshape(self.rows, self.cols)[::-1].copy()   # nord en premier
            data[data >= SURFER_BLANK * 0.999] = np.nan
            data[data < -1000] = np.nan
            self._data = data
        return self._data

    def window(self, west, north, east, south, step):
        data = self._load()
        r0, r1, c0, c1 = self._index_window(west, north, east, south)
        return data[r0:r1, c0:c1], self.extent[0] + c0 * self.dx, self.extent[1] - r0 * self.dy, self.dx, self.dy

    def release(self):
        self._data = None


class HgtRaster(Raster):
    """Tuile SRTM : grille carrée big-endian int16 (1201² en 3", 3601² en 1"), nœuds aux degrés
    entiers, position tirée du nom (N48E002) ; -32768 = vide."""

    def __init__(self, path):
        self.path = Path(path)
        ns, lat, ew, lon = HGT_RE.match(self.path.name).groups()
        self.tile = (int(lat) * (-1 if ns.upper() == "S" else 1), int(lon) * (-1 if ew.upper() == "W" else 1))
        raw = terrain.read_hgt(path)
        size = math.isqrt(len(raw) // 2)
        if size < 2 or size * size * 2 != len(raw):
            raise ValueError(tr("taille de fichier .hgt inattendue ({size} octets)", size=len(raw)))
        data = np.frombuffer(raw, dtype=">i2").reshape(size, size).astype(np.float64)
        data[data == -32768] = np.nan
        self._data = data
        self.dx = self.dy = 1 / (size - 1)
        half = self.dx / 2
        lat, lon = self.tile
        self.extent = (lon - half, lat + 1 + half, lon + 1 + half, lat - half)
        self.proj, self.assumed = Geographic(), False

    def tiles(self):
        return [self.tile]                                # pas les tuiles voisines (demi-maille de bord)

    def window(self, west, north, east, south, step):
        return self._data, self.extent[0], self.extent[1], self.dx, self.dy


def is_hgt(path):
    """Tuile SRTM d'après son nom : N48E002.hgt, .hgt.gz ou .zip (N48E002.SRTMGL1.hgt.zip…)."""
    name = Path(path).name.lower()
    return bool(HGT_RE.match(name)) and name.endswith((".hgt", ".hgt.gz", ".zip"))


def supported(path):
    """Fichier importable par ce module dans un dossier : GeoTIFF, ASCII Grid, Surfer, grille brute
    avec .hdr, XYZ ou tuile SRTM."""
    suffix = Path(path).suffix.lower()
    return (suffix in SUFFIXES or is_hgt(path)
            or suffix in RAW_HDR_SUFFIXES and _header_path(path) is not None)


def open_raster(path):
    if Path(path).suffix.lower() == ".asc":
        return AsciiGridRaster(path)
    if Path(path).suffix.lower() in XYZ_SUFFIXES:
        return XyzRaster(path)
    if Path(path).suffix.lower() == ".grd":
        return SurferRaster(path)
    if Path(path).suffix.lower() in RAW_SUFFIXES + RAW_HDR_SUFFIXES:
        return RawRaster(path)
    if is_hgt(path):
        return HgtRaster(path)
    return GeoTiffRaster(path)


def find_files(inputs):
    """Fichiers importables désignés directement ou contenus dans des dossiers."""
    files = []
    for item in map(Path, inputs):
        if item.is_dir():
            files += sorted(p for p in item.rglob("*") if supported(p))
        else:
            files.append(item)
    return files


# --- Échantillonnage ---------------------------------------------------------------------------

def _block_mean(window, factor):
    """Moyenne par blocs factor × factor (trous ignorés) : données plus fines que la maille SDF."""
    data, west, north, dx, dy = window
    h, w = -(-data.shape[0] // factor) * factor, -(-data.shape[1] // factor) * factor
    if h == factor or w == factor:
        return window
    # Bords complétés par des trous plutôt que tronqués : la dernière rangée et la dernière
    # colonne restent couvertes (tuile SRTM de 3601², par exemple).
    padded = np.full((h, w), np.nan)
    padded[:data.shape[0], :data.shape[1]] = data
    blocks = padded.reshape(h // factor, factor, w // factor, factor)
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
        except (OSError, ValueError, KeyError, IndexError, struct.error, EOFError, StopIteration,
                zipfile.BadZipFile) as exc:
            failed.append(Path(path))
            log(tr("Échec de {src} : {exc}\n", src=Path(path).name, exc=exc))
    return rasters, failed


def import_files(files, sdf_dir, hd, log=print, cancel=lambda: False, progress=lambda n, total: None):
    """Convertit des fichiers de relief (GeoTIFF, ASCII Grid, Surfer, BIL, XYZ, SRTM) en tuiles SDF ; résumé
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
                if hasattr(raster, "release"):
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
