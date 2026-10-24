"""Conversion de tuiles DTED (.dt0, .dt1, .dt2) en fichiers SDF de SPLAT! (voir splatgui/dted.py).

Usage :
    python tools/dt2sdf.py FICHIER_OU_DOSSIER [...] [-o DOSSIER_SDF] [--hd | --standard]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from splatgui import dted, terrain  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Convertit des tuiles DTED en SDF pour SPLAT!.")
    parser.add_argument("inputs", nargs="+", type=Path, help="fichiers .dt0/.dt1/.dt2 ou dossiers")
    parser.add_argument("-o", "--out", type=Path, default=terrain.SDF_DIR, help="dossier des SDF")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--hd", dest="hd", action="store_true", default=None, help="forcer le SDF HD (1\")")
    mode.add_argument("--standard", dest="hd", action="store_false", help="forcer le SDF standard (3\")")
    args = parser.parse_args()

    files = dted.find_files(args.inputs)
    if not files:
        sys.exit("Aucun fichier DTED trouvé.")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    summary = dted.convert_files(files, args.out, args.hd, log=lambda text: print(text, end="", flush=True))
    sys.exit(1 if summary["failed"] else 0)


if __name__ == "__main__":
    main()
