"""Pré-requis d'exécution : installation des exécutables SPLAT! et des DLL dont ils dépendent.

- Exécutables : extraits d'une archive (.zip, .tar.gz, .tar.bz2, .tar.xz) locale ou téléchargée.
  L'architecture de chaque .exe est lue dans son en-tête PE ; splat.exe / splat-hd.exe vont
  dans bin/<arch>/, les autres outils (srtm2sdf…) dans bin/<arch>/utils/, les DLL éventuelles
  dans deps/<arch>/.
- DLL : téléchargées dans deps/<arch>/ (dossier à ajouter au PATH d'exécution) :
    x64 : msys-2.0.dll, msys-stdc++-6.dll, msys-gcc_s-seh-1.dll, msys-z.dll… (dépôt MSYS2,
          SHA-256 vérifié)
    x86 : libstdc++-6.dll, libgcc_s_dw2-1.dll, libbz2-2.dll, zlib1.dll (MinGW.org, GCC 6.3.0)
- gnuplot (graphes point à point) : distribution Windows officielle (64 bits,
  SHA-256 vérifié) extraite dans gnuplot/, dont le dossier bin/ est ajouté au PATH d'exécution
  des deux architectures (gnuplot est lancé comme processus séparé par SPLAT!).
"""

import hashlib
import io
import lzma
import os
import shutil
import struct
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .storage import BIN_DIR, PROJECT_DIR
from .i18n import tr

DEPS_DIR = PROJECT_DIR / "deps"
TOOLS_DIR = PROJECT_DIR / "tools"
GNUPLOT_DIR = PROJECT_DIR / "gnuplot"

# Dernière version publiée en .zip (les suivantes ne sont qu'en .7z / installateur).
GNUPLOT_VERSION = "6.0.3"
GNUPLOT_URL = "https://downloads.sourceforge.net/project/gnuplot/gnuplot/6.0.3/gp603-win64-mingw.zip"
GNUPLOT_SHA256 = "215df79b3388d2e5e7335017267fbb305345c142676f0502c1ad237c23478ccc"
GNUPLOT_SKIPPED = ("docs/", "demo/")      # documentation et démos non installées

MSYS2_REPOS = {
    "msys": "https://repo.msys2.org/msys/x86_64/msys.db",
    "mingw32": "https://repo.msys2.org/mingw/mingw32/mingw32.db",
}
MINGW_REPO = "https://downloads.sourceforge.net/project/mingw/MinGW/"
# Paquets par architecture : (dépôt MSYS2, nom du paquet) ou (None, URL de l'archive).
DLL_PACKAGES = {
    "x64": [("msys", "msys2-runtime"), ("msys", "gcc-libs"),
            ("msys", "zlib")],                                         # utils/fontdata.exe
    "x86": [(None, MINGW_REPO + "Base/gcc/Version6/gcc-6.3.0/libgcc-6.3.0-1-mingw32-dll-1.tar.xz"),
            (None, MINGW_REPO + "Base/gcc/Version6/gcc-6.3.0/libstdc++-6.3.0-1-mingw32-dll-6.tar.xz"),
            (None, MINGW_REPO + "Extension/bzip2/bzip2-1.0.6-4/libbz2-1.0.6-4-mingw32-dll-2.tar.lzma"),
            ("mingw32", "mingw-w64-i686-zlib")],                       # zlib1.dll, utils/fontdata.exe
}

SPLAT_NAMES = {"splat.exe", "splat-hd.exe"}
PE_MACHINES = {0x8664: "x64", 0x14C: "x86"}


class Cancelled(Exception):
    pass


def dll_dir(arch):
    return DEPS_DIR / arch


def _label(path):
    return path.relative_to(PROJECT_DIR) if path.is_relative_to(PROJECT_DIR) else path


# --- En-têtes PE ------------------------------------------------------------------

def pe_info(data):
    """(architecture, DLL importées) d'un exécutable ou d'une DLL Windows ; (None, []) sinon."""
    try:
        if data[:2] != b"MZ":
            return None, []
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            return None, []
        arch = PE_MACHINES.get(struct.unpack_from("<H", data, pe + 4)[0])
        sections = struct.unpack_from("<H", data, pe + 6)[0]
        optional_size = struct.unpack_from("<H", data, pe + 20)[0]
        optional = pe + 24
        magic = struct.unpack_from("<H", data, optional)[0]
        directories = optional + (112 if magic == 0x20B else 96)
        import_rva = struct.unpack_from("<I", data, directories + 8)[0]
        table = [struct.unpack_from("<IIII", data, optional + optional_size + i * 40 + 8)
                 for i in range(sections)]

        def offset(rva):
            for virtual_size, address, raw_size, raw_offset in table:
                if address <= rva < address + max(virtual_size, raw_size):
                    return rva - address + raw_offset
            raise ValueError("RVA hors sections")

        imports = []
        if import_rva:
            entry = offset(import_rva)
            while True:
                name_rva = struct.unpack_from("<I", data, entry + 12)[0]
                if not name_rva:
                    break
                start = offset(name_rva)
                imports.append(data[start:data.index(b"\0", start)].decode("latin-1"))
                entry += 20
        return arch, imports
    except (struct.error, ValueError):
        return None, []


def _system_dir(arch):
    windir = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    # Un processus 32 bits sous Windows 64 bits voit SysWOW64 sous le nom System32.
    if arch == "x86" and (windir / "SysWOW64").is_dir():
        return windir / "SysWOW64"
    return windir / "System32"


def missing_dlls(arch, extra_paths=""):
    """DLL non système introuvables pour les exécutables installés de `arch` (dépendances
    indirectes comprises), cherchées comme à l'exécution : `extra_paths` puis le PATH.
    Une DLL d'une autre architecture ne compte pas."""
    folders = [Path(p.strip()) for p in (extra_paths + os.pathsep + os.environ.get("PATH", "")).split(os.pathsep)
               if p.strip()]
    system = _system_dir(arch)
    pending = installed_executables(arch)
    seen, missing = set(), set()
    while pending:
        path = pending.pop()
        _arch, imports = pe_info(path.read_bytes())
        for name in imports:
            key = name.lower()
            if key in seen or key.startswith(("api-ms-win-", "ext-ms-win-")):
                continue
            seen.add(key)
            if (system / name).exists():
                continue
            found = None
            for folder in folders:
                candidate = folder / name
                try:
                    if candidate.is_file() and pe_info(candidate.read_bytes())[0] == arch:
                        found = candidate
                        break
                except OSError:
                    continue
            if found is None:
                missing.add(name)
            else:
                pending.append(found)
    return sorted(missing, key=str.lower)


def check(extra_paths):
    """État des dépendances par architecture :
    {arch: {"splat": [...], "missing": [...], "gnuplot": chemin de gnuplot.exe ou None}}."""
    report = {}
    for arch in PE_MACHINES.values():
        names = {p.name.lower() for p in installed_executables(arch)}
        report[arch] = {"splat": sorted(SPLAT_NAMES & names),
                        "missing": missing_dlls(arch, extra_paths.get(arch, "")) if names else [],
                        "gnuplot": find_gnuplot(extra_paths.get(arch, ""))}
    return report


def gnuplot_missing(report):
    """Architectures de SPLAT! installées qui ne trouveront pas gnuplot."""
    return [arch for arch, state in report.items() if state["splat"] and not state["gnuplot"]]


def summary(report):
    """(tout est présent ?, message court) d'après `check`."""
    installed = [arch for arch, state in report.items() if state["splat"]]
    if not installed:
        return False, tr("SPLAT! absent (ni x64 ni x86)")
    problems = [tr("{n} DLL {arch} manquante(s)", n=len(state["missing"]), arch=arch)
                for arch, state in report.items() if state["splat"] and state["missing"]]
    if problems:
        return False, tr("Dépendances : ") + ", ".join(problems)
    # gnuplot est facultatif : le profil de liaison est tracé par SPLAT!Gui.
    note = tr(" ; gnuplot absent (graphes gnuplot facultatifs)") if gnuplot_missing(report) else ""
    return True, tr("Dépendances présentes (SPLAT! {arches})", arches=", ".join(installed)) + note


def installed_executables(arch):
    folder = BIN_DIR / arch
    return [p for p in (*folder.glob("*.exe"), *(folder / "utils").glob("*.exe")) if p.is_file()]


# --- Téléchargement ---------------------------------------------------------------

def fetch(url, log, cancel):
    """Contenu d'une URL, avec suivi de la progression."""
    log("  " + tr("Téléchargement : {url}", url=url) + "\n")
    request = urllib.request.Request(url, headers={"User-Agent": "SPLAT!Gui"})
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        buffer = io.BytesIO()
        next_report = 0.25
        while chunk := response.read(256 * 1024):
            if cancel():
                raise Cancelled()
            buffer.write(chunk)
            if total and buffer.tell() / total >= next_report:
                log(f"    {buffer.tell() / total:.0%} ({buffer.tell() / 1e6:.1f} Mo)\n")
                next_report += 0.25
    return buffer.getvalue()


def _write(target, data):
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def _archive_members(data, name):
    """Itère sur (nom, lecteur) des fichiers d'une archive zip ou tar (gz, bz2, xz, lzma, zst)."""
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    yield info.filename, lambda i=info: archive.read(i)
        return
    if name.lower().endswith(".lzma"):
        stream = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(data)
        archive = tarfile.open(fileobj=io.BytesIO(stream))
    else:
        try:
            archive = tarfile.open(fileobj=io.BytesIO(data))   # compression détectée
        except tarfile.TarError as exc:
            raise ValueError(tr("{name} : format d'archive non reconnu (zip ou tar attendu).", name=name)) from exc
    with archive:
        for member in archive:
            if member.isfile():
                yield member.name, lambda m=member: archive.extractfile(m).read()


# --- Installation des DLL ---------------------------------------------------------

def _msys2_index(db_url, log, cancel):
    """{nom du paquet: (fichier, sha256)} d'après la base d'un dépôt MSYS2."""
    index = {}
    for name, read in _archive_members(fetch(db_url, log, cancel), db_url):
        if not name.endswith("/desc"):
            continue
        fields = {}
        for block in read().decode("utf-8", "replace").strip().split("\n\n"):
            key, *values = block.split("\n")
            fields[key.strip("%")] = values
        if fields.get("NAME") and fields.get("FILENAME"):
            index[fields["NAME"][0]] = (fields["FILENAME"][0], (fields.get("SHA256SUM") or [""])[0])
    return index


def install_dlls(arch, log, cancel=lambda: False):
    """Télécharge les DLL d'exécution de SPLAT! pour `arch` dans deps/<arch>.
    Renvoie la liste des DLL installées."""
    if arch not in DLL_PACKAGES:
        raise ValueError(tr("Architecture inconnue : {arch}", arch=arch))
    indexes = {}
    sources = []
    for repo, package in DLL_PACKAGES[arch]:
        if repo is None:
            sources.append((package, ""))
            continue
        db_url = MSYS2_REPOS[repo]
        if repo not in indexes:
            log(tr("Lecture de la base du dépôt MSYS2 {repo}…", repo=repo) + "\n")
            indexes[repo] = _msys2_index(db_url, log, cancel)
        if package not in indexes[repo]:
            raise RuntimeError(tr("Paquet {package} absent du dépôt MSYS2 {repo}.", package=package, repo=repo))
        filename, sha256 = indexes[repo][package]
        sources.append((db_url.rsplit("/", 1)[0] + "/" + filename, sha256))

    installed = []
    for url, sha256 in sources:
        data = fetch(url, log, cancel)
        if sha256 and hashlib.sha256(data).hexdigest() != sha256:
            raise RuntimeError(tr("Somme SHA-256 incorrecte pour {url}", url=url))
        for name, read in _archive_members(data, url):
            if not name.lower().endswith(".dll") or "/bin/" not in "/" + name:
                continue
            content = read()
            if pe_info(content)[0] != arch:
                continue
            target = dll_dir(arch) / Path(name).name
            _write(target, content)
            installed.append(target.name)
            log(f"  → {_label(target)}\n")
    if not installed:
        raise RuntimeError(tr("Aucune DLL trouvée dans les paquets téléchargés."))
    return installed


# --- Installation des exécutables SPLAT! ----------------------------------------------

def install_splat(source, log, cancel=lambda: False):
    """Installe les exécutables d'une archive (chemin local ou URL http/https).
    Renvoie {arch: [fichiers installés]}."""
    source = str(source).strip()
    if source.lower().startswith(("http://", "https://")):
        data = fetch(source, log, cancel)
        name = source.split("?", 1)[0]
    else:
        log(tr("Lecture de {source}", source=source) + "\n")
        data = Path(source).read_bytes()
        name = source

    installed = {}
    for member, read in _archive_members(data, name):
        if cancel():
            raise Cancelled()
        base = Path(member).name
        lower = base.lower()
        if not lower.endswith((".exe", ".dll")):
            continue
        content = read()
        arch, _imports = pe_info(content)
        if arch is None:
            log("  " + tr("{member} ignoré (pas un exécutable Windows x86/x64)", member=member) + "\n")
            continue
        if lower.endswith(".dll"):
            target = dll_dir(arch) / base
        elif lower in SPLAT_NAMES:
            target = BIN_DIR / arch / lower
        else:
            target = BIN_DIR / arch / "utils" / base
        _write(target, content)
        installed.setdefault(arch, []).append(str(_label(target)))
        log(f"  {member} → {_label(target)}\n")
        if lower.startswith("srtm2sdf"):
            # Copie renommée de srtm2sdf (mode HD) dérivée de l'ancien exécutable.
            (TOOLS_DIR / arch / "srtm2sdf-hd.exe").unlink(missing_ok=True)

    if not any(Path(f).name.lower() in SPLAT_NAMES for files in installed.values() for f in files):
        raise RuntimeError(tr("L'archive ne contient ni splat.exe ni splat-hd.exe."))
    return installed


def _path_with(extra_paths, folder, first):
    """Chaîne PATH complétée par `folder` (en tête ou en fin) s'il n'y figure pas."""
    folder = str(folder)
    current = [p for p in extra_paths.split(";") if p.strip()]
    if any(os.path.normcase(os.path.normpath(p)) == os.path.normcase(os.path.normpath(folder)) for p in current):
        return ";".join(current)
    return ";".join([folder] + current if first else current + [folder])


def add_to_path_setting(extra_paths, arch):
    """Chaîne PATH de `arch` complétée par deps/<arch> (en tête) s'il n'y figure pas."""
    return _path_with(extra_paths, dll_dir(arch), first=True)


# --- gnuplot ----------------------------------------------------------------------

def gnuplot_bin():
    return GNUPLOT_DIR / "bin"


def find_gnuplot(extra_paths=""):
    """Chemin de gnuplot.exe tel que SPLAT! le trouvera (`extra_paths` puis le PATH), ou None."""
    for folder in (extra_paths + os.pathsep + os.environ.get("PATH", "")).split(os.pathsep):
        if folder.strip() and (Path(folder.strip()) / "gnuplot.exe").is_file():
            return Path(folder.strip()) / "gnuplot.exe"
    return None


def add_gnuplot_to_path_setting(extra_paths):
    """Chaîne PATH complétée par gnuplot/bin (en fin : ses DLL ne masquent pas celles de SPLAT!)."""
    return _path_with(extra_paths, gnuplot_bin(), first=False)


def install_gnuplot(log, cancel=lambda: False):
    """Télécharge la distribution Windows de gnuplot et l'installe dans gnuplot/ (remplace
    une installation précédente). Renvoie le chemin de gnuplot.exe."""
    data = fetch(GNUPLOT_URL, log, cancel)
    if hashlib.sha256(data).hexdigest() != GNUPLOT_SHA256:
        raise RuntimeError(tr("Somme SHA-256 incorrecte pour {url}", url=GNUPLOT_URL))
    log(tr("Extraction dans {folder}…", folder=_label(GNUPLOT_DIR)) + "\n")
    staging = GNUPLOT_DIR.with_name(GNUPLOT_DIR.name + ".part")
    shutil.rmtree(staging, ignore_errors=True)
    count = 0
    for name, read in _archive_members(data, GNUPLOT_URL):
        if cancel():
            shutil.rmtree(staging, ignore_errors=True)
            raise Cancelled()
        relative = name.split("/", 1)[1] if name.startswith("gnuplot/") else name
        if not relative or relative.startswith(GNUPLOT_SKIPPED) or ".." in Path(relative).parts:
            continue
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(read())
        count += 1
    if not (staging / "bin" / "gnuplot.exe").is_file():
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(tr("L'archive ne contient pas gnuplot.exe."))
    shutil.rmtree(GNUPLOT_DIR, ignore_errors=True)
    os.replace(staging, GNUPLOT_DIR)
    log(tr("{n} fichiers installés.", n=count) + "\n")
    return str(gnuplot_bin() / "gnuplot.exe")
