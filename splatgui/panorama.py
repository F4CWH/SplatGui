"""Panorama à 360° du relief vu depuis un site (émetteur), calculé à partir des fichiers SDF.

Pour chaque azimut, le relief est échantillonné le long du rayon (pas des SDF près du site,
puis de plus en plus large) ; l'angle de site d'un point vaut
    atan((altitude - altitude d'observation - d² / (2 k R)) / d)
(courbure terrestre et réfraction : rayon effectif k × R, k = -m comme le profil de liaison).
Le panorama est dessiné par plans successifs, du plus lointain au plus proche : chaque plan
masque naturellement ceux qu'il cache.

Les localités (villes et bourgs dans tout le rayon, villages à moins de 30 km) viennent
d'OpenStreetMap (API Overpass, réponses mises en cache), ou à défaut du calque GeoJSON local
« communes » ; leurs noms sont affichés en capitales au-dessus de l'horizon, d'autant plus
transparents que la localité est lointaine.

Placage : une carte (IGN carte physique, Plan IGN, photographies…) peut être plaquée sur le
relief. Chaque point échantillonné prend la couleur de la carte à sa position ; le panorama est
alors calculé pixel par pixel (pour chaque colonne, la ligne de visée rencontre le premier point
dont l'angle de site cumulé dépasse celui du pixel), avec un voile de lointain.
"""

import json
import math
import re
import urllib.parse
import urllib.request

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (QColor, QFont, QFontMetricsF, QImage, QLinearGradient, QPainter, QPainterPath, QPalette,
                         QPen, QPolygonF)
from PyQt6.QtWidgets import QWidget

from . import basemap, layers
from .palettes import PALETTES, colorize, gradient
from .basemap import TERRAIN_DIR, USER_AGENT
from .i18n import N_, tr
from .linkprofile import EARTH_RADIUS_M, _sdf_path, ground_elevations, read_sdf, units

AZ_STEP = 0.5          # degrés entre deux rayons
BANDS = 28             # plans de profondeur
CARDINALS = {0: "N", 45: "NE", 90: "E", 135: "SE", 180: "S", 225: "SO", 270: "O", 315: "NO"}
MIN_FOV = 90.0         # champ de vision horizontal minimal (degrés)
DEFAULT_FOV = 120.0    # champ de vision à l'ouverture (Origine / double-clic : vue complète, 360°)
FOV_STEPS = (90, 120, 150, 180, 210, 240, 270, 300, 330, 360)   # choix proposés dans la liste déroulante
ELEVATION_RANGE = (-5.0, 20.0)   # angles de site affichables (degrés)
HEIGHT_FRACTION = 0.48 # part de la hauteur disponible occupée par le panorama (onglet et export)
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
PLACES_DIR = TERRAIN_DIR / "lieux"
PLACE_RANK = {"city": 0, "town": 1, "village": 2}
# Population supposée quand OpenStreetMap ne la donne pas (selon le type de localité).
ASSUMED_POPULATION = {0: 50000, 1: 2000, 2: 0, 3: 0}
POPULATION_STEPS = (0, 500, 1000, 2000, 5000, 10000, 15000, 20000, 30000, 50000, 100000)
VILLAGE_RADIUS_KM = 30
MAX_SIGNS = 60
MAP_EXTENT = 1.6               # demi-côté du relief de la carte, en rayons du panorama
MAP_PLACES = MAP_EXTENT * math.sqrt(2)   # portée des localités de la carte (coins compris), en rayons
MAP_MAX_PLACES = 400
# Ciel : (haut du cadre, horizon) en thème clair puis sombre ; la couleur d'horizon sert aussi de voile de lointain
SKY = ((QColor(92, 146, 212), QColor(214, 230, 245)), (QColor(16, 30, 56), QColor(56, 82, 116)))
# Brume (atténuation des couleurs vers celle de l'horizon avec la distance) : part au bord du rayon
HAZE_LEVELS = ((N_("Aucune"), 0.0), (N_("Légère"), 0.35), (N_("Moyenne"), 0.6), (N_("Forte"), 0.85))


def _distances(step, max_m):
    """Distances d'échantillonnage : dès deux mailles SDF (la maille du site et ses voisines sont
    trop imprécises à quelques dizaines de mètres), au pas des SDF puis + 0,5 % par pas."""
    out, d = [], 2 * step
    while d <= max_m:
        out.append(d)
        d += max(step, d * 0.005)
    return np.asarray(out)


def _destinations(lat, lon, azimuths, distances):
    """Latitudes et longitudes (grilles azimut × distance) sur la sphère terrestre."""
    p1, l1 = math.radians(lat), math.radians(lon)
    az = np.radians(azimuths)[:, None]
    delta = (distances / EARTH_RADIUS_M)[None, :]
    p2 = np.arcsin(np.sin(p1) * np.cos(delta) + np.cos(p1) * np.sin(delta) * np.cos(az))
    l2 = l1 + np.arctan2(np.sin(az) * np.sin(delta) * np.cos(p1), np.cos(delta) - np.sin(p1) * np.sin(p2))
    return np.degrees(p2), (np.degrees(l2) + 180) % 360 - 180


def ground_bilinear(lats, lons, sdf_dirs, hd, missing=0.0):
    """Altitudes (m) interpolées entre les quatre mailles SDF voisines (relief sans marches) ;
    `missing` hors des SDF."""
    lats, lons = np.asarray(lats, dtype=float), ((np.asarray(lons, dtype=float) + 180) % 360) - 180
    out = np.full(lats.shape, missing)
    tiles = np.floor(np.stack([lats, lons], axis=-1)).astype(int)
    for tile in {tuple(t) for t in tiles.reshape(-1, 2).tolist()}:
        mask = (tiles[..., 0] == tile[0]) & (tiles[..., 1] == tile[1])
        path = next((p for p in (_sdf_path(d, tile, hd) for d in sdf_dirs if d) if p), None)
        if path is None:
            continue
        grid = read_sdf(str(path)).astype(float)
        n = grid.shape[0]
        rows = (tile[0] + 1 - lats[mask]) * n - 1          # rangée 0 = nord - 1 maille
        cols = (lons[mask] - tile[1]) * n
        r0 = np.clip(np.floor(rows).astype(int), 0, n - 2)
        c0 = np.clip(np.floor(cols).astype(int), 0, n - 2)
        fr, fc = np.clip(rows - r0, 0, 1), np.clip(cols - c0, 0, 1)
        out[mask] = (grid[r0, c0] * (1 - fr) * (1 - fc) + grid[r0, c0 + 1] * (1 - fr) * fc
                     + grid[r0 + 1, c0] * fr * (1 - fc) + grid[r0 + 1, c0 + 1] * fr * fc)
    return out


def sample(site, sdf_dirs, hd, max_km):
    """Relief autour du site : {"azimuth", "distance", "ground" (azimut × distance), "site_ground"}."""
    azimuths = np.arange(0, 360, AZ_STEP)
    distances = _distances(30.0 if hd else 90.0, max_km * 1000.0)
    lat, lon = float(site["lat"]), float(site["lon"])
    lats, lons = _destinations(lat, lon, azimuths, distances)
    ground = ground_bilinear(lats, lons, sdf_dirs, hd)
    site_ground = float(ground_elevations(np.array([lat]), np.array([lon]), sdf_dirs, hd)[0])
    return {"azimuth": azimuths, "distance": distances, "ground": ground, "site_ground": site_ground,
            "missing": bool(np.all(ground == 0)), "lat": lats, "lon": lons}


DRAPE_SOURCES = ("ign_physique", "ign_plan", "ign_ortho", "opentopo", "osm")


def drape_colors(data, source_key, width=2048):
    """Couleurs RGBA (azimut × distance) de la carte `source_key` aux points échantillonnés."""
    lats, lons = data["lat"], data["lon"]
    west, east = float(lons.min()), float(lons.max())
    south, north = float(lats.min()), float(lats.max())
    height = max(2, round(width * (north - south) / max(east - west, 1e-6)))
    ref = basemap.GeoRef(west, north, east, south, width, height)
    image = basemap.build_basemap(source_key, ref).convertToFormat(QImage.Format.Format_RGBA8888)
    ptr = image.constBits()
    ptr.setsize(image.sizeInBytes())
    pixels = np.frombuffer(ptr, np.uint8).reshape(height, image.bytesPerLine() // 4, 4)[:, :width]
    cols = np.clip(np.rint((lons - west) / max(east - west, 1e-9) * (width - 1)).astype(int), 0, width - 1)
    rows = np.clip(np.rint((north - lats) / max(north - south, 1e-9) * (height - 1)).astype(int), 0, height - 1)
    return pixels[rows, cols].copy()


CONTOUR_STEPS = (5, 10, 20, 25, 50, 100, 200, 250, 500, 1000)
CONTOUR_MASTER = 5     # une courbe maîtresse toutes les cinq courbes


def contour_step(low, high, bands=12):
    """Équidistance des courbes de niveau (m) : une douzaine de tranches sur la plage d'altitudes."""
    return next((s for s in CONTOUR_STEPS if (high - low) / s <= bands), CONTOUR_STEPS[-1])


def contour_shading(ground, step):
    """Facteur d'éclairement des courbes de niveau sur une image d'altitudes (lignes × colonnes,
    NaN hors relief) : plus sombre sur les courbes, davantage sur les maîtresses, 1 ailleurs
    (pas de dégradé par tranche : rendu continu, sans bandes).
    Un pixel est sur une courbe quand son voisin du dessous ou de droite est dans une autre tranche
    d'altitude : courbes d'un pixel d'épaisseur quelle que soit la distance."""
    valid = ~np.isnan(ground)
    level = np.floor(np.where(valid, ground, 0) / step).astype(np.int64)
    out = np.ones(ground.shape, dtype=np.float32)
    for axis in (0, 1):
        other = np.concatenate([np.take(level, np.arange(1, level.shape[axis]), axis=axis),
                                np.take(level, [-1], axis=axis)], axis=axis)
        other_valid = np.concatenate([np.take(valid, np.arange(1, valid.shape[axis]), axis=axis),
                                      np.take(valid, [-1], axis=axis)], axis=axis)
        crossed = (level != other) & valid & other_valid
        master = crossed & (np.maximum(level, other) % CONTOUR_MASTER == 0)
        out = np.minimum(out, np.where(master, 0.5, np.where(crossed, 0.72, 1.0)))
    return out


def slope_light(data):
    """Éclairement (azimut × distance) selon la pente vue depuis le site : versants tournés vers
    l'observateur plus clairs."""
    slope = np.gradient(data["ground"], data["distance"], axis=1)
    return 0.82 + 0.28 * np.tanh(slope * 8)


# Sens du dégradé : vertical (selon l'altitude) ou horizontal (selon la distance au site)
GRADIENT_MODES = (("altitude", N_("Vertical (altitude)")), ("distance", N_("Horizontal (distance)")))


def map_terrain(lat, lon, half, sdf_dirs, hd, size=640):
    """Altitudes (m) d'une grille carrée orientée au nord (rangée 0 au nord) de demi-côté `half`
    autour du site (projection locale équirectangulaire : mêmes mètres par pixel en x et en y,
    écart négligeable à l'échelle du rayon) : relief de la carte sur tout le cadre ; NaN hors
    des SDF chargés."""
    c = (np.arange(size) + 0.5 - size / 2) / (size / 2) * half
    dx, dy = np.meshgrid(c, c)
    lats = lat - dy / 110540.0
    lons = lon + dx / (111320.0 * max(math.cos(math.radians(lat)), 0.01))
    return ground_bilinear(lats, lons, sdf_dirs, hd, missing=np.nan)


def map_layers(lat, lon, radius_m, sdf_dirs, hd, extent=MAP_EXTENT):
    """Relief de la carte physique du panorama : {"half" : demi-côté en m, "ground" : altitudes}
    d'un carré de demi-côté `extent` × rayon, pour remplir aussi les coins du cadre."""
    half = radius_m * extent
    return {"half": half, "ground": map_terrain(lat, lon, half, sdf_dirs, hd)}


def hillshade(ground, pixel_m, exaggeration=2.0):
    """Éclairement relatif (1 = terrain plat) d'une grille d'altitudes orientée au nord, lumière
    venant du nord-ouest."""
    gy, gx = np.gradient(ground, pixel_m)            # gy : vers le sud
    nx, ny = -exaggeration * gx, exaggeration * gy   # normale (est, nord, haut) non normée
    light = np.array([-1.0, 1.0, 1.4]) / math.sqrt(3.96)
    shade = (nx * light[0] + ny * light[1] + light[2]) / np.sqrt(nx * nx + ny * ny + 1)
    return shade / light[2]


def altitude_colors(data, palette, mode="altitude"):
    """Couleurs RGBA (azimut × distance) du dégradé `palette`, étalé sur toute la plage d'altitudes
    échantillonnées (`mode` "altitude") ou de distances (`mode` "distance", échelle logarithmique :
    plans proches et lointains également contrastés), éclairées selon la pente vue depuis le site
    (versants tournés vers l'observateur plus clairs) ; (couleurs, (valeur basse, haute))."""
    ground = data["ground"]
    if mode == "distance":
        d = data["distance"]
        low, high = float(d[0]), float(d[-1])
        t = np.broadcast_to((np.log(d / low) / np.log(max(high / low, 1.0001)))[None, :], ground.shape)
    else:
        low, high = float(ground.min()), float(ground.max())
        high = max(high, low + 1.0)
        t = (ground - low) / (high - low)
    rgb = colorize(t, palette)
    out = np.empty(ground.shape + (4,), dtype=np.uint8)
    out[..., :3] = np.clip(rgb * slope_light(data)[..., None], 0, 255)
    out[..., 3] = 255
    return out, (low, high)


def view(data, height_m, k):
    """Angles de site (degrés) pour une hauteur d'observation au-dessus du sol, et plans de
    profondeur : (angles, plans [plan × azimut], distances limites des plans, distance de
    l'horizon visible par azimut)."""
    d = data["distance"]
    eye = data["site_ground"] + height_m
    drop = d ** 2 / (2 * k * EARTH_RADIUS_M)
    angles = np.degrees(np.arctan2(data["ground"] - eye - drop[None, :], d[None, :]))
    edges = np.geomspace(d[0], d[-1] * 1.0001, BANDS + 1)
    index = np.clip(np.searchsorted(edges, d, side="right") - 1, 0, BANDS - 1)
    bands = np.full((BANDS, angles.shape[0]), np.nan)
    for b in range(BANDS):
        sel = index == b
        if sel.any():
            bands[b] = angles[:, sel].max(axis=1)
    horizon = angles.max(axis=1)
    horizon_distance = d[np.argmax(angles, axis=1)]
    return angles, bands, edges, horizon, horizon_distance


def _overpass_places(lat, lon, radius_km):
    villages = min(radius_km, VILLAGE_RADIUS_KM) * 1000
    around = lambda r: f"(around:{r:.0f},{lat:.5f},{lon:.5f})"
    query = (f'[out:json][timeout:90];(node{around(radius_km * 1000)}[place~"^(city|town)$"][name];'
             f'node{around(villages)}[place=village][name];);out body;')
    request = urllib.request.Request(OVERPASS_URL, data=urllib.parse.urlencode({"data": query}).encode(),
                                     headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = json.load(response)
    places = []
    for element in payload.get("elements", []):
        tags = element.get("tags", {})
        digits = re.sub(r"\D", "", tags.get("population", ""))
        places.append({"name": tags.get("name", ""), "lat": element["lat"], "lon": element["lon"],
                       "rank": PLACE_RANK.get(tags.get("place"), 3), "population": int(digits) if digits else 0})
    return places


def _local_places(lat, lon, radius_km):
    """Centres des communes du calque GeoJSON local (niveau le plus simplifié), ou []."""
    layer = layers.discover().get("communes")
    if not layer:
        return []
    features = layers.load(layer["levels"][max(layer["levels"])])
    dlat = radius_km / 111.0
    dlon = dlat / max(math.cos(math.radians(lat)), 0.05)
    return [{"name": name, "lat": c[1], "lon": c[0], "rank": 2, "population": 0}
            for name, _kind, _arrays, _bbox, c, _area in features
            if name and abs(c[1] - lat) <= dlat and abs(c[0] - lon) <= dlon]


def population(place):
    return place.get("population") or ASSUMED_POPULATION.get(place.get("rank", 3), 0)


def places_around(site, radius_km, sdf_dirs, hd):
    """Localités autour du site, avec azimut, distance et altitude du sol ; (localités, source)."""
    lat, lon = float(site["lat"]), float(site["lon"])
    cache = PLACES_DIR / f"{lat:.3f}_{lon:.3f}_{radius_km:.0f}km.json"
    source = "OpenStreetMap"
    if cache.exists():
        places = json.loads(cache.read_text(encoding="utf-8"))
    else:
        try:
            places = _overpass_places(lat, lon, radius_km)
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(places, ensure_ascii=False), encoding="utf-8")
        except (OSError, ValueError) as exc:          # hors ligne : calque local, s'il existe
            places, source = _local_places(lat, lon, radius_km), "GeoJSON"
            if not places:
                raise OSError(tr("localités indisponibles ({error})", error=exc)) from exc
    out = []
    for place in places:
        distance = _distance_m(lat, lon, place["lat"], place["lon"])
        if 300 < distance <= radius_km * 1000 and place["name"]:
            out.append(dict(place, azimuth=bearing(lat, lon, place["lat"], place["lon"]), distance=distance))
    if out:
        grounds = ground_bilinear(np.array([p["lat"] for p in out]), np.array([p["lon"] for p in out]), sdf_dirs, hd)
        for place, ground in zip(out, grounds):
            place["ground"] = float(ground)
    return out, source


def bearing(lat1, lon1, lat2, lon2):
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    return math.degrees(math.atan2(math.sin(dl) * math.cos(p2),
                                   math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) % 360


def _distance_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _mix(a, b, f):
    return QColor(round(a.red() + (b.red() - a.red()) * f), round(a.green() + (b.green() - a.green()) * f),
                  round(a.blue() + (b.blue() - a.blue()) * f))


class PanoramaView(QWidget):
    """Panorama : molette ou +/- = champ de vision, glisser ou flèches gauche/droite = tourner,
    Maj + molette ou Page préc./suiv. = angle de vue (lever / baisser les yeux), Origine = vue
    complète ; survol = azimut / angle / distance. Vue prise à la hauteur de l'antenne."""

    hovered = pyqtSignal(str)
    repainted = pyqtSignal()       # vue redessinée (orientation, champ, curseur…) : carte à synchroniser

    def __init__(self, parent=None):
        super().__init__(parent)
        self.data = None
        self.site = None
        self.markers = []          # [(nom, azimut, angle de site, distance m)]
        self.height_m = 10.0
        self.k = 4 / 3
        self.metric = True
        self.center = 180.0        # azimut au centre de la vue
        self.fov = DEFAULT_FOV     # champ de vision horizontal (degrés)
        self.tilt = 0.0            # angle de vue : décalage vertical de la fenêtre (degrés, + = vers le haut)
        self.exaggeration = None   # exagération du relief imposée, ou None : relief ajusté à la hauteur
        self.places = []           # localités : {"name", "azimuth", "distance", "ground", "rank", "population"}
        self.show_places = True
        self.min_population = 0    # seuil d'habitants des localités affichées
        self.texture = None        # couleurs plaquées (azimut × distance × RGBA), ou None
        self.palette_name = ""     # dégradé d'altitude (PALETTES), utilisé sans carte plaquée
        self._altitude = None      # (clé, couleurs du dégradé, (altitude basse, haute))
        self.contours = False      # teintes par tranches et courbes de niveau
        self.gradient_mode = "altitude"   # sens du dégradé (GRADIENT_MODES)
        self.haze = 0.6            # brume au bord du rayon (0 = aucune)
        self._shaded = None        # (clé, couleurs avec voile de lointain)
        self._draped = None        # (clé de la vue, image plaquée) : réutilisée au survol
        self.cursor_az = None
        self._drag = None
        self._view = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(240)

    def set_data(self, data, site, markers, height_m, k, metric):
        self.data, self.site, self.metric = data, site, metric
        self._shaded = None
        self.markers, self.k = markers, k
        self.set_height(height_m)

    def set_height(self, height_m):
        self.height_m = float(height_m)
        self._view = view(self.data, self.height_m, self.k) if self.data else None
        self.update()

    def set_places(self, places):
        self.places = places or []
        self.update()

    def set_texture(self, colors):
        self.texture = colors
        self._shaded = None
        self.update()

    def set_palette(self, name):
        self.palette_name = name if name in PALETTES else ""
        self._shaded = None
        self.update()

    def set_contours(self, show):
        self.contours = bool(show)
        self._shaded = None
        self.update()

    def set_gradient_mode(self, mode):
        self.gradient_mode = mode if mode in dict(GRADIENT_MODES) else "altitude"
        self._shaded = None
        self._draped = None
        self.update()

    def set_haze(self, strength):
        self.haze = float(strength)
        self._shaded = None
        self.update()

    def _colors(self):
        """Couleurs du relief (azimut × distance × RGBA) : carte plaquée, sinon dégradé d'altitude,
        sinon None (plans gris)."""
        if self.texture is not None and self.texture.shape[:2] == self._view[0].shape:
            return self.texture
        return self._altitude_colors()[0]

    def _palette(self):
        """Dégradé d'altitude utilisé : celui choisi, ou l'hypsométrique pour les courbes de niveau."""
        return self.palette_name or ("hypso" if self.contours else "")

    def _altitude_colors(self):
        if not self._palette() or not self.data:
            return None, None
        key = (id(self.data), self._palette(), self.gradient_mode)
        if self._altitude is None or self._altitude[0] != key:
            self._altitude = (key, *altitude_colors(self.data, self._palette(), self.gradient_mode))
        return self._altitude[1], self._altitude[2]

    def _shaded_texture(self, base, text):
        """Couleurs plaquées voilées par la distance (`base` : couleur de la brume) ; gris neutre là
        où la carte manque."""
        colors = self._colors()
        key = (id(colors), self.haze, base.rgb(), text.rgb())
        if self._shaded is None or self._shaded[0] != key:
            d = self.data["distance"]
            haze = (self.haze * (d / d[-1]) ** 0.8)[None, :, None]
            sky = np.array([base.red(), base.green(), base.blue()], dtype=np.float32)
            grey = np.array([c for c in (_mix(base, text, 0.45).red(), _mix(base, text, 0.45).green(),
                                         _mix(base, text, 0.45).blue())], dtype=np.float32)
            rgb = colors[..., :3].astype(np.float32)
            rgb = np.where(colors[..., 3:4] >= 128, rgb, grey)
            self._shaded = (key, np.clip(rgb * (1 - haze) + sky * haze, 0, 255).astype(np.uint8))
        return self._shaded[1]

    def _draped_image(self, rect, low, high, col_az, base, text):
        """Panorama plaqué, calculé colonne par colonne (azimut interpolé entre deux rayons), à la
        résolution des rayons (≤ 720 colonnes, moitié pendant un glisser) puis agrandi avec lissage ;
        mis en cache tant que la vue ne change pas."""
        width = len(col_az)
        height = max(1, int(round(rect.height())))
        key = (self.center, self.fov, low, high, width, height, id(self._colors()), self.haze, id(self._view), self.contours,
               base.rgb(), text.rgb(), bool(self._drag))
        if self._draped and self._draped[0] == key:
            return self._draped[1]
        limit = 360 if self._drag else 720
        count = int(min(width, limit, math.ceil(self.fov / AZ_STEP) * 2))
        if count < width:
            col_az = np.interp(np.linspace(0, width - 1, count), np.arange(width), np.unwrap(col_az, period=360)) % 360
        image = self._draped_columns(height, low, high, col_az, base, text)
        if count < width:
            image = image.scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        self._draped = (key, image)
        return image

    def _draped_columns(self, height, low, high, col_az, base, text):
        angles = self._view[0]
        n_az, n_d = angles.shape
        rows_angle = high - (np.arange(height) + 0.5) / height * (high - low)
        position = col_az / AZ_STEP
        r0 = np.floor(position).astype(int) % n_az
        weight = position - np.floor(position)
        r1 = (r0 + 1) % n_az
        colors = self._shaded_texture(base, text)
        ground = self.data["ground"]
        # Dégradé d'altitude (sans carte plaquée) recalculé pixel par pixel : teinte continue, sans
        # bandes d'un plan de relief à l'autre
        smooth = (self.gradient_mode == "altitude" and bool(self._palette())
                  and (self.texture is None or self.texture.shape[:2] != angles.shape))
        altitude = np.full((height, len(col_az)), np.nan) if self.contours or smooth else None
        sample = np.full((height, len(col_az)), -1, dtype=np.int64) if smooth else None
        out = np.zeros((height, len(col_az), 4), dtype=np.uint8)
        for j in range(len(col_az)):
            ray = angles[r0[j]] * (1 - weight[j]) + angles[r1[j]] * weight[j]
            cumulative = np.maximum.accumulate(ray)
            index = np.searchsorted(cumulative, rows_angle)
            visible = index < n_d
            k = index[visible]               # couleur mélangée entre les deux rayons voisins
            out[visible, j, :3] = (colors[r0[j], k] * (1 - weight[j]) + colors[r1[j], k] * weight[j]).astype(np.uint8)
            out[visible, j, 3] = 255
            if altitude is not None:         # altitude du pixel, interpolée entre deux points du rayon
                g = ground[r0[j]] * (1 - weight[j]) + ground[r1[j]] * weight[j]
                before = np.maximum(k - 1, 0)
                span = cumulative[k] - cumulative[before]
                f = np.clip((rows_angle[visible] - cumulative[before]) / np.where(span > 0, span, 1), 0, 1)
                altitude[visible, j] = g[before] + f * (g[k] - g[before])
                if sample is not None:
                    sample[visible, j] = k
        if sample is not None:
            self._smooth_tints(out, altitude, sample, r0, r1, weight, base)
        if self.contours:
            shading = contour_shading(altitude, contour_step(float(ground.min()), float(ground.max())))[..., None]
            out[..., :3] = np.clip(out[..., :3] * shading, 0, 255).astype(np.uint8)
        return QImage(out.data, out.shape[1], height, out.shape[1] * 4, QImage.Format.Format_RGBA8888).copy()

    def _smooth_tints(self, out, altitude, sample, r0, r1, weight, base):
        """Remplace la couleur des pixels du relief par la teinte du dégradé à leur propre altitude
        (interpolée dans le pixel), avec l'éclairement des pentes et la brume du point échantillonné."""
        rows, cols = np.nonzero(sample >= 0)
        k = sample[rows, cols]
        w = weight[cols]
        light = slope_light(self.data)
        lit = light[r0[cols], k] * (1 - w) + light[r1[cols], k] * w
        d = self.data["distance"]
        haze = self.haze * (d[k] / d[-1]) ** 0.8
        ground = self.data["ground"]
        low, high = float(ground.min()), max(float(ground.max()), float(ground.min()) + 1.0)
        rgb = colorize(np.clip((altitude[rows, cols] - low) / (high - low), 0, 1), self._palette()) * lit[:, None]
        sky = np.array([base.red(), base.green(), base.blue()], dtype=np.float64)
        rgb = rgb * (1 - haze)[:, None] + sky * haze[:, None]
        out[rows, cols, :3] = np.clip(rgb, 0, 255).astype(np.uint8)

    def set_min_population(self, value):
        self.min_population = int(value or 0)
        self.update()

    def set_show_places(self, show):
        self.show_places = bool(show)
        self.update()

    def set_exaggeration(self, factor):
        self.exaggeration = float(factor) if factor else None
        self.update()

    def reset_view(self):
        self.fov, self.center, self.tilt = 360.0, 180.0, 0.0
        self.update()

    def set_fov(self, fov):
        self.fov = min(360.0, max(MIN_FOV, float(fov)))
        self.update()

    def center_on(self, azimuth):
        self.center = azimuth % 360
        self.update()

    def marker_angles(self):
        """Angle de site de chaque repère pour la hauteur d'observation courante."""
        if not self.data:
            return []
        eye = self.data["site_ground"] + self.height_m
        out = []
        for name, azimuth, altitude, distance in self.markers:
            drop = distance ** 2 / (2 * self.k * EARTH_RADIUS_M)
            out.append((name, azimuth, math.degrees(math.atan2(altitude - eye - drop, max(distance, 1.0))), distance))
        return out

    # Repère ------------------------------------------------------------------------
    def _rect(self, width, height):
        fm = QFontMetricsF(self.font())
        lh = fm.height()
        return QRectF(fm.horizontalAdvance("-00.0°") + lh, lh * 1.8, width - fm.horizontalAdvance("-00.0°") - lh * 2,
                      height - lh * 4.2)

    def _x(self, rect, azimuth):
        offset = (azimuth - self.center + 540) % 360 - 180
        return rect.center().x() + offset * rect.width() / self.fov

    def _azimuth(self, rect, x):
        return (self.center + (x - rect.center().x()) * self.fov / rect.width()) % 360

    def _vertical_range(self, rect):
        """Plage d'angles de site affichée : relief visible dans le champ + marge."""
        _angles, _bands, _edges, horizon, _hd = self._view
        az = self.data["azimuth"]
        offset = (az - self.center + 540) % 360 - 180
        visible = np.abs(offset) <= self.fov / 2 + AZ_STEP
        top = float(horizon[visible].max()) if visible.any() else 1.0
        marks = [a for _n, azm, a, _d in self.marker_angles() if abs((azm - self.center + 540) % 360 - 180) <= self.fov / 2]
        top = max([top, 0.0] + marks)
        # Bas : relief au-delà du vingtième du rayon (le premier plan, plus bas, remplit le pied).
        far = self.data["distance"] >= self.data["distance"][-1] / 20
        low = float(self._view[0][visible][:, far].min()) if visible.any() and far.any() else -1.0
        low = min(low, -0.2, *(marks or [0.0]))
        span = max(top - low, 0.5)
        low, high = low - span * 0.08, top + span * 0.25
        if self.exaggeration:
            # Exagération imposée : degrés verticaux = degrés horizontaux / exagération, autour du relief.
            middle = (low + high) / 2
            half = rect.height() / (self.exaggeration * rect.width() / self.fov) / 2
            low, high = middle - half, middle + half
        return self._clamp_window(low + self.tilt, high + self.tilt)

    @staticmethod
    def _clamp_window(low, high):
        """Fenêtre verticale ramenée dans ELEVATION_RANGE (réduite à cette plage si plus large)."""
        bottom, top = ELEVATION_RANGE
        span = min(high - low, top - bottom)
        low = min(max(low, bottom), top - span)
        return low, low + span

    def _tilt_by(self, rect, step):
        """Change l'angle de vue sans sortir de ELEVATION_RANGE (la visée ne s'accumule pas au-delà)."""
        before = self._vertical_range(rect)
        self.tilt += step
        after = self._vertical_range(rect)
        self.tilt -= step - (after[0] - before[0])

    def _tilt_step(self, rect):
        low, high = self._vertical_range(rect)
        return (high - low) * 0.1

    # Dessin ------------------------------------------------------------------------
    def paintEvent(self, _event):
        painter = QPainter(self)
        self.render_to(painter, self.width(), self.height())
        painter.end()
        self.repainted.emit()

    def render_to(self, painter, width, height):
        palette = self.palette()
        base = palette.color(QPalette.ColorRole.Base)
        text = palette.color(QPalette.ColorRole.Text)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(QRectF(0, 0, width, height), base)
        painter.setFont(self.font())
        fm = QFontMetricsF(self.font())
        lh = fm.height()
        if not self._view:
            painter.setPen(text)
            painter.drawText(QRectF(0, 0, width, height), Qt.AlignmentFlag.AlignCenter,
                             tr("Panorama disponible après un calcul (relief des fichiers SDF)."))
            return
        dim = _mix(text, base, 0.4)
        dark = base.lightness() < 128
        rect = self._rect(width, height)
        low, high = self._vertical_range(rect)
        y_of = lambda angle: rect.bottom() - (angle - low) / (high - low) * rect.height()
        _angles, bands, edges, horizon, _hd = self._view
        az = self.data["azimuth"]

        frame = QPainterPath()
        frame.addRoundedRect(rect, lh * 0.3, lh * 0.3)
        zenith, haze = SKY[dark]
        sky = QLinearGradient(0, rect.top(), 0, rect.bottom())
        sky.setColorAt(0, zenith)
        sky.setColorAt(0.75, haze)
        sky.setColorAt(1, haze)
        painter.fillPath(frame, sky)
        painter.save()
        painter.setClipPath(frame)

        # Colonnes de pixels -> azimut ; valeurs interpolées entre les rayons voisins
        columns = np.arange(int(rect.left()), int(rect.right()) + 2)
        col_az = (self.center + (columns - rect.center().x()) * self.fov / rect.width()) % 360
        at_columns = lambda values: np.interp(col_az, az, values, period=360)

        # Plans du plus lointain (fondu dans la brume du ciel) au plus proche (foncé)
        near = _mix(base, text, 0.62) if not dark else _mix(base, text, 0.45)
        far = _mix(near, haze, min(1.0, 1.2 * self.haze))
        draped = self._colors() is not None          # carte plaquée ou dégradé d'altitude
        if draped:
            painter.drawImage(QPointF(float(columns[0]), rect.top()),
                              self._draped_image(rect, low, high, col_az, haze, text))
        for b in range(BANDS - 1, -1, -1):
            if np.all(np.isnan(bands[b])):
                continue
            values = at_columns(np.nan_to_num(bands[b], nan=-90.0))
            f = 1 - b / (BANDS - 1)          # 0 = plan le plus lointain
            color = _mix(far, near, f ** 1.4)
            ys = np.where(np.isnan(values), rect.bottom() + 2, rect.bottom() - (values - low) / (high - low) * rect.height())
            polygon = QPolygonF([QPointF(float(x), float(y)) for x, y in zip(columns, ys)])
            if draped:                       # carte plaquée : une crête sur trois, discrète
                if b % 3:
                    continue
                painter.setPen(QPen(QColor(0, 0, 0, 30), 0.8))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPolyline(polygon)
                continue
            polygon.append(QPointF(float(columns[-1]), rect.bottom() + 2))
            polygon.append(QPointF(float(columns[0]), rect.bottom() + 2))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawPolygon(polygon)
            painter.setPen(QPen(_mix(color, text, 0.25), 0.8))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolyline(polygon.mid(0, len(columns)))

        # Ligne d'horizon visible
        ys = rect.bottom() - (at_columns(horizon) - low) / (high - low) * rect.height()
        painter.setPen(QPen(_mix(text, base, 0.1), 1.2))
        painter.drawPolyline(QPolygonF([QPointF(float(x), float(y)) for x, y in zip(columns, ys)]))

        # Horizontale de l'observateur (angle de site nul)
        painter.setPen(QPen(QColor(104, 120, 142), 1, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(rect.left(), y_of(0)), QPointF(rect.right(), y_of(0)))

        # Repères des autres sites (récepteur…) placés en premier, dessinés par-dessus les localités
        markers, placed = self._layout_markers(rect, y_of, horizon, lh)
        if self.show_places and self.places:
            self._draw_places(painter, rect, y_of, horizon, base, lh, placed)
        self._draw_markers(painter, markers, base, lh)

        # Curseur
        if self.cursor_az is not None:
            x = self._x(rect, self.cursor_az)
            highlight = palette.color(QPalette.ColorRole.Highlight)
            painter.setPen(QPen(highlight, 1))
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            painter.setBrush(highlight)
            painter.drawEllipse(QPointF(x, y_of(float(np.interp(self.cursor_az, az, horizon, period=360)))), 3.5, 3.5)
        painter.restore()
        painter.setPen(QPen(_mix(text, base, 0.75), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(frame)

        # Graduations : azimuts (points cardinaux) et angles de site
        step = next(s for s in (1, 2, 5, 10, 15, 30, 45) if s * rect.width() / self.fov >= fm.horizontalAdvance("000°") * 1.6)
        start = math.floor((self.center - self.fov / 2) / step) * step
        bold = QFont(self.font())
        bold.setBold(True)
        value = start
        while value <= self.center + self.fov / 2 + step:
            a = value % 360
            x = self._x(rect, a)
            if rect.left() - 1 <= x <= rect.right() + 1:
                painter.setPen(QPen(_mix(text, base, 0.7), 1))
                painter.drawLine(QPointF(x, rect.bottom()), QPointF(x, rect.bottom() + lh * 0.3))
                cardinal = CARDINALS.get(int(round(a)) % 360) if abs(a - round(a)) < 1e-6 else None
                painter.setFont(bold if cardinal else self.font())
                painter.setPen(text if cardinal else dim)
                painter.drawText(QRectF(x - 30, rect.bottom() + lh * 0.3, 60, lh), Qt.AlignmentFlag.AlignHCenter,
                                 cardinal or f"{a:g}°")
            value += step
        painter.setFont(self.font())
        painter.setPen(dim)
        span = high - low
        tick = next(s for s in (0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10) if s * rect.height() / span >= lh * 1.6)
        a = math.ceil(low / tick) * tick
        while a <= high:
            y = y_of(a)
            painter.drawText(QRectF(0, y - lh / 2, rect.left() - lh * 0.4, lh),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{a:+.{2 if tick < 0.1 else 1}f}°")
            a += tick

        # En-tête et pied
        h_factor, h_unit, _df, _du = units(self._unit_profile())
        painter.setFont(bold)
        painter.setPen(text)
        title = tr("{name} — vue depuis {h:.4g} {u} au-dessus du sol", name=self.site["name"],
                   h=self.height_m / h_factor, u=h_unit)
        painter.drawText(QRectF(rect.left(), 0, rect.width(), lh * 1.6), Qt.AlignmentFlag.AlignVCenter, title)
        painter.setFont(self.font())
        painter.setPen(dim)
        exaggeration = (rect.height() / span) / (rect.width() / self.fov)
        info = tr("champ {fov:.0f}° · rayon {r:.0f} km · k = {k:.3g} · relief × {e:.3g}",
                  fov=self.fov, r=self.data["distance"][-1] / 1000, k=self.k, e=exaggeration)
        if abs(self.tilt) > 1e-9:
            info = tr("visée {t:+.2f}°", t=self.tilt) + " · " + info
        painter.drawText(QRectF(rect.left(), 0, rect.width(), lh * 1.6),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, info)
        if self.texture is None or self.texture.shape[:2] != self._view[0].shape:
            self._draw_altitude_legend(painter, fm, lh, rect.left() + QFontMetricsF(bold).horizontalAdvance(title)
                                       + lh * 1.5, rect.right() - fm.horizontalAdvance(info) - lh * 1.5, h_factor, h_unit)
        if self.data.get("missing"):
            painter.setPen(QColor(176, 64, 52))
            painter.drawText(QRectF(rect.left(), rect.bottom() + lh * 1.4, rect.width(), lh * 1.4),
                             Qt.AlignmentFlag.AlignCenter, tr("Aucune tuile SDF trouvée autour du site : relief plat."))

    def _draw_altitude_legend(self, painter, fm, lh, left, right, h_factor, h_unit):
        """Barre du dégradé (altitude ou distance) dans l'en-tête, entre le titre et les informations."""
        _colors, extent = self._altitude_colors()
        if extent is None:
            return
        if self.gradient_mode == "distance":
            _hf, _hu, d_factor, d_unit = units(self._unit_profile())
            low_text = f"{extent[0] / d_factor:.1f}"
            high_text = f"{extent[1] / d_factor:.0f} {d_unit}"
        else:
            low_text = f"{extent[0] / h_factor:.0f}"
            high_text = f"{extent[1] / h_factor:.0f} {h_unit}"
        if self.contours:
            ground = self.data["ground"]
            step = contour_step(float(ground.min()), float(ground.max()))
            high_text += " · " + tr("courbes tous les {s:.0f} {u}", s=step / h_factor, u=h_unit)
        bar_w = lh * 7
        needed = fm.horizontalAdvance(low_text) + fm.horizontalAdvance(high_text) + bar_w + lh
        if right - left < needed:
            return
        x = left + (right - left - needed) / 2
        painter.setPen(self.palette().color(QPalette.ColorRole.Text))
        painter.drawText(QRectF(x, 0, fm.horizontalAdvance(low_text), lh * 1.6), Qt.AlignmentFlag.AlignVCenter, low_text)
        x += fm.horizontalAdvance(low_text) + lh * 0.5
        bar = QRectF(x, lh * 0.4, bar_w, lh * 0.8)
        painter.setPen(QPen(_mix(self.palette().color(QPalette.ColorRole.Text),
                                 self.palette().color(QPalette.ColorRole.Base), 0.6), 1))
        painter.setBrush(gradient(self._palette(), bar.left(), 0, bar.right(), 0))
        painter.drawRoundedRect(bar, lh * 0.2, lh * 0.2)
        painter.setPen(self.palette().color(QPalette.ColorRole.Text))
        painter.drawText(QRectF(bar.right() + lh * 0.5, 0, fm.horizontalAdvance(high_text) + 2, lh * 1.6),
                         Qt.AlignmentFlag.AlignVCenter, high_text)

    # Étiquettes (localités et repères) ----------------------------------------------
    SIZE_NEAR, SIZE_FAR = 1.0, 0.55   # taille du nom (en hauteurs de ligne), proche / lointain
    FADE = 0.75                       # transparence du nom de localité le plus lointain
    MARKER_FADE = 0.75                # transparence du repère le plus lointain (comme les localités)

    def _depth(self, distance):
        """Profondeur 0 (≤ 1 km) à 1 (bord du rayon), en échelle logarithmique."""
        d_near, d_far = 1000.0, max(2000.0, float(self.data["distance"][-1]))
        return min(1.0, max(0.0, math.log(max(distance, d_near) / d_near) / math.log(d_far / d_near)))

    def _sign_font(self, lh, t):
        font = QFont(self.font())
        font.setBold(True)
        font.setPixelSize(max(8, round(lh * (self.SIZE_NEAR + (self.SIZE_FAR - self.SIZE_NEAR) * t))))
        return font, QFontMetricsF(font)

    def _place_sign(self, rect, y_of, horizon, lh, placed, azimuth, distance, angle, width, sign_h):
        """Étiquette suspendue au-dessus de l'horizon à l'azimut donné, hauteur selon la distance
        (perspective d'un mât de hauteur fixe : les proches en haut, les lointains au ras de
        l'horizon), décalée au-dessus ou en dessous en cas de chevauchement avec `placed`.
        (cadre, pied du pointillé, masqué), ou None si aucune place."""
        x = self._x(rect, azimuth)
        t = self._depth(distance)
        ground_y = y_of(angle)
        sky_y = min(y_of(float(np.interp(azimuth, self.data["azimuth"], horizon, period=360))), ground_y)
        # Étiquette recalée dans le cadre ; le pointillé reste à l'azimut exact
        left = min(max(x - width / 2, rect.left() + 3), rect.right() - 3 - width)
        target = sky_y - lh * (0.4 + 2.8 * (1 - t)) - sign_h
        lowest = sky_y - lh * 0.3 - sign_h
        step = (sign_h + lh * 0.2) / 2
        for offset in (0, -1, 1, -2, 2, -3, 3, -4, -5, -6):
            top = target + offset * step
            if top > lowest:
                continue
            box = QRectF(left, top, width, sign_h)
            if box.top() < rect.top() + 2:
                continue
            if not any(box.adjusted(-lh * 0.3, -2, lh * 0.3, 2).intersects(r) for r in placed):
                break
        else:
            return None
        crest = self._horizon_before(azimuth, distance)
        hidden = angle < crest
        # Masqué : le pointillé s'arrête sur la crête qui le cache, sans passer devant le relief
        foot_y = y_of(crest) if hidden else ground_y
        return box, min(foot_y, rect.bottom() - 1), hidden

    def _draw_mast(self, painter, x, box, foot_y, color, hidden, t, lh):
        """Pointillé de l'étiquette au sol (tirets longs et épais au premier plan, points fins et
        serrés au loin) et pied : plein si visible, creux si masqué."""
        weight = 1 - t
        foot = QPointF(x, foot_y)
        pen = QPen(color, 0.6 + 0.8 * weight)
        pen.setDashPattern([1 + 3 * weight, 2 + 0.5 * weight])
        painter.setPen(pen)
        painter.drawLine(QPointF(x, box.bottom() + 1), foot)
        painter.setPen(QPen(color, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush if hidden else color)
        radius = lh * (0.09 + 0.11 * weight)
        painter.drawEllipse(foot, radius, radius)

    def _layout_markers(self, rect, y_of, horizon, lh):
        """Étiquettes des repères (nom et distance dans une pastille), mêmes règles que les
        localités, placées avant elles : ([(distance, t, x, cadre, police, texte, pied, masqué)],
        cadres occupés)."""
        _hf, _hu, d_factor, d_unit = units(self._unit_profile())
        markers, placed = [], []
        for name, azimuth, angle, distance in sorted(self.marker_angles(), key=lambda m: m[3]):
            x = self._x(rect, azimuth)
            if not rect.left() <= x <= rect.right():
                continue
            t = self._depth(distance)
            font, fm = self._sign_font(lh, t)
            label = f"{name} · {distance / d_factor:.1f} {d_unit}"
            sign_h = fm.height() * 1.25
            width = fm.horizontalAdvance(label) + sign_h * 0.8
            layout = self._place_sign(rect, y_of, horizon, lh, placed, azimuth, distance, angle, width, sign_h)
            if layout is None:
                continue
            box, foot_y, hidden = layout
            placed.append(box)
            markers.append((distance, t, x, box, font, label, foot_y, hidden))
        return markers, placed

    def _draw_markers(self, painter, markers, base, lh):
        """Repères en pastille verte (visible) ou rouge (masqué), les plus proches par-dessus."""
        for distance, t, x, box, font, label, foot_y, hidden in sorted(markers, key=lambda m: -m[0]):
            color = QColor(176, 64, 52) if hidden else QColor(52, 120, 80)
            painter.setOpacity(1.0 - self.MARKER_FADE * t)
            self._draw_mast(painter, x, box, foot_y, color, hidden, t, lh)
            painter.setPen(QPen(color, 1))
            painter.setBrush(base)
            painter.drawRoundedRect(box, box.height() / 2, box.height() / 2)
            painter.setFont(font)
            painter.setPen(color)
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, label)
        painter.setOpacity(1.0)
        painter.setFont(self.font())

    def _draw_places(self, painter, rect, y_of, horizon, base, lh, placed):
        """Noms en capitales suspendus au-dessus de l'horizon, reliés au sol par un pointillé.
        Profondeur rendue par la distance : nom, pied et liseré de plus en plus petits et
        transparents en s'éloignant, les plus proches dessinés par-dessus les plus lointains ;
        mât (hauteur du nom au-dessus de l'horizon) et tirets du pointillé raccourcis au loin.
        Priorité de placement : repères (`placed`, déjà occupés), puis villes, bourgs et villages,
        les plus proches d'abord."""
        eye = self.data["site_ground"] + self.height_m
        text = self.palette().color(QPalette.ColorRole.Text)
        ink = _mix(text, base, 0.35)             # pointillés et pieds
        candidates = sorted((p for p in self.places if population(p) >= self.min_population),
                            key=lambda p: (p["rank"], -population(p), p["distance"]))
        placed = list(placed)
        signs = []
        for place in candidates:
            if len(placed) >= MAX_SIGNS + len(self.markers):
                break
            x = self._x(rect, place["azimuth"])
            if not rect.left() + 4 <= x <= rect.right() - 4:
                continue
            distance = place["distance"]
            t = self._depth(distance)
            font, fm = self._sign_font(lh, t)
            drop = distance ** 2 / (2 * self.k * EARTH_RADIUS_M)
            angle = math.degrees(math.atan2(place.get("ground", 0.0) - eye - drop, distance))
            name = place["name"].upper()
            pad = fm.height() * 0.25
            layout = self._place_sign(rect, y_of, horizon, lh, placed, place["azimuth"], distance, angle,
                                      fm.horizontalAdvance(name) + 2 * pad, fm.height())
            if layout is None:
                continue
            box, foot_y, hidden = layout
            placed.append(box)
            signs.append((distance, t, x, box, font, fm, pad, name, hidden, foot_y))
        for distance, t, x, box, font, fm, pad, name, hidden, foot_y in sorted(signs, key=lambda s: -s[0]):
            painter.setOpacity(1.0 - self.FADE * t)
            self._draw_mast(painter, x, box, foot_y, ink, hidden, t, lh)
            # Nom en capitales, liseré de la couleur du fond pour rester lisible sur le relief
            path = QPainterPath()
            path.addText(box.left() + pad, box.top() + fm.ascent(), font, name)
            painter.setPen(QPen(base, max(1.5, fm.height() * 0.2), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(text)
            painter.drawPath(path)
        painter.setOpacity(1.0)
        painter.setFont(self.font())

    def _unit_profile(self):
        return {"metric": self.metric}

    def _horizon_before(self, azimuth, distance):
        """Plus grand angle de site du relief entre le site et `distance` dans la direction donnée."""
        angles = self._view[0]
        i = int(round(azimuth / AZ_STEP)) % angles.shape[0]
        sel = self.data["distance"] < distance - 1
        return float(angles[i, sel].max()) if sel.any() else -90.0

    # Interaction -------------------------------------------------------------------
    def wheelEvent(self, event):
        if not self._view:
            return
        rect = self._rect(self.width(), self.height())
        # Maj + molette : certains systèmes transmettent alors un défilement horizontal.
        delta = event.angleDelta().y() or event.angleDelta().x()
        if not delta:
            return
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self._tilt_by(rect, self._tilt_step(rect) * (1 if delta > 0 else -1))
            self.update()
            return
        anchor = self._azimuth(rect, event.position().x())
        factor = 0.8 if delta > 0 else 1.25
        self.fov = min(360.0, max(MIN_FOV, self.fov * factor))
        # Garde l'azimut sous le pointeur immobile
        self.center = (anchor - (event.position().x() - rect.center().x()) * self.fov / rect.width()) % 360
        self.update()

    def keyPressEvent(self, event):
        if not self._view:
            return super().keyPressEvent(event)
        key, fine = event.key(), bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            step = self.fov / (60 if fine else 12)            # Maj : pas fin
            self.center = (self.center + (step if key == Qt.Key.Key_Right else -step)) % 360
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal, Qt.Key.Key_Minus):
            self.fov = min(360.0, max(MIN_FOV, self.fov * (0.8 if key != Qt.Key.Key_Minus else 1.25)))
        elif key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            rect = self._rect(self.width(), self.height())
            step = self._tilt_step(rect) * (0.2 if fine else 1)
            self._tilt_by(rect, step if key == Qt.Key.Key_PageUp else -step)
        elif key == Qt.Key.Key_Home:
            self.fov, self.center, self.tilt = 360.0, 180.0, 0.0
        else:
            return super().keyPressEvent(event)
        self.update()

    def mousePressEvent(self, event):
        self.setFocus()
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = (event.position().x(), self.center)

    def mouseReleaseEvent(self, _event):
        if self._drag:
            self._drag = None
            self.update()                 # placage recalculé en pleine résolution

    def mouseDoubleClickEvent(self, _event):
        self.reset_view()

    def mouseMoveEvent(self, event):
        if not self._view:
            return
        rect = self._rect(self.width(), self.height())
        if self._drag:
            x0, center0 = self._drag
            self.center = (center0 - (event.position().x() - x0) * self.fov / rect.width()) % 360
        if not rect.left() <= event.position().x() <= rect.right():
            self.leaveEvent(None)
            return
        self.cursor_az = self._azimuth(rect, event.position().x())
        i = int(round(self.cursor_az / AZ_STEP)) % len(self.data["azimuth"])
        _a, _b, _e, horizon, horizon_distance = self._view
        _hf, _hu, d_factor, d_unit = units(self._unit_profile())
        self.hovered.emit(tr("azimut {az:.1f}° — horizon à {el:+.2f}° — relief masquant à {d:.1f} {du}",
                             az=self.cursor_az, el=horizon[i], d=horizon_distance[i] / d_factor, du=d_unit))
        self.update()

    def leaveEvent(self, _event):
        if self.cursor_az is None:
            return
        self.cursor_az = None
        self.hovered.emit("")
        self.update()

    def image(self, width=2400, height=round(700 * HEIGHT_FRACTION)):
        """Panorama rendu en image (export PNG)."""
        cursor, self.cursor_az = self.cursor_az, None
        image = QImage(width, height, QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        font = QFont(self.font())
        font.setPixelSize(15)
        saved = self.font()
        self.setFont(font)
        self.render_to(painter, width, height)
        painter.end()
        self.setFont(saved)
        self.cursor_az = cursor
        return image


class PanoramaMap(QWidget):
    """Carte rectangulaire vue de dessus autour du site, tournée comme le panorama : l'azimut au
    centre du panorama pointe vers le haut ; même largeur de cadre que le panorama. Carte physique
    (relief SDF en teintes hypsométriques ombrées) sur tout le cadre, courbes de niveau, zones
    masquées depuis le site assombries, ligne d'horizon visible (option), champ de vision, graduation d'azimut au bord du cadre,
    cercles de distance, localités et repères. Glisser fait tourner le panorama, un clic l'oriente
    vers le point visé, le survol place le curseur du panorama, la molette zoome (double-clic :
    tout le rayon / largeur remplie)."""

    MAX_ZOOM = 8.0
    IMAGE_LIMIT = 2048             # côté maximal de l'image du relief (agrandie au-delà)

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view = view
        self.zoom = 1.0            # 1 = rayon calé sur le plus grand côté du cadre
        self.layers = None         # relief de `map_layers`, ou None
        self.places = []           # localités jusqu'aux coins de la carte (au-delà du rayon)
        self.show_horizon = True   # ligne d'horizon visible
        self._image = None         # (clé, image orientée au nord)
        self._relief = None        # (clé, image du relief ombré du carré)
        self._drag = None          # (angle du pointeur, azimut central) au début du glisser
        self._moved = False
        view.repainted.connect(self.update)
        self.setMouseTracking(True)
        self.setMinimumHeight(160)

    def set_places(self, places):
        self.places = places or []
        self.update()

    def set_show_horizon(self, show):
        self.show_horizon = bool(show)
        self.update()

    def set_layers(self, layers):
        self.layers = layers
        self._relief = None
        self.update()

    def _frame(self):
        """Cadre de la carte, aligné sur celui du panorama."""
        lh = QFontMetricsF(self.font()).height()
        pano = self.view._rect(self.view.width(), self.view.height())
        left, right = pano.left(), pano.right()
        if right - left < 50 or right > self.width():
            left, right = lh * 0.5, self.width() - lh * 0.5
        return QRectF(left, lh * 0.3, right - left, self.height() - lh * 0.6)

    def _layout(self, frame):
        """(position du site, rayon à zoom 1 : la carte remplit la largeur du cadre, zoom minimal :
        tout le rayon visible devant le site). Site en bas du cadre pour voir loin devant, au
        centre quand le panorama couvre 360°."""
        lh = QFontMetricsF(self.font()).height()
        site = QPointF(frame.center().x(), frame.top() + frame.height() * (0.5 if self.view.fov >= 360 else 0.82))
        fill = max(frame.width() / 2, site.y() - frame.top())
        fit = max(10.0, min(site.y() - frame.top(), frame.width() / 2) - lh * 0.4)
        return site, fill, fit / fill

    def _geometry(self):
        """(cadre, position du site, rayon du panorama en pixels)."""
        frame = self._frame()
        site, fill, min_zoom = self._layout(frame)
        return frame, site, max(10.0, fill * min(self.MAX_ZOOM, max(min_zoom, self.zoom)))

    def _polar(self, center, radius, azimuth, distance):
        """Point de la carte (orientée selon le panorama) à l'azimut et la distance donnés."""
        rel = math.radians(azimuth - self.view.center)
        r = distance / self.view.data["distance"][-1] * radius
        return QPointF(center.x() + r * math.sin(rel), center.y() - r * math.cos(rel))

    def _relief_image(self, base, text):
        """Carte physique du carré des calques, orientée au nord : teintes hypsométriques ombrées
        (lumière du nord-ouest), mer en bleu, courbes de niveau si elles sont affichées."""
        v, layers = self.view, self.layers
        key = (id(layers), v.contours, base.rgb())
        if self._relief and self._relief[0] == key:
            return self._relief[1]
        ground = layers["ground"]
        # Grille affinée (interpolation bilinéaire) : courbes de niveau fines une fois la carte agrandie
        size = min(self.IMAGE_LIMIT, ground.shape[0] * 3)
        x = (np.arange(size) + 0.5) * ground.shape[0] / size - 0.5
        i0 = np.clip(np.floor(x).astype(int), 0, ground.shape[0] - 2)
        f = np.clip(x - i0, 0, 1)
        ground = ground[i0] * (1 - f)[:, None] + ground[i0 + 1] * f[:, None]
        ground = ground[:, i0] * (1 - f) + ground[:, i0 + 1] * f
        light = np.nan_to_num(hillshade(ground, 2 * layers["half"] / size), nan=1.0)   # hors SDF : plat
        lines = np.ones(ground.shape, dtype=np.float32)
        if v.contours:                     # même équidistance que le panorama
            pano = v.data["ground"]
            lines = contour_shading(ground, contour_step(float(pano.min()), float(pano.max())))
        land = np.nan_to_num(ground, nan=0.0)
        sea = land <= 0
        low = float(land[~sea].min()) if (~sea).any() else 0.0
        high = max(float(land.max()), low + 1.0)
        rgb = colorize(np.clip((land - low) / (high - low), 0, 1), "hypso")
        rgb[sea] = (150, 190, 222)
        rgb = rgb * np.where(sea, 1.0, np.clip(light, 0.55, 1.3))[..., None] * lines[..., None]
        sky = np.array([base.red(), base.green(), base.blue()], dtype=np.float64)
        out = np.zeros((size, size, 4), dtype=np.uint8)
        out[..., :3] = np.clip(rgb * 0.85 + sky * 0.15, 0, 255)
        out[..., 3] = np.where(np.isnan(ground), 0, 255)
        image = QImage(out.data, size, size, size * 4, QImage.Format.Format_RGBA8888).copy()
        self._relief = (key, image)
        return image

    def _terrain_image(self, radius, base, text, overlay=False):
        """Relief vu de dessus, orienté au nord, calculé pixel par pixel à partir des rayons du
        panorama ; mis en cache tant que les couleurs et la hauteur d'observation ne changent pas.
        `overlay` : seulement le voile des zones masquées, à poser sur le relief des calques."""
        v = self.view
        size = max(2, min(self.IMAGE_LIMIT, int(round(2 * radius))))
        colors = v._colors()
        key = (size, id(v.data), id(colors), id(v._view), v.contours, base.rgb(), text.rgb(), overlay)
        if self._image and self._image[0] == key:
            return self._image[1]
        d = v.data["distance"]
        ground = v.data["ground"]
        n_az = ground.shape[0]
        c = (np.arange(size) + 0.5 - size / 2) / (size / 2)
        dx, dy = np.meshgrid(c, c)
        dist = np.hypot(dx, dy) * d[-1]
        inside = (dist >= d[0]) & (dist <= d[-1])
        az = np.degrees(np.arctan2(dx, -dy)) % 360
        i = np.rint(az / AZ_STEP).astype(int) % n_az
        k = np.clip(np.searchsorted(d, dist), 0, len(d) - 1)
        # Zones que le relief masque depuis le site : légèrement assombries
        angles = v._view[0]
        before = np.concatenate([np.full((n_az, 1), -90.0), np.maximum.accumulate(angles, axis=1)[:, :-1]], axis=1)
        shade = np.where(angles[i, k] < before[i, k], 0.85, 1.0)
        if v.contours and not overlay:
            altitude = np.where(inside, ground[i, k], np.nan)
            shade = shade * contour_shading(altitude, contour_step(float(ground.min()), float(ground.max())))
        out = np.zeros((size, size, 4), dtype=np.uint8)
        if overlay:                        # voile noir d'autant plus opaque que l'ombre est forte
            out[..., 3] = np.where(inside, np.clip((1 - shade) * 255, 0, 255), 0)
        else:
            sky = np.array([base.red(), base.green(), base.blue()], dtype=np.float64)
            if colors is not None:
                rgb = colors[i, k, :3].astype(np.float64)
                rgb = np.where(colors[i, k, 3:4] >= 128, rgb, sky * 0.8)     # carte manquante
            else:                          # gris ombré selon la pente
                grey = _mix(base, text, 0.3)
                rgb = np.array([grey.red(), grey.green(), grey.blue()], dtype=np.float64) * slope_light(v.data)[i, k, None]
            rgb = (rgb * 0.75 + sky * 0.25) * shade[..., None]               # carte éclaircie
            out[..., :3] = np.clip(rgb, 0, 255)
            out[..., 3] = np.where(inside, 255, 0)
        image = QImage(out.data, size, size, size * 4, QImage.Format.Format_RGBA8888).copy()
        self._image = (key, image)
        return image

    @staticmethod
    def _border_point(frame, center, rel, inset):
        """Point où la direction `rel` (degrés, 0 = haut) issue de `center` atteint le cadre réduit
        de `inset`."""
        dx, dy = math.sin(math.radians(rel)), -math.cos(math.radians(rel))
        box = frame.adjusted(inset, inset, -inset, -inset)
        t = min((box.right() - center.x()) / dx if dx > 1e-9 else (box.left() - center.x()) / dx if dx < -1e-9 else math.inf,
                (box.bottom() - center.y()) / dy if dy > 1e-9 else (box.top() - center.y()) / dy if dy < -1e-9 else math.inf)
        return QPointF(center.x() + t * dx, center.y() + t * dy)

    def paintEvent(self, _event):
        v = self.view
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        base = palette.color(QPalette.ColorRole.Base)
        text = palette.color(QPalette.ColorRole.Text)
        painter.fillRect(self.rect(), palette.color(QPalette.ColorRole.Window))
        if not v._view:
            return
        frame, center, radius = self._geometry()
        fm = QFontMetricsF(self.font())
        lh = fm.height()
        d_max = v.data["distance"][-1]
        outline = QPainterPath()
        outline.addRoundedRect(frame, lh * 0.3, lh * 0.3)
        disc = QPainterPath()
        disc.addEllipse(center, radius, radius)
        painter.fillPath(outline, _mix(base, text, 0.04))       # au-delà du rayon du panorama
        painter.save()
        painter.setClipPath(outline)

        # Relief tourné : l'azimut central du panorama vers le haut ; carte physique sur tout le
        # cadre une fois chargée, voile des zones masquées par-dessus
        painter.save()
        painter.translate(center)
        painter.rotate(-v.center)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if self.layers:                    # les noms des localités restent droits (dessinés plus bas)
            side = self.layers["half"] / d_max * radius
            painter.drawImage(QRectF(-side, -side, 2 * side, 2 * side), self._relief_image(base, text))
        painter.setClipPath(disc.translated(-center.x(), -center.y()), Qt.ClipOperation.IntersectClip)
        painter.drawImage(QRectF(-radius, -radius, 2 * radius, 2 * radius),
                          self._terrain_image(radius, base, text, overlay=bool(self.layers)))
        painter.restore()

        # Champ de vision du panorama : hors champ assombri
        if v.fov < 360:
            reach = math.hypot(frame.width(), frame.height())
            wedge = QPainterPath(center)
            wedge.arcTo(QRectF(center.x() - reach, center.y() - reach, 2 * reach, 2 * reach), 90 + v.fov / 2, -v.fov)
            wedge.closeSubpath()
            painter.fillPath(outline.subtracted(wedge), QColor(0, 0, 0, 45))
            painter.setPen(QPen(palette.color(QPalette.ColorRole.Highlight), 1.2, Qt.PenStyle.DashLine))
            for side in (-1, 1):
                painter.drawLine(center, self._border_point(frame, center, side * v.fov / 2, 0))

        # Cercles de distance (pas « rond », trois à cinq dans le cadre) et rayon du panorama
        _hf, _hu, d_factor, d_unit = units(v._unit_profile())
        visible = math.hypot(frame.width(), frame.height()) / 2 / radius * d_max / d_factor
        ring = next((r for r in (0.5, 1, 2, 5, 10, 20, 25, 50, 100, 200) if visible / r <= 5), 500)
        small = QFont(self.font())
        small.setPixelSize(max(8, round(lh * 0.72)))
        small_fm = QFontMetricsF(small)
        painter.setFont(small)
        value = ring
        while value * d_factor < d_max:
            r = value * d_factor / d_max * radius
            painter.setPen(QPen(_mix(text, base, 0.55), 0.8, Qt.PenStyle.DotLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(center, r, r)
            label = f"{value:g} {d_unit}"
            for rel in (135, 90, 45, 180):
                point = QPointF(center.x() + r * math.sin(math.radians(rel)), center.y() - r * math.cos(math.radians(rel)))
                if frame.adjusted(lh, lh, -lh * 3, -lh).contains(point):
                    path = QPainterPath()
                    path.addText(point.x() + 3, point.y() + small_fm.ascent() / 2, small, label)
                    painter.setPen(QPen(base, 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                        Qt.PenJoinStyle.RoundJoin))
                    painter.drawPath(path)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(_mix(text, base, 0.2))
                    painter.drawPath(path)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    break
            value += ring
        painter.setPen(QPen(_mix(text, base, 0.4), 1))
        painter.drawEllipse(center, radius, radius)

        # Ligne d'horizon visible : distance du relief qui ferme la vue, azimut par azimut
        if self.show_horizon:
            points = [self._polar(center, radius, a, dd) for a, dd in zip(v.data["azimuth"], v._view[4])]
            painter.setPen(QPen(_mix(text, base, 0.1), 1.1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolygon(QPolygonF(points))

        # Curseur du panorama
        if v.cursor_az is not None:
            painter.setPen(QPen(palette.color(QPalette.ColorRole.Highlight), 1.4))
            painter.drawLine(center, self._border_point(frame, center, v.cursor_az - v.center, 0))

        # Localités et repères ; noms des repères prioritaires, ceux des localités dans la place libre
        bold = QFont(small)
        bold.setBold(True)
        bold_fm = QFontMetricsF(bold)
        markers = []
        for name, azimuth, angle, distance in v.marker_angles():
            point = self._polar(center, radius, azimuth, min(distance, d_max))
            box = QRectF(point.x() + 5, point.y() - bold_fm.height() / 2, bold_fm.horizontalAdvance(name) + 2,
                         bold_fm.height())
            markers.append((name, point, box, angle < v._horizon_before(azimuth, distance)))
        placed = [box for _n, _p, box, _h in markers]
        if v.show_places:
            candidates = sorted((p for p in self.places or v.places if population(p) >= v.min_population),
                                key=lambda p: (p["rank"], -population(p), p["distance"]))
            for place in candidates[:MAP_MAX_PLACES]:
                point = self._polar(center, radius, place["azimuth"], place["distance"])
                if not frame.contains(point):
                    continue
                name = place["name"]
                box = QRectF(point.x() + 4, point.y() - small_fm.height() / 2, small_fm.horizontalAdvance(name) + 2,
                             small_fm.height())
                painter.setPen(QPen(base, 1))
                painter.setBrush(text)
                painter.drawEllipse(point, 2.2, 2.2)
                if any(box.intersects(r) for r in placed) or not frame.contains(box):
                    continue                   # point seul, sans nom
                placed.append(box)
                path = QPainterPath()
                path.addText(box.left() + 1, box.top() + small_fm.ascent(), small, name)
                painter.setPen(QPen(base, 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(path)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(text)
                painter.drawPath(path)
        for name, point, box, hidden in markers:
            color = QColor(176, 64, 52) if hidden else QColor(52, 120, 80)
            painter.setPen(QPen(color, 1.2, Qt.PenStyle.DashLine))
            painter.drawLine(center, point)
            painter.setPen(QPen(base, 1))
            painter.setBrush(color)
            painter.drawEllipse(point, 3.5, 3.5)
            path = QPainterPath()
            path.addText(box.left() + 1, box.top() + bold_fm.ascent(), bold, name)
            painter.setPen(QPen(base, 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawPath(path)

        # Site
        painter.setPen(QPen(base, 1.5))
        painter.setBrush(palette.color(QPalette.ColorRole.Highlight))
        painter.drawEllipse(center, 4, 4)

        self._draw_azimuth_scale(painter, frame, center, small, bold, base, text,
                                 palette.color(QPalette.ColorRole.Highlight))
        painter.restore()
        painter.setFont(self.font())
        painter.setPen(QPen(_mix(text, base, 0.75), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(outline)

    def _draw_azimuth_scale(self, painter, frame, center, small, bold, base, text, highlight):
        """Graduation d'azimut au bord du cadre, tournée avec la carte : traits tous les 5°, 10° ou
        30° selon la place, valeurs posées sans chevauchement (points cardinaux d'abord) ; champ de
        vision en couleur, repère de l'azimut central."""
        v = self.view
        lh = QFontMetricsF(self.font()).height()
        band = lh * 0.35

        def edge(azimuth, inset):
            return self._border_point(frame, center, (azimuth - v.center + 180) % 360 - 180, inset)

        def inward(point):
            if abs(point.y() - frame.top()) < 0.5:
                return QPointF(0, 1)
            if abs(point.y() - frame.bottom()) < 0.5:
                return QPointF(0, -1)
            return QPointF(1, 0) if abs(point.x() - frame.left()) < 0.5 else QPointF(-1, 0)

        def in_view(azimuth):
            return v.fov >= 360 or abs((azimuth - v.center + 180) % 360 - 180) <= v.fov / 2 + 1e-6

        # Bande du champ de vision le long du bord
        if v.fov < 360:
            steps = max(2, int(v.fov))
            points = [edge(v.center - v.fov / 2 + v.fov * t / steps, band / 2) for t in range(steps + 1)]
            painter.setPen(QPen(QColor(highlight.red(), highlight.green(), highlight.blue(), 150), band,
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPolyline(QPolygonF(points))

        # Traits
        for a in range(0, 360, 5):
            level = 3 if a % 30 == 0 else 2 if a % 10 == 0 else 1
            spacing = (30, 10, 5)[3 - level]
            p, q = edge(a - spacing / 2, 0), edge(a + spacing / 2, 0)
            if math.hypot(p.x() - q.x(), p.y() - q.y()) < 4:
                continue
            point = edge(a, 0)
            n = inward(point)
            length = lh * (0.25, 0.4, 0.6)[level - 1]
            painter.setPen(QPen(text if in_view(a) else _mix(text, base, 0.5), 1.4 if level == 3 else 1))
            painter.drawLine(point, QPointF(point.x() + n.x() * length, point.y() + n.y() * length))

        # Valeurs : cardinaux, puis 45°, 30°, 10°, sans chevauchement
        order = sorted(range(0, 360, 10), key=lambda a: (a % 90 != 0, a % 45 != 0, a % 30 != 0, a))
        placed = []
        for a in order:
            cardinal = CARDINALS.get(a)
            font = bold if cardinal else small
            fm = QFontMetricsF(font)
            label = cardinal or f"{a}°"
            w, h = fm.horizontalAdvance(label), fm.height()
            point = edge(a, lh * 0.75 + max(w, h) / 2)
            box = QRectF(point.x() - w / 2, point.y() - h / 2, w, h)
            if not frame.contains(box) or any(box.adjusted(-3, -1, 3, 1).intersects(r) for r in placed):
                continue
            placed.append(box)
            color = (text if a == 0 else _mix(text, base, 0.2)) if in_view(a) else _mix(text, base, 0.5)
            path = QPainterPath()
            path.addText(box.left(), box.top() + fm.ascent(), font, label)
            painter.setPen(QPen(base, 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawPath(path)

        # Azimut central : triangle au bord, pointe vers le site
        point = edge(v.center, 0)
        n = inward(point)
        t = QPointF(-n.y(), n.x())
        s = lh * 0.4
        tip = QPointF(point.x() + n.x() * s * 1.6, point.y() + n.y() * s * 1.6)
        painter.setPen(QPen(base, 1))
        painter.setBrush(highlight)
        painter.drawPolygon(QPolygonF([QPointF(point.x() + t.x() * s, point.y() + t.y() * s), tip,
                                       QPointF(point.x() - t.x() * s, point.y() - t.y() * s)]))

    # Interaction -------------------------------------------------------------------
    def _pointer(self, position):
        """(azimut, distance en m) sous le pointeur, ou None hors du cadre ou au-delà du rayon."""
        frame, center, radius = self._geometry()
        dx, dy = position.x() - center.x(), position.y() - center.y()
        r = math.hypot(dx, dy)
        if not self.view._view or not frame.contains(position) or r > radius:
            return None
        return (self.view.center + math.degrees(math.atan2(dx, -dy))) % 360, r / radius * self.view.data["distance"][-1]

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.angleDelta().x()
        if not delta or not self.view._view:
            return
        _site, _fill, min_zoom = self._layout(self._frame())
        zoom = min(self.MAX_ZOOM, max(min_zoom, self.zoom))
        self.zoom = min(self.MAX_ZOOM, max(min_zoom, zoom * (1.25 if delta > 0 else 0.8)))
        self.update()

    def mouseDoubleClickEvent(self, _event):
        self.zoom = 0.0 if self.zoom >= 1.0 else 1.0    # alterne carte entière / largeur remplie
        self.update()

    def mousePressEvent(self, event):
        hit = self._pointer(event.position())
        if event.button() == Qt.MouseButton.LeftButton and hit:
            self._drag = (hit[0] - self.view.center, self.view.center)
            self._moved = False

    def mouseMoveEvent(self, event):
        hit = self._pointer(event.position())
        v = self.view
        if self._drag and hit:
            # Glisser : le point saisi suit le pointeur, carte et panorama tournent ensemble
            rel0, center0 = self._drag
            rel = (hit[0] - v.center) % 360
            v.center = (center0 + rel0 - rel) % 360
            v._drag = (0.0, v.center)          # placage allégé pendant la rotation
            self._moved = True
        if hit is None:
            self.leaveEvent(None)
            return
        v.cursor_az = hit[0]
        _hf, _hu, d_factor, d_unit = units(v._unit_profile())
        v.hovered.emit(tr("azimut {az:.1f}° — distance {d:.1f} {du}", az=hit[0], d=hit[1] / d_factor, du=d_unit))
        v.update()

    def mouseReleaseEvent(self, event):
        if self._drag and not self._moved:
            hit = self._pointer(event.position())
            if hit:
                self.view.center_on(hit[0])    # clic : panorama orienté vers le point visé
        self._drag = None
        self.view._drag = None
        self.view.update()

    def leaveEvent(self, _event):
        if self.view.cursor_az is None:
            return
        self.view.cursor_az = None
        self.view.hovered.emit("")
        self.view.update()


def site_markers(site, others, sdf_dirs, hd):
    """Repères (nom, azimut, altitude de l'antenne, distance) des autres sites vus depuis `site`."""
    from .linkprofile import site_height_m
    markers = []
    for other in others:
        lat, lon = float(other["lat"]), float(other["lon"])
        distance = _distance_m(float(site["lat"]), float(site["lon"]), lat, lon)
        if distance < 1:
            continue
        ground = float(ground_elevations(np.array([lat]), np.array([lon]), sdf_dirs, hd)[0])
        markers.append((other["name"], bearing(float(site["lat"]), float(site["lon"]), lat, lon),
                        ground + site_height_m(other), distance))
    return markers
