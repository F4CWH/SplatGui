"""Logique SPLAT! indépendante de l'interface : fichiers .qth/.lrp, ligne de commande,
préparation d'un dossier d'exécution et classement des fichiers produits.

Convention des longitudes : l'interface utilise la convention usuelle (Est positif,
Ouest négatif). SPLAT! attend des degrés Ouest positifs : on inverse le signe à
l'écriture et à la lecture des fichiers .qth.
"""

import datetime
import os
import re
import shlex
import struct
import unicodedata
from pathlib import Path

from . import antennas, dem, terrain
from .storage import BIN_DIR, PROJECT_DIR
from .i18n import N_, tr

ARCHES = {"x64": N_("x64 (64 bits)"), "x86": N_("x86 (32 bits)")}
VARIANTS = {"standard": N_("Standard (3\" d'arc)"), "hd": N_("HD (1\" d'arc)")}
MODES = {
    "p2p": N_("Point à point"),
    "coverage": N_("Couverture LOS (-c)"),
    "pathloss": N_("Perte de trajet / champ (-L)"),
}
CLIMATES = {
    1: N_("1 – Équatorial"),
    2: N_("2 – Continental subtropical"),
    3: N_("3 – Maritime subtropical"),
    4: N_("4 – Désertique"),
    5: N_("5 – Continental tempéré"),
    6: N_("6 – Maritime tempéré (terre)"),
    7: N_("7 – Maritime tempéré (mer)"),
}
GROUNDS = {
    N_("Eau salée"): (80, 5.0),
    N_("Bon sol"): (25, 0.020),
    N_("Eau douce"): (80, 0.010),
    N_("Marécage"): (12, 0.007),
    N_("Terres agricoles, forêt"): (15, 0.005),
    N_("Sol moyen"): (15, 0.005),
    N_("Montagne, sable"): (13, 0.002),
    N_("Ville"): (5, 0.001),
    N_("Sol pauvre"): (4, 0.001),
}
GRAPHS = {
    "p": N_("Profil du terrain (-p)"),
    "e": N_("Élévation (-e)"),
    "h": N_("Hauteur (-h)"),
    "H": N_("Hauteur normalisée (-H)"),
    "l": N_("Perte de trajet (-l)"),
}
VALUE_OPTIONS = {
    "m": N_("Multiplicateur du rayon terrestre (-m)"),
    "R": N_("Portée max. -c/-L (-R) [km/mi]"),
    "f": N_("Fréquence zone de Fresnel (-f) [MHz]"),
    "fz": N_("Dégagement Fresnel (-fz) [%]"),
    "gc": N_("Hauteur du sursol / clutter (-gc) [m/ft]"),
    "erp": N_("Forcer la PAR (-erp) [W]"),
    "db": N_("Seuil des contours (-db) [dB]"),
}
FLAG_OPTIONS = {
    "n": N_("Ne pas tracer les trajets LOS sur la carte (-n)"),
    "N": N_("Pas de rapports de site / obstacles (-N)"),
    "sc": N_("Contours lissés (-sc)"),
    "nf": N_("Pas de zones de Fresnel sur les graphes (-nf)"),
    "ngs": N_("Topographie en blanc (-ngs)"),
    "kml": N_("Sortie Google Earth .kml (-kml)"),
    "geo": N_("Fichier de géoréférence Xastir .geo (-geo)"),
    "dbm": N_("Puissance reçue en dBm (-dbm)"),
    "olditm": N_("Modèle Longley-Rice au lieu d'ITWOM (-olditm)"),
    "gpsav": N_("Conserver les fichiers gnuplot (-gpsav)"),
}

IMAGE_EXT = {".ppm", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg"}
TEXT_EXT = {".txt", ".lrp", ".qth", ".scf", ".dcf", ".lcf", ".az", ".el", ".kml",
            ".geo", ".gp", ".dat", ".log", ".cmd", ".pgw"}

# Codes de retour Windows typiques d'un exécutable qui ne peut pas démarrer.
WIN_LOAD_ERRORS = {
    0xC0000135: N_("DLL introuvable (STATUS_DLL_NOT_FOUND)"),
    0xC0000139: N_("point d'entrée introuvable dans une DLL (STATUS_ENTRYPOINT_NOT_FOUND)"),
    0xC000007B: N_("image invalide / mélange 32-64 bits (STATUS_INVALID_IMAGE_FORMAT)"),
    0xC0000142: N_("échec d'initialisation d'une DLL (STATUS_DLL_INIT_FAILED)"),
}


class ParamError(ValueError):
    """Paramètres incohérents détectés avant le lancement."""


def executable_path(arch, variant):
    name = "splat-hd.exe" if variant == "hd" else "splat.exe"
    return BIN_DIR / arch / name


TOOLS_DIR = PROJECT_DIR / "tools"
STACK_RESERVE = 256 * 1024 * 1024


def set_stack_reserve(data, size):
    """Modifie SizeOfStackReserve dans l'en-tête PE (PE32 ou PE32+)."""
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("en-tête PE introuvable")
    optional = pe + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    if magic == 0x20B:      # PE32+ (64 bits)
        struct.pack_into("<Q", data, optional + 72, size)
    elif magic == 0x10B:    # PE32 (32 bits)
        struct.pack_into("<I", data, optional + 72, size)
    else:
        raise ValueError("format PE inconnu")


def runnable_executable(exe):
    """Copie de l'exécutable avec une pile de 256 Mo.

    Les binaires fournis réservent 2 Mo de pile, ce qui provoque un débordement (SIGSEGV)
    pendant l'analyse point à point. Les originaux de bin/ ne sont pas modifiés ; la copie
    est régénérée si l'original change."""
    exe = Path(exe)
    target = TOOLS_DIR / exe.parent.name / exe.name
    try:
        if not target.exists() or target.stat().st_mtime < exe.stat().st_mtime:
            data = bytearray(exe.read_bytes())
            set_stack_reserve(data, STACK_RESERVE)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
        return target
    except (OSError, ValueError, struct.error):
        return exe


def available_arches():
    return [a for a in ARCHES if executable_path(a, "standard").exists()
            or executable_path(a, "hd").exists()]


# Les exécutables MSYS/Cygwin tués par un signal sortent avec le code (signal << 8).
MSYS_SIGNALS = {4: "SIGILL", 6: "SIGABRT", 7: "SIGBUS", 8: "SIGFPE", 11: "SIGSEGV"}


def exit_code_problem(code):
    """Problème de chargement (DLL…) : l'exécutable n'a pas pu démarrer. Texte non traduit."""
    return WIN_LOAD_ERRORS.get(code & 0xFFFFFFFF)


def describe_exit_code(code):
    """Problème de chargement (DLL…), traduit."""
    message = exit_code_problem(code)
    return tr(message) if message else None


def crash_signal(code):
    """Signal ayant arrêté SPLAT! en cours d'exécution, d'après la convention MSYS, ou None."""
    if code > 0 and code & 0xFF == 0 and (code >> 8) in MSYS_SIGNALS:
        return MSYS_SIGNALS[code >> 8]
    return None


# --- Fichiers de site ----------------------------------------------------------

def parse_angle(text):
    """Convertit '74.6864', '-74.6864' ou '74 41 11.0' (DMS) en degrés décimaux."""
    parts = text.replace(",", ".").split()
    if not parts:
        raise ValueError(tr("angle vide"))
    negative = parts[0].startswith("-")
    values = [abs(float(p)) for p in parts[:3]]
    deg = values[0] + (values[1] / 60 if len(values) > 1 else 0) + (values[2] / 3600 if len(values) > 2 else 0)
    return -deg if negative else deg


def read_qth(path):
    """Lit un fichier .qth et renvoie un site (longitude convertie en Est positif)."""
    with open(path, "r", encoding="latin-1") as fh:
        lines = [line.strip() for line in fh if line.strip()]
    if len(lines) < 4:
        raise ValueError(tr("{path} : fichier .qth incomplet", path=path))
    match = re.match(r"([-+]?[\d.,]+)\s*(.*)", lines[3])
    if not match:
        raise ValueError(tr("{path} : hauteur d'antenne illisible", path=path))
    unit = "m" if match.group(2).lower().startswith("m") else "ft"
    lon = -parse_angle(lines[2])
    if lon <= -180:
        lon += 360
    elif lon > 180:
        lon -= 360
    return {
        "name": lines[0],
        "lat": round(parse_angle(lines[1]), 6),
        "lon": round(lon, 6),
        "height": float(match.group(1).replace(",", ".")),
        "height_unit": unit,
    }


def qth_text(site):
    unit = " meters" if site.get("height_unit", "m") == "m" else ""
    return (f"{site['name']}\n{float(site['lat']):.6f}\n{-float(site['lon']):.6f}\n"
            f"{float(site['height']):.2f}{unit}\n")


def read_lrp(path):
    """Lit un fichier .lrp et renvoie le dictionnaire 'lrp' des paramètres."""
    keys = ["dielectric", "conductivity", "bending", "frequency", "climate",
            "polarization", "frac_situations", "frac_time", "erp"]
    values = []
    with open(path, "r", encoding="latin-1") as fh:
        for line in fh:
            token = line.split(";")[0].strip()
            if not token:
                continue
            try:
                values.append(float(token.split()[0]))
            except ValueError:
                break  # texte libre après les paramètres
            if len(values) == len(keys):
                break
    if len(values) < 8:
        raise ValueError(tr("{path} : fichier .lrp incomplet", path=path))
    lrp = dict(zip(keys, values))
    lrp["climate"] = int(lrp["climate"])
    lrp["polarization"] = int(lrp["polarization"])
    lrp.setdefault("erp", 0.0)
    lrp["enabled"] = True
    return lrp


def lrp_text(lrp):
    text = (
        f"{float(lrp['dielectric']):.3f}\t; Earth Dielectric Constant (Relative permittivity)\n"
        f"{float(lrp['conductivity']):.3f}\t; Earth Conductivity (Siemens per meter)\n"
        f"{float(lrp['bending']):.3f}\t; Atmospheric Bending Constant (N-Units)\n"
        f"{float(lrp['frequency']):.3f}\t; Frequency in MHz (20 MHz to 20 GHz)\n"
        f"{int(lrp['climate'])}\t; Radio Climate\n"
        f"{int(lrp['polarization'])}\t; Polarization (0 = Horizontal, 1 = Vertical)\n"
        f"{float(lrp['frac_situations']):.2f}\t; Fraction of situations\n"
        f"{float(lrp['frac_time']):.2f}\t; Fraction of time\n"
    )
    if float(lrp.get("erp") or 0) > 0:
        text += f"{float(lrp['erp']):.3f}\t; ERP in watts\n"
    return text


def safe_filename(name):
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._") or "site"


# --- Ligne de commande ---------------------------------------------------------

def validate(params):
    mode = params["mode"]
    tx = params["tx_sites"]
    if not tx:
        raise ParamError(tr("Ajoutez au moins un site émetteur."))
    if mode == "coverage" and len(tx) > 4:
        raise ParamError(tr("Le mode couverture (-c) accepte au maximum 4 émetteurs."))
    if mode == "pathloss" and len(tx) > 30:
        raise ParamError(tr("Le mode perte de trajet (-L) accepte au maximum 30 émetteurs."))
    if mode == "p2p" and not params["rx_site"].get("name"):
        raise ParamError(tr("Le mode point à point nécessite un site récepteur."))
    for site in tx + ([params["rx_site"]] if mode == "p2p" else []):
        if not str(site.get("name", "")).strip():
            raise ParamError(tr("Chaque site doit avoir un nom."))
        if not -90 <= float(site["lat"]) <= 90:
            raise ParamError(tr("Latitude invalide pour {name}.", name=site["name"]))
        if not -360 <= float(site["lon"]) <= 360:
            raise ParamError(tr("Longitude invalide pour {name}.", name=site["name"]))
    for key, value in params["values"].items():
        if str(value).strip():
            try:
                float(str(value).replace(",", "."))
            except ValueError:
                raise ParamError(tr("Valeur non numérique pour -{key} : {value!r}", key=key, value=value)) from None
    if len(params["city_files"]) > 5 or len(params["boundary_files"]) > 5:
        raise ParamError(tr("5 fichiers de villes et 5 fichiers de limites au maximum."))
    if params["mode"] != "p2p" and not params["map_enabled"]:
        raise ParamError(tr("Les modes de couverture nécessitent la génération d'une carte (-o)."))


def _fwd(path):
    return str(path).replace("\\", "/")


def effective_sdf_dir(params):
    """Dossier SDF passé à -d : celui choisi par l'utilisateur, sinon celui de l'application
    lorsque la gestion automatique du relief est active (sinon aucun). Les SDF produits par
    dem.py (autre source que le SRTM, ou sursol) ont un dossier par configuration."""
    if params["sdf_dir"].strip():
        return params["sdf_dir"].strip()
    if params.get("auto_terrain"):
        return str(dem.sdf_dir_for(params) if dem.uses_dem(params) else terrain.SDF_DIR)
    return ""


def build_arguments(params, tx_files, rx_file):
    """Construit la liste d'arguments SPLAT! (sans l'exécutable)."""
    args = ["-t", *tx_files]
    mode = params["mode"]
    if mode == "p2p" and rx_file:
        args += ["-r", rx_file]
    elif mode == "coverage":
        args += ["-c", f"{float(params['rx_height']):g}"]
    elif mode == "pathloss":
        args += ["-L", f"{float(params['rx_height']):g}"]

    if params["metric"]:
        args.append("-metric")
    for key, value in params["values"].items():
        value = str(value).strip().replace(",", ".")
        if value:
            args += [f"-{key}", value]
    for key, enabled in params["flags"].items():
        if enabled:
            args.append(f"-{key}")

    if params["map_enabled"] and params["map_name"].strip():
        args += ["-o", safe_filename(params["map_name"].strip().removesuffix(".ppm")) + ".ppm"]
        # Calage de la carte (.geo) pour la superposition des fonds OSM / IGN.
        # Avec -kml, SPLAT! n'écrit pas de .geo : le calage est alors lu dans le .kml.
        if not params["flags"].get("geo") and not params["flags"].get("kml"):
            args.append("-geo")
    if mode == "p2p":
        ext = params.get("graph_format", "png")
        for key, enabled in params["graphs"].items():
            if enabled:
                suffix = "hauteur_norm" if key == "H" else {
                    "p": "profil", "e": "elevation", "h": "hauteur", "l": "perte"}[key]
                args += [f"-{key}", f"{suffix}.{ext}"]

    sdf_dir = effective_sdf_dir(params)
    if sdf_dir:
        args += ["-d", _fwd(sdf_dir)]
    if params["city_files"]:
        args += ["-s", *[_fwd(f) for f in params["city_files"]]]
    if params["boundary_files"]:
        args += ["-b", *[_fwd(f) for f in params["boundary_files"]]]
    for key in ("udt", "ani"):
        if params[key].strip():
            args += [f"-{key}", _fwd(params[key].strip())]
    if params["ano"].strip():
        args += ["-ano", safe_filename(params["ano"].strip())]
    if params["log"]:
        args += ["-log", "commande.log"]
    if params["extra_args"].strip():
        args += shlex.split(params["extra_args"], posix=False)
    return args


def site_file_bases(params):
    """Noms de base (sans extension) des fichiers de site : (émetteurs, récepteur ou None).
    Deux sites de même nom reçoivent des noms distincts. Sans point : SPLAT! cherche les
    .lrp / .az / .el d'un site en coupant le nom du .qth au premier point."""
    used = set()

    def base_for(site, prefix):
        base = f"{prefix}_{safe_filename(site['name']).replace('.', '_')}"
        while base.lower() in used:
            base += "_"
        used.add(base.lower())
        return base

    tx = [base_for(site, f"tx{index}") for index, site in enumerate(params["tx_sites"], start=1)]
    rx = base_for(params["rx_site"], "rx") if params["mode"] == "p2p" else None
    return tx, rx


def prepare_run(params, settings, profile_name):
    """Crée le dossier d'exécution, y écrit les .qth/.lrp et renvoie
    (dossier, exécutable, arguments, environnement)."""
    validate(params)
    exe = executable_path(params["arch"], params["variant"])
    if not exe.exists():
        raise ParamError(tr("Exécutable introuvable : {exe}", exe=exe))
    exe = runnable_executable(exe)

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path(settings["runs_dir"]) / f"{stamp}_{safe_filename(profile_name)}"
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = run_dir.with_name(f"{stamp}_{safe_filename(profile_name)}_{suffix}")
    run_dir.mkdir(parents=True)

    tx_bases, rx_base = site_file_bases(params)
    tx_files = []
    for base, site in zip(tx_bases, params["tx_sites"]):
        (run_dir / f"{base}.qth").write_text(qth_text(site), encoding="latin-1", errors="replace")
        if params["lrp"]["enabled"]:
            (run_dir / f"{base}.lrp").write_text(lrp_text(params["lrp"]), encoding="ascii")
        if site.get("antenna"):
            pattern = antennas.load(site["antenna"])
            if pattern is None:
                raise ParamError(tr("Antenne « {antenna} » introuvable dans la bibliothèque (émetteur {name}).",
                                    antenna=site["antenna"], name=site["name"]))
            # Diagramme orienté (rotation = azimut du lobe) et incliné : <base>.az / <base>.el
            antennas.write_splat_files(pattern, run_dir / base, float(site.get("azimuth", 0)),
                                       float(site.get("tilt", 0)))
        tx_files.append(f"{base}.qth")
    rx_file = None
    if rx_base:
        (run_dir / f"{rx_base}.qth").write_text(qth_text(params["rx_site"]), encoding="latin-1", errors="replace")
        rx_file = f"{rx_base}.qth"

    args = build_arguments(params, tx_files, rx_file)

    env = os.environ.copy()
    extra = settings.get("extra_paths", {}).get(params["arch"], "")
    if extra.strip():
        env["PATH"] = extra.strip().rstrip(";") + os.pathsep + env.get("PATH", "")

    (run_dir / "commande.cmd").write_text(
        f'@echo off\r\nset "PATH={extra.strip().rstrip(";")};%PATH%"\r\ncd /d "%~dp0"\r\n'
        f'"{exe}" {" ".join(_quote(a) for a in args)}\r\n',
        encoding="utf-8",
    )
    return run_dir, exe, args, env


def _quote(arg):
    return f'"{arg}"' if (" " in arg or not arg) else arg


def command_preview(params):
    """Ligne de commande indicative, pour l'aperçu dans l'interface."""
    exe = executable_path(params["arch"], params["variant"]).name
    try:
        tx_bases, rx_base = site_file_bases(params)
        args = build_arguments(params, [f"{b}.qth" for b in tx_bases], rx_base and f"{rx_base}.qth")
    except (ValueError, KeyError) as exc:
        return tr("(paramètres invalides : {exc})", exc=exc)
    return " ".join([exe, *(_quote(a) for a in args)])


# --- Résultats -----------------------------------------------------------------

INPUT_FILES = {"commande.cmd"}


def collect_outputs(run_dir):
    """Classe les fichiers d'un dossier d'exécution en (images, textes, autres)."""
    images, texts, others = [], [], []
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        return images, texts, others
    for path in sorted(run_dir.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file() or path.name in INPUT_FILES:
            continue
        ext = path.suffix.lower()
        if ext in IMAGE_EXT:
            images.append(path)
        elif ext in TEXT_EXT:
            texts.append(path)
        else:
            others.append(path)
    # Carte principale avant sa légende (-ck), rapports SPLAT! avant les fichiers d'entrée.
    images.sort(key=lambda p: (p.stem.endswith("-ck"), p.name.lower()))

    texts.sort(key=lambda p: (p.suffix.lower() in {".qth", ".lrp"}, p.name.lower()))
    return images, texts, others
