"""Persistance : profils, réglages de l'application et historique des exécutions.

Tout est stocké en JSON dans %APPDATA%\\SplatGui (ou ~/.splatgui hors Windows).
"""

import configparser
import copy
import json
import os
import re
import shutil
import sys
from pathlib import Path

from .i18n import tr

# Application compilée (PyInstaller) : données à côté de l'exécutable, pas dans lib/.
if getattr(sys, "frozen", False):
    PROJECT_DIR = Path(sys.executable).resolve().parent
else:
    PROJECT_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = PROJECT_DIR / "bin"

# Toutes les données de l'application sont dans son propre répertoire.
DATA_DIR = PROJECT_DIR
PROFILES_DIR = DATA_DIR / "profiles"
QTH_DIR = DATA_DIR / "qth"                             # sites importés / exportés (.qth) : défaut
LRP_DIR = DATA_DIR / "lrp"                             # paramètres ITM (.lrp) : défaut
INSTALL_FILE = DATA_DIR / "install.json"               # dossiers choisis à l'installation
FOLDER_SETTINGS = ("runs_dir", "qth_dir", "lrp_dir")
SETTINGS_FILE = DATA_DIR / "settings.json"             # paramètres de l'application
HISTORY_FILE = DATA_DIR / "history.json"

# Anciens emplacements, repris une fois s'ils existent (fichiers copiés, jamais supprimés).
if os.name == "nt" and os.environ.get("APPDATA"):
    LEGACY_DIR = Path(os.environ["APPDATA"]) / "SplatGui"
else:
    LEGACY_DIR = Path.home() / ".splatgui"
LEGACY_INI_FILE = PROJECT_DIR / "configuration.ini"
LEGACY_SETTINGS_FILE = LEGACY_DIR / "settings.json"
HISTORY_MAX = 100

DEFAULT_PROFILE = "Défaut"


def _default_extra_paths():
    """Dossiers ajoutés au PATH pour trouver les DLL et gnuplot, par architecture."""
    x64 = [p for p in (r"C:\msys64\usr\bin", r"C:\msys64\mingw64\bin") if Path(p).is_dir()]
    x86 = [p for p in (r"C:\msys64\mingw32\bin",) if Path(p).is_dir()]
    return {"x64": ";".join(x64), "x86": ";".join(x86)}


def default_site(name="SITE", lat=0.0, lon=0.0, height=10.0):
    return {"name": name, "lat": lat, "lon": lon, "height": height, "height_unit": "m",
            "antenna": "", "azimuth": 0.0, "tilt": 0.0}


def default_params():
    """Jeu de paramètres complet d'une exécution (contenu d'un profil)."""
    return {
        "arch": "x64",              # x64 | x86
        "variant": "standard",      # standard | hd
        "mode": "p2p",              # p2p | coverage | pathloss
        "metric": True,
        "rx_height": 10.0,          # hauteur RX pour -c / -L
        "tx_sites": [default_site("EMETTEUR", 48.8584, 2.2945, 30.0)],
        "rx_site": default_site("RECEPTEUR", 48.8049, 2.1204, 10.0),
        "lrp": {
            "enabled": True,
            "dielectric": 15.0,
            "conductivity": 0.005,
            "bending": 301.0,
            "frequency": 446.0,
            "climate": 6,
            "polarization": 1,
            "frac_situations": 0.5,
            "frac_time": 0.9,
            "erp": 5.0,
        },
        "values": {                 # options à valeur (vide = non transmise)
            "m": "", "R": "", "f": "", "fz": "", "gc": "", "erp": "", "db": "",
        },
        "flags": {                  # options booléennes
            "n": False, "N": False, "sc": False, "nf": False, "ngs": False,
            "kml": False, "geo": False, "dbm": False, "olditm": False, "gpsav": False,
        },
        "map_enabled": True,
        "map_name": "carte",
        "map_aspect_file": True,    # écrire aussi la carte aux bonnes proportions
        "map_aspect_format": "ppm", # ppm | png | ppm+png
        "graphs": {"p": False, "e": False, "h": False, "H": False, "l": False},
        "graph_format": "png",
        "sdf_dir": "",              # vide = dossier du relief de l'application
        "auto_terrain": True,       # télécharger/convertir les tuiles SRTM manquantes
        "city_files": [],
        "boundary_files": [],
        "udt": "",
        "ani": "",
        "ano": "",
        "log": True,
        "extra_args": "",
        "erp_calc": {               # calcul de la PAR (onglet Émetteurs)
            "power": 10.0,          # W
            "cable": "",            # cables.CABLES, "custom" ou "" (aucun)
            "attenuation": 10.0,    # dB/100 m (câble personnalisé)
            "length": 10.0,         # m
            "extra_loss": 0.5,      # dB (connecteurs…)
            "gain": 2.15,           # dBi
        },
    }


def merge_defaults(params):
    """Complète un jeu de paramètres (ancien profil, import) avec les valeurs par défaut."""
    base = default_params()

    def merge(dst, src):
        for key, value in src.items():
            if isinstance(dst.get(key), dict) and isinstance(value, dict):
                merge(dst[key], value)
            else:
                dst[key] = value

    merge(base, copy.deepcopy(params or {}))
    # Sites enregistrés avant l'ajout de champs (antenne, azimut, inclinaison…).
    for site in base["tx_sites"] + [base["rx_site"]]:
        for key, value in default_site().items():
            site.setdefault(key, value)
    return base


def default_settings():
    return {
        "last_profile": DEFAULT_PROFILE,
        "last_params": None,
        "extra_paths": _default_extra_paths(),
        "runs_dir": str(PROJECT_DIR / "runs"),
        "qth_dir": str(QTH_DIR),
        "lrp_dir": str(LRP_DIR),
    }


# --- Chemins relatifs ---------------------------------------------------------------
# Les chemins situés dans le répertoire de l'application sont enregistrés relativement à
# celui-ci (dossier déplaçable) et redeviennent absolus à la lecture. Les autres restent tels quels.

PARAM_PATHS = ("sdf_dir", "udt", "ani")
PARAM_PATH_LISTS = ("city_files", "boundary_files")


def relative(path):
    if not path or not os.path.isabs(path):
        return path
    try:
        inside = os.path.commonpath([os.path.normcase(os.path.normpath(path)),
                                     os.path.normcase(str(PROJECT_DIR))]) == os.path.normcase(str(PROJECT_DIR))
    except ValueError:                       # autre lecteur
        return path
    return os.path.relpath(path, PROJECT_DIR) if inside else path


def absolute(path):
    if not path or os.path.isabs(path):
        return path
    return os.path.normpath(PROJECT_DIR / path)


def _convert_path_list(value, convert):
    """Liste « ; » de dossiers (PATH)."""
    return ";".join(convert(p.strip()) for p in value.split(";") if p.strip())


def _convert_params(params, convert):
    if not isinstance(params, dict):
        return params
    params = copy.deepcopy(params)
    for key in PARAM_PATHS:
        if isinstance(params.get(key), str):
            params[key] = convert(params[key].strip())
    for key in PARAM_PATH_LISTS:
        if isinstance(params.get(key), list):
            params[key] = [convert(p) if isinstance(p, str) else p for p in params[key]]
    return params


def _convert_settings(settings, convert):
    settings = dict(settings)
    for key in FOLDER_SETTINGS:
        if isinstance(settings.get(key), str):
            settings[key] = convert(settings[key])
    if isinstance(settings.get("extra_paths"), dict):
        settings["extra_paths"] = {arch: _convert_path_list(value, convert) if isinstance(value, str) else value
                                   for arch, value in settings["extra_paths"].items()}
    settings["last_params"] = _convert_params(settings.get("last_params"), convert)
    return settings


def _convert_history(history, convert):
    result = []
    for entry in history:
        if isinstance(entry, dict):
            entry = dict(entry)
            if isinstance(entry.get("run_dir"), str):
                entry["run_dir"] = convert(entry["run_dir"])
            if "params" in entry:
                entry["params"] = _convert_params(entry["params"], convert)
        result.append(entry)
    return result


def _read_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return fallback


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# --- Réglages -----------------------------------------------------------------

# --- Lecture de l'ancien configuration.ini (reprise des réglages) -------------------

def _ini_parse(text):
    try:
        return json.loads(text)
    except ValueError:
        return text.replace("\\n", "\n")


def _new_parser():
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str                           # clés sensibles à la casse
    return parser


def _read_ini(path):
    parser = _new_parser()
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        return {}
    data = {}
    for section in parser.sections():
        values = {key: _ini_parse(value) for key, value in parser.items(section)}
        if section == "general":
            data.update(values)
        else:
            data[section] = values
    return data


def load_settings():
    settings = default_settings()
    if SETTINGS_FILE.exists():
        stored = _read_json(SETTINGS_FILE, {})
    elif LEGACY_INI_FILE.exists():
        stored = _read_ini(LEGACY_INI_FILE)
    else:
        stored = _read_json(LEGACY_SETTINGS_FILE, {})
    if isinstance(stored, dict):
        extra = stored.pop("extra_paths", None)
        settings.update(stored)
        if isinstance(extra, dict):
            settings["extra_paths"].update(extra)
    return _convert_settings(settings, absolute)


def apply_install_paths(settings):
    """Dossiers choisis dans l'installeur (install.json, écrit à l'installation) : reportés
    dans les réglages, qui sont enregistrés, puis le fichier est supprimé. Renvoie True s'il
    y en avait un."""
    if not INSTALL_FILE.exists():
        return False
    try:                                   # écrit par Inno Setup, éventuellement avec un BOM UTF-8
        data = json.loads(INSTALL_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict):
        for key in FOLDER_SETTINGS:
            if isinstance(data.get(key), str) and data[key].strip():
                settings[key] = absolute(data[key].strip())
        save_settings(settings)
    try:
        INSTALL_FILE.unlink()
    except OSError:
        pass
    return True


def save_settings(settings):
    _write_json(SETTINGS_FILE, _convert_settings(settings, relative))


# --- Reprise des profils et de l'historique de l'ancien emplacement ---------------

_migrated = False


def migrate_legacy_data():
    """Copie les profils et l'historique de %APPDATA%/SplatGui s'ils n'existent pas encore
    dans le répertoire de l'application."""
    global _migrated
    if _migrated:
        return
    _migrated = True
    legacy_profiles = LEGACY_DIR / "profiles"
    if legacy_profiles.is_dir() and LEGACY_DIR.resolve() != DATA_DIR.resolve():
        PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        for path in legacy_profiles.glob("*.json"):
            target = PROFILES_DIR / path.name
            if not target.exists():
                shutil.copy2(path, target)
    legacy_history = LEGACY_DIR / "history.json"
    if legacy_history.exists() and not HISTORY_FILE.exists() and LEGACY_DIR.resolve() != DATA_DIR.resolve():
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy_history, HISTORY_FILE)


# --- Profils --------------------------------------------------------------------

def _profile_path(name):
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "_"
    return PROFILES_DIR / f"{safe}.json"


def list_profiles():
    migrate_legacy_data()
    names = []
    if PROFILES_DIR.is_dir():
        for path in PROFILES_DIR.glob("*.json"):
            data = _read_json(path, None)
            if isinstance(data, dict):
                names.append(data.get("name", path.stem))
    if DEFAULT_PROFILE not in names:
        names.append(DEFAULT_PROFILE)
    return sorted(names, key=str.casefold)


def load_profile(name):
    migrate_legacy_data()
    data = _read_json(_profile_path(name), None)
    if not isinstance(data, dict):
        return default_params()
    return merge_defaults(_convert_params(data.get("params"), absolute))


def save_profile(name, params):
    _write_json(_profile_path(name), {"name": name, "params": _convert_params(params, relative)})


def delete_profile(name):
    try:
        _profile_path(name).unlink()
    except FileNotFoundError:
        pass


def export_profile(path, name, params):
    _write_json(Path(path), {"name": name, "params": _convert_params(params, relative)})


def import_profile(path):
    data = _read_json(Path(path), None)
    if not isinstance(data, dict) or "params" not in data:
        raise ValueError(tr("Fichier de profil invalide"))
    return data.get("name") or Path(path).stem, merge_defaults(_convert_params(data["params"], absolute))


# --- Historique -----------------------------------------------------------------

def load_history():
    migrate_legacy_data()
    data = _read_json(HISTORY_FILE, [])
    return _convert_history(data, absolute) if isinstance(data, list) else []


def _save_history(history):
    _write_json(HISTORY_FILE, _convert_history(history, relative))


def add_history(entry):
    history = load_history()
    history.insert(0, entry)
    _save_history(history[:HISTORY_MAX])
    return history[:HISTORY_MAX]


def update_history(run_dir, **fields):
    history = load_history()
    for entry in history:
        if entry.get("run_dir") == run_dir:
            entry.update(fields)
            break
    _save_history(history)
    return history


def remove_history(run_dir):
    history = [e for e in load_history() if e.get("run_dir") != run_dir]
    _save_history(history)
    return history
