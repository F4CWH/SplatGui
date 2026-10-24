"""Conversion de tuiles DTED (.dt0, .dt1, .dt2) en fichiers SDF de SPLAT!.

Par défaut, les DTED niveau 2 donnent des SDF HD (1", 3601 nœuds, splat-hd) et les niveaux 0/1
des SDF standard (3", 1201 nœuds). Au-delà de 50° de latitude, le DTED espace davantage les
méridiens (2", 3", 4" ou 6") : les colonnes sont réinterpolées pour revenir à une grille carrée.
Les trous (-32767) sont comblés par la moyenne des voisins, comme pour les autres sources.
"""

from pathlib import Path

import numpy as np

from . import dem, terrain
from .i18n import tr

SUFFIXES = (".dt0", ".dt1", ".dt2")
HEADER_SIZE = 80 + 648 + 2700           # UHL + DSI + ACC
VOID = -32767


def _angle(text):
    """« DDDMMSSH » (UHL) → degrés signés."""
    deg, minutes, sec, hemi = int(text[:3]), int(text[3:5]), int(text[5:7]), text[7]
    value = deg + minutes / 60 + sec / 3600
    return -value if hemi in "SW" else value


def read_dted(path):
    """Renvoie (tuile (lat, lon), grille du nord au sud et d'ouest en est, masque des trous)."""
    raw = Path(path).read_bytes()
    if not raw:
        raise ValueError(tr("{path} : fichier vide", path=path))
    uhl = raw[:80].decode("ascii", "replace")
    if not uhl.startswith("UHL"):
        raise ValueError(tr("{path} : en-tête UHL absent, pas un fichier DTED", path=path))
    lon, lat = _angle(uhl[4:12]), _angle(uhl[12:20])
    n_lon, n_lat = int(uhl[47:51]), int(uhl[51:55])
    record = 8 + 2 * n_lat + 4               # sentinelle + compteurs, altitudes, somme de contrôle
    if len(raw) < HEADER_SIZE + n_lon * record:
        raise ValueError(tr("{path} : fichier DTED tronqué", path=path))
    body = np.frombuffer(raw, np.uint8, count=n_lon * record, offset=HEADER_SIZE).reshape(n_lon, record)
    if not (body[:, 0] == 0xAA).all():
        raise ValueError(tr("{path} : enregistrements de données corrompus", path=path))
    words = body[:, 8:8 + 2 * n_lat].copy().view(">u2").astype(np.int32)
    # Altitudes en « signe et valeur absolue » sur 16 bits.
    heights = np.where(words & 0x8000, -(words & 0x7FFF), words)   # colonnes ouest → est, sud → nord
    grid = heights.T[::-1]                                          # nord → sud, ouest → est
    return (round(lat), round(lon)), grid, grid == VOID


def resample(grid, n):
    """Interpolation bilinéaire d'une grille de nœuds (bords compris) vers n × n nœuds."""
    rows, cols = grid.shape
    if (rows, cols) == (n, n):
        return grid.astype(np.float64)
    y = np.linspace(0, rows - 1, n)
    x = np.linspace(0, cols - 1, n)
    r0, c0 = np.floor(y).astype(int), np.floor(x).astype(int)
    r1, c1 = np.minimum(r0 + 1, rows - 1), np.minimum(c0 + 1, cols - 1)
    fy, fx = (y - r0)[:, None], (x - c0)[None, :]
    d = grid.astype(np.float64)
    top = d[np.ix_(r0, c0)] * (1 - fx) + d[np.ix_(r0, c1)] * fx
    bottom = d[np.ix_(r1, c0)] * (1 - fx) + d[np.ix_(r1, c1)] * fx
    return top * (1 - fy) + bottom * fy


def is_hd(path):
    return Path(path).suffix.lower() == ".dt2"


def convert(path, sdf_dir, hd=None):
    """Convertit une tuile ; `hd` None : d'après le niveau (HD pour le .dt2). Renvoie
    (fichier SDF, nombre de trous comblés)."""
    tile, grid, void = read_dted(path)
    if hd is None:
        hd = is_hd(path)
    if void.any():
        grid = dem.fill_voids(grid, void)
    grid = resample(grid, dem.nodes(hd))
    target = terrain.sdf_file(sdf_dir, tile, hd)
    dem.write_sdf(target, grid, tile, hd)
    return target, int(void.sum())


def find_files(inputs):
    """Fichiers DTED désignés directement ou contenus dans des dossiers (récursivement)."""
    files = []
    for item in map(Path, inputs):
        if item.is_dir():
            files += sorted(p for p in item.rglob("*") if p.suffix.lower() in SUFFIXES)
        else:
            files.append(item)
    return files


def convert_files(files, sdf_dir, hd=None, log=print, cancel=lambda: False, progress=lambda n, total: None):
    """Convertit une liste de tuiles DTED ; résumé {"converted": [...], "failed": [...]}."""
    Path(sdf_dir).mkdir(parents=True, exist_ok=True)
    summary = {"converted": [], "failed": []}
    progress(0, len(files))
    for i, path in enumerate(files, 1):
        if cancel():
            raise terrain.Cancelled()
        try:
            target, voids = convert(path, sdf_dir, hd)
            summary["converted"].append(target)
            log(tr("{src} → {sdf}", src=Path(path).name, sdf=target.name)
                + (tr(" ({n} trous comblés)", n=voids) if voids else "") + "\n")
        except (OSError, ValueError) as exc:
            summary["failed"].append(path)
            log(tr("Échec de {src} : {exc}\n", src=Path(path).name, exc=exc))
        progress(i, len(files))
    return summary
