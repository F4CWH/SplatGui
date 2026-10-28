#!/bin/sh
# Installe SPLAT!Gui pour l'utilisateur courant, sans droits administrateur :
#   ~/.local/share/splatgui/        application et ses données (profils, relief, résultats)
#   ~/.local/bin/splatgui           lien vers l'exécutable
#   ~/.local/share/applications/splatgui.desktop et l'icône : entrée du menu des applications
# Une mise à jour remplace l'application et garde les données. SPLAT! vient du paquet de la
# distribution : sudo apt install splat gnuplot
# Usage : ./install.sh [--uninstall]
set -eu

SRC=$(cd "$(dirname "$0")" && pwd)
DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}
DEST=$DATA_HOME/splatgui
BIN_DIR=$HOME/.local/bin
DESKTOP=$DATA_HOME/applications/splatgui.desktop
ICON=$DATA_HOME/icons/hicolor/256x256/apps/splatgui.png

refresh_menu() {
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DATA_HOME/applications" || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q "$DATA_HOME/icons/hicolor" || true
}

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$BIN_DIR/splatgui" "$DESKTOP" "$ICON"
    rm -rf "$DEST/lib" "$DEST/splatgui"
    refresh_menu
    echo "SPLAT!Gui désinstallé. Les données restent dans $DEST (à supprimer à la main si besoin)."
    exit 0
fi

if [ ! -x "$SRC/splatgui" ]; then
    echo "Exécutable splatgui introuvable à côté de install.sh." >&2
    exit 1
fi

if [ "$SRC" != "$DEST" ]; then
    mkdir -p "$DEST"
    rm -rf "$DEST/lib" "$DEST/splatgui"         # bibliothèques de la version précédente
    cp -R "$SRC/." "$DEST/"
fi

mkdir -p "$BIN_DIR" "$(dirname "$DESKTOP")" "$(dirname "$ICON")"
ln -sf "$DEST/splatgui" "$BIN_DIR/splatgui"
cp "$DEST/icons/splat_icon.png" "$ICON"
cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=SPLAT!Gui
GenericName=Radio propagation analysis
GenericName[fr]=Analyse de propagation radio
Comment=Graphical interface for SPLAT! (RF coverage and point-to-point links)
Comment[fr]=Interface graphique pour SPLAT! (couverture radio et liaisons point à point)
Exec="$DEST/splatgui"
Path=$DEST
Icon=splatgui
Terminal=false
Categories=Science;HamRadio;
StartupWMClass=splatgui
EOF
refresh_menu

echo "SPLAT!Gui installé dans $DEST"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "Remarque : $BIN_DIR n'est pas dans le PATH ; lancez SPLAT!Gui depuis le menu des applications." ;;
esac

# Dépendances système : SPLAT! (paquet de la distribution) et la bibliothèque requise par Qt sous X11.
missing=""
command -v splat >/dev/null 2>&1 || missing="$missing splat"
command -v gnuplot >/dev/null 2>&1 || missing="$missing gnuplot"
if ! /sbin/ldconfig -p 2>/dev/null | grep -q 'libxcb-cursor\.so\.0'; then
    missing="$missing libxcb-cursor0"
fi
if [ -n "$missing" ]; then
    echo "Paquets à installer :$missing"
    echo "    sudo apt install$missing"
fi
