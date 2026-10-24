"""Conversion de modèles de terrain GeoTIFF (.tif) et ESRI ASCII Grid (.asc) en fichiers SDF de
SPLAT! (voir splatgui/demimport.py). Les tuiles DTED se convertissent avec tools/dt2sdf.py.

Usage :
    python tools/dem2sdf.py FICHIER_OU_DOSSIER [...] [-o DOSSIER_SDF] [--hd]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from splatgui import demimport, terrain  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Convertit des GeoTIFF / ASCII Grid en SDF pour SPLAT!.")
    parser.add_argument("inputs", nargs="+", type=Path, help="fichiers .tif/.asc ou dossiers")
    parser.add_argument("-o", "--out", type=Path, default=terrain.SDF_DIR, help="dossier des SDF")
    parser.add_argument("--hd", action="store_true", help="SDF HD (1\") pour splat-hd ; standard (3\") sinon")
    args = parser.parse_args()

    files = demimport.find_files(args.inputs)
    if not files:
        sys.exit("Aucun fichier GeoTIFF ou ASCII Grid trouvé.")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    summary = demimport.import_files(files, args.out, args.hd, log=lambda text: print(text, end="", flush=True))
    sys.exit(1 if summary["failed"] or not summary["converted"] else 0)


if __name__ == "__main__":
    main()
