#!/bin/sh
# Compile SPLAT!Gui sous Linux (PyInstaller) puis produit l'archive à distribuer :
#   dist/SPLAT!Gui/                                    application (exécutable splatgui, install.sh)
#   dist/SPLAT!Gui-<version>-linux-<arch>.tar.gz       archive : décompresser puis lancer ./install.sh
# Compiler sur la distribution la plus ancienne visée (glibc), par exemple Debian 12 ou Ubuntu 22.04.
# Usage : sh installer/build_linux.sh [--skip-pyinstaller]
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
VERSION=$(sed -n 's/^__version__ *= *"\([^"]*\)".*/\1/p' "$ROOT/splatgui/__init__.py")
ARCH=$(uname -m)
DIST=$ROOT/dist
NAME='SPLAT!Gui'
APP=$DIST/$NAME
ARCHIVE=$DIST/$NAME-$VERSION-linux-$ARCH.tar.gz

if [ "${1:-}" != "--skip-pyinstaller" ]; then
    PYINSTALLER=$ROOT/.venv/bin/pyinstaller
    if [ ! -x "$PYINSTALLER" ]; then
        echo "PyInstaller introuvable : $ROOT/.venv/bin/pip install -r requirements.txt pyinstaller" >&2
        exit 1
    fi
    "$PYINSTALLER" "$ROOT/SplatGui.spec" --noconfirm --distpath "$DIST" --workpath "$ROOT/build"
fi

if [ ! -x "$APP/splatgui" ]; then
    echo "Application compilée introuvable : $APP/splatgui" >&2
    exit 1
fi

cp "$ROOT/installer/linux/install.sh" "$APP/install.sh"
chmod 755 "$APP/install.sh" "$APP/splatgui"

# Dossier racine de l'archive : SPLAT!Gui-<version>.
rm -f "$ARCHIVE"
tar -C "$DIST" --transform "s|^$NAME|$NAME-$VERSION|" -czf "$ARCHIVE" "$NAME"
echo "Application : $APP"
echo "Archive     : $ARCHIVE"
