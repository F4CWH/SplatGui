"""Différences entre Windows et Linux.

Windows : exécutables SPLAT! fournis dans bin/<arch>/ (portage MSYS / MinGW, avec leurs DLL).
Linux (Debian, Ubuntu) : exécutables du paquet de la distribution (`sudo apt install splat`),
cherchés dans le PATH ; une seule version, rangée sous la clé d'architecture « x64 ».
"""

import os
import shutil
from pathlib import Path

WINDOWS = os.name == "nt"
EXE_SUFFIX = ".exe" if WINDOWS else ""
INSTALL_COMMAND = "sudo apt install splat gnuplot"


def program_name(name):
    """Nom de fichier d'un exécutable : « splat » → « splat.exe » sous Windows."""
    return name + EXE_SUFFIX


def find_program(name, extra_paths=""):
    """Chemin d'un exécutable cherché dans `extra_paths` puis dans le PATH, ou None."""
    search = os.pathsep.join(p for p in (extra_paths.strip(), os.environ.get("PATH", "")) if p)
    found = shutil.which(program_name(name), path=search or None)
    return Path(found) if found else None


def split_paths(text):
    """Liste des dossiers d'une chaîne de type PATH (séparateur du système)."""
    return [p.strip() for p in text.split(os.pathsep) if p.strip()]


def join_paths(folders):
    return os.pathsep.join(folders)


def prepend_path(extra_paths, path):
    """PATH d'exécution : `extra_paths` placés devant `path`."""
    extra = join_paths(split_paths(extra_paths))
    return extra + os.pathsep + path if extra else path
