"""Modèles d'antennes : importation des diagrammes de rayonnement courants, bibliothèque et
conversion au format SPLAT! (.az / .el).

Représentation interne d'un diagramme (indépendante du format d'origine) :
- `az` : 360 valeurs en dB relatifs au maximum (≤ 0), indice = angle en degrés mesuré
  dans le sens horaire (vu de dessus) depuis la direction du lobe principal ;
- `el` : liste [(élévation, dB relatifs)], élévation en degrés AU-DESSUS de l'horizon
  (positive vers le ciel, négative vers le sol), de -90 à +90, dans la direction du lobe ;
- `gain_dbi` : gain maximal (dBi) si connu, `frequency_mhz` si connue.

Formats reconnus :
- MSI / Planet (.msi, .pln, .txt…) : fabricants d'antennes (Kathrein, CommScope, RFS…) ;
- sortie NEC-2 (.out, .nou) : 4nec2, nec2c, xnec2c, NEC-4 (tableau « RADIATION PATTERNS ») ;
- tableau de champ lointain d'EZNEC (« FF Tab », diagramme azimutal ou d'élévation) ;
- fichiers SPLAT! .az / .el ;
- texte / CSV générique à deux colonnes (angle, valeur en dB ou linéaire).

Format SPLAT! (documentation) :
- .az : 1re ligne = rotation (azimut du lobe principal, degrés depuis le nord, sens horaire),
  puis « azimut  champ relatif (0 à 1, tension) » de 0 à 359 ;
- .el : 1re ligne = inclinaison mécanique (positive vers le bas) et azimut de l'inclinaison,
  puis « angle  champ relatif » de -10 à +90, angle NÉGATIF = AU-DESSUS de l'horizon.
"""

import json
import re
import shutil
from pathlib import Path

import numpy as np

from .storage import PROJECT_DIR
from .i18n import N_, tr

LIBRARY_DIR = PROJECT_DIR / "antennas"
SOURCES_DIR = LIBRARY_DIR / "sources"
FLOOR_DB = -60.0          # plancher (au-delà : considéré comme nul)
DBD_TO_DBI = 2.15

FILE_FILTER = N_("Diagrammes d'antenne (*.msi *.pln *.ant *.out *.nou *.txt *.csv *.dat *.az *.el);;"
               "MSI / Planet (*.msi *.pln *.ant *.txt);;Sortie NEC-2 / 4nec2 (*.out *.nou);;"
               "EZNEC FF Tab (*.txt *.dat);;SPLAT! (*.az *.el);;Texte / CSV (*.txt *.csv *.dat);;"
               "Tous les fichiers (*)")


class PatternError(ValueError):
    pass


# --- Modèle -----------------------------------------------------------------------

def _clean_db(values):
    arr = np.asarray(values, dtype=float)
    arr = np.where(np.isfinite(arr), arr, FLOOR_DB)
    return np.maximum(arr - np.max(arr), FLOOR_DB)


def _resample_az(angles, values):
    """Valeurs (dB) aux angles 0..359 par interpolation circulaire."""
    angles = np.mod(np.asarray(angles, dtype=float), 360.0)
    order = np.argsort(angles)
    angles, values = angles[order], np.asarray(values, dtype=float)[order]
    angles, unique = np.unique(angles, return_index=True)
    values = values[unique]
    if len(angles) == 1:
        return np.full(360, values[0])
    xs = np.concatenate([angles - 360, angles, angles + 360])
    ys = np.concatenate([values, values, values])
    return np.interp(np.arange(360), xs, ys)


def make_pattern(name, az_angles=None, az_db=None, el_angles=None, el_db=None, **meta):
    """Construit un diagramme normalisé. Angles d'azimut : sens horaire, quelconque origine
    (ramenée au lobe principal) ; élévations : au-dessus de l'horizon."""
    pattern = {"name": name, "az": None, "el": None, "gain_dbi": None, "frequency_mhz": None,
               "source_format": "", "source_file": "", "comment": "", "az_reference": 0.0}
    pattern.update({k: v for k, v in meta.items() if v is not None})
    if az_angles is not None and len(az_angles):
        az = _resample_az(az_angles, az_db)
        peak = int(np.argmax(az))
        pattern["az_reference"] = float(peak)        # direction du lobe dans le repère d'origine
        pattern["az"] = [round(float(v), 3) for v in _clean_db(np.roll(az, -peak))]
    if el_angles is not None and len(el_angles):
        el_angles = np.asarray(el_angles, dtype=float)
        keep = (el_angles >= -90) & (el_angles <= 90)
        el_angles, el_values = el_angles[keep], np.asarray(el_db, dtype=float)[keep]
        order = np.argsort(el_angles)
        el_angles, unique = np.unique(el_angles[order], return_index=True)
        values = _clean_db(el_values[order][unique])
        pattern["el"] = [[round(float(a), 3), round(float(v), 3)] for a, v in zip(el_angles, values)]
    if pattern["az"] is None and pattern["el"] is None:
        raise PatternError(tr("aucune donnée de diagramme"))
    return pattern


def mirror_az(pattern):
    """Inverse le sens de l'azimut (sens horaire <-> antihoraire)."""
    if pattern.get("az"):
        az = pattern["az"]
        pattern["az"] = [az[0]] + az[:0:-1]
    return pattern


def az_array(pattern):
    return np.asarray(pattern["az"], dtype=float) if pattern.get("az") else np.zeros(360)


def el_curve(pattern):
    """(élévations, dB) ; isotrope dans le plan vertical si absent."""
    if pattern.get("el"):
        data = np.asarray(pattern["el"], dtype=float)
        return data[:, 0], data[:, 1]
    return np.array([-90.0, 90.0]), np.array([0.0, 0.0])


def el_value(pattern, elevation):
    angles, values = el_curve(pattern)
    return np.interp(elevation, angles, values, left=values[0], right=values[-1])


def beamwidth(values, angles, center):
    """Ouverture à -3 dB autour de `center` (degrés), ou None."""
    values = np.asarray(values, dtype=float)
    angles = np.asarray(angles, dtype=float)
    if len(values) < 3 or np.max(values) - np.min(values) < 3:
        return None
    i0 = int(np.argmin(np.abs(angles - center)))
    lo = hi = i0
    while lo > 0 and values[lo] > -3:
        lo -= 1
    while hi < len(values) - 1 and values[hi] > -3:
        hi += 1
    if values[lo] > -3 or values[hi] > -3:
        return None
    return float(angles[hi] - angles[lo])


def summary(pattern):
    """Caractéristiques principales, pour affichage."""
    info = {}
    if pattern.get("az"):
        az = az_array(pattern)
        extended = np.concatenate([az[180:], az[:180]])
        bw = beamwidth(extended, np.arange(-180, 180), 0)
        info["ouverture_h"] = bw
        info["avant_arriere"] = float(-az[180]) if az[180] < 0 else 0.0
    if pattern.get("el"):
        angles, values = el_curve(pattern)
        peak = float(angles[int(np.argmax(values))])
        info["elevation_max"] = peak
        info["ouverture_v"] = beamwidth(values, angles, peak)
    return info


# --- Lecture des formats ------------------------------------------------------------

_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"


def _floats(line):
    return [float(x) for x in re.findall(_NUM, line)]


def _read_text(path):
    data = Path(path).read_bytes()
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", "replace")


def parse_msi(text, name):
    """MSI / Planet. HORIZONTAL : angle (sens antihoraire depuis le lobe) et atténuation (dB,
    positive) ; VERTICAL : 0 = horizon devant, 90 = vers le sol, 270 = vers le ciel."""
    meta = {"source_format": "MSI / Planet"}
    section, horizontal, vertical = None, [], []
    gain = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        key = line.split()[0].upper()
        rest = line[len(line.split()[0]):].strip()
        if key == "NAME" and rest:
            name = rest
        elif key == "FREQUENCY":
            values = _floats(rest)
            if values:
                meta["frequency_mhz"] = values[0]
        elif key == "GAIN":
            values = _floats(rest)
            if values:
                gain = values[0] + (0.0 if "DBI" in rest.upper() else DBD_TO_DBI)
        elif key == "COMMENT":
            meta["comment"] = rest
        elif key == "HORIZONTAL":
            section = "h"
        elif key == "VERTICAL":
            section = "v"
        elif section and re.match(_NUM, line):
            values = _floats(line)
            if len(values) >= 2:
                (horizontal if section == "h" else vertical).append(values[:2])
            else:
                section = None
    if not horizontal and not vertical:
        raise PatternError(tr("sections HORIZONTAL / VERTICAL introuvables"))
    az_angles = az_db = el_angles = el_db = None
    if horizontal:
        h = np.asarray(horizontal)
        az_angles, az_db = -h[:, 0], -h[:, 1]          # antihoraire -> horaire ; atténuation -> dB
    if vertical:
        v = np.asarray(vertical)
        angle = np.mod(v[:, 0], 360)
        front = (angle <= 90) | (angle >= 270)
        elev = np.where(angle <= 90, -angle, 360 - angle)
        el_angles, el_db = elev[front], -v[front, 1]
    return [make_pattern(name, az_angles, az_db, el_angles, el_db, gain_dbi=gain, **meta)]


def parse_nec(text, name):
    """Sortie NEC-2 / NEC-4 (4nec2, nec2c, xnec2c) : un diagramme par tableau
    « RADIATION PATTERNS ». THETA : angle depuis le zénith ; PHI : azimut antihoraire depuis +X."""
    lines = text.splitlines()
    frequency = None
    patterns = []
    i = 0
    while i < len(lines):
        line = lines[i]
        match = re.search(r"FREQUENCY\s*[=:]\s*(" + _NUM + r")\s*MHZ", line, re.IGNORECASE)
        if match:
            frequency = float(match.group(1))
        if "RADIATION PATTERNS" in line.upper():
            rows = []
            j = i + 1
            started = False
            while j < len(lines):
                values = _floats(lines[j])
                text_line = lines[j].strip()
                if len(values) >= 5 and not re.search(r"[A-DF-Za-df-z]{4,}", text_line.split()[0]):
                    rows.append(values[:5])
                    started = True
                elif started and (not text_line or "*" in text_line or "PATTERN" in text_line.upper()
                                  or len(values) < 5):
                    break
                j += 1
            i = j
            if not rows:
                continue
            data = np.asarray(rows)
            theta, phi, total = data[:, 0], data[:, 1], data[:, 4]
            valid = total > -900
            if not valid.any():
                continue
            k = int(np.argmax(np.where(valid, total, -1e9)))
            theta_max, phi_max, peak = theta[k], phi[k], total[k]
            az_sel = valid & np.isclose(theta, theta_max)
            az_angles = az_db = None
            if np.unique(np.round(phi[az_sel], 3)).size >= 3:
                az_angles, az_db = phi_max - phi[az_sel], total[az_sel]   # antihoraire -> horaire
            # Élévation dans la direction du lobe ; THETA négatif = côté opposé.
            front = valid & (((np.isclose(phi, phi_max)) & (theta >= 0)) |
                             ((np.isclose(np.mod(phi - phi_max, 360), 180)) & (theta < 0)))
            el_angles = el_db = None
            if np.count_nonzero(front) >= 3:
                el_angles, el_db = 90 - np.abs(theta[front]), total[front]
            label = name if frequency is None else f"{name} ({frequency:g} MHz)"
            try:
                patterns.append(make_pattern(label, az_angles, az_db, el_angles, el_db,
                                             gain_dbi=float(peak), frequency_mhz=frequency,
                                             source_format="NEC-2 / 4nec2",
                                             comment=f"lobe : θ={theta_max:g}°, φ={phi_max:g}°"))
            except PatternError:
                pass
            continue
        i += 1
    if not patterns:
        raise PatternError(tr("aucun tableau RADIATION PATTERNS exploitable"))
    return patterns


def parse_eznec(text, name):
    """Tableau de champ lointain EZNEC (FF Tab). Diagramme azimutal : azimut antihoraire
    depuis +X ; diagramme d'élévation : 0 = horizon, 90 = zénith, 180 = horizon arrière."""
    kind = None
    upper = text.upper()
    if re.search(r"AZIMUTH\s+(PLOT|PATTERN)", upper):
        kind = "az"
    elif re.search(r"ELEVATION\s+(PLOT|PATTERN)", upper):
        kind = "el"
    frequency = None
    match = re.search(r"FREQUENCY\s*[=:]?\s*(" + _NUM + r")\s*MHZ", text, re.IGNORECASE)
    if match:
        frequency = float(match.group(1))
    groups, tot_index, rows = 1, None, []
    for line in text.splitlines():
        tokens = line.split()
        if tokens and tokens[0].lower().startswith("deg") and any(t.lower().startswith("tot") for t in tokens):
            groups = max(1, sum(1 for t in tokens if t.lower().startswith("deg")))
            per_group = [t for t in tokens[: len(tokens) // groups] if not t.lower().startswith("db")]
            tot_index = next(i for i, t in enumerate(per_group) if t.lower().startswith("tot"))
            width = len(per_group)
            continue
        if tot_index is not None:
            values = _floats(line)
            if values and len(values) % width == 0 and re.match(r"^\s*" + _NUM, line):
                for g in range(len(values) // width):
                    chunk = values[g * width:(g + 1) * width]
                    rows.append((chunk[0], chunk[tot_index]))
    if not rows:
        raise PatternError(tr("tableau « Deg … Tot dB » introuvable"))
    data = np.asarray(rows)
    angles, total = data[:, 0], data[:, 1]
    valid = total > -900
    angles, total = angles[valid], total[valid]
    peak = float(np.max(total))
    if kind is None:   # sans en-tête explicite : 0..360 = azimut, sinon élévation
        kind = "az" if np.ptp(angles) > 200 else "el"
    meta = dict(gain_dbi=peak, frequency_mhz=frequency, source_format=N_("EZNEC (azimut)") if kind == "az" else N_("EZNEC (élévation)"))
    if kind == "az":
        return [make_pattern(name, -angles, total, **meta)]
    a = np.mod(angles, 360)
    front = (a <= 90) | (a >= 270)
    elev = np.where(a <= 90, a, a - 360)
    return [make_pattern(name, el_angles=elev[front], el_db=total[front], **meta)]


def _to_db(values, mode="auto"):
    values = np.asarray(values, dtype=float)
    if mode == "auto":
        mode = "lin" if values.min() >= 0 and values.max() <= 1.0001 else "db"
    if mode == "lin":
        return 20 * np.log10(np.maximum(values, 1e-6))
    if mode == "att":
        return -values
    return values


def parse_splat(text, name, kind):
    lines = [l for l in text.splitlines() if l.strip()]
    first = _floats(lines[0])
    rows = np.asarray([_floats(l)[:2] for l in lines[1:] if len(_floats(l)) >= 2])
    if not len(rows):
        raise PatternError(tr("fichier SPLAT! vide"))
    db = _to_db(rows[:, 1], "lin")
    if kind == "az":
        pattern = make_pattern(name, rows[:, 0], db, source_format="SPLAT! .az")
        pattern["suggested_azimuth"] = (first[0] if first else 0.0) + pattern["az_reference"]
    else:
        pattern = make_pattern(name, el_angles=-rows[:, 0], el_db=db, source_format="SPLAT! .el")
        if first:
            pattern["suggested_tilt"] = first[0]
    return [pattern]


def parse_generic(text, name, plane="az", values="auto", clockwise=True, elevation_up=True):
    """Deux colonnes (angle, valeur), séparateurs espace / ; / , / tabulation."""
    rows = []
    for line in text.splitlines():
        if not re.match(r"^\s*" + _NUM, line):
            continue
        numbers = _floats(line.replace(";", " ").replace(",", " ") if line.count(",") > 1 or ";" in line
                          else line.replace(",", "."))
        if len(numbers) >= 2:
            rows.append(numbers[:2])
    if len(rows) < 3:
        raise PatternError(tr("moins de trois lignes « angle valeur »"))
    data = np.asarray(rows)
    db = _to_db(data[:, 1], values)
    if plane == "az":
        angles = data[:, 0] if clockwise else -data[:, 0]
        return [make_pattern(name, angles, db, source_format=N_("Texte (azimut)"))]
    angles = data[:, 0] if elevation_up else -data[:, 0]
    return [make_pattern(name, el_angles=angles, el_db=db, source_format=N_("Texte (élévation)"))]


def detect_format(path, text):
    upper = text.upper()
    suffix = Path(path).suffix.lower()
    if suffix == ".az":
        return "splat_az"
    if suffix == ".el":
        return "splat_el"
    if "RADIATION PATTERNS" in upper and "THETA" in upper:
        return "nec"
    if re.search(r"^\s*HORIZONTAL\s+\d+", text, re.IGNORECASE | re.MULTILINE) or \
            re.search(r"^\s*VERTICAL\s+\d+", text, re.IGNORECASE | re.MULTILINE):
        return "msi"
    if "EZNEC" in upper or re.search(r"^\s*DEG\b.*\bTOT", text, re.IGNORECASE | re.MULTILINE):
        return "eznec"
    return "generic"


FORMAT_LABELS = {"msi": "MSI / Planet", "nec": "Sortie NEC-2 / 4nec2", "eznec": "EZNEC (FF Tab)",
                 "splat_az": "SPLAT! .az", "splat_el": "SPLAT! .el", "generic": "Texte / CSV"}


def import_file(path, generic_options=None):
    """Lit un fichier de diagramme ; renvoie (format détecté, [diagrammes])."""
    text = _read_text(path)
    name = Path(path).stem
    fmt = detect_format(path, text)
    if fmt == "msi":
        patterns = parse_msi(text, name)
    elif fmt == "nec":
        patterns = parse_nec(text, name)
    elif fmt == "eznec":
        patterns = parse_eznec(text, name)
    elif fmt in ("splat_az", "splat_el"):
        patterns = parse_splat(text, name, fmt[-2:])
    else:
        patterns = parse_generic(text, name, **(generic_options or {}))
    for pattern in patterns:
        pattern["source_file"] = Path(path).name
    return fmt, patterns


def merge(base, other):
    """Complète `base` avec le plan (azimut / élévation) fourni par `other`."""
    for key in ("az", "el"):
        if other.get(key):
            base[key] = other[key]
    for key in ("gain_dbi", "frequency_mhz"):
        if base.get(key) is None and other.get(key) is not None:
            base[key] = other[key]
    formats = {base.get("source_format", ""), other.get("source_format", "")} - {""}
    base["source_format"] = " + ".join(sorted(formats))
    files = [f for f in (base.get("source_file"), other.get("source_file")) if f]
    base["source_file"] = " + ".join(dict.fromkeys(files))
    return base


# --- Écriture SPLAT! -------------------------------------------------------------

def splat_az_text(pattern, azimuth):
    lines = [f"{azimuth % 360:.1f}"]
    az = az_array(pattern)
    for angle in range(360):
        lines.append(f"{angle}\t{10 ** (az[angle] / 20):.4f}")
    return "\n".join(lines) + "\n"


def splat_el_text(pattern, tilt, azimuth):
    """Élévations SPLAT! de -10 à +90 (positif = sous l'horizon), pas de 0,5°."""
    lines = [f"{tilt:.1f}\t{azimuth % 360:.1f}"]
    for step in range(0, 201):
        splat_angle = -10 + step * 0.5
        value = el_value(pattern, -splat_angle)
        lines.append(f"{splat_angle:.1f}\t{10 ** (value / 20):.4f}")
    return "\n".join(lines) + "\n"


def write_splat_files(pattern, base_path, azimuth=0.0, tilt=0.0):
    """Écrit <base>.az et <base>.el ; renvoie les chemins."""
    base_path = Path(base_path)          # extensions ajoutées, pas substituées (« YAGI_10.25dBi »)
    az_path, el_path = base_path.with_name(base_path.name + ".az"), base_path.with_name(base_path.name + ".el")
    az_path.write_text(splat_az_text(pattern, azimuth), encoding="ascii")
    el_path.write_text(splat_el_text(pattern, tilt, azimuth), encoding="ascii")
    return az_path, el_path


# --- Bibliothèque ------------------------------------------------------------------

def _safe(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "antenne"


def library_names():
    if not LIBRARY_DIR.is_dir():
        return []
    names = []
    for path in LIBRARY_DIR.glob("*.json"):
        try:
            names.append(json.loads(path.read_text(encoding="utf-8"))["name"])
        except (OSError, ValueError, KeyError):
            continue
    return sorted(names, key=str.casefold)


def load(name):
    path = LIBRARY_DIR / f"{_safe(name)}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save(pattern, source_path=None):
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    (LIBRARY_DIR / f"{_safe(pattern['name'])}.json").write_text(
        json.dumps(pattern, ensure_ascii=False, indent=1), encoding="utf-8")
    if source_path:
        SOURCES_DIR.mkdir(parents=True, exist_ok=True)
        target = SOURCES_DIR / Path(source_path).name
        if Path(source_path).resolve() != target.resolve():
            shutil.copy2(source_path, target)


def delete(name):
    (LIBRARY_DIR / f"{_safe(name)}.json").unlink(missing_ok=True)


def rename(old, new):
    pattern = load(old)
    if pattern is None:
        return
    pattern["name"] = new
    save(pattern)
    if _safe(old) != _safe(new):
        delete(old)


def deg_to_text(value):
    return "—" if value is None else f"{value:.1f}°"


def describe(pattern):
    info = summary(pattern)
    parts = [" + ".join(tr(f) for f in (pattern.get("source_format") or "?").split(" + "))]
    if pattern.get("frequency_mhz"):
        parts.append(f"{pattern['frequency_mhz']:g} MHz")
    if pattern.get("gain_dbi") is not None:
        parts.append(tr("gain {dbi:.2f} dBi ({dbd:.2f} dBd)", dbi=pattern["gain_dbi"], dbd=pattern["gain_dbi"] - DBD_TO_DBI))
    if "ouverture_h" in info:
        parts.append(tr("ouverture H {value}", value=deg_to_text(info["ouverture_h"])))
        parts.append(tr("avant/arrière {value:.1f} dB", value=info["avant_arriere"]))
    if "ouverture_v" in info:
        parts.append(tr("ouverture V {value}", value=deg_to_text(info["ouverture_v"])))
        parts.append(tr("lobe à {value:+.1f}° d'élévation", value=info["elevation_max"]))
    if not pattern.get("az"):
        parts.append(tr("pas de diagramme horizontal (omnidirectionnel)"))
    if not pattern.get("el"):
        parts.append(tr("pas de diagramme vertical"))
    return " — ".join(parts)

