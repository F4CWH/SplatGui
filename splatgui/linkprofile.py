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
import re
from functools import lru_cache
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QImage, QLinearGradient, QPainter, QPainterPath,
                         QPalette, QPen, QPolygonF)
from PyQt6.QtWidgets import QWidget

from . import dem, terrain
from .i18n import N_, tr

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
    drop = length ** 2 / (2 * k * EARTH_RADIUS_M)
    tilt = math.degrees(math.atan2(a_rx - a_tx - drop, length))
    tilt_rx = math.degrees(math.atan2(a_tx - a_rx - drop, length))
    return {
        "distance": distance, "ground": ground, "terrain": terrain_line, "bulge": bulge, "los": los,
        "fresnel": fresnel, "clearance": clearance, "length": length, "azimuth": azimuth, "k": k,
        "frequency": frequency, "tilt": tilt, "tx": tx, "rx": rx, "h_tx": h_tx, "h_rx": h_rx,
        "tilt_rx": tilt_rx, "a_tx": a_tx, "a_rx": a_rx,
        "azimuth_back": great_circle(lat2, lon2, lat1, lon1, 2)[3],
        "metric": bool(params.get("metric", True)), "fz": fz,
        "worst": worst, "worst_ratio": worst_ratio,
        "min_clearance": float(clearance[worst]),
        "min_ratio": float(ratio[worst_ratio]) if frequency else None,
        "obstructions": int(np.count_nonzero(obstructed)),
        "first_obstruction": float(distance[inner][obstructed][0]) if obstructed.any() else None,
    }


# --- Rapport de SPLAT! (<émetteur>-to-<récepteur>.txt) -------------------------------------

_NUM = r"([-+]?\d+(?:\.\d+)?)"
_LENGTH = {"meters": 1.0, "feet": FEET, "kilometers": 1000.0, "miles": MILE}
_POWER = {"": 1.0, "milli": 1e-3, "kilo": 1e3, "mega": 1e6}


def parse_report(text):
    """Valeurs du rapport d'analyse de liaison de SPLAT! (longueurs en m, puissances en W)."""
    def number(pattern):
        match = re.search(pattern, text)
        return float(match.group(1)) if match else None

    out = {
        "free_space": number(r"Free space path loss:\s*" + _NUM),
        "terrain_loss": number(r"terrain shielding:\s*" + _NUM),
        "field": number(r"Field strength at .+?:\s*" + _NUM),
        "power": number(r"Signal power level at .+?:\s*" + _NUM),
        "density": number(r"Signal power density at .+?:\s*" + _NUM),
        "situations": number(r"Fraction of Situations:\s*" + _NUM),
        "time": number(r"Fraction of Time:\s*" + _NUM),
    }
    match = re.search(r"^(?!Free space)(.+?) path loss:\s*" + _NUM, text, re.M)
    if match:
        out["model"], out["path_loss"] = match.group(1).strip(), float(match.group(2))
    match = re.search(r"Voltage across a 50 ohm dipole at .+?:\s*" + _NUM + r"\s*uV\s*\(\s*" + _NUM, text)
    if match:
        out["voltage"] = float(match.group(1)), float(match.group(2))
    match = re.search(r"Mode of propagation:\s*(.+)", text)
    if match:
        out["mode"] = match.group(1).strip()
    for key, label in (("erp", "ERP"), ("eirp", "EIRP")):
        match = re.search(rf"Transmitter {label}:\s*{_NUM}\s*(milli|kilo|mega)?Watts", text, re.I)
        if match:
            out[key] = float(match.group(1)) * _POWER[(match.group(2) or "").lower()]
    match = re.search(r"error number:\s*(\d+)\s*\((.+?)\)", text)
    if match and match.group(1) != "0":
        out["error"] = match.group(2)
    # Hauteurs d'antenne du récepteur exigées : obstacles, 1re zone de Fresnel, fraction de celle-ci.
    out["raise"] = {}
    for match in re.finditer(r"must be raised to at least\s*" + _NUM + r"\s*(meters|feet)\s*AGL\s*to clear\s*"
                             r"(all obstructions|the first Fresnel zone|(\d+)% of the first Fresnel zone)", text):
        key = "obstructions" if match.group(3).startswith("all") else \
            "fresnel" if match.group(4) is None else "fresnel_part"
        out["raise"][key] = float(match.group(1)) * _LENGTH[match.group(2)]
    # Obstacles (distances comptées depuis le récepteur).
    out["obstructions"] = [
        (float(m.group(1)) * _LENGTH[m.group(2)], float(m.group(3)) * _LENGTH[m.group(4)])
        for m in re.finditer(r"^\s*[-\d.]+\s*[NS],\s*[-\d.]+\s*[EW],\s*" + _NUM + r"\s*(kilometers|miles),\s*"
                             + _NUM + r"\s*(meters|feet)", text, re.M)]
    return out


def read_report(run_dir, tx_name):
    """Rapport de liaison de SPLAT! pour l'émetteur `tx_name`, ou {} s'il est absent."""
    reports = []
    for path in sorted(Path(run_dir).glob("*-to-*.txt")):
        try:
            text = path.read_text(encoding="latin-1")
        except OSError:
            continue
        match = re.search(r"Transmitter site:\s*(.+)", text)
        if match and match.group(1).strip() == str(tx_name).strip():
            return parse_report(text)
        reports.append(text)
    return parse_report(reports[0]) if len(reports) == 1 else {}


_MODES = [("Line-Of-Sight Mode", N_("visibilité directe")), ("Line of Sight", N_("visibilité directe")),
          ("Single Horizon", N_("simple horizon")), ("Double Horizon", N_("double horizon")),
          ("Diffraction Dominant", N_("diffraction")),
          ("Troposcatter Dominant", N_("diffusion troposphérique"))]


def propagation_mode(text):
    for english, french in _MODES:
        text = re.sub(re.escape(english), tr(french), text, flags=re.I)
    text = re.sub(r"(horizon)\s+(?=\w)", r"\1, ", text.strip(), flags=re.I)
    text = re.sub(r"\s*,\s*", ", ", text)
    return text[:1].upper() + text[1:]


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
        return tr("Visibilité obstruée"), QColor(176, 64, 52)
    ratio = profile["min_ratio"]
    if ratio is None:
        return tr("Visibilité directe"), QColor(52, 120, 80)
    if ratio >= profile.get("fz", 0.6):
        return (tr("Dégagée (≥ {pct:g} % de la 1re zone de Fresnel)", pct=profile.get("fz", 0.6) * 100),
                QColor(52, 120, 80))
    return tr("Visibilité directe, Fresnel partiellement obstruée"), QColor(176, 118, 30)


# --- Tracé ----------------------------------------------------------------------------------

# Palette neutre : gris, une teinte ardoise pour Fresnel ; seule la couleur d'état est vive.
FRESNEL_COLOR = QColor(104, 120, 142)
OBSTACLE_COLOR = QColor(170, 72, 60)
CURVE_COLOR = QColor(140, 140, 140)
ROW = 1.12            # hauteur d'une ligne de fiche, en hauteurs de texte
CARD_TITLE = 1.55     # hauteur du titre d'une fiche (filet compris)


def _alpha(color, alpha):
    color = QColor(color)
    color.setAlpha(alpha)
    return color


def _mix(a, b, f):
    return QColor(round(a.red() + (b.red() - a.red()) * f), round(a.green() + (b.green() - a.green()) * f),
                  round(a.blue() + (b.blue() - a.blue()) * f))


def _terrain_brush(top, bottom, dark):
    gradient = QLinearGradient(0, top, 0, bottom)
    if dark:
        gradient.setColorAt(0, QColor(98, 96, 92))
        gradient.setColorAt(1, QColor(62, 61, 58))
    else:
        gradient.setColorAt(0, QColor(200, 197, 190))
        gradient.setColorAt(1, QColor(158, 154, 147))
    return QBrush(gradient)


def _terrain_edge(dark):
    return QColor(176, 172, 165) if dark else QColor(104, 100, 94)


class ProfileView(QWidget):
    """Schéma du profil (relief, visée, Fresnel, antennes), légende et fiches de synthèse ;
    le survol affiche distance, altitude et dégagement."""

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
        self._update_minimum()
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_minimum()

    def _update_minimum(self):
        height = self._layout(self.width(), 0)["required"] if self.profile else 240
        if height != self.minimumHeight():
            self.setMinimumHeight(height)

    # Mise en page ------------------------------------------------------------------
    def _metrics(self):
        fm = QFontMetricsF(self.font())
        return fm, fm.height()

    def _layout(self, width, height):
        """Rectangles de l'en-tête, du schéma, de la légende et des fiches ; échelle verticale."""
        p = self.profile
        fm, lh = self._metrics()
        margin = round(lh * 0.8)
        # Fiches : nombre d'unités de largeur par rangée donnant la hauteur totale la plus faible ;
        # le bilan de liaison occupe deux unités, les fiches d'une rangée incomplète s'élargissent.
        cards = self._cards()
        best = None
        for slots in range(sum(c["span"] for c in cards), 0, -1):
            if (width - margin * (slots + 1)) / slots < lh * 11 and slots > 1:
                continue
            rows, current, used = [], [], 0
            for card in cards:
                span = min(card["span"], slots)
                if current and used + span > slots:
                    rows.append(current)
                    current, used = [], 0
                current.append(card)
                used += span
            rows.append(current)
            placed, heights = [], []
            for row in rows:
                spans = sum(min(c["span"], slots) for c in row)
                room_w = width - margin * (len(row) + 1)
                row_cards = [(c, room_w * min(c["span"], slots) / spans) for c in row]
                placed.append(row_cards)
                heights.append(lh * (CARD_TITLE + 0.5) + max(self._card_lines(c, w) for c, w in row_cards) * lh * ROW)
            total = sum(heights) + margin * len(heights)
            # Chaque valeur renvoyée à la ligne pèse comme trois lignes : on préfère des fiches larges.
            score = total + lh * 3 * sum(self._card_wraps(c, w) for row in placed for c, w in row)
            if best is None or score < best[0] - 1:
                best = (score, total, placed, heights)
        _score, cards_h, card_rows, row_heights = best
        # Légende : éléments répartis sur autant de lignes que nécessaire.
        items = self._legend_items()
        item_w = [lh * 2.2 + fm.horizontalAdvance(text) + lh * 1.4 for _kind, _color, text in items]
        lines, current, used = [], [], 0.0
        available = width - 2 * margin - lh
        for index, w in enumerate(item_w):
            if current and used + w > available:
                lines.append(current)
                current, used = [], 0.0
            current.append(index)
            used += w
        if current:
            lines.append(current)
        legend_h = len(lines) * lh * 1.3 + lh * 0.3
        header_h = lh * 2.0
        axis_h = lh * 1.6
        chart_min, chart_max = lh * 8, lh * 10.5      # schéma plafonné : la place va aux fiches
        required = int(margin + header_h + chart_min + axis_h + legend_h + margin + cards_h)
        left = fm.horizontalAdvance("00000 m") + margin + lh * 0.6
        top = margin + header_h
        chart_h = min(chart_max, max(chart_min, height - cards_h - margin - legend_h - axis_h - top))
        rect = QRectF(left, top, max(10, width - left - margin - lh * 0.8), chart_h)
        legend_top = rect.bottom() + axis_h
        legend_rect = QRectF(margin, legend_top, width - 2 * margin, legend_h)
        card_rects, y = [], legend_rect.bottom() + margin * 0.6
        for row, row_h in zip(card_rows, row_heights):
            x = margin
            for card, card_w in row:
                card_rects.append((card, QRectF(x, y, card_w, row_h)))
                x += card_w + margin
            y += row_h + margin
        # Échelle verticale : marge sous le relief, place au-dessus des antennes pour les étiquettes.
        low = float(min(p["terrain"].min(), p["los"].min()))
        high = float(max((p["los"] + p["fresnel"]).max(), p["terrain"].max()))
        y_min = math.floor((low - max(5.0, (high - low) * 0.1)) / 10) * 10
        room = min(lh * 2.6, rect.height() * 0.35)
        y_max = high + (high - y_min) * room / max(1.0, rect.height() - room)
        return {"rect": rect, "y_min": y_min, "y_max": y_max, "legend": legend_rect, "legend_lines": lines,
                "items": items, "item_w": item_w, "cards": card_rects, "required": required, "margin": margin,
                "natural": int(required + chart_max - chart_min),
                "header": QRectF(margin, margin, width - 2 * margin, header_h)}

    def _frame(self, width, height):
        layout = self._layout(width, height)
        return layout["rect"], layout["y_min"], layout["y_max"]

    def _point(self, rect, y_min, y_max, distance, value):
        x = rect.left() + rect.width() * distance / self.profile["length"]
        y = rect.bottom() - rect.height() * (value - y_min) / (y_max - y_min)
        return QPointF(x, y)

    def _critical(self):
        p = self.profile
        return p["worst_ratio"] if p["frequency"] else p["worst"]

    # Contenu -----------------------------------------------------------------------
    def _legend_items(self):
        p = self.profile
        status, color = verdict(p)
        items = [("terrain", None, tr("Relief")),
                 ("dash", CURVE_COLOR, tr("Courbure terrestre (k = {k:g})", k=p["k"])),
                 ("line", color, tr("Ligne de visée"))]
        if p["frequency"]:
            items += [("fill", FRESNEL_COLOR, tr("1re zone de Fresnel")),
                      ("dash", FRESNEL_COLOR, tr("{pct:g} % de F1 (exigé)", pct=p["fz"] * 100))]
        if (p.get("report") or {}).get("obstructions"):
            items.append(("triangle", OBSTACLE_COLOR, tr("Obstacles (SPLAT!)")))
        items.append(("critical", color, tr("Point critique")))
        return items

    def _cards(self):
        """Fiches : {"title", "span" (unités de largeur), "columns" ([[(libellé, valeur, couleur)]])}."""
        p = self.profile
        r = p.get("report") or {}
        hf, hu, df, du = units(p)
        h = lambda v: f"{v / hf:.1f} {hu}"
        dist = lambda v: f"{v / df:.2f} {du}"
        dbm = lambda watts: f"{10 * math.log10(watts * 1000):+.1f} dBm" if watts > 0 else "—"
        status, color = verdict(p)
        link = [(tr("Distance"), dist(p["length"]), None),
                (tr("Azimut A / R"), f"{p['azimuth']:.1f}° / {p['azimuth_back']:.1f}°", None),
                (tr("Site TX / RX"), f"{p['tilt']:+.2f}° / {p['tilt_rx']:+.2f}°", None),
                (tr("Facteur k"), f"{p['k']:g}", None)]
        if r.get("mode"):
            link.append((tr("Propagation"), propagation_mode(r["mode"]), None))

        def site_rows(role, site, index, height, antenna):
            return [(role, site["name"], None),
                    (tr("Position"), f"{float(site['lat']):.4f}, {float(site['lon']):.4f}", None),
                    (tr("Altitude du sol"), h(p["ground"][index]), None),
                    (tr("Antenne (sol / mer)"), f"{height / hf:.4g} / {antenna / hf:.5g} {hu}", None)]

        # Bilan de liaison : émission et propagation | affaiblissements et réception.
        emission, reception = [], []
        if r.get("model"):
            emission.append((tr("Modèle"), r["model"].replace(" Version ", " "), None))
        if p["frequency"]:
            emission.append((tr("Fréquence"), f"{p['frequency']:g} MHz", None))
        if r.get("erp") is not None:
            emission.append((tr("PAR"), f"{r['erp']:.4g} W ({dbm(r['erp'])})", None))
        if r.get("eirp") is not None:
            emission.append((tr("PIRE"), f"{r['eirp']:.4g} W ({dbm(r['eirp'])})", None))
        if r.get("situations") is not None and r.get("time") is not None:
            emission.append((tr("Situations / temps"), f"{r['situations']:g} % / {r['time']:g} %", None))
        if r.get("error"):
            emission.append((tr("Avertissement"), r["error"], OBSTACLE_COLOR))
        if r.get("free_space") is not None:
            reception.append((tr("Espace libre"), f"{r['free_space']:.1f} dB", None))
        if r.get("path_loss") is not None:
            reception.append((tr("Affaiblissement total"), f"{r['path_loss']:.1f} dB", None))
        if r.get("terrain_loss") is not None:
            reception.append((tr("dont relief"), f"{r['terrain_loss']:.1f} dB", None))
        if r.get("power") is not None:
            reception.append((tr("Puissance reçue"), f"{r['power']:.1f} dBm", None))
        if r.get("field") is not None:
            reception.append((tr("Champ"), f"{r['field']:.1f} dBµV/m", None))
        if r.get("density") is not None:
            reception.append((tr("Densité de puissance"), f"{r['density']:.1f} dBW/m²", None))
        if r.get("voltage"):
            reception.append((tr("Tension 50 Ω"), f"{r['voltage'][0]:.3g} µV ({r['voltage'][1]:.1f} dBµV)", None))
        if not (emission or reception) and p["frequency"]:
            link.append((tr("Fréquence"), f"{p['frequency']:g} MHz", None))

        critical = self._critical()
        clear = [(tr("Dégagement min."), tr("{c} à {d}", c=f"{p['clearance'][critical] / hf:+.1f} {hu}",
                                               d=dist(p["distance"][critical])), color)]
        if p["min_ratio"] is not None:
            clear.append((tr("Fresnel (point critique)"), f"{p['clearance'][critical] / p['fresnel'][critical]:.0%} F1"
                          if p["fresnel"][critical] > 0 else "—", None))
        obstacles = r.get("obstructions")
        if obstacles:
            clear.append((tr("Obstacles (SPLAT!)"), tr("{n} dès {d}", n=len(obstacles),
                                                      d=dist(p["length"] - max(o[0] for o in obstacles))), None))
        elif p["first_obstruction"] is not None:
            clear.append((tr("Premier obstacle"), dist(p["first_obstruction"]), None))
        raise_ = r.get("raise", {})
        if "obstructions" in raise_:
            clear.append((tr("RX requise : visée"), h(raise_["obstructions"]), None))
        if "fresnel_part" in raise_ or "fresnel" in raise_:
            heights = [f"{raise_[k] / hf:.0f}" for k in ("fresnel_part", "fresnel") if k in raise_]
            clear.append((tr("RX requise : {pct:g} / 100 % F1", pct=p["fz"] * 100),
                          f"{' / '.join(heights)} {hu}", None))

        cards = []
        if emission or reception:
            cards.append({"title": tr("Bilan de liaison"), "span": 2,
                          "columns": [c for c in (emission, reception) if c]})
        cards += [{"title": tr("Liaison"), "span": 1, "columns": [link]},
                  {"title": tr("Dégagement"), "span": 1, "columns": [clear]},
                  {"title": tr("Sites"), "span": 2,
                   "columns": [site_rows(tr("Émetteur"), p["tx"], 0, p["h_tx"], p["a_tx"]),
                               site_rows(tr("Récepteur"), p["rx"], -1, p["h_rx"], p["a_rx"])]}]
        return cards

    # Dessin ------------------------------------------------------------------------
    def paintEvent(self, _event):
        painter = QPainter(self)
        self.render_to(painter, self.width(), self.height())

    def render_to(self, painter, width, height):
        palette = self.palette()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        base = palette.color(QPalette.ColorRole.Base)
        painter.fillRect(QRectF(0, 0, width, height), base)
        text_color = palette.color(QPalette.ColorRole.Text)
        painter.setFont(self.font())
        if not self.profile:
            painter.setPen(text_color)
            painter.drawText(QRectF(0, 0, width, height), Qt.AlignmentFlag.AlignCenter,
                             tr("Profil disponible après un calcul point à point."))
            return
        p = self.profile
        dark = base.lightness() < 128
        dim = _mix(text_color, base, 0.4)
        fm, lh = self._metrics()
        layout = self._layout(width, height)
        rect, y_min, y_max = layout["rect"], layout["y_min"], layout["y_max"]
        point = lambda d, v: self._point(rect, y_min, y_max, d, v)
        d = p["distance"]
        status, color = verdict(p)
        h_factor, h_unit, d_factor, d_unit = units(p)

        self._draw_header(painter, layout["header"], status, color, text_color, dim, h_factor, h_unit)

        # Fond du schéma : aplat neutre
        frame = QPainterPath()
        frame.addRoundedRect(rect, lh * 0.3, lh * 0.3)
        painter.fillPath(frame, _mix(base, text_color, 0.03))
        painter.save()
        painter.setClipPath(frame)

        # Grille
        grid_pen = QPen(_alpha(text_color, 28), 1)
        y_ticks = _ticks(y_min / h_factor, y_max / h_factor, 6)
        unit, label = (d_factor, d_unit) if p["length"] >= 2 * d_factor else (h_factor, h_unit)
        x_ticks = _ticks(0, p["length"] / unit, 8)
        painter.setPen(grid_pen)
        for value in y_ticks:
            y = point(0, value * h_factor).y()
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        for value in x_ticks:
            x = point(value * unit, y_min).x()
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))

        # Zone de Fresnel (1re zone et dégagement exigé -fz)
        if p["frequency"]:
            upper = [point(x, v) for x, v in zip(d, p["los"] + p["fresnel"])]
            lower = [point(x, v) for x, v in zip(d[::-1], (p["los"] - p["fresnel"])[::-1])]
            painter.setPen(QPen(_alpha(FRESNEL_COLOR, 120), 1))
            painter.setBrush(_alpha(FRESNEL_COLOR, 38))
            painter.drawPolygon(QPolygonF(upper + lower))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(_alpha(FRESNEL_COLOR, 170), 1, Qt.PenStyle.DashLine))
            for sign in (1, -1):
                painter.drawPolyline(QPolygonF([point(x, v) for x, v in zip(d, p["los"] + sign * p["fz"] * p["fresnel"])]))

        # Relief (rehaussé de la courbure), et courbure seule
        ground = QPainterPath(point(d[0], y_min))
        for x, v in zip(d, p["terrain"]):
            ground.lineTo(point(x, v))
        ground.lineTo(point(d[-1], y_min))
        ground.closeSubpath()
        crest = min(point(x, v).y() for x, v in zip(d, p["terrain"]))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_terrain_brush(crest, rect.bottom(), dark))
        painter.drawPath(ground)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(_terrain_edge(dark), 1.2))
        painter.drawPolyline(QPolygonF([point(x, v) for x, v in zip(d, p["terrain"])]))
        painter.setPen(QPen(CURVE_COLOR, 1, Qt.PenStyle.DashLine))
        painter.drawPolyline(QPolygonF([point(x, y_min + v) for x, v in zip(d, p["bulge"])]))

        # Ligne de visée
        painter.setPen(QPen(color, 1.8, cap=Qt.PenCapStyle.RoundCap))
        painter.drawLine(point(0, p["los"][0]), point(p["length"], p["los"][-1]))

        # Obstacles détectés par SPLAT! (distances depuis le récepteur)
        size = lh * 0.35
        for from_rx, _height in (p.get("report") or {}).get("obstructions", []):
            i = int(np.clip(np.searchsorted(d, p["length"] - from_rx), 0, len(d) - 1))
            c = point(d[i], p["terrain"][i])
            _triangle(painter, c, size, OBSTACLE_COLOR)

        # Point critique : cote du dégagement entre relief et visée
        self._draw_critical(painter, point, color, base, lh)

        # Curseur
        if self.cursor_index is not None:
            i = self.cursor_index
            x = point(d[i], y_min).x()
            highlight = palette.color(QPalette.ColorRole.Highlight)
            painter.setPen(QPen(highlight, 1))
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            painter.setBrush(highlight)
            painter.drawEllipse(point(d[i], p["terrain"][i]), 3.5, 3.5)
            painter.drawEllipse(point(d[i], p["los"][i]), 3.5, 3.5)
        painter.restore()

        painter.setPen(QPen(_alpha(text_color, 60), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(frame)

        # Antennes (hors du découpage : les pylônes sont posés sur les bords du schéma)
        mast = _mix(text_color, base, 0.15)
        self._draw_mast(painter, point(0, p["terrain"][0]), point(0, p["los"][0]), mast, mast, lh, True)
        self._draw_mast(painter, point(p["length"], p["terrain"][-1]), point(p["length"], p["los"][-1]),
                        base, mast, lh, False)
        for x, value, name, align in (
                (rect.left(), p["a_tx"], p["tx"]["name"], Qt.AlignmentFlag.AlignLeft),
                (rect.right(), p["a_rx"], p["rx"]["name"], Qt.AlignmentFlag.AlignRight)):
            top_y = point(0, value).y()
            text = f"{name} · {value / h_factor:.0f} {h_unit}"
            w = fm.horizontalAdvance(text) + lh
            box = QRectF(x + lh * 0.9 if align == Qt.AlignmentFlag.AlignLeft else x - lh * 0.9 - w,
                         max(rect.top() + lh * 0.3, top_y - lh * 2.1), w, lh * 1.3)
            _pill(painter, box, _alpha(base, 230), _alpha(text_color, 70), text, text_color)

        # Graduations
        painter.setPen(dim)
        for value in y_ticks:
            y = point(0, value * h_factor).y()
            if rect.top() + lh * 0.5 < y < rect.bottom() + 1:
                painter.drawText(QRectF(0, y - lh / 2, rect.left() - lh * 0.4, lh),
                                 Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{value:g} {h_unit}")
        for value in x_ticks:
            x = point(value * unit, y_min).x()
            painter.drawText(QRectF(x - 50, rect.bottom() + lh * 0.25, 100, lh), Qt.AlignmentFlag.AlignHCenter,
                             f"{value:g} {label}")

        self._draw_legend(painter, layout, text_color, base, dark, lh, fm)
        for card, card_rect in layout["cards"]:
            self._draw_card(painter, card, card_rect, text_color, dim, base, lh, fm)

    def _draw_header(self, painter, rect, status, color, text_color, dim, h_factor, h_unit):
        p = self.profile
        fm, lh = self._metrics()
        bold = QFont(self.font())
        bold.setBold(True)
        bold_fm = QFontMetricsF(bold)
        # Pastille d'état au centre, sites de part et d'autre
        width = bold_fm.horizontalAdvance(status) + lh * 1.6
        pill = QRectF(rect.center().x() - width / 2, rect.top() + (rect.height() - lh * 1.5) / 2, width, lh * 1.5)
        painter.setFont(bold)
        painter.setPen(QPen(color, 1.2))
        painter.setBrush(_alpha(color, 22))
        painter.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
        painter.setPen(color)
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, status)
        side_w = max(0.0, pill.left() - rect.left() - lh)
        for site, height, left in ((p["tx"], p["h_tx"], True), (p["rx"], p["h_rx"], False)):
            name = f"{tr('Émetteur') if left else tr('Récepteur')} : {site['name']}"
            detail = tr("antenne {h:.4g} {u}", h=height / h_factor, u=h_unit)
            line_rect = QRectF(rect.left() if left else rect.right() - side_w, rect.top(), side_w, rect.height())
            dot = lh * 0.55
            name_w = min(bold_fm.horizontalAdvance(name), side_w - dot - lh * 0.5)
            total = dot + lh * 0.4 + name_w + lh * 0.5 + fm.horizontalAdvance(detail)
            x = line_rect.left() if left else max(line_rect.left(), line_rect.right() - total)
            cy = rect.center().y()
            painter.setPen(QPen(text_color, 1.2))
            painter.setBrush(text_color if left else Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(x + dot / 2, cy), dot / 2 - 0.6, dot / 2 - 0.6)
            x += dot + lh * 0.4
            painter.setFont(bold)
            painter.setPen(text_color)
            painter.drawText(QRectF(x, cy - lh, name_w, lh * 2), Qt.AlignmentFlag.AlignVCenter,
                             bold_fm.elidedText(name, Qt.TextElideMode.ElideRight, name_w))
            x += name_w + lh * 0.5
            painter.setFont(self.font())
            painter.setPen(dim)
            painter.drawText(QRectF(x, cy - lh, max(0.0, line_rect.right() - x), lh * 2),
                             Qt.AlignmentFlag.AlignVCenter, detail)
        painter.setFont(self.font())

    def _draw_mast(self, painter, base_pt, top_pt, accent, structure, lh, transmitter):
        """Pylône en treillis surmonté d'une antenne (ondes rayonnées pour l'émetteur)."""
        x, yb, yt = base_pt.x(), base_pt.y(), top_pt.y()
        w0, w1 = lh * 0.45, lh * 0.12
        pen = QPen(structure, 1.3)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if yb - yt > lh * 0.6:
            painter.drawLine(QPointF(x - w0, yb), QPointF(x - w1, yt))
            painter.drawLine(QPointF(x + w0, yb), QPointF(x + w1, yt))
            steps = max(1, int((yb - yt) / (lh * 0.7)))
            painter.setPen(QPen(structure, 0.8))
            for k in range(steps):
                f0, f1 = k / steps, (k + 1) / steps
                y0, y1 = yb + (yt - yb) * f0, yb + (yt - yb) * f1
                a0, a1 = w0 + (w1 - w0) * f0, w0 + (w1 - w0) * f1
                side = 1 if k % 2 == 0 else -1
                painter.drawLine(QPointF(x - side * a0, y0), QPointF(x + side * a1, y1))
                painter.drawLine(QPointF(x - a1, y1), QPointF(x + a1, y1))
        else:
            painter.drawLine(QPointF(x, yb), QPointF(x, yt))
        painter.setPen(QPen(structure, 1.6))
        painter.drawLine(QPointF(x - w0 * 1.6, yb), QPointF(x + w0 * 1.6, yb))
        r = lh * 0.26
        painter.setPen(QPen(structure, 1))
        painter.setBrush(accent)
        painter.drawEllipse(top_pt, r, r)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(accent, 1.4, cap=Qt.PenCapStyle.RoundCap))
        arcs = (1.9, 2.9) if transmitter else (1.9,)
        for k in arcs:
            box = QRectF(top_pt.x() - r * k, top_pt.y() - r * k, 2 * r * k, 2 * r * k)
            painter.drawArc(box, -40 * 16, 80 * 16)
            painter.drawArc(box, 140 * 16, 80 * 16)

    def _draw_critical(self, painter, point, color, base, lh):
        p = self.profile
        i = self._critical()
        hf, hu, _df, _du = units(p)
        g, l = point(p["distance"][i], p["terrain"][i]), point(p["distance"][i], p["los"][i])
        painter.setPen(QPen(color, 1.4))
        painter.drawLine(g, l)
        for end, direction in ((g, 1), (l, -1)):
            if abs(g.y() - l.y()) > lh * 0.8:
                tip = lh * 0.3 * direction * (1 if g.y() > l.y() else -1)
                painter.drawLine(end, QPointF(end.x() - lh * 0.2, end.y() + tip))
                painter.drawLine(end, QPointF(end.x() + lh * 0.2, end.y() + tip))
        painter.setBrush(base)
        painter.drawEllipse(g, lh * 0.3, lh * 0.3)
        text = f"{p['clearance'][i] / hf:+.1f} {hu}"
        if p["frequency"] and p["fresnel"][i] > 0:
            text += f" · {p['clearance'][i] / p['fresnel'][i]:.0%} F1"
        fm, _ = self._metrics()
        w = fm.horizontalAdvance(text) + lh
        mid = QPointF(g.x(), (g.y() + l.y()) / 2)
        right = g.x() + lh * 0.6 + w < painter.clipBoundingRect().right()
        box = QRectF(mid.x() + lh * 0.6 if right else mid.x() - lh * 0.6 - w, mid.y() - lh * 0.65, w, lh * 1.3)
        _pill(painter, box, _alpha(base, 235), color, text, color)

    def _draw_legend(self, painter, layout, text_color, base, dark, lh, fm):
        rect = layout["legend"]
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_mix(base, text_color, 0.03))
        painter.drawRoundedRect(rect, lh * 0.3, lh * 0.3)
        items, widths = layout["items"], layout["item_w"]
        y = rect.top() + lh * 0.15
        for line in layout["legend_lines"]:
            total = sum(widths[i] for i in line)
            x = rect.center().x() - total / 2
            for i in line:
                kind, color, text = items[i]
                swatch = QRectF(x, y + lh * 0.25, lh * 1.8, lh * 0.8)
                _legend_symbol(painter, kind, color, swatch, text_color, dark, lh)
                painter.setPen(_mix(text_color, base, 0.25))
                painter.drawText(QRectF(swatch.right() + lh * 0.4, y, widths[i], lh * 1.3),
                                 Qt.AlignmentFlag.AlignVCenter, text)
                x += widths[i]
            y += lh * 1.3

    def _row_fits(self, label, value, width):
        fm, lh = self._metrics()
        bold = QFont(self.font())
        bold.setBold(True)
        return fm.horizontalAdvance(label) + QFontMetricsF(bold).horizontalAdvance(value) + lh * 0.4 <= width

    def _card_columns(self, card, card_w):
        """(colonnes de lignes, largeur d'une colonne, écart) ; une seule colonne si la fiche est étroite."""
        _fm, lh = self._metrics()
        columns = card["columns"]
        if len(columns) > 1 and card_w < lh * 24:
            columns = [[row for column in columns for row in column]]
        gap = lh * 1.4
        inner = card_w - lh * 1.4
        return columns, (inner - gap * (len(columns) - 1)) / len(columns), gap

    def _card_wraps(self, card, card_w):
        columns, col_w, _gap = self._card_columns(card, card_w)
        return sum(not self._row_fits(label, value, col_w) for column in columns for label, value, _c in column)

    def _card_lines(self, card, card_w):
        """Hauteur des lignes d'une fiche (en lignes de ROW × la hauteur du texte)."""
        columns, col_w, _gap = self._card_columns(card, card_w)
        return max(sum(1 if self._row_fits(label, value, col_w) else 1.9 for label, value, _c in column)
                   for column in columns)

    def _draw_card(self, painter, card, rect, text_color, dim, base, lh, fm):
        painter.setPen(QPen(_alpha(text_color, 38), 1))
        painter.setBrush(_mix(base, text_color, 0.025))
        painter.drawRoundedRect(rect, lh * 0.3, lh * 0.3)
        bold = QFont(self.font())
        bold.setBold(True)
        bold_fm = QFontMetricsF(bold)
        inner = rect.adjusted(lh * 0.7, lh * 0.25, -lh * 0.7, 0)
        painter.setFont(bold)
        painter.setPen(text_color)
        painter.drawText(QRectF(inner.left(), inner.top(), inner.width(), lh * 1.2), Qt.AlignmentFlag.AlignVCenter,
                         bold_fm.elidedText(card["title"], Qt.TextElideMode.ElideRight, inner.width()))
        painter.setPen(QPen(_alpha(text_color, 40), 1))
        painter.drawLine(QPointF(inner.left(), inner.top() + lh * 1.25), QPointF(inner.right(), inner.top() + lh * 1.25))
        columns, col_w, gap = self._card_columns(card, rect.width())
        for index, column in enumerate(columns):
            left = inner.left() + index * (col_w + gap)
            if index:
                painter.setPen(QPen(_alpha(text_color, 30), 1))
                painter.drawLine(QPointF(left - gap / 2, inner.top() + lh * CARD_TITLE),
                                 QPointF(left - gap / 2, rect.bottom() - lh * 0.4))
            y = inner.top() + lh * CARD_TITLE
            for label, value, color in column:
                wrap = not self._row_fits(label, value, col_w)
                value_w = min(bold_fm.horizontalAdvance(value), col_w)
                label_w = col_w if wrap else col_w - value_w - lh * 0.4
                painter.setFont(self.font())
                painter.setPen(dim)
                painter.drawText(QRectF(left, y, label_w, lh * ROW), Qt.AlignmentFlag.AlignVCenter,
                                 fm.elidedText(label, Qt.TextElideMode.ElideRight, label_w))
                if wrap:
                    y += lh * 0.9
                painter.setFont(bold)
                painter.setPen(color or text_color)
                painter.drawText(QRectF(left + col_w - value_w, y, value_w, lh * ROW),
                                 Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                                 bold_fm.elidedText(value, Qt.TextElideMode.ElideRight, value_w))
                y += lh * ROW
        painter.setFont(self.font())

    # Interaction -------------------------------------------------------------------
    def mouseMoveEvent(self, event):
        if not self.profile:
            return
        rect, _, _ = self._frame(self.width(), self.height())
        f = (event.position().x() - rect.left()) / rect.width()
        if not 0 <= f <= 1 or not rect.top() <= event.position().y() <= rect.bottom():
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
        if self.cursor_index is None:
            return
        self.cursor_index = None
        self.hovered.emit("")
        self.update()

    def image(self, width=1600, height=None):
        """Profil rendu en image (export PNG), fiches et légende comprises."""
        cursor, self.cursor_index = self.cursor_index, None
        font = QFont(self.font())
        font.setPixelSize(15)
        saved = self.font()
        self.setFont(font)
        height = height or self._layout(width, 0)["natural"]
        image = QImage(width, height, QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        self.render_to(painter, width, height)
        painter.end()
        self.setFont(saved)
        self.cursor_index = cursor
        self._update_minimum()
        return image


def _pill(painter, rect, fill, border, text, text_color):
    painter.setPen(QPen(border, 1.2))
    painter.setBrush(fill)
    painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
    painter.setPen(text_color)
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)


def _triangle(painter, center, size, color):
    painter.setPen(QPen(QColor(255, 255, 255), 1))
    painter.setBrush(color)
    painter.drawPolygon(QPolygonF([QPointF(center.x(), center.y() - size * 1.6),
                                   QPointF(center.x() - size, center.y() + size * 0.2),
                                   QPointF(center.x() + size, center.y() + size * 0.2)]))


def _legend_symbol(painter, kind, color, box, text_color, dark, lh):
    mid = box.center().y()
    if kind == "terrain":
        painter.setPen(QPen(_terrain_edge(dark), 1))
        painter.setBrush(_terrain_brush(box.top(), box.bottom(), dark))
        painter.drawRoundedRect(box, 2, 2)
    elif kind == "fill":
        painter.setPen(QPen(_alpha(color, 120), 1))
        painter.setBrush(_alpha(color, 38))
        painter.drawRoundedRect(box, 2, 2)
    elif kind in ("dash", "line"):
        painter.setPen(QPen(color, 1.8 if kind == "line" else 1.1,
                            Qt.PenStyle.SolidLine if kind == "line" else Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(box.left() + 3, mid), QPointF(box.right() - 3, mid))
    elif kind == "triangle":
        _triangle(painter, QPointF(box.center().x(), mid + lh * 0.25), lh * 0.35, color)
    elif kind == "critical":
        painter.setPen(QPen(color, 1.4))
        painter.drawLine(QPointF(box.center().x(), box.top() - 2), QPointF(box.center().x(), box.bottom() + 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(box.center().x(), box.bottom()), lh * 0.22, lh * 0.22)


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
