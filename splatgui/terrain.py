"""Relief : téléchargement des tuiles SRTM et conversion en fichiers SDF avec srtm2sdf.

Organisation (sous-répertoire de l'application ; tools/ contient la copie
srtm2sdf-hd.exe, car srtm2sdf choisit le mode HD d'après son propre nom) :
    terrain/srtm/   tuiles SRTM téléchargées ou déposées à la main (N48E002.hgt[.gz|.zip])
    terrain/sdf/    fichiers SDF produits par srtm2sdf (standard et -hd)

Les tuiles téléchargées sont en SRTM 1" (3601 x 3601). Pour SPLAT! standard, elles sont
réduites en 3" (1201 x 1201) avant conversion ; pour SPLAT! HD elles sont utilisées telles quelles.
"""

import array
import gzip
import math
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from .storage import BIN_DIR, PROJECT_DIR
from .i18n import tr

TERRAIN_DIR = PROJECT_DIR / "terrain"
SRTM_DIR = TERRAIN_DIR / "srtm"
SDF_DIR = TERRAIN_DIR / "sdf"
TOOLS_DIR = PROJECT_DIR / "tools"
WORK_DIR = TERRAIN_DIR / "tmp"

DEFAULT_URL = "https://s3.amazonaws.com/elevation-tiles-prod/skadi/{lat_dir}/{tile}.hgt.gz"
MAX_AUTO_TILES = 64

SRTM1_SIZE = 3601 * 3601 * 2
SRTM3_SIZE = 1201 * 1201 * 2

# Message de SPLAT! lorsqu'une tuile SDF est absente.
MISSING_RE = re.compile(r'Region\s+"(-?\d+)[_:](-?\d+)[_:](\d+)[_:](\d+)(?:-hd)?"\s+assumed as sea-level')

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Cancelled(Exception):
    pass


# --- Nommage des tuiles --------------------------------------------------------
# Une tuile est identifiée par (lat, lon) : coin sud-ouest, longitude Est positive.

def tile_name(lat, lon):
    return f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}"


def region_name(lat, lon, hd=False):
    """Nom SPLAT! de la région : lat_lat+1_ouestmin_ouestmax (degrés Ouest, 0-360)."""
    west_min = (-(lon + 1)) % 360
    return f"{lat}_{lat + 1}_{west_min}_{west_min + 1}" + ("-hd" if hd else "")


def tile_from_region(min_lat, min_west):
    lon = -(min_west + 1)
    if lon < -180:
        lon += 360
    return min_lat, lon


def sdf_file(sdf_dir, tile, hd):
    return Path(sdf_dir) / f"{region_name(*tile, hd)}.sdf"


def sdf_exists(sdf_dir, tile, hd):
    path = sdf_file(sdf_dir, tile, hd)
    # Tuile E000 : « …_359_360 » ou « …_359_0 » selon la convention de l'outil.
    return path.exists() or Path(str(path).replace("_359_360", "_359_0")).exists()


# Le portage Windows de splat-hd.exe cherche « 48:49:357:358-hd.sdf » (le format standard a été
# corrigé en « _ », pas le format HD) ; sous MSYS, le « : » d'un nom de fichier devient U+F03A.
MSYS_COLON = ""


def hd_alias(path):
    """Nom sous lequel splat-hd.exe cherche une tuile HD « …-hd.sdf »."""
    path = Path(path)
    return path.with_name(path.name.replace("_", MSYS_COLON))


def link_hd_aliases(folder):
    """Donne à chaque tuile « *-hd.sdf » de `folder` le nom attendu par splat-hd.exe (lien
    physique, sinon copie). Renvoie les erreurs (liste de messages)."""
    try:
        paths = [p for p in Path(folder).glob("*-hd.sdf") if "_" in p.name]
    except OSError:
        return []
    errors = []
    for path in paths:
        alias = hd_alias(path)
        try:
            if alias.exists():
                if os.path.samefile(path, alias):
                    continue
                source, target = path.stat(), alias.stat()
                if target.st_size == source.st_size and target.st_mtime >= source.st_mtime:
                    continue        # copie à jour
                alias.unlink()      # tuile réécrite (os.replace) : l'ancien lien pointe sur l'ancienne
            try:
                os.link(path, alias)
            except OSError:
                shutil.copy2(path, alias)
        except OSError as exc:
            errors.append(f"{alias.name} : {exc}")
    return errors


def missing_from_output(text):
    """Tuiles signalées comme absentes (« assumed as sea-level ») dans la sortie de SPLAT!."""
    return sorted({tile_from_region(int(m.group(1)), int(m.group(3))) for m in MISSING_RE.finditer(text)})


# --- Estimation des tuiles nécessaires -------------------------------------------

def _range_km(params):
    value = str(params["values"].get("R", "")).strip().replace(",", ".")
    if not value:
        return None
    try:
        distance = float(value)
    except ValueError:
        return None
    return distance if params["metric"] else distance * 1.609344


def tiles_for_params(params):
    """Tuiles couvrant les sites et, si la portée -R est donnée, la zone d'analyse.
    Sans -R en mode couverture, les tuiles manquantes sont détectées après une première exécution."""
    sites = list(params["tx_sites"])
    if params["mode"] == "p2p":
        sites.append(params["rx_site"])
    tiles = set()
    lats = [float(s["lat"]) for s in sites]
    lons = [float(s["lon"]) for s in sites]
    if params["mode"] == "p2p":
        boxes = [(min(lats), max(lats), min(lons), max(lons))]
    else:
        margin = _range_km(params) or 0.0
        boxes = []
        for lat, lon in zip(lats, lons):
            dlat = margin / 111.0
            dlon = margin / (111.0 * max(math.cos(math.radians(lat)), 0.05))
            boxes.append((lat - dlat, lat + dlat, lon - dlon, lon + dlon))
    for lat_min, lat_max, lon_min, lon_max in boxes:
        for lat in range(math.floor(max(lat_min, -90)), math.floor(min(lat_max, 89.999)) + 1):
            for lon in range(math.floor(lon_min), math.floor(lon_max) + 1):
                lon_norm = ((lon + 180) % 360) - 180
                tiles.add((lat, lon_norm))
    return sorted(tiles)


# --- Fichiers SRTM -------------------------------------------------------------

def _unavailable_marker(tile):
    return SRTM_DIR / f"{tile_name(*tile)}.absent"


ABSENT_MAX_AGE = 7 * 86400


def marker_valid(path):
    """Marqueur « pas de données » encore valable. Au-delà d'une semaine la tuile est redemandée :
    un refus passager du serveur ne doit pas imposer le niveau de la mer définitivement."""
    try:
        return time.time() - Path(path).stat().st_mtime < ABSENT_MAX_AGE
    except OSError:
        return False


def run_cancellable(args, cancel, timeout, **kwargs):
    """Comme subprocess.run (sorties fusionnées), mais interrompu dès que `cancel()` est vrai.
    Renvoie (code de retour, sortie en octets) ; lève Cancelled ou subprocess.TimeoutExpired."""
    with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          creationflags=CREATE_NO_WINDOW, **kwargs) as proc:
        deadline = time.monotonic() + timeout
        while True:
            try:
                output, _ = proc.communicate(timeout=0.5)
                return proc.returncode, output
            except subprocess.TimeoutExpired:
                stop = cancel()
                if stop or time.monotonic() > deadline:
                    proc.kill()
                    proc.communicate()
                    if stop:
                        raise Cancelled() from None
                    raise subprocess.TimeoutExpired(args, timeout) from None


def find_srtm(tile):
    for suffix in (".hgt", ".hgt.gz", ".hgt.zip", ".zip"):
        path = SRTM_DIR / f"{tile_name(*tile)}{suffix}"
        if path.exists():
            return path
    return None


def read_hgt(path):
    """Renvoie le contenu brut (big-endian int16) d'un fichier .hgt, .hgt.gz ou .zip."""
    path = Path(path)
    name = path.name.lower()
    if name.endswith(".gz"):
        with gzip.open(path, "rb") as fh:
            return fh.read()
    if name.endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            member = next(n for n in zf.namelist() if n.lower().endswith(".hgt"))
            return zf.read(member)
    return path.read_bytes()


def decimate_to_srtm3(data):
    """Réduit une tuile SRTM 1" (3601²) en SRTM 3" (1201²) en gardant un point sur trois."""
    samples = array.array("h")
    samples.frombytes(data)
    out = array.array("h")
    for row in range(0, 3601, 3):
        out.extend(samples[row * 3601:(row + 1) * 3601:3])
    return out.tobytes()


def download(tile, url_template, log, cancel):
    """Télécharge une tuile SRTM dans terrain/srtm. Renvoie le chemin, ou None si inexistante."""
    name = tile_name(*tile)
    url = url_template.format(tile=name, lat_dir=name[:3], lat=tile[0], lon=tile[1])
    target = SRTM_DIR / (name + (".hgt.gz" if url.endswith(".gz") else ".zip" if url.endswith(".zip") else ".hgt"))
    partial = target.with_name(target.name + ".part")
    log("  " + tr("Téléchargement de {name} : {url}", name=name, url=url) + "\n")
    try:
        with urllib.request.urlopen(url, timeout=60) as response, open(partial, "wb") as fh:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            next_report = 0.25
            while chunk := response.read(256 * 1024):
                if cancel():
                    raise Cancelled()
                fh.write(chunk)
                done += len(chunk)
                if total and done / total >= next_report:
                    log(f"    {done / total:.0%} ({done / 1e6:.1f} Mo)\n")
                    next_report += 0.25
    except urllib.error.HTTPError as exc:
        partial.unlink(missing_ok=True)
        if exc.code in (403, 404):
            _unavailable_marker(tile).write_text(url, encoding="utf-8")
            log("    " + tr("Aucune donnée SRTM pour {name} (HTTP {code}).", name=name, code=exc.code) + "\n")
            return None
        raise
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    os.replace(partial, target)
    return target


# --- Conversion ----------------------------------------------------------------

def converter_candidates(arch, extra_paths, hd):
    """Exécutables srtm2sdf utilisables, par ordre de préférence : (exe, env)."""
    order = [arch] + [a for a in ("x64", "x86") if a != arch]
    result = []
    for candidate_arch in order:
        utils = BIN_DIR / candidate_arch / "utils"
        exe = utils / ("srtm2sdf-hd.exe" if hd else "srtm2sdf.exe")
        if hd and not exe.exists() and (utils / "srtm2sdf.exe").exists():
            # srtm2sdf choisit le mode HD d'après son propre nom : on crée une copie renommée.
            exe = TOOLS_DIR / candidate_arch / "srtm2sdf-hd.exe"
            if not exe.exists():
                exe.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(utils / "srtm2sdf.exe", exe)
        if not exe.exists():
            continue
        env = os.environ.copy()
        extra = (extra_paths or {}).get(candidate_arch, "").strip().rstrip(";")
        if extra:
            env["PATH"] = extra + os.pathsep + env.get("PATH", "")
        result.append((exe, env))
    return result


def convert(tile, srtm_path, hd, sdf_dir, converters, log, cancel):
    """Convertit une tuile SRTM en SDF. Renvoie le chemin du SDF produit."""
    name = tile_name(*tile)
    data = read_hgt(srtm_path)
    if hd:
        if len(data) != SRTM1_SIZE:
            raise RuntimeError(tr("{name} : SPLAT! HD nécessite une tuile SRTM 1\" (3601×3601).", name=name))
    elif len(data) == SRTM1_SIZE:
        data = decimate_to_srtm3(data)
    elif len(data) != SRTM3_SIZE:
        raise RuntimeError(tr("{name} : taille de tuile SRTM inattendue ({size} octets).", name=name, size=len(data)))

    work = WORK_DIR / name
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        (work / f"{name}.hgt").write_bytes(data)
        errors = []
        for exe, env in converters:
            if cancel():
                raise Cancelled()
            label = exe.relative_to(PROJECT_DIR) if exe.is_relative_to(PROJECT_DIR) else exe
            log("  " + tr("Conversion de {name} avec {exe}", name=name, exe=label)
                + (tr(" (environ une minute en HD)") if hd else "") + "…\n")
            try:
                returncode, output = run_cancellable([str(exe), "-d", "/dev/null", f"{name}.hgt"], cancel,
                                                     900, cwd=work, env=env)
            except (OSError, subprocess.TimeoutExpired) as exc:
                errors.append(f"{label} : {exc}")
                continue
            produced = [p for p in work.iterdir() if p.suffix.lower() == ".sdf"]
            if returncode == 0 and produced:
                Path(sdf_dir).mkdir(parents=True, exist_ok=True)
                target = None
                for path in produced:
                    # Les outils MSYS écrivent « 48:49:357:358.sdf » ; sous Windows le « : »
                    # devient U+F03A. SPLAT! cherche « 48_49_357_358.sdf ».
                    clean = path.name.replace("", "_").replace(":", "_")
                    target = Path(sdf_dir) / clean
                    shutil.move(path, target)          # dossier SDF éventuellement sur un autre lecteur
                return target
            output = output.decode("latin-1", "replace").strip()
            errors.append(f"{label} : code {returncode:#x} {output[-200:]}")
        raise RuntimeError(tr("Échec de la conversion de {name} :", name=name) + "\n    "
                           + "\n    ".join(errors or [tr("aucun srtm2sdf")]))
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --- Occupation du disque ---------------------------------------------------------

SRTM_SUFFIXES = (".hgt", ".hgt.gz", ".zip")


def folder_usage(folder, suffixes):
    """(nombre, taille totale en octets) des fichiers de `folder` (sans sous-dossiers) dont le
    nom se termine par l'un des `suffixes` ; (0, 0) si le dossier n'existe pas."""
    count = size = 0
    seen = set()            # liens physiques (alias des tuiles HD) comptés une seule fois
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return 0, 0
    for entry in entries:
        try:
            if entry.is_file() and entry.name.lower().endswith(suffixes):
                stat = os.stat(entry.path)
                if stat.st_ino and (stat.st_dev, stat.st_ino) in seen:
                    continue
                seen.add((stat.st_dev, stat.st_ino))
                count += 1
                size += stat.st_size
        except OSError:
            continue
    return count, size


def disk_space(folder):
    """(racine du lecteur, octets libres, octets au total) du lecteur de `folder` (premier
    dossier parent existant), ou None."""
    path = Path(folder).resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return path.anchor or str(path), usage.free, usage.total


def ensure_tiles(tiles, hd, sdf_dir, converters, url_template, log, cancel=lambda: False,
                 retry_unavailable=False, progress=lambda done, total: None):
    """Garantit la présence des SDF des tuiles demandées. Renvoie un résumé (dict de listes).
    `progress(n, total)` est appelé avant chaque tuile puis à la fin."""
    for directory in (SRTM_DIR, Path(sdf_dir)):
        directory.mkdir(parents=True, exist_ok=True)
    summary = {"present": [], "converted": [], "unavailable": [], "failed": []}
    for index, tile in enumerate(tiles):
        progress(index, len(tiles))
        if cancel():
            raise Cancelled()
        name = tile_name(*tile)
        if sdf_exists(sdf_dir, tile, hd):
            summary["present"].append(name)
            continue
        srtm = find_srtm(tile)
        if hd and srtm is not None and len(read_hgt(srtm)) != SRTM1_SIZE:
            srtm = None  # tuile 3" déposée à la main : inutilisable en HD
        if srtm is None:
            if marker_valid(_unavailable_marker(tile)) and not retry_unavailable:
                summary["unavailable"].append(name)
                continue
            try:
                srtm = download(tile, url_template, log, cancel)
            except (OSError, urllib.error.URLError) as exc:
                log("  " + tr("Téléchargement de {name} impossible : {exc}", name=name, exc=exc) + "\n")
                summary["failed"].append(name)
                continue
            if srtm is None:
                summary["unavailable"].append(name)
                continue
        try:
            path = convert(tile, srtm, hd, sdf_dir, converters, log, cancel)
            log(f"  → {path.name}\n")
            summary["converted"].append(name)
        except (RuntimeError, OSError, ValueError, StopIteration) as exc:
            log(f"  {exc}\n")
            summary["failed"].append(name)
    progress(len(tiles), len(tiles))
    return summary
