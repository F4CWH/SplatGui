"""Palettes de couleurs des cartes de couverture RF (-L).

SPLAT! lit les couleurs des contours dans les fichiers <émetteur>.lcf (perte de trajet),
.scf (champ, avec une PAR) et .dcf (puissance reçue, -dbm) du dossier d'exécution ; s'ils
n'existent pas, il les écrit avec ses couleurs par défaut. Une palette est une suite de
couleurs, du signal le plus fort au plus faible, répartie sur des seuils : les siens pour
chaque grandeur s'ils sont définis, sinon ceux de SPLAT! (couleurs interpolées si leur nombre
diffère du nombre de seuils ; une couleur par seuil donne des bandes franches).

Palette : {"colors": [[r, g, b], …], "levels": {".dcf": [seuils], …}} (seuils facultatifs).
Palettes personnelles : réglage "coverage_palettes" = [{"name": …, "colors": …, "levels": …}].
"""

import re

import numpy as np

from .i18n import N_, tr

# Seuils par défaut de SPLAT!, du signal le plus fort au plus faible.
LEVELS = {
    ".lcf": list(range(80, 231, 10)),          # perte de trajet (dB)
    ".scf": list(range(128, 7, -10)),          # champ (dBµV/m)
    ".dcf": list(range(0, -151, -10)),         # puissance reçue (dBm)
}
HEADERS = {
    ".lcf": ("Path-Loss", "dB", "path loss (in dB)"),
    ".scf": ("Signal", "dBuV/m", "signal strength (in dBuV/m)"),
    ".dcf": ("DBM Signal Level", "dBm", "received signal power level (in dBm)"),
}
DBM_RANGE = (-200, 40)                         # niveaux acceptés par SPLAT! dans un .dcf

SPLAT_COLORS = [(255, 0, 0), (255, 128, 0), (255, 165, 0), (255, 206, 0), (255, 255, 0), (184, 255, 0),
                (0, 255, 0), (0, 208, 0), (0, 196, 196), (0, 148, 255), (80, 80, 255), (0, 38, 255),
                (142, 63, 255), (196, 54, 255), (255, 0, 255), (255, 194, 204)]

# clé -> (libellé, couleurs du plus fort au plus faible, seuils propres) ; "" : fichiers de SPLAT!
BUILTIN = {
    "": (N_("SPLAT! (par défaut)"), SPLAT_COLORS, {}),
    "feu": (N_("Feu tricolore (vert → rouge)"), [(0, 150, 60), (90, 200, 70), (220, 230, 60), (250, 170, 40),
                                                 (230, 70, 40), (150, 20, 30)], {}),
    "spectre": (N_("Spectre (rouge → violet)"), [(220, 30, 30), (250, 150, 30), (240, 230, 50), (80, 210, 80),
                                                 (40, 170, 220), (60, 70, 220), (130, 50, 190)], {}),
    "chaleur": (N_("Chaleur (jaune → violet foncé)"), [(252, 255, 164), (249, 190, 42), (235, 104, 43),
                                                       (182, 54, 84), (114, 31, 129), (40, 11, 84)], {}),
    "viridis": (N_("Viridis (daltoniens)"), [(253, 231, 37), (94, 201, 98), (33, 145, 140), (59, 82, 139),
                                             (68, 1, 84)], {}),
    "bleus": (N_("Bleus (foncé → clair)"), [(8, 48, 107), (33, 113, 181), (107, 174, 214), (198, 219, 239)], {}),
    # Échelle des cartes de la démonstration Python d'ITM (https://udel.edu/~mm/itm/, matplotlib Spectral) :
    # bandes de 20 dBµV/m de -14 à 126.
    "itm": (N_("Spectral"), [(197, 44, 75), (245, 117, 71), (254, 202, 121), (255, 254, 190),
                             (205, 235, 157), (110, 197, 165), (63, 119, 181)],
            {".scf": [106, 86, 66, 46, 26, 6, -14]}),
}
DEFAULT = ""
USER_PREFIX = "user:"
MAX_COLORS = 32                                # zones de contour lues par SPLAT!

# Couleurs réservées sur la carte SPLAT! : rouge des repères de sites, bleu de la mer.
RESERVED = {(255, 0, 0): (254, 0, 0), (0, 0, 170): (0, 0, 171)}


def user_palettes(settings):
    return [p for p in settings.get("coverage_palettes", [])
            if isinstance(p, dict) and p.get("name") and len(p.get("colors", [])) >= 2]


def _palette(colors, levels):
    return {"colors": [tuple(c) for c in colors],
            "levels": {kind: list(values) for kind, values in (levels or {}).items()
                       if kind in LEVELS and len(values) >= 2}}


def choices(settings):
    """[(clé, libellé traduit, palette)] : palettes fournies puis personnelles."""
    items = [(key, tr(label), _palette(colors, levels)) for key, (label, colors, levels) in BUILTIN.items()]
    items += [(USER_PREFIX + p["name"], p["name"], _palette(p["colors"], p.get("levels")))
              for p in user_palettes(settings)]
    return items


def palette_of(key, settings):
    """Palette `key` (fournie ou personnelle), ou None si elle n'existe plus."""
    for item_key, _label, palette in choices(settings):
        if item_key == key:
            return palette
    return None


def levels_of(palette, kind):
    """Seuils de la palette pour la grandeur `kind`, du plus fort au plus faible signal."""
    return palette.get("levels", {}).get(kind) or LEVELS[kind]


def sort_levels(values, kind):
    """Du signal le plus fort au plus faible : perte croissante, champ et puissance décroissants."""
    return sorted(values, reverse=kind != ".lcf")


def parse_levels(text, kind):
    """Seuils saisis (séparés par des espaces, virgules ou points-virgules) triés du plus fort au
    plus faible signal ; [] si vide. Lève ValueError avec un message à afficher."""
    parts = [p for p in re.split(r"[\s,;]+", text.strip()) if p]
    if not parts:
        return []
    values = []
    for part in parts:
        try:
            values.append(int(part))
        except ValueError:
            raise ValueError(tr("« {value} » : les seuils sont des nombres entiers.", value=part)) from None
    if len(set(values)) != len(values):
        raise ValueError(tr("Seuils en double."))
    if not 2 <= len(values) <= MAX_COLORS:
        raise ValueError(tr("De 2 à {n} seuils.", n=MAX_COLORS))
    if kind == ".dcf" and not all(DBM_RANGE[0] <= v <= DBM_RANGE[1] for v in values):
        raise ValueError(tr("Puissance reçue : seuils de {low} à {high} dBm.", low=DBM_RANGE[0], high=DBM_RANGE[1]))
    if kind == ".lcf" and min(values) < 0:
        raise ValueError(tr("Perte de trajet : seuils positifs."))
    return sort_levels(values, kind)


def level_colors(colors, count):
    """`count` couleurs réparties sur la palette (interpolation linéaire en RVB). Les gris
    (relief) et les couleurs réservées de la carte sont légèrement décalés : SPLAT!Gui doit
    pouvoir distinguer une zone couverte du relief, de la mer et des repères de sites."""
    colors = np.array(colors, dtype=float)
    if len(colors) == count:
        values = colors
    else:
        t = np.linspace(0, 1, count)
        stops = np.linspace(0, 1, len(colors))
        values = np.stack([np.interp(t, stops, colors[:, c]) for c in range(3)], axis=-1)
    out = []
    for rgb in np.rint(values).astype(int):
        rgb = tuple(int(v) for v in np.clip(rgb, 0, 255))
        if rgb[0] == rgb[1] == rgb[2]:
            rgb = (rgb[0], rgb[1], rgb[2] + 1 if rgb[2] < 255 else 254)
        out.append(RESERVED.get(rgb, rgb))
    return out


def levels_and_colors(palette, kind):
    """[(seuil, (r, g, b))] de la palette pour la grandeur `kind`."""
    levels = levels_of(palette, kind)
    return list(zip(levels, level_colors(palette["colors"], len(levels))))


def _band_color(value, kind, new_entries):
    """Couleur de la nouvelle palette pour la bande de seuil `value` d'un calcul : bande de la
    nouvelle palette qui contient ce seuil (la plus faible au-delà de la dernière)."""
    for level, color in new_entries:
        if (value <= level) if kind == ".lcf" else (value >= level):
            return color
    return new_entries[-1][1]


def map_entries(entries, kind, palette):
    """[(seuil, couleur)] d'un fichier de couleurs du calcul, avec les couleurs de `palette` :
    seuils du calcul conservés (les bandes de la carte sont figées par SPLAT!), couleurs exactes
    si la palette a les mêmes seuils."""
    new_entries = levels_and_colors(palette, kind)
    return [(value, _band_color(value, kind, new_entries)) for value, _color in entries]


def recolor_image(image, entries, new_entries, kind, smooth):
    """Carte SPLAT! `image` (QImage) dont les couleurs des seuils `entries` [(seuil, couleur)]
    deviennent celles de `new_entries` (mêmes seuils). Avec -sc (`smooth`), les couleurs
    intermédiaires sont interpolées entre les nouvelles couleurs des deux seuils voisins.
    Relief (gris), mer, repères et couleurs inconnues inchangés. Renvoie `image` si rien ne change."""
    from PyQt6.QtGui import QImage
    old = dict((tuple(c), tuple(n)) for (_v, c), (_w, n) in zip(entries, new_entries))
    if all(o == n for o, n in old.items()):
        return image
    order = sorted(range(len(entries)), key=lambda i: entries[i][0], reverse=kind != ".lcf")
    old_colors = np.array([entries[i][1] for i in order], dtype=float)
    new_colors = np.array([new_entries[i][1] for i in order], dtype=float)

    source = image.convertToFormat(QImage.Format.Format_ARGB32)
    w, h = source.width(), source.height()
    pixels = np.frombuffer(source.constBits().asstring(source.sizeInBytes()), dtype=np.uint32)
    pixels = pixels.reshape(h, source.bytesPerLine() // 4)[:, :w]
    rgb = pixels & 0xFFFFFF
    if not smooth:
        # Couleurs des seuils seulement : remplacement direct (bien plus rapide que np.unique).
        out = rgb.copy()
        for (r, g, b), (nr, ng, nb) in old.items():
            out[rgb == (r << 16) | (g << 8) | b] = (nr << 16) | (ng << 8) | nb
        out = np.ascontiguousarray((pixels & 0xFF000000) | out)
        return QImage(out.data, w, h, w * 4, QImage.Format.Format_ARGB32).copy()
    unique, inverse = np.unique(rgb, return_inverse=True)
    replaced = unique.copy()
    for i, value in enumerate(unique):
        color = (int(value >> 16) & 255, int(value >> 8) & 255, int(value) & 255)
        target = old.get(color)
        if target is None and smooth and len(old_colors) > 1 and not (color[0] == color[1] == color[2]):
            # Couleur intermédiaire (-sc) : point le plus proche sur les segments entre seuils voisins.
            point = np.array(color, dtype=float)
            start, end = old_colors[1:], old_colors[:-1]
            segment = end - start
            t = np.clip(((point - start) * segment).sum(axis=1) / np.maximum((segment ** 2).sum(axis=1), 1e-9), 0, 1)
            distance = np.linalg.norm(start + t[:, None] * segment - point, axis=1)
            k = int(np.argmin(distance))
            if distance[k] <= 12:
                mixed = new_colors[k + 1] + t[k] * (new_colors[k] - new_colors[k + 1])
                target = tuple(int(round(v)) for v in mixed)
        if target is not None:
            replaced[i] = (target[0] << 16) | (target[1] << 8) | target[2]
    out = (pixels & 0xFF000000) | replaced[inverse].reshape(rgb.shape).astype(np.uint32)
    out = np.ascontiguousarray(out)
    return QImage(out.data, w, h, w * 4, QImage.Format.Format_ARGB32).copy()


def file_text(kind, palette, name):
    """Contenu d'un fichier de couleurs SPLAT! (.lcf, .scf ou .dcf)."""
    title, unit, what = HEADERS[kind]
    lines = [f'; SPLAT!Gui {title} Color Definition ("{name}") File', ";",
             "; Format for the parameters held in this file is as follows:", ";",
             f"; {unit}: red, green, blue", ";",
             f'; ...where "{unit}" is the {what} and', '; "red", "green", and "blue" are the corresponding RGB color',
             "; definitions ranging from 0 to 255 for the region specified.", ";"]
    for level, (r, g, b) in levels_and_colors(palette, kind):
        lines.append(f"{level}: {r}, {g}, {b}")
    return "\n".join(lines) + "\n"


def write_files(run_dir, base, palette):
    """Écrit <base>.lcf, .scf et .dcf dans `run_dir` (SPLAT! choisit selon la PAR et -dbm)."""
    for kind in LEVELS:
        (run_dir / f"{base}{kind}").write_text(file_text(kind, palette, base + kind), encoding="ascii")
