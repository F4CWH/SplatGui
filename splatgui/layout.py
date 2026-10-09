"""Mise en page de la carte de sortie : légende, échelle graphique et flèche du nord.

La légende est construite à partir de ce que la carte contient réellement :
- couverture LOS (-c) : couleurs des émetteurs et de leurs recouvrements (table de SPLAT!) ;
- perte de trajet / champ (-L) : seuils lus dans les fichiers .lcf / .scf / .dcf du dossier ;
- point à point : couleur du trajet de chaque émetteur vers le récepteur ;
- sites (icônes), calques GeoJSON cochés et attribution du fond de carte.
"""

import math
import re
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPen, QPolygonF
from .i18n import tr

GREEN, CYAN, VIOLET, SIENNA = (0, 255, 0), (0, 255, 255), (147, 112, 219), (255, 130, 71)
# Couleurs SPLAT! des zones en visibilité (-c) : combinaison d'émetteurs (indices 1..4).
COVERAGE_COLORS = [
    ((1,), GREEN), ((2,), CYAN), ((3,), VIOLET), ((4,), SIENNA),
    ((1, 2), (255, 255, 0)), ((1, 3), (255, 192, 203)), ((1, 4), (173, 255, 47)),
    ((2, 3), (255, 165, 0)), ((2, 4), (193, 255, 193)), ((3, 4), (0, 206, 209)),
    ((1, 2, 3), (0, 100, 0)), ((1, 2, 4), (255, 235, 205)), ((1, 3, 4), (0, 250, 154)),
    ((2, 3, 4), (210, 180, 140)), ((1, 2, 3, 4), (238, 201, 0)),
]
PATH_COLORS = [GREEN, CYAN, VIOLET, SIENNA]


# --- Lecture de l'exécution -------------------------------------------------------

def run_arguments(run_dir):
    """Arguments SPLAT! de l'exécution, relus dans commande.cmd."""
    cmd = Path(run_dir) / "commande.cmd"
    if not cmd.exists():
        return []
    for line in cmd.read_text(encoding="utf-8", errors="replace").splitlines():
        if ".exe\"" in line:
            return re.findall(r'"[^"]*"|\S+', line.split('.exe"', 1)[1])
    return []


def _arg_value(args, flag):
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1].strip('"')
    return None


def run_mode(args):
    if "-c" in args:
        return "coverage"
    if "-L" in args:
        return "pathloss"
    if "-r" in args:
        return "p2p"
    return "map"


def read_color_file(path):
    """[(valeur, (r, g, b)), …] dans l'ordre du fichier .lcf / .scf / .dcf."""
    entries = []
    for line in Path(path).read_text(encoding="latin-1").splitlines():
        line = line.split(";")[0].strip()
        match = re.match(r"(-?\d+(?:\.\d+)?)\s*:\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", line)
        if match:
            entries.append((float(match.group(1)), tuple(int(match.group(i)) for i in (2, 3, 4))))
    return entries


def _pathloss_scale(run_dir, args):
    """(titre, [(texte, couleur)]) pour une carte -L, d'après le fichier de couleurs utilisé."""
    levels = _pathloss_levels(run_dir, args)
    if levels is None:
        return None
    kind, title, unit, entries = levels
    rows = []
    for i, (value, color) in enumerate(entries):
        if kind == ".lcf":
            text = f"< {value:g} {unit}" if i == 0 else f"{entries[i - 1][0]:g} – {value:g} {unit}"
        else:
            text = f"≥ {value:g} {unit}"
        rows.append((text, color))
    return title, rows


def _pathloss_levels(run_dir, args):
    """(extension, titre, unité, [(valeur, couleur)]) du fichier de couleurs d'une carte -L
    (.dcf : dBm avec -dbm, .scf : champ avec une PAR, sinon .lcf : perte), ou None."""
    run_dir = Path(run_dir)
    tx = next(iter(sorted(run_dir.glob("tx1_*.qth"))), None)
    if tx is None:
        return None
    erp = _arg_value(args, "-erp")
    if erp is None:
        lrp = tx.with_suffix(".lrp")
        if lrp.exists():
            values = [l.split(";")[0].strip() for l in lrp.read_text(encoding="latin-1").splitlines()]
            values = [v for v in values if v]
            erp = values[8] if len(values) > 8 else None
    try:
        erp = float(erp) if erp else 0.0
    except ValueError:
        erp = 0.0
    if "-dbm" in args:
        kind, title, unit = ".dcf", tr("Puissance reçue"), "dBm"
    elif erp > 0:
        kind, title, unit = ".scf", tr("Champ électrique"), "dBµV/m"
    else:
        kind, title, unit = ".lcf", tr("Perte de trajet"), "dB"
    path = tx.with_suffix(kind)
    if not path.exists():
        return None
    entries = read_color_file(path)
    return (kind, title, unit, entries) if entries else None


def _is_relief(rgb):
    """Pixel de relief (gris) ou de mer de la carte SPLAT! : aucune couverture tracée."""
    r, g, b = rgb
    return r == g == b or rgb == (0, 0, 170)


def _distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, a)))


def level_lookup(run_dir, tx_sites):
    """Fonction (r, g, b, lat, lon) -> texte du niveau de réception représenté par un pixel de
    la carte SPLAT! du dossier `run_dir`, ou None (point à point, couleur non reconnue).
    -L : couleur du fichier .dcf / .scf / .lcf (avec -sc, couleurs interpolées entre deux
    seuils : valeur interpolée) ; -c : émetteurs en visibilité. Au-delà de la portée -R de
    tous les émetteurs, SPLAT! ne calcule rien : « hors de la zone calculée »."""
    args = run_arguments(run_dir)
    mode = run_mode(args)
    tx_names = [site["name"] for site in tx_sites]
    try:
        range_km = float(_arg_value(args, "-R")) * (1.0 if "-metric" in args else 1.609344)
    except (TypeError, ValueError):
        range_km = None
    lookup = _level_function(run_dir, args, mode, tx_names)
    if lookup is None:
        return None

    def level(rgb, lat, lon):
        if range_km and tx_sites and min(_distance_km(lat, lon, float(s["lat"]), float(s["lon"]))
                                         for s in tx_sites) > range_km:
            return tr("hors de la zone calculée")
        return lookup(rgb)
    return level


def _level_function(run_dir, args, mode, tx_names):
    """Fonction (r, g, b) -> texte du niveau, selon le mode de la carte, ou None."""
    if mode == "coverage":
        colors = {color: combo for combo, color in COVERAGE_COLORS if max(combo) <= len(tx_names)}

        def coverage(rgb):
            combo = colors.get(rgb)
            if combo:
                return tr("en visibilité de {sites}", sites=" + ".join(tx_names[i - 1] for i in combo))
            return tr("hors visibilité") if _is_relief(rgb) else None
        return coverage
    if mode != "pathloss":
        return None
    levels = _pathloss_levels(run_dir, args)
    if levels is None:
        return None
    kind, title, unit, entries = levels
    # Seuils du meilleur au moins bon : puissance / champ décroissants, perte croissante.
    entries = sorted(entries, key=lambda e: e[0], reverse=kind != ".lcf")
    exact = {color: i for i, (_value, color) in reversed(list(enumerate(entries)))}
    worst = entries[-1][0]
    smooth = "-sc" in args
    colors = np.array([color for _value, color in entries], dtype=float)

    def describe(i):
        # SPLAT! donne la couleur du seuil i aux points entre ce seuil et le précédent (meilleur).
        value = entries[i][0]
        if i == 0:
            return f"{'<' if kind == '.lcf' else '≥'} {value:g} {unit}"
        low, high = sorted((value, entries[i - 1][0]))
        return f"{low:g} … {high:g} {unit}"

    def pathloss(rgb):
        if rgb in exact:
            return tr("{title} : {level}", title=title, level=describe(exact[rgb]))
        if _is_relief(rgb):
            return tr("{title} : {level}", title=title, level=f"{'>' if kind == '.lcf' else '<'} {worst:g} {unit}")
        if smooth and len(entries) > 1:
            # Couleur intermédiaire : point le plus proche sur les segments entre seuils voisins.
            point = np.array(rgb, dtype=float)
            start, end = colors[1:], colors[:-1]
            segment = end - start
            length2 = np.maximum((segment ** 2).sum(axis=1), 1e-9)
            t = np.clip(((point - start) * segment).sum(axis=1) / length2, 0, 1)
            distance = np.linalg.norm(start + t[:, None] * segment - point, axis=1)
            k = int(np.argmin(distance))
            if distance[k] <= 12:
                value = entries[k + 1][0] + t[k] * (entries[k][0] - entries[k + 1][0])
                return tr("{title} : {level}", title=title, level=f"≈ {value:.0f} {unit}")
        return None
    return pathloss


def _present_colors(coverage):
    """Ensemble des couleurs (r, g, b) présentes dans le calque de couverture."""
    image = coverage.convertToFormat(QImage.Format.Format_ARGB32)
    ptr = image.constBits()
    ptr.setsize(image.sizeInBytes())
    arr = np.frombuffer(ptr, np.uint8).reshape(image.height(), image.bytesPerLine() // 4, 4)
    arr = arr[:, : image.width()]
    opaque = arr[..., 3] > 0
    if not opaque.any():
        return set()
    pixels = arr[opaque][:, :3]
    colors, counts = np.unique(pixels, axis=0, return_counts=True)
    return {(int(c[2]), int(c[1]), int(c[0])) for c, n in zip(colors, counts) if n >= 3}


def legend_entries(run_dir, coverage, sites, site_icons, layers, attributions, show_coverage=True):
    """Liste d'éléments : ("title", texte) | ("swatch", couleur, texte) |
    ("line", couleur, texte) | ("icon", QImage, texte) | ("note", texte)."""
    args = run_arguments(run_dir)
    mode = run_mode(args)
    present = _present_colors(coverage) if coverage is not None else set()
    smooth = "-sc" in args
    tx_names = [s["name"] for s in sites if s["role"] == "tx"]
    rx_names = [s["name"] for s in sites if s["role"] == "rx"]
    entries = []
    metric = "-metric" in args

    if not show_coverage:
        mode = "hidden"
    if mode == "coverage":
        height = _arg_value(args, "-c")
        unit = "m" if metric else "ft"
        entries.append(("title", tr("Zones en visibilité (réception à {height} {unit})", height=height, unit=unit)
                        if height else tr("Zones en visibilité")))
        for combo, color in COVERAGE_COLORS:
            if max(combo) <= len(tx_names) and color in present:
                entries.append(("swatch", color, " + ".join(tx_names[i - 1] for i in combo)))
    elif mode == "pathloss":
        scale = _pathloss_scale(run_dir, args)
        if scale:
            title, rows = scale
            height = _arg_value(args, "-L")
            unit = "m" if metric else "ft"
            entries.append(("title", tr("{title} (réception à {height} {unit})", title=title, height=height, unit=unit)
                            if height else title))
            for text, color in rows:
                if smooth or color in present:
                    entries.append(("swatch", color, text))
    elif mode == "p2p":
        entries.append(("title", tr("Trajets")))
        rx = rx_names[0] if rx_names else tr("récepteur")
        for i, name in enumerate(tx_names[:4]):
            entries.append(("line", PATH_COLORS[i], f"{name} → {rx}"))

    if site_icons:
        entries.append(("title", tr("Sites")))
        for image, site in site_icons:
            role = tr("émetteur") if site["role"] == "tx" else tr("récepteur")
            entries.append(("icon", image, f"{site['name']} ({role})"))
    if layers:
        entries.append(("title", tr("Limites")))
        for color, label in layers:
            entries.append(("line", QColor(color), label))
    for text in attributions:
        entries.append(("note", text))
    return entries


# --- Dessin -----------------------------------------------------------------

def _font(px, bold=False):
    font = QFont()
    font.setPixelSize(max(4, int(round(px))))
    font.setBold(bold)
    return font


def base_size(image):
    return max(11, round(min(image.width(), image.height()) / 55))


def _qcolor(color):
    return color if isinstance(color, QColor) else QColor(*color)


def legend_size(entries, px):
    title_font, text_font, note_font = _font(px, True), _font(px), _font(px * 0.8)
    swatch = px * 1.4
    width, height = 0.0, px * 0.6
    for entry in entries:
        kind = entry[0]
        if kind == "title":
            m = QFontMetricsF(title_font)
            width = max(width, m.horizontalAdvance(entry[1]))
            height += m.height() + px * 0.3
        elif kind == "note":
            m = QFontMetricsF(note_font)
            width = max(width, m.horizontalAdvance(entry[1]))
            height += m.height()
        else:
            m = QFontMetricsF(text_font)
            width = max(width, swatch + px * 0.5 + m.horizontalAdvance(entry[2]))
            height += max(swatch, m.height()) + px * 0.25
    return width + px * 1.4, height + px * 0.4


def draw_legend(painter, entries, rect, px):
    title_font, text_font, note_font = _font(px, True), _font(px), _font(px * 0.8)
    swatch = px * 1.4
    painter.setPen(QPen(QColor(60, 60, 60), 1))
    painter.setBrush(QColor(255, 255, 255, 225))
    painter.drawRoundedRect(rect, px * 0.4, px * 0.4)
    x = rect.left() + px * 0.7
    y = rect.top() + px * 0.5
    for entry in entries:
        kind = entry[0]
        if kind == "title":
            painter.setFont(title_font)
            m = QFontMetricsF(title_font)
            y += px * 0.2
            painter.setPen(QColor(20, 20, 20))
            painter.drawText(QPointF(x, y + m.ascent()), entry[1])
            y += m.height() + px * 0.1
        elif kind == "note":
            painter.setFont(note_font)
            m = QFontMetricsF(note_font)
            painter.setPen(QColor(90, 90, 90))
            painter.drawText(QPointF(x, y + m.ascent()), entry[1])
            y += m.height()
        else:
            painter.setFont(text_font)
            m = QFontMetricsF(text_font)
            row_h = max(swatch, m.height())
            box = QRectF(x, y + (row_h - swatch) / 2, swatch, swatch)
            if kind == "swatch":
                painter.setPen(QPen(QColor(80, 80, 80), 1))
                painter.setBrush(_qcolor(entry[1]))
                painter.drawRect(box)
            elif kind == "line":
                painter.setPen(QPen(_qcolor(entry[1]), max(2.0, px / 5)))
                painter.drawLine(QPointF(box.left(), box.center().y()), QPointF(box.right(), box.center().y()))
            elif kind == "icon":
                icon = entry[1]
                scaled = icon.scaled(int(swatch), int(swatch), Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
                painter.drawImage(QPointF(box.center().x() - scaled.width() / 2,
                                          box.center().y() - scaled.height() / 2), scaled)
            painter.setPen(QColor(20, 20, 20))
            painter.drawText(QPointF(x + swatch + px * 0.5, y + (row_h - m.height()) / 2 + m.ascent()),
                             entry[2])
            y += row_h + px * 0.25


def _nice_length(max_km):
    """Longueur « ronde » (1, 2, 5 × 10ⁿ km) inférieure ou égale à max_km."""
    exponent = math.floor(math.log10(max_km))
    for factor in (5, 2, 1):
        value = factor * 10 ** exponent
        if value <= max_km:
            return value
    return 10 ** exponent


def draw_scale(painter, ref, anchor_right, bottom, px, image_width):
    """Échelle graphique (km) en bas de la carte. `ref` doit avoir des pixels de taille réelle
    homogène (carte aux bonnes proportions)."""
    dx, _dy = ref.pixel_size()
    lat_mid = math.radians((ref.lat0 + ref.lat1) / 2)
    km_per_px = abs(dx) * 111.32 * math.cos(lat_mid)
    length_km = _nice_length(image_width * 0.25 * km_per_px)
    length_px = length_km / km_per_px
    bar_h = px * 0.6
    font = _font(px)
    m = QFontMetricsF(font)
    label = f"{length_km:g} km" if length_km >= 1 else f"{length_km * 1000:g} m"
    pad = px * 0.6
    total_w = length_px + m.horizontalAdvance(label) / 2 + m.horizontalAdvance("0") / 2 + 2 * pad
    total_h = bar_h + m.height() + 2 * pad
    left = anchor_right - total_w if anchor_right is not None else px
    box = QRectF(left, bottom - total_h, total_w, total_h)
    painter.setPen(QPen(QColor(60, 60, 60), 1))
    painter.setBrush(QColor(255, 255, 255, 225))
    painter.drawRoundedRect(box, px * 0.4, px * 0.4)
    x0 = box.left() + pad + m.horizontalAdvance("0") / 2
    y0 = box.top() + pad + m.height()
    segments = 4
    for i in range(segments):
        painter.setBrush(QColor(20, 20, 20) if i % 2 == 0 else QColor(255, 255, 255))
        painter.setPen(QPen(QColor(20, 20, 20), 1))
        painter.drawRect(QRectF(x0 + i * length_px / segments, y0, length_px / segments, bar_h))
    painter.setFont(font)
    painter.setPen(QColor(20, 20, 20))
    painter.drawText(QPointF(x0 - m.horizontalAdvance("0") / 2, box.top() + pad + m.ascent()), "0")
    painter.drawText(QPointF(x0 + length_px - m.horizontalAdvance(label) / 2, box.top() + pad + m.ascent()),
                     label)
    return box


def draw_north(painter, center_x, top, px):
    size = px * 2.2
    cx, cy = center_x, top + size * 0.9
    painter.setPen(QPen(QColor(20, 20, 20), 1))
    painter.setBrush(QColor(255, 255, 255, 225))
    painter.drawEllipse(QPointF(cx, cy), size * 0.75, size * 0.75)
    left = QPolygonF([QPointF(cx, cy - size * 0.6), QPointF(cx - size * 0.3, cy + size * 0.4), QPointF(cx, cy + size * 0.2)])
    right = QPolygonF([QPointF(cx, cy - size * 0.6), QPointF(cx + size * 0.3, cy + size * 0.4), QPointF(cx, cy + size * 0.2)])
    painter.setBrush(QColor(20, 20, 20))
    painter.drawPolygon(left)
    painter.setBrush(QColor(255, 255, 255))
    painter.drawPolygon(right)
    font = _font(px, True)
    painter.setFont(font)
    m = QFontMetricsF(font)
    painter.setPen(QColor(20, 20, 20))
    painter.drawText(QPointF(cx - m.horizontalAdvance("N") / 2, cy - size * 0.78), "N")


def _busy_corner(coverage, corner, fraction=0.3):
    """Part de pixels de couverture dans un coin (pour placer la légende au plus dégagé)."""
    if coverage is None:
        return 0.0
    w, h = coverage.width(), coverage.height()
    cw, ch = int(w * fraction), int(h * fraction)
    x = 0 if "left" in corner else w - cw
    y = 0 if "top" in corner else h - ch
    sub = coverage.copy(x, y, cw, ch).convertToFormat(QImage.Format.Format_ARGB32)
    ptr = sub.constBits()
    ptr.setsize(sub.sizeInBytes())
    alpha = np.frombuffer(ptr, np.uint8).reshape(ch, sub.bytesPerLine() // 4, 4)[:, :cw, 3]
    return float((alpha > 0).mean())


LEGEND_SCALES = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75)
DEFAULT_LEGEND_SCALE = 0.75


def decorate(image, ref, entries, coverage_display, show_legend, show_scale, avoid=(),
             legend_scale=DEFAULT_LEGEND_SCALE):
    """Ajoute légende, échelle et nord sur `image` (déjà aux bonnes proportions).
    La légende va dans le coin le plus dégagé ; elle ne recouvre pas les points `avoid`
    (positions des sites en pixels de `image`) si un autre coin le permet.
    `legend_scale` multiplie la taille de la légende (texte, pastilles, cadre)."""
    out = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    margin = base_size(out)
    px = base_size(out) * legend_scale
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    corners = ["bottom-left", "top-left", "top-right", "bottom-right"]
    legend_corner = None
    if show_legend and entries:
        width, height = legend_size(entries, px)
        # Légende trop grande pour la carte : on réduit le texte.
        while (width > out.width() * 0.6 or height > out.height() * 0.8) and px > 4:
            px *= 0.9
            width, height = legend_size(entries, px)
        def rect_for(corner):
            x = margin if "left" in corner else out.width() - width - margin
            y = margin if "top" in corner else out.height() - height - margin
            return QRectF(x, y, width, height)

        def score(corner):
            covered = rect_for(corner).adjusted(-px * 3, -px * 3, px * 3, px * 3)
            hidden_sites = sum(covered.contains(QPointF(x, y)) for x, y in avoid)
            return hidden_sites * 10 + _busy_corner(coverage_display, corner)

        legend_corner = min(corners, key=score)
        x, y = rect_for(legend_corner).left(), rect_for(legend_corner).top()
        draw_legend(painter, entries, QRectF(x, y, width, height), px)
    if show_scale:
        px = base_size(out)
        scale_right = legend_corner != "bottom-right"
        draw_scale(painter, ref, out.width() - margin if scale_right else None,
                   out.height() - margin, px, out.width())
        north_x = out.width() - margin - px * 2 if legend_corner != "top-right" else margin + px * 2
        draw_north(painter, north_x, margin, px)
    painter.end()
    return out
