# Compilation de SPLAT!Gui : pyinstaller SplatGui.spec --noconfirm
# Résultat : dist/SPLAT!Gui/SPLAT!Gui.exe, bibliothèques dans dist/SPLAT!Gui/lib (au lieu de _internal),
# données livrées (geojson, icons, antennas/MSI) copiées à côté de l'exécutable. Les répertoires de
# travail (bin, deps, tools, terrain, profiles, runs) sont créés au premier lancement.

import shutil
from pathlib import Path

ROOT = Path(SPECPATH)
NAME = "SPLAT!Gui"
DATA_DIRS = ("geojson", "icons", "antennas/MSI")    # antennes : modèles MSI livrés seulement (pas sources/ ni imports)

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "splatgui" / "resources" / "splash_map.png"), "splatgui/resources"),
           (str(ROOT / "icons" / "splat_icon.ico"), "icons"),           # icône de fenêtre de secours
           (str(ROOT / "splatgui" / "locales" / "*.json"), "splatgui/locales"),    # traductions
           (str(ROOT / "splatgui" / "help" / "*.html"), "splatgui/help"),          # aide intégrée (fr, en, es)
           (str(ROOT / "splatgui" / "help" / "*.pdf"), "splatgui/help"),           # documentation d'origine
           (str(ROOT / "LICENSE"), ".")],                                    # GNU GPL v2 (secours)
    excludes=["tkinter"],
    noarchive=False,
)

# Panorama 3D (Qt Quick 3D) : les hooks QML embarquent tous les modules QML de PyQt6 (Controls,
# Multimedia, Pdf, Physics…). Seuls QtQml, QtQuick et QtQuick3D (sans leurs sous-modules) sont
# gardés, puis les DLL de Qt qu'aucun fichier restant n'utilise sont retirées.
from PyInstaller.depend import bindepend

QML_DIR = "PyQt6/Qt6/qml/"
QML_KEEP = ("QtQml/", "QtQuick/qmldir", "QtQuick/plugins.qmltypes", "QtQuick/qtquick2plugin.dll",
            "QtQuick3D/qmldir", "QtQuick3D/Quick3D.qmltypes", "QtQuick3D/qquick3dplugin.dll")
QT_BIN = "PyQt6/Qt6/bin/"


def _dest(entry):
    return entry[0].replace("\\", "/")


def _keep_qml(entry):
    dest = _dest(entry)
    return not dest.startswith(QML_DIR) or dest[len(QML_DIR):].startswith(QML_KEEP)


a.datas = [e for e in a.datas if _keep_qml(e)]
a.binaries = [e for e in a.binaries if _keep_qml(e)]
qt_dlls = {Path(_dest(e)).name.lower(): e for e in a.binaries
           if _dest(e).startswith(QT_BIN) and Path(_dest(e)).name.lower().startswith("qt6")}
needed, todo = set(), [e[1] for e in a.binaries if e not in qt_dlls.values()]
while todo:
    for name in bindepend.get_imports(todo.pop()):
        name = (name if isinstance(name, str) else name[0]).lower()
        if name in qt_dlls and name not in needed:
            needed.add(name)
            todo.append(qt_dlls[name][1])
a.binaries = [e for e in a.binaries if e not in qt_dlls.values() or Path(_dest(e)).name.lower() in needed]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    icon=str(ROOT / "icons" / "splat_icon.ico"),
    console=False,
    upx=False,
    contents_directory="lib",
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)

dist = Path(DISTPATH) / NAME
for folder in DATA_DIRS:
    if (ROOT / folder).is_dir():
        shutil.copytree(ROOT / folder, dist / folder, dirs_exist_ok=True)
shutil.copy2(ROOT / "LICENSE", dist / "LICENSE")          # texte de la GNU GPL v2, à côté de l'exécutable
